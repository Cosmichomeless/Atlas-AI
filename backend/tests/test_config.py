import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from app.core.config import REPO_ROOT, Settings, get_settings

DB_URL = "postgresql+psycopg://u:p@localhost:5433/db"


def make(**values: object) -> Settings:
    """Settings aislados: sin `.env` ni variables heredadas del entorno."""
    with pytest.MonkeyPatch.context() as mp:
        for name in Settings.model_fields:
            mp.delenv(name.upper(), raising=False)
        return Settings(_env_file=None, database_url=DB_URL, **values)  # type: ignore[call-arg, arg-type]


def test_defaults_are_safe_for_development() -> None:
    settings = make()
    assert settings.app_env == "development"
    assert settings.embedding_provider == "fake"
    assert settings.llm_provider == "fake"
    assert settings.embedding_dimensions == 1536
    assert settings.secret_key is None
    assert settings.openai_api_key is None


def test_test_suite_runs_with_test_environment_and_fake_providers() -> None:
    # conftest.py fija estas variables antes de importar la aplicación.
    assert os.environ["APP_ENV"] == "test"
    assert os.environ["EMBEDDING_PROVIDER"] == "fake"


def test_secrets_are_not_leaked_in_repr() -> None:
    settings = make(embedding_provider="openai", openai_api_key="sk-super-secret")
    assert "sk-super-secret" not in repr(settings)
    assert "sk-super-secret" not in str(settings.model_dump())
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "sk-super-secret"


@pytest.mark.parametrize("field", ["embedding_provider", "llm_provider"])
def test_openai_provider_requires_api_key(field: str) -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        make(**{field: "openai"})
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        make(**{field: "openai", "openai_api_key": ""})


def test_production_requires_strong_secret_key() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make(app_env="production")
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make(app_env="production", secret_key="short")
    assert make(app_env="production", secret_key="x" * 32).app_env == "production"


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(embedding_provider="other")


def test_env_file_is_read_and_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={DB_URL}\nMAX_UPLOAD_MB=5\nFRONTEND_ORIGIN=http://a.test\n")
    monkeypatch.setenv("MAX_UPLOAD_MB", "9")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.max_upload_mb == 9
    assert settings.frontend_origin == "http://a.test"


def test_get_settings_reads_test_database() -> None:
    get_settings.cache_clear()
    assert get_settings().database_url.endswith("/atlas_test")


def test_env_example_documents_every_setting_without_real_secrets() -> None:
    values = dotenv_values(REPO_ROOT / ".env.example")
    for name in Settings.model_fields:
        assert name.upper() in values, f"{name.upper()} falta en .env.example"
    assert not values["OPENAI_API_KEY"]
    assert not values["SECRET_KEY"]
    assert "sk-" not in (REPO_ROOT / ".env.example").read_text()
    # Los ejemplos cargan sin errores con el modelo de configuración.
    env_example = {k: v or "" for k, v in values.items() if v is not None}
    for k in ("OPENAI_API_KEY", "SECRET_KEY"):
        env_example.pop(k)
    keys = {k.lower(): v for k, v in env_example.items() if k.lower() in Settings.model_fields}
    with pytest.MonkeyPatch.context() as mp:
        for name in Settings.model_fields:
            mp.delenv(name.upper(), raising=False)
        Settings(_env_file=None, **keys)  # type: ignore[call-arg, arg-type]
