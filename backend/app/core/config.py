from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

Environment = Literal["development", "test", "production"]
Provider = Literal["fake", "openai"]


class Settings(BaseSettings):
    """Configuración de la aplicación.

    Orden de precedencia: variables de entorno > `.env` en la raíz del repositorio > valores por
    defecto. Ningún secreto tiene valor por defecto; los de desarrollo viven en `.env` (ignorado
    por git) y `.env.example` solo documenta los nombres.
    """

    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    app_env: Environment = "development"
    frontend_origin: str = Field(
        default="http://localhost:3000",
        description="Origen permitido por CORS y comprobaciones de Origin.",
    )
    secret_key: SecretStr | None = Field(
        default=None,
        description="Clave de firma de cookies; obligatoria en producción (>= 32 caracteres).",
    )

    session_ttl_hours: int = Field(
        default=168, gt=0, description="Duración de una sesión de usuario (7 días por defecto)."
    )

    cookie_samesite: Literal["lax", "strict", "none"] = Field(
        default="lax", description="Atributo SameSite de las cookies de sesión y CSRF."
    )
    cookie_secure: bool | None = Field(
        default=None, description="Atributo Secure; por defecto solo activo en producción."
    )

    database_url: str = Field(
        description="URL SQLAlchemy de PostgreSQL, p. ej. postgresql+psycopg://user:pass@host:5433/db",
    )

    storage_dir: Path = Field(
        default=Path("storage"), description="Directorio local de los archivos subidos."
    )
    max_upload_mb: int = Field(default=20, gt=0, description="Tamaño máximo de subida en MB.")

    embedding_provider: Provider = "fake"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, gt=0)
    llm_provider: Provider = "fake"
    llm_model: str = "gpt-4o-mini"
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"

    @field_validator("cookie_secure", mode="before")
    @classmethod
    def _empty_cookie_secure_means_default(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def session_cookie_secure(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env == "production"

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        uses_openai = "openai" in (self.embedding_provider, self.llm_provider)
        if uses_openai and not (self.openai_api_key and self.openai_api_key.get_secret_value()):
            raise ValueError("OPENAI_API_KEY es obligatoria cuando algún proveedor es 'openai'")
        if self.cookie_samesite == "none" and not self.session_cookie_secure:
            raise ValueError("COOKIE_SAMESITE=none requiere cookies Secure")
        if self.app_env == "production":
            if not self.session_cookie_secure:
                raise ValueError("En producción las cookies deben ser Secure")
            key = self.secret_key.get_secret_value() if self.secret_key else ""
            if len(key) < 32:
                raise ValueError("SECRET_KEY (>= 32 caracteres) es obligatoria en producción")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # database_url llega por entorno
