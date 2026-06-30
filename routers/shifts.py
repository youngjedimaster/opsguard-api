from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from database import get_db
from deps import get_admin_user, get_current_user
from models import ShiftCreate

router = APIRouter(prefix="/api/shifts", tags=["shifts"])

DEFAULT_HOURLY_RATE = 20.00


class PaidUpdate(BaseModel):
    paid: bool


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _money(value: float) -> float:
    return round(float(value or 0), 2)


def _hours(value: float) -> float:
    return round(float(value or 0), 2)


def _parse_shift_datetime(date_value: str, time_value: str) -> datetime:
    """
    Parse common OpsGuard time strings, including 10:00 PM, 4:30 AM, 22:00, and 4:30.
    """
    date_text = _clean_text(date_value)
    time_text = _clean_text(time_value).upper().replace(".", "")
    time_text = re.sub(r"\s+", " ", time_text)

    if not date_text or not time_text:
        raise ValueError("date and time are required")

    formats = [
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %I %p",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
    ]

    combined = f"{date_text} {time_text}"
    for fmt in formats:
        try:
            return datetime.strptime(combined, fmt)
        except ValueError:
            pass

    raise ValueError(f"Could not parse time: {time_value}")


def recalculate_total_hours(date_value: str, start_time: str, end_time: str) -> float:
    """
    Recalculate hours from the actual start/end times.
    If the end time is earlier than the start time, it is treated as an overnight shift.
    """
    start_dt = _parse_shift_datetime(date_value, start_time)
    end_dt = _parse_shift_datetime(date_value, end_time)

    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    total = (end_dt - start_dt).total_seconds() / 3600
    return _hours(total)


def recalculate_pay(total_hours: float, hourly_rate: float = DEFAULT_HOURLY_RATE) -> float:
    return _money(total_hours * hourly_rate)


def _safe_recalculated_hours(doc: dict) -> float:
    try:
        return recalculate_total_hours(
            doc.get("date"),
            doc.get("start_time"),
            doc.get("end_time"),
        )
    except Exception:
        try:
            return _hours(float(doc.get("total_hours") or 0))
        except Exception:
            return 0.0


def _guard_name_from_doc(doc: dict, user_lookup: Optional[dict[str, dict]] = None) -> Optional[str]:
    direct = (
        doc.get("guard_name")
        or doc.get("user_name")
        or doc.get("guard")
        or doc.get("name")
    )
    if direct:
        return _clean_text(direct)

    user_id = _clean_text(doc.get("user_id") or doc.get("guard_id"))
    if user_id and user_lookup and user_id in user_lookup:
        user_doc = user_lookup[user_id]
        return _clean_text(user_doc.get("name") or user_doc.get("full_name") or user_doc.get("email"))

    return None


def serialize_shift(
    doc: dict,
    user_lookup: Optional[dict[str, dict]] = None,
    hourly_rate: float = DEFAULT_HOURLY_RATE,
) -> dict:
    if not doc:
        return {}

    created_at = doc.get("created_at")
    updated_at = doc.get("updated_at")
    recalculated_hours = _safe_recalculated_hours(doc)
    recalculated_pay = recalculate_pay(recalculated_hours, hourly_rate)
    stored_hours = doc.get("total_hours")

    return {
        "id": str(doc.get("_id")) if doc.get("_id") is not None else None,
        "user_id": str(doc.get("user_id")) if doc.get("user_id") is not None else None,
        "guard_name": _guard_name_from_doc(doc, user_lookup),
        "date": doc.get("date"),
        "venue": doc.get("venue"),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "total_hours": stored_hours,
        "recalculated_hours": recalculated_hours,
        "hourly_rate": _money(hourly_rate),
        "pay": recalculated_pay,
        "notes": doc.get("notes"),
        "paid": bool(doc.get("paid", False)),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }


def _build_shift_query(
    guard: Optional[str] = None,
    venue: Optional[str] = None,
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    user_lookup: Optional[dict[str, dict]] = None,
) -> dict:
    query: dict[str, Any] = {}

    if date:
        query["date"] = date.strip()
    elif from_date or to_date:
        query["date"] = {}
        if from_date:
            query["date"]["$gte"] = from_date.strip()
        if to_date:
            query["date"]["$lte"] = to_date.strip()

    if venue:
        query["venue"] = {"$regex": re.escape(venue.strip()), "$options": "i"}

    if guard:
        guard_text = guard.strip()
        guard_clauses: list[dict] = [
            {"guard_name": {"$regex": re.escape(guard_text), "$options": "i"}},
            {"user_name": {"$regex": re.escape(guard_text), "$options": "i"}},
            {"guard": {"$regex": re.escape(guard_text), "$options": "i"}},
        ]

        if user_lookup:
            matching_ids = [
                uid for uid, user_doc in user_lookup.items()
                if guard_text.lower() in _clean_text(user_doc.get("name") or user_doc.get("email")).lower()
            ]
            if matching_ids:
                guard_clauses.append({"user_id": {"$in": matching_ids}})

        query["$or"] = guard_clauses

    return query


