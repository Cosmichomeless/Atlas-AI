"""Exporta el contrato OpenAPI a `backend/openapi.json`.

Es la fuente del cliente tipado del frontend: `uv run python -m app.openapi_export`.
"""

import json
import os
from pathlib import Path

OPENAPI_PATH = Path(__file__).resolve().parents[1] / "openapi.json"


def render_openapi() -> str:
    from app.main import create_app

    schema = create_app().openapi()
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    # Generar el contrato no necesita una base de datos real.
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://export:export@localhost/export")
    OPENAPI_PATH.write_text(render_openapi(), encoding="utf-8")
    print(f"Escrito {OPENAPI_PATH}")  # noqa: T201


if __name__ == "__main__":
    main()
