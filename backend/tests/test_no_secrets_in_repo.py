"""Ningún secreto debe estar versionado: ni claves reales ni archivos `.env` con valores."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Formas habituales de credenciales: claves tipo OpenAI, AWS, tokens de GitHub y claves privadas.
SECRET_PATTERNS = {
    "clave de API tipo sk-": re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}"),
    "clave de acceso de AWS": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "token de GitHub": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "clave privada": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico", ".webp", ".pdf", ".lock"}


def tracked_files() -> list[Path]:
    result = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("no es un repositorio git")
    names = [n for n in result.stdout.decode().split("\0") if n]
    return [ROOT / n for n in names if (ROOT / n).suffix.lower() not in SKIP_SUFFIXES]


def test_no_tracked_file_contains_a_credential() -> None:
    found = []
    for path in tracked_files():
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                found.append(f"{path.relative_to(ROOT)}: {label}")
    assert not found, "Posibles secretos versionados:\n" + "\n".join(found)


def test_only_the_example_env_files_are_tracked() -> None:
    env_files = [
        p.relative_to(ROOT).as_posix()
        for p in tracked_files()
        if p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example")
    ]
    assert env_files == []


def test_env_example_has_no_real_secret_values() -> None:
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.split("#")[0].strip()
    for key in ("SECRET_KEY", "OPENAI_API_KEY"):
        assert values[key] == "", f"{key} debe ir vacío en .env.example"
