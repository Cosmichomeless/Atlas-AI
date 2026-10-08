"""La imagen del frontend no debe incorporar secretos y debe poder vigilarse."""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
DOCKERFILE = (FRONTEND / "Dockerfile").read_text()
IGNORED = {
    line.strip()
    for line in (FRONTEND / ".dockerignore").read_text().splitlines()
    if line.strip() and not line.startswith("#")
}


def test_secrets_and_local_artifacts_are_kept_out_of_the_build_context() -> None:
    assert {".env", ".env.*", "**/.env", "node_modules/", ".next/"} <= IGNORED


def test_the_image_bakes_no_secret_values() -> None:
    env_lines = re.findall(r"^\s*(?:ENV|ARG)\s+(.*)$", DOCKERFILE, flags=re.MULTILINE)
    assert not [line for line in env_lines if re.search(r"SECRET|PASSWORD|API_KEY|TOKEN", line)]


def test_the_container_runs_as_a_non_root_user_with_a_health_check() -> None:
    assert re.search(r"^USER (?!root)\S+", DOCKERFILE, flags=re.MULTILINE)
    assert "HEALTHCHECK" in DOCKERFILE


def test_dependencies_come_from_the_lockfile_and_the_build_is_standalone() -> None:
    assert "npm ci" in DOCKERFILE
    assert "NEXT_OUTPUT=standalone" in DOCKERFILE
    assert '"server.js"' in DOCKERFILE
