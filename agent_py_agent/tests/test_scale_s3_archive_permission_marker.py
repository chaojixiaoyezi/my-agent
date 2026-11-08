"""在隔离目录验证权限复核窗口、失败重试及流式扫描的资源边界。"""
from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_archive import storage
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位与 no-follow FD 语义")


def _archive(tmp_path, files=3):
    root = tmp_path / "owner" / "memory_archive"
    root.mkdir(parents=True)
    root.chmod(0o755)
    paths = []
    for number in range(files):
        path = root / f"event-{number}.json"
        path.write_text('{"content":"原始字节"}', encoding="utf-8")
        path.chmod(0o644)
        paths.append(path)
    return root, paths


def _marker(root):
    return root.parent / f".{root.name}.permissions-verified.json"


def _counts(files=0, directories=0, skipped=0):
    return {"tightened_count": files + directories, "tightened_files": files,
            "tightened_directories": directories, "failed_count": 0,
            "failure_codes": {}, "symlink_skipped_count": skipped}


def test_clean_wide_walk_preserves_counts_and_writes_private_marker(tmp_path):
    root, _ = _archive(tmp_path, files=0)
    files = []
    for number in range(8):
        directory = root / f"batch-{number}"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        for event in range(8):
            path = directory / f"{event}.json"
            path.write_bytes(b'{"event":"unchanged"}\n')
            path.chmod(0o644)
            files.append(path)
    before = {path: path.read_bytes() for path in files}
    assert storage.tighten_memory_archive_permissions(root) == _counts(files=64, directories=9)
    marker = _marker(root)
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    payload = json.loads(marker.read_text())
    assert payload["schema_version"] == "memory_archive_permissions_verified.v1"
    assert (payload["dev"], payload["ino"]) == (root.stat().st_dev, root.stat().st_ino)
    assert {path: path.read_bytes() for path in files} == before
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (root, *root.iterdir()))


def test_second_call_skips_the_entire_walk(tmp_path, monkeypatch):
    root, _ = _archive(tmp_path)
    storage.tighten_memory_archive_permissions(root)

    def unexpected(*_args, **_kwargs):
        pytest.fail("有效复核窗口内不得再次遍历归档")

    monkeypatch.setattr(storage, "_tighten_tree", unexpected)
    assert storage.tighten_memory_archive_permissions(root) == _counts()


def test_chmod_failure_does_not_publish_marker_and_next_call_retries(tmp_path, monkeypatch):
    root, paths = _archive(tmp_path)
    original = storage.os.chmod

    def denied(path, mode, **kwargs):
        if Path(path) == paths[0]:
            raise PermissionError("single chmod denied")
        return original(path, mode, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "chmod", denied)
        result = storage.tighten_memory_archive_permissions(root)
    assert result["failed_count"] == 1
    assert result["failure_codes"] == {"permission_denied": 1}
    assert not _marker(root).exists()
    assert storage.tighten_memory_archive_permissions(root) == _counts(files=1)
    assert _marker(root).exists()


def test_replaced_archive_identity_forces_a_fresh_walk(tmp_path):
    root, _ = _archive(tmp_path, files=1)
    storage.tighten_memory_archive_permissions(root)
    previous = json.loads(_marker(root).read_text())
    root.rename(root.with_name("original_archive"))
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    data = root / "restored.json"
    data.write_bytes(b"restored")
    data.chmod(0o644)
    assert root.stat().st_ino != previous["ino"]
    assert storage.tighten_memory_archive_permissions(root) == _counts(files=1, directories=1)
    assert json.loads(_marker(root).read_text())["ino"] == root.stat().st_ino


def test_expired_marker_rechecks_legacy_permissions(tmp_path):
    root, paths = _archive(tmp_path)
    storage.tighten_memory_archive_permissions(root)
    marker = _marker(root)
    payload = json.loads(marker.read_text())
    payload["verified_at"] -= MemoryRetentionPolicy().security_audit_days * 86400 + 1
    marker.write_text(json.dumps(payload))
    paths[0].chmod(0o644)
    assert storage.tighten_memory_archive_permissions(root) == _counts(files=1)
    assert json.loads(marker.read_text())["verified_at"] > payload["verified_at"]


def test_marker_commit_uses_original_transition_and_scanning_stays_outside(tmp_path, monkeypatch):
    root, _ = _archive(tmp_path)
    active, operations = [], []
    original_write, original_scan = storage.write_private_text_file_atomic, storage.os.scandir

    def write(*args, **kwargs):
        assert active, "标记写入必须处于原短事务内"
        return original_write(*args, **kwargs)

    def scan(path):
        assert not active, "目录扫描不得持有短事务"
        return original_scan(path)

    def transition(operation):
        assert not active
        operations.append(operation)
        active.append(True)
        try:
            return operation()
        finally:
            active.pop()

    monkeypatch.setattr(storage, "write_private_text_file_atomic", write)
    monkeypatch.setattr(storage.os, "scandir", scan)
    assert storage.tighten_memory_archive_permissions(root, commit_transition=transition) == _counts(files=3, directories=1)
    assert operations[-1].func is storage._write_permission_marker
    assert _marker(root).exists()
    count = len(operations)
    assert storage.tighten_memory_archive_permissions(root, commit_transition=transition) == _counts()
    assert len(operations) == count


