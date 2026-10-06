# Atlas AI — Frontend

Aplicación web en Next.js (App Router) y TypeScript.

## Comandos

| Comando | Descripción |
| --- | --- |
| `npm install` | Instala dependencias |
| `npm run dev` | Servidor de desarrollo en <http://localhost:3000> (`PORT=3100 npm run dev` para otro puerto) |
| `npm run lint` | ESLint |
| `npm run typecheck` | Comprobación de tipos con `tsc` |
| `npm test` | Tests unitarios (Vitest) |
| `npm run api:types` | Regenera `src/lib/api/schema.d.ts` desde `../backend/openapi.json` |
| `npm run build` | Build de producción |
| `npm start` | Sirve el build de producción |

## Estructura

- `src/app/` — rutas, layouts y estilos globales (App Router).
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
