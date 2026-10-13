"""MongoDB metadata and content-addressed GridFS artifacts. No silent local fallback."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from typing import Protocol
from uuid import uuid4

import gridfs
from gridfs.errors import FileExists
from pymongo import MongoClient, ReplaceOne
from pymongo.errors import DuplicateKeyError


class Store(Protocol):
    def put(self, collection: str, record: dict) -> None: ...
    def get(self, collection: str, identifier: str) -> dict: ...
    def find(self, collection: str, query: dict | None = None) -> list[dict]: ...
    def artifact(self, data: bytes, media_type: str, name: str) -> str: ...
    def read_artifact(self, identifier: str) -> bytes: ...

    def put_many(self, collection: str, records: list[dict]) -> None: ...


def artifact_id(data: bytes) -> str:
    return sha256(data).hexdigest()


class MongoStore:
    def __init__(self, uri: str, database: str):
        if not database.startswith("datasheet_"):
            raise ValueError("Database name must start with datasheet_ to isolate project writes")
        self.client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="datasheet-parser")
        self.db = self.client[database]
        self.fs = gridfs.GridFS(self.db, collection="artifacts")

    def ping(self):
        self.client.admin.command("ping")
        # An authenticated read checks actual database access, unlike ping alone.
        self.db.documents.find_one({}, {"_id": 1})

    def ensure_indexes(self):
        for coll, keys in {
            "regions": [("parse_id", 1), ("page", 1)],
            "pages": [("parse_id", 1), ("number", 1)],
            "targets": [("parse_id", 1)],
            "coverage": [("run_id", 1), ("requirement_id", 1)],
            "edges": [("run_id", 1), ("source", 1), ("relation", 1)],
            "events": [("run_id", 1), ("time", 1)],
            "results": [("run_id", 1), ("section_id", 1)],
            "chunks": [("index_id", 1), ("ordinal", 1)],
            "document_indexes": [("parse_id", 1)],
            "evidence_packets": [("run_id", 1), ("section_id", 1)],
        }.items():
            self.db[coll].create_index(keys)

    def put(self, collection, record):
        record = deepcopy(record)
        identifier = record.pop("id")
        self.db[collection].replace_one({"_id": identifier}, {"_id": identifier, **record}, upsert=True)

    def put_many(self, collection, records):
        writes = []
        for record in records:
            row = deepcopy(record)
            identifier = row.pop("id")
            writes.append(ReplaceOne({"_id": identifier}, {"_id": identifier, **row}, upsert=True))
        if writes:
            self.db[collection].bulk_write(writes, ordered=True)

    def get(self, collection, identifier):
        row = self.db[collection].find_one({"_id": identifier})
        if row is None:
            raise KeyError(f"{collection}/{identifier} does not exist")
        row["id"] = row.pop("_id")
        return row

    def find(self, collection, query=None):
        rows = list(self.db[collection].find(query or {}))
        for row in rows:
            row["id"] = row.pop("_id")
        return rows

    def artifact(self, data, media_type, name):
        identifier = artifact_id(data)
        if not self.fs.exists(identifier):
            try:
                self.fs.put(data, _id=identifier, filename=name, contentType=media_type)
            except (DuplicateKeyError, FileExists):
                # GridFS inserts chunks before its files metadata. A competing upload
                # can therefore collide while the completed file is not visible yet.
                deadline = monotonic() + 5.0
                while not self.fs.exists(identifier):
                    if monotonic() >= deadline:
                        raise
                    sleep(0.05)
                # A duplicate key alone does not prove that the winner is intact.
                # Never accept orphaned/corrupt chunks or a mismatched stored artifact.
                if self.read_artifact(identifier) != data:
                    raise ValueError("Artifact content mismatch")
        return identifier

    def read_artifact(self, identifier):
        data = self.fs.get(identifier).read()
        if artifact_id(data) != identifier:
            raise ValueError("Artifact checksum mismatch")
        return data


class MemoryStore:
    """Test double only; intentionally not exposed as a CLI persistence backend."""

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self._lock = RLock()

    def put(self, collection, record):
        with self._lock:
            self.records.setdefault(collection, {})[record["id"]] = deepcopy(record)

    def put_many(self, collection, records):
        with self._lock:
            for record in records:
                self.put(collection, record)

    def get(self, collection, identifier):
        with self._lock:
            return deepcopy(self.records[collection][identifier])

    def find(self, collection, query=None):
        with self._lock:
            return [
                deepcopy(r)
                for r in self.records.get(collection, {}).values()
                if all(r.get(k) == v for k, v in (query or {}).items())
            ]

    def artifact(self, data, media_type, name):
        identifier = artifact_id(data)
        with self._lock:
            self.blobs[identifier] = data
        return identifier

    def read_artifact(self, identifier):
        with self._lock:
            return self.blobs[identifier]


def _cache_bytes(path: Path) -> bytes:
    # Windows can briefly deny opening an atomically replaced destination.
    # Wait only for that sharing race, and still verify the winner's exact hash.
    deadline = monotonic() + 1.0
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            if monotonic() >= deadline:
                raise
            sleep(0.01)


def materialize(store: Store, identifier: str, cache: Path, suffix: str) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{identifier}{suffix}"
    if not path.exists() or artifact_id(_cache_bytes(path)) != identifier:
        data = store.read_artifact(identifier)
        if artifact_id(data) != identifier:
            raise ValueError("Artifact checksum mismatch")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            # Readers see either the previous complete artifact or the new complete bytes.
            try:
                temporary.replace(path)
            except (FileExistsError, PermissionError):
                # Windows can deny replacing a winner while another thread verifies it.
                # Accept that race only after independently checking the published content.
                if not path.exists() or artifact_id(_cache_bytes(path)) != identifier:
                    raise
        finally:
            temporary.unlink(missing_ok=True)
    return path
