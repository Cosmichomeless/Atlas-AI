"""Pruebas de humo de un despliegue: el recorrido subir → indexar → preguntar → abrir la cita.

Se ejecuta contra una API ya levantada (Compose local, el E2E o cualquier otra) y solo usa la
biblioteca estándar, así que corre igual desde el host que dentro de la imagen del backend:

    uv run python -m app.smoke --api-url http://localhost:8000 --origin http://localhost:3000

Qué hace, en este orden, y se detiene en el primer fallo:

1. `/health` y `/health/db` responden (la API y la base de datos están vivas).
2. Crea una cuenta desechable (`smoke-<fecha>@example.com`) e inicia sesión.
3. Sube un documento **sintético** de unas pocas líneas y espera a que quede `READY`.
4. Una pregunta con respuesta en el documento devuelve `answered`, con una cita a ese documento.
5. El pasaje de esa cita se abre y contiene el texto original.
6. Una pregunta que el documento no contesta devuelve `abstained`.
7. Borra el documento de prueba.

No toca datos de nadie más: todo lo que crea es suyo y de contenido ficticio. Queda una cuenta de
prueba (no hay endpoint para borrar usuarios); el correo lo indica. No imprime contraseñas ni
tokens. Código de salida 0 si todo pasa y 1 si algo falla.
"""

import argparse
import http.cookiejar
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

DOCUMENT_NAME = "smoke-contrato.txt"
DOCUMENT_TEXT = "\n".join(
    [
        "La renta mensual del alquiler es de mil doscientos euros.",
        "El inquilino entrega la fianza el primer día del contrato.",
        "El proveedor del agua factura cada trimestre.",
    ]
)
QUESTION = "¿Cuál es la renta mensual del alquiler?"
PASSAGE_FRAGMENT = "renta mensual del alquiler"
UNANSWERABLE_QUESTION = "¿Qué altura tiene el Everest?"
CSRF_HEADER = "X-CSRF-Token"


class SmokeFailure(Exception):
    """Una comprobación falló; el mensaje dice cuál y por qué."""


