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

SHIFT_TITLE_RE = re.compile(r"^\s*(S[123])\s*(?::\s*(.+?))?\s*$", re.IGNORECASE)


class PreviewRequest(BaseModel):
    from_date: date
    to_date: date


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
    if "standard time" in hay or re.search(r"^\s*S[123]\s*:\s*ST", title, re.IGNORECASE):
        return "Standard Time"
    if re.search(r"^\s*S[123]\s*:\s*JD", title, re.IGNORECASE) or "jean darlene" in hay:
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


def _configured() -> bool:
    return bool(
        str(getattr(settings, "COMPOSIO_API_KEY", "") or "").strip()
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


def _execute_composio(tool_slug: str, arguments: dict) -> dict:
    api_key = str(getattr(settings, "COMPOSIO_API_KEY", "") or "").strip()
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


async def _list_calendar_events(calendar_id: str, from_date: date, to_date: date) -> list[dict]:
    tz = ZoneInfo(str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"))
    start = datetime.combine(from_date, time.min, tzinfo=tz)
    end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=tz)
    payload = await _execute(
        "GOOGLECALENDAR_EVENTS_LIST",
        {
            "calendarId": calendar_id,
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": str(tz.key),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 2500,
            "showDeleted": False,
        },
    )
    return _event_items(payload)


async def _existing_google_ids(db) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    cursor = db.schedules.find({}, {"shifts.calendar_id": 1, "shifts.google_event_id": 1})
    async for doc in cursor:
        for shift in doc.get("shifts") or []:
            cal = str(shift.get("calendar_id") or "").strip()
            eid = str(shift.get("google_event_id") or "").strip()
            if cal and eid:
                pairs.add((cal, eid))
    return pairs


@router.get("/status")
async def calendar_sync_status(admin=Depends(get_admin_user)):
    calendars = [
        {"source": src["name"], "calendar_id": src["calendar_id"], "fixed_venue": src["fixed_venue"]}
        for src in _calendar_sources()
        if src["calendar_id"]
    ]
    return {
        "configured": _configured(),
        "account_configured": bool(str(getattr(settings, "COMPOSIO_CALENDAR_ACCOUNT", "") or "").strip()),
        "timezone": str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"),
        "calendars": calendars,
        "import_rule": "Read S1/S2/S3 only. Garrison and Baby G are fixed calendars; Titan Security is classified into known OpsGuard venues from title/location and unrelated events are ignored.",
        "export_rule": "Patch the title of an existing matching calendar event only; never alter calendar times or auto-create a missing event.",
    }


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
        raise HTTPException(status_code=503, detail="Calendar connection is not configured in staging")

    tz = ZoneInfo(str(getattr(settings, "CALENDAR_TIMEZONE", "America/Toronto") or "America/Toronto"))
    existing = await _existing_google_ids(db)
    out: list[dict] = []
    errors: list[dict] = []
    for source in _calendar_sources():
        calendar_id = source["calendar_id"]
        if not calendar_id:
            continue
        try:
            events = await _list_calendar_events(calendar_id, payload.from_date, payload.to_date)
            for event in events:
                row = _normalize_shift_event(event, source["fixed_venue"], calendar_id, tz)
                if not row:
                    continue
                row["calendar_source"] = source["name"]
                row["already_in_opsguard"] = (calendar_id, row["google_event_id"]) in existing
                out.append(row)
        except Exception as exc:
            errors.append({"calendar": source["name"], "error": str(exc)[:500]})

    out.sort(key=lambda x: (x.get("date") or "", x.get("venue") or "", x.get("start_time") or "", x.get("shift_code") or ""))
    return {
        "items": out,
        "count": len(out),
        "errors": errors,
        "note": "Calendar event end times are shown for reference only. Short calendar slots are flagged for end-time review before creating an OpsGuard schedule.",
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
    if shift_code not in {"S1", "S2", "S3"} or not date_text:
        raise HTTPException(status_code=409, detail="Shift needs a date and S1/S2/S3 code before calendar export")

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
    if shift_code not in {"S1", "S2", "S3"}:
        raise HTTPException(status_code=409, detail="Only S1/S2/S3 shifts can sync to Google Calendar")

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


@router.post("/export/{schedule_id}/shifts/{shift_index}")
async def export_confirmed_shift(
    schedule_id: str,
    shift_index: int,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not _configured():
        raise HTTPException(status_code=503, detail="Calendar connection is not configured in staging")
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
        raise HTTPException(status_code=503, detail="Calendar connection is not configured in staging")

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
