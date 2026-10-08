"""La pila completa de Compose arranca en orden, sin secretos incrustados y solo en loopback."""

from pathlib import Path
from typing import Any

import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"
COMPOSE: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text())
SERVICES: dict[str, Any] = COMPOSE["services"]


def test_the_full_stack_has_every_service() -> None:
    assert set(SERVICES) == {"db", "migrate", "api", "ingestion", "frontend"}


def test_migrations_run_once_before_the_api_and_the_worker() -> None:
    assert SERVICES["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert SERVICES["migrate"]["restart"] == "no"
    assert SERVICES["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"
    for name in ("api", "ingestion"):
        dependency = SERVICES[name]["depends_on"]["migrate"]
        assert dependency["condition"] == "service_completed_successfully"


def test_the_frontend_waits_for_a_healthy_api() -> None:
    assert SERVICES["frontend"]["depends_on"]["api"]["condition"] == "service_healthy"


def test_api_and_worker_share_the_uploads_volume() -> None:
    for name in ("api", "ingestion"):
        assert "atlas_data:/data" in SERVICES[name]["volumes"]
    assert {"atlas_pgdata", "atlas_data"} <= set(COMPOSE["volumes"])


def test_the_worker_has_time_to_finish_the_document_in_progress() -> None:
    assert SERVICES["ingestion"]["build"]["target"] == "ingestion"
    assert SERVICES["ingestion"]["stop_grace_period"] == "90s"


def test_the_backend_reaches_the_database_by_service_name_and_ignores_the_local_url() -> None:
    environment = SERVICES["api"]["environment"]
    assert "@db:5432/" in environment["DATABASE_URL"]
    assert SERVICES["migrate"]["environment"] == environment == SERVICES["ingestion"]["environment"]


def test_ports_are_only_published_on_loopback() -> None:
    for service in SERVICES.values():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:")


def test_no_secret_value_is_hardcoded() -> None:
    for service in SERVICES.values():
        environment = service.get("environment", {})
        for key in ("SECRET_KEY", "OPENAI_API_KEY"):
            if key in environment:
                assert str(environment[key]).startswith("${"), key
