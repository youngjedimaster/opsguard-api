from datetime import datetime, date
from pydantic import BaseModel, EmailStr
from typing import Optional, Literal


class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    name: str
    email: EmailStr
    role: Literal["guard", "admin"] = "guard"
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ShiftCreate(BaseModel):
    date: date
    venue: str
    start_time: str
    end_time: str
    total_hours: float
    notes: Optional[str] = None


class AvailabilityCreate(BaseModel):
    date: date
    is_available: bool = True
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    notes: Optional[str] = None
