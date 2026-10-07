from datetime import datetime, timedelta
from typing import Optional, List
import asyncio
import hashlib
import os
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

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





RESET_TTL_MINUTES = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "30"))
RESET_BASE_URL = os.getenv("PASSWORD_RESET_BASE_URL", "https://opsguard-api.onrender.com/portal")


def _smtp_configured() -> bool:
    return bool(
        os.getenv("SMTP_HOST")
        and os.getenv("SMTP_USERNAME")
        and os.getenv("SMTP_PASSWORD")
        and (os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME"))
    )


def _send_password_reset_email(to_email: str, reset_link: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    from_email = (os.getenv("SMTP_FROM") or username).strip()
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() not in {"0", "false", "no"}

    if not host or not username or not password or not from_email:
        raise RuntimeError("SMTP is not configured")

    msg = EmailMessage()
    msg["Subject"] = "Reset your OpsGuard password"
    msg["From"] = from_email
    msg["To"] = to_email
    msg.set_content(
        "A password reset was requested for your OpsGuard account.\n\n"
        f"Reset your password here:\n{reset_link}\n\n"
        f"This link expires in {RESET_TTL_MINUTES} minutes and can be used only once.\n"
        "If you did not request this reset, you can ignore this email."
    )

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
            server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            if use_tls:
                server.starttls(context=context)
                server.ehlo()
            server.login(username, password)
            server.send_message(msg)


def _reset_link(raw_token: str) -> str:
    sep = "&" if "?" in RESET_BASE_URL else "?"
    return f"{RESET_BASE_URL}{sep}reset_token={quote(raw_token)}"


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


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
    current_email = (user_doc.get("email") or "").lower().strip()
    requested_email = payload.email.lower().strip() if payload.email else current_email
    email_changed = requested_email != current_email
    password_changed = bool(payload.new_password)

    if not email_changed and not password_changed:
        return {"status": "unchanged", "user": serialize_user(user_doc)}

    if not payload.current_password:
        raise HTTPException(status_code=400, detail="Current password is required")
    if not verify_password(payload.current_password, user_doc["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    updates = {"updated_at": datetime.utcnow()}

    if email_changed:
        existing = await db.users.find_one({"email": requested_email, "_id": {"$ne": user_doc["_id"]}})
        if existing:
            raise HTTPException(status_code=409, detail="Email already in use")
        updates["email"] = requested_email
        user_doc["email"] = requested_email

    if password_changed:
        if len(payload.new_password) < 8:
            raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
        updates["password_hash"] = hash_password(payload.new_password)

    await db.users.update_one({"_id": user_doc["_id"]}, {"$set": updates})
    return {"status": "updated", "user": serialize_user(user_doc)}


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordIn, db=Depends(get_db)):
    if not _smtp_configured():
        raise HTTPException(status_code=503, detail="Password reset email service is not configured yet")

    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})

    # Always return the same public result for an unknown email.
    if not user:
        return {
            "status": "accepted",
            "message": "If that account exists, a password reset link has been emailed.",
        }

    now = datetime.utcnow()
    user_id = str(user["_id"])

    # Invalidate any older unused links before issuing a new one.
    await db.password_reset_tokens.update_many(
        {"user_id": user_id, "used_at": None},
        {"$set": {"used_at": now, "invalidated_reason": "superseded"}},
    )

    raw_token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(minutes=RESET_TTL_MINUTES)
    token_doc = {
        "token_hash": _token_hash(raw_token),
        "user_id": user_id,
        "email": email,
        "created_at": now,
        "expires_at": expires_at,
        "used_at": None,
    }
    inserted = await db.password_reset_tokens.insert_one(token_doc)

    try:
        await asyncio.to_thread(_send_password_reset_email, email, _reset_link(raw_token))
    except Exception:
        await db.password_reset_tokens.update_one(
            {"_id": inserted.inserted_id},
            {"$set": {"used_at": datetime.utcnow(), "invalidated_reason": "email_delivery_failed"}},
        )
        raise HTTPException(status_code=503, detail="Password reset email could not be sent. Please try again later.")

    return {
        "status": "accepted",
        "message": "If that account exists, a password reset link has been emailed.",
    }


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordIn, db=Depends(get_db)):
    if not payload.new_password or len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    now = datetime.utcnow()
    token_doc = await db.password_reset_tokens.find_one(
        {"token_hash": _token_hash(payload.token), "used_at": None}
    )
    if not token_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    expires_at = token_doc.get("expires_at")
    if not expires_at or expires_at <= now:
        await db.password_reset_tokens.update_one(
            {"_id": token_doc["_id"]},
            {"$set": {"used_at": now, "invalidated_reason": "expired"}},
        )
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    user_id = token_doc.get("user_id")
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid reset link")

    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=400, detail="Invalid reset link")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": hash_password(payload.new_password), "updated_at": now}},
    )
    await db.password_reset_tokens.update_many(
        {"user_id": user_id, "used_at": None},
        {"$set": {"used_at": now}},
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