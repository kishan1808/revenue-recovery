from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RecoverySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DATABASE_URL: str = Field(
        default="",
        description="PostgreSQL connection string in psycopg format "
        "(postgresql+psycopg://user:password@host:port/dbname)",
    )
    APP_ENV: str = "development"
    DEBUG: bool = True


settings = RecoverySettings()
