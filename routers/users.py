from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from datetime import datetime

from database import get_db
from models import UserCreate, UserOut, Token
from auth import hash_password, verify_password, create_access_token

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
