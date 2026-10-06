import io
import stat
import uuid
from pathlib import Path

import pytest
from sqlalchemy import LargeBinary, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.features.documents.storage import (
    InvalidStorageKey,
    LocalFileStorage,
    StoredFileNotFound,
    StoredFileTooLarge,
    document_key,
    get_storage,
    validate_key,
)
from app.features.users.models import User
from tests.factories import make_document


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def store(root: Path) -> LocalFileStorage:
    return LocalFileStorage(root)


def test_save_and_read_roundtrip(store: LocalFileStorage) -> None:
    key = document_key(uuid.uuid4(), uuid.uuid4())

    written = store.save(key, io.BytesIO(b"hola mundo"))

    assert written == 10
    assert store.exists(key)
    with store.open(key) as handle:
        assert handle.read() == b"hola mundo"


def test_save_overwrites_atomically_and_leaves_no_temporaries(
    store: LocalFileStorage, root: Path
) -> None:
    key = "owner/doc/original"
    store.save(key, io.BytesIO(b"v1"))
    store.save(key, io.BytesIO(b"version dos"))

    with store.open(key) as handle:
        assert handle.read() == b"version dos"
    assert [p.name for p in (root / "owner" / "doc").iterdir()] == ["original"]


def test_save_over_limit_discards_the_file(store: LocalFileStorage, root: Path) -> None:
    key = "owner/doc/original"

    with pytest.raises(StoredFileTooLarge):
        store.save(key, io.BytesIO(b"x" * 11), max_bytes=10)

    assert not store.exists(key)
    assert list((root / "owner" / "doc").iterdir()) == []


def test_failed_save_keeps_the_previous_file(store: LocalFileStorage) -> None:
    key = "owner/doc/original"
    store.save(key, io.BytesIO(b"intacto"))

    with pytest.raises(StoredFileTooLarge):
        store.save(key, io.BytesIO(b"x" * 100), max_bytes=10)

    with store.open(key) as handle:
        assert handle.read() == b"intacto"


def test_open_missing_file_raises(store: LocalFileStorage) -> None:
    with pytest.raises(StoredFileNotFound):
        store.open("owner/doc/original")
    assert not store.exists("owner/doc/original")


def test_delete_is_idempotent(store: LocalFileStorage) -> None:
    key = "owner/doc/original"
    store.save(key, io.BytesIO(b"x"))

    store.delete(key)
    store.delete(key)

    assert not store.exists(key)


def test_files_and_directories_are_private(store: LocalFileStorage, root: Path) -> None:
    store.save("owner/doc/original", io.BytesIO(b"x"))

    assert stat.S_IMODE((root / "owner" / "doc" / "original").stat().st_mode) == 0o600
    for directory in (root, root / "owner", root / "owner" / "doc"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../secreto",
        "owner/../../secreto",
        "owner/doc/..",
        "/etc/passwd",
        "owner//doc",
        "owner/doc/",
        ".",
        ".oculto",
        "owner/./doc",
        "owner\\..\\secreto",
        "C:\\Windows\\win.ini",
        "owner/doc/orig\x00inal",
        "owner/doc/con espacios",
        "owner/doc/ñandú",
        "%2e%2e/secreto",
        "a" * 600,
    ],
)
def test_malicious_keys_are_rejected(store: LocalFileStorage, key: str) -> None:
    with pytest.raises(InvalidStorageKey):
        validate_key(key)
    with pytest.raises(InvalidStorageKey):
        store.save(key, io.BytesIO(b"x"))
    with pytest.raises(InvalidStorageKey):
        store.open(key)
    with pytest.raises(InvalidStorageKey):
        store.exists(key)
    with pytest.raises(InvalidStorageKey):
        store.delete(key)


def test_rejected_keys_write_nothing_outside_the_root(
    store: LocalFileStorage, root: Path, tmp_path: Path
) -> None:
    with pytest.raises(InvalidStorageKey):
        store.save("../fuera", io.BytesIO(b"x"))

    assert not (tmp_path / "fuera").exists()


def test_symlink_pointing_outside_the_root_is_rejected(
    store: LocalFileStorage, root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    (outside / "secreto").write_text("no debe leerse")
    store.save("owner/doc/original", io.BytesIO(b"x"))
    (root / "owner" / "enlace").symlink_to(outside)
    (root / "owner" / "doc" / "robado").symlink_to(outside / "secreto")

    for key in ("owner/enlace/secreto", "owner/doc/robado"):
        with pytest.raises(InvalidStorageKey):
            store.open(key)
        with pytest.raises(InvalidStorageKey):
            store.save(key, io.BytesIO(b"x"))
        with pytest.raises(InvalidStorageKey):
            store.delete(key)

    assert (outside / "secreto").read_text() == "no debe leerse"


def test_symlinked_directory_cannot_receive_new_files(
    store: LocalFileStorage, root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    root.mkdir()
    (root / "owner").symlink_to(outside)

    with pytest.raises(InvalidStorageKey):
        store.save("owner/doc/original", io.BytesIO(b"x"))

    assert list(outside.iterdir()) == []


def test_document_key_is_opaque_and_ignores_user_filenames() -> None:
    owner, doc = uuid.uuid4(), uuid.uuid4()

    assert document_key(owner, doc) == f"{owner}/{doc}/original"
    assert document_key(owner, doc, "text.txt") == f"{owner}/{doc}/text.txt"
    with pytest.raises(InvalidStorageKey):
        document_key(owner, doc, "../original")


def test_get_storage_uses_the_configured_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "configurado"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    get_storage.cache_clear()
    try:
        storage = get_storage()
        assert isinstance(storage, LocalFileStorage)
        assert storage.root == (tmp_path / "configurado").resolve()
    finally:
        get_settings.cache_clear()
        get_storage.cache_clear()


def test_document_defaults_its_storage_key_from_owner_and_id(db_session: Session) -> None:
    user = User(email="storage@example.com", password_hash="x")
    db_session.add(user)
    db_session.flush()

    document = make_document(user.id)
    db_session.add(document)
    db_session.flush()

    assert document.storage_key == f"{user.id}/{document.id}/original"


def test_database_stores_references_not_binaries(db_engine: Engine) -> None:
    columns = {c["name"]: c for c in inspect(db_engine).get_columns("documents")}

    assert "storage_key" in columns
    assert [n for n, c in columns.items() if isinstance(c["type"], LargeBinary)] == []


def test_storage_key_is_unique(db_session: Session) -> None:
    user = User(email="unica@example.com", password_hash="x")
    db_session.add(user)
    db_session.flush()
    db_session.add(make_document(user.id, storage_key="a/b/original"))
    db_session.flush()

    db_session.add(make_document(user.id, storage_key="a/b/original"))
    with pytest.raises(IntegrityError):
        db_session.flush()
