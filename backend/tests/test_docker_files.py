"""La imagen no debe incorporar secretos ni datos de usuarios, y debe poder vigilarse."""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DOCKERFILE = (BACKEND / "Dockerfile").read_text()
IGNORED = {
    line.strip()
    for line in (BACKEND / ".dockerignore").read_text().splitlines()
    if line.strip() and not line.startswith("#")
}


def test_secrets_and_user_files_are_kept_out_of_the_build_context() -> None:
    assert {".env", ".env.*", "**/.env", "storage/", "data/", ".venv/"} <= IGNORED


def test_the_image_bakes_no_secret_values() -> None:
    env_lines = re.findall(r"^\s*(?:ENV|ARG)\s+(.*)$", DOCKERFILE, flags=re.MULTILINE)
    assert not [line for line in env_lines if re.search(r"SECRET|PASSWORD|API_KEY|TOKEN", line)]
    instructions = [line for line in DOCKERFILE.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in instructions if "DATABASE_URL" in line]


def test_the_container_runs_as_a_non_root_user_with_a_health_check() -> None:
    assert re.search(r"^USER (?!root)\S+", DOCKERFILE, flags=re.MULTILINE)
    assert "HEALTHCHECK" in DOCKERFILE
    assert "/health" in DOCKERFILE


def test_dependencies_are_installed_from_the_lockfile() -> None:
    assert "uv sync --frozen" in DOCKERFILE
    assert "COPY app " in DOCKERFILE
    assert "COPY migrations " in DOCKERFILE


def test_the_ingestion_stage_runs_the_worker_and_the_api_stays_the_default() -> None:
    stages = re.findall(r"^FROM .* AS (\w+)$", DOCKERFILE, flags=re.MULTILINE)
    assert stages[-1] == "api"
    ingestion = DOCKERFILE.split(" AS ingestion", 1)[1].split(" AS api", 1)[0]
    assert "app.ingestion.worker" in ingestion
    assert "app.ingestion.heartbeat" in ingestion
    assert "STOPSIGNAL SIGTERM" in ingestion
    assert "INGESTION_HEARTBEAT_FILE" in ingestion
