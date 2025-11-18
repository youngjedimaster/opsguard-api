from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import users, shifts, availability
from database import db

app = FastAPI()

# CORS CONFIGURATION
origins = [
    "https://titannglobal.com",
    "https://www.titannglobal.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ROUTES
app.include_router(users.router, prefix="/api/auth", tags=["auth"])
app.include_router(shifts.router, prefix="/api/shifts", tags=["shifts"])
app.include_router(availability.router, prefix="/api/availability", tags=["availability"])

@app.get("/api/health")
async def health():
    return {"status": "ok"}