@dataclass
class Client:
    """Cliente HTTP mínimo con cookies de sesión y el doble envío CSRF de la API."""

    api_url: str
    origin: str
    timeout: float = 30.0
    csrf_token: str | None = None
    jar: http.cookiejar.CookieJar = field(default_factory=http.cookiejar.CookieJar)

    def __post_init__(self) -> None:
        self.api_url = self.api_url.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        raw: tuple[bytes, str] | None = None,
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json", "Origin": self.origin}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data, headers["Content-Type"] = raw
        if method not in {"GET", "HEAD", "OPTIONS"} and self.csrf_token:
            headers[CSRF_HEADER] = self.csrf_token
        request = urllib.request.Request(  # noqa: S310 - la URL la elige quien ejecuta el script
            self.api_url + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        except urllib.error.URLError as error:
            raise SmokeFailure(f"{method} {path}: no se pudo conectar ({error.reason})") from error
        return status, _decode(payload)


def _decode(payload: bytes) -> Any:
    if not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return payload.decode(errors="replace")


def multipart_file(field_name: str, filename: str, content: bytes) -> tuple[bytes, str]:
    """Cuerpo `multipart/form-data` con un archivo de texto y su cabecera Content-Type."""
    boundary = f"----smoke{secrets.token_hex(8)}"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def expect_status(actual: int, wanted: int, what: str, payload: Any = None) -> None:
    if actual == wanted:
        return
    detail = payload.get("message") if isinstance(payload, dict) else None
    hint = (
        " Si es un 403, comprueba que --origin coincide con FRONTEND_ORIGIN de la API."
        if actual == 403
        else ""
    )
    raise SmokeFailure(
        f"{what}: se esperaba HTTP {wanted} y llegó {actual}"
        + (f" ({detail})" if detail else "")
        + hint
    )


def check_health(client: Client) -> None:
    status, body = client.request("GET", "/api/v1/health")
    expect_status(status, 200, "GET /health", body)
    status, body = client.request("GET", "/api/v1/health/db")
    expect_status(status, 200, "GET /health/db", body)
    expect(isinstance(body, dict) and body.get("status") == "ok", "la base de datos no está ok")


def refresh_csrf(client: Client) -> None:
    status, body = client.request("GET", "/api/v1/auth/csrf")
    expect_status(status, 200, "GET /auth/csrf", body)
    client.csrf_token = body["csrf_token"]


def sign_in(client: Client, email: str, password: str) -> None:
    credentials = {"email": email, "password": password}
    refresh_csrf(client)  # incluso el registro es una operación mutable y exige el doble envío
    status, body = client.request("POST", "/api/v1/auth/register", body=credentials)
    expect_status(status, 201, "POST /auth/register", body)
    status, body = client.request("POST", "/api/v1/auth/login", body=credentials)
    expect_status(status, 200, "POST /auth/login", body)
    refresh_csrf(client)  # el inicio de sesión puede rotar el token


def upload_and_wait(
    client: Client, *, timeout: float, sleep: Callable[[float], None] = time.sleep
) -> str:
    status, body = client.request(
        "POST",
        "/api/v1/documents",
        raw=multipart_file("file", DOCUMENT_NAME, DOCUMENT_TEXT.encode()),
    )
    expect_status(status, 201, "POST /documents", body)
    document_id = str(body["id"])
    deadline = time.monotonic() + timeout
    last = body["status"]
    while time.monotonic() < deadline:
        status, detail = client.request("GET", f"/api/v1/documents/{document_id}")
        expect_status(status, 200, "GET /documents/{id}", detail)
        last = detail["status"]
        if last == "READY":
            return document_id
        expect(last != "FAILED", f"la ingestión falló: {detail.get('error_summary')}")
        sleep(1.0)
    raise SmokeFailure(
        f"el documento no llegó a READY en {timeout:.0f} s (último estado: {last}); "
        "¿está arrancado el worker de ingestión?"
    )


def ask(client: Client, question: str, document_id: str) -> dict[str, Any]:
    status, body = client.request(
        "POST", "/api/v1/questions", body={"question": question, "document_ids": [document_id]}
    )
    expect_status(status, 200, "POST /questions", body)
    return dict(body)


def check_answer(client: Client, document_id: str) -> None:
    answer = ask(client, QUESTION, document_id)
    expect(
        answer["status"] == "answered", f"se esperaba una respuesta y llegó «{answer['status']}»"
    )
    expect(bool(answer["text"]), "la respuesta no tiene texto")
    citations = answer["citations"]
    expect(len(citations) >= 1, "la respuesta no cita ninguna fuente")
    expect(
        all(c["document_id"] == document_id and c["filename"] == DOCUMENT_NAME for c in citations),
        "una cita apunta a un documento que no es el subido",
    )
    citation = citations[0]
    status, passage = client.request(
        "GET", f"/api/v1/documents/{document_id}/chunks/{citation['chunk_id']}"
    )
    expect_status(status, 200, "GET /documents/{id}/chunks/{chunk}", passage)
    expect(PASSAGE_FRAGMENT in passage["text"], "el pasaje citado no contiene el texto original")


def check_abstention(client: Client, document_id: str) -> None:
    answer = ask(client, UNANSWERABLE_QUESTION, document_id)
    expect(
        answer["status"] == "abstained",
        "una pregunta sin respuesta en el documento debía abstenerse y llegó una respuesta "
        "(con LLM_PROVIDER=fake hace falta FAKE_LLM_GROUNDED=true: si no, el LLM falso "
        "siempre responde)",
    )
    expect(not answer["citations"], "una abstención no debe citar fuentes")


def cleanup(client: Client, document_id: str) -> None:
    status, body = client.request("DELETE", f"/api/v1/documents/{document_id}")
    expect(status in {200, 204}, f"DELETE /documents/{{id}}: HTTP {status} {body or ''}")


def run(
    client: Client, *, ingestion_timeout: float, email: str, password: str
) -> list[tuple[str, float]]:
    """Ejecuta las comprobaciones y devuelve (nombre, segundos) de cada una; lanza SmokeFailure."""
    timings: list[tuple[str, float]] = []
    state: dict[str, str] = {}

    def step(name: str, action: Callable[[], None]) -> None:
        started = time.monotonic()
        action()
        timings.append((name, time.monotonic() - started))

    def upload() -> None:
        state["document"] = upload_and_wait(client, timeout=ingestion_timeout)

    step("API y base de datos responden", lambda: check_health(client))
    step("registro, sesión y CSRF", lambda: sign_in(client, email, password))
    step("subir y esperar a READY", upload)
    step(
        "respuesta con cita al documento y pasaje original",
        lambda: check_answer(client, state["document"]),
    )
    step("abstención sin evidencia", lambda: check_abstention(client, state["document"]))
    step("borrar el documento de prueba", lambda: cleanup(client, state["document"]))
    return timings


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.smoke", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--api-url", default="http://localhost:8000", help="URL base de la API")
    parser.add_argument(
        "--origin",
        default="http://localhost:3000",
        help="Origen del frontend (debe coincidir con FRONTEND_ORIGIN de la API)",
    )
    parser.add_argument(
        "--ingestion-timeout", type=float, default=90.0, help="segundos máx. hasta READY"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    email = f"smoke-{stamp}-{uuid.uuid4().hex[:6]}@example.com"
    password = secrets.token_urlsafe(18)
    client = Client(api_url=args.api_url, origin=args.origin)
    print(f"Prueba de humo contra {client.api_url} (cuenta de prueba: {email})")
    try:
        timings = run(
            client, ingestion_timeout=args.ingestion_timeout, email=email, password=password
        )
    except SmokeFailure as failure:
        print(f"FALLO: {failure}", file=sys.stderr)
        return 1
    for name, seconds in timings:
        print(f"  ok  {name} ({seconds:.1f} s)")
    print(f"{len(timings)} comprobaciones superadas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
