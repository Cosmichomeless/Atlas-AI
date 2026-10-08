# Despliegue local (Compose) y frontend en Vercel

Cómo se ejecuta Atlas AI de forma completa **en tu máquina**, cómo se vigila que cada proceso está vivo y cómo se
publica solo el frontend en Vercel (issue #68). No hay servicios de nube de pago: la alternativa que se estudió
(Azure) está en [azure-deployment.md](azure-deployment.md) y quedó descartada.

## Pila local con Docker Compose

```bash
cp .env.example .env               # los valores por defecto sirven; SECRET_KEY es opcional en desarrollo
docker compose up -d --build       # db → migrate (una vez) → api + ingestion → frontend
docker compose ps                  # db, api, ingestion y frontend en «healthy»; migrate termina con código 0
```

| Proceso | Qué es | URL / puerto | Cómo se sabe que está vivo |
| --- | --- | --- | --- |
| `db` | PostgreSQL 16 + pgvector | `127.0.0.1:5433` | `pg_isready` cada 5 s |
| `migrate` | `alembic upgrade head`, una sola vez | — | Debe terminar con código 0; API y worker esperan a ello |
| `api` | FastAPI | <http://localhost:8000> (`/docs`) | `GET /health` cada 10 s (solo vivo) y `GET /api/v1/health/db` (base de datos) |
| `ingestion` | Worker de ingestión | — (sin puerto) | **Latido**: reescribe un archivo tras cada vuelta sana; `python -m app.ingestion.heartbeat` falla si es más antiguo que `INGESTION_LEASE_SECONDS` |
| `frontend` | Next.js `standalone` | <http://localhost:3000> | `GET /` cada 10 s |

Los puertos solo se publican en `127.0.0.1`. Docker marca un contenedor como `unhealthy` cuando falla su
comprobación; `restart: unless-stopped` reinicia los que **terminan**, pero **no** los que quedan `unhealthy` sin
terminar: míralos con `docker compose ps` o `docker inspect --format '{{.State.Health.Status}}' atlas-ingestion`.

Comprobado con el worker real fuera de Docker: con el worker en marcha el comando de latido sale con 0, y 9 s después
de pararlo (con un arriendo de 6 s) sale con 1. Los health checks de las imágenes ejecutan esos mismos comandos.

### Comprobar que todo funciona de verdad

```bash
docker compose exec api python -m app.smoke --api-url http://localhost:8000 --origin http://localhost:3000
```

Sube un documento de prueba, espera a `READY`, pregunta, abre la cita y comprueba la abstención (ver
[backend/README.md](../backend/README.md)). Con los proveedores `fake` (el valor por defecto) la comprobación de abstención exige `FAKE_LLM_GROUNDED=true` en `.env` (después, `docker compose up -d`): con `false` el LLM falso siempre responde y la prueba falla en ese paso. Si el worker no corre, falla con «¿está arrancado el worker de
ingestión?». La calidad de las respuestas se mide con `python -m app.evaluation.gate`.

### Operación

```bash
docker compose logs -f api ingestion      # registros
docker compose stop api ingestion         # parada limpia: el worker termina el documento en curso (hasta 90 s)
docker compose up -d --build              # actualizar tras un git pull (vuelve a migrar)
scripts/backup.sh                         # copia de seguridad: ver backup-and-restore.md
docker compose down                       # detener (los datos se conservan); `down -v` los borra
```

Configuración, secretos y registro: [access-and-secrets.md](access-and-secrets.md). Copias y restauración:
[backup-and-restore.md](backup-and-restore.md).

## Frontend en Vercel

Vercel solo puede alojar el frontend (Next.js). La API, el worker y la base de datos siguen en tu máquina, así que el
frontend publicado **solo muestra el flujo completo si puede llegar a una API**. Hay dos formas de usarlo:

1. **Solo escaparate (sin API pública).** La portada, el acceso y el registro se ven, pero no se puede iniciar sesión
   ni usar la aplicación: las peticiones a `NEXT_PUBLIC_API_BASE_URL` fallan y la interfaz muestra «No se pudo
   conectar con el servidor». Es lo que se obtiene con el valor por defecto (`http://localhost:8000`), que solo
   existe en el equipo de quien lo abre.
2. **Con tu API expuesta por un túnel HTTPS** (p. ej. Cloudflare Tunnel o ngrok hacia `localhost:8000`). Funciona
   mientras tu equipo, Compose y el túnel estén encendidos. Configuración exacta en
   [access-and-secrets.md](access-and-secrets.md) (fila «Frontend en Vercel + API local»): en resumen
   `FRONTEND_ORIGIN=https://<tu-app>.vercel.app`, `COOKIE_SAMESITE=none`, `COOKIE_SECURE=true` y, para
   evitar que cualquiera cree cuentas en una API expuesta, `REGISTRATION_ENABLED=false` tras crear las tuyas.

### Pasos en Vercel

1. *Add New → Project* y elige el repositorio.
2. **Root Directory: `frontend`** (el repositorio es un monorepo). Framework: Next.js (se detecta solo). Node.js 22.
3. *Environment Variables*: `NEXT_PUBLIC_API_BASE_URL` = URL pública (HTTPS) de la API. **Se fija al compilar**: si
   cambia (p. ej. un túnel nuevo), hay que volver a desplegar.
4. No hace falta `NEXT_OUTPUT`: esa variable solo la usa la imagen Docker (`standalone`).

La instancia del proyecto está en <https://atlas-ai-one-silk.vercel.app>. Vercel despliega cada `push` a `main` en
*Production* y cada PR en *Preview*, y GitHub guarda el resultado: `gh api repos/<usuario>/<repo>/deployments`. El último
despliegue de *Production* revisado (el commit de #140, `83a6299`) terminó en `success`, es decir, **el frontend
compila y se publica**. Lo que no he podido hacer es abrir la web (la red del entorno donde se desarrolló bloquea
`vercel.app`), así que no he visto cómo se ve ni confirmado a qué API apunta `NEXT_PUBLIC_API_BASE_URL`; sin una API
accesible, la aplicación no funciona más allá de la portada y el acceso.

## Qué está desplegado y qué no

- **Verificado**: el flujo completo (API, worker, PostgreSQL con pgvector, frontend compilado) con los E2E de Playwright
  y las pruebas de humo, y los health checks del worker.
- **Sin verificar**: los contenedores de Docker Compose (el entorno de desarrollo no tiene Docker; la configuración se
  valida con `docker compose config` y tests de ficheros) y el aspecto y la conexión del frontend ya publicado en Vercel
  (solo consta que el despliegue terminó bien).
- **Sin desplegar**: no hay API pública permanente, ni dominio propio, ni HTTPS gestionado.