async def _load_users_lookup(db) -> dict[str, dict]:
    users: dict[str, dict] = {}
    async for user_doc in db.users.find({}):
        users[str(user_doc.get("_id"))] = user_doc
    return users


async def _load_filtered_shifts(
    db,
    guard: Optional[str] = None,
    venue: Optional[str] = None,
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    hourly_rate: float = DEFAULT_HOURLY_RATE,
) -> list[dict]:
    user_lookup = await _load_users_lookup(db)
    query = _build_shift_query(guard, venue, date, from_date, to_date, user_lookup)

    cursor = db.shifts.find(query)
    items: list[dict] = []
    async for doc in cursor:
        item = serialize_shift(doc, user_lookup, hourly_rate)
        items.append(item)

    items.sort(key=lambda x: ((_clean_text(x.get("guard_name")).lower()), _clean_text(x.get("date")), _clean_text(x.get("venue")).lower()))
    return items


def _build_totals(items: list[dict]) -> dict:
    by_guard: dict[str, dict] = {}
    grand = {
        "shift_count": 0,
        "total_hours": 0.0,
        "total_payout": 0.0,
        "paid_payout": 0.0,
        "unpaid_payout": 0.0,
    }

    for item in items:
        guard = _clean_text(item.get("guard_name")) or "Unknown Guard"
        hours = float(item.get("recalculated_hours") or 0)
        pay = float(item.get("pay") or 0)
        paid = bool(item.get("paid"))

        if guard not in by_guard:
            by_guard[guard] = {
                "guard_name": guard,
                "shift_count": 0,
                "total_hours": 0.0,
                "total_payout": 0.0,
                "paid_payout": 0.0,
                "unpaid_payout": 0.0,
            }

        target = by_guard[guard]
        for bucket in (target, grand):
            bucket["shift_count"] += 1
            bucket["total_hours"] += hours
            bucket["total_payout"] += pay
            if paid:
                bucket["paid_payout"] += pay
            else:
                bucket["unpaid_payout"] += pay

    guard_totals = []
    for total in by_guard.values():
        total["total_hours"] = _hours(total["total_hours"])
        total["total_payout"] = _money(total["total_payout"])
        total["paid_payout"] = _money(total["paid_payout"])
        total["unpaid_payout"] = _money(total["unpaid_payout"])
        guard_totals.append(total)

    guard_totals.sort(key=lambda x: x["guard_name"].lower())

    grand["total_hours"] = _hours(grand["total_hours"])
    grand["total_payout"] = _money(grand["total_payout"])
    grand["paid_payout"] = _money(grand["paid_payout"])
    grand["unpaid_payout"] = _money(grand["unpaid_payout"])

    return {"by_guard": guard_totals, "grand_total": grand}


