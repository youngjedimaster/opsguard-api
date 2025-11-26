from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr

from database import get_db
from models import UserCreate, UserOut, Token
from auth import hash_password, verify_password, create_access_token
from deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut)
async def register(user_in: UserCreate, db=Depends(get_db)):
    existing = await db.users.find_one({"email": user_in.email.lower()})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    doc = {
        "name": user_in.name,
        "email": user_in.email.lower(),
        "password_hash": hash_password(user_in.password),
        "role": "guard",
        "created_at": datetime.utcnow(),
    }
    res = await db.users.insert_one(doc)
    return {
        "id": str(res.inserted_id),
        "name": doc["name"],
        "email": doc["email"],
        "role": doc["role"],
        "created_at": doc["created_at"],
    }


@router.post("/login", response_model=Token)
async def login(form: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    user = await db.users.find_one({"email": form.username.lower()})
    if not user or not verify_password(form.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid credentials")
    token = create_access_token({"sub": str(user["_id"])})
    return {
        "access_token": token,
        "user": {
            "id": str(user["_id"]),
            "name": user["name"],
            "email": user["email"],
            "role": user.get("role", "guard"),
            "created_at": user["created_at"],
        },
    }


# =========================
# New models for profile/me
# =========================

class ProfileUpdateIn(BaseModel):
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None


# =========================
# New: who am I endpoint
# =========================

@router.get("/me")
async def me(user=Depends(get_current_user)):
    """
    Return the current authenticated user.
    Front end probes this to get a clean email and name.
    """
    return {
        "id": str(user["_id"]),
        "name": user.get("name"),
        "email": user.get("email"),
        "role": user.get("role", "guard"),
        "created_at": user.get("created_at"),
    }


# =========================
# New: profile update endpoint
# =========================

@router.put("/profile")
async def update_profile(
    payload: ProfileUpdateIn,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Update current user's email and/or password.

    Front end call:
      PUT /api/auth/profile
      Authorization: Bearer <token>

      Body can include:
        email            optional (change login email)
        current_password optional, but required if changing password
        new_password     optional (change password)

    Rules:
    - If new_password is provided, current_password must be correct.
    - Email change checks for uniqueness in the users collection.
    """

    # Make a mutable copy so we can update and return it
    user_doc = dict(user)

    # 1. Handle password change
    if payload.new_password:
        if not payload.current_password:
            raise HTTPException(
                status_code=400,
                detail="current_password is required to change password",
            )

        if not verify_password(payload.current_password, user_doc["password_hash"]):
            raise HTTPException(
                status_code=401,
                detail="Current password is incorrect",
            )

        new_hash = hash_password(payload.new_password)

        await db.users.update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"password_hash": new_hash}},
        )

        user_doc["password_hash"] = new_hash

    # 2. Handle email change
    if payload.email and payload.email.lower() != user_doc["email"]:
        new_email = payload.email.lower()

        existing = await db.users.find_one(
            {"email": new_email, "_id": {"$ne": user_doc["_id"]}}
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail="Email already in use",
            )

        await db.users.update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"email": new_email}},
        )
        user_doc["email"] = new_email

    return {
        "email": user_doc["email"],
        "name": user_doc.get("name"),
    }
