from datetime import datetime
from typing import Optional, List
import secrets

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr

from database import get_db
from models import UserCreate, UserOut, Token
from auth import hash_password, verify_password, create_access_token
from deps import get_current_user, get_admin_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class ProfileUpdateIn(BaseModel):
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


class AdminResetPasswordIn(BaseModel):
    user_id: Optional[str] = None
    email: Optional[EmailStr] = None
    new_password: str


def serialize_user(user: dict) -> dict:
    return {
        "id": str(user.get("_id")),
        "name": user.get("name") or user.get("full_name") or "",
        "email": user.get("email") or "",
        "role": user.get("role", "guard"),
        "created_at": user.get("created_at"),
    }


@router.post("/register", response_model=UserOut)
async def register(user_in: UserCreate, db=Depends(get_db)):
    existing = await db.users.find_one({"email": user_in.email.lower()})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    doc = {
        "name": user_in.name.strip(),
        "email": user_in.email.lower().strip(),
        "password_hash": hash_password(user_in.password),
        "role": "guard",
        "created_at": datetime.utcnow(),
    }
    res = await db.users.insert_one(doc)
    doc["_id"] = res.inserted_id
    return serialize_user(doc)


@router.post("/login", response_model=Token)
async def login(form: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    user = await db.users.find_one({"email": form.username.lower().strip()})
    if not user or not verify_password(form.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid credentials")

    token = create_access_token({"sub": str(user["_id"])})
    return {
        "access_token": token,
        "user": serialize_user(user),
    }


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return serialize_user(user)


@router.put("/profile")
async def update_profile(
    payload: ProfileUpdateIn,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_doc = dict(user)

    if payload.new_password:
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="current_password is required to change password")
        if not verify_password(payload.current_password, user_doc["password_hash"]):
            raise HTTPException(status_code=401, detail="Current password is incorrect")
        await db.users.update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"password_hash": hash_password(payload.new_password), "updated_at": datetime.utcnow()}},
        )

    if payload.email and payload.email.lower() != user_doc.get("email"):
        new_email = payload.email.lower().strip()
        existing = await db.users.find_one({"email": new_email, "_id": {"$ne": user_doc["_id"]}})
        if existing:
            raise HTTPException(status_code=409, detail="Email already in use")
        await db.users.update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"email": new_email, "updated_at": datetime.utcnow()}},
        )
        user_doc["email"] = new_email

    return {"status": "updated", "user": serialize_user(user_doc)}


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordIn, db=Depends(get_db)):
    """
    Records a password-reset request. This app does not have SMTP/email configured, so the
    secure reset action is completed by an admin in the Admin tab.
    """
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})

    now = datetime.utcnow()
    request_doc = {
        "email": email,
        "user_id": str(user["_id"]) if user else None,
        "status": "open" if user else "unknown_email",
        "created_at": now,
        "updated_at": now,
    }
    if user:
        await db.password_reset_requests.update_one(
            {"email": email, "status": "open"},
            {"$set": request_doc, "$setOnInsert": {"first_created_at": now}},
            upsert=True,
        )
    else:
        # Keep the public response generic so this endpoint does not reveal whether an email exists.
        await db.password_reset_requests.insert_one(request_doc)

    return {
        "status": "recorded",
        "message": "If that account exists, a password reset request has been sent to an OpsGuard administrator.",
    }


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordIn, db=Depends(get_db)):
    token_doc = await db.password_reset_tokens.find_one({"token": payload.token, "used_at": None})
    if not token_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user_id = token_doc.get("user_id")
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid reset token")

    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"password_hash": hash_password(payload.new_password), "updated_at": datetime.utcnow()}},
    )
    await db.password_reset_tokens.update_one(
        {"_id": token_doc["_id"]},
        {"$set": {"used_at": datetime.utcnow()}},
    )
    return {"status": "password_reset"}


