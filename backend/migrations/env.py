from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.base import Base
from app.core.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Importar aquí los módulos con modelos para que autogenerate los detecte.
from app.embeddings import models as _embeddings  # noqa: E402, F401
from app.features.auth import models as _auth  # noqa: E402, F401
from app.features.documents import models as _documents  # noqa: E402, F401
from app.features.usage import models as _usage  # noqa: E402, F401
from app.features.users import models as _users  # noqa: E402, F401

target_metadata = Base.metadata


def _database_url() -> str:
    # `attributes["url"]` permite a los tests apuntar a una base temporal.
    url = config.attributes.get("url")
    return str(url) if url else get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
