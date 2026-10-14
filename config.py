from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "OpsGuard API"
    APP_ENV: str = "production"
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "opsguard"
    JWT_SECRET: str = "CHANGE_THIS_TO_SOMETHING_SECRET"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 1 day

    # Server-side Composio bridge for Google Calendar sync.
    # COMPOSIO_API_KEY is always supplied as a Render secret, never committed.
    COMPOSIO_API_KEY: str = ""
    COMPOSIO_CALENDAR_ACCOUNT: str = "googlecalendar_lif-bowet"
    CALENDAR_TIMEZONE: str = "America/Toronto"
    CALENDAR_GARRISON_ID: str = "garrisonstaff@gmail.com"
    CALENDAR_BABY_G_ID: str = "s374q7mjj2hoc77f71oml6n5uc@group.calendar.google.com"
    CALENDAR_TITAN_SECURITY_ID: str = "78f673188cd9e7f1c76014eee634b0b5578f9da7f0657796fcb45e05080f45c3@group.calendar.google.com"

    ALLOWED_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://titannglobal.com",
        "https://www.titannglobal.com",
    ]

    class Config:
        env_file = ".env"


settings = Settings()
