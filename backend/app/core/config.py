from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.answers.tokens import min_context_tokens

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

    ingestion_poll_seconds: float = Field(
        default=2.0, gt=0, description="Espera del worker cuando no hay documentos pendientes."
    )
    ingestion_lease_seconds: int = Field(
        default=300,
        gt=0,
        description="Segundos que un worker reserva un documento antes de perderlo.",
    )
    ingestion_max_attempts: int = Field(
        default=3, gt=0, description="Intentos de procesamiento antes de dejar el documento FAILED."
    )

    chunk_size_chars: int = Field(
        default=1000, ge=100, description="Tamaño máximo de un fragmento, en caracteres."
    )
    chunk_overlap_chars: int = Field(
        default=150, ge=0, description="Caracteres que se repiten entre fragmentos contiguos."
    )

    question_max_chars: int = Field(
        default=1000, ge=1, description="Longitud máxima de una pregunta, en caracteres."
    )

    search_default_k: int = Field(
        default=5, ge=1, description="Fragmentos que devuelve una búsqueda si no se indica `k`."
    )
    search_max_k: int = Field(
        default=20, ge=1, description="Máximo de fragmentos que una búsqueda puede pedir."
    )
    search_min_score: float = Field(
        default=0.0,
        ge=0,
        le=1,
        description="Similitud coseno mínima por defecto; lo que quede por debajo no se devuelve.",
    )

    search_max_documents: int = Field(
        default=50, ge=1, description="Máximo de documentos que una búsqueda puede seleccionar."
    )

    search_dedup_overlap: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="Solape (0-1] a partir del cual un fragmento contiguo se considera repetido.",
    )
    search_dedup_window: int = Field(
        default=1, ge=0, description="Distancia máxima de ordinal para considerar contiguos."
    )
    search_overfetch: int = Field(
        default=3, ge=1, description="Candidatos pedidos por cada resultado antes de reducir."
    )

    search_rerank: Literal["off", "lexical"] = Field(
        default="off",
        description="Reordenación de candidatos: `off` (solo similitud vectorial) o `lexical`.",
    )
    search_rerank_weight: float = Field(
        default=0.5, ge=0, le=1, description="Peso del solape léxico en la puntuación combinada."
    )
    search_rerank_pool: int = Field(
        default=3, ge=1, description="Candidatos pedidos por cada resultado antes de reordenar."
    )

    answer_context_max_tokens: int = Field(
        default=3000,
        ge=1,
        description="Tokens (estimados) que puede ocupar el contexto enviado al modelo.",
    )

    embedding_provider: Provider = "fake"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, gt=0)
    llm_provider: Provider = "fake"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = Field(
        default=0.0, ge=0, le=2, description="Temperatura de generación (0 = lo más determinista)."
    )
    llm_max_output_tokens: int = Field(
        default=512, ge=1, description="Máximo de tokens que el modelo puede generar."
    )
    llm_timeout_seconds: float = Field(
        default=60.0, gt=0, description="Tiempo máximo de espera de una generación."
    )
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
        if self.search_default_k > self.search_max_k:
            raise ValueError("SEARCH_DEFAULT_K no puede superar SEARCH_MAX_K")
        if self.chunk_overlap_chars > self.chunk_size_chars // 2:
            raise ValueError("CHUNK_OVERLAP_CHARS no puede superar la mitad de CHUNK_SIZE_CHARS")
        if self.answer_context_max_tokens < min_context_tokens(self.chunk_size_chars):
            raise ValueError(
                "ANSWER_CONTEXT_MAX_TOKENS debe bastar para un fragmento de CHUNK_SIZE_CHARS "
                f"(al menos {min_context_tokens(self.chunk_size_chars)})"
            )
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
