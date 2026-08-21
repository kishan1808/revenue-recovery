from pydantic import Field
from pydantic_settings import BaseSettings


class RecoverySettings(BaseSettings):
    DATABASE_URL: str = Field(
        default="",
        description="PostgreSQL connection string in asyncpg format "
        "(postgresql+asyncpg://user:password@host:port/dbname)",
    )
    APP_ENV: str = "development"
    DEBUG: bool = True

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = RecoverySettings()
