"""Las pruebas de humo deben fallar con un mensaje claro, no con un traceback."""

import http.server
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app import smoke


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 403

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"message": "Origen no permitido."}')

    def log_message(self, format: str, *args: object) -> None:  # silencia el log del servidor
        return


@contextmanager
def stub_server(status: int) -> Iterator[str]:
    handler = type("Handler", (_Handler,), {"status": status})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_multipart_body_carries_the_file_and_a_matching_boundary() -> None:
    body, content_type = smoke.multipart_file("file", "a.txt", b"hola")
    boundary = content_type.split("boundary=")[1]
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert b'name="file"; filename="a.txt"' in body
    assert b"\r\n\r\nhola\r\n" in body
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_an_http_error_is_reported_with_the_server_message() -> None:
    with stub_server(403) as url:
        client = smoke.Client(api_url=url, origin="http://localhost:3000")
        with pytest.raises(
            smoke.SmokeFailure, match=r"GET /health: se esperaba HTTP 200 y llegó 403"
        ):
            smoke.check_health(client)


def test_an_unreachable_api_is_a_failure_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with stub_server(200) as url:
        pass  # el servidor ya está cerrado: nadie escucha en ese puerto
    assert smoke.main(["--api-url", url]) == 1
    assert "FALLO" in capsys.readouterr().err


def test_the_wait_for_ingestion_gives_up_with_a_useful_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient(smoke.Client):
        def request(self, method: str, path: str, **_: object) -> tuple[int, object]:
            if method == "POST":
                return 201, {"id": "d1", "status": "UPLOADED"}
            return 200, {"status": "UPLOADED"}

    client = FakeClient(api_url="http://x", origin="http://x")
    with pytest.raises(smoke.SmokeFailure, match="worker de ingestión"):
        smoke.upload_and_wait(client, timeout=0.05, sleep=lambda _: None)


def test_a_failed_ingestion_stops_the_wait_with_its_cause() -> None:
    class FakeClient(smoke.Client):
        def request(self, method: str, path: str, **_: object) -> tuple[int, object]:
            if method == "POST":
                return 201, {"id": "d1", "status": "UPLOADED"}
            return 200, {"status": "FAILED", "error_summary": "archivo ilegible"}

    client = FakeClient(api_url="http://x", origin="http://x")
    with pytest.raises(smoke.SmokeFailure, match="archivo ilegible"):
        smoke.upload_and_wait(client, timeout=5, sleep=lambda _: None)
