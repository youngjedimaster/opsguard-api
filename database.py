from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient

from config import settings

_client: AsyncIOMotorClient | None = None


def _client_instance() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGODB_URI)
    return _client


async def get_db():
    return _client_instance()[settings.MONGODB_DB]


async def sync_production_guards_to_staging_once() -> dict:
    """Mirror the production guard roster into staging without copying live credentials."""
    if str(getattr(settings, "APP_ENV", "")).lower() != "staging":
        return {"status": "skipped", "reason": "not_staging"}
    if str(getattr(settings, "MONGODB_DB", "")) != "opsguard_staging":
        return {"status": "skipped", "reason": "unexpected_staging_db"}

    client = _client_instance()
    staging_db = client["opsguard_staging"]
    production_db = client["opsguard"]
    marker_id = "production_guard_roster_v1"

    marker = await staging_db.migrations.find_one({"_id": marker_id})
    if marker:
        return {
            "status": "already_synced",
            "copied": int(marker.get("copied", 0)),
            "skipped_email_conflicts": int(marker.get("skipped_email_conflicts", 0)),
        }

    production_guards = []
    async for doc in production_db.users.find({}):
        if str(doc.get("role") or "guard").lower() != "admin":
            production_guards.append(doc)

    staging_admins = []
    async for doc in staging_db.users.find({"role": "admin"}):
        staging_admins.append(doc)
    admin_emails = {
        str(doc.get("email") or "").strip().lower()
        for doc in staging_admins
        if str(doc.get("email") or "").strip()
    }

    await staging_db.users.delete_many({
        "$or": [
            {"role": {"$exists": False}},
            {"role": {"$ne": "admin"}},
        ]
    })

    copied = 0
    skipped_email_conflicts = 0
    blocked_keys = {
        "password_hash", "password", "token", "access_token", "refresh_token",
        "reset_token", "secret", "api_key", "jwt", "session_token",
    }

    for source in production_guards:
        clone = {
            key: value
            for key, value in source.items()
            if key not in blocked_keys and not any(
                blocked in str(key).lower()
                for blocked in ("password", "token", "secret", "api_key")
            )
        }
        email = str(clone.get("email") or "").strip().lower()
        if email and email in admin_emails:
            clone["production_email"] = email
            local, sep, domain = email.partition("@")
            clone["email"] = (
                f"{local}+stagingguard@{domain}" if sep
                else f"{email}+stagingguard"
            )
            skipped_email_conflicts += 1

        clone["role"] = "guard"
        clone["password_hash"] = "STAGING_LOGIN_DISABLED"
        clone["staging_mirrored_from"] = "production"
        clone["staging_mirrored_at"] = datetime.utcnow()

        await staging_db.users.replace_one(
            {"_id": clone["_id"]},
            clone,
            upsert=True,
        )
        copied += 1

    await staging_db.migrations.insert_one({
        "_id": marker_id,
        "completed_at": datetime.utcnow(),
        "copied": copied,
        "skipped_email_conflicts": skipped_email_conflicts,
    })
    return {
        "status": "synced",
        "copied": copied,
        "skipped_email_conflicts": skipped_email_conflicts,
    }
