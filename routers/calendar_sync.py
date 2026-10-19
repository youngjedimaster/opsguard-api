from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import settings
from database import get_db
from deps import get_admin_user

router = APIRouter(prefix="/api/calendar-sync", tags=["calendar-sync"])

SHIFT_TITLE_RE = re.compile(r"^\s*(S[1-9]\d*)\s*(?::\s*(.+?))?\s*$", re.IGNORECASE)

def _valid_shift_code(value: Any) -> bool:
    return bool(re.fullmatch(r"S[1-9]\d*", str(value or "").strip().upper()))



class PreviewRequest(BaseModel):
    from_date: date
    to_date: date


class ImportDraftItem(BaseModel):
    calendar_id: str
    google_event_id: str


class ImportDraftRequest(BaseModel):
    items: list[ImportDraftItem]


class PromoteDraftRequest(BaseModel):
    guard_id: str
    end_time: str = ""
    note: str = ""


def _calendar_map() -> dict[str, str]:
    titan = str(getattr(settings, "CALENDAR_TITAN_SECURITY_ID", "") or "").strip()
    return {
        "Garrison": str(getattr(settings, "CALENDAR_GARRISON_ID", "") or "").strip(),
        "Baby G": str(getattr(settings, "CALENDAR_BABY_G_ID", "") or "").strip(),
        "St Anne's": titan,
        "Jean Darlene": titan,
        "Get Well": titan,
        "Standard Time": titan,
        "Two Kats": titan,
    }


def _calendar_sources() -> list[dict]:
    return [
        {"name": "Garrison Staff", "calendar_id": _calendar_map().get("Garrison", ""), "fixed_venue": "Garrison"},
        {"name": "Baby G Staff", "calendar_id": _calendar_map().get("Baby G", ""), "fixed_venue": "Baby G"},
        {
            "name": "Titan Security",
            "calendar_id": str(getattr(settings, "CALENDAR_TITAN_SECURITY_ID", "") or "").strip(),
            "fixed_venue": None,
        },
    ]


def _event_location(event: dict) -> str:
    return str(event.get("location") or "").strip()


def _infer_titan_security_venue(event: dict) -> str | None:
    title = _event_summary(event)
    location = _event_location(event)
    hay = f"{title} {location}".lower()
    if "st anne" in hay or "st  anne" in hay:
        return "St Anne's"
    if "getwell" in hay or "get well" in hay:
        return "Get Well"
    if "standard time" in hay or re.search(r"^\s*S[1-9]\d*\s*:\s*ST", title, re.IGNORECASE):
        return "Standard Time"
    if re.search(r"^\s*S[1-9]\d*\s*:\s*JD", title, re.IGNORECASE) or "jean darlene" in hay:
        return "Jean Darlene"
    if "two kats" in hay or "2 kats" in hay:
        return "Two Kats"
    return None


def _strip_venue_prefix(remainder: str, venue: str) -> str:
    text = str(remainder or "").strip()
    patterns = {
        "St Anne's": r"^\s*st\s+anne'?s\s*[-:]?\s*",
        "Get Well": r"^\s*get\s*well\s*[-:]?\s*",
        "Standard Time": r"^\s*(?:standard\s*time|st)\s*[-:]?\s*",
        "Jean Darlene": r"^\s*(?:jean\s*darlene|jd)\s*[-:]?\s*",
        "Two Kats": r"^\s*(?:two\s*kats|2\s*kats)\s*[-:]?\s*",
    }
    pat = patterns.get(venue)
    if pat:
        text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()
    return text


def _event_venue(event: dict, calendar_id: str) -> str | None:
    mapping = _calendar_map()
    titan = str(getattr(settings, "CALENDAR_TITAN_SECURITY_ID", "") or "").strip()
    if calendar_id == titan:
        return _infer_titan_security_venue(event)
    for venue in ("Garrison", "Baby G"):
        if mapping.get(venue) == calendar_id:
            return venue
    return None


def _event_guard(event: dict, venue: str | None = None) -> str:
    match = SHIFT_TITLE_RE.match(_event_summary(event))
    if not match:
        return ""
    remainder = (match.group(2) or "").strip()
    venue = venue or ""
    if venue in {"St Anne's", "Get Well", "Standard Time", "Jean Darlene", "Two Kats"}:
        remainder = _strip_venue_prefix(remainder, venue)
    return remainder.strip()


def _assignment_title(event: dict, venue: str, shift_code: str, guard: str) -> str:
    current = _event_summary(event)
    remainder = ""
    match = SHIFT_TITLE_RE.match(current)
    if match:
        remainder = (match.group(2) or "").strip()

    if venue == "St Anne's":
        if "st anne" in remainder.lower() or "st  anne" in remainder.lower():
            return f"{shift_code}:St Anne's - {guard}"
        return f"{shift_code}:{guard}"
    if venue == "Get Well":
        return f"{shift_code}:Getwell - {guard}"
    if venue == "Standard Time":
        return f"{shift_code}:ST - {guard}"
    if venue == "Jean Darlene":
        return f"{shift_code}:JD - {guard}"
    if venue == "Two Kats":
        return f"{shift_code}:Two Kats - {guard}"
    return f"{shift_code}:{guard}"


def _composio_key() -> str:
    return str(getattr(settings, "COMPOSIO_API_KEY", "") or "").strip()


def _composio_auth_mode() -> str:
    key = _composio_key()
    if key.startswith("ck_"):
        return "consumer_mcp"
    if key:
        return "project_api"
    return "none"


def _configured() -> bool:
    return bool(
        _composio_key()
        and str(getattr(settings, "COMPOSIO_CALENDAR_ACCOUNT", "") or "").strip()
    )


def _tool_data(obj: Any) -> Any:
    current = obj
    # Direct Composio REST responses are often wrapped in data/response_data.
    for _ in range(5):
        if not isinstance(current, dict):
            break
        moved = False
        for key in ("data", "response_data"):
            value = current.get(key)
            if isinstance(value, (dict, list)):
                current = value
                moved = True
                break
        if not moved:
            break
    return current