def test_streaming_walk_never_follows_file_directory_or_root_symlinks(tmp_path):
    root, _ = _archive(tmp_path, files=1)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    data = outside / "keep.txt"
    data.write_bytes(b"keep outside")
    data.chmod(0o644)
    (root / "file-link").symlink_to(data)
    (root / "dir-link").symlink_to(outside, target_is_directory=True)
    assert storage.tighten_memory_archive_permissions(root) == _counts(files=1, directories=1, skipped=2)
    assert data.read_bytes() == b"keep outside" and stat.S_IMODE(data.stat().st_mode) == 0o644
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    alias = root.with_name("archive-link")
    alias.symlink_to(root, target_is_directory=True)
    assert storage.tighten_memory_archive_permissions(alias) == _counts()
    assert not _marker(alias).exists()


@pytest.mark.parametrize("abort", [False, True])
def test_scandir_is_lazy_and_closed_on_success_or_interrupted_commit(tmp_path, monkeypatch, abort):
    root, paths = _archive(tmp_path, files=8)
    processed, closed = set(), []
    original_scan, original_tighten = storage.os.scandir, storage._tighten_entry

    @contextmanager
    def scan(directory):
        with original_scan(directory) as entries:
            def stream():
                previous = None
                for entry in entries:
                    if previous is not None:
                        assert previous in processed, "不得先物化整个目录再处理首项"
                    previous = Path(entry.path)
                    yield entry
            try:
                yield stream()
            finally:
                closed.append(directory)

    def tighten(path, *args, **kwargs):
        if abort and path in paths:
            raise RuntimeError("commit interrupted")
        result = original_tighten(path, *args, **kwargs)
        processed.add(path)
        return result

    monkeypatch.setattr(storage.os, "scandir", scan)
    monkeypatch.setattr(storage, "_tighten_entry", tighten)
    if abort:
        with pytest.raises(RuntimeError, match="commit interrupted"):
            storage.tighten_memory_archive_permissions(root)
        assert not _marker(root).exists()
    else:
        assert storage.tighten_memory_archive_permissions(root) == _counts(files=8, directories=1)
    assert closed == [root]


def test_marker_write_failure_is_nonfatal_and_does_not_skip_next_walk(tmp_path, monkeypatch):
    root, _ = _archive(tmp_path, files=1)

    def denied(*_args, **_kwargs):
        raise OSError("marker write failed")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "write_private_text_file_atomic", denied)
        assert storage.tighten_memory_archive_permissions(root) == _counts(files=1, directories=1)
    assert not _marker(root).exists()
    assert storage.tighten_memory_archive_permissions(root) == _counts()
    assert _marker(root).exists()


@pytest.mark.parametrize("damage", ["schema", "device", "bool_device", "float_inode", "nan", "infinity",
                                   "text_timestamp", "future", "non_object", "oversized", "malformed"])
def test_invalid_marker_never_skips_verification(tmp_path, damage):
    root, _ = _archive(tmp_path, files=0)
    storage.tighten_memory_archive_permissions(root)
    marker = _marker(root)
    payload = json.loads(marker.read_text())
    now = storage.time.time()
    changes = {"schema": {"schema_version": "unknown"}, "device": {"dev": payload["dev"] + 1},
               "bool_device": {"dev": True}, "float_inode": {"ino": float(payload["ino"])},
               "nan": {"verified_at": float("nan")}, "infinity": {"verified_at": float("inf")},
               "text_timestamp": {"verified_at": str(now)}, "future": {"verified_at": now + 86400}}
    payload.update(changes.get(damage, {}))
    raw = json.dumps([] if damage == "non_object" else payload).encode()
    if damage == "oversized":
        raw += b" " * 4096
    if damage == "malformed":
        raw = b"{"
    marker.write_bytes(raw)
    assert not storage._permission_marker_valid(marker, storage._archive_identity(root), now=now)


def test_marker_reads_are_bounded_and_do_not_follow_links(tmp_path, monkeypatch):
    root, _ = _archive(tmp_path, files=0)
    storage.tighten_memory_archive_permissions(root)
    marker, reads = _marker(root), []
    original = storage.os.read

    def read(descriptor, size):
        reads.append(size)
        return original(descriptor, size)

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "read", read)
        assert storage._permission_marker_valid(marker, storage._archive_identity(root), now=storage.time.time())
    assert reads and sum(reads) <= 4096
    outside = tmp_path / "outside-marker.json"
    marker.rename(outside)
    original_bytes = outside.read_bytes()
    marker.symlink_to(outside)
    reads.clear()
    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "read", read)
        assert not storage._permission_marker_valid(marker, storage._archive_identity(root), now=storage.time.time())
    assert not reads
    storage.tighten_memory_archive_permissions(root)
    assert not marker.is_symlink() and outside.read_bytes() == original_bytes
