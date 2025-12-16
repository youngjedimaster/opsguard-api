from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId

from database import get_db
from deps import get_current_user, get_admin_user

router = APIRouter(prefix="/api/availability", tags=["availability"])

from pydantic import BaseModel


class AvailabilityIn(BaseModel):
    """
    Payload used when a guard submits availability.
    date is expected as YYYY-MM-DD, times as strings like '9:00 PM'.
    """
    date: str
    is_available: bool
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    notes: Optional[str] = None


class AvailabilityOut(BaseModel):
    id: str
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    date: str
    is_available: bool
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def serialize_availability(doc: dict, user: Optional[dict] = None) -> AvailabilityOut:
    if not doc:
        raise ValueError("Cannot serialize empty availability document")

    user_id = str(doc.get("user_id") or doc.get("user") or "")
    user_name = None
    user_email = None

    if user:
        user_id = str(user.get("_id") or user_id)
        user_name = user.get("name") or user.get("full_name")
        user_email = user.get("email")
    else:
        user_name = doc.get("user_name")
        user_email = doc.get("user_email")

    return AvailabilityOut(
        id=str(doc.get("_id")),
        user_id=user_id,
        user_name=user_name,
        user_email=user_email,
        date=doc.get("date"),
        is_available=bool(doc.get("is_available")),
        start_time=doc.get("start_time"),
        end_time=doc.get("end_time"),
        notes=doc.get("notes"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


@router.post("", response_model=AvailabilityOut)
async def upsert_my_availability(
    payload: AvailabilityIn,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = str(current_user["_id"])
    user_email = current_user.get("email")
    user_name = current_user.get("name") or current_user.get("full_name")

    try:
        _ = datetime.strptime(payload.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format")

    query = {"user_id": user_id, "date": payload.date}

    update_doc = {
        "$set": {
            "user_id": user_id,
            "user_email": user_email,
            "user_name": user_name,
            "date": payload.date,
            "is_available": payload.is_available,
            "start_time": payload.start_time,
            "end_time": payload.end_time,
            "notes": payload.notes,
            "updated_at": datetime.utcnow(),
        },
        "$setOnInsert": {
            "created_at": datetime.utcnow(),
        },
    }

    await db.availability.update_one(query, update_doc, upsert=True)

    doc = await db.availability.find_one(query)
    return serialize_availability(doc, current_user)


@router.get("/me", response_model=List[AvailabilityOut])
async def get_my_availability_for_month(
    month: str = Query(..., description="Month in YYYY-MM format"),
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = str(current_user["_id"])

    try:
        datetime.strptime(month + "-01", "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format")

    cursor = db.availability.find(
        {"user_id": user_id, "date": {"$regex": f"^{month}"}}
    ).sort("date", 1)

    items: List[AvailabilityOut] = []
    async for doc in cursor:
        items.append(serialize_availability(doc, current_user))

    return items


@router.get("", response_model=List[AvailabilityOut])
async def admin_get_all_for_month(
    month: str = Query(..., description="Month in YYYY-MM format"),
    db=Depends(get_db),
    admin=Depends(get_admin_user),
):
    try:
        datetime.strptime(month + "-01", "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format")

    cursor = db.availability.find(
        {"date": {"$regex": f"^{month}"}}
    ).sort([("date", 1), ("user_name", 1)])

    items: List[AvailabilityOut] = []
    async for doc in cursor:
        items.append(serialize_availability(doc))

    return items


@router.get("/admin", response_model=List[AvailabilityOut])
async def admin_get_for_guard(
    guard: str = Query(..., description="Guard name or email"),
    month: Optional[str] = Query(None, description="Optional month filter in YYYY-MM format"),
    db=Depends(get_db),
    admin=Depends(get_admin_user),
):
    guard = guard.strip()
    if not guard:
        raise HTTPException(status_code=400, detail="guard parameter is required")

    user = await db.users.find_one({"name": guard})

    if not user and "@" in guard:
        user = await db.users.find_one({"email": guard.lower()})

    if not user:
        return []

    user_id = str(user["_id"])
    query: dict = {"user_id": user_id}

    if month:
        try:
            datetime.strptime(month + "-01", "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="month must be in YYYY-MM format")
        query["date"] = {"$regex": f"^{month}"}

    cursor = db.availability.find(query).sort("date", 1)

    items: List[AvailabilityOut] = []
    async for doc in cursor:
        items.append(serialize_availability(doc, user))

    return items


@router.delete("/{availability_id}")
async def delete_availability(
    availability_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    # Validate ObjectId
    try:
        oid = ObjectId(availability_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid availability id")

    doc = await db.availability.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Availability not found")

    is_admin = bool(current_user.get("is_admin", False))
    if not is_admin and str(doc.get("user_id")) != str(current_user.get("_id")):
        raise HTTPException(status_code=403, detail="Not allowed to delete this availability")

    await db.availability.delete_one({"_id": oid})
    return {"ok": True}
