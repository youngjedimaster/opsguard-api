from fastapi import APIRouter, Depends
from datetime import datetime
from bson import ObjectId

from database import get_db
from deps import get_current_user, get_admin_user
from models import ShiftCreate

router = APIRouter(prefix="/api/shifts", tags=["shifts"])


@router.post("")
async def create_shift(shift: ShiftCreate, user=Depends(get_current_user), db=Depends(get_db)):
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
    return {"id": str(res.inserted_id), **doc}


@router.get("/me")
async def get_my_shifts(page: int = 1, page_size: int = 20, user=Depends(get_current_user), db=Depends(get_db)):
    skip = (page - 1) * page_size
    cursor = db.shifts.find({"user_id": str(user["_id"])}).sort("date", -1).skip(skip).limit(page_size)
    items = []
    async for doc in cursor:
        doc["id"] = str(doc["_id"])
        items.append(doc)
    return {"items": items, "page": page, "page_size": page_size}


@router.get("")
async def admin_list_shifts(admin=Depends(get_admin_user), db=Depends(get_db)):
    cursor = db.shifts.find({}).sort("date", -1)
    items = []
    async for doc in cursor:
        guard_doc = await db.users.find_one({"_id": ObjectId(doc["user_id"])})
        doc["id"] = str(doc["_id"])
        doc["guard_name"] = guard_doc["name"] if guard_doc else None
        items.append(doc)
    return {"items": items}
