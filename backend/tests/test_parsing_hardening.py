"""Endurecimiento del análisis de archivos: corruptos, tipos engañosos y recursos excesivos.

Los casos de límites corren en proceso (rápidos); los de aislamiento lanzan subprocesos reales para
comprobar que un parser colgado o caído se mata y no deja procesos ni temporales huérfanos.
"""

import io
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from sqlalchemy.orm import Session

from app.features.documents import isolation, parsing
from app.features.documents.extraction import (
    ExtractionError,
    ExtractionLimits,
    extract_blocks,
    extract_or_fail,
)
from app.features.documents.isolation import Isolation, run_isolated
from app.features.documents.parsing import Limits, ParseError
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from tests.pdfs import make_pdf
from tests.test_ingestion import add_document, claim

DOC = uuid.uuid4()
GENEROUS = Limits(max_pages=100, max_chars=100_000)
ISOLATED = ExtractionLimits(isolated=True, timeout_seconds=20)


def extract(content: bytes, content_type: str, limits: ExtractionLimits | None = None) -> None:
    extract_blocks(DOC, io.BytesIO(content), content_type, limits or ExtractionLimits())


class TestCorruptFiles:
    @pytest.mark.parametrize(
        "content",
        [
            b"%PDF-1.4\nesto no es un pdf",
            make_pdf(["Hola mundo"])[:120],  # truncado a mitad de los objetos
            b"%PDF-1.4\n" + os.urandom(4096),
            make_pdf(["Hola mundo"]).replace(b"/Kids", b"/Kidz"),  # árbol de páginas roto
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 1 0 R >>\nendobj\ntrailer\n"
            b"<< /Root 1 0 R >>\n%%EOF",  # el catálogo se apunta a sí mismo
        ],
        ids=["no-es-pdf", "truncado", "basura", "kids-roto", "referencia-circular"],
    )
    def test_a_broken_pdf_fails_with_a_clear_cause_instead_of_crashing(
        self, content: bytes
    ) -> None:
        with pytest.raises(ExtractionError) as error:
            extract(content, "application/pdf")
        assert str(error.value)

    def test_binary_content_is_not_text(self) -> None:
        with pytest.raises(ExtractionError, match="UTF-8"):
            extract(bytes(range(128, 256)) * 10, "text/plain")

    def test_a_pdf_named_as_text_is_rejected_by_content(self) -> None:
        content = make_pdf(["Hola"]) + bytes([0xFF, 0xFE])
        with pytest.raises(ExtractionError):
            extract(content, "text/markdown")

    def test_text_declared_as_pdf_is_rejected_by_content(self) -> None:
        with pytest.raises(ExtractionError, match="dañado"):
            extract(b"Esto es solo texto plano, no un PDF.", "application/pdf")

    def test_an_unknown_type_is_refused(self) -> None:
        with pytest.raises(ExtractionError, match="no compatible"):
            extract(b"MZ\x90\x00", "application/x-msdownload")


