from fastapi import APIRouter, Depends
from datetime import datetime

from database import get_db
from deps import get_current_user, get_admin_user
from models import AvailabilityCreate

router = APIRouter(prefix="/api/availability", tags=["availability"])


@router.post("")
async def upsert_availability(body: AvailabilityCreate, user=Depends(get_current_user), db=Depends(get_db)):
    existing = await db.availability.find_one(
        {"user_id": str(user["_id"]), "date": body.date.isoformat()}
    )
    doc = {
        "user_id": str(user["_id"]),
        "date": body.date.isoformat(),
        "is_available": body.is_available,
        "start_time": body.start_time,
        "end_time": body.end_time,
        "notes": body.notes,
        "updated_at": datetime.utcnow(),
    }
    if existing:
        await db.availability.update_one({"_id": existing["_id"]}, {"$set": doc})
        doc["created_at"] = existing.get("created_at", datetime.utcnow())
        return {"id": str(existing["_id"]), **doc}
    else:
        doc["created_at"] = datetime.utcnow()
        res = await db.availability.insert_one(doc)
        return {"id": str(res.inserted_id), **doc}


@router.get("/me")
async def get_my_availability(month: str, user=Depends(get_current_user), db=Depends(get_db)):
    cursor = db.availability.find(
        {"user_id": str(user["_id"]), "date": {"$regex": f"^{month}"}}
    ).sort("date", 1)
    items = []
    async for doc in cursor:
        doc["id"] = str(doc["_id"])
        items.append(doc)
    return items


@router.get("")
async def admin_get_all(month: str, admin=Depends(get_admin_user), db=Depends(get_db)):
    cursor = db.availability.find({"date": {"$regex": f"^{month}"}}).sort("date", 1)
    items = []
    async for doc in cursor:
        doc["id"] = str(doc["_id"])
        items.append(doc)
    return items
