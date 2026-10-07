"""Qué afirma una respuesta y en qué se apoya: etiquetas de fuente por afirmación."""

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

EXTERNAL_MARKER = "(No consta en los documentos)"
"""Prefijo con el que el modelo declara que una frase no sale de los documentos."""

_CITATION = re.compile(r"\[\s*S\d+(?:\s*[,;]\s*S\d+)*\s*\]")
_LABEL = re.compile(r"S\d+")
_LEADING_CITATION = re.compile(r"^" + _CITATION.pattern + r"\s*")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")

Kind = Literal["cited", "external", "uncited"]


def extract_labels(text: str) -> tuple[str, ...]:
    """Etiquetas de fuente (`S1`, `S2`…) citadas en `text`, sin repetir y en orden de aparición."""
    found: list[str] = []
    for citation in _CITATION.findall(text):
        for label in _LABEL.findall(citation):
            if label not in found:
                found.append(label)
    return tuple(found)


def strip_labels(text: str, invalid: Collection[str]) -> str:
    """Quita de `text` las etiquetas de `invalid`: una cita inventada no debe verse como válida.

    `[S1, S9]` queda como `[S1]`; un marcador que se queda sin etiquetas desaparece entero.
    """
    bad = set(invalid)

    def rewrite(match: re.Match[str]) -> str:
        labels = _LABEL.findall(match.group(0))
        keep = [label for label in labels if label not in bad]
        if len(keep) == len(labels):
            return match.group(0)
        return f" [{', '.join(keep)}]" if keep else ""

    return re.sub(r"\s*" + _CITATION.pattern, rewrite, text).strip()


@dataclass(frozen=True, slots=True)
class Statement:
    """Una frase de la respuesta.

    `cited`: lleva al menos una etiqueta de fuente. `external`: el modelo la declara ajena a los
    documentos. `uncited`: ni una cosa ni otra, así que no puede presentarse como contenido de
    los documentos.
    """

    text: str
    labels: tuple[str, ...]
    kind: Kind


def _is_line_start(line: str, piece: str) -> bool:
    """La primera frase de una línea no hereda la etiqueta de la línea anterior."""
    return line.startswith(piece)


def split_statements(answer: str) -> tuple[Statement, ...]:
    """Divide la respuesta en frases y clasifica cada una. Una etiqueta suelta tras el punto
    (`... días. [S1]`) se atribuye a la frase anterior."""
    pieces: list[str] = []
    for line in answer.splitlines():
        line = _BULLET.sub("", line).strip()
        for piece in filter(None, (p.strip() for p in _SENTENCE_END.split(line))):
            lead = _LEADING_CITATION.match(piece)
            if pieces and lead and not _is_line_start(line, piece):
                pieces[-1] = f"{pieces[-1]} {lead.group(0).strip()}"
                piece = piece[lead.end() :].strip()
            if piece:
                pieces.append(piece)

    statements: list[Statement] = []
    for piece in pieces:
        labels = extract_labels(piece)
        if piece.startswith(EXTERNAL_MARKER) and not labels:
            kind: Kind = "external"
        else:
            kind = "cited" if labels else "uncited"
        statements.append(Statement(piece, labels, kind))
    return tuple(statements)
