import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from agent_py_agent.agent.artifacts.registry import REGISTRY_RELATIVE_PATH
from agent_py_agent.agent.gateway_parts import business_upload_registry as registry
from agent_py_agent.agent.gateway_parts.business_upload_registry import (
    lookup_business_upload,
    verify_record_proof,
)
from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.agent.observability.telemetry_ledger import ToolCallRecord
from agent_py_agent.agent.plugin_host.errors import HostAPIError


def _registry(tmp_path, count):
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    path = owner / "artifacts" / "business" / REGISTRY_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    ids = [f"artifact-{index:040x}" for index in range(count)]
    path.write_bytes(b"".join(_line(artifact_id) for artifact_id in ids))
    return owner, path, ids


def _line(artifact_id, version="v1"):
    return json.dumps({
        "artifact_id": artifact_id,
        "source": "business_upload",
        "status": "ready",
        "metadata": {"owner_id": "local/test", "version_ref": version, "space_id": "space-1"},
    }).encode() + b"\n"


@pytest.mark.parametrize("count", [9, 32])
def test_concurrent_distinct_lookups_complete(tmp_path, count):
    owner, path, ids = _registry(tmp_path, count)

    def step(artifact_id):
        return lookup_business_upload(owner, artifact_id, max_bytes=36, max_steps=1, return_pending=True)

    # 先让八个查询保存部分进度，再并发发起其余查询，固定父提交的饱和交错。
    for batch in (ids[:8], ids[8:]):
        start = Barrier(len(batch))

        def run(artifact_id, start=start):
            start.wait(timeout=20)
            return step(artifact_id)

        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            assert all(result.pending for result in pool.map(run, batch))
    target = ids[8]
    for _ in range(path.stat().st_size // 18 + 4):
        result = step(target)
        if not result.pending:
            break
    assert not result.pending, "ninth lookup never advances to completion"

    start = Barrier(count)

    def complete(artifact_id):
        start.wait(timeout=20)
        return step(artifact_id)

    with ThreadPoolExecutor(max_workers=count) as pool:
        assert [result.record.artifact_id for result in pool.map(complete, ids)] == ids


def _database(owner):
    return closing(sqlite3.connect(owner / "cache" / "owner_read_index.sqlite3"))


@pytest.mark.parametrize("populated", [False, True])
def test_keyed_schema_and_rebuild_preserve_other_domains(tmp_path, populated):
    owner, _, ids = _registry(tmp_path, 3)
    if populated:
        source = owner / "telemetry.jsonl"
        source.write_text(json.dumps(ToolCallRecord(
            call_id="existing", tool_name="read_file", run_id="run-1", owner_id="local/test",
            started_at=1.0, ended_at=2.0, duration_ms=1000.0,
        ).to_dict()) + "\n")
        assert OwnerReadIndex(owner).advance("telemetry", (source.name,))["status"] == "complete"
        with _database(owner) as db, db:
            db.execute("CREATE TABLE progress (artifact_id TEXT PRIMARY KEY)")
    first = lookup_business_upload(owner, ids[1])
    with _database(owner) as db, db:
        assert [(row[1], row[2], row[5]) for row in db.execute("PRAGMA table_info(ori_upload_registry)")] == [
            ("artifact_id", "TEXT", 1), ("byte_offset", "INTEGER", 0), ("length", "INTEGER", 0),
            ("row_sha", "TEXT", 0), ("generation", "TEXT", 0),
        ]
        assert any(row[3] == "pk" for row in db.execute("PRAGMA index_list(ori_upload_registry)"))
        plan = db.execute("EXPLAIN QUERY PLAN SELECT byte_offset FROM ori_upload_registry WHERE artifact_id=?", (ids[1],))
        assert any("SEARCH ori_upload_registry USING INDEX" in row[3] and "artifact_id=?" in row[3] for row in plan)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='progress'").fetchone() is None
        assert db.execute("SELECT count(*) FROM ori_telemetry").fetchone()[0] == int(populated)
        assert db.execute("SELECT value FROM ori_meta WHERE key='schema'").fetchone()[0] == "owner_read_index.v1"
        db.execute("DROP TABLE ori_upload_registry")
    second = lookup_business_upload(owner, ids[1])
    assert second.record == first.record
    assert second.proof.offset == first.proof.offset


def test_shared_offset_survives_process_restart_with_different_id_and_block(tmp_path):
    owner, _, ids = _registry(tmp_path, 5)
    first = lookup_business_upload(owner, ids[0], max_bytes=36, max_steps=1, return_pending=True)
    assert first.pending and first.watermark == 36
    source = """
import sys
from agent_py_agent.agent.gateway_parts.business_upload_registry import lookup_business_upload
result = lookup_business_upload(sys.argv[1], sys.argv[2], max_bytes=37, max_steps=1, return_pending=True)
assert result.pending
print(result.watermark)
"""
    flags = [] if getattr(sys, "_is_gil_enabled", lambda: True)() else ["-X", "gil=0"]
    child = subprocess.run(
        [sys.executable, *flags, "-c", source, str(owner), ids[1]],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert child.stdout.strip() == "73"


@pytest.mark.parametrize("replacement", ["rotate", "truncate"])
def test_replacement_resets_watermark_and_old_proof(tmp_path, replacement):
    owner, path, ids = _registry(tmp_path, 5)
    old = lookup_business_upload(owner, ids[-1])
    if replacement == "rotate":
        target = path.with_name("next.jsonl")
        target.write_bytes(_line(ids[0], "v2"))
        target.replace(path)
    else:
        path.write_bytes(_line(ids[0], "v2"))
    with pytest.raises(HostAPIError) as exc:
        verify_record_proof(owner, old.proof)
    assert exc.value.code == "STATE_CONFLICT"
    progress = lookup_business_upload(owner, ids[0], max_bytes=36, max_steps=1, return_pending=True)
    assert progress.pending and progress.watermark == 36
    assert lookup_business_upload(owner, ids[-1]).record is None
    assert lookup_business_upload(owner, ids[0]).record.metadata["version_ref"] == "v2"


def test_invalid_hit_fails_and_rebuilds(tmp_path):
    owner, _, ids = _registry(tmp_path, 4)
    original = lookup_business_upload(owner, ids[1])
    with _database(owner) as db, db:
        db.execute("UPDATE ori_upload_registry SET row_sha=? WHERE artifact_id=?", ("0" * 64, ids[1]))
    with pytest.raises(HostAPIError) as exc:
        lookup_business_upload(owner, ids[1])
    assert exc.value.code == "STATE_CONFLICT"
    assert lookup_business_upload(owner, ids[1]).record == original.record


def test_original_row_digest_is_reverified_even_with_same_stamp(tmp_path, monkeypatch):
    owner, path, ids = _registry(tmp_path, 1)
    first = lookup_business_upload(owner, ids[0])
    path.write_bytes(_line(ids[0], "v2"))
    monkeypatch.setattr(registry, "_stamp", lambda fd: first.proof.source_stamp)
    with pytest.raises(HostAPIError) as exc:
        lookup_business_upload(owner, ids[0])
    assert exc.value.code == "STATE_CONFLICT"


def test_partial_failure_rolls_back_index_and_watermark(tmp_path, monkeypatch):
    owner, path, ids = _registry(tmp_path, 5)
    block = len(_line(ids[0]))
    first = lookup_business_upload(owner, ids[0], max_bytes=block, max_steps=1, return_pending=True)
    assert first.pending and first.watermark == block
    read = registry._read

    def fail_second_read(fd, offset, length):
        if offset == 2 * block:
            raise OSError("simulated I/O error")
        return read(fd, offset, length)

    with monkeypatch.context() as patch:
        patch.setattr(registry, "_read", fail_second_read)
        with pytest.raises(HostAPIError) as exc:
            lookup_business_upload(owner, ids[-1], max_bytes=block, max_steps=2)
    assert exc.value.registry_reason == "io_error"
    with _database(owner) as db:
        stored = db.execute("SELECT value FROM ori_meta WHERE key='upload_registry_watermark'").fetchone()[0]
        assert json.loads(stored)["offset"] == block
        assert db.execute("SELECT count(*) FROM ori_upload_registry").fetchone()[0] == 1
    assert path.stat().st_size == 5 * block
    assert lookup_business_upload(owner, ids[-1]).record.artifact_id == ids[-1]


def test_total_read_budget_includes_hit_verification(tmp_path, monkeypatch):
    owner, _, ids = _registry(tmp_path, 2)
    limit = len(_line(ids[0]))
    lengths = []
    read = registry._read

    def observe(fd, offset, length):
        lengths.append(length)
        return read(fd, offset, length)

    monkeypatch.setattr(registry, "_read", observe)
    for expected_offset in (limit, limit * 2):
        lengths.clear()
        pending = lookup_business_upload(owner, ids[-1], max_total_bytes=limit, return_pending=True)
        assert pending.pending and pending.watermark == expected_offset
        assert sum(lengths) == limit
    lengths.clear()
    assert lookup_business_upload(owner, ids[-1], max_total_bytes=limit).record.artifact_id == ids[-1]
    assert lengths == [limit]


def _publish(owner, artifact_id, version="v1"):
    # The production upload route publishes through the same writer lock and atomic option.
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
    )
    from agent_py_agent.agent.common.directory_lock import locked_private_directory

    root = owner / "artifacts" / "business"
    request = ArtifactRegistration(
        root, root / f"{artifact_id}.blob", artifact_id=artifact_id, status="ready", source="business_upload",
        metadata={"owner_id": "local/test", "version_ref": version, "space_id": "space-1"},
    )
    with locked_private_directory(owner, relative_parts=("artifacts", "business"), lock_name=".business-artifacts.lock"):
        return register_observed_artifact(request, observed_sha256="0" * 64, observed_size_bytes=1, atomic_publish=True)


def test_atomic_uploads_between_bounded_lookups_keep_verified_progress(tmp_path):
    owner, path, ids = _registry(tmp_path, 400)
    block = 40 * len(_line(ids[0]))
    watermarks, inodes = [], set()
    result = None
    for step in range(200):
        result = lookup_business_upload(owner, ids[0], max_bytes=block, max_steps=1, return_pending=True)
        if not result.pending:
            break
        watermarks.append(result.watermark)
        # An ordinary upload replaces the registry inode between every pair of bounded reads.
        _publish(owner, f"artifact-new-{step:035x}")
        inodes.add(path.stat().st_ino)
    assert result is not None and not result.pending
    assert result.record.artifact_id == ids[0]
    assert len(inodes) == len(watermarks) > 1
    assert watermarks == sorted(watermarks) and len(set(watermarks)) == len(watermarks)
    assert verify_record_proof(owner, result.proof).artifact_id == ids[0]
    # The appended rows are indexed from the carried frontier with latest-record semantics.
    assert lookup_business_upload(owner, "artifact-new-" + "0" * 35).record is not None
    _publish(owner, ids[1], "v2")
    assert lookup_business_upload(owner, ids[1]).record.metadata["version_ref"] == "v2"


@pytest.mark.parametrize("change", ["same_size_rewrite", "unterminated_tail"])
def test_unproven_extension_still_rebuilds(tmp_path, change):
    owner, path, ids = _registry(tmp_path, 6)
    block = len(_line(ids[0]))
    assert lookup_business_upload(owner, ids[0], max_bytes=block, max_steps=2, return_pending=True).watermark == 2 * block
    if change == "same_size_rewrite":
        # An unrelated replacement of the same length, not written by the atomic publisher.
        target = path.with_name("next.jsonl")
        target.write_bytes(b"".join(_line(f"artifact-{index:040x}", "v9") for index in range(6, 12)))
        target.replace(path)
    else:
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        _publish(owner, "artifact-" + "f" * 40)
    progress = lookup_business_upload(owner, ids[0], max_bytes=block, max_steps=1, return_pending=True)
    assert progress.pending and progress.watermark == block


def test_projection_rebind_failure_keeps_canonical_publication(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import owner_read_index

    owner, path, ids = _registry(tmp_path, 6)
    block = len(_line(ids[0]))
    assert lookup_business_upload(owner, ids[0], max_bytes=block, max_steps=2, return_pending=True).watermark == 2 * block

    def unavailable(self):
        raise owner_read_index.OwnerReadUnavailable({"status": "unavailable", "reason": "io_error"})

    with monkeypatch.context() as patch:
        patch.setattr(OwnerReadIndex, "upload_registry_transaction", unavailable)
        record = _publish(owner, "artifact-" + "e" * 40)
    assert record.artifact_id == "artifact-" + "e" * 40
    assert path.read_bytes().endswith(_line(ids[-1]) + path.read_bytes().splitlines(keepends=True)[-1])
    # The checkpoint was never rebound, so the next lookup rebuilds instead of trusting stale coverage.
    progress = lookup_business_upload(owner, ids[0], max_bytes=block, max_steps=1, return_pending=True)
    assert progress.pending and progress.watermark == block
    assert lookup_business_upload(owner, record.artifact_id).record.artifact_id == record.artifact_id
