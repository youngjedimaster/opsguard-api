from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "OpsGuard API"
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "opsguard"
    JWT_SECRET: str = "CHANGE_THIS_TO_SOMETHING_SECRET"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 1 day

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
