from __future__ import annotations

from contextlib import nullcontext
from io import BytesIO
from pathlib import Path

import pytest

from agent_py_agent.agent.owner_object_store import OwnerFile, OwnerSnapshotStore


class _Objects:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}
        self.extra: dict[str, str] = {}
        self.versioning = "Enabled"

    def upload_file(self, source: str, bucket: str, key: str, *, ExtraArgs: dict[str, str]) -> None:
        self.values[(bucket, key)] = Path(source).read_bytes()
        self.extra = dict(ExtraArgs)

    def download_file(self, bucket: str, key: str, target: str) -> None:
        Path(target).write_bytes(self.values[(bucket, key)])

    def head_bucket(self, *, Bucket: str) -> None:
        assert Bucket

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.values:
            error = RuntimeError("missing")
            error.response = {"Error": {"Code": "404"}}
            raise error

    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, str]:
        assert Bucket
        return {"Status": self.versioning}

    def put_object(self, **kwargs) -> None:
        assert kwargs["IfNoneMatch"] == "*"
        target = (kwargs["Bucket"], kwargs["Key"])
        if target in self.values:
            error = RuntimeError("exists")
            error.response = {"Error": {"Code": "PreconditionFailed"}}
            raise error
        self.values[target] = kwargs["Body"]

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, BytesIO]:
        return {"Body": BytesIO(self.values[(Bucket, Key)])}


def _store(client: _Objects) -> OwnerSnapshotStore:
    store = object.__new__(OwnerSnapshotStore)
    store._client = client
    store._bucket = "owners"
    store._prefix = "prod"
    store._sse = "AES256"
    store._kms_key = ""
    store._owner_lock = lambda *_args: nullcontext()
    return store


def test_commit_uploads_durable_files_and_excludes_ephemeral_roots(tmp_path) -> None:
    client = _Objects()
    store = _store(client)
    captured: list[OwnerFile] = []
    store._replace_manifest = lambda _t, _k, _o, rows: captured.extend(rows)
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "store.jsonl").write_text("durable\n", encoding="utf-8")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "index.bin").write_bytes(b"ephemeral")

    assert store.commit("acme", "user", "u1", tmp_path) == 1
    assert [row.relative_path for row in captured] == ["memory/store.jsonl"]
    assert next(iter(client.values.values())) == b"durable\n"
    assert client.extra == {"ServerSideEncryption": "AES256"}


def test_restore_verifies_hash_and_rejects_path_escape(tmp_path) -> None:
    client = _Objects()
    store = _store(client)
    payload = b"hello"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    client.values[("owners", "object-1")] = payload
    store._read_manifest = lambda *_args: [OwnerFile("memory.md", "object-1", digest, len(payload), 0o600)]
    assert store.restore("acme", "user", "u1", tmp_path) == 1
    assert (tmp_path / "memory.md").read_bytes() == payload

    store._read_manifest = lambda *_args: [OwnerFile("../escape", "object-1", digest, len(payload), 0o600)]
    with pytest.raises(RuntimeError, match="路径越界"):
        store.restore("acme", "user", "u1", tmp_path)


def test_bucket_versioning_is_startup_hard_gate() -> None:
    client = _Objects()
    store = _store(client)
    store.require_ready()
    client.versioning = "Suspended"
    with pytest.raises(RuntimeError, match="versioning"):
        store.require_ready()
