from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_db
from deps import get_current_user, get_admin_user

router = APIRouter(prefix="/api/availability", tags=["availability"])


# ---------- Pydantic models ----------

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


# ---------- Helpers ----------


def serialize_availability(doc: dict, user: Optional[dict] = None) -> AvailabilityOut:
    """
    Turn a Mongo document into a clean API response.
    """
    if not doc:
        raise ValueError("Cannot serialize empty availability document")

    # Try to use user info provided by caller, otherwise fall back to fields on the document
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


# ---------- Guard endpoints ----------


@router.post("", response_model=AvailabilityOut)
async def upsert_my_availability(
    payload: AvailabilityIn,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Guard submits or updates availability for a specific date.
    If a document already exists for that user and date, it is updated.
    Otherwise a new document is created.
    """
    user_id = str(current_user["_id"])
    user_email = current_user.get("email")
    user_name = current_user.get("name") or current_user.get("full_name")

    # Basic validation of date format
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

    # Fetch the final version to return to the client
    doc = await db.availability.find_one(query)
    return serialize_availability(doc, current_user)


@router.get("/me", response_model=List[AvailabilityOut])
async def get_my_availability_for_month(
    month: str = Query(..., description="Month in YYYY-MM format"),
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Get all availability records for the current user in a given month.
    This is used by the Availability tab when a guard loads their calendar.
    """
    user_id = str(current_user["_id"])

    # Basic check of month format
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


# ---------- Admin endpoints ----------


@router.get("", response_model=List[AvailabilityOut])
async def admin_get_all_for_month(
    month: str = Query(..., description="Month in YYYY-MM format"),
    db=Depends(get_db),
    admin=Depends(get_admin_user),
):
    """
    Admin endpoint to see all availability for a given month.
    Used if you want to show an overview of all guards.
    """
    # Basic check of month format
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
    month: Optional[str] = Query(
        None, description="Optional month filter in YYYY-MM format"
    ),
    db=Depends(get_db),
    admin=Depends(get_admin_user),
):
    """
    Admin endpoint for the front end "Check availability" button.

    It returns availability records for a single guard, optionally filtered by month.

    The guard parameter is usually the guard's display name, but it can also be their email.
    """
    guard = guard.strip()
    if not guard:
        raise HTTPException(status_code=400, detail="guard parameter is required")

    # Try to find the user by name first
    user = await db.users.find_one({"name": guard})

    # If not found by name, try by email
    if not user and "@" in guard:
        user = await db.users.find_one({"email": guard.lower()})

    # If we still cannot find the user, just return an empty list
    if not user:
        return []

    user_id = str(user["_id"])

    query: dict = {"user_id": user_id}

    if month:
        try:
            datetime.strptime(month + "-01", "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=400, detail="month must be in YYYY-MM format"
            )
        query["date"] = {"$regex": f"^{month}"}

    cursor = db.availability.find(query).sort("date", 1)

    items: List[AvailabilityOut] = []
    async for doc in cursor:
        items.append(serialize_availability(doc, user))

    return items
