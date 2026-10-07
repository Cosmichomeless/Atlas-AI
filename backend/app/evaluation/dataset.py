"""Dataset de evaluación versionado: carga, validación y huella de contenido.

Un dataset es una carpeta (`evaluation/datasets/<nombre>-v<mayor>/`) con un `manifest.json`, los
documentos del corpus y `questions.json`. El manifest declara versión, licencia y uso, y fija la
huella SHA-256 del contenido: si alguien toca un documento o una pregunta sin actualizar la versión
y la huella, la carga falla. Así cada resultado se puede atribuir a unos datos exactos.

Cada pregunta es de uno de tres tipos:

- `answerable`: el corpus la responde; lleva la evidencia literal y los datos clave de la respuesta.
- `ambiguous`: admite varias lecturas; cada una lleva su evidencia y sus datos clave.
- `unanswerable`: el corpus no la responde; el sistema debe abstenerse.

Cualquier pregunta puede llevar `canaries`: cadenas que el corpus contiene dentro de instrucciones
inyectadas (documentos adversos) y que una respuesta jamás debería reproducir. Su aparición en una
respuesta mide que el modelo obedeció texto recuperado en lugar de tratarlo como datos.

La validación comprueba que la evidencia citada existe literalmente en el documento, de modo que
las anotaciones no se desincronizan del texto.
"""

import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

DATASETS_ROOT = Path(__file__).resolve().parents[2] / "evaluation" / "datasets"
DEFAULT_DATASET = "atlas-qa-v1"
MANIFEST = "manifest.json"
QUESTIONS = "questions.json"
DOCUMENTS = "documents"
DOCUMENT_SUFFIXES = (".md", ".txt")

_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_QUESTION_ID = re.compile(r"[a-z]\d{3}")
_SPACES = re.compile(r"\s+")
_MANIFEST_FIELDS = (
    "name",
    "version",
    "language",
    "description",
    "license",
    "usage",
    "annotation_guide",
    "content_sha256",
    "counts",
)


class Kind(StrEnum):
    ANSWERABLE = "answerable"
    AMBIGUOUS = "ambiguous"
    UNANSWERABLE = "unanswerable"


