import re
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "X-Request-ID"
_VALID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIdMiddleware:
    """Asigna un `request_id` a cada solicitud y lo devuelve en la cabecera `X-Request-ID`.

    Reutiliza el valor entrante si es seguro (alfanumérico, `._-`, hasta 64 caracteres).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = next((v for k, v in scope["headers"] if k == HEADER.lower().encode()), b"")
        candidate = incoming.decode("latin-1")
        request_id = candidate if _VALID.match(candidate) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_id)
