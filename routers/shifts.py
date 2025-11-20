from fastapi import APIRouter, Depends
from datetime import datetime
from bson import ObjectId

from database import get_db
from deps import get_current_user, get_admin_user
from models import ShiftCreate

router = APIRouter(prefix="/api/shifts", tags=["shifts"])


def serialize_shift(doc: dict) -> dict:
    """
    Convert a MongoDB shift document into a JSON-serializable dict.
    This removes / converts any ObjectId or datetime objects so FastAPI
    can safely return it in JSON responses.
    """
    if not doc:
        return {}

    created_at = doc.get("created_at")

    return {
        "id": str(doc.get("_id")) if doc.get("_id") is not None else None,
        "user_id": str(doc.get("user_id")) if doc.get("user_id") is not None else None,
        "date": doc.get("date"),
        "venue": doc.get("venue"),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "total_hours": doc.get("total_hours"),
        "notes": doc.get("notes"),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        # for admin listing we may attach this:
        "guard_name": doc.get("guard_name"),
    }


@router.post("")
async def create_shift(
    shift: ShiftCreate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    doc = {
        "user_id": str(user["_id"]),
        "date": shift.date.isoformat(),
        "venue": shift.venue,
        "start_time": shift.start_time,
        "end_time": shift.end_time,
        "total_hours": shift.total_hours,
        "notes": shift.notes,
        "created_at": datetime.utcnow(),
    }

    res = await db.shifts.insert_one(doc)
    # attach Mongo _id to the doc so we can serialize it
    doc["_id"] = res.inserted_id

    return serialize_shift(doc)


@router.get("/me")
async def get_my_shifts(
    page: int = 1,
    page_size: int = 20,
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
        items.append(serialize_shift(doc))

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
    }


@router.get("")
async def admin_list_shifts(
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    cursor = db.shifts.find({}).sort("date", -1)
    items = []

    async for doc in cursor:
        # doc["user_id"] is stored as a string of the user's ObjectId
        guard_doc = await db.users.find_one({"_id": ObjectId(doc["user_id"])})
        doc["guard_name"] = guard_doc["name"] if guard_doc else None
        items.append(serialize_shift(doc))

    return {"items": items}
