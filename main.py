from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

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

app.include_router(users.router)
app.include_router(shifts.router)
app.include_router(availability.router)
app.include_router(schedules.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


def _no_cache_headers():
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


@app.get("/csv-fix.js", include_in_schema=False)
async def opsguard_csv_fix():
    fix_path = Path(__file__).resolve().parent / "csv-fix.js"
    if not fix_path.exists():
        return HTMLResponse("csv-fix.js is missing from the deployed app.", status_code=500)
    return FileResponse(fix_path, media_type="application/javascript", headers=_no_cache_headers())


@app.get("/schedule-fix.js", include_in_schema=False)
async def opsguard_schedule_fix():
    fix_path = Path(__file__).resolve().parent / "schedule-fix.js"
    if not fix_path.exists():
        return HTMLResponse("schedule-fix.js is missing from the deployed app.", status_code=500)
    return FileResponse(fix_path, media_type="application/javascript", headers=_no_cache_headers())


@app.get("/portal", include_in_schema=False)
async def opsguard_portal():
    """Serve the OpsGuard frontend so GoDaddy only needs a small iframe embed."""
    portal_path = Path(__file__).resolve().parent / "portal.html"
    if not portal_path.exists():
        return HTMLResponse("portal.html is missing from the deployed app.", status_code=500)

    portal_html = portal_path.read_text(encoding="utf-8")
    scripts = (
        '<script src="/csv-fix.js?v=20261008-1"></script>'
        '<script src="/schedule-fix.js?v=20261009-1"></script>'
    )

    if "/schedule-fix.js?v=20261009-1" not in portal_html:
        if "</body>" in portal_html:
            before, after = portal_html.rsplit("</body>", 1)
            portal_html = before + scripts + "</body>" + after
        else:
            portal_html += scripts

    return HTMLResponse(portal_html, headers=_no_cache_headers())
