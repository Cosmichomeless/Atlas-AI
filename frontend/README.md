# Atlas AI — Frontend

Aplicación web en Next.js (App Router) y TypeScript.

## Comandos

| Comando | Descripción |
| --- | --- |
| `npm install` | Instala dependencias |
| `npm run dev` | Servidor de desarrollo en <http://localhost:3000> (`PORT=3100 npm run dev` para otro puerto) |
| `npm run lint` | ESLint |
| `npm run typecheck` | Comprobación de tipos con `tsc` |
| `npm run build` | Build de producción |
| `npm start` | Sirve el build de producción |

## Estructura

- `src/app/` — rutas, layouts y estilos globales (App Router).
- `src/app/globals.css` — variables de diseño (colores, espaciado, tipografía) y soporte de tema claro/oscuro.