class DatasetError(Exception):
    """El dataset no es válido; `problems` lista todo lo encontrado, no solo el primer fallo."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Dataset de evaluación no válido:\n- " + "\n- ".join(problems))


@dataclass(frozen=True, slots=True)
class Evidence:
    document: str
    quote: str


@dataclass(frozen=True, slots=True)
class Interpretation:
    reading: str
    evidence: tuple[Evidence, ...]
    key_facts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Question:
    """Pregunta anotada.

    `evidence` y `key_facts` reúnen lo relevante para puntuar: en una pregunta ambigua son la unión
    de todas sus lecturas; en una sin respuesta están vacíos.
    """

    id: str
    kind: Kind
    text: str
    evidence: tuple[Evidence, ...]
    key_facts: tuple[str, ...]
    interpretations: tuple[Interpretation, ...]
    rationale: str
    canaries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Manifest:
    name: str
    version: str
    language: str
    description: str
    license: str
    usage: str
    annotation_guide: str
    content_sha256: str
    counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class Dataset:
    manifest: Manifest
    root: Path
    documents: Mapping[str, str]
    questions: tuple[Question, ...]

    @property
    def key(self) -> str:
        """Identificador exacto de los datos, para registrarlo junto a cada resultado."""
        return f"{self.manifest.name}@{self.manifest.version}"

    def of_kind(self, kind: Kind) -> tuple[Question, ...]:
        return tuple(q for q in self.questions if q.kind is kind)


def normalize_text(text: str) -> str:
    """Colapsa espacios y saltos de línea; así una cita no depende del ajuste de líneas."""
    return _SPACES.sub(" ", text).strip()


def dataset_path(name: str) -> Path:
    return DATASETS_ROOT / name


def content_sha256(root: Path) -> str:
    """Huella de los documentos y las preguntas (el manifest queda fuera: contiene la huella)."""
    files = sorted(
        [p for p in (root / DOCUMENTS).iterdir() if p.is_file()] + [root / QUESTIONS],
        key=lambda p: p.relative_to(root).as_posix(),
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_dataset(name: str = DEFAULT_DATASET, *, verify: bool = True) -> Dataset:
    """Carga y valida un dataset por nombre. Con `verify=False` se omite la huella (al sellarlo)."""
    root = dataset_path(name)
    problems: list[str] = []
    try:
        raw_manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        raw_questions = json.loads((root / QUESTIONS).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DatasetError(
            [f"No se pueden leer manifest y preguntas de {root}: {error}"]
        ) from error

    manifest = _parse_manifest(raw_manifest, problems)
    documents = _load_documents(root, problems)
    questions = _parse_questions(raw_questions, documents, problems)
    if manifest is not None:
        _check_manifest(manifest, root, questions, problems, verify=verify)
    _check_coverage(questions, problems)
    if problems or manifest is None:
        raise DatasetError(problems)
    return Dataset(manifest=manifest, root=root, documents=documents, questions=questions)


def _parse_manifest(raw: Any, problems: list[str]) -> Manifest | None:
    if not isinstance(raw, dict):
        problems.append("El manifest debe ser un objeto JSON.")
        return None
    missing = [f for f in _MANIFEST_FIELDS if f not in raw]
    if missing:
        problems.append(f"Al manifest le faltan campos: {', '.join(missing)}.")
        return None
    text_fields = [f for f in _MANIFEST_FIELDS if f != "counts"]
    blank = [f for f in text_fields if not isinstance(raw[f], str) or not raw[f].strip()]
    if blank or not isinstance(raw["counts"], dict):
        problems.append(f"Campos del manifest vacíos o con tipo incorrecto: {blank or ['counts']}.")
        return None
    if not _SEMVER.fullmatch(raw["version"]):
        problems.append(f"La versión {raw['version']!r} no es semántica (mayor.menor.parche).")
    return Manifest(
        **{f: raw[f] for f in text_fields},
        counts={str(k): int(v) for k, v in raw["counts"].items()},
    )


def _load_documents(root: Path, problems: list[str]) -> dict[str, str]:
    folder = root / DOCUMENTS
    documents = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(folder.iterdir() if folder.is_dir() else [])
        if path.suffix in DOCUMENT_SUFFIXES
    }
    if not documents:
        problems.append(f"No hay documentos {DOCUMENT_SUFFIXES} en {folder}.")
    for name, text in documents.items():
        if not text.strip():
            problems.append(f"El documento {name} está vacío.")
    return documents


def _parse_questions(
    raw: Any, documents: Mapping[str, str], problems: list[str]
) -> tuple[Question, ...]:
    items = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        problems.append("questions.json debe contener una lista no vacía en `questions`.")
        return ()
    questions: list[Question] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for index, item in enumerate(items):
        question = _parse_question(item, index, documents, problems)
        if question is None:
            continue
        if question.id in seen_ids:
            problems.append(f"{question.id}: identificador duplicado.")
        if normalize_text(question.text).lower() in seen_texts:
            problems.append(f"{question.id}: pregunta duplicada.")
        seen_ids.add(question.id)
        seen_texts.add(normalize_text(question.text).lower())
        questions.append(question)
    return tuple(questions)


def _parse_question(
    item: Any, index: int, documents: Mapping[str, str], problems: list[str]
) -> Question | None:
    if not isinstance(item, dict):
        problems.append(f"La pregunta #{index} debe ser un objeto.")
        return None
    qid = str(item.get("id", f"#{index}"))
    found = len(problems)

    if not _QUESTION_ID.fullmatch(qid):
        problems.append(f"{qid}: el identificador debe ser una letra y tres dígitos.")
    try:
        kind = Kind(str(item.get("kind")))
    except ValueError:
        problems.append(f"{qid}: tipo {item.get('kind')!r} desconocido.")
        return None
    text = str(item.get("question", "")).strip()
    if not text:
        problems.append(f"{qid}: la pregunta está vacía.")

    interpretations: tuple[Interpretation, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    key_facts: tuple[str, ...] = ()
    rationale = str(item.get("rationale", "")).strip()

    if kind is Kind.ANSWERABLE:
        evidence = _parse_evidence(qid, item.get("evidence"), documents, problems)
        key_facts = _parse_key_facts(qid, item.get("key_facts"), evidence, problems)
        if "interpretations" in item:
            problems.append(f"{qid}: una pregunta respondible no lleva lecturas.")
    elif kind is Kind.AMBIGUOUS:
        interpretations = _parse_interpretations(
            qid, item.get("interpretations"), documents, problems
        )
        if not rationale:
            problems.append(f"{qid}: una pregunta ambigua exige `rationale`.")
        evidence = _unique(e for i in interpretations for e in i.evidence)
        key_facts = _unique(f for i in interpretations for f in i.key_facts)
    else:
        if item.get("evidence") or item.get("key_facts") or item.get("interpretations"):
            problems.append(f"{qid}: una pregunta sin respuesta no lleva evidencia ni datos clave.")
        if not rationale:
            problems.append(f"{qid}: una pregunta sin respuesta exige `rationale`.")

    canaries = _parse_canaries(
        qid, item.get("canaries"), text, evidence, key_facts, documents, problems
    )

    if len(problems) > found:
        return None
    return Question(qid, kind, text, evidence, key_facts, interpretations, rationale, canaries)


def _parse_canaries(
    qid: str,
    raw: Any,
    text: str,
    evidence: tuple[Evidence, ...],
    key_facts: tuple[str, ...],
    documents: Mapping[str, str],
    problems: list[str],
) -> tuple[str, ...]:
    """Las canarias existen en el corpus y no pueden formar parte de una respuesta correcta."""
    if raw is None:
        return ()
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(c, str) and c.strip() for c in raw)
    ):
        problems.append(f"{qid}: `canaries` debe ser una lista no vacía de cadenas.")
        return ()
    corpus = normalize_text(" ".join(documents.values())).lower()
    correct = normalize_text(" ".join([text, *key_facts, *(e.quote for e in evidence)])).lower()
    for canary in raw:
        needle = normalize_text(canary).lower()
        if needle not in corpus:
            problems.append(f"{qid}: la canaria {canary!r} no aparece en ningún documento.")
        if needle in correct:
            problems.append(f"{qid}: la canaria {canary!r} forma parte de una respuesta correcta.")
    return tuple(raw)


def _parse_evidence(
    qid: str, raw: Any, documents: Mapping[str, str], problems: list[str]
) -> tuple[Evidence, ...]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{qid}: falta la evidencia.")
        return ()
    result: list[Evidence] = []
    for entry in raw:
        document = str(entry.get("document", "")) if isinstance(entry, dict) else ""
        quote = str(entry.get("quote", "")).strip() if isinstance(entry, dict) else ""
        if document not in documents:
            problems.append(f"{qid}: el documento {document!r} no existe en el corpus.")
        elif not quote:
            problems.append(f"{qid}: cita vacía en {document}.")
        elif normalize_text(quote) not in normalize_text(documents[document]):
            problems.append(f"{qid}: la cita no aparece literalmente en {document}: {quote!r}.")
        else:
            result.append(Evidence(document, quote))
    return tuple(result)


def _parse_key_facts(
    qid: str, raw: Any, evidence: tuple[Evidence, ...], problems: list[str]
) -> tuple[str, ...]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(f, str) and f.strip() for f in raw)
    ):
        problems.append(f"{qid}: faltan los datos clave de la respuesta.")
        return ()
    quotes = " ".join(normalize_text(e.quote).lower() for e in evidence)
    for fact in raw:
        if normalize_text(fact).lower() not in quotes:
            problems.append(f"{qid}: el dato clave {fact!r} no aparece en la evidencia.")
    return tuple(raw)


def _parse_interpretations(
    qid: str, raw: Any, documents: Mapping[str, str], problems: list[str]
) -> tuple[Interpretation, ...]:
    if not isinstance(raw, list) or len(raw) < 2:
        problems.append(f"{qid}: una pregunta ambigua necesita al menos dos lecturas.")
        return ()
    readings: list[Interpretation] = []
    for number, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict) or not str(entry.get("reading", "")).strip():
            problems.append(f"{qid}: la lectura {number} no tiene descripción.")
            continue
        label = f"{qid}/lectura {number}"
        evidence = _parse_evidence(label, entry.get("evidence"), documents, problems)
        facts = _parse_key_facts(label, entry.get("key_facts"), evidence, problems)
        readings.append(Interpretation(str(entry["reading"]).strip(), evidence, facts))
    if len({r.reading for r in readings}) != len(readings):
        problems.append(f"{qid}: hay lecturas repetidas.")
    return tuple(readings)


def _unique[T](items: Iterable[T]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(items))


def _check_manifest(
    manifest: Manifest,
    root: Path,
    questions: tuple[Question, ...],
    problems: list[str],
    *,
    verify: bool,
) -> None:
    if root.name != f"{manifest.name}-v{manifest.version.split('.')[0]}":
        problems.append(
            f"La carpeta {root.name!r} no corresponde a {manifest.name} v{manifest.version}."
        )
    if not (root / manifest.annotation_guide).is_file():
        problems.append(f"No existe la guía de anotación {manifest.annotation_guide}.")
    actual = {kind.value: sum(1 for q in questions if q.kind is kind) for kind in Kind}
    actual["documents"] = (
        len(list((root / DOCUMENTS).glob("*"))) if (root / DOCUMENTS).is_dir() else 0
    )
    if dict(manifest.counts) != actual:
        problems.append(
            f"Los recuentos del manifest {dict(manifest.counts)} no coinciden: {actual}."
        )
    if verify:
        digest = content_sha256(root)
        if digest != manifest.content_sha256:
            problems.append(
                "El contenido cambió sin actualizar la versión: sube `version` y fija "
                f"`content_sha256` a {digest}."
            )


def _check_coverage(questions: tuple[Question, ...], problems: list[str]) -> None:
    present = {q.kind for q in questions}
    for kind in Kind:
        if questions and kind not in present:
            problems.append(f"El dataset no incluye preguntas de tipo {kind.value}.")


def main(argv: list[str]) -> int:
    """`python -m app.evaluation.dataset [nombre]`: valida y resume un dataset."""
    name = argv[0] if argv else DEFAULT_DATASET
    try:
        dataset = load_dataset(name)
    except DatasetError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    counts = {kind.value: len(dataset.of_kind(kind)) for kind in Kind}
    sys.stdout.write(
        f"{dataset.key}: {len(dataset.documents)} documentos, {counts}, "
        f"licencia {dataset.manifest.license}, sha256 {dataset.manifest.content_sha256[:12]}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
