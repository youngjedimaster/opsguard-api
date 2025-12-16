from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from routers import users, shifts, availability, schedules


def _normalize_origins(origins):
    """
    Ensures allow_origins is always a list[str].
    Supports:
      - list/tuple/set of origins
      - comma-separated string
      - single string origin
    """
    if origins is None:
        return []
    if isinstance(origins, (list, tuple, set)):
        return list(origins)
    if isinstance(origins, str):
        # allow comma-separated env var like: "https://a.com,https://b.com"
        parts = [o.strip() for o in origins.split(",") if o.strip()]
        return parts if parts else []
    return []


app = FastAPI(title=getattr(settings, "APP_NAME", "OpsGuard API"))

allowed_origins = _normalize_origins(getattr(settings, "ALLOWED_ORIGINS", []))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
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
