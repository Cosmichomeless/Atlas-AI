from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración de la aplicación, leída de variables de entorno."""

    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = Field(
        description="URL SQLAlchemy de PostgreSQL, p. ej. postgresql+psycopg://user:pass@host:5433/db",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # database_url llega por entorno
