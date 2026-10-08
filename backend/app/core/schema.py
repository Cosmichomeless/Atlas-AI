"""Versión del esquema: el código solo debe trabajar contra una base migrada a su misma revisión.

La API y el worker salen de la misma imagen y comparten migraciones; este módulo comprueba que la
base de datos las tiene aplicadas, para que un worker nunca procese contra un esquema distinto.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


def expected_revision() -> str | None:
    """Revisión más reciente de las migraciones que lleva este código."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def applied_revision(engine: Engine) -> str | None:
    """Revisión aplicada en la base de datos; `None` si todavía no se ha migrado."""
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        return None


def is_current(engine: Engine) -> bool:
    return applied_revision(engine) == expected_revision()