class TestLimits:
    def test_too_many_pages_fails_without_reading_them(self) -> None:
        pdf = make_pdf(["p"] * 6)
        with pytest.raises(ParseError, match=r"6 páginas.*máximo permitido es 5"):
            parsing.parse("application/pdf", pdf, Limits(max_pages=5, max_chars=10_000))
        assert len(parsing.parse("application/pdf", pdf, Limits(6, 10_000))) == 6

    def test_a_pdf_with_too_much_text_fails(self) -> None:
        pdf = make_pdf(["a" * 400, "b" * 400])
        with pytest.raises(ParseError, match="supera el máximo"):
            parsing.parse("application/pdf", pdf, Limits(max_pages=10, max_chars=500))

    def test_a_text_file_with_too_many_characters_fails(self) -> None:
        with pytest.raises(ParseError, match="supera el máximo"):
            parsing.parse("text/plain", b"x" * 1001, Limits(max_pages=10, max_chars=1000))
        assert parsing.parse("text/plain", b"x" * 1000, Limits(max_pages=10, max_chars=1000))

    def test_the_limit_is_in_characters_not_bytes(self) -> None:
        # 600 caracteres «ñ» ocupan 1200 bytes pero caben en un tope de 1000 caracteres
        assert parsing.parse("text/plain", "ñ".encode() * 600, Limits(10, 1000))

    def test_a_file_over_the_size_limit_is_refused_before_parsing(self) -> None:
        limits = ExtractionLimits(max_bytes=1024 * 1024)
        with pytest.raises(ExtractionError, match=r"supera el tamaño máximo.*1 MB"):
            extract(b"x" * (1024 * 1024 + 1), "text/plain", limits)
        extract(b"x" * (1024 * 1024), "text/plain", limits)

    def test_the_size_limit_does_not_read_the_whole_file(self) -> None:
        class Endless:
            read_bytes = 0

            def read(self, size: int) -> bytes:
                self.read_bytes += size
                return b"x" * size

        source = Endless()
        with pytest.raises(ExtractionError, match="tamaño máximo"):
            extract_blocks(
                DOC, cast(BinaryIO, source), "text/plain", ExtractionLimits(max_bytes=1000)
            )
        assert source.read_bytes < 1024 * 1024

    def test_a_document_over_a_limit_ends_failed_with_the_cause(
        self, db_session: Session, storage: LocalFileStorage
    ) -> None:
        add_document(db_session, storage, make_pdf(["p"] * 4))
        document = claim(db_session)
        assert document is not None

        assert extract_or_fail(document, storage, ExtractionLimits(max_pages=3)) == []

        assert document.status is DocumentStatus.FAILED
        assert document.error_summary is not None
        assert "4 páginas" in document.error_summary


