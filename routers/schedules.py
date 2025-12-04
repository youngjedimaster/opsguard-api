from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from database import get_db
from deps import get_admin_user, get_current_user

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

    # Try to match the guard to a user in Mongo
    guard_user = await db.users.find_one({"name": guard})

    if not guard_user and "@" in guard:
        guard_user = await db.users.find_one({"email": guard.lower()})

    guard_id = str(guard_user["_id"]) if guard_user else None

    clean_shifts = []
    for s in shifts:
        clean_shifts.append(
            {
                "date": s.get("date"),
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
            }
        )

    doc = {
        "guard": guard,
        "guard_id": guard_id,
        "note": note,
        "shifts": clean_shifts,
        "created_at": datetime.utcnow(),
        "created_by_admin_id": str(admin["_id"]),
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
        }
    )


@router.get("/me")
async def get_my_schedules(
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Guard endpoint to fetch schedules that admins created for them.

    It matches either guard_id or guard name/email, then returns a list of schedule documents.
    """
    user_id = str(current_user["_id"])
    name = (current_user.get("name") or current_user.get("full_name") or "").strip()
    email = (current_user.get("email") or "").lower().strip()

    query = {
        "$or": [
            {"guard_id": user_id},
            {"guard": name} if name else {},
            {"guard": email} if email else {},
        ]
    }

    # Clean out any empty dicts from the $or list
    query["$or"] = [c for c in query["$or"] if c]

    if not query["$or"]:
        # Fallback: no good keys, so just return empty
        return []

    cursor = db.schedules.find(query).sort("created_at", -1)

    items: List[dict] = []
    async for doc in cursor:
        items.append(serialize_schedule(doc))

    return items