def _event_items(obj: Any) -> list[dict]:
    seen: set[int] = set()

    def walk(value: Any) -> list[dict]:
        ident = id(value)
        if ident in seen:
            return []
        seen.add(ident)
        if isinstance(value, dict):
            for key in ("items", "events"):
                rows = value.get(key)
                if isinstance(rows, list) and all(isinstance(x, dict) for x in rows):
                    if not rows or any(("id" in x or "event_id" in x) and ("start" in x or "start_time" in x) for x in rows):
                        return rows
            for key in ("data", "response_data", "event_data", "result"):
                if key in value:
                    rows = walk(value[key])
                    if rows:
                        return rows
            for child in value.values():
                rows = walk(child)
                if rows:
                    return rows
        elif isinstance(value, list):
            if all(isinstance(x, dict) for x in value):
                if not value or any(("id" in x or "event_id" in x) for x in value):
                    return value
            for child in value:
                rows = walk(child)
                if rows:
                    return rows
        return []

    return walk(obj)


def _parse_http_payload(raw: str, content_type: str = "") -> dict:
    text = (raw or "").strip()
    if not text:
        return {}
    if "text/event-stream" in (content_type or "").lower() or text.startswith("event:") or text.startswith("data:"):
        messages: list[dict] = []
        for block in re.split(r"\r?\n\r?\n", text):
            data_lines = []
            for line in block.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            candidate = "\n".join(data_lines).strip()
            if not candidate or candidate == "[DONE]":
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                messages.append(parsed)

        if not messages:
            raise RuntimeError("Calendar MCP returned an unreadable event-stream response")

        # Streamable MCP can emit progress/notification messages before the
        # final tool response. Always choose the final JSON-RPC result/error.
        for message in reversed(messages):
            if "result" in message or "error" in message:
                return message
        return messages[-1]

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Calendar service returned invalid JSON") from exc


def _mcp_post(api_key: str, payload: dict, session_id: str | None = None, allow_empty: bool = False) -> tuple[dict, str | None]:
    headers = {
        "x-consumer-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = Request(
        str(getattr(settings, "COMPOSIO_CONNECT_MCP_URL", "https://connect.composio.dev/mcp") or "https://connect.composio.dev/mcp"),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            returned_session = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id") or session_id
            if not raw.strip() and allow_empty:
                return {}, returned_session
            return _parse_http_payload(raw, resp.headers.get("Content-Type", "")), returned_session
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"Composio Connect calendar call failed ({exc.code}): {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("Could not reach Composio Connect") from exc


def _extract_mcp_tool_text(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError("Composio Connect returned an invalid response")
    if payload.get("error"):
        err = payload.get("error")
        if isinstance(err, dict):
            raise RuntimeError(str(err.get("message") or err)[:800])
        raise RuntimeError(str(err)[:800])
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Composio Connect did not return a tool result")
    if result.get("isError"):
        raise RuntimeError("Composio Connect tool execution failed")
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    # Some MCP servers may return structuredContent directly.
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    raise RuntimeError("Composio Connect returned no structured tool data")


def _execute_composio_consumer(tool_slug: str, arguments: dict) -> dict:
    api_key = _composio_key()
    account = str(getattr(settings, "COMPOSIO_CALENDAR_ACCOUNT", "") or "").strip()
    if not api_key or not account:
        raise RuntimeError("Calendar connection is not configured")

    init, session_id = _mcp_post(
        api_key,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "OpsGuard", "version": "1.0"},
            },
        },
    )
    if init.get("error"):
        raise RuntimeError("Composio Connect MCP initialization failed")
    if session_id:
        _mcp_post(
            api_key,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            session_id=session_id,
            allow_empty=True,
        )

    call_payload, _ = _mcp_post(
        api_key,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "COMPOSIO_MULTI_EXECUTE_TOOL",
                "arguments": {
                    "tools": [
                        {
                            "tool_slug": tool_slug,
                            "account": account,
                            "arguments": arguments,
                        }
                    ],
                    "thought": "OpsGuard Google Calendar synchronization",
                    "sync_response_to_workbench": False,
                },
            },
        },
        session_id=session_id,
    )
    wrapper = _extract_mcp_tool_text(call_payload)
    if wrapper.get("successful") is False:
        raise RuntimeError(str(wrapper.get("error") or "Calendar tool failed")[:800])
    data = wrapper.get("data") if isinstance(wrapper.get("data"), dict) else wrapper
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        raise RuntimeError("Composio Connect returned no calendar execution result")
    first = results[0]
    if not isinstance(first, dict):
        raise RuntimeError("Composio Connect returned an invalid calendar result")
    if first.get("error"):
        raise RuntimeError(str(first.get("error"))[:800])
    response = first.get("response")
    if isinstance(response, dict):
        if response.get("successful") is False:
            raise RuntimeError(str(response.get("error") or "Calendar tool failed")[:800])
        return response
    return first


