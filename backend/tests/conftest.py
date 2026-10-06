import os

# Credenciales de desarrollo local definidas en docker-compose.yml (solo para la base de pruebas).
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://atlas:atlas_dev_password@localhost:5433/atlas_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
