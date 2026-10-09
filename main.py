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

# Routers already define their own prefixes
app.include_router(users.router)
app.include_router(shifts.router)
app.include_router(availability.router)
app.include_router(schedules.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/csv-fix.js", include_in_schema=False)
async def opsguard_csv_fix():
    fix_path = Path(__file__).resolve().parent / "csv-fix.js"
    if not fix_path.exists():
        return HTMLResponse("csv-fix.js is missing from the deployed app.", status_code=500)
    return FileResponse(
        fix_path,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/portal", include_in_schema=False)
async def opsguard_portal():
    """Serve the OpsGuard frontend so GoDaddy only needs a small iframe embed."""
    portal_path = Path(__file__).resolve().parent / "portal.html"
    if not portal_path.exists():
        return HTMLResponse("portal.html is missing from the deployed app.", status_code=500)

    portal_html = portal_path.read_text(encoding="utf-8")
    fix_script = '<script src="/csv-fix.js?v=20261008-1"></script>'
    if fix_script not in portal_html:
        if "</body>" in portal_html:
            portal_html = portal_html.replace("</body>", fix_script + "</body>")
        else:
            portal_html += fix_script

    return HTMLResponse(
        portal_html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
