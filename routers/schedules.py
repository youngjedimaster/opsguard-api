from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from database import get_db
from deps import get_admin_user, get_current_user

# Prefix starts with /api so the final paths are /api/schedules and /api/schedules/me
router = APIRouter(prefix="/api/schedules", tags=["schedules"])


def serialize_schedule(doc: dict) -> dict:
    if not doc:
        return {}

    return {
        "id": str(doc.get("_id")),
        "guard": doc.get("guard"),
        "guard_id": str(doc.get("guard_id")) if doc.get("guard_id") else None,
        "note": doc.get("note"),
        "shifts": doc.get("shifts") or [],
        "created_at": doc.get("created_at"),
        "created_by_admin_id": str(doc.get("created_by_admin_id")) if doc.get("created_by_admin_id") else None,
        "status": doc.get("status") or "pending",
        "confirmed_at": doc.get("confirmed_at"),
        "confirmed_by_guard_id": str(doc.get("confirmed_by_guard_id")) if doc.get("confirmed_by_guard_id") else None,
    }


@router.post("")
async def create_schedule(
    payload: dict,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """
    Save a schedule for a single guard.

    Expected payload from the front end:

    {
        "guard": "Big Papi",
        "note": "Optional message",
        "shifts": [
            {
                "guard": "Big Papi",
                "date": "2025-11-27",
                "start_time": "9:00 PM",
                "end_time": "5:00 AM"
            },
            ...
        ]
    }
    """
    guard = (payload.get("guard") or "").strip()
    note = (payload.get("note") or "").strip()
    shifts = payload.get("shifts") or []

    if not guard:
        raise HTTPException(status_code=400, detail="Guard is required")

    if not shifts:
        raise HTTPException(status_code=400, detail="At least one shift is required")

    # Prefer the exact selected guard account; names can be duplicated.
    selected_guard_id = str(payload.get("guard_id") or "").strip()
    if selected_guard_id:
        if not ObjectId.is_valid(selected_guard_id):
            raise HTTPException(status_code=400, detail="Invalid guard account id")
        guard_user = await db.users.find_one({"_id": ObjectId(selected_guard_id), "role": "guard"})
        if not guard_user:
            raise HTTPException(status_code=404, detail="Selected guard account not found")
    else:
        guard_user = await db.users.find_one({"name": guard})

    if not guard_user and not selected_guard_id and "@" in guard:
        guard_user = await db.users.find_one({"email": guard.lower()})

    guard_id = str(guard_user["_id"]) if guard_user else None

    clean_shifts = []
    for s in shifts:
        shift_code = str(s.get("shift_code") or "").strip().upper()
        if shift_code not in {"", "S1", "S2", "S3"}:
            raise HTTPException(status_code=400, detail="Shift code must be S1, S2 or S3")
        clean_shifts.append(
            {
                "date": s.get("date"),
                "venue": str(s.get("venue") or "").strip(),
                "shift_code": shift_code,
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "calendar_id": str(s.get("calendar_id") or "").strip() or None,
                "google_event_id": str(s.get("google_event_id") or "").strip() or None,
                "calendar_start_time": s.get("calendar_start_time"),
                "calendar_end_time": s.get("calendar_end_time"),
                "calendar_exported_at": None,
                "calendar_export_method": None,
                "status": "pending",
                "confirmed_at": None,
            }
        )

    doc = {
        "guard": guard,
        "guard_id": guard_id,
        "note": note,
        "shifts": clean_shifts,
        "created_at": datetime.utcnow(),
        "created_by_admin_id": str(admin["_id"]),
        "status": "pending",
        "confirmed_at": None,
        "confirmed_by_guard_id": None,
    }

    res = await db.schedules.insert_one(doc)

    return serialize_schedule(
        {
            "_id": res.inserted_id,
            "guard": guard,
            "guard_id": guard_id,
            "note": note,
            "shifts": clean_shifts,
            "created_at": doc["created_at"],
            "created_by_admin_id": doc["created_by_admin_id"],
            "status": doc["status"],
            "confirmed_at": doc["confirmed_at"],
            "confirmed_by_guard_id": doc["confirmed_by_guard_id"],
        }
    )


@router.get("")
async def admin_get_schedules(
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    items: List[dict] = []
    cursor = db.schedules.find({}).sort("created_at", -1)
    async for doc in cursor:
        items.append(serialize_schedule(doc))
    return items


@router.delete("/{schedule_id}")
async def admin_delete_schedule(
    schedule_id: str,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    schedule = await db.schedules.find_one({"_id": ObjectId(schedule_id)})
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if (schedule.get("status") or "pending") == "confirmed" or any(
        sh.get("status") == "confirmed" for sh in (schedule.get("shifts") or [])
    ):
        raise HTTPException(status_code=409, detail="Schedules with confirmed shifts are locked and cannot be deleted")
    # The deletion itself is conditional so a concurrent guard confirmation cannot race it.
    result = await db.schedules.delete_one({
        "_id": ObjectId(schedule_id),
        "status": {"$ne": "confirmed"},
        "shifts.status": {"$ne": "confirmed"},
    })
    if result.deleted_count != 1:
        raise HTTPException(status_code=409, detail="Schedule was confirmed while being deleted")
    return {"status": "deleted", "id": schedule_id}


@router.post("/{schedule_id}/confirm")
async def confirm_schedule(
    schedule_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")

    user_id = str(current_user["_id"])
    name = (current_user.get("name") or current_user.get("full_name") or "").strip()
    email = (current_user.get("email") or "").lower().strip()

    or_clauses = [{"guard_id": user_id}]
    if name:
        or_clauses.append({"guard_id": None, "guard": name})
    if email:
        or_clauses.append({"guard_id": None, "guard": email})

    schedule = await db.schedules.find_one({"_id": ObjectId(schedule_id), "$or": or_clauses})
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found for this guard")

    if (schedule.get("status") or "pending") == "confirmed":
        return serialize_schedule(schedule)

    now = datetime.utcnow()
    confirmed_shifts = [dict(sh, status="confirmed", confirmed_at=now) for sh in (schedule.get("shifts") or [])]
    await db.schedules.update_one(
        {"_id": schedule["_id"]},
        {"$set": {
            "status": "confirmed",
            "shifts": confirmed_shifts,
            "confirmed_at": now,
            "confirmed_by_guard_id": user_id,
            "updated_at": now,
        }},
    )
    schedule["shifts"] = confirmed_shifts
    schedule["status"] = "confirmed"
    schedule["confirmed_at"] = now
    schedule["confirmed_by_guard_id"] = user_id
    return serialize_schedule(schedule)


@router.post("/{schedule_id}/shifts/{shift_index}/confirm")
async def confirm_one_shift(
    schedule_id: str,
    shift_index: int,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Guard acknowledgement for exactly one assigned shift; confirmed shifts stay locked."""
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    uid = str(current_user["_id"])
    name = (current_user.get("name") or current_user.get("full_name") or "").strip()
    email = (current_user.get("email") or "").lower().strip()
    clauses = [{"guard_id": uid}]
    if name:
        clauses.append({"guard_id": None, "guard": name})
    if email:
        clauses.append({"guard_id": None, "guard": email})
    doc = await db.schedules.find_one({"_id": ObjectId(schedule_id), "$or": clauses})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found for this guard")
    shifts = doc.get("shifts") or []
    if not 0 <= shift_index < len(shifts):
        raise HTTPException(status_code=404, detail="Shift not found in schedule")
    if (doc.get("status") or "pending") == "confirmed" or shifts[shift_index].get("status") == "confirmed":
        return serialize_schedule(doc)
    now = datetime.utcnow()
    # Atomic update prevents a double-click from creating inconsistent confirmation records.
    await db.schedules.update_one(
        {"_id": doc["_id"], f"shifts.{shift_index}.status": {"$ne": "confirmed"}},
        {"$set": {f"shifts.{shift_index}.status": "confirmed",
                  f"shifts.{shift_index}.confirmed_at": now,
                  f"shifts.{shift_index}.confirmed_by_guard_id": uid,
                  "updated_at": now}},
    )
    doc = await db.schedules.find_one({"_id": doc["_id"]})
    all_confirmed = bool(doc.get("shifts")) and all(
        sh.get("status") == "confirmed" for sh in doc["shifts"]
    )
    parent_status = "confirmed" if all_confirmed else "partial"
    await db.schedules.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": parent_status,
                  "confirmed_at": now if all_confirmed else None,
                  "confirmed_by_guard_id": uid if all_confirmed else None,
                  "updated_at": now}},
    )
    doc["status"] = parent_status
    doc["confirmed_at"] = now if all_confirmed else None
    doc["confirmed_by_guard_id"] = uid if all_confirmed else None
    return serialize_schedule(doc)


@router.post("/{schedule_id}/shifts/{shift_index}/calendar-exported")
async def mark_calendar_exported(
    schedule_id: str,
    shift_index: int,
    payload: dict,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """Mark a confirmed shift as exported from OpsGuard so bulk export does not duplicate it."""
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    doc = await db.schedules.find_one({"_id": ObjectId(schedule_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    shifts = doc.get("shifts") or []
    if not 0 <= shift_index < len(shifts):
        raise HTTPException(status_code=404, detail="Shift not found in schedule")
    shift = shifts[shift_index]
    if not ((doc.get("status") or "pending") == "confirmed" or shift.get("status") == "confirmed"):
        raise HTTPException(status_code=409, detail="Only confirmed shifts can be exported")
    if shift.get("calendar_exported_at"):
        return serialize_schedule(doc)
    now = datetime.utcnow()
    method = str(payload.get("method") or "calendar").strip()[:64]
    update = {
        f"shifts.{shift_index}.calendar_exported_at": now,
        f"shifts.{shift_index}.calendar_export_method": method,
        "updated_at": now,
    }
    await db.schedules.update_one({"_id": doc["_id"]}, {"$set": update})
    doc = await db.schedules.find_one({"_id": doc["_id"]})
    return serialize_schedule(doc)


@router.get("/me")
async def get_my_schedules(
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Guard endpoint to fetch schedules that admins created for them.

    It matches either guard_id or guard name or guard email.
    """
    user_id = str(current_user["_id"])
    name = (current_user.get("name") or current_user.get("full_name") or "").strip()
    email = (current_user.get("email") or "").lower().strip()

    # Build a flexible query
    or_clauses = [{"guard_id": user_id}]
    if name:
        or_clauses.append({"guard_id": None, "guard": name})
    if email:
        or_clauses.append({"guard_id": None, "guard": email})

    query = {"$or": or_clauses}

    cursor = db.schedules.find(query).sort("created_at", -1)

    items: List[dict] = []
    async for doc in cursor:
        items.append(serialize_schedule(doc))

    return items
