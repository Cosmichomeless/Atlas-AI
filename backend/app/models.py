"""Registro de todos los modelos ORM.

SQLAlchemy resuelve las claves foráneas por nombre al configurar los mappers, así que cualquier
proceso que abra sesiones (API, worker, migraciones) necesita tener importados todos los modelos.
Importar este módulo basta; la API lo consigue de rebote por sus routers, el worker no.
"""

from app.embeddings import models as _embeddings  # noqa: F401
from app.features.auth import models as _auth  # noqa: F401
from app.features.documents import models as _documents  # noqa: F401
from app.features.usage import models as _usage  # noqa: F401
from app.features.users import models as _users  # noqa: F401