@router.get("/users")
async def admin_list_users(
    role: Optional[str] = Query(None, description="Optional role filter, such as guard or admin"),
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    query = {}
    if role:
        query["role"] = role

    items: List[dict] = []
    async for user in db.users.find(query).sort("name", 1):
        items.append(serialize_user(user))
    return {"items": items}




@router.get("/admin/password-reset-requests")
async def admin_password_reset_requests(
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    items = []
    cursor = db.password_reset_requests.find({"status": "open"}).sort("created_at", -1).limit(100)
    async for doc in cursor:
        items.append({
            "id": str(doc.get("_id")),
            "email": doc.get("email"),
            "user_id": doc.get("user_id"),
            "created_at": doc.get("created_at"),
            "status": doc.get("status"),
        })
    return {"items": items}

@router.post("/admin/reset-password")
async def admin_reset_password(
    payload: AdminResetPasswordIn,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    query = None
    if payload.user_id and ObjectId.is_valid(payload.user_id):
        query = {"_id": ObjectId(payload.user_id)}
    elif payload.email:
        query = {"email": payload.email.lower().strip()}

    if not query:
        raise HTTPException(status_code=400, detail="user_id or email is required")

    user = await db.users.find_one(query)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": hash_password(payload.new_password), "updated_at": datetime.utcnow()}},
    )
    await db.password_reset_requests.update_many(
        {"$or": [{"user_id": str(user["_id"])}, {"email": user.get("email")}], "status": "open"},
        {"$set": {"status": "completed", "completed_at": datetime.utcnow(), "completed_by": str(admin["_id"])}},
    )

    return {"status": "password_reset", "user": serialize_user(user)}


@router.post("/admin/create-reset-token")
async def admin_create_reset_token(
    payload: AdminResetPasswordIn,
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    query = None
    if payload.user_id and ObjectId.is_valid(payload.user_id):
        query = {"_id": ObjectId(payload.user_id)}
    elif payload.email:
        query = {"email": payload.email.lower().strip()}

    if not query:
        raise HTTPException(status_code=400, detail="user_id or email is required")

    user = await db.users.find_one(query)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    token = secrets.token_urlsafe(32)
    await db.password_reset_tokens.insert_one({
        "token": token,
        "user_id": str(user["_id"]),
        "created_at": datetime.utcnow(),
        "created_by": str(admin["_id"]),
        "used_at": None,
    })
    return {"status": "token_created", "token": token, "user": serialize_user(user)}


@router.delete("/users/{user_id}")
async def admin_delete_user(
    user_id: str,
    delete_shifts: bool = Query(False, description="If true, also delete this user's shifts"),
    admin=Depends(get_admin_user),
    db=Depends(get_db),
):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user id")

    if str(admin.get("_id")) == user_id:
        raise HTTPException(status_code=400, detail="You cannot delete your own admin account")

    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    shift_count = await db.shifts.count_documents({"user_id": user_id})
    if delete_shifts:
        await db.shifts.delete_many({"user_id": user_id})
    else:
        await db.shifts.update_many(
            {"user_id": user_id},
            {"$set": {"guard_name": user.get("name") or user.get("email"), "deleted_user_id": user_id, "updated_at": datetime.utcnow()}, "$unset": {"user_id": ""}},
        )

    result = await db.users.delete_one({"_id": ObjectId(user_id)})
    if result.deleted_count != 1:
        raise HTTPException(status_code=409, detail="Guard account could not be deleted")
    await db.password_reset_requests.update_many(
        {"$or": [{"user_id": user_id}, {"email": user.get("email")}], "status": "open"},
        {"$set": {"status": "closed_account_deleted", "completed_at": datetime.utcnow(), "completed_by": str(admin["_id"])}},
    )
    return {"status": "deleted", "user_id": user_id, "shift_count_preserved": 0 if delete_shifts else shift_count, "shift_count_deleted": shift_count if delete_shifts else 0}