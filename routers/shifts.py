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
        # ... other fields ...
    }
    # something here like insert_one(...)
    # and then a return