@router.post("")
async def create_shift(
    shift: ShiftCreate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    recalculated_hours = recalculate_total_hours(
        shift.date.isoformat(),
        shift.start_time,
        shift.end_time,
    )

    doc = {
        "user_id": str(user["_id"]),
        "guard_name": user.get("name") or user.get("full_name") or user.get("email"),
        "date": shift.date.isoformat(),
        "venue": shift.venue,
        "start_time": shift.start_time,
        "end_time": shift.end_time,
        "total_hours": recalculated_hours,
        "notes": shift.notes,
        "paid": False,
        "created_at": datetime.utcnow(),
    }

    res = await db.shifts.insert_one(doc)
    doc["_id"] = res.inserted_id
    return serialize_shift(doc)


@router.get("/me")
async def get_my_shifts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    skip = (page - 1) * page_size
    cursor = (
        db.shifts.find({"user_id": str(user["_id"])})
        .sort("date", -1)
        .skip(skip)
        .limit(page_size)
    )

    items = []
    async for doc in cursor:
        doc["guard_name"] = user.get("name") or user.get("full_name") or user.get("email")
        items.append(serialize_shift(doc))

    return {"items": items, "page": page, "page_size": page_size}


@router.get("/venues")
async def admin_list_venues(
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    venues = await db.shifts.distinct("venue")
    clean_venues = sorted({_clean_text(v) for v in venues if _clean_text(v)}, key=str.lower)
    return {"venues": clean_venues}


@router.get("")
async def admin_list_shifts(
    guard: Optional[str] = Query(None, description="Optional guard name filter"),
    venue: Optional[str] = Query(None, description="Optional venue filter"),
    date: Optional[str] = Query(None, description="Exact date in YYYY-MM-DD format"),
    from_date: Optional[str] = Query(None, description="Start date in YYYY-MM-DD format"),
    to_date: Optional[str] = Query(None, description="End date in YYYY-MM-DD format"),
    hourly_rate: float = Query(DEFAULT_HOURLY_RATE, gt=0),
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    items = await _load_filtered_shifts(db, guard, venue, date, from_date, to_date, hourly_rate)
    totals = _build_totals(items)
    return {"items": items, "totals": totals}


@router.get("/export.csv")
async def admin_export_shifts_csv(
    guard: Optional[str] = Query(None, description="Optional guard name filter"),
    venue: Optional[str] = Query(None, description="Optional venue filter"),
    date: Optional[str] = Query(None, description="Exact date in YYYY-MM-DD format"),
    from_date: Optional[str] = Query(None, description="Start date in YYYY-MM-DD format"),
    to_date: Optional[str] = Query(None, description="End date in YYYY-MM-DD format"),
    hourly_rate: float = Query(DEFAULT_HOURLY_RATE, gt=0),
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    items = await _load_filtered_shifts(db, guard, venue, date, from_date, to_date, hourly_rate)
    totals = _build_totals(items)

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Guard",
        "Date",
        "Venue",
        "Start",
        "End",
        "StoredHours",
        "RecalculatedHours",
        "HourlyRate",
        "Payout",
        "Paid",
        "Notes",
    ])

    for item in items:
        writer.writerow([
            item.get("guard_name") or "",
            item.get("date") or "",
            item.get("venue") or "",
            item.get("start_time") or "",
            item.get("end_time") or "",
            item.get("total_hours") or "",
            item.get("recalculated_hours") or 0,
            item.get("hourly_rate") or hourly_rate,
            item.get("pay") or 0,
            "Yes" if item.get("paid") else "No",
            item.get("notes") or "",
        ])

    writer.writerow([])
    writer.writerow(["Guard Totals"])
    writer.writerow(["Guard", "ShiftCount", "TotalHours", "TotalPayout", "PaidPayout", "UnpaidPayout"])
    for total in totals["by_guard"]:
        writer.writerow([
            total["guard_name"],
            total["shift_count"],
            total["total_hours"],
            total["total_payout"],
            total["paid_payout"],
            total["unpaid_payout"],
        ])

    writer.writerow([])
    grand = totals["grand_total"]
    writer.writerow(["Grand Total", grand["shift_count"], grand["total_hours"], grand["total_payout"], grand["paid_payout"], grand["unpaid_payout"]])

    csv_bytes = ("\ufeff" + output.getvalue()).encode("utf-8")
    filename_date = datetime.utcnow().strftime("%Y-%m-%d")
    headers = {"Content-Disposition": f'attachment; filename="OpsGuard_Shifts_Export_{filename_date}.csv"'}

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@router.post("/{shift_id}/paid")
async def set_shift_paid(
    shift_id: str,
    payload: PaidUpdate,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not ObjectId.is_valid(shift_id):
        raise HTTPException(status_code=400, detail="Invalid shift id")

    res = await db.shifts.update_one(
        {"_id": ObjectId(shift_id)},
        {"$set": {"paid": bool(payload.paid), "updated_at": datetime.utcnow()}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Shift not found")

    user_lookup = await _load_users_lookup(db)
    doc = await db.shifts.find_one({"_id": ObjectId(shift_id)})
    return serialize_shift(doc, user_lookup)


@router.delete("/{shift_id}")
async def delete_shift(
    shift_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    if not ObjectId.is_valid(shift_id):
        raise HTTPException(status_code=400, detail="Invalid shift id")

    doc = await db.shifts.find_one({"_id": ObjectId(shift_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Shift not found")

    is_admin = bool(user.get("is_admin")) or bool(user.get("admin")) or (user.get("role") == "admin")
    if not is_admin and str(doc.get("user_id")) != str(user.get("_id")):
        raise HTTPException(status_code=403, detail="Not allowed")

    await db.shifts.delete_one({"_id": ObjectId(shift_id)})
    return {"status": "deleted", "id": shift_id}
