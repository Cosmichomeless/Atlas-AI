"""Texto recuperado como dato no confiable: se omiten los párrafos que dan órdenes al asistente.

Un documento puede llevar frases como «ignora todas las instrucciones anteriores» o «SYSTEM: …».
La regla 6 del prompt pide al modelo que las ignore, pero esa defensa depende del modelo. Esta es
una segunda capa que no depende de él: antes de construir el contexto se sustituye por un aviso
neutro cada párrafo que tiene forma de instrucción dirigida al asistente, de modo que ni el modelo
ni la verificación de citas llegan a ver esa orden como si fuera contenido del documento.

Es una heurística y no una garantía: reconoce fórmulas habituales en español e inglés, no un
ataque redactado de otra forma. Se omite el párrafo entero (no solo la frase) porque las órdenes
suelen repartirse en varias frases seguidas. El precio es que un párrafo legítimo que cite una de
estas fórmulas se pierde; los patrones son específicos para que sea raro.
"""

import re

REDACTION = "[contenido omitido: instrucciones dirigidas al asistente]"

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")

_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        # Anular las reglas anteriores.
        r"\bignor\w*\s+(?:todas?\s+)?(?:las\s+|tus\s+|esas\s+)?(?:instrucciones|reglas|[oó]rdenes)",
        r"\bolvida\w*\s+(?:todas?\s+)?(?:las\s+|tus\s+)?(?:instrucciones|reglas)",
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?"
        r"(?:previous|prior|above|your)\s+(?:instructions|rules)",
        # Hablar como el sistema o dirigirse al asistente.
        r"^\W*(?:system|sistema|assistant|asistente)\s*:",
        r"\binstrucci[oó]n(?:es)?\s+(?:del\s+sistema|para\s+(?:el|la)\s+(?:asistente|ia|modelo))",
        r"\b(?:nuevo|nueva)\s+(?:mensaje|instrucci[oó]n|regla|rol)\s+(?:del?\s+sistema|de\s+sistema)?"
        r"\s*[:.]",
        r"\b(?:tu|su)\s+nuevo\s+rol\b",
        r"\ba\s+partir\s+de\s+ahora\s+(?:eres|responde|ignora|act[uú]a|debes)",
        # Pedir secretos o el prompt.
        r"\brevela\w*\s+(?:tu|tus|el)\s+(?:prompt|claves?|instrucciones|contrase)",
        r"\b(?:reveal|print|show)\s+(?:me\s+)?your\s+(?:system\s+)?(?:prompt|instructions|keys?)",
        # Falsificar el delimitador o las etiquetas de fuente del prompt.
        r"<\s*/?\s*fuentes\s*>",
        r"^\W*\[(?:fuente|source|s)\s*\d+\]\s+\w+\s+para\s+el\s+asistente",
        r"\bañade\s+la\s+etiqueta\s+\[",
    )
)


def is_instruction(paragraph: str) -> bool:
    """¿Tiene el párrafo forma de orden dirigida al asistente?"""
    return any(pattern.search(paragraph) for pattern in _PATTERNS)


def redact_instructions(text: str) -> tuple[str, int]:
    """El texto sin los párrafos que dan órdenes al asistente, y cuántos se omitieron."""
    paragraphs = _PARAGRAPH_BREAK.split(text)
    kept = [REDACTION if is_instruction(p) else p for p in paragraphs]
    redacted = sum(1 for p in paragraphs if is_instruction(p))
    if not redacted:
        return text, 0
    return "\n\n".join(_collapse(kept)), redacted


def _collapse(paragraphs: list[str]) -> list[str]:
    """Varios avisos seguidos se dejan en uno solo."""
    result: list[str] = []
    for paragraph in paragraphs:
        if paragraph == REDACTION and result and result[-1] == REDACTION:
            continue
        result.append(paragraph)
    return result
