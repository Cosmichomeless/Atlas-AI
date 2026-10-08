"""El worker corre en su propio proceso: debe poder configurar los mappers sin la API."""

import subprocess
import sys
from pathlib import Path


def test_the_worker_module_registers_every_model_in_a_clean_process() -> None:
    code = (
        "import app.ingestion.worker\n"
        "from sqlalchemy.orm import configure_mappers\n"
        "configure_mappers()\n"
        "from app.core.base import Base\n"
        "print(sorted(Base.metadata.tables))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert result.returncode == 0, result.stderr
    for table in ("users", "documents", "usage_days"):
        assert f"'{table}'" in result.stdout
