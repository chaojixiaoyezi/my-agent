"""验证候选不可变索引在分片、兼容读取与并发发布下的持久化约定。"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.agent.memory_store import retention_scan
from agent_py_agent.agent.memory_store.candidates import (
    CandidateService,
    _NullAdmission,
    _write_candidates_unlocked,
)
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.tests.test_memory_candidate_daily_v2 import _user_observation
from agent_py_agent.tests.test_memory_candidates import candidate as candidate_row
from agent_py_agent.tests.test_memory_retention_wave5 import runtime
from agent_py_agent.tests.test_r8_candidate_pin_stream import _ledger


def _directory(service):
    return service.path.with_name(service.path.name + ".records")


def _flat_node(directory, entries):
    raw = (json.dumps({"entries": entries}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = directory / (digest + ".json")
    path.write_bytes(raw)
    return path, {"ref": {"ref": path.name, "sha256": digest, "byte_length": len(raw)}, "maximum": None}


def test_candidate_writer_stores_json_nodes_under_hash_prefix(tmp_path):
    _, service, _ = runtime(tmp_path)
    candidate = service.observe(_user_observation())
    name = json.loads(service.path.read_bytes())["root"]["ref"]["ref"]
    directory = _directory(service)
    assert (directory / name[:2] / name).is_file()
    assert not (directory / name).exists()
    assert service.get(candidate.candidate_id) == candidate


@pytest.mark.skipif(not nofollow_fs._supports_dir_fd(), reason="platform uses portable directory writes")
def test_unconfirmed_shard_birth_cannot_publish_candidate_head(tmp_path, monkeypatch):
    _, service, _ = runtime(tmp_path)
    directory = _directory(service)
    original_head = service.path.read_bytes()
    fsync = os.fsync
    directory_synced = False

    def fail_first_directory_sync(fd):
        nonlocal directory_synced
        if directory.is_dir() and os.path.samestat(os.fstat(fd), directory.stat()):
            if not directory_synced:
                directory_synced = True
                raise OSError("simulated directory sync error")
        return fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_directory_sync)
    with pytest.raises(OSError, match="simulated directory sync error"):
        service.observe(_user_observation())
    assert directory_synced and service.path.read_bytes() == original_head
    assert not list(directory.glob("*/*.json"))
    candidate = service.observe(_user_observation())
    assert service.get(candidate.candidate_id) == candidate


def test_retention_reclaims_unreachable_node_while_writer_publishes(tmp_path, monkeypatch):
    _, service, retention = runtime(tmp_path)
    first = service.observe(_user_observation())
    directory = _directory(service)
    orphan, _ = _flat_node(directory, {"unused": 1})
    orphan_shard = directory / orphan.name[:2] / orphan.name
    second = candidate_row(17)
    with service._locked_candidates():
        pending = service._load_unlocked()
        pending.append(second)
        root, writes, _ = pending.plan()
    future_name = root["ref"]["ref"]
    future = directory / future_name[:2] / future_name
    future.parent.mkdir(exist_ok=True)
    future.write_bytes(writes[future_name])
    start = datetime.now(timezone.utc)
    retention.apply(now=start)
    retention.apply(now=start + timedelta(hours=23))
    assert orphan.exists() or orphan_shard.exists()
    assert future.is_file()

    publish = Event()
    original_plan = retention.plan
    with ThreadPoolExecutor(max_workers=1) as executor:
        def write():
            assert publish.wait(5)
            with service._locked_candidates():
                _write_candidates_unlocked(service.path, pending, admission=_NullAdmission())

        writer = executor.submit(write)

        def plan(**kwargs):
            result = original_plan(**kwargs)
            publish.set()
            writer.result(timeout=5)
            return result

        monkeypatch.setattr(retention, "plan", plan)
        later = start + timedelta(hours=25)
        for _ in range(24):
            retention.apply(now=later)
            if not orphan.exists() and not orphan_shard.exists():
                break
        writer.result(timeout=5)

    assert not orphan.exists() and not orphan_shard.exists()
    assert future.is_file()
    assert json.loads(service.path.read_bytes())["root"]["ref"]["ref"] == future_name
    assert {row.candidate_id for row in service.list()} == {first.candidate_id, second.candidate_id}


def test_legacy_flat_node_remains_readable(tmp_path):
    directory = tmp_path / "nodes"
    directory.mkdir()
    _, ref = _flat_node(directory, {"saved": 17})
    assert ImmutableJsonIndex(tmp_path, (directory.name,)).get(ref, "saved") == 17


def test_migration_resumes_after_interruption_with_one_entry_slices(tmp_path):
    home, path = _ledger(tmp_path, 6)
    service = CandidateService(path)
    directory = _directory(service)
    names = sorted(node.name for node in directory.glob("*.json"))
    assert len(names) > 1
    config = SimpleNamespace(retention_scan_slice_entries=1, retention_scan_slice_ms=100)
    now = datetime.now(timezone.utc)
    retention = MemoryRetentionService(home_paths=home, candidates=service, config=config)

    for _ in range(len(names) + 8):
        report = retention.apply(now=now)
        pending = list(directory.glob("*.json"))
        if len(pending) < len(names):
            assert len(pending) == len(names) - 1 and report.scan_partial
            break
    else:
        pytest.fail("one-entry maintenance did not migrate a legacy node")

    restarted = MemoryRetentionService(home_paths=home, candidates=CandidateService(path), config=config)
    for _ in range(8 * len(names)):
        report = restarted.apply(now=now)
        assert not report.errors
        if all((directory / name[:2] / name).exists() for name in names):
            break
    assert all((directory / name[:2] / name).is_file() and not (directory / name).exists() for name in names)
    assert len(restarted.candidates.list()) == 6
    restarted.apply(now=now)
    assert all((directory / name[:2] / name).is_file() for name in names)


def test_prepared_transaction_preserves_rollback_root(tmp_path):
    home, service, retention = runtime(tmp_path)
    first = service.observe(_user_observation())
    old_head = service.path.read_bytes()
    name = json.loads(old_head)["root"]["ref"]["ref"]
    directory = _directory(service)
    old_root = directory / name[:2] / name
    transaction = home.owner_memory_curator_dir / "transactions" / "prepared-run"
    transaction.mkdir(parents=True)
    backup = transaction / "before-000.txt"
    backup.write_bytes(old_head)
    service.transition(first.candidate_id, "approved", reviewer="test")
    assert old_head != service.path.read_bytes()

    later = datetime.now(timezone.utc) + timedelta(days=2)
    report = retention.apply(now=later)
    assert report.scan_cursor["candidate:immutable_nodes"]["status"] == "transaction_pending"
    assert old_root.is_file() and backup.read_bytes() == old_head
    assert service.get(first.candidate_id).status == "approved"
    backup.unlink()
    transaction.rmdir()
    for _ in range(8):
        retention.apply(now=later)
        if not old_root.exists():
            break
    assert not old_root.exists()
    assert service.get(first.candidate_id).status == "approved"


@pytest.mark.parametrize("move_during_read", [False, True])
def test_candidate_and_retention_readers_accept_a_mixed_tree(tmp_path, monkeypatch, move_during_read):
    home, path = _ledger(tmp_path, 16)
    service = CandidateService(path)
    root_name = json.loads(path.read_bytes())["root"]["ref"]["ref"]
    directory = _directory(service)
    shard = directory / root_name[:2] / root_name
    shard.parent.mkdir()
    flat = directory / root_name
    if move_during_read:
        open_source = retention_scan._fingerprint_source

        def move_before_open(source):
            if source == flat:
                flat.rename(shard)
            return open_source(source)

        monkeypatch.setattr(retention_scan, "_fingerprint_source", move_before_open)
    else:
        flat.rename(shard)
    errors = []
    now = datetime.now(timezone.utc)
    for _ in range(128):
        pins, complete = retention_scan.current_retention_pins(home, (), errors, now=now)
        assert not errors
        if complete:
            break
    assert complete and {"message-0", "message-15"} <= pins.ids
    assert shard.is_file() and list(directory.glob("*.json"))
    assert len(service.list()) == 16


def test_index_rechecks_shard_when_flat_node_moves_between_probes(tmp_path, monkeypatch):
    directory = tmp_path / "nodes"
    directory.mkdir()
    flat, ref = _flat_node(directory, {"saved": 17})
    shard = directory / flat.name[:2] / flat.name
    original_read = nofollow_fs.read_bytes_beneath

    def move_after_miss(root, parts, **kwargs):
        raw = original_read(root, parts, **kwargs)
        if root.joinpath(*parts) == shard and raw is None:
            shard.parent.mkdir()
            flat.rename(shard)
        return raw

    monkeypatch.setattr(nofollow_fs, "read_bytes_beneath", move_after_miss)
    assert ImmutableJsonIndex(tmp_path, (directory.name,)).get(ref, "saved") == 17
    assert shard.is_file() and not flat.exists()


def test_corrupt_shard_cannot_be_hidden_by_valid_flat_node(tmp_path):
    directory = tmp_path / "nodes"
    directory.mkdir()
    flat, ref = _flat_node(directory, {"saved": 17})
    shard = directory / flat.name[:2] / flat.name
    shard.parent.mkdir()
    shard.write_bytes(b"x" * ref["ref"]["byte_length"])
    with pytest.raises(OSError, match="digest or length"):
        ImmutableJsonIndex(tmp_path, (directory.name,)).get(ref, "saved")
