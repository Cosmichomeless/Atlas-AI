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
| `npm run e2e` | Recorrido extremo a extremo con Playwright (ver abajo) |
| `npm run build` | Build de producción |
| `npm start` | Sirve el build de producción |

## Estructura

- `src/app/` — rutas, layouts y estilos globales (App Router).
- `src/app/(public)/` — `/login` y `/register`. `src/app/(app)/` — rutas privadas (`/documents`…), envueltas en `PrivateShell`.
- `src/components/` — componentes compartidos (`AuthForm`, `PrivateShell`) y `documents/` (`DocumentLibrary`, `DocumentRow`, `UploadForm`).
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

### Estado de ingestión y acciones

- **Refresco:** mientras la página visible tenga algún documento `UPLOADED` o `PROCESSING`,
  `DocumentLibrary` vuelve a consultar la lista cada 3 s (`POLL_INTERVAL_MS`). Cuando todos están en
  `READY`/`FAILED` el temporizador se cancela, también al desmontar o con la pestaña oculta. Un fallo
  del refresco en segundo plano no tapa la lista (se reintenta solo) y una respuesta antigua nunca
  pisa a una más reciente.
- **Documento fallido:** «Ver causa» pide `GET /documents/{id}` y muestra `error_summary` (la lista
  no lo incluye); si el servidor no registró causa se explica y se sugiere volver a subirlo.
- **Eliminar:** pide confirmación, desaparece de la lista al instante y se concilia con una recarga
  silenciosa. Está desactivado mientras se procesa; si aun así el servidor responde 409
  (`document_processing`) se mantiene el documento y se muestra el mensaje de reintento; un 404 se
  trata como «ya eliminado».

## Preguntas y respuestas

Ruta privada `/ask` (enlace «Preguntar» en el marco). `QuestionView` envía `POST /api/v1/questions`.

- **Alcance:** lista los documentos `READY` del usuario (`GET /documents?status=READY`, hasta 100) con
  casillas. Sin selección no se envía `document_ids` y el backend consulta todos los listos; con
  selección se envían solo los marcados. Sin documentos listos se guía a «Subir uno».
- **Pregunta:** `textarea` con contador y `maxLength` de 1000 (valor por defecto de
  `QUESTION_MAX_CHARS`; el servidor decide y devuelve 422 si no la acepta). No se envía vacía.
- **Sin duplicados:** una ref de «petición en vuelo» corta un segundo envío inmediato (Enter o doble
  clic) antes de que React deshabilite el botón; mientras tanto el formulario queda bloqueado.
- **Resultado, siempre distinguible:**
  - *Respuesta* (`article` «Respuesta»): texto con etiquetas `[S#]`, fuentes (archivo, página,
    sección), aviso si `truncated` y las frases sin fuente (`uncited_statements`).
  - *Abstención* (`article` «Sin respuesta»): es un 200 con `status: "abstained"`, no un error; el
    mensaje depende de `abstention_reason` (`src/lib/questions/messages.ts`).
  - *Error* (`role="alert"`): 409 `index_incompatible` (reindexar), 503 `embedding_unavailable` /
    `llm_unavailable` (reintentar más tarde), 422 y fallo de red con el mensaje del servidor.

### Citas y pasaje original

Cada fuente de la respuesta (`CitationSource`) es un botón con etiqueta, archivo y ubicación
(`p. 3 · Sección · líneas 4–9`, según lo que tenga el fragmento). Al pulsarlo pide
`GET /documents/{document_id}/chunks/{chunk_id}` y muestra el texto original del fragmento; volver a
pulsar lo oculta. `aria-expanded`/`aria-controls` lo hacen accesible y el botón se deshabilita
mientras carga, así que no hay peticiones repetidas.

- **Fuente borrada o no disponible:** si el documento se eliminó o se reindexó el servidor responde
  404 `passage_not_found` y se muestra «Esta fuente ya no está disponible…» sin texto ni reintento. El
  resto de fallos (red, 5xx) se explican y permiten reintentar pulsando de nuevo.
- **Backend:** el endpoint filtra por propietario y exige que el fragmento pertenezca al documento
  de la URL; ajeno, inexistente o borrado devuelven el mismo 404.

### Pruebas de los estados principales

`src/test/journeys.test.tsx` recorre las páginas reales (`/documents`, `/ask`) con la API
simulada (`mockApi`), sin red ni backend:

- Subida fallida (413) → se explica, no aparece documento → reintento correcto → «En cola» →
  «Procesando» → «Listo» por el refresco automático; y un documento `FAILED` muestra su causa.
- Pregunta sin evidencia (abstención, bloque propio y sin alerta) → pregunta mejor con respuesta que
  sustituye a la abstención.
- Con dos citas, cada una abre **su** pasaje (el de otro documento no se mezcla) y una fuente
  borrada avisa solo en su cita sin impedir abrir las demás.

Los componentes tienen además sus propias pruebas unitarias junto al código.

## Pruebas extremo a extremo

`e2e/` (Playwright, Chromium) ejercita el sistema real —Next.js, API, worker y Postgres— con proveedores
fake y sin credenciales de pago. Cubre registro → subida → «Listo» → pregunta → cita con su pasaje, la
abstención, el acceso sin sesión y el aislamiento entre usuarios.

```bash
npx playwright install chromium      # una sola vez
E2E_DATABASE_URL=postgresql+psycopg://atlas:atlas_dev_password@localhost:5434/atlas_e2e npm run e2e
```

`playwright.config.ts` levanta solo el backend (`backend/scripts/e2e_backend.py`, puerto 8765) y el
frontend (build de producción en el 3765). **Recrea la base de datos indicada, que debe llamarse
`*_e2e`**; nunca apuntes `E2E_DATABASE_URL` a datos que quieras conservar. Los specs no los recoge
Vitest (`npm test`).

## Integración continua

`.github/workflows/frontend.yml` se ejecuta en cada pull request (y en `main`): `npm ci`, `npm run lint`,
`npx next typegen` + `npm run typecheck`, `npm test`, `npm run build` y que `src/lib/api/schema.d.ts` esté al
día respecto a `backend/openapi.json` (`npm run api:types` no debe dejar diff). `LayoutProps`/`PageProps` son
tipos globales que genera Next, por eso el `typegen` previo: en un clon limpio `tsc` falla sin él. Las pruebas
E2E de Playwright necesitan backend y base de datos y no se ejecutan en este workflow.
