import os

# Credenciales de desarrollo local definidas en docker-compose.yml (solo para la base de pruebas).
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://atlas:atlas_dev_password@localhost:5433/atlas_test",
)

# La suite nunca debe usar proveedores reales ni la base de desarrollo, aunque exista un `.env`
# con claves: las variables de entorno tienen prioridad sobre ese archivo.
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "EMBEDDING_PROVIDER": "fake",
        "LLM_PROVIDER": "fake",
        "OPENAI_API_KEY": "",
    }
)
