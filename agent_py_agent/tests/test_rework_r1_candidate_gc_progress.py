"""候选不可变节点回收在持续发布、owner缓存淘汰与进程重启之间仍能完成并保留当前树。"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_capacity
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex, immutable_node_path
from agent_py_agent.agent.memory_store import candidate_node_maintenance, retention_scan
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
KEY = "candidate:immutable_nodes"
# 三层虚拟扇出 64：64**3 = 262,144 个叶子，加上中间节点与真实树，标记日志超过一片 8 MiB 字节预算。
VIRTUAL_FANOUT = 64


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


class _VirtualNode(str):
    """测试内的虚拟节点：值为其在虚拟树中的路径，叶子为空串。"""


def _virtual_reference(label, interior):
    digest = hashlib.sha256(f"virtual-node:{label}".encode()).hexdigest()
    # 字节长 1（叶子）或 2（中间节点）只出现在本测试：真实节点至少是一段 JSON。
    return {"ref": {"sha256": digest, "byte_length": 2 if interior else 1, "ref": digest + ".json"}, "maximum": None}


def _attach_virtual_tree(monkeypatch):
    """让当前头额外引用一棵不落盘的大树，标记集合因此超过一片字节预算；清扫只见真实节点。"""
    interiors = {}
    root = _virtual_reference("root", True)
    interiors[root["ref"]["ref"]] = "root"
    original_load = ImmutableJsonIndex._load
    original_children = retention_scan.candidate_node_children
    original_incorporate = retention_scan._incorporate_candidate_head

    def load(self, reference, **options):
        if reference["ref"]["byte_length"] in (1, 2):
            return _VirtualNode(interiors.get(reference["ref"]["ref"], ""))
        return original_load(self, reference, **options)

    def children(node):
        if not isinstance(node, _VirtualNode):
            return original_children(node)
        if not node:
            return ()
        interior = node.count("/") < 2  # root 与第一层的子节点是中间节点，第二层的子节点是叶子。
        references = []
        for number in range(VIRTUAL_FANOUT):
            label = f"{node}/{number}"
            reference = _virtual_reference(label, interior)
            if interior:
                interiors[reference["ref"]["ref"]] = label
            references.append(reference)
        return references

    def incorporate(path, generation, mark):
        before = mark.head
        incorporated = original_incorporate(path, generation, mark)
        if incorporated and mark.head != before:
            retention_scan._push_candidate_reference(mark, root)
        return incorporated

    monkeypatch.setattr(ImmutableJsonIndex, "_load", load)
    monkeypatch.setattr(retention_scan, "candidate_node_children", children)
    monkeypatch.setattr(retention_scan, "_incorporate_candidate_head", incorporate)


def _rotate_owners(path):
    """另外128个owner依次占用128格驻留缓存，本owner的驻留标记状态每片都被挤出。"""
    for owner in range(128):
        retention_scan._remember_candidate_mark(Path(f"/rotating-owner-{owner:03d}") / path.name, object())
    assert path not in retention_scan._CANDIDATE_NODE_MARKS


def test_mark_log_beyond_one_byte_budget_resumes_by_shard_while_129_owners_rotate(tmp_path, monkeypatch):
    _capacity(monkeypatch, 128)
    monkeypatch.setattr(retention_scan, "_CANDIDATE_NODE_MARKS", OrderedDict())
    _attach_virtual_tree(monkeypatch)
    home, path = _ledger(tmp_path, 128)
    service = CandidateService(path)
    directory = _records(path)
    # 生产字节预算（8 MiB）；条目放宽到一片可标记 65,536 个节点，墙钟放宽使结果与机器快慢无关。
    config = SimpleNamespace(retention_scan_slice_entries=65_536, retention_scan_slice_bytes=8 * 1024 * 1024,
                             retention_scan_slice_ms=60_000)
    retention = MemoryRetentionService(home_paths=home, candidates=service, config=config)
    present = datetime.now(timezone.utc)
    before_proof = _orphan(directory, "unreferenced-before-proof")
    service.transition(candidate_row(0).candidate_id, "approved", reviewer="gc-progress")

    # 两天前（受控时钟）的一轮完整回收：每片之前驻留状态都被挤出，只能从游标与分片日志重建。
    for _ in range(24):
        _rotate_owners(path)
        report = retention.apply(now=present - timedelta(days=2))
        if report.scan_cursor[KEY]["status"] == "complete":
            break
    else:
        pytest.fail("collection pass over a mark log larger than one byte budget did not complete")
    entry = report.scan_cursor[KEY]
    assert entry["mark"]["marked"] > 262_144
    assert entry["proof"]["marked"] == entry["mark"]["marked"]
    assert before_proof.exists()
    after_proof = _orphan(directory, "unreferenced-after-proof")

    # 当前时刻头未满24小时：只有两天前的证明能授权回收证明前的孤儿，证明同样按分片读回。
    for _ in range(8):
        _rotate_owners(path)
        report = retention.apply(now=present)
        _current_tree(path)
        if report.scan_cursor[KEY]["status"] == "complete":
            break
    else:
        pytest.fail("sweep with a rotating owner cache did not complete")
    assert not before_proof.exists(), "orphan covered by a 2-day-old proof survived the sweep"
    assert after_proof.exists()
    assert report.scan_cursor[KEY]["proof"]["observed_at"] == present.timestamp()


def _damage_mark_record(home, epoch, name, damage):
    """在本周期日志里找到该节点的标记记录：截短到它之前，或改动它的一个字节。"""
    record = bytes.fromhex(name[:-len(".json")])
    for log in sorted(candidate_node_maintenance.workspace(home).glob(f"{epoch}*.marks")):
        raw = log.read_bytes()
        for offset in range(0, len(raw), len(record)):
            if raw[offset:offset + len(record)] == record:
                if damage == "truncated":
                    log.write_bytes(raw[:offset])
                else:
                    last = offset + len(record) - 1
                    log.write_bytes(raw[:last] + bytes([raw[last] ^ 1]) + raw[last + 1:])
                return
    pytest.fail("current tree node has no mark record in the committed cycle")


@pytest.mark.parametrize("damage", ["truncated", "digest_mismatch"])
def test_damaged_mark_shard_fails_closed_and_keeps_the_current_tree(tmp_path, damage):
    home, path = _ledger(tmp_path, 256)
    service = CandidateService(path)
    retention = MemoryRetentionService(home_paths=home, candidates=service, config=_config())
    present = datetime.now(timezone.utc)
    service.transition(candidate_row(0).candidate_id, "approved", reviewer="gc-progress")
    for _ in range(SLICE_LIMIT):
        report = retention.apply(now=present - timedelta(days=2))
        if report.scan_cursor[KEY]["status"] == "complete":
            break
    else:
        pytest.fail("quiet collection pass did not complete")
    epoch = report.scan_cursor[KEY]["mark"]["epoch"]
    assert report.scan_cursor[KEY]["proof"]["epoch"] == epoch
    # 同一周期的标记与满龄证明共用这份日志：若损坏记录被当作"未标记"，当前树节点会被当作孤儿删除。
    target = read_manifest(path)["root"]["ref"]["ref"]
    _damage_mark_record(home, epoch, target, damage)
    retention_scan._CANDIDATE_NODE_MARKS.clear()

    for _ in range(SLICE_LIMIT):
        report = retention.apply(now=present)
        assert target in _current_tree(path)
        if report.scan_cursor[KEY]["status"] == "complete":
            break
    else:
        pytest.fail("collection did not complete after the damaged checkpoint was abandoned")
    assert report.scan_cursor[KEY]["mark"]["epoch"] != epoch
