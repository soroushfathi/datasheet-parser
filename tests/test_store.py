from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier, Event, Lock
from unittest.mock import Mock

import pytest
from gridfs.errors import CorruptGridFile, FileExists
from pymongo.errors import AutoReconnect, DuplicateKeyError

from datasheet_parser.store import MongoStore, artifact_id


def mongo_store_with_fs(fs):
    """Exercise MongoStore storage logic without constructing a network client."""
    store = MongoStore.__new__(MongoStore)
    store.fs = fs
    return store


class ConcurrentGridFS:
    """Model GridFS chunk reservation preceding publication of files metadata."""

    def __init__(self, collision_type):
        self.collision_type = collision_type
        self.lock = Lock()
        self.initial_checks = Barrier(2)
        self.collision_seen = Event()
        self.metadata_wait_seen = Event()
        self.winner_visible = Event()
        self.check_count = 0
        self.chunk_owner = None
        self.blobs = {}
        self.put_calls = 0
        self.read_calls = 0

    def exists(self, identifier):
        with self.lock:
            self.check_count += 1
            initial = self.check_count <= 2
        if initial:
            # Both writers observed the absent file before either inserts chunks.
            self.initial_checks.wait(timeout=5)
            return False
        visible = self.winner_visible.is_set()
        if not visible:
            self.metadata_wait_seen.set()
        return visible

    def put(self, data, *, _id, filename, contentType):
        with self.lock:
            self.put_calls += 1
            winner = self.chunk_owner is None
            if winner:
                self.chunk_owner = _id
        if not winner:
            self.collision_seen.set()
            raise self.collision_type("Chunk already reserved by the concurrent writer")
        assert self.collision_seen.wait(timeout=5)
        assert self.metadata_wait_seen.wait(timeout=5)
        self.blobs[_id] = data
        self.winner_visible.set()
        return _id

    def get(self, identifier):
        self.read_calls += 1
        return BytesIO(self.blobs[identifier])


@pytest.mark.parametrize("collision_type", [FileExists, DuplicateKeyError])
def test_concurrent_artifact_upload_accepts_only_verified_winner(collision_type):
    fs = ConcurrentGridFS(collision_type)
    store = mongo_store_with_fs(fs)
    data = b"shared complete document bytes" * 10000
    identifier = artifact_id(data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.artifact, data, "application/pdf", f"copy-{n}.pdf") for n in range(2)]
        assert [future.result(timeout=10) for future in futures] == [identifier, identifier]
    assert fs.put_calls == 2
    assert fs.metadata_wait_seen.is_set()
    assert fs.read_calls == 1  # The collision is accepted only after full verification.
    assert store.read_artifact(identifier) == data
    assert store.artifact(data, "application/pdf", "third-copy.pdf") == identifier
    assert fs.put_calls == 2  # Published blobs retain content-addressed deduplication.


@pytest.mark.parametrize("collision_type", [FileExists, DuplicateKeyError])
def test_duplicate_artifact_without_completed_winner_preserves_original_error(monkeypatch, collision_type):
    collision = collision_type("Orphaned chunks are not a completed artifact")
    fs = Mock()
    fs.exists.return_value = False
    fs.put.side_effect = collision
    store = mongo_store_with_fs(fs)
    clock = iter([0.0, 0.0, 5.0])
    pause = Mock()
    monkeypatch.setattr("datasheet_parser.store.monotonic", lambda: next(clock))
    monkeypatch.setattr("datasheet_parser.store.sleep", pause)
    with pytest.raises(collision_type) as error:
        store.artifact(b"original", "application/pdf", "original.pdf")
    assert error.value is collision
    fs.get.assert_not_called()
    pause.assert_called_once_with(0.05)


@pytest.mark.parametrize("collision_type", [FileExists, DuplicateKeyError])
def test_duplicate_artifact_rejects_corrupt_winner(collision_type):
    fs = Mock()
    fs.exists.side_effect = [False, True]
    fs.put.side_effect = collision_type("Existing ID")
    fs.get.return_value = BytesIO(b"corrupted")
    store = mongo_store_with_fs(fs)
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.artifact(b"original", "application/pdf", "original.pdf")


def test_duplicate_artifact_does_not_mask_gridfs_corruption():
    fs = Mock()
    fs.exists.side_effect = [False, True]
    fs.put.side_effect = FileExists("Existing ID")
    fs.get.side_effect = CorruptGridFile("Missing chunk")
    store = mongo_store_with_fs(fs)
    with pytest.raises(CorruptGridFile, match="Missing chunk"):
        store.artifact(b"original", "application/pdf", "original.pdf")


def test_artifact_does_not_mask_unrelated_database_errors():
    fs = Mock()
    fs.exists.return_value = False
    fs.put.side_effect = AutoReconnect("Database unavailable")
    store = mongo_store_with_fs(fs)
    with pytest.raises(AutoReconnect, match="Database unavailable"):
        store.artifact(b"original", "application/pdf", "original.pdf")
    fs.exists.assert_called_once_with(artifact_id(b"original"))
    fs.get.assert_not_called()
