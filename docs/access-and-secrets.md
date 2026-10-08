# Acceso, secretos y cookies

Cómo se gestionan las credenciales, quién puede crear cuentas y qué configuración de cookies, CORS y HTTPS
necesita cada forma de ejecutar Atlas AI (issue #66). El proyecto se ejecuta **en local** (Docker Compose o
procesos sueltos); el frontend puede servirse además desde Vercel. No hay nada desplegado en un proveedor
de nube de pago (la alternativa estudiada está en [azure-deployment.md](azure-deployment.md)).

## Secretos

| Dato | Dónde vive | Dónde no debe estar |
| --- | --- | --- |
| `SECRET_KEY` (firma del token CSRF), `OPENAI_API_KEY`, contraseña de la base de datos | Variables de entorno o `.env` (ignorado por git) | Git, imágenes Docker, el navegador |
| Contraseñas de usuario | Hash Argon2id en PostgreSQL | En claro en ningún sitio, ni en logs |
| `NEXT_PUBLIC_API_BASE_URL` | Variable de **compilación** del frontend | — es pública a propósito: acaba en el JavaScript del navegador |

Garantías comprobadas por tests (`backend/tests/`):

- `test_no_secrets_in_repo.py`: ningún archivo versionado contiene claves tipo `sk-…`, `AKIA…`, tokens de GitHub ni
  claves privadas; solo `.env.example` está versionado y sus `SECRET_KEY`/`OPENAI_API_KEY` van vacíos.
- `test_docker_files.py`, `test_frontend_docker_files.py` y `test_compose_file.py`: las imágenes no incorporan valores
  de secretos (`ENV`/`ARG`), el contexto de build excluye `.env*` y `docker-compose.yml` solo referencia variables.

Si publicas una clave por error: revócala en el proveedor, **no** basta con borrarla en un commit posterior.

## Quién puede crear cuentas

Por defecto el registro está abierto (`REGISTRATION_ENABLED=true`). Cuando la API sea accesible desde fuera de tu
máquina (túnel, red local compartida), crea tus cuentas y ciérralo:

```bash
REGISTRATION_ENABLED=false     # en .env; reinicia la API
```

Con el registro cerrado `POST /auth/register` responde **403** con el código `registration_closed`, la página de
registro muestra «El registro está cerrado en este servidor.» y las cuentas existentes siguen entrando. No hay
invitaciones ni verificación de email: es un interruptor, no un sistema de altas.

Las cuotas por usuario (`USAGE_DAILY_QUESTIONS`, `USAGE_DAILY_TOKENS`) acotan el gasto con un proveedor de IA de
pago aunque haya varias cuentas.

## Cookies, CORS y HTTPS según el escenario

La sesión y el token CSRF viajan en cookies `HttpOnly`. Tres variables lo controlan: `FRONTEND_ORIGIN` (el único
origen que acepta CORS y la comprobación de `Origin`), `COOKIE_SAMESITE` y `COOKIE_SECURE`.

| Escenario | `FRONTEND_ORIGIN` | `COOKIE_SAMESITE` | `COOKIE_SECURE` | `NEXT_PUBLIC_API_BASE_URL` |
| --- | --- | --- | --- | --- |
| Todo en local, sin HTTPS (por defecto) | `http://localhost:3000` | `lax` | vacío | `http://localhost:8000` |
| Frontend en Vercel + API local expuesta con un túnel HTTPS (sitios distintos) | `https://<tu-app>.vercel.app` | `none` | `true` | `https://<túnel>` |
| Frontend y API bajo el mismo dominio con HTTPS (p. ej. `app.` y `api.`) | `https://app.ejemplo.com` | `lax` | `true` | `https://api.ejemplo.com` |

Reglas que la API impone al arrancar (`Settings`): `COOKIE_SAMESITE=none` exige cookies `Secure`, y con
`APP_ENV=production` hacen falta `SECRET_KEY` de al menos 32 caracteres y cookies `Secure`.

Lo verifica `test_cross_site_deployment.py` para el escenario de Vercel: CORS solo admite el dominio del
frontend (con credenciales), las cookies salen `SameSite=None; Secure; HttpOnly` y una petición con otro
`Origin` es rechazada (403) aunque lleve un token CSRF válido.

### Limitaciones del escenario con Vercel

- **Las cookies entre sitios dependen del navegador.** Safari y los navegadores con bloqueo de cookies de
  terceros pueden descartar una cookie `SameSite=None` de otro sitio y la sesión no se mantendrá. Si pasa, la
  solución es servir frontend y API bajo el mismo sitio (tercera fila).
- **Un túnel gratuito cambia de URL** al reiniciarse: hay que actualizar `NEXT_PUBLIC_API_BASE_URL` y volver a
  desplegar el frontend (la variable se fija al compilar), y `FRONTEND_ORIGIN` si cambia el dominio de Vercel.
- **Solo funciona mientras tu equipo, Compose y el túnel estén encendidos.**
- Este documento y sus tests describen y comprueban la configuración de la API; **no he podido comprobar un
  despliegue concreto en Vercel**.