def _execute_composio_project(tool_slug: str, arguments: dict) -> dict:
    api_key = _composio_key()
    account = str(getattr(settings, "COMPOSIO_CALENDAR_ACCOUNT", "") or "").strip()
    if not api_key or not account:
        raise RuntimeError("Calendar connection is not configured")
    url = f"https://backend.composio.dev/api/v3.1/tools/execute/{quote(tool_slug)}"
    body = json.dumps(
        {
            "connected_account_id": account,
            "version": "latest",
            "arguments": arguments,
        }
    ).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw or "{}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"Composio calendar call failed ({exc.code}): {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("Could not reach the calendar service") from exc
    if isinstance(payload, dict) and payload.get("successful") is False:
        raise RuntimeError(str(payload.get("error") or "Calendar tool failed"))
    return payload if isinstance(payload, dict) else {"data": payload}


def _execute_composio(tool_slug: str, arguments: dict) -> dict:
    if _composio_auth_mode() == "consumer_mcp":
        return _execute_composio_consumer(tool_slug, arguments)
    return _execute_composio_project(tool_slug, arguments)


async def _execute(tool_slug: str, arguments: dict) -> dict:
    return await asyncio.to_thread(_execute_composio, tool_slug, arguments)


def _parse_dt(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except Exception:
        return None


def _format_time(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.strftime("%-I:%M %p")


def _event_bounds(event: dict) -> tuple[str | None, str | None]:
    start = event.get("start")
    end = event.get("end")
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date")
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("date")
    return (str(start) if start else None, str(end) if end else None)


def _event_id(event: dict) -> str:
    return str(event.get("id") or event.get("event_id") or "").strip()


def _event_summary(event: dict) -> str:
    return str(event.get("summary") or event.get("title") or "").strip()


def _normalize_shift_event(event: dict, venue: str | None, calendar_id: str, tz: ZoneInfo) -> dict | None:
    title = _event_summary(event)
    match = SHIFT_TITLE_RE.match(title)
    if not match:
        return None
    venue = venue or _event_venue(event, calendar_id)
    if not venue:
        return None
    start_raw, end_raw = _event_bounds(event)
    if not start_raw or "T" not in start_raw:
        return None
    start_dt = _parse_dt(start_raw, tz)
    end_dt = _parse_dt(end_raw, tz)
    if not start_dt:
        return None
    duration = None
    if end_dt:
        duration = max(0, round((end_dt - start_dt).total_seconds() / 60))
    guard = _event_guard(event, venue)
    return {
        "calendar_id": calendar_id,
        "google_event_id": _event_id(event),
        "venue": venue,
        "shift_code": match.group(1).upper(),
        "guard": guard,
        "date": start_dt.date().isoformat(),
        "start_time": _format_time(start_dt),
        "calendar_end_time": _format_time(end_dt),
        "calendar_start_iso": start_raw,
        "calendar_end_iso": end_raw,
        "calendar_duration_minutes": duration,
        "requires_end_time_review": duration is None or duration <= 90,
        "source": "google_calendar",
    }


async def _get_calendar_event(calendar_id: str, event_id: str) -> dict:
    payload = await _execute(
        "GOOGLECALENDAR_EVENTS_GET",
        {
            "calendar_id": calendar_id,
            "event_id": event_id,
            "time_zone": str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"),
        },
    )
    current: Any = payload
    for _ in range(6):
        if isinstance(current, dict):
            if ("id" in current or "event_id" in current) and ("start" in current or "start_time" in current):
                return current
            moved = False
            for key in ("data", "response_data", "event_data", "result"):
                child = current.get(key)
                if isinstance(child, dict):
                    current = child
                    moved = True
                    break
            if not moved:
                break
        else:
            break
    raise RuntimeError("Calendar event could not be read")


def _next_page_token(obj: Any) -> str:
    seen: set[int] = set()

    def walk(value: Any) -> str:
        ident = id(value)
        if ident in seen:
            return ""
        seen.add(ident)
        if isinstance(value, dict):
            token = value.get("nextPageToken")
            if token:
                return str(token)
            for key in ("data", "response_data", "event_data", "result", "response"):
                if key in value:
                    found = walk(value[key])
                    if found:
                        return found
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return ""

    return walk(obj)


async def _list_calendar_events(calendar_id: str, from_date: date, to_date: date) -> list[dict]:
    """Read every Calendar event in the inclusive local-date window.

    Composio consumer-MCP responses can occasionally arrive clipped without a
    nextPageToken even when Google has more events. We first use normal small
    page-token pagination. If a terminal page is suspiciously full, split the
    date window and retry each half, then merge only by Google event ID.
    """
    tz = ZoneInfo(str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"))
    fields = "items(id,status,summary,start,end,location),nextPageToken"
    page_size = 8

    async def read_window(window_start: date, window_end: date, depth: int = 0) -> list[dict]:
        start = datetime.combine(window_start, time.min, tzinfo=tz)
        end = datetime.combine(window_end + timedelta(days=1), time.min, tzinfo=tz)
        events: list[dict] = []
        seen_event_ids: set[str] = set()
        page_token = ""
        seen_tokens: set[str] = set()
        terminal_page_count = 0
        terminal_had_token = False

        for _ in range(80):
            args = {
                "calendarId": calendar_id,
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "timeZone": str(tz.key),
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": page_size,
                "showDeleted": False,
                "fields": fields,
            }
            if page_token:
                args["pageToken"] = page_token
            payload = await _execute("GOOGLECALENDAR_EVENTS_LIST", args)
            page_items = _event_items(payload)
            for event in page_items:
                event_id = _event_id(event)
                if event_id and event_id in seen_event_ids:
                    continue
                if event_id:
                    seen_event_ids.add(event_id)
                events.append(event)

            next_token = _next_page_token(payload)
            terminal_page_count = len(page_items)
            terminal_had_token = bool(next_token)
            if not next_token:
                break
            if next_token in seen_tokens:
                raise RuntimeError("Calendar pagination returned a repeated page token")
            seen_tokens.add(next_token)
            page_token = next_token
        else:
            raise RuntimeError("Calendar pagination exceeded the safety limit")

        # A page at/above the requested page size with no continuation token can
        # indicate the consumer MCP response was clipped. Split and retry until
        # the window is small enough to be unambiguous. One-day windows are the
        # floor; OpsGuard never merges distinct same-day events.
        if (
            not terminal_had_token
            and terminal_page_count >= page_size
            and window_start < window_end
            and depth < 8
        ):
            span_days = (window_end - window_start).days
            midpoint = window_start + timedelta(days=span_days // 2)
            left, right = await asyncio.gather(
                read_window(window_start, midpoint, depth + 1),
                read_window(midpoint + timedelta(days=1), window_end, depth + 1),
            )
            merged: list[dict] = []
            merged_ids: set[str] = set()
            for event in left + right:
                event_id = _event_id(event)
                if event_id and event_id in merged_ids:
                    continue
                if event_id:
                    merged_ids.add(event_id)
                merged.append(event)
            return merged

        return events

    return await read_window(from_date, to_date)


async def _existing_google_ids(db) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    cursor = db.schedules.find({}, {"shifts.calendar_id": 1, "shifts.google_event_id": 1})
    async for doc in cursor:
        for shift in doc.get("shifts") or []:
            cal = str(shift.get("calendar_id") or "").strip()
            eid = str(shift.get("google_event_id") or "").strip()
            if cal and eid:
                pairs.add((cal, eid))
    draft_cursor = db.calendar_imports.find(
        {"status": {"$ne": "discarded"}},
        {"calendar_id": 1, "google_event_id": 1},
    )
    async for doc in draft_cursor:
        cal = str(doc.get("calendar_id") or "").strip()
        eid = str(doc.get("google_event_id") or "").strip()
        if cal and eid:
            pairs.add((cal, eid))
    return pairs


@router.get("/status")
async def calendar_sync_status(admin=Depends(get_admin_user), db=Depends(get_db)):
    calendars = [
        {"source": src["name"], "calendar_id": src["calendar_id"], "fixed_venue": src["fixed_venue"]}
        for src in _calendar_sources()
        if src["calendar_id"]
    ]
    draft_count = await db.calendar_imports.count_documents({"status": "review"})
    return {
        "configured": _configured(),
        "auth_mode": _composio_auth_mode(),
        "draft_count": draft_count,
        "account_configured": bool(str(getattr(settings, "COMPOSIO_CALENDAR_ACCOUNT", "") or "").strip()),
        "timezone": str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"),
        "calendars": calendars,
        "import_rule": "Read all S-numbered security shifts. Garrison and Baby G are fixed calendars; Titan Security is classified into known OpsGuard venues from title/location and unrelated events are ignored.",
        "export_rule": "Sync Confirmed to Google patches the title of an existing matching event only and never changes its times.",
        "creation_rule": "Create Calendar Event is a separate confirmed-shift action. It checks for an existing same venue/code/date/time event first, links that event instead of duplicating it, and only creates a new event when no conflicting slot exists.",
    }


async def _list_calendar_preview_events(calendar_id: str, from_date: date, to_date: date) -> list[dict]:
    """Preview reader that avoids cross-day pagination loss in consumer MCP.

    Each local date is read independently. Distinct Calendar events are merged
    only by Google event ID, so two S1/S2/etc. shifts on the same date remain
    separate rows.
    """
    if to_date < from_date:
        return []
    total_days = (to_date - from_date).days + 1
    days = [from_date + timedelta(days=i) for i in range(total_days)]
    semaphore = asyncio.Semaphore(4)

    async def read_day(day: date) -> list[dict]:
        async with semaphore:
            return await _list_calendar_events(calendar_id, day, day)

    batches = await asyncio.gather(*(read_day(day) for day in days))
    merged: list[dict] = []
    seen_ids: set[str] = set()
    for batch in batches:
        for event in batch:
            event_id = _event_id(event)
            if event_id and event_id in seen_ids:
                continue
            if event_id:
                seen_ids.add(event_id)
            merged.append(event)
    return merged


@router.post("/preview")
async def preview_calendar_shifts(
    payload: PreviewRequest,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if payload.to_date < payload.from_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    if (payload.to_date - payload.from_date).days > 62:
        raise HTTPException(status_code=400, detail="Preview range is limited to 63 days")
    if not _configured():
        raise HTTPException(status_code=503, detail="Calendar connection is not configured")

    tz = ZoneInfo(str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"))
    existing = await _existing_google_ids(db)
    out: list[dict] = []
    errors: list[dict] = []
    sources: list[dict] = []
    for source in _calendar_sources():
        calendar_id = source["calendar_id"]
        if not calendar_id:
            sources.append({
                "source": source["name"],
                "configured": False,
                "events_read": 0,
                "shifts_found": 0,
            })
            continue
        source_rows = 0
        try:
            # Over-fetch one additional local day, then enforce the requested inclusive
            # date range after normalization. This avoids Calendar/MCP boundary quirks
            # that can otherwise omit events occurring on the selected "to" date.
            events = await _list_calendar_preview_events(
                calendar_id,
                payload.from_date,
                payload.to_date,
            )
            for event in events:
                row = _normalize_shift_event(event, source["fixed_venue"], calendar_id, tz)
                if not row:
                    continue
                try:
                    row_date = date.fromisoformat(str(row.get("date") or ""))
                except ValueError:
                    continue
                if row_date < payload.from_date or row_date > payload.to_date:
                    continue
                row["calendar_source"] = source["name"]
                row["already_in_opsguard"] = (calendar_id, row["google_event_id"]) in existing
                out.append(row)
                source_rows += 1
            sources.append({
                "source": source["name"],
                "configured": True,
                "events_read": len(events),
                "shifts_found": source_rows,
            })
        except Exception as exc:
            message = str(exc)[:500]
            errors.append({"calendar": source["name"], "error": message})
            sources.append({
                "source": source["name"],
                "configured": True,
                "events_read": 0,
                "shifts_found": 0,
                "error": message,
            })

    out.sort(key=lambda x: (x.get("date") or "", x.get("venue") or "", x.get("start_time") or "", x.get("shift_code") or ""))
    return {
        "items": out,
        "count": len(out),
        "sources": sources,
        "errors": errors,
        "note": "Calendar event end times are shown for reference only. Short calendar slots are flagged for end-time review before creating an OpsGuard schedule.",
    }


def _serialize_import_draft(doc: dict) -> dict:
    return {
        "id": str(doc.get("_id")) if doc.get("_id") else None,
        "calendar_id": doc.get("calendar_id"),
        "google_event_id": doc.get("google_event_id"),
        "calendar_source": doc.get("calendar_source"),
        "venue": doc.get("venue"),
        "shift_code": doc.get("shift_code"),
        "guard": doc.get("guard") or "",
        "matched_guard_id": str(doc.get("matched_guard_id")) if doc.get("matched_guard_id") else None,
        "date": doc.get("date"),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "calendar_start_time": doc.get("calendar_start_time"),
        "calendar_end_time": doc.get("calendar_end_time"),
        "calendar_duration_minutes": doc.get("calendar_duration_minutes"),
        "needs_guard_review": bool(doc.get("needs_guard_review", False)),
        "needs_end_time_review": bool(doc.get("needs_end_time_review", True)),
        "status": doc.get("status") or "review",
        "created_at": doc.get("created_at").isoformat() if isinstance(doc.get("created_at"), datetime) else doc.get("created_at"),
    }


def _calendar_source_name(calendar_id: str) -> str:
    for source in _calendar_sources():
        if source.get("calendar_id") == calendar_id:
            return str(source.get("name") or "Google Calendar")
    return "Google Calendar"


@router.get("/drafts")
async def list_calendar_import_drafts(
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    items = []
    cursor = db.calendar_imports.find({"status": "review"}).sort([("date", 1), ("start_time", 1), ("created_at", 1)])
    async for doc in cursor:
        items.append(_serialize_import_draft(doc))
    return {"items": items, "count": len(items)}


@router.post("/import-drafts")
async def import_calendar_drafts(
    payload: ImportDraftRequest,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not _configured():
        raise HTTPException(status_code=503, detail="Calendar connection is not configured")
    if not payload.items:
        raise HTTPException(status_code=400, detail="Select at least one calendar shift")
    if len(payload.items) > 100:
        raise HTTPException(status_code=400, detail="Import is limited to 100 shifts at a time")

    known_calendar_ids = {src["calendar_id"] for src in _calendar_sources() if src.get("calendar_id")}
    existing = await _existing_google_ids(db)
    tz = ZoneInfo(str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"))
    imported: list[dict] = []
    skipped: list[dict] = []

    for requested in payload.items:
        calendar_id = str(requested.calendar_id or "").strip()
        event_id = str(requested.google_event_id or "").strip()
        if not calendar_id or not event_id:
            skipped.append({"calendar_id": calendar_id, "google_event_id": event_id, "reason": "Missing calendar or event id"})
            continue
        if calendar_id not in known_calendar_ids:
            skipped.append({"calendar_id": calendar_id, "google_event_id": event_id, "reason": "Calendar is not an OpsGuard calendar source"})
            continue
        if (calendar_id, event_id) in existing:
            skipped.append({"calendar_id": calendar_id, "google_event_id": event_id, "reason": "Already linked or imported"})
            continue

        try:
            event = await _get_calendar_event(calendar_id, event_id)
            row = _normalize_shift_event(event, None, calendar_id, tz)
            if not row:
                skipped.append({"calendar_id": calendar_id, "google_event_id": event_id, "reason": "Event is not a timed S-numbered shift with a recognized venue"})
                continue

            guard_text = str(row.get("guard") or "").strip()
            guard_user = None
            if guard_text:
                candidate = await db.users.find_one(
                    {"name": {"$regex": f"^{re.escape(guard_text)}$", "$options": "i"}}
                )
                if candidate and str(candidate.get("role") or "guard").lower() != "admin":
                    guard_user = candidate

            now = datetime.utcnow()
            doc = {
                "calendar_id": calendar_id,
                "google_event_id": event_id,
                "calendar_source": _calendar_source_name(calendar_id),
                "venue": row.get("venue"),
                "shift_code": row.get("shift_code"),
                "guard": guard_text,
                "matched_guard_id": guard_user.get("_id") if guard_user else None,
                "date": row.get("date"),
                "start_time": row.get("start_time"),
                # Never trust the short Google Calendar slot as the OpsGuard end time.
                "end_time": None,
                "calendar_start_time": row.get("calendar_start_iso"),
                "calendar_end_time": row.get("calendar_end_iso"),
                "calendar_duration_minutes": row.get("calendar_duration_minutes"),
                "needs_guard_review": not bool(guard_user),
                "needs_end_time_review": True,
                "status": "review",
                "created_at": now,
                "created_by_admin_id": str(admin["_id"]),
                "updated_at": now,
            }
            res = await db.calendar_imports.insert_one(doc)
            doc["_id"] = res.inserted_id
            imported.append(_serialize_import_draft(doc))
            existing.add((calendar_id, event_id))
        except Exception as exc:
            skipped.append({"calendar_id": calendar_id, "google_event_id": event_id, "reason": str(exc)[:500]})

    return {"imported": imported, "skipped": skipped}


@router.post("/drafts/{draft_id}/promote")
async def promote_calendar_import_draft(
    draft_id: str,
    payload: PromoteDraftRequest,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """Turn one reviewed Calendar draft into a pending OpsGuard schedule without writing to Google."""
    if not ObjectId.is_valid(draft_id):
        raise HTTPException(status_code=400, detail="Invalid Calendar draft id")
    guard_id = str(payload.guard_id or "").strip()
    if not ObjectId.is_valid(guard_id):
        raise HTTPException(status_code=400, detail="Choose a registered guard")

    guard_user = await db.users.find_one({"_id": ObjectId(guard_id)})
    if not guard_user or str(guard_user.get("role") or "guard").lower() == "admin":
        raise HTTPException(status_code=404, detail="Selected guard account not found")

    draft = await db.calendar_imports.find_one({"_id": ObjectId(draft_id), "status": "review"})
    if not draft:
        raise HTTPException(status_code=404, detail="Calendar draft is no longer awaiting review")

    calendar_id = str(draft.get("calendar_id") or "").strip()
    event_id = str(draft.get("google_event_id") or "").strip()
    if not calendar_id or not event_id:
        raise HTTPException(status_code=409, detail="Calendar draft is missing its Google event link")

    linked = await db.schedules.find_one(
        {"shifts": {"$elemMatch": {"calendar_id": calendar_id, "google_event_id": event_id}}},
        {"_id": 1},
    )
    if linked:
        await db.calendar_imports.update_one(
            {"_id": draft["_id"]},
            {"$set": {
                "status": "promoted",
                "schedule_id": linked["_id"],
                "promoted_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }},
        )
        raise HTTPException(status_code=409, detail="This Calendar event is already linked to an OpsGuard schedule")

    date_text = str(draft.get("date") or "").strip()
    venue = str(draft.get("venue") or "").strip()
    shift_code = str(draft.get("shift_code") or "").strip().upper()
    start_time = str(draft.get("start_time") or "").strip()
    end_time = str(payload.end_time or "").strip().upper()

    if not date_text or not venue or not _valid_shift_code(shift_code) or not start_time:
        raise HTTPException(status_code=409, detail="Draft is missing a valid date, venue, shift code or start time")
    try:
        date.fromisoformat(date_text)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Draft date is invalid") from exc

    # End time is optional when assigning or sending a shift. Validate it only when supplied.
    _parse_schedule_clock(start_time)
    if end_time:
        start_clock = _parse_schedule_clock(start_time)
        end_clock = _parse_schedule_clock(end_time)
        tz = ZoneInfo(str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"))
        start_dt = datetime.combine(date.fromisoformat(date_text), start_clock, tzinfo=tz)
        end_dt = datetime.combine(date.fromisoformat(date_text), end_clock, tzinfo=tz)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        if end_dt - start_dt > timedelta(hours=24):
            raise HTTPException(status_code=409, detail="Shift duration is longer than 24 hours")

    # Claim the draft so a double-tap cannot create two schedules.
    now = datetime.utcnow()
    claim = await db.calendar_imports.update_one(
        {"_id": draft["_id"], "status": "review"},
        {"$set": {"status": "promoting", "updated_at": now}},
    )
    if claim.modified_count != 1:
        raise HTTPException(status_code=409, detail="Calendar draft is already being processed")

    guard_name = str(guard_user.get("name") or guard_user.get("email") or "").strip()
    if not guard_name:
        await db.calendar_imports.update_one(
            {"_id": draft["_id"], "status": "promoting"},
            {"$set": {"status": "review", "updated_at": datetime.utcnow()}},
        )
        raise HTTPException(status_code=409, detail="Selected guard account has no name or email")

    note = str(payload.note or "").strip()
    schedule_doc = {
        "guard": guard_name,
        "guard_id": str(guard_user["_id"]),
        "note": note or "Imported from Google Calendar",
        "shifts": [{
            "date": date_text,
            "venue": venue,
            "shift_code": shift_code,
            "start_time": start_time,
            "end_time": end_time,
            "calendar_id": calendar_id,
            "google_event_id": event_id,
            "calendar_start_time": draft.get("calendar_start_time"),
            "calendar_end_time": draft.get("calendar_end_time"),
            "calendar_exported_at": None,
            "calendar_export_method": None,
            "status": "pending",
            "confirmed_at": None,
        }],
        "created_at": now,
        "created_by_admin_id": str(admin["_id"]),
        "status": "pending",
        "confirmed_at": None,
        "confirmed_by_guard_id": None,
        "source": "calendar_import_draft",
        "calendar_import_draft_id": str(draft["_id"]),
    }

    try:
        result = await db.schedules.insert_one(schedule_doc)
    except Exception:
        await db.calendar_imports.update_one(
            {"_id": draft["_id"], "status": "promoting"},
            {"$set": {"status": "review", "updated_at": datetime.utcnow()}},
        )
        raise

    await db.calendar_imports.update_one(
        {"_id": draft["_id"], "status": "promoting"},
        {"$set": {
            "status": "promoted",
            "schedule_id": result.inserted_id,
            "selected_guard_id": guard_user["_id"],
            "selected_guard": guard_name,
            "end_time": end_time,
            "needs_guard_review": False,
            "needs_end_time_review": not bool(end_time),
            "promoted_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }},
    )
    return {
        "status": "promoted",
        "draft_id": draft_id,
        "schedule_id": str(result.inserted_id),
        "guard": guard_name,
        "date": date_text,
        "venue": venue,
        "shift_code": shift_code,
        "start_time": start_time,
        "end_time": end_time,
        "google_calendar_changed": False,
    }


@router.post("/drafts/{draft_id}/discard")
async def discard_calendar_import_draft(
    draft_id: str,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not ObjectId.is_valid(draft_id):
        raise HTTPException(status_code=400, detail="Invalid Calendar draft id")
    now = datetime.utcnow()
    result = await db.calendar_imports.update_one(
        {"_id": ObjectId(draft_id), "status": "review"},
        {"$set": {
            "status": "discarded",
            "discarded_at": now,
            "discarded_by_admin_id": str(admin["_id"]),
            "updated_at": now,
        }},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=404, detail="Calendar draft is no longer awaiting review")
    return {"status": "discarded", "draft_id": draft_id}


def _creation_title(venue: str, shift_code: str, guard: str) -> str:
    if venue == "St Anne's":
        return f"{shift_code}:St Anne's - {guard}"
    if venue == "Get Well":
        return f"{shift_code}:Getwell - {guard}"
    if venue == "Standard Time":
        return f"{shift_code}:ST - {guard}"
    if venue == "Jean Darlene":
        return f"{shift_code}:JD - {guard}"
    if venue == "Two Kats":
        return f"{shift_code}:Two Kats - {guard}"
    return f"{shift_code}:{guard}"


def _parse_schedule_clock(value: Any) -> time:
    text = str(value or "").strip().upper()
    for fmt in ("%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise HTTPException(status_code=409, detail=f"Invalid shift time: {text or 'missing'}")


def _schedule_shift_datetimes(shift: dict) -> tuple[datetime, datetime]:
    date_text = str(shift.get("date") or "").strip()
    if not date_text:
        raise HTTPException(status_code=409, detail="Shift date is missing")
    try:
        day = date.fromisoformat(date_text)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Shift date is invalid") from exc

    start_clock = _parse_schedule_clock(shift.get("start_time"))
    if not str(shift.get("end_time") or "").strip():
        raise HTTPException(status_code=409, detail="Add an end time before creating a new Google Calendar event")
    end_clock = _parse_schedule_clock(shift.get("end_time"))
    tz = ZoneInfo(str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"))
    start_dt = datetime.combine(day, start_clock, tzinfo=tz)
    end_dt = datetime.combine(day, end_clock, tzinfo=tz)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    if end_dt - start_dt > timedelta(hours=24):
        raise HTTPException(status_code=409, detail="Shift duration is longer than 24 hours")
    return start_dt, end_dt


def _created_event_record(obj: Any) -> dict:
    seen: set[int] = set()

    def walk(value: Any) -> dict:
        ident = id(value)
        if ident in seen:
            return {}
        seen.add(ident)
        if isinstance(value, dict):
            if value.get("id") or value.get("event_id"):
                if any(k in value for k in ("summary", "start", "end", "htmlLink", "iCalUID")):
                    return value
            for key in ("data", "response_data", "event_data", "result", "response"):
                if key in value:
                    found = walk(value[key])
                    if found:
                        return found
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return {}

    return walk(obj)


async def _save_calendar_link(
    db,
    schedule_doc: dict,
    shift_index: int,
    *,
    calendar_id: str,
    event_id: str,
    start_raw: str,
    end_raw: str,
    method: str,
) -> None:
    now = datetime.utcnow()
    await db.schedules.update_one(
        {"_id": schedule_doc["_id"]},
        {"$set": {
            f"shifts.{shift_index}.calendar_id": calendar_id,
            f"shifts.{shift_index}.google_event_id": event_id,
            f"shifts.{shift_index}.calendar_start_time": start_raw,
            f"shifts.{shift_index}.calendar_end_time": end_raw,
            f"shifts.{shift_index}.calendar_exported_at": now,
            f"shifts.{shift_index}.calendar_export_method": method,
            "updated_at": now,
        }},
    )


async def _create_or_link_calendar_event(db, schedule_doc: dict, shift_index: int) -> dict:
    shifts = schedule_doc.get("shifts") or []
    if not 0 <= shift_index < len(shifts):
        raise HTTPException(status_code=404, detail="Shift not found in schedule")
    shift = shifts[shift_index]
    confirmed = (schedule_doc.get("status") == "confirmed") or (shift.get("status") == "confirmed")
    if not confirmed:
        raise HTTPException(status_code=409, detail="Only confirmed shifts can create a Google Calendar event")

    existing_event_id = str(shift.get("google_event_id") or "").strip()
    if existing_event_id:
        return {
            "action": "already_linked",
            "schedule_id": str(schedule_doc["_id"]),
            "shift_index": shift_index,
            "calendar_id": str(shift.get("calendar_id") or ""),
            "google_event_id": existing_event_id,
        }

    guard = str(schedule_doc.get("guard") or shift.get("guard") or "").strip()
    venue = str(shift.get("venue") or "").strip()
    shift_code = str(shift.get("shift_code") or "").strip().upper()
    if not guard:
        raise HTTPException(status_code=409, detail="Guard name is missing")
    if not venue:
        raise HTTPException(status_code=409, detail="Venue is missing")
    if not _valid_shift_code(shift_code):
        raise HTTPException(status_code=409, detail="Create Calendar Event requires an S-numbered shift code")

    calendar_id = _calendar_map().get(venue, "")
    if not calendar_id:
        raise HTTPException(status_code=409, detail=f"No Google Calendar is mapped for {venue}")

    start_dt, end_dt = _schedule_shift_datetimes(shift)
    day = start_dt.date()
    events = await _list_calendar_events(calendar_id, day, day)
    same_slot: list[dict] = []
    exact_time: list[dict] = []
    tz = start_dt.tzinfo

    for event in events:
        match = SHIFT_TITLE_RE.match(_event_summary(event))
        if not match or match.group(1).upper() != shift_code:
            continue
        event_venue = _event_venue(event, calendar_id)
        if event_venue != venue:
            continue
        same_slot.append(event)
        start_raw, _ = _event_bounds(event)
        event_start = _parse_dt(start_raw, tz)
        if event_start and event_start.astimezone(tz).replace(second=0, microsecond=0) == start_dt.replace(second=0, microsecond=0):
            exact_time.append(event)

    if len(exact_time) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"More than one matching {shift_code} event already exists for {venue} at {shift.get('start_time')}; no event was created.",
        )

    title = _creation_title(venue, shift_code, guard)

    if len(exact_time) == 1:
        event = exact_time[0]
        event_id = _event_id(event)
        if not event_id:
            raise HTTPException(status_code=409, detail="Existing matching Calendar event has no event id")
        current_title = _event_summary(event)
        if current_title != title:
            await _execute(
                "GOOGLECALENDAR_PATCH_EVENT",
                {
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                    "summary": title,
                    "send_updates": "none",
                },
            )
        start_raw, end_raw = _event_bounds(event)
        await _save_calendar_link(
            db,
            schedule_doc,
            shift_index,
            calendar_id=calendar_id,
            event_id=event_id,
            start_raw=start_raw or start_dt.isoformat(),
            end_raw=end_raw or end_dt.isoformat(),
            method="composio-link-existing-via-create",
        )
        return {
            "action": "linked_existing",
            "schedule_id": str(schedule_doc["_id"]),
            "shift_index": shift_index,
            "calendar_id": calendar_id,
            "google_event_id": event_id,
            "title": title,
            "changed_title": current_title != title,
            "times_changed": False,
        }

    if same_slot:
        existing_times = []
        for event in same_slot[:5]:
            start_raw, _ = _event_bounds(event)
            event_start = _parse_dt(start_raw, tz)
            if event_start:
                existing_times.append(_format_time(event_start.astimezone(tz)))
        time_note = ", ".join(existing_times) if existing_times else "another time"
        raise HTTPException(
            status_code=409,
            detail=f"A {shift_code} Calendar event already exists for {venue} on {shift.get('date')} at {time_note}. Review that event before creating another one.",
        )

    payload = await _execute(
        "GOOGLECALENDAR_CREATE_EVENT",
        {
            "calendar_id": calendar_id,
            "summary": title,
            "location": venue,
            "description": "Confirmed OpsGuard shift",
            "start_datetime": start_dt.isoformat(),
            "end_datetime": end_dt.isoformat(),
            "timezone": str(getattr(start_dt.tzinfo, "key", "America/Toronto")),
            "send_updates": "none",
            "create_meeting_room": False,
            "exclude_organizer": True,
            "transparency": "opaque",
            "extended_properties": {
                "private": {
                    "opsguard_schedule_id": str(schedule_doc["_id"]),
                    "opsguard_shift_index": str(shift_index),
                }
            },
        },
    )
    event = _created_event_record(payload)
    event_id = _event_id(event)
    if not event_id:
        raise RuntimeError("Google Calendar event was created but no event id was returned")

    start_raw, end_raw = _event_bounds(event)
    await _save_calendar_link(
        db,
        schedule_doc,
        shift_index,
        calendar_id=calendar_id,
        event_id=event_id,
        start_raw=start_raw or start_dt.isoformat(),
        end_raw=end_raw or end_dt.isoformat(),
        method="composio-create-event",
    )
    return {
        "action": "created",
        "schedule_id": str(schedule_doc["_id"]),
        "shift_index": shift_index,
        "calendar_id": calendar_id,
        "google_event_id": event_id,
        "title": title,
        "start": start_raw or start_dt.isoformat(),
        "end": end_raw or end_dt.isoformat(),
    }


async def _find_existing_calendar_slot(shift: dict, guard: str) -> tuple[dict, str]:
    calendar_id = str(shift.get("calendar_id") or "").strip()
    if not calendar_id:
        calendar_id = _calendar_map().get(str(shift.get("venue") or "").strip(), "")
    if not calendar_id:
        raise HTTPException(status_code=409, detail="No Google Calendar is mapped for this venue")

    event_id = str(shift.get("google_event_id") or "").strip()
    if event_id:
        return {"id": event_id}, calendar_id

    shift_code = str(shift.get("shift_code") or "").strip().upper()
    date_text = str(shift.get("date") or "").strip()
    if not _valid_shift_code(shift_code) or not date_text:
        raise HTTPException(status_code=409, detail="Shift needs a date and S-numbered code before calendar export")

    try:
        day = date.fromisoformat(date_text)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Shift date is invalid") from exc

    events = await _list_calendar_events(calendar_id, day, day)
    target_venue = str(shift.get("venue") or "").strip()
    candidates = []
    for event in events:
        title = _event_summary(event)
        match = SHIFT_TITLE_RE.match(title)
        if not match or match.group(1).upper() != shift_code:
            continue
        event_venue = _event_venue(event, calendar_id)
        if event_venue and target_venue and event_venue != target_venue:
            continue
        candidates.append(event)

    if not candidates:
        raise HTTPException(
            status_code=409,
            detail=f"No existing {shift_code} Google Calendar slot was found for {date_text}. OpsGuard will not create a new event automatically.",
        )
    if len(candidates) == 1:
        return candidates[0], calendar_id

    # Prefer an already-assigned candidate with the same guard.
    guard_norm = guard.strip().lower()
    same_guard = []
    for event in candidates:
        event_guard = _event_guard(event, target_venue).lower()
        if guard_norm and event_guard == guard_norm:
            same_guard.append(event)
    if len(same_guard) == 1:
        return same_guard[0], calendar_id

    # If calendar has more than one same-code slot, require the OpsGuard start to match.
    shift_start = str(shift.get("start_time") or "").strip().upper()
    tz = ZoneInfo(str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"))
    time_matches = []
    for event in candidates:
        start_raw, _ = _event_bounds(event)
        start_dt = _parse_dt(start_raw, tz)
        if start_dt and _format_time(start_dt).upper() == shift_start:
            time_matches.append(event)
    if len(time_matches) == 1:
        return time_matches[0], calendar_id

    raise HTTPException(
        status_code=409,
        detail=f"More than one {shift_code} calendar slot exists for {date_text}; the match is ambiguous and no calendar event was changed.",
    )


async def _export_one(db, schedule_doc: dict, shift_index: int) -> dict:
    shifts = schedule_doc.get("shifts") or []
    if not 0 <= shift_index < len(shifts):
        raise HTTPException(status_code=404, detail="Shift not found in schedule")
    shift = shifts[shift_index]
    confirmed = (schedule_doc.get("status") == "confirmed") or (shift.get("status") == "confirmed")
    if not confirmed:
        raise HTTPException(status_code=409, detail="Only confirmed shifts can sync to Google Calendar")

    guard = str(schedule_doc.get("guard") or shift.get("guard") or "").strip()
    shift_code = str(shift.get("shift_code") or "").strip().upper()
    if not guard:
        raise HTTPException(status_code=409, detail="Guard name is missing")
    if not _valid_shift_code(shift_code):
        raise HTTPException(status_code=409, detail="Only S-numbered shifts can sync to Google Calendar")

    event, calendar_id = await _find_existing_calendar_slot(shift, guard)
    event_id = _event_id(event)
    if not event_id:
        raise HTTPException(status_code=409, detail="Matched calendar event has no event id")

    venue = str(shift.get("venue") or "").strip()
    target_title = _assignment_title(event, venue, shift_code, guard)
    current_title = _event_summary(event)
    if current_title != target_title:
        await _execute(
            "GOOGLECALENDAR_PATCH_EVENT",
            {
                "calendar_id": calendar_id,
                "event_id": event_id,
                "summary": target_title,
                "send_updates": "none",
            },
        )

    now = datetime.utcnow()
    start_raw, end_raw = _event_bounds(event)
    update = {
        f"shifts.{shift_index}.calendar_id": calendar_id,
        f"shifts.{shift_index}.google_event_id": event_id,
        f"shifts.{shift_index}.calendar_start_time": start_raw,
        f"shifts.{shift_index}.calendar_end_time": end_raw,
        f"shifts.{shift_index}.calendar_exported_at": now,
        f"shifts.{shift_index}.calendar_export_method": "composio-title-patch",
        "updated_at": now,
    }
    await db.schedules.update_one({"_id": schedule_doc["_id"]}, {"$set": update})
    return {
        "schedule_id": str(schedule_doc["_id"]),
        "shift_index": shift_index,
        "calendar_id": calendar_id,
        "google_event_id": event_id,
        "title": target_title,
        "changed_title": current_title != target_title,
        "times_changed": False,
    }


@router.post("/create/{schedule_id}/shifts/{shift_index}")
async def create_confirmed_shift_calendar_event(
    schedule_id: str,
    shift_index: int,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """Separate action: create a missing Google event for one confirmed OpsGuard shift."""
    if not _configured():
        raise HTTPException(status_code=503, detail="Calendar connection is not configured")
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    doc = await db.schedules.find_one({"_id": ObjectId(schedule_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return await _create_or_link_calendar_event(db, doc, shift_index)


@router.post("/export/{schedule_id}/shifts/{shift_index}")
async def export_confirmed_shift(
    schedule_id: str,
    shift_index: int,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not _configured():
        raise HTTPException(status_code=503, detail="Calendar connection is not configured")
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    doc = await db.schedules.find_one({"_id": ObjectId(schedule_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return await _export_one(db, doc, shift_index)


@router.post("/export-confirmed")
async def export_all_confirmed(
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not _configured():
        raise HTTPException(status_code=503, detail="Calendar connection is not configured")

    exported: list[dict] = []
    skipped: list[dict] = []
    cursor = db.schedules.find({}).sort("created_at", 1)
    async for doc in cursor:
        for index, shift in enumerate(doc.get("shifts") or []):
            confirmed = (doc.get("status") == "confirmed") or (shift.get("status") == "confirmed")
            if not confirmed or shift.get("calendar_exported_at"):
                continue
            try:
                exported.append(await _export_one(db, doc, index))
            except HTTPException as exc:
                skipped.append(
                    {
                        "schedule_id": str(doc["_id"]),
                        "shift_index": index,
                        "venue": shift.get("venue"),
                        "date": shift.get("date"),
                        "shift_code": shift.get("shift_code"),
                        "reason": str(exc.detail),
                    }
                )
            if len(exported) + len(skipped) >= 50:
                return {"exported": exported, "skipped": skipped, "limited": True}

    return {"exported": exported, "skipped": skipped, "limited": False}
