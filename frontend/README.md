# Atlas AI — Frontend

Aplicación web en Next.js (App Router) y TypeScript.

## Comandos

| Comando | Descripción |
| --- | --- |
| `npm install` | Instala dependencias |
| `npm run dev` | Servidor de desarrollo en <http://localhost:3000> (`PORT=3100 npm run dev` para otro puerto) |
| `npm run lint` | ESLint |
| `npm run typecheck` | Comprobación de tipos con `tsc` |
| `npm test` | Tests (Vitest + Testing Library, entorno jsdom, API simulada) |
| `npm run api:types` | Regenera `src/lib/api/schema.d.ts` desde `../backend/openapi.json` |
| `npm run build` | Build de producción |
| `npm start` | Sirve el build de producción |

## Estructura

- `src/app/` — rutas, layouts y estilos globales (App Router).
- `src/app/(public)/` — `/login` y `/register`. `src/app/(app)/` — rutas privadas (`/documents`…), envueltas en `PrivateShell`.
- `src/components/` — componentes compartidos (`AuthForm`, `PrivateShell`) y `documents/` (`DocumentLibrary`, `UploadForm`).
- `src/lib/documents/` — reglas de validación de subida y etiquetas de estado.
- `src/test/` — utilidades de test: `mockApi` (sustituye `fetch` por un servidor simulado) y doble de `next/navigation`.
- `src/app/globals.css` — variables de diseño (colores, espaciado, tipografía) y soporte de tema claro/oscuro.

## Cliente de la API

`src/lib/api/` contiene el cliente tipado ([openapi-fetch](https://openapi-ts.dev/openapi-fetch/)) generado a
partir del contrato OpenAPI del backend; no duplica lógica de negocio.

- `schema.d.ts` — tipos generados (no editar a mano; `npm run api:types`).
- `client.ts` — `api` (cliente con `credentials: "include"`, base `NEXT_PUBLIC_API_BASE_URL`) y `unwrap()`,
  que devuelve los datos o lanza `ApiError`.
- `csrf.ts` — middleware instalado en el cliente: antes de POST/PUT/PATCH/DELETE obtiene el token de
  `/api/v1/auth/csrf` (una vez, en memoria) y lo envía en `X-CSRF-Token`; lo descarta si el servidor responde
  `csrf_failed`.
- `errors.ts` — `ApiError` (`status`, `code`, `message`, `requestId`, `details`) para el formato común de errores.
- `health.ts` — ejemplo: `getHealth()` y `getDatabaseHealth()`.

## Sesión y rutas privadas

La sesión es una cookie **HttpOnly** del origen de la API: el navegador la envía sola
(`credentials: "include"`) pero el código de la web nunca la lee ni la guarda. Por eso el estado
de sesión se pregunta al servidor.

- `src/lib/auth/session.tsx` — `SessionProvider` (en el layout raíz) y `useSession()`. Estados:
  `loading`, `authenticated` (con el usuario), `anonymous` (401 de `/auth/me`) y `error` (no se pudo
  comprobar: red o servidor).
- `PrivateShell` protege `(app)/`: sin sesión redirige a `/login` y **no pinta** el contenido; si no
  se pudo comprobar la sesión muestra un aviso con «Reintentar» en vez de expulsar. Incluye el
  cierre de sesión (si falla, la persona sigue dentro y se le avisa).
- `AuthForm` (login y registro): errores de validación asociados a su campo (`details[].loc`), error
  general en un `role="alert"`, y el botón se desactiva mientras se envía (sin dobles envíos). Tras
  registrarse se abre sesión automáticamente (el backend no la crea al registrar).

La redirección se hace en el cliente porque la cookie de sesión no es legible por el servidor de
Next cuando la API vive en otro origen; **la autorización real es siempre la del backend** (cada
endpoint exige sesión y filtra por propietario). El guard solo evita mostrar pantallas vacías.

## Biblioteca de documentos

`/documents` (dentro de `(app)/`) lista los documentos **del usuario en sesión**: el filtrado por
propietario lo hace el backend, la web solo pinta lo que `GET /api/v1/documents` devuelve.

- `DocumentLibrary` — lista paginada (20 por página, `limit`/`offset`), estado vacío, error de carga
  con «Reintentar» y etiqueta de estado (En cola, Procesando, Listo, Falló). Al subir un documento
  vuelve a la primera página; si una página queda vacía retrocede a la última que exista.
- `UploadForm` — valida en el cliente la extensión (`.pdf`, `.txt`, `.md`, `.markdown`), que no esté
  vacío y el máximo de 20 MB **antes** de enviar (`src/lib/documents/validation.ts`; la validación
  real es la del servidor). Muestra barra de progreso, impide dobles envíos y enseña el mensaje del
  servidor ante 413 / 415 / 422 o fallo de red; un error del servidor no bloquea reintentar.
- `src/lib/api/upload.ts` — la subida usa `XMLHttpRequest` (y no `fetch`) porque `fetch` no informa
  del progreso de envío. Reutiliza el mismo almacén de token CSRF que el cliente tipado
  (`csrf` en `client.ts`) y reintenta una vez con un token nuevo si el servidor responde
  `csrf_failed`. Admite `AbortSignal`.
