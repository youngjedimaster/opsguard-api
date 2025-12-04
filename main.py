from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from routers import users, shifts, availability, schedules

app = FastAPI(title=settings.APP_NAME)

# CORS using allowed origins from config.py
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers already define their own prefixes
app.include_router(users.router)
app.include_router(shifts.router)
app.include_router(availability.router)
app.include_router(schedules.router)

@app.get("/api/health")
async def health():
    return {"status": "ok"}
