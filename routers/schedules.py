from datetime import datetime
from typing import List
import re

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
        guard_user = await db.users.find_one({"_id": ObjectId(selected_guard_id)})
        if not guard_user or str(guard_user.get("role") or "guard").lower() == "admin":
            raise HTTPException(status_code=404, detail="Selected guard account not found")
    else:
        guard_user = await db.users.find_one({"name": guard})

    if not guard_user and not selected_guard_id and "@" in guard:
        guard_user = await db.users.find_one({"email": guard.lower()})
    if guard_user and str(guard_user.get("role") or "guard").lower() == "admin":
        guard_user = None

    guard_id = str(guard_user["_id"]) if guard_user else None

    clean_shifts = []
    for s in shifts:
        shift_code = str(s.get("shift_code") or "").strip().upper()
        if shift_code and not re.fullmatch(r"S[1-9]\d*", shift_code):
            raise HTTPException(status_code=400, detail="Shift code must be a valid S-number such as S1, S2 or S5")
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
                "availability_status": "pending",
                "availability_confirmed_at": None,
                "availability_confirmed_by_guard_id": None,
                "assignment_updated_at": datetime.utcnow(),
            }
        )

    skipped_duplicates = []
    updated_duplicates = []
    locked_duplicates = []
    if guard_id:
        existing_by_pair: dict[tuple[str, str], tuple[dict, int]] = {}
        cursor = db.schedules.find({"guard_id": guard_id})
        async for existing_schedule in cursor:
            for existing_index, existing_shift in enumerate(existing_schedule.get("shifts") or []):
                cal_id = str(existing_shift.get("calendar_id") or "").strip()
                event_id = str(existing_shift.get("google_event_id") or "").strip()
                if cal_id and event_id:
                    existing_by_pair[(cal_id, event_id)] = (existing_schedule, existing_index)

        schedule_revisions: dict[str, dict] = {}
        filtered_shifts = []
        request_pairs: set[tuple[str, str]] = set()
        compare_fields = (
            "date", "venue", "shift_code", "start_time", "end_time",
            "calendar_id", "google_event_id", "calendar_start_time", "calendar_end_time",
        )

        for shift in clean_shifts:
            cal_id = str(shift.get("calendar_id") or "").strip()
            event_id = str(shift.get("google_event_id") or "").strip()
            pair = (cal_id, event_id) if cal_id and event_id else None

            if pair and pair in request_pairs:
                skipped_duplicates.append(shift)
                continue
            if pair:
                request_pairs.add(pair)

            existing_info = existing_by_pair.get(pair) if pair else None
            if not existing_info:
                filtered_shifts.append(shift)
                continue

            existing_schedule, existing_index = existing_info
            schedule_key = str(existing_schedule["_id"])
            work = schedule_revisions.get(schedule_key)
            if work is None:
                work = {
                    "doc": existing_schedule,
                    "shifts": [dict(sh) for sh in (existing_schedule.get("shifts") or [])],
                    "changed": False,
                }
                schedule_revisions[schedule_key] = work

            existing_shift = work["shifts"][existing_index]
            schedule_locked = (existing_schedule.get("status") or "pending") == "confirmed"
            shift_locked = (existing_shift.get("status") or "pending") == "confirmed"
            if schedule_locked or shift_locked:
                locked_duplicates.append(shift)
                continue

            changed = any(
                (existing_shift.get(field) or "") != (shift.get(field) or "")
                for field in compare_fields
            )
            if not changed:
                skipped_duplicates.append(shift)
                continue

            now = datetime.utcnow()
            revised = dict(existing_shift)
            revised.update({
                "date": shift.get("date"),
                "venue": shift.get("venue"),
                "shift_code": shift.get("shift_code"),
                "start_time": shift.get("start_time"),
                "end_time": shift.get("end_time"),
                "calendar_id": shift.get("calendar_id"),
                "google_event_id": shift.get("google_event_id"),
                "calendar_start_time": shift.get("calendar_start_time"),
                "calendar_end_time": shift.get("calendar_end_time"),
                "calendar_exported_at": None,
                "calendar_export_method": None,
                "status": "pending",
                "confirmed_at": None,
                "availability_status": "pending",
                "availability_confirmed_at": None,
                "availability_confirmed_by_guard_id": None,
                "assignment_updated_at": now,
            })
            work["shifts"][existing_index] = revised
            work["changed"] = True
            updated_duplicates.append(shift)

        for work in schedule_revisions.values():
            if not work["changed"]:
                continue
            existing_schedule = work["doc"]
            existing_shifts = work["shifts"]
            remaining_confirmed = sum(1 for sh in existing_shifts if sh.get("status") == "confirmed")
            if remaining_confirmed == len(existing_shifts) and existing_shifts:
                parent_status = "confirmed"
            elif remaining_confirmed:
                parent_status = "partial"
            else:
                parent_status = "pending"

            now = datetime.utcnow()
            await db.schedules.update_one(
                {"_id": existing_schedule["_id"]},
                {"$set": {
                    "shifts": existing_shifts,
                    "status": parent_status,
                    "confirmed_at": None if parent_status != "confirmed" else existing_schedule.get("confirmed_at"),
                    "confirmed_by_guard_id": None if parent_status != "confirmed" else existing_schedule.get("confirmed_by_guard_id"),
                    "updated_at": now,
                }},
            )

        clean_shifts = filtered_shifts

    if not clean_shifts:
        return {
            "created": False,
            "duplicate_only": True,
            "guard": guard,
            "guard_id": guard_id,
            "updated_existing": len(updated_duplicates),
            "locked_duplicates": len(locked_duplicates),
            "skipped_duplicates": len(skipped_duplicates),
            "message": (
                f"{len(updated_duplicates)} existing shift(s) updated."
                if updated_duplicates
                else "All selected Calendar shifts are already assigned to this guard."
            ),
        }

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

    response = serialize_schedule(
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
    response["created"] = True
    response["updated_existing"] = len(updated_duplicates)
    response["locked_duplicates"] = len(locked_duplicates)
    response["skipped_duplicates"] = len(skipped_duplicates)
    return response


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


@router.delete("/{schedule_id}/shifts/{shift_index}")
async def admin_delete_schedule_shift(
    schedule_id: str,
    shift_index: int,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """Admin override: delete one scheduled shift even if the guard confirmed it."""
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    doc = await db.schedules.find_one({"_id": ObjectId(schedule_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    shifts = list(doc.get("shifts") or [])
    if not 0 <= shift_index < len(shifts):
        raise HTTPException(status_code=404, detail="Shift not found in schedule")

    removed = shifts.pop(shift_index)
    if not shifts:
        await db.schedules.delete_one({"_id": doc["_id"]})
        return {"status": "deleted", "schedule_deleted": True, "removed_shift": removed}

    confirmed_count = sum(1 for sh in shifts if sh.get("status") == "confirmed")
    if confirmed_count == len(shifts):
        parent_status = "confirmed"
        confirmed_at = doc.get("confirmed_at")
        confirmed_by_guard_id = doc.get("confirmed_by_guard_id")
    elif confirmed_count:
        parent_status = "partial"
        confirmed_at = None
        confirmed_by_guard_id = None
    else:
        parent_status = "pending"
        confirmed_at = None
        confirmed_by_guard_id = None

    now = datetime.utcnow()
    await db.schedules.update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "shifts": shifts,
            "status": parent_status,
            "confirmed_at": confirmed_at,
            "confirmed_by_guard_id": confirmed_by_guard_id,
            "updated_at": now,
        }},
    )
    updated = await db.schedules.find_one({"_id": doc["_id"]})
    return serialize_schedule(updated)


@router.post("/{schedule_id}/confirm")
async def admin_confirm_schedule(
    schedule_id: str,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """Legacy admin-only bulk confirmation. Guards cannot finalize schedules."""
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    schedule = await db.schedules.find_one({"_id": ObjectId(schedule_id)})
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    shifts = list(schedule.get("shifts") or [])
    if not shifts:
        raise HTTPException(status_code=409, detail="Schedule has no shifts")
    if any(sh.get("availability_status") != "confirmed" for sh in shifts if sh.get("status") != "confirmed"):
        raise HTTPException(status_code=409, detail="Every unconfirmed shift requires guard availability before admin confirmation")
    if (schedule.get("status") or "pending") == "confirmed":
        return serialize_schedule(schedule)
    now = datetime.utcnow()
    admin_id = str(admin["_id"])
    confirmed_shifts=[]
    for sh in shifts:
        item=dict(sh)
        item["status"]="confirmed"
        item["confirmed_at"]=item.get("confirmed_at") or now
        item["confirmed_by_admin_id"]=admin_id
        item["confirmed_by_guard_id"]=None
        confirmed_shifts.append(item)
    await db.schedules.update_one(
        {"_id": schedule["_id"]},
        {"$set": {
            "status": "confirmed",
            "shifts": confirmed_shifts,
            "confirmed_at": now,
            "confirmed_by_guard_id": None,
            "confirmed_by_admin_id": admin_id,
            "updated_at": now,
        }},
    )
    return serialize_schedule(await db.schedules.find_one({"_id": schedule["_id"]}))


@router.post("/{schedule_id}/shifts/{shift_index}/availability")
async def confirm_shift_availability(
    schedule_id: str,
    shift_index: int,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Guard confirms they are available for one assigned shift, without locking final acceptance."""
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
    shift = shifts[shift_index]
    if shift.get("status") == "confirmed":
        return serialize_schedule(doc)
    if shift.get("availability_status") == "confirmed":
        return serialize_schedule(doc)
    now = datetime.utcnow()
    await db.schedules.update_one(
        {"_id": doc["_id"], f"shifts.{shift_index}.status": {"$ne": "confirmed"}},
        {"$set": {
            f"shifts.{shift_index}.availability_status": "confirmed",
            f"shifts.{shift_index}.availability_confirmed_at": now,
            f"shifts.{shift_index}.availability_confirmed_by_guard_id": uid,
            "updated_at": now,
        }},
    )
    doc = await db.schedules.find_one({"_id": doc["_id"]})
    return serialize_schedule(doc)


@router.post("/{schedule_id}/shifts/{shift_index}/confirm")
async def admin_confirm_one_shift(
    schedule_id: str,
    shift_index: int,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """Admin makes the final shift assignment after the guard confirms availability."""
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    doc = await db.schedules.find_one({"_id": ObjectId(schedule_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    shifts = doc.get("shifts") or []
    if not 0 <= shift_index < len(shifts):
        raise HTTPException(status_code=404, detail="Shift not found in schedule")
    shift = shifts[shift_index]
    if (doc.get("status") or "pending") == "confirmed" or shift.get("status") == "confirmed":
        return serialize_schedule(doc)
    if shift.get("availability_status") != "confirmed":
        raise HTTPException(status_code=409, detail="Guard must confirm availability before admin can confirm this shift")
    now = datetime.utcnow()
    admin_id = str(admin["_id"])
    await db.schedules.update_one(
        {"_id": doc["_id"], f"shifts.{shift_index}.status": {"$ne": "confirmed"}},
        {"$set": {
            f"shifts.{shift_index}.status": "confirmed",
            f"shifts.{shift_index}.confirmed_at": now,
            f"shifts.{shift_index}.confirmed_by_admin_id": admin_id,
            f"shifts.{shift_index}.confirmed_by_guard_id": None,
            "updated_at": now,
        }},
    )
    doc = await db.schedules.find_one({"_id": doc["_id"]})
    all_confirmed = bool(doc.get("shifts")) and all(sh.get("status") == "confirmed" for sh in doc["shifts"])
    parent_status = "confirmed" if all_confirmed else "partial"
    await db.schedules.update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "status": parent_status,
            "confirmed_at": now if all_confirmed else None,
            "confirmed_by_guard_id": None,
            "confirmed_by_admin_id": admin_id if all_confirmed else None,
            "updated_at": now,
        }},
    )
    return serialize_schedule(await db.schedules.find_one({"_id": doc["_id"]}))


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


@router.delete("/{schedule_id}/shifts/{shift_index}/calendar-exported")
async def clear_calendar_exported(
    schedule_id: str,
    shift_index: int,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """Clear OpsGuard's calendar export marker so a shift can be exported again."""
    if not ObjectId.is_valid(schedule_id):
        raise HTTPException(status_code=400, detail="Invalid schedule id")
    doc = await db.schedules.find_one({"_id": ObjectId(schedule_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    shifts = doc.get("shifts") or []
    if not 0 <= shift_index < len(shifts):
        raise HTTPException(status_code=404, detail="Shift not found in schedule")
    now = datetime.utcnow()
    await db.schedules.update_one(
        {"_id": doc["_id"]},
        {"$set": {
            f"shifts.{shift_index}.calendar_exported_at": None,
            f"shifts.{shift_index}.calendar_export_method": None,
            "updated_at": now,
        }},
    )
    updated = await db.schedules.find_one({"_id": doc["_id"]})
    return serialize_schedule(updated)


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
