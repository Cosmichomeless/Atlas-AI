"""Análisis puro de archivos: de bytes a bloques de texto, con límites duros.

Este módulo no importa nada del resto de la aplicación a propósito: se puede ejecutar como script en
un subproceso aislado (`python -I parsing.py`, ver `isolation.py`) sin cargar FastAPI ni SQLAlchemy,
y así un parser colgado o descontrolado se puede matar sin tocar al worker. El mismo código sirve en
proceso (`parse`) para las pruebas y para quien desactive el aislamiento.

Protocolo del subproceso: argumentos `tipo máx_páginas máx_caracteres plazo_segundos`, el archivo
por la entrada estándar y un único JSON por la salida estándar (`{"blocks": [...]}` o
`{"error": "..."}`). Cualquier otro final (código distinto de 0, señal, plazo agotado) lo trata
el proceso padre.
"""

import io
import json
import re
import signal
import sys
from dataclasses import asdict, dataclass

from pypdf import PdfReader
from pypdf.errors import PyPdfError

PDF = "application/pdf"
TEXT = "text/plain"
MARKDOWN = "text/markdown"

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")

# Margen entre el plazo del padre y el vigilante del hijo: el padre mata primero y el vigilante solo
# actúa si el padre desaparece (así no queda ningún proceso huérfano).
WATCHDOG_GRACE_SECONDS = 5


class ParseError(Exception):
    """El archivo no se puede convertir en texto; el mensaje está pensado para el usuario."""


@dataclass(frozen=True)
class Limits:
    """Topes de recursos del análisis; el exceso es un error del archivo, no del sistema."""

    max_pages: int
    max_chars: int


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    page: int | None = None
    section: str | None = None
    start_line: int | None = None
    end_line: int | None = None


def parse(content_type: str, data: bytes, limits: Limits) -> list[ParsedBlock]:
    match content_type:
        case "application/pdf":
            blocks = _parse_pdf(data, limits)
        case "text/markdown":
            blocks = _parse_text(data, limits, markdown=True)
        case "text/plain":
            blocks = _parse_text(data, limits, markdown=False)
        case _:
            raise ParseError("Tipo de archivo no compatible con la extracción de texto.")
    if not blocks:
        raise ParseError("El archivo no contiene texto.")
    return blocks


def _too_long(limits: Limits) -> ParseError:
    return ParseError(
        f"El texto del documento supera el máximo permitido ({limits.max_chars:,} caracteres)."
    )


def _parse_pdf(data: bytes, limits: Limits) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    total = 0
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ParseError("El PDF está protegido con contraseña y no se puede leer.")
        pages = len(reader.pages)
        if pages > limits.max_pages:
            raise ParseError(
                f"El PDF tiene {pages:,} páginas y el máximo permitido es {limits.max_pages:,}."
            )
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                total += len(text)
                if total > limits.max_chars:
                    raise _too_long(limits)
                blocks.append(ParsedBlock(text, page=number))
    except ParseError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, RecursionError, OSError) as exc:
        raise ParseError("El PDF está dañado o no se puede leer.") from exc
    if not blocks:
        raise ParseError(
            "El PDF no contiene texto extraíble: parece un escaneo o solo tiene imágenes."
        )
    return blocks


def _parse_text(data: bytes, limits: Limits, *, markdown: bool) -> list[ParsedBlock]:
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("El archivo no está codificado en UTF-8.") from exc
    if len(content) > limits.max_chars:
        raise _too_long(limits)

    blocks: list[ParsedBlock] = []
    headings: list[tuple[int, str]] = []
    paragraph: list[str] = []
    start = 0
    in_fence = False
    lines = content.splitlines()

    def flush(end_line: int) -> None:
        text = "\n".join(paragraph).strip()
        if text:
            section = " > ".join(title for _, title in headings) or None
            blocks.append(ParsedBlock(text, None, section, start, end_line))
        paragraph.clear()

    for number, line in enumerate(lines, start=1):
        if markdown and _FENCE.match(line):
            in_fence = not in_fence
        heading = None if (not markdown or in_fence) else _HEADING.match(line)
        if heading:
            flush(number - 1)
            level, title = len(heading.group(1)), heading.group(2).strip()
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            start = number
            paragraph.append(line)
        elif not line.strip() and not in_fence:
            # Un encabezado suelto no es un bloque útil: se une al párrafo que le sigue
            if not (markdown and len(paragraph) == 1 and _HEADING.match(paragraph[0])):
                flush(number - 1)
        else:
            if not paragraph:
                start = number
            paragraph.append(line)
    flush(len(lines))
    return blocks


def _confine(timeout_seconds: int) -> None:
    """Pone el tope de CPU y el vigilante de reloj; nunca debe sobrevivir a su padre."""
    try:
        import resource

        cpu = max(1, timeout_seconds)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    except (ImportError, ValueError, OSError):
        pass  # Plataforma sin `resource`: quedan el plazo del padre y el vigilante
    if hasattr(signal, "SIGALRM"):
        signal.alarm(timeout_seconds + WATCHDOG_GRACE_SECONDS)


def _child_main(argv: list[str]) -> int:
    content_type, max_pages, max_chars, timeout = argv[0], int(argv[1]), int(argv[2]), int(argv[3])
    _confine(timeout)
    data = sys.stdin.buffer.read()
    try:
        blocks = parse(content_type, data, Limits(max_pages, max_chars))
        result: dict[str, object] = {"blocks": [asdict(b) for b in blocks]}
    except ParseError as exc:
        result = {"error": str(exc)}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))
