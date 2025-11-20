from fastapi import APIRouter, Depends
from datetime import datetime

from database import get_db
from deps import get_current_user, get_admin_user
from models import AvailabilityCreate

router = APIRouter(prefix="/api/availability", tags=["availability"])


def serialize_availability(doc: dict) -> dict:
    """
    Convert a MongoDB availability document into a JSON-serializable dict.
    """
    if not doc:
        return {}

    created_at = doc.get("created_at")
    updated_at = doc.get("updated_at")

    return {
        "id": str(doc.get("_id")) if doc.get("_id") is not None else None,
        "user_id": str(doc.get("user_id")) if doc.get("user_id") is not None else None,
        "date": doc.get("date"),
        "is_available": doc.get("is_available"),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "notes": doc.get("notes"),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at,
    }


@router.post("")
async def upsert_availability(
    body: AvailabilityCreate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    # One availability record per user per date
    query = {
        "user_id": str(user["_id"]),
        "date": body.date.isoformat(),
    }

    existing = await db.availability.find_one(query)

    base_doc = {
        "user_id": str(user["_id"]),
        "date": body.date.isoformat(),
        "is_available": body.is_available,
        "start_time": body.start_time,
        "end_time": body.end_time,
        "notes": body.notes,
        "updated_at": datetime.utcnow(),
    }

    if existing:
        # Update existing record
        await db.availability.update_one({"_id": existing["_id"]}, {"$set": base_doc})
        existing.update(base_doc)
        return serialize_availability(existing)
    else:
        # Insert new record
        base_doc["created_at"] = datetime.utcnow()
        res = await db.availability.insert_one(base_doc)
        base_doc["_id"] = res.inserted_id
        return serialize_availability(base_doc)


@router.get("/me")
async def get_my_availability(
    month: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Return the current user's availability for a given month (YYYY-MM).
    Frontend calls: GET /api/availability/me?month=2025-11
    """
    cursor = (
        db.availability.find(
            {
                "user_id": str(user["_id"]),
                "date": {"$regex": f"^{month}"},
            }
        )
        .sort("date", 1)
    )

    items = []
    async for doc in cursor:
        items.append(serialize_availability(doc))

    # Frontend expects a plain list, not {"items": ...}
    return items


@router.get("")
async def admin_get_all(
    month: str,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    """
    Admin endpoint to see all availability for a given month (YYYY-MM).
    """
    cursor = db.availability.find({"date": {"$regex": f"^{month}"}}).sort("date", 1)

    items = []
    async for doc in cursor:
        items.append(serialize_availability(doc))

    return items