class TestIsolation:
    def test_an_isolated_run_gives_the_same_blocks_as_an_in_process_one(self) -> None:
        content = make_pdf(["Primera", None, "Tercera"])
        assert extract_blocks(DOC, io.BytesIO(content), "application/pdf", ISOLATED) == (
            extract_blocks(DOC, io.BytesIO(content), "application/pdf", ExtractionLimits())
        )
        markdown = "# Título\n\nUn párrafo.\n\n## Sección\n\nOtro párrafo.\n".encode()
        assert extract_blocks(DOC, io.BytesIO(markdown), "text/markdown", ISOLATED) == (
            extract_blocks(DOC, io.BytesIO(markdown), "text/markdown", ExtractionLimits())
        )

    def test_limits_and_errors_cross_the_process_boundary(self) -> None:
        limits = ExtractionLimits(max_pages=3, isolated=True, timeout_seconds=20)
        with pytest.raises(ExtractionError, match="4 páginas"):
            extract(make_pdf(["p"] * 4), "application/pdf", limits)
        with pytest.raises(ExtractionError, match="dañado"):
            extract(b"no soy un pdf", "application/pdf", ISOLATED)
        with pytest.raises(ExtractionError, match="UTF-8"):
            extract(b"\xff\xfe\xfd", "text/plain", ISOLATED)

    def test_the_child_runs_without_the_parent_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secreto")
        out = tmp_path / "env.json"
        self.fake_parser(
            tmp_path,
            monkeypatch,
            f"import json; open({str(out)!r}, 'w').write(json.dumps(sorted(os.environ)))\n"
            "sys.stdout.write('{\"blocks\": []}')\n",
        )
        run_isolated("text/plain", b"hola", GENEROUS, Isolation(timeout_seconds=5))
        assert "OPENAI_API_KEY" not in out.read_text()

    @staticmethod
    def fake_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
        """Sustituye `parsing.py` por un script que se comporta mal; deja su PID en un archivo."""
        pid_file = tmp_path / "pid"
        script = tmp_path / "parser.py"
        script.write_text(
            "import os, sys, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n" + textwrap.dedent(body)
        )
        monkeypatch.setattr(isolation, "_SCRIPT", script)
        return pid_file

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_a_hung_parser_is_killed_at_the_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = self.fake_parser(tmp_path, monkeypatch, "time.sleep(300)\n")

        started = time.monotonic()
        with pytest.raises(ParseError, match=r"tardó más de 1 s"):
            run_isolated("text/plain", b"hola", GENEROUS, Isolation(timeout_seconds=1))

        assert time.monotonic() - started < 10
        assert not self.alive(int(pid_file.read_text()))

    def test_a_busy_loop_is_killed_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = self.fake_parser(tmp_path, monkeypatch, "while True:\n    pass\n")

        with pytest.raises(ParseError, match="tardó más"):
            run_isolated("text/plain", b"hola", GENEROUS, Isolation(timeout_seconds=1))

        assert not self.alive(int(pid_file.read_text()))

    def test_a_crashing_parser_is_a_clean_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.fake_parser(tmp_path, monkeypatch, "os._exit(139)\n")
        with pytest.raises(ParseError, match="terminó de forma inesperada"):
            run_isolated("text/plain", b"hola", GENEROUS, Isolation(timeout_seconds=5))

    def test_a_parser_killed_by_a_signal_is_a_clean_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.fake_parser(tmp_path, monkeypatch, "os.kill(os.getpid(), 9)\n")
        with pytest.raises(ParseError, match="terminó de forma inesperada"):
            run_isolated("text/plain", b"hola", GENEROUS, Isolation(timeout_seconds=5))

    @pytest.mark.parametrize("output", ["no es json", '{"blocks": [{"nope": 1}]}', "[]", "{}"])
    def test_a_garbled_answer_is_a_clean_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
    ) -> None:
        self.fake_parser(tmp_path, monkeypatch, f"sys.stdout.write({output!r})\n")
        with pytest.raises(ParseError, match="respuesta del análisis inválida"):
            run_isolated("text/plain", b"hola", GENEROUS, Isolation(timeout_seconds=5))

    def test_an_oversized_answer_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.fake_parser(tmp_path, monkeypatch, "sys.stdout.write('x' * (3 * 1024 * 1024))\n")
        with pytest.raises(ParseError, match="más datos de los permitidos"):
            run_isolated("text/plain", b"hola", Limits(10, 100), Isolation(timeout_seconds=5))

    @pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="requiere SIGALRM")
    def test_the_watchdog_ends_a_child_whose_parent_never_comes_back(self) -> None:
        code = (
            "import sys, time; sys.path.insert(0, sys.argv[1]); import parsing; "
            "parsing.WATCHDOG_GRACE_SECONDS = 0; parsing._confine(1); time.sleep(60)"
        )
        started = time.monotonic()
        done = subprocess.run(  # noqa: S603 - intérprete propio y código fijo
            [sys.executable, "-I", "-c", code, str(Path(parsing.__file__).parent)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert done.returncode in (-signal.SIGALRM, -signal.SIGXCPU, 128 + signal.SIGALRM)
        assert time.monotonic() - started < 20

    def test_a_timed_out_document_ends_failed_and_leaves_no_files_behind(
        self,
        db_session: Session,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        storage: LocalFileStorage,
    ) -> None:
        pid_file = self.fake_parser(tmp_path, monkeypatch, "time.sleep(300)\n")
        add_document(db_session, storage, b"hola", content_type="text/plain", filename="lento.txt")
        document = claim(db_session)
        assert document is not None
        before = sorted(p.name for p in storage.root.rglob("*") if p.is_file())

        limits = ExtractionLimits(isolated=True, timeout_seconds=1)
        assert extract_or_fail(document, storage, limits) == []

        assert document.status is DocumentStatus.FAILED
        assert document.error_summary is not None
        assert "tardó más de 1 s" in document.error_summary
        assert not self.alive(int(pid_file.read_text()))
        after = sorted(p.name for p in storage.root.rglob("*") if p.is_file())
        assert after == before
        assert not [name for name in after if name.startswith(".upload-")]
