"""候选不可变节点回收在持续发布、owner缓存淘汰与进程重启之间仍能完成并保留当前树。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_capacity
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex, immutable_node_path
from agent_py_agent.agent.memory_store import retention_scan
from agent_py_agent.agent.memory_store.candidate_ledger import (
    candidate_node_children,
    read_manifest,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.tests.test_memory_candidate_daily_v2 import _user_observation
from agent_py_agent.tests.test_memory_candidates import candidate as candidate_row
from agent_py_agent.tests.test_memory_retention_wave5 import runtime
from agent_py_agent.tests.test_r8_candidate_pin_stream import _ledger

ROWS = 1024
SLICE_LIMIT = 160


def _config():
    # 条目与字节沿用生产默认值；墙钟预算放宽，使每片只受128条约束，结果与机器快慢无关。
    return SimpleNamespace(retention_scan_slice_entries=128, retention_scan_slice_bytes=8 * 1024 * 1024,
                           retention_scan_slice_ms=60_000)


def _records(path):
    return path.with_name(path.name + ".records")


def _orphan(directory, label):
    raw = (json.dumps({"entries": {label: 1}}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    target = immutable_node_path(directory, hashlib.sha256(raw).hexdigest() + ".json")
    target.parent.mkdir(mode=0o700, exist_ok=True)
    target.write_bytes(raw)
    return target


def _current_tree(path):
    """从当前头逐节点读取整棵树；任何缺失或损坏节点都会抛出。"""
    header = read_manifest(path)
    index = ImmutableJsonIndex(path.parent, (_records(path).name,))
    pending, seen = [header["root"]], set()
    while pending:
        reference = pending.pop()
        name = reference["ref"]["ref"]
        if name not in seen:
            seen.add(name)
            pending.extend(candidate_node_children(index._load(reference)))
    return seen


def _assert_rows_readable(path, service):
    assert read_manifest(path)["count"] == ROWS
    assert len(_current_tree(path)) > 4 * 128
    for number in (0, ROWS // 2, ROWS - 1):
        assert service.get(candidate_row(number).candidate_id).candidate_id == candidate_row(number).candidate_id


def _capacity(monkeypatch, count):
    monkeypatch.setattr(cache_capacity, "_SLOTS", cache_capacity._SLOTS.copy())
    cache_capacity.configure_from_config(AgentConfig(owner_agent_pool_max_agents=count))
    assert retention_scan._pin_capacity() == count


def test_tree_larger_than_one_slice_reclaims_orphan_while_writer_publishes_between_slices(tmp_path):
    home, path = _ledger(tmp_path, ROWS)
    service = CandidateService(path)
    orphan = _orphan(_records(path), "unreferenced-before-gc")
    retention = MemoryRetentionService(home_paths=home, candidates=service, config=_config())
    later = datetime.now(timezone.utc) + timedelta(days=3)

    published = 0
    for _ in range(SLICE_LIMIT):
        retention.apply(now=later)
        _current_tree(path)
        if not orphan.exists():
            break
        service.transition(candidate_row(published).candidate_id, "approved", reviewer="gc-progress")
        published += 1

    assert not orphan.exists(), f"eligible orphan survived {published} publications between slices"
    assert published > 8
    _assert_rows_readable(path, service)


@pytest.mark.parametrize("loss", ["restart", "eviction"])
def test_tree_larger_than_one_slice_reclaims_orphan_after_cached_progress_is_lost(tmp_path, monkeypatch, loss):
    home, path = _ledger(tmp_path, ROWS)
    service = CandidateService(path)
    orphan = _orphan(_records(path), "unreferenced-before-gc")
    retention = MemoryRetentionService(home_paths=home, candidates=service, config=_config())
    later = datetime.now(timezone.utc) + timedelta(days=3)
    if loss == "eviction":
        _capacity(monkeypatch, 1)
        _, other_service, other = runtime(tmp_path / "other")
        other_service.observe(_user_observation())

    slices = 0
    for slices in range(1, SLICE_LIMIT + 1):
        retention.apply(now=later)
        if not orphan.exists():
            break
        if loss == "restart":
            retention_scan._CANDIDATE_NODE_MARKS.clear()
        else:
            other.apply(now=later)
            assert path not in retention_scan._CANDIDATE_NODE_MARKS

    assert not orphan.exists(), f"eligible orphan survived {slices} slices with cached progress discarded"
    assert slices > 8
    _assert_rows_readable(path, service)


def test_aged_absence_proof_survives_restart_and_later_publications_do_not_renew_grace(tmp_path):
    home, path = _ledger(tmp_path, ROWS)
    service = CandidateService(path)
    directory = _records(path)
    retention = MemoryRetentionService(home_paths=home, candidates=service, config=_config())
    present = datetime.now(timezone.utc)
    before_proof = _orphan(directory, "unreferenced-before-proof")
    service.transition(candidate_row(0).candidate_id, "approved", reviewer="gc-progress")

    # 两天前（受控时钟）无写入的一轮完整回收建立不可达证明；此时宽限未满，不删除。
    for _ in range(SLICE_LIMIT):
        report = retention.apply(now=present - timedelta(days=2))
        if report.scan_cursor["candidate:immutable_nodes"]["status"] == "complete":
            break
    else:
        pytest.fail("quiet collection pass did not complete")
    assert before_proof.exists()
    after_proof = _orphan(directory, "unreferenced-after-proof")
    retention_scan._CANDIDATE_NODE_MARKS.clear()

    # 当前时刻每片之间都有正常发布，头永远不满24小时；旧证明必须仍允许回收证明前的孤儿。
    published = 1
    for _ in range(SLICE_LIMIT):
        retention.apply(now=present)
        if not before_proof.exists():
            break
        service.transition(candidate_row(published).candidate_id, "approved", reviewer="gc-progress")
        published += 1

    assert not before_proof.exists(), f"orphan covered by a 2-day-old proof survived {published} publications"
    assert after_proof.exists()
    _assert_rows_readable(path, service)
