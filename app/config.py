from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RecoverySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DATABASE_URL: str = Field(
        default="",
        description="PostgreSQL connection string in psycopg format "
        "(postgresql+psycopg://user:password@host:port/dbname)",
    )
    RAZORPAY_WEBHOOK_SECRET: str = Field(
        default="",
        description="Razorpay webhook signing secret "
        "(never logged, printed, persisted, or committed)",
    )
    APP_ENV: str = "development"
    DEBUG: bool = True


settings = RecoverySettings()
