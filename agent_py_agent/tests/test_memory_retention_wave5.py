# LLM: 只在pytest临时home运行真实保留/配额链；故障注入不替换授权或删除CAS，禁止访问真实owner。
# 模块用途: 覆盖M5误删反例、引用与回收宽限保护、流式读取和展示缓存不绕过配额。
"""M5：坏指纹、读取竞态、引用保护及配额扫描的回归合同。"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store import retention_apply as apply
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionAction
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)

class _ProjectionSnapshot:
    """测试端只推进正式的有界投影；partial 和来源错误都不能当作已发布快照。"""

    def __init__(self, home, roots, now, builder=None, *, size=1024 * 1024, entries=128):
        from types import SimpleNamespace

        self.home, self.roots, self.now = home, roots, now
        self.config = SimpleNamespace(retention_scan_slice_entries=entries,
            retention_scan_slice_bytes=size, retention_scan_slice_ms=25)
        self.projection = None
        self.last_budget = None

    def current(self, errors, *, target=None):
        self.last_budget = scan.RetentionScanBudget.default(self.config)
        result = scan.current_retention_pins(self.home, self.roots, errors, now=self.now,
            scan_budget=self.last_budget, config=self.config)
        self.projection = scan._CURRENT_PIN_BUILDS.get(Path(self.home.owner_home_dir))
        assert self.last_budget.entries <= self.last_budget.max_entries
        assert self.last_budget.size <= self.last_budget.max_bytes
        if not result[1]:
            assert not result[0].complete
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
        return result

    @property
    def value(self):
        return self.projection.value if self.projection is not None else None

    @property
    def pending(self):
        return self.projection.pending if self.projection is not None else None

    @property
    def contents(self):
        return self.projection.contents

    @property
    def sources(self):
        return self.projection.sources




def _transient_pin_source(monkeypatch, path, transient, payload):
    original, fired = scan._fingerprint_source, []

    class Reader:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def close(self):
            try:
                self.raw.close()
            finally:
                path.write_text(payload)

    def source(target):
        if target == path and not fired:
            path.write_text(transient)
            fired.append(True)
            return Reader(original(target))
        return original(target)

    monkeypatch.setattr(scan, "_fingerprint_source", source)
    return fired


def _complete_pins(home, roots, errors, *, now):
    """稳定正例允许多片完成；每片仍独立检查当前默认预算和错误回执。"""
    for _ in range(512):
        budget = scan.RetentionScanBudget.default()
        result = scan.current_retention_pins(home, roots, errors, now=now, scan_budget=budget)
        assert budget.entries <= budget.max_entries and budget.size <= budget.max_bytes
        if result[1] or errors:
            return result
        assert not result[0].complete
    pytest.fail("stable pin sources did not finish bounded slices")


@pytest.fixture(autouse=True)
def _deterministic_scan_clock(monkeypatch):
    """固定单片时钟成本；条目/实读字节预算与测试自行注入的慢IO时钟仍生效。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.memory_store import retention_scan_budget

    ticks = [0.0]

    def monotonic():
        ticks[0] += 0.000001
        return ticks[0]

    monkeypatch.setattr(retention_scan_budget, "time", SimpleNamespace(monotonic=monotonic))
    yield
    with scan._PIN_BUILDS_LOCK:
        snapshots = list(scan._CURRENT_PIN_BUILDS.values()) + list(scan._PIN_BUILDS.values())
        scan._CURRENT_PIN_BUILDS.clear()
        scan._PIN_BUILDS.clear()
    for snapshot in snapshots:
        snapshot.close()



# LLM: 冷详情的pin合同不变；正式记忆无论是否引用都永久保留，并用缓存对照证明留存没有全停。
# 函数用途: 验证冷候选证据在真实引用扫描、规划及执行中的保护与坏源失败边界。
@pytest.mark.parametrize("damage", [False, True])
def test_r2_promoted_cold_details_pin_expired_evidence_or_fail_closed(tmp_path, damage):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    home, candidates, service = runtime(tmp_path)
    path = old_file(home.owner_memory_daily_dir / "cold-evidence.jsonl")
    control = old_file(home.owner_memory_daily_dir / "unreferenced.jsonl")
    cache = old_file(home.owner_cache_dir / "unreferenced-cache.jsonl")
    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="已晋升的归档事实",
        subject_key="r2.cold", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson", evidence_refs=[{"path": str(path)}]))
    candidates.transition(row.candidate_id, "approved", reviewer="test")
    row = candidates.mark_promoted(row.candidate_id, reviewer="test", promotion_ref="memory/lessons/r2")
    candidates.path.write_text(json.dumps(replace(row, updated_at="2020-01-01T00:00:00+00:00").to_record()) + "\n")
    assert candidates.compact_terminal_candidates(now=NOW) == 1
    hot = candidates.readonly_rows()[0]
    assert hot.status == "promoted" and hot.cold_detail_ref and not hot.evidence_refs
    if damage:
        next((candidates.path.parent / "candidates_archive").glob("*.jsonl")).write_text("{corrupt\n")
    errors = []
    pins, complete = _complete_pins(home, (), errors, now=NOW)
    assert complete is not damage
    if not damage:
        assert not errors and pins.protects(path, errors)
        archive = next((candidates.path.parent / "candidates_archive").glob("*.jsonl"))
        assert pins.protects(archive, errors)
    else:
        assert errors and errors[0].error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"
    plan = service.plan(now=NOW)
    assert not [item for item in plan.actions if item.path == path]
    if not damage:
        assert not [item for item in plan.actions if item.path == control]
    report = service.apply(now=NOW)
    assert path.exists() and not [item for item in report.actions if item.path == path and item.status == "trashed"]
    assert control.exists() and not cache.exists()


# LLM: 运行账恢复链及未引用分片都是永久记忆资产；原pin与日志读取断言仍须满足。
# 函数用途: 验证版本化 Curator 游标分片跨日被引用保护。
def test_r2_versioned_curator_shards_are_pinned_across_days(tmp_path):
    from agent_py_agent.agent.memory_store.curator_run_log import (
        _CURSOR_CHECKPOINT,
        _CURSOR_DELTA,
        CuratorRunLog,
        CuratorRunRecord,
    )

    home, _, service = runtime(tmp_path)
    log = CuratorRunLog(home.owner_memory_curator_runs_dir)
    before = {"per_thread_cursors": {f"thread-{i}": f"message-{i}" for i in range(40)}, "last_audit_event_id": ""}
    after = {"per_thread_cursors": {**before["per_thread_cursors"], "active": "new"}, "last_audit_event_id": ""}
    first = CuratorRunRecord("r2-checkpoint", "lease", "succeeded", "admin", "test", "test",
        "2020-01-02T00:00:00+00:00", "2020-01-02T00:00:00+00:00", cursor_before=before, cursor_after=after)
    log.append(first)
    log.append(replace(first, run_id="r2-delta", started_at="2020-01-02T01:00:00+00:00",
        finished_at="2020-01-02T01:00:00+00:00", cursor_before=after))
    log.append(replace(first, run_id="r2-new-day", started_at="2026-10-26T00:00:00+00:00",
        finished_at="2026-10-26T00:00:00+00:00", cursor_before=after))
    old = home.owner_memory_curator_runs_dir / "2020-01-02.jsonl"
    warnings = [warning for line in old.read_text().splitlines() for warning in json.loads(line)["warnings"]]
    assert any(value.startswith(_CURSOR_CHECKPOINT) for value in warnings)
    assert any(value.startswith(_CURSOR_DELTA) for value in warnings)
    log.append(replace(first, run_id="r2-unreferenced", started_at="2020-01-01T00:00:00+00:00",
        finished_at="2020-01-01T00:00:00+00:00", cursor_before={}, cursor_after={}))
    control = old.with_name("2020-01-01.jsonl")
    os.utime(control, (1, 1))
    os.utime(old, (1, 1))
    errors = []
    pins, complete = _complete_pins(home, (), errors, now=NOW)
    assert complete and not errors and old.resolve() in pins.paths
    assert not [item for item in service.plan(now=NOW).actions if item.path == old]
    assert not [item for item in service.plan(now=NOW).actions if item.path == control]
    report = service.apply(now=NOW)
    assert report.ok and old.exists() and control.exists()
    assert {row.run_id for row in log.list()} == {"r2-checkpoint", "r2-delta", "r2-new-day", "r2-unreferenced"}
    assert len(log.recent_finished(4)) == 4


# LLM: 所有写入限于pytest临时home；返回真实服务，不替换授权和执行链。
# 函数用途: 构造最小保留服务，便于验证实际文件是否被保护。
def runtime(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    return home, candidates, MemoryRetentionService(home_paths=home, candidates=candidates)


# LLM: 用真实可恢复写者生成已过宽限的缓存容器；这些是依赖pins的可清理对象，不再要求删除永久记忆。
# 函数用途: 为旧引用安全回归提供真实墓碑、摘要与payload。
def _expired_pin_target(home, name, content="{}\n"):
    source = old_file(home.owner_cache_dir / name, content)
    destination = Path(home.owner_trash_dir) / "retention" / "cache" / Path(name).stem
    moved = apply._trash_file(home=home, action=MemoryRetentionAction(
        action_id=f"fixture-{Path(name).stem}", category="cache", operation="trash_file", path=source,
        reason="expired cache fixture", destination=destination, source_fingerprint=scan.file_fingerprint(source),
        cutoff_timestamp=NOW.timestamp()), now=NOW - timedelta(days=60))
    assert moved.status == "trashed" and destination.exists() and not source.exists()
    return destination


def test_generation_formal_pins_invalidate_same_apply_snapshot(tmp_path, monkeypatch):
    home, _, _ = runtime(tmp_path)
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    first = memory.add("user", "fact", attributes={"source_refs": ["evidence-before"]})
    memory.remove(first.entry_id)
    builds, original = [], scan._PinProjection._formal
    def formal(projection, pins):
        builds.append(True)
        yield from original(projection, pins)
    monkeypatch.setattr(scan._PinProjection, "_formal", formal)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    old = _r5_finish_verification(snapshot, [])
    assert len(builds) == 1
    assert _r5_finish_verification(snapshot, [])[0] is old[0] and len(builds) == 1
    target = old_file(home.owner_memory_daily_dir / "new-generation-evidence.jsonl")
    memory.add("user", "new fact", attributes={"source_refs": [str(target)]})
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and len(builds) == 2 and pins.protects(target, [])


@pytest.mark.parametrize("source", ["formal", "legacy-tombstone"])
@pytest.mark.parametrize("damage", ["unknown-action", "negative-expiry", "nan-expiry", "inf-expiry", "bool-expiry", "text-expiry"])
def test_legacy_semantic_corruption_blocks_retention(tmp_path, source, damage):
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
    from agent_py_agent.agent.memory_store.jsonl import _tombstone_path

    home, _, service = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "still-referenced.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "still important", attributes={"source_refs": [str(target)]})
    assert MemoryGenerationStore(memory.path).manifest() is None
    row = json.loads(memory.path.read_text().splitlines()[0])
    path = memory.path
    if source == "legacy-tombstone":
        path = _tombstone_path(path)
        row.update(action="remove", content="", entry_id="unrelated-deletion", attributes=None)
    if damage == "unknown-action":
        row["action"] = "bogus"
    else:
        row["expires_at"] = {"negative-expiry": -1, "nan-expiry": float("nan"),
                             "inf-expiry": float("inf"), "bool-expiry": True, "text-expiry": "later"}[damage]
    path.write_text(json.dumps(row) + "\n")
    errors = []
    _, complete = _complete_pins(home, (), errors, now=NOW)
    assert not complete
    assert any(error.error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" for error in errors)
    assert not [action for action in service.plan(now=NOW).actions if action.path == target]
    report = service.apply(now=NOW)
    assert target.exists()
    assert not [action for action in report.actions if action.path == target and action.status == "trashed"]


# LLM: 真实墓碑与缓存pin沿同一留存入口验证；旧版本记录也不得通过重新追加而复活来源身份。
# 函数用途: 验证被墓碑删除的记忆不再复活，也不再阻止其过期缓存进入回收计划。
@pytest.mark.parametrize("version", [1, 3])
def test_legacy_tombstone_remains_a_permanent_identity_barrier(tmp_path, version):
    from agent_py_agent.agent.memory_store.jsonl import (
        _tombstone_path,
        read_memory_records_from_path,
    )

    home, _, service = runtime(tmp_path)
    target = _expired_pin_target(home, "deleted-source.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "deleted fact", attributes={"source_refs": [str(target)]})
    row = json.loads(memory.path.read_text().splitlines()[0])
    row["version"] = version
    memory.path.write_text(json.dumps(row) + "\n")
    # 旧删除账也永久屏蔽同 ID；不能靠放在 hot 文件末尾或提高版本复活。
    tombstone = {**row, "action": "remove", "content": "", "version": 2, "attributes": None}
    _tombstone_path(memory.path).write_text(json.dumps(tombstone) + "\n")
    assert read_memory_records_from_path(memory.path) == []
    errors = []
    pins, complete = _complete_pins(home, (), errors, now=NOW)
    assert complete and not errors and not pins.protects(target, errors)
    assert [action for action in service.plan(now=NOW).actions if action.path == target]


# LLM: 只冻结指定路径与其打开FD；不改内容或时间流，供同tick读写反例复用。
# 函数用途: 模拟文件系统在一次粗时钟片内报告同样元数据。
def _freeze_pin_file_stat(monkeypatch, path):
    fixed = path.stat(follow_symlinks=False)
    original_stat, original_fstat = Path.stat, os.fstat

    # LLM: 只替换指定路径，其余参数和文件仍交原实现。
    # 函数用途: 固定被测路径的stat观察。
    def frozen_stat(self, *args, **kwargs):
        return fixed if self == path else original_stat(self, *args, **kwargs)

    # LLM: 仅相同dev/ino的真实FD冻结，不把其他句柄冒充目标。
    # 函数用途: 固定被测文件的FD元数据。
    def frozen_fstat(fd):
        current = original_fstat(fd)
        return fixed if (current.st_dev, current.st_ino) == (fixed.st_dev, fixed.st_ino) else current

    monkeypatch.setattr(Path, "stat", frozen_stat)
    monkeypatch.setattr(os, "fstat", frozen_fstat)


@pytest.mark.parametrize("name", ["formal.jsonl", "memory_tombstones.jsonl"])
def test_pin_source_stamp_detects_same_inode_equal_length_rewrite_with_frozen_stat(tmp_path, monkeypatch, name):
    from agent_py_agent.agent.memory_store.reference_pins import source_stamp

    path = tmp_path / name
    before_raw, after_raw = b'{"version":1}\n', b'{"version":2}\n'
    assert len(before_raw) == len(after_raw)
    path.write_bytes(before_raw)
    identity = os.stat(path, follow_symlinks=False)
    _freeze_pin_file_stat(monkeypatch, path)
    before = source_stamp(path)
    path.write_bytes(after_raw)
    current = os.stat(path, follow_symlinks=False)
    assert (current.st_ino, current.st_size) == (identity.st_ino, identity.st_size)
    after = source_stamp(path)
    assert before[:6] == after[:6]
    assert before != after


@pytest.mark.parametrize("source", ["formal", "legacy-tombstone"])
@pytest.mark.parametrize("freeze_stat", [False, True], ids=["native-clock", "coarse-clock"])
def test_legacy_pin_source_mid_read_change_is_not_cached(tmp_path, monkeypatch, source, freeze_stat):
    from agent_py_agent.agent.memory_store.jsonl import _tombstone_path
    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "fact", attributes={"source_refs": [str(control)]})
    path = memory.path
    if source == "legacy-tombstone":
        row = json.loads(path.read_text())
        row.update(action="remove", content="", attributes=None)
        path = _tombstone_path(path)
        path.write_text(json.dumps(row)+'\n')
    payload = path.read_text()
    if freeze_stat:
        _freeze_pin_file_stat(monkeypatch, path)
    original, changed = scan._PinProjection._rows, []
    def rows(projection, source_path, **kwargs):
        for row in original(projection, source_path, **kwargs):
            if row is not None and source_path == path and not changed:
                rewritten = payload.replace("pin-a.jsonl", "pin-b.jsonl") if source == "formal" else payload.replace(row["entry_id"], "x"*len(row["entry_id"]))
                path.write_text(rewritten)
                changed.append(path)
            yield row
    monkeypatch.setattr(scan._PinProjection, "_rows", rows)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current([])
    assert changed == [path] and snapshot.value is None
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and pins.protects(target if source == "formal" else control, [])


# LLM: 零有效期遵循正式写入的永久语义；实际plan/apply都要保护证据而非全局停清理。
# 函数用途: 验证legacy正式事件与删除账接受显式零expiry。
@pytest.mark.parametrize("source", ["formal", "legacy-tombstone"])
@pytest.mark.parametrize("expiry", [0, 0.0])
def test_legacy_zero_expiry_is_permanent(tmp_path, source, expiry):
    from agent_py_agent.agent.memory_store.jsonl import _tombstone_path

    home, _, service = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "permanent.jsonl")
    control = _expired_pin_target(home, "unreferenced.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "permanent", attributes={"source_refs": [str(target)]})
    row = json.loads(memory.path.read_text().splitlines()[0])
    path = memory.path if source == "formal" else _tombstone_path(memory.path)
    if source == "legacy-tombstone":
        row.update(action="remove", content="", entry_id="unrelated-deletion", attributes=None)
    path.write_text(json.dumps({**row, "expires_at": expiry}) + "\n")
    errors = []
    pins, complete = _complete_pins(home, (), errors, now=NOW)
    assert complete and not errors and pins.protects(target, errors)
    report = service.apply(now=NOW)
    assert report.ok and target.exists() and not control.exists()


# LLM: 来源真实迁移成generation；分别破坏不同权威源，实际apply不得移动受保护目标。
# 函数用途: 将generation坏源保护纳入正式回归。
@pytest.mark.parametrize("damage", ["manifest", "missing-segment", "segment", "storage-tombstone", "legacy-tombstone"])
def test_generation_pin_corruption_blocks_plan_and_apply(tmp_path, damage):
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
    from agent_py_agent.agent.memory_store.jsonl import _tombstone_path

    home, candidates, service = runtime(tmp_path)
    target = _expired_pin_target(home, "generation-source.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "generation fact", attributes={"source_refs": ["other-evidence"]})
    memory.migrate_tiers()
    store = MemoryGenerationStore(memory.path)
    manifest = store.manifest()
    assert manifest and manifest["segments"]
    old_plan = service.plan(now=NOW)
    assert old_plan.ok and [item for item in old_plan.actions if item.path == target]
    if damage == "manifest":
        store.manifest_path.write_text("{bad")
    elif damage in {"segment", "missing-segment"}:
        segment = memory.path.parent / manifest["segments"][0]["path"]
        if damage == "missing-segment":
            segment.unlink()
        else:
            segment.write_text(segment.read_text().replace("fact", "fakt"))
    else:
        path = store.tombstones.path if damage == "storage-tombstone" else _tombstone_path(memory.path)
        path.write_text("{bad\n")
    plan = service.plan(now=NOW)
    assert any(error.error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" for error in plan.errors)
    assert not [item for item in plan.actions if item.path == target]
    report = apply.execute_retention_plan(home=home, candidates=candidates, plan=old_plan, now=NOW)
    assert report.scan_partial
    assert next(item for item in report.actions if item.path == target).status == "not_started"
    errors = []
    _, complete = _complete_pins(home, (), errors, now=NOW)
    assert not complete and any(error.error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" for error in errors)
    assert target.exists()


# LLM: 目录元数据冻结但新增线程及引用均真实；必须重建pin，不能由无关文件变化代替触发。
# 函数用途: 守护同tick新线程引用的缓存失效。
def test_same_tick_new_thread_invalidates_pin_snapshot(tmp_path, monkeypatch):

    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "daily.jsonl", '{"event_id":"daily-id"}\n')
    directory = home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads"
    old_file(directory / "a.json", json.dumps({"thread_id": "a", "status": "active"}))
    _freeze_pin_file_stat(monkeypatch, directory)
    snapshot = _ProjectionSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    first, complete = snapshot.current(errors)
    assert complete and not errors and not first.protects(target, errors)
    (directory / "new.json").write_text(json.dumps({"thread_id": "new", "status": "active",
                                                  "source_refs": [{"event_id": "daily-id"}]}))
    second, complete = _r5_finish_verification(snapshot, errors)
    assert complete and not errors and second is not first and second.protects(target, errors)


# LLM: 大于旧16MiB门的真实稀疏文件仍须流式校验，冻结元数据不能隐藏等长原地变化。
# 函数用途: 验证大来源不会退回只看stat且单次读块有界。
def test_fresh_large_pin_source_is_streamed_and_detects_same_tick_rewrite(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.reference_pins import source_stamp

    path = tmp_path / "large.jsonl"
    with path.open("wb") as output:
        output.truncate(17 * 1024 * 1024)
    _freeze_pin_file_stat(monkeypatch, path)
    original_read, reads = os.read, []

    # LLM: 保留实际字节，观测被测文件读取是否分块而不是整本分配。
    # 函数用途: 记录系统读取请求大小。
    def read(fd, size):
        reads.append(size)
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", read)
    before = source_stamp(path)
    with path.open("r+b") as output:
        output.seek(-1, os.SEEK_END)
        output.write(b"x")
    after = source_stamp(path)
    assert before[:6] == after[:6] and before != after
    assert reads and max(reads) <= 64 * 1024


# LLM: 将真实文件代次移到2秒窗口外；日记永久保留，缓存删除仍提供多动作重验证成本对照。
# 函数用途: 用读取次数而非易抖动耗时约束多动作保留成本。
def test_apply_revalidation_does_not_reread_settled_thread_bodies(tmp_path, monkeypatch):
    import time

    from agent_py_agent.agent.conversation.models import ConversationThread

    home, _, service = runtime(tmp_path)
    # 先建立正常审计设施，再固定被测来源；首次回执创建锁侧车不是正文的按动作重读。
    assert not service.apply(now=NOW).scan_partial
    directory = Path(home.owner_home_dir) / "conversations/threads"
    directory.mkdir(parents=True)
    identities = set()
    source_paths = set()
    for index in range(20):
        path = directory / f"t{index:04d}.json"
        thread = ConversationThread(thread_id=path.stem, canonical_user_id="owner", updated_at=NOW.timestamp(),
                                    summary="s" * 200000)
        path.write_text(json.dumps(thread.to_dict()))
        identities.add(path.stat().st_ino)
        source_paths.add(path)
        old_file(home.owner_memory_daily_dir / f"old-{index}.jsonl")
        old_file(home.owner_cache_dir / f"old-{index}.jsonl")
    later = time.time_ns() + 3_000_000_000
    monkeypatch.setattr(time, "time_ns", lambda: later)
    original_read, reads = os.read, []

    # LLM: 只统计所建线程正文的OS读取，实际保留和JSON解析链不被替换。
    # 函数用途: 检测每动作重复的来源hash读取。
    def read(fd, size):
        if os.fstat(fd).st_ino in identities:
            reads.append(size)
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", read)
    original_source, opened, actual_bytes = scan._fingerprint_source, {}, []

    class CountedSource:
        def __init__(self, raw, intervals):
            self.raw = raw
            self.intervals = intervals

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def read1(self, size):
            offset = self.raw.tell()
            raw = self.raw.read1(size)
            actual_bytes.append(len(raw))
            self.intervals.append((offset, len(raw)))
            return raw

    def source(path):
        raw = original_source(path)
        if path in source_paths:
            intervals = []
            opened.setdefault(path, []).append((pass_number, intervals))
            return CountedSource(raw, intervals)
        return raw

    monkeypatch.setattr(scan, "_fingerprint_source", source)
    actions = []
    for pass_number in range(64):
        report = service.apply(now=NOW)
        assert report.ok
        actions.extend(report.actions)
        if not report.scan_partial:
            break
    assert not report.scan_partial
    assert len([item for item in actions if item.category == "daily" and item.status == "trashed"]) == 0
    assert len(list(home.owner_memory_daily_dir.glob("old-*.jsonl"))) == 20
    assert len([item for item in actions if item.category == "cache" and item.status == "deleted"]) == 20
    assert not reads, "settled thread content must not be rehashed per retention action"
    # 每个稳定文件只从头读两遍。真实时间片恰好中断文件时，后续片才可在准确偏移重新打开。
    assert set(opened) == source_paths
    for path, reads_by_pass in opened.items():
        length = path.stat().st_size
        offset = passes = 0
        previous_pass = None
        for tick, intervals in reads_by_pass:
            assert intervals, "must not reopen a settled source merely to check EOF"
            assert not offset or tick != previous_pass, "same slice reopened a small settled source"
            previous_pass = tick
            for start, size in intervals:
                assert start == offset and size > 0
                offset += size
                assert offset <= length
            if offset == length:
                passes += 1
                offset = 0
        assert (passes, offset) == (2, 0)
    assert sum(actual_bytes) == 2 * sum(path.stat().st_size for path in source_paths)


# LLM: 热源缓存只能省hash，仍须逐次核真实字节；相同stat下改写必须重建引用视图。
# 函数用途: 防止2秒窗口内不变线程重复做昂贵摘要，同时守住冻结stat的改写。
def test_recent_thread_digest_is_reused_but_equal_length_changes_are_not(tmp_path, monkeypatch):
    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    path = old_file(home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads/t.json",
        json.dumps({"thread_id":"t", "summary":"s"*200000, "source_refs":[str(control)]}))
    _freeze_pin_file_stat(monkeypatch, path)
    parsed, reads, original = [], [], scan._PinProjection._rows
    original_source = scan._fingerprint_source
    def rows(projection, source, **kwargs):
        if source == path:
            parsed.append(source)
        yield from original(projection, source, **kwargs)
    def opened(source):
        raw = original_source(source)
        if source == path:
            reads.append(raw)
        return raw
    monkeypatch.setattr(scan._PinProjection, "_rows", rows)
    monkeypatch.setattr(scan, "_fingerprint_source", opened)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    first, complete = _r5_finish_verification(snapshot, [])
    assert complete and not first.protects(target, []) and len(parsed) == 1
    for _ in range(5):
        before = len(reads)
        assert _r5_finish_verification(snapshot, [])[0] is first
        assert len(reads) > before and len(parsed) == 1
        assert all(reader.closed for reader in reads)
    path.write_text(path.read_text().replace("pin-a.jsonl", "pin-b.jsonl"))
    second, complete = _r5_finish_verification(snapshot, [])
    assert complete and second is not first and second.protects(target, []) and len(parsed) == 2
    assert all(reader.closed for reader in reads)


# LLM: 热源转冷仍须最后比对真实字节；同tick变化才重建，未变化不得因跨时钟片而失败。
# 函数用途: 验证跨2秒窗口时仍可靠地识别是否有新引用。
@pytest.mark.parametrize("changed", [False, True])
def test_recent_pin_snapshot_is_rebuilt_after_settling(tmp_path, monkeypatch, changed):
    import time


    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "fact", attributes={"source_refs": [str(control)]})
    _freeze_pin_file_stat(monkeypatch, memory.path)
    snapshot = _ProjectionSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    first, complete = snapshot.current(errors)
    assert complete and not errors and not first.protects(target, errors)
    if changed:
        memory.path.write_text(memory.path.read_text().replace("pin-a.jsonl", "pin-b.jsonl"))
    later = time.time_ns() + 3_000_000_000
    monkeypatch.setattr(time, "time_ns", lambda: later)
    second, complete = _r5_finish_verification(snapshot, errors)
    assert complete and not errors and second.protects(target, errors) is changed
    assert (second is not first) is changed


# LLM: 通用pin解析期间真实改写引用但冻结stat；读后才登记来源会把旧pins和新摘要错误配对。
# 函数用途: 验证线程/策展源都具备读前读后的内容代次门。
@pytest.mark.parametrize("kind", ["thread", "curator"])
def test_generic_pin_source_mid_read_change_is_not_cached(tmp_path, monkeypatch, kind):
    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    path = (home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads/t.json" if kind == "thread"
            else home.owner_memory_curator_runs_dir / "run.jsonl")
    payload = json.dumps({"thread_id":"t", "source_refs":[str(control)]})+'\n'
    old_file(path, payload)
    _freeze_pin_file_stat(monkeypatch, path)
    original, changed = scan._PinProjection._rows, []
    def rows(projection, source, **kwargs):
        for row in original(projection, source, **kwargs):
            if source == path and row is not None and not changed:
                path.write_text(payload.replace("pin-a.jsonl", "pin-b.jsonl"))
                changed.append(path)
            yield row
    monkeypatch.setattr(scan._PinProjection, "_rows", rows)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    with pytest.raises(OSError):
        snapshot.current([])
    assert changed == [path] and snapshot.value is None
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and pins.protects(target, [])


# LLM: 小预算和空源分别压体积及条目上限；新一轮来源集合不得滞留已删除路径，外层上下文须恢复。
# 函数用途: 验证临时正文缓存和FD缓存有界且不跨snapshot/owner泄漏。
@pytest.mark.parametrize("payload_size", [0, 600])
def test_pin_source_cache_bounds_and_rebuild_cleanup(tmp_path, monkeypatch, payload_size):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, _ = runtime(tmp_path)
    paths = [old_file(home.owner_memory_curator_runs_dir / f"run-{index}.jsonl",
                     json.dumps({"summary": "s" * payload_size}) + "\n" if payload_size else "")
             for index in range(140)]
    monkeypatch.setattr(reference_pins, "_MAX_PIN_SOURCE_DIGEST_BYTES", 1024)
    snapshot = _ProjectionSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    assert _r5_finish_verification(snapshot, errors)[1] and not errors
    assert len(snapshot.contents["files"]) <= 128 and len(snapshot.contents["fds"]) <= 128
    assert snapshot.contents["bytes"] <= 4096
    assert snapshot.contents["bytes"] == sum(item[3] for item in snapshot.contents["files"].values())
    other_home, _, _ = runtime(tmp_path / "other")
    other = _ProjectionSnapshot(other_home, (), NOW, scan.current_retention_pins)
    assert _r5_finish_verification(other, errors)[1] and other.contents is not snapshot.contents
    assert not set(paths).intersection(other.contents["files"])
    for path in paths:
        path.unlink()
    assert _r5_finish_verification(snapshot, errors)[1] and not errors
    assert not set(paths).intersection(snapshot.contents["files"])
    assert reference_pins._SOURCE_CONTENTS.get() is None and reference_pins._SOURCES.get() is None
    home.owner_memory_long_term_jsonl.write_text("{bad\n")
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current(errors)
    assert reference_pins._SOURCE_CONTENTS.get() is None and reference_pins._SOURCES.get() is None


# LLM: 仅准备超期测试文件，不修改真实用户数据。
# 函数用途: 写入文件并把修改时间设为过期。
def old_file(path, text="{}\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (1, 1))
    return path


# LLM: 构造执行端可消费的旧文件动作；调用方可注入无效指纹。
# 函数用途: 准备一个用于直接验证执行防线的删除动作。
def action(path, digest):
    return MemoryRetentionAction(action_id="test", category="tmp", operation="delete_file",
        path=path, reason="old", source_fingerprint=digest, cutoff_timestamp=NOW.timestamp())


# LLM: 注入指定路径的真实OSError形状，同时覆盖Path与非阻塞FD入口，其余文件仍走原open。
# 函数用途: 在不依赖root权限差异的情况下重现读失败。
def block_open(monkeypatch, path, error):
    original = Path.open

    # LLM: 仅目标路径失败，保留其他文件与参数语义。
    # 函数用途: 模拟目标文件读取故障。
    def opened(self, *args, **kwargs):
        if self == path:
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    original_fd = os.open

    # LLM: 原生FD打开与Path入口注入相同故障，不影响其他路径。
    # 函数用途: 验证非阻塞指纹入口也正确分类读错。
    def opened_fd(target, *args, **kwargs):
        if os.fspath(target) == os.fspath(path):
            raise error
        return original_fd(target, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened_fd)


# LLM: 注入三类非ENOENT错误，摘要入口必须与missing分离。
# 函数用途: 验证权限、句柄和I/O读错均返回unreadable。
@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"),
    OSError(errno.EMFILE, "fds"), OSError(errno.EIO, "io")])
def test_fingerprint_distinguishes_unreadable_from_missing(tmp_path, monkeypatch, error):
    path = old_file(tmp_path / "file")
    block_open(monkeypatch, path, error)
    assert scan.file_fingerprint(path) == "unreadable"
    assert scan.file_fingerprint(tmp_path / "absent") == "missing"


# LLM: 直接污染计划摘要，不能靠执行阶段补偿扫描端坏计划。
# 函数用途: 验证非法摘要不进入清理计划且有诊断。
@pytest.mark.parametrize("digest", ["missing", "unreadable", "", "a" * 63, "A" * 64, "g" * 64])
def test_scan_never_schedules_invalid_digest(tmp_path, monkeypatch, digest):
    home, _, service = runtime(tmp_path)
    path = old_file(home.owner_cache_dir / "old.jsonl")
    original = scan.file_fingerprint
    monkeypatch.setattr(scan, "file_fingerprint", lambda p: digest if p == path else original(p))
    plan = service.plan(now=NOW)
    assert not [a for a in plan.actions if a.path == path]
    assert any(e.path == str(path) and "FINGERPRINT" in e.error_code for e in plan.errors)


# LLM: 计划和当前坏摘要故意相等，原文件仍必须保留。
# 函数用途: 重现并阻止哨兵值相等导致的误删。
@pytest.mark.parametrize("digest", ["missing", "unreadable", "", "a" * 63, "A" * 64, "g" * 64])
def test_apply_never_accepts_matching_invalid_digests(tmp_path, monkeypatch, digest):
    path = old_file(tmp_path / "file")
    monkeypatch.setattr(apply, "file_fingerprint", lambda p: digest)
    result = apply._delete_file(action(path, digest))
    assert path.exists()
    assert result.status == "state_changed"


# LLM: 组合摘要不得把坏成员标记重新hash成看似有效证据。
# 函数用途: 验证目录与多路径摘要传播成员读取失败。
@pytest.mark.parametrize("composite", ["tree", "paths"])
@pytest.mark.parametrize("digest", ["missing", "unreadable", "invalid"])
def test_composite_fingerprint_propagates_bad_members(tmp_path, monkeypatch, composite, digest):
    path = old_file(tmp_path / "file")
    monkeypatch.setattr(scan, "file_fingerprint", lambda p: digest)
    result = scan.tree_fingerprint(tmp_path) if composite == "tree" else scan.paths_fingerprint((path,))
    assert result == "unreadable"


# LLM: 真实FD和真实并发字节变化守护块大小及读前后身份门。
# 函数用途: 验证64KiB读取与读中修改拒绝。
def test_streaming_hash_uses_64k_and_rejects_concurrent_mutation(tmp_path, monkeypatch):
    path = old_file(tmp_path / "file", "x" * 150000)
    original = Path.open
    reads = []

    # LLM: 使用原FD读取，记录块大小并可在首块后真实修改目标。
    # 类用途: 暴露hash读取大小和读中变化，而不伪造摘要。
    class Reader:
        # LLM: 持原文件上下文，仅观察二进制读取。
        # 函数用途: 打开真实被测文件。
        def __enter__(self):
            self.file = original(path, "rb")
            return self

        # LLM: 始终关闭原FD，不吞异常。
        # 函数用途: 清理测试读取句柄。
        def __exit__(self, *args):
            self.file.close()

        # LLM: fstat必须来自真实FD。
        # 函数用途: 提供实际文件描述符。
        def fileno(self):
            return self.file.fileno()

        # LLM: 仅mutation打开时在首块后追加字节，模拟写入竞争。
        # 函数用途: 记录有界读取并制造可检测的读中变更。
        def read(self, size=-1):
            reads.append(size)
            data = self.file.read(size)
            if mutate and len(reads) == 1:
                with original(path, "ab") as writer:
                    writer.write(b"changed")
            return data

    original_source = scan._fingerprint_source
    monkeypatch.setattr(scan, "_fingerprint_source", lambda p: Reader() if p == path else original_source(p))
    mutate = False
    assert scan.file_fingerprint(path) == hashlib.sha256(b"x" * 150000).hexdigest()
    assert reads and set(reads) == {65536}
    reads.clear()
    mutate = True
    assert scan.file_fingerprint(path) == "unreadable"


# LLM: 内容hash监视器只观察真实入口，fresh目标不应触发读取。
# 函数用途: 验证年龄过滤先于摘要计算。
def test_fresh_daily_and_compact_are_not_hashed(tmp_path, monkeypatch):
    home, _, service = runtime(tmp_path)
    paths = {old_file(home.owner_memory_daily_dir / "fresh.jsonl"),
             old_file(home.owner_compact_dir / "fresh.json")}
    for path in paths:
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    original = scan.file_fingerprint
    observed = []

    # LLM: 保留原摘要算法，只记录目标hash访问。
    # 函数用途: 证明mtime过滤发生在读正文之前。
    def hashed(path):
        observed.append(path)
        return original(path)

    monkeypatch.setattr(scan, "file_fingerprint", hashed)
    assert service.plan(now=NOW).ok
    assert not paths.intersection(observed)


# LLM: 正式记忆走原仓库；未引用的过期快照仍须可回收，防止永久记忆规则全停假绿。
# 函数用途: 同时验证引用保护与30天回收宽限。
def test_formal_source_pin_and_trash_grace(tmp_path):
    home, _, service = runtime(tmp_path)
    pinned = old_file(home.owner_memory_daily_dir / "pinned.jsonl")
    unpinned = old_file(Path(home.owner_home_dir) / "memory_archive/snapshots/context_bundles/2020-01-01/expired.json", "{}")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "fact", attributes={"source_refs": [{"path": str(pinned)}]})
    report = service.apply(now=NOW)
    assert report.ok and pinned.exists() and not unpinned.exists()
    moved = next(a for a in report.actions if a.path == unpinned)
    assert moved.status == "trashed" and (moved.destination / "payload").exists()
    assert not [a for a in service.plan(now=NOW).actions if a.category == "trash"]


# LLM: 规划之后真实新增引用，执行不得只信旧pin集合。
# 函数用途: 验证执行前重核正式来源。
def test_apply_rechecks_new_formal_pins(tmp_path):
    home, candidates, service = runtime(tmp_path)
    path = _expired_pin_target(home, "new-formal-pin.jsonl")
    plan = service.plan(now=NOW)
    JsonlMemory(home.owner_memory_long_term_jsonl).add("user", "fact", attributes={"source_refs": [str(path)]})
    report = apply.execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert path.exists()
    assert next(a for a in report.actions if a.path == path).status == "not_started"
    assert report.scan_partial and path.exists()


# LLM: 各pin来源坏行都使相关回收关闭，不将解析器零条视为零引用。
# 函数用途: 验证损坏来源保留Daily原件并报告错误。
@pytest.mark.parametrize("store", ["formal", "candidate", "thread", "curator"])
def test_unreadable_pin_sources_fail_closed(tmp_path, store):
    home, _, service = runtime(tmp_path)
    path = old_file(home.owner_memory_daily_dir / "old.jsonl")
    sources = {"formal": home.owner_memory_long_term_jsonl,
        "candidate": home.owner_memory_candidates_jsonl,
        "thread": home.owner_home_dir / "workspace/runtime/workspaces/demo/conversations/threads/t.json",
        "curator": home.owner_memory_curator_runs_dir / "bad.jsonl"}
    old_file(sources[store], "{bad")
    plan = service.plan(now=NOW)
    assert not [a for a in plan.actions if a.path == path]
    assert plan.errors


# LLM: 旧龄夹具同步观察时间；原候选删除锁内CAS保持，仅观察借用入口与合法状态筛选。
# 函数用途: 防止执行端恢复全账本深复制。
def test_apply_candidate_phase_borrows_filtered_readonly_rows(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    home, candidates, service = runtime(tmp_path)
    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="old",
        subject_key="test.old", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson"))
    candidates.path.write_text(json.dumps(replace(row, updated_at="2020-01-01T00:00:00+00:00",
        last_observed_at="2020-01-01T00:00:00+00:00").to_record()) + "\n")
    plan = service.plan(now=NOW)
    original = candidates.readonly_rows
    calls = []

    # LLM: 仅监视真实只读借用，不替代候选删除锁内CAS。
    # 函数用途: 记录执行阶段使用的状态筛选。
    def borrowed(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(candidates, "readonly_rows", borrowed)
    results, errors = apply._apply_candidate_phase(candidates, plan, now=NOW)
    assert not errors and results[0].status == "deleted"
    assert calls == [{"statuses": apply.RETENTION_ELIGIBLE_CANDIDATE_STATUSES}]


# LLM: 逐个来源构造路径或事件ID引用，不能仅用形式错误作为保护证据。
# 函数用途: 验证冷详情、Curator、任务、线程及检查点引用覆盖。
@pytest.mark.parametrize("source", ["candidate", "curator", "task", "thread", "checkpoint"])
def test_reference_pin_sources_cover_ids_and_paths(tmp_path, source):
    home, candidates, service = runtime(tmp_path)
    path = old_file(home.owner_memory_daily_dir / "daily.jsonl", '{"event_id":"daily-id"}\n')
    ref = {"source_refs": [{"event_id": "daily-id"}]}
    root = home.owner_workspace_dir / "runtime/workspaces/demo/conversations"
    if source == "candidate":
        from agent_py_agent.agent.memory_store.candidate_models import (
            CandidateObservation,
            MemoryScope,
        )

        row = candidates.observe(CandidateObservation(candidate_type="lesson", content="cold detail",
            subject_key="test.cold", scope=MemoryScope("personal", "personal"), origin="model_inferred",
            promotion_target="lesson", evidence_refs=[{"path": str(path)}]))
        assert row.evidence_refs
        report = service.plan(now=NOW)
        assert report.ok, report.errors
        errors = []
        pins, complete = _complete_pins(home, (), errors, now=NOW)
        assert complete and pins.protects(path, errors)
    else:
        if source == "curator":
            old_file(home.owner_memory_curator_runs_dir / "run.jsonl", json.dumps({"base_checkpoint_ref": ref}) + "\n")
        elif source == "task":
            old_file(home.owner_runs_dir / "2026-01-01/t/work/state.json",
                json.dumps({"task_id": "t", "status": "RUNNING", **ref}))
        else:
            thread = {"thread_id": "t", "status": "active", "updated_at": NOW.timestamp(), **ref}
            if source == "checkpoint":
                thread.pop("source_refs")
                thread.update(compact_checkpoint_id="cp", compact_generation=1)
                old_file(home.owner_compact_dir / "conversations/t.jsonl", json.dumps({"checkpoint_id": "cp", **ref}) + "\n")
            old_file(root / "threads/t.json", json.dumps(thread))
        report = service.plan(now=NOW)
        assert report.ok, report.errors
    assert not [a for a in report.actions if a.path == path]


# LLM: 原删除墓碑物化仍生效，不因旧版本引用永久保留已无效来源。
# 函数用途: 验证正式记忆删除后可释放pin。
def test_deleted_formal_memory_releases_pin(tmp_path):
    home, _, service = runtime(tmp_path)
    path = _expired_pin_target(home, "released-pin.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    row = memory.add("user", "fact", attributes={"source_refs": [str(path)]})
    assert not [a for a in service.plan(now=NOW).actions if a.path == path]
    memory.remove(row.entry_id, expected_version=row.version)
    assert [a for a in service.plan(now=NOW).actions if a.path == path]


# LLM: 仅固定目标目录stat模拟ext4同tick；文件字节和遍历仍真实，新增/删除必须失效、乱序文件名仍命中。
# 函数用途: 验证展示档案热扫描减少stat且不漏掉同一时钟tick内的文件增删。
def test_display_archive_observation_cache_is_bounded_and_invalidated(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota as quota

    root = tmp_path / "owner"
    path = old_file(root / "memory_archive/display_archives/2026-01-01/a.txt", "1234")
    original, calls = Path.stat, []
    directory_info = original(path.parent, follow_symlinks=False)

    # LLM: 观察真实stat调用；不替换字节数或准入结果。
    # 函数用途: 统计热目录是否重复逐文件stat。
    def tracked(self, *args, **kwargs):
        if self == path.parent:
            return directory_info
        if self == path:
            calls.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", tracked)
    assert quota.owner_logical_usage_bytes(root) == 4
    calls.clear()
    assert quota.owner_logical_usage_bytes(root) == 4
    assert not calls
    extra = old_file(path.parent / "b.txt", "567")
    info = path.parent.stat(follow_symlinks=False)
    assert (info.st_mtime_ns, info.st_ctime_ns) == (directory_info.st_mtime_ns, directory_info.st_ctime_ns)
    assert quota.owner_logical_usage_bytes(root) == 7
    assert calls
    calls.clear()
    assert quota._directory_usage_bytes(root, path.parent, ["b.txt", "a.txt"], cached=True) == 7
    assert not calls
    extra.unlink()
    assert quota.owner_logical_usage_bytes(root) == 4
    assert calls


# LLM: 原地扩写不改目录mtime，权威准入仍须准确拒绝增长、允许净缩减。
# 函数用途: 验证展示TTL不能绕过owner配额。
def test_quota_admission_never_uses_stale_display_archive_cache(tmp_path):
    from agent_py_agent.agent.user_space import owner_quota as quota

    root = tmp_path / "owner"
    path = old_file(root / "display_archives/a.txt", "1")
    assert quota.owner_logical_usage_bytes(root) == 1
    # 原地扩写不改变父目录mtime，权威配额仍必须准确拒绝。
    path.write_text("x" * 20)
    enforcer = quota.OwnerQuotaEnforcer(root, max_bytes=10)
    with pytest.raises(quota.OwnerQuotaExceeded):
        with enforcer.reserve([quota.OwnerQuotaChange(root / "new", 1)]):
            pass
    with enforcer.reserve([quota.OwnerQuotaChange(path, 5)]) as projection:
        assert projection.used_bytes == 20 and projection.projected_bytes == 5


# LLM: 统计根内所有普通文件，不因移入trash/cold降低计费。
# 函数用途: 验证回收及冷归档仍占用配额。
def test_quota_counts_trash_and_cold_archives(tmp_path):
    from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes

    old_file(tmp_path / "trash/retention/daily/a/payload", "1234")
    old_file(tmp_path / "memory/candidates_archive/2026-01.jsonl", "567")
    assert owner_logical_usage_bytes(tmp_path) == 7


# LLM: pin源坏只阻断依赖引用的类别，保留既有R4独立子树隔离。
# 函数用途: 验证独立cache动作不会被误填owner根错误吞掉。
def test_pin_source_failure_does_not_block_unrelated_cache_cleanup(tmp_path):
    home, _, service = runtime(tmp_path)
    cached = old_file(home.owner_cache_dir / "old")
    old_file(home.owner_memory_long_term_jsonl, "{bad")
    report = service.apply(now=NOW)
    assert not cached.exists()
    assert report.errors


# LLM: 活跃head已存在而账本缺失不是可选空源。
# 函数用途: 验证恢复材料不完整时不清理Compact。
def test_checkpoint_pointer_to_missing_ledger_fails_closed(tmp_path):
    home, _, service = runtime(tmp_path)
    target = old_file(home.owner_compact_dir / "expired.json")
    root = home.owner_workspace_dir / "runtime/workspaces/demo/conversations"
    old_file(root / "threads/t.json", json.dumps({"thread_id": "t", "status": "active",
        "updated_at": NOW.timestamp(), "compact_generation": 1, "compact_checkpoint_id": "cp"}))
    report = service.plan(now=NOW)
    assert report.errors and not [a for a in report.actions if a.path == target]


# LLM: 源在计划后损坏时必须留错误和原文件，不借旧pin集合执行。
# 函数用途: 验证执行期来源读取故障安全关闭。
def test_apply_pin_source_read_error_preserves_old_file(tmp_path):
    home, candidates, service = runtime(tmp_path)
    path = _expired_pin_target(home, "unreadable-pin.jsonl")
    plan = service.plan(now=NOW)
    old_file(home.owner_memory_long_term_jsonl, "{bad")
    report = apply.execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert path.exists() and report.scan_partial
    assert next(a for a in report.actions if a.path == path).status == "not_started"
    errors = []
    _, complete = _complete_pins(home, (), errors, now=NOW)
    assert not complete and errors


# LLM: 目录发现EACCES不能被glob静默洗成无会话。
# 函数用途: 验证来源发现阶段也遵守失败关闭。
def test_unreadable_conversation_discovery_is_not_zero_pins(tmp_path, monkeypatch):
    home, _, service = runtime(tmp_path)
    path = old_file(home.owner_memory_daily_dir / "old.jsonl")
    root = home.owner_workspace_dir / "runtime/workspaces"
    root.mkdir(parents=True, exist_ok=True)
    original = scan._PinProjection._directory_members
    blocked_calls = []
    def blocked(projection, target, stamp):
        if target == root:
            blocked_calls.append(target)
            raise PermissionError(errno.EACCES, "denied", str(root))
        yield from original(projection, target, stamp)
    monkeypatch.setattr(scan._PinProjection, "_directory_members", blocked)
    report = service.plan(now=NOW)
    assert blocked_calls == [root]
    assert report.errors and report.errors[0].path == str(root)
    assert not [a for a in report.actions if a.path == path] and path.exists()


# LLM: 旧计划允许purge后再新增原路径/ID引用，执行仍保护实际payload。
# 函数用途: 验证回收宽限后的永久删除也重核pins。
@pytest.mark.parametrize("kind", ["path", "id", "unreadable"])
def test_trash_purge_rechecks_original_source_pins(tmp_path, kind):
    from datetime import timedelta

    home, candidates, service = runtime(tmp_path)
    container = _expired_pin_target(home, "old-source.jsonl", '{"event_id":"daily-id"}\n')
    source = home.owner_cache_dir / "old-source.jsonl"
    later = NOW + timedelta(days=31)
    if kind == "unreadable":
        old_file(home.owner_memory_long_term_jsonl, "{bad")
        plan = service.plan(now=later)
        assert not [a for a in plan.actions if a.path == container]
    else:
        # 先生成允许purge的旧计划，再新增引用，执行仍须重新保护回收内容。
        plan = service.plan(now=later)
        assert [a for a in plan.actions if a.path == container]
        ref = str(source) if kind == "path" else {"event_id": "daily-id"}
        JsonlMemory(home.owner_memory_long_term_jsonl).add("user", "fact", attributes={"source_refs": [ref]})
        report = apply.execute_retention_plan(home=home, candidates=candidates, plan=plan, now=later)
        assert next(a for a in report.actions if a.path == container).status == "not_started"
        assert report.scan_partial and (container / "payload").exists()
    assert (container / "payload").exists()


# LLM: 递归目录动作与任一子文件pin相交即须保留。
# 函数用途: 防止目录清理连带删除已引用文件。
def test_pin_of_descendant_protects_whole_directory(tmp_path):
    errors = []
    pins = scan._RetentionPins(tmp_path)
    path = old_file(tmp_path / "directory/file.json")
    pins.paths.add(path)
    assert pins.protects(path.parent, errors)


# LLM: 非普通文件不能通过阻塞式Path.open挂住维护入口。
# 函数用途: 验证FIFO指纹读取安全拒绝。
def test_non_regular_fingerprint_does_not_open_fifo(tmp_path, monkeypatch):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    original = Path.open

    # LLM: 避免旧实现实际阻塞；若试图阻塞式打开FIFO就明确失败。
    # 函数用途: 验证非普通文件在读取前被拒绝。
    def blocked(self, *args, **kwargs):
        assert self != path, "FIFO must not be opened with blocking Path.open"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", blocked)
    assert scan.file_fingerprint(path) == "unreadable"


# LLM: 逐行返回且尾部坏行仍传播，不能为省内存吞掉读取完整性错误。
# 函数用途: 验证pin来源不会重新物化整本候选JSONL。
def test_pin_sources_are_streamed_without_whole_ledger_materialization(tmp_path):
    path = old_file(tmp_path / "candidates.jsonl", '{"source_refs":["first"]}\n{}\n')
    rows = scan._pin_rows(path)
    assert iter(rows) is rows, "pin JSONL must be streamed, not loaded as a whole list"
    assert next(rows) == {"source_refs": ["first"]}
    assert list(rows) == [{}]
    broken = old_file(tmp_path / "broken.jsonl", '{"source_refs":["first"]}\n{bad\n')
    rows = scan._pin_rows(broken)
    assert next(rows) == {"source_refs": ["first"]}
    with pytest.raises(ValueError):
        next(rows)


# LLM: 注入实际EACCES而非仅坏JSON，来源不可读取时必须关闭相关清理。
# 函数用途: 验证pin存储权限错误不是零引用。
@pytest.mark.parametrize("kind", ["formal", "candidate", "curator"])
def test_permission_failure_in_pin_store_is_not_zero_pins(tmp_path, monkeypatch, kind):
    home, _, service = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "old.jsonl")
    sources = {"formal": home.owner_memory_long_term_jsonl,
        "candidate": home.owner_memory_candidates_jsonl,
        "curator": home.owner_memory_curator_runs_dir / "run.jsonl"}
    old_file(sources[kind], "{}\n")
    block_open(monkeypatch, sources[kind], PermissionError(errno.EACCES, "denied"))
    report = service.plan(now=NOW)
    assert report.errors and not [a for a in report.actions if a.path == target]


# LLM: 控制单调时钟和LRU容量，验证准确入口不依赖展示缓存。
# 函数用途: 验证缓存有界、过期和exact模式。
def test_display_usage_cache_ttl_eviction_and_exact_mode(tmp_path, monkeypatch):
    from collections import OrderedDict

    from agent_py_agent.agent.user_space import owner_quota as quota

    monkeypatch.setattr(quota, "_DISPLAY_USAGE_CACHE", OrderedDict())
    monkeypatch.setattr(quota, "_DISPLAY_USAGE_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(quota.cache_capacity, "_SLOTS", {**quota.cache_capacity._SLOTS, "owners": 0})
    monkeypatch.setattr(quota.time, "monotonic", lambda: 10.0)
    root = tmp_path / "owner"
    paths = [old_file(root / "display_archives" / str(index) / "file", "x") for index in range(3)]
    assert quota.owner_logical_usage_bytes(root) == 3
    assert len(quota._DISPLAY_USAGE_CACHE) <= 2
    paths[-1].write_text("xxxx")
    assert quota.owner_logical_usage_bytes(root, exact=True) == 6
    monkeypatch.setattr(quota.time, "monotonic", lambda: 16.0)
    assert quota.owner_logical_usage_bytes(root) == 6


# LLM: 关闭候选清理不等于关闭候选pin来源校验，坏schema不能被当成零引用。
# 函数用途: 验证未启用候选删除时仍保护不完整的来源集合。
def test_candidate_pin_schema_is_checked_when_candidate_cleanup_disabled(tmp_path):
    home, _, service = runtime(tmp_path)
    payload = json.loads(home.owner_retention_json.read_text())
    home.owner_retention_json.write_text(json.dumps({**payload, "rejected_candidate_days": 0}))
    target = old_file(home.owner_memory_daily_dir / "old.jsonl")
    old_file(home.owner_memory_candidates_jsonl, "{}\n")
    report = service.plan(now=NOW)
    assert report.errors and not [a for a in report.actions if a.path == target]


# LLM: stat阶段的EIO也要可见；is_file在新Python可能把OSError洗成False，不可据此静默跳过。
# 函数用途: 验证年龄预筛选故障留下诊断且不生成删除动作。
def test_stat_failure_during_age_filter_is_diagnosed(tmp_path, monkeypatch):
    home, _, service = runtime(tmp_path)
    target = old_file(home.owner_cache_dir / "old.jsonl")
    original = Path.stat

    # LLM: 仅目标元数据读取失败，不改变其他来源或删除接口。
    # 函数用途: 在mtime预筛阶段注入EIO。
    def failed(path, *args, **kwargs):
        if path == target:
            raise OSError(errno.EIO, "io", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failed)
    report = service.plan(now=NOW)
    assert any(e.path == str(target) for e in report.errors)
    assert not [a for a in report.actions if a.path == target]


# LLM: 17MiB稠密热文件单次source_stamp读取与hash均不得超过256KiB上限。
# 函数用途: 复现并验证超大稠密热来源的单次256KiB读取与摘要预算硬门。
def test_r4_dense_large_hot_pin_source_respects_256k_read_and_hash_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import reference_pins

    path = tmp_path / "dense-17m.jsonl"
    path.write_bytes(b"a" * (17 * 1024 * 1024))
    _freeze_pin_file_stat(monkeypatch, path)
    target_ino = path.stat().st_ino
    original_read, original_hash = os.read, hashlib.blake2b
    read_bytes, hash_bytes, max_chunk = [0], [0], [0]

    def counted_read(fd, size):
        raw = original_read(fd, size)
        if os.fstat(fd).st_ino == target_ino:
            read_bytes[0] += len(raw)
            max_chunk[0] = max(max_chunk[0], len(raw))
        return raw

    class CountedHash:
        def __init__(self, *args, **kwargs):
            self.inner = original_hash(*args, **kwargs)

        def update(self, data):
            hash_bytes[0] += len(data)
            return self.inner.update(data)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(os, "read", counted_read)
    monkeypatch.setattr(hashlib, "blake2b", CountedHash)
    first = reference_pins.source_stamp(path)
    first_read, first_hash = read_bytes[0], hash_bytes[0]
    assert 0 < first_read <= 256 * 1024
    assert 0 < first_hash <= 256 * 1024
    second = reference_pins.source_stamp(path)
    second_read = read_bytes[0] - first_read
    second_hash = hash_bytes[0] - first_hash
    assert 0 < second_read <= 256 * 1024
    assert 0 < second_hash <= 256 * 1024
    assert max_chunk[0] <= 64 * 1024
    assert first[:6] == second[:6]


# LLM: 超过256KiB的600KiB热源在400KiB预算外位置发生等长改写时，热复核与冷却后复核均必须检出。
# 函数用途: 证明不会只抽样前256KiB而漏掉中间400KiB偏移处的新增引用。
@pytest.mark.parametrize("settle_after_rewrite", [False, True])
def test_r4_beyond_256k_offset_equal_length_rewrite_detected_hot_and_settled(
    tmp_path, monkeypatch, settle_after_rewrite
):
    import time


    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    directory = home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads"
    # 将 source_refs 放在 400 KiB 偏移之后（超出首 256 KiB 切片窗口）。
    prefix = "p" * (400 * 1024)
    suffix = "q" * (180 * 1024)
    payload = (
        '{"thread_id":"t","updated_at":'
        + str(NOW.timestamp())
        + ',"summary":"'
        + prefix
        + '","source_refs":["'
        + str(control)
        + '"],"note":"'
        + suffix
        + '"}\n'
    )
    path = old_file(directory / "t.json", payload)
    assert path.stat().st_size > 580 * 1024
    assert payload.index("pin-a.jsonl") > 400 * 1024
    _freeze_pin_file_stat(monkeypatch, path)
    from agent_py_agent.agent.memory_store import reference_pins

    source_stamp = reference_pins._metadata_stamp(path.stat(follow_symlinks=False))
    settled = [False]
    # 只让本反例来源处于热窗口，其它已稳定发现根不消耗热组预算。
    monkeypatch.setattr(reference_pins, "_is_settled", lambda stamp: settled[0] or stamp != source_stamp)
    snapshot = _ProjectionSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    # 读前/读后共需六片，不能恢复旧的每watch四片预算来让这一轮直接通过。
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current(errors)
    assert snapshot.value is None and snapshot.pending is not None
    first, complete = _r5_finish_verification(snapshot, errors)
    assert complete and not errors and not first.protects(target, errors)
    rewritten = payload.replace("pin-a.jsonl", "pin-b.jsonl")
    assert len(rewritten) == len(payload)
    path.write_text(rewritten)
    if settle_after_rewrite:
        later = time.time_ns() + 3_000_000_000
        monkeypatch.setattr(time, "time_ns", lambda: later)
        settled[0] = True
    else:
        with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
            snapshot.current(errors)
        assert snapshot.value is None
    second, complete = _r5_finish_verification(snapshot, errors)
    assert complete and not errors and second is not first and second.protects(target, errors)


# LLM: 仅预算待续可失败关闭，必须在有限轮数内恢复；不会吞掉最终失败或把complete断言改成仅存在。
# 函数用途: 为新双阶段预算合同完成有界续验，逐轮确保未发布不完整快照。
def _r5_finish_verification(snapshot, errors):
    for _ in range(512):
        errors.clear()
        try:
            result = snapshot.current(errors)
        except OSError as exc:
            assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" in str(exc)
            assert snapshot.value is None
            assert not errors, errors
            continue
        assert result[1] and not errors
        return result
    pytest.fail("stable source did not finish bounded verification")


# LLM: 夹具只写临时线程；固定真实inode的粗时钟观察，并精确控制引用偏移与总字节数。
# 函数用途: 构造可复现中段等长改写的大热来源。
def _r5_hot_thread(tmp_path, monkeypatch, size_kib, offset_kib=300):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, service = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    prefix = '{"thread_id":"t","summary":"' + "p" * (offset_kib * 1024)
    prefix += '","source_refs":["' + str(control) + '"],"note":"'
    payload = prefix + "q" * (size_kib * 1024 - len(prefix.encode()) - 3) + '"}\n'
    directory = home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads"
    path = old_file(directory / "t.json", payload)
    assert path.stat().st_size == size_kib * 1024
    assert offset_kib * 1024 < payload.index("pin-a.jsonl") < (offset_kib + 1) * 1024
    fixed = path.stat()
    _freeze_pin_file_stat(monkeypatch, path)
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: fixed.st_ctime_ns)
    return home, service, path, target, payload


# LLM: 两个接缝都走真实解析、快照和apply；冻结stat不能让旧pins取得完整授权，稳定后须可恢复。
# 函数用途: 覆盖解析前/后中段改写，防止尾片捷径发布旧引用或误回收新证据。
@pytest.mark.parametrize("size_kib", [600, 1100, 2048])
@pytest.mark.parametrize("offset_kib", [300, 400])
@pytest.mark.parametrize("timing", ["before_parse", "during_parse", "after_parse"])
def test_r5_hot_large_source_mid_build_rewrite_never_publishes_stale_pins(
    tmp_path, monkeypatch, size_kib, offset_kib, timing
):
    from agent_py_agent.agent.memory_store import reference_pins
    home, service, path, target, payload = _r5_hot_thread(tmp_path, monkeypatch, size_kib, offset_kib)
    changed, parsed = [], []
    original_read, original_rows, original_source = scan._PinProjection._read, scan._PinProjection._rows, scan._fingerprint_source
    def rewrite():
        if not changed:
            path.write_text(payload.replace("pin-a.jsonl", "pin-b.jsonl"))
            changed.append(True)
    def read(projection, source):
        raw = yield from original_read(projection, source)
        if source == path and timing == "before_parse":
            rewrite()
        return raw
    def rows(projection, source, **kwargs):
        for row in original_rows(projection, source, **kwargs):
            if source == path and row is not None:
                parsed.append(True)
                if timing == "after_parse":
                    rewrite()
            yield row
    class Reader:
        def __init__(self, raw): self.raw = raw
        def __getattr__(self, name): return getattr(self.raw, name)
        def read1(self, size):
            raw = self.raw.read1(size)
            if timing == "during_parse" and self.raw.tell() > offset_kib * 1024:
                rewrite()
            return raw
    def opened(source):
        raw = original_source(source)
        return Reader(raw) if source == path else raw
    monkeypatch.setattr(scan._PinProjection, "_read", read)
    monkeypatch.setattr(scan._PinProjection, "_rows", rows)
    monkeypatch.setattr(scan, "_fingerprint_source", opened)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    for _ in range(8):
        try:
            pins, complete = snapshot.current([])
        except OSError:
            assert snapshot.value is None
        else:
            assert changed and complete and pins.protects(target, [])
            break
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: path.stat().st_ctime_ns+3_000_000_000)
    pins, complete = _r5_finish_verification(snapshot, [])
    assert changed and parsed and complete and pins.protects(target, [])
    report = service.apply(now=NOW)
    assert target.exists() and not [a for a in report.actions if a.path == target and a.status == "trashed"]


# LLM: OS摘要读取与C层解析读取分别计数；预验partial禁止打开解析器，解析流不占额外摘要预算。
# 函数用途: 验证每current统一1MiB摘要硬门，防止17MiB来源每次partial被重复解析。
@pytest.mark.parametrize("size_kib", [200, 600, 2048, 17 * 1024])
def test_r5_hot_source_current_read_bytes_strictly_bounded_to_one_mib(
    tmp_path, monkeypatch, size_kib
):
    home, _, path, _, _ = _r5_hot_thread(tmp_path, monkeypatch, size_kib, 100)
    original, total, chunks, parsed = scan._fingerprint_source, [0], [], []
    object_parser = scan._pin_object

    class Reader:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def read1(self, size):
            raw = self.raw.read1(size)
            total[0] += len(raw)
            chunks.append(len(raw))
            return raw

    def source(source_path):
        raw = original(source_path)
        return Reader(raw) if source_path == path else raw

    def parse(source_path):
        if source_path == path:
            parsed.append(path)
        return object_parser(source_path)

    monkeypatch.setattr(scan, "_fingerprint_source", source)
    monkeypatch.setattr(scan, "_pin_object", parse)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    for _ in range(4):
        total[0] = 0
        try:
            snapshot.current([])
        except OSError:
            assert snapshot.value is None
        assert 0 < total[0] <= 1_048_576
        assert snapshot.last_budget.size >= total[0]
    assert max(chunks) <= 64 * 1024
    if size_kib == 17 * 1024:
        assert not parsed


# LLM: 多来源进度可交错但不能让已完成post的来源每轮重开，导致全源永远无法同时完成；单轮读不完的热源先失败关闭。
# 函数用途: 验证多个大源的pending可收敛（热期单轮或冷却后跨轮），且续验不会重复解析。
@pytest.mark.parametrize("sizes", [(600, 600), (1100, 1100), (1100, 2048)])
def test_r5_multiple_hot_sources_pending_finishes_without_reparsing(tmp_path, monkeypatch, sizes):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, first, target, _ = _r5_hot_thread(tmp_path, monkeypatch, sizes[0])
    second = first.with_name("u.json")
    second.write_text(json.dumps({"thread_id": "u", "summary": "s" * (sizes[1] * 1024)}))
    _freeze_pin_file_stat(monkeypatch, second)
    original, parsed = scan._pin_object, []

    def parse(source):
        result = original(source)
        if source in (first, second):
            parsed.append(source)
        return result

    monkeypatch.setattr(scan, "_pin_object", parse)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    for _ in range(8):
        try:
            pins, complete = snapshot.current([])
        except OSError:
            assert snapshot.value is None
        else:
            assert complete and not pins.protects(target, [])
            break
    assert set(parsed) == {first, second}
    counts = {path: parsed.count(path) for path in (first, second)}
    assert set(counts.values()) == {1}
    settled = max(first.stat().st_ctime_ns, second.stat().st_ctime_ns) + 3_000_000_000
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: settled)
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and not pins.protects(target, [])
    assert all(parsed.count(path) == 1 for path in (first, second))


# LLM: 解析后的待验源滑出热窗口仍须把剩余切片验完，不能把冷元数据当完成证明或重读正文。
# 函数用途: 验证pending热转冷安全收敛。
def test_r5_pending_hot_source_settles_without_reparse(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, path, target, _ = _r5_hot_thread(tmp_path, monkeypatch, 2048)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    for _ in range(8):
        with pytest.raises(OSError):
            snapshot.current([])
        if snapshot.pending is not None:
            break
    assert snapshot.pending is not None and snapshot.value is None
    original = scan._pin_object

    def parse(source):
        assert source != path, "settling pending source was reparsed"
        return original(source)

    monkeypatch.setattr(scan, "_pin_object", parse)
    settled = path.stat().st_ctime_ns + 3_000_000_000
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: settled)
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and not pins.protects(target, [])
    assert snapshot.sources[path][1] == hashlib.sha256(path.read_bytes()).digest()


# LLM: 全局LRU状态可被回收并被另一snapshot重建，pending不能仅凭重置为零的generation接受另一份解析基线。
# 函数用途: 验证切片状态替换不会使旧待发布pins嫁接到新内容。
def test_r5_pending_does_not_accept_replaced_digest_state(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, path, target, payload = _r5_hot_thread(tmp_path, monkeypatch, 600)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    with pytest.raises(OSError):
        snapshot.current([])
    old = snapshot.projection
    assert old.pending is not None
    with scan._PIN_BUILDS_LOCK:
        assert scan._CURRENT_PIN_BUILDS.pop(Path(home.owner_home_dir)) is old
    old.close()
    path.write_text(payload.replace("pin-a.jsonl", "pin-b.jsonl"))
    settled = path.stat().st_ctime_ns + 3_000_000_000
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: settled)
    other = _ProjectionSnapshot(home, (), NOW)
    other_pins, complete = _r5_finish_verification(other, [])
    assert complete and other_pins.protects(target, [])
    pins, complete = _r5_finish_verification(snapshot, [])
    assert snapshot.projection is not old and old.closed
    assert complete and pins.protects(target, [])


# LLM: 超过单轮1MiB快照预算的热源热期必须一直失败关闭；冷却后冷期post可续完成，并捕获热期1.2MiB处改写。
# 函数用途: 验证超1MiB热源的分轮失败关闭可续验证与超远偏移等长改写检测。
@pytest.mark.parametrize("rewrite", [False, True])
def test_r4_oversized_hot_pin_source_fails_closed_until_resumable_slices_complete(tmp_path, monkeypatch, rewrite):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    directory = home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads"
    prefix = "p" * (1200 * 1024)
    suffix = "q" * (300 * 1024)
    payload = (
        '{"thread_id":"t","updated_at":'
        + str(NOW.timestamp())
        + ',"summary":"'
        + prefix
        + '","source_refs":["'
        + str(control)
        + '"],"note":"'
        + suffix
        + '"}\n'
    )
    path = old_file(directory / "t.json", payload)
    assert path.stat().st_size > 1500 * 1024
    assert payload.index("pin-a.jsonl") > 1200 * 1024
    _freeze_pin_file_stat(monkeypatch, path)
    hot = path.stat().st_ctime_ns
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: hot)
    snapshot = _ProjectionSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    # 每轮最多核 4 片（<= 1 MiB），热期读后验证只认本轮读到的片，读不完全部切片就一直失败关闭。
    for _ in range(8):
        with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
            snapshot.current(errors)
    assert snapshot.pending is not None and snapshot.value is None
    if rewrite:
        # 热期在 1.2 MiB 偏移处做stat不可见的等长原地改写，热期仍不得放行。
        rewritten = payload.replace("pin-a.jsonl", "pin-b.jsonl")
        assert len(rewritten) == len(payload)
        path.write_text(rewritten)
        for _ in range(4):
            with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
                snapshot.current(errors)
    # 冷却后冷期post从头跨轮核完全部切片才发布；热期改写必被冷期全片比对检出并重建。
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: hot + 3_000_000_000)
    errors = []
    pins, complete = _r5_finish_verification(snapshot, errors)
    assert complete and not errors and pins.protects(target, errors) is rewrite


# LLM: 粗时钟下读后post已核过某片、整轮未完时再等长改写该片；热期不得用跨current旧片发布旧pins，单轮读不完的源冷却后才发布且须保护新引用。
# 函数用途: 回归R5-F01单源读后窗口，覆盖热期单轮可完成(600KiB)与须冷却(2MiB)两种来源。
@pytest.mark.parametrize("size_kib", [600, 2048])
def test_r6_rewrite_of_post_verified_slice_never_publishes_stale_pins(tmp_path, monkeypatch, size_kib):
    from agent_py_agent.agent.memory_store import reference_pins
    home,_,path,target,payload=_r5_hot_thread(tmp_path,monkeypatch,size_kib,100)
    original,fired=scan._PinProjection._chunks,[]
    def chunks(projection,source,**kwargs):
        for raw in original(projection,source,**kwargs):
            yield raw
            if source==path and kwargs.get("verifying") and raw and not fired:
                path.write_text(payload.replace("pin-a.jsonl","pin-b.jsonl"))
                fired.append(True)
    monkeypatch.setattr(scan._PinProjection,"_chunks",chunks)
    snapshot=_ProjectionSnapshot(home,(),NOW)
    for _ in range(8):
        try:
            pins,complete=snapshot.current([])
        except OSError:
            assert snapshot.value is None
        else:
            assert fired and complete and pins.protects(target,[])
            break
    assert fired
    settled=path.stat().st_ctime_ns+3_000_000_000
    monkeypatch.setattr(reference_pins.time,"time_ns",lambda:settled)
    pins,complete=_r5_finish_verification(snapshot,[])
    assert complete and pins.protects(target,[])


# LLM: t为600KiB引用来源（pin-a位于100KiB处）；u_kib为None时u是原输入的2MiB无引用来源，否则是与t同布局、恰好u_kib大小的引用来源。
#   两源身份、大小与内容不随访问顺序改变；测试只重排同一原生页内这两个线程成员（任一顺序都是合法readdir结果），生产保持真实目录顺序自由。
# 函数用途: 构造双源热组并按order固定成员访问顺序，返回记录到的目录页供用例断言。
def _dual_source_threads(tmp_path, monkeypatch, order, u_kib=None):
    from agent_py_agent.agent.user_space import directory_paging

    home,_,t,target,payload=_r5_hot_thread(tmp_path,monkeypatch,600,100)
    u=t.with_name("u.json")
    if u_kib is None:
        u.write_text(json.dumps({"thread_id":"u","summary":"s"*(2048*1024)}))
    else:
        u.write_text(payload.replace('"thread_id":"t"','"thread_id":"u"').replace('"note":"','"note":"'+"s"*((u_kib-600)*1024)))
        assert u.stat().st_size==u_kib*1024
    _freeze_pin_file_stat(monkeypatch,u)
    first,second=(t,u) if order==("t","u") else (u,t)
    original_page, pages = directory_paging.directory_page, []

    # LLM: 续点、成员身份与其余成员不变，只给两个线程成员定序。
    # 函数用途: 包装原生目录页以固定两个来源的访问顺序。
    def page(root, parts, cursor, limit, *, require_dir_fd=True):
        names, after, complete = original_page(root, parts, cursor, limit, require_dir_fd=require_dir_fd)
        if Path(root, *parts) == t.parent and {t.name, u.name} <= set(names):
            names = [first.name, second.name, *(name for name in names if name not in {t.name, u.name})]
            pages.append(tuple(names))
        return names, after, complete

    monkeypatch.setattr(directory_paging, "directory_page", page)
    return home,target,payload,first,second,pages


# LLM: 先访问的来源已在本轮完成post、另一来源仍pending时等长改写先访问的来源；后续发布不得复用其跨current的旧完成结果。
#   事件证明改写点：先访问来源的post作用域是本轮令牌而另一来源不是；热期若发布须已保护新引用，冷却后发布须保护新引用。
# 函数用途: 在指定成员访问顺序下回归R5-F01多源post完成复用，发布结果必须保护改写后的新引用。
def _completed_post_reuse_case(tmp_path, monkeypatch, *, order, u_kib=None):
    import threading

    from agent_py_agent.agent.memory_store import reference_pins

    home,target,_,first,second,pages=_dual_source_threads(tmp_path,monkeypatch,order,u_kib)
    rewritten=first.read_text().replace("pin-a.jsonl","pin-b.jsonl")
    assert "pin-b.jsonl" in rewritten and len(rewritten.encode())==first.stat().st_size
    original,fired=scan._PinProjection._chunks,[]
    first_verified, second_pending = threading.Event(), threading.Event()

    # LLM: 本轮令牌在进入时读取；完成判定在原生成器结束后重读令牌，跨轮续读的post不算本轮完成。
    # 函数用途: 观察post核验次序，并在先访问来源本轮已完成、首次进入另一来源post时注入一次等长改写。
    def chunks(projection,source,**kwargs):
        verifying,token=kwargs.get("verifying"),projection.contents["pass_token"]
        if (verifying and source==second and not fired and first_verified.is_set()
                and projection.post_scopes.get(first) is token):
            if projection.post_scopes.get(second) is not token:
                second_pending.set()
            first.write_text(rewritten)
            fired.append(True)
        yield from original(projection,source,**kwargs)
        if verifying and source==first and projection.post_scopes.get(first) is projection.contents["pass_token"]:
            first_verified.set()
    monkeypatch.setattr(scan._PinProjection,"_chunks",chunks)
    snapshot=_ProjectionSnapshot(home,(),NOW)
    for _ in range(12):
        try:
            pins,complete=snapshot.current([])
        except OSError:
            assert snapshot.value is None
        else:
            assert fired and complete and pins.protects(target,[]),"stale hot post was reused across passes"
            break
    assert fired
    assert first_verified.is_set() and second_pending.is_set()
    assert pages and all(names[:2] == (first.name, second.name) for names in pages)
    settled=max(first.stat().st_ctime_ns,second.stat().st_ctime_ns)+3_000_000_000
    monkeypatch.setattr(reference_pins.time,"time_ns",lambda:settled)
    pins,complete=_r5_finish_verification(snapshot,[])
    assert complete and pins.protects(target,[])


# LLM: 原输入：600KiB的t先于2MiB的u；t本轮完成post、u仍pending时改写t。
# 函数用途: 回归R5-F01多源post完成复用，发布结果必须保护改写后的新引用。
def test_r6_completed_post_of_one_source_is_not_reused_while_another_is_pending(tmp_path, monkeypatch):
    _completed_post_reuse_case(tmp_path, monkeypatch, order=("t", "u"))


# LLM: 颠倒成员访问顺序且来源身份、大小与内容不变：2MiB的u先于600KiB的t（R2在Linux 3.12看到的目录次序）。
#   单轮1MiB热期预算装不下u，热组只能部分覆盖：每轮都失败关闭，事件证明u先于t进入post，且u在热期从不完成post；
#   u待验时等长改写t，冷却后发布须保护新引用。
# 函数用途: 证明较长来源先被访问时多源热组保持部分覆盖合同直至冷却。
def test_r6_completed_post_reuse_in_reversed_member_access_order(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.memory_store import reference_pins

    home,target,payload,first,second,pages=_dual_source_threads(tmp_path,monkeypatch,("u","t"))
    original,fired,long_completed=scan._PinProjection._chunks,[],[]
    long_first=threading.Event()

    # LLM: 较长来源首次进入post时较短来源须尚未核验；此时注入一次等长改写，并记录较长来源在热期完成post的轮次。
    # 函数用途: 观察热期post核验次序并在较长来源待验时改写较短来源。
    def chunks(projection,source,**kwargs):
        verifying=kwargs.get("verifying")
        if verifying and source==first and not fired:
            if second not in projection.post_scopes:
                long_first.set()
            second.write_text(payload.replace("pin-a.jsonl","pin-b.jsonl"))
            fired.append(True)
        yield from original(projection,source,**kwargs)
        if verifying and source==first:
            long_completed.append(projection.contents["pass_token"])
    monkeypatch.setattr(scan._PinProjection,"_chunks",chunks)
    snapshot=_ProjectionSnapshot(home,(),NOW)
    for _ in range(12):
        with pytest.raises(OSError,match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
            snapshot.current([])
        assert snapshot.value is None
    assert fired and long_first.is_set() and not long_completed
    assert pages and all(names[:2] == (first.name, second.name) for names in pages)
    settled=max(first.stat().st_ctime_ns,second.stat().st_ctime_ns)+3_000_000_000
    monkeypatch.setattr(reference_pins.time,"time_ns",lambda:settled)
    pins,complete=_r5_finish_verification(snapshot,[])
    assert complete and pins.protects(target,[])


# LLM: 两源单独都能在一轮1MiB预算内完成post（600KiB与700KiB）、合计不能；两源都引用pin-a，身份、大小与内容固定，只颠倒访问顺序。
#   两种顺序都确定进入“先访问来源本轮已完成、另一来源pending”的交错；另一来源下一轮即可完成，热期发布判定因此直接受检。
# 函数用途: 证明多源post复用防线与成员访问顺序无关。
@pytest.mark.parametrize("order", [("t", "u"), ("u", "t")], ids=["t-then-u", "u-then-t"])
def test_r6_completed_post_reuse_independent_of_member_access_order(tmp_path, monkeypatch, order):
    _completed_post_reuse_case(tmp_path, monkeypatch, order=order, u_kib=700)


# LLM: 正式记忆旧格式路径对同一<=256KiB热源有显式watch、_pin_rows读前读后、显式读后与最终复核；外加重试也不得每current超过1MiB摘要读取。
# 函数用途: 回归R5-F06小文件分支绕过共享预算，同时确认读后复核仍实际读取且结果完整。
@pytest.mark.parametrize("racing", [False, True])
def test_r6_small_hot_formal_source_shares_one_mib_digest_budget(tmp_path, monkeypatch, racing):
    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "fact", attributes={"note": "n" * 250000})
    original, total = scan._fingerprint_source, [0]
    racer = old_file(home.owner_memory_curator_runs_dir / "racing.jsonl") if racing else None

    class Reader:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def read1(self, size):
            raw = self.raw.read1(size)
            total[0] += len(raw)
            return raw

    def opened(source):
        raw = original(source)
        if source == racer:
            with racer.open("ab") as output:
                output.write(b"{}\n")
        return Reader(raw) if source == memory.path else raw

    monkeypatch.setattr(scan, "_fingerprint_source", opened)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    for _ in range(3):
        total[0] = 0
        try:
            pins, complete = snapshot.current([])
        except OSError:
            assert racing and snapshot.value is None
        else:
            assert not racing and complete and not pins.protects(target, [])
        assert memory.path.stat().st_size <= total[0] <= 1024 * 1024
        assert snapshot.last_budget.size >= total[0]


# LLM: 用真实schema删除记录填满600KiB永久墓碑，首条屏蔽仍引用目标的正式记忆；改写只把其entry_id等长换掉以解除屏蔽。
# 函数用途: 构造超出单次预算、须走可续切片的StorageTombstones来源及其同长改写。
def _r7_large_storage_tombstones(memory, target):
    from agent_py_agent.agent.memory_store.generation_manifest import (
        TOMBSTONE_SCHEMA,
        StorageTombstones,
    )

    memory.add("user", "fact", attributes={"source_refs": [str(target)]})
    record = json.loads(memory.path.read_text())
    entry_id = record["entry_id"]
    record.update(action="remove", content="", attributes=None)
    store = StorageTombstones(memory.path)
    head = json.dumps({"schema_version": TOMBSTONE_SCHEMA, "owner": store.owner}, separators=(",", ":"))[:-1]
    head += ',"records":{'
    rows = [json.dumps(entry_id) + ":" + json.dumps(record, separators=(",", ":"))]
    size = len(head) + len(rows[0]) + 2
    while True:
        dummy = f"old-deletion-{len(rows):020d}"
        row = json.dumps(dummy) + ":" + json.dumps({**record, "entry_id": dummy}, separators=(",", ":"))
        if size + 1 + len(row) > 600 * 1024:
            break
        rows.append(row)
        size += 1 + len(row)
    payload = head + ",".join(rows) + "}}"
    payload += " " * (600 * 1024 - len(payload))
    store.path.write_text(payload)
    return store.path, payload, payload.replace(entry_id, "x" * len(entry_id))


# LLM: 墓碑存储、生成分片与保留策略都不经_pin_rows流式摘要；前次快照成功或构建失败遗留、本轮读前又核满的post进度，
#   不能在解析后同tick等长改写时充当读后验证。改写前断言post确已核满，证明旧路径会走到陈旧完整发布。
# 函数用途: 回归R6-PIN-01，非_pin_rows解析入口在解析边界作废post进度，不发布与新内容不符的完整快照。
@pytest.mark.parametrize("prior", ["snapshot-success", "build-failure"])
@pytest.mark.parametrize("source", ["storage-tombstones", "segment", "policy"])
def test_r7_non_streaming_pin_parse_resets_post_progress(tmp_path, monkeypatch, source, prior):
    from agent_py_agent.agent.memory_store import reference_pins
    from agent_py_agent.agent.memory_store.generation_manifest import (
        MemoryGenerationStore,
        StorageTombstones,
    )

    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    if source == "storage-tombstones":
        path, payload, rewritten = _r7_large_storage_tombstones(memory, target)
        owner, name = StorageTombstones, "load"
    elif source == "segment":
        for index in range(3):
            memory.add(
                "user",
                f"fact {index}",
                attributes={"source_refs": [f"other-{index}"], "note": "n" * 200000},
            )
        memory.migrate_tiers()
        path = (
            memory.path.parent
            / MemoryGenerationStore(memory.path).manifest()["segments"][0]["path"]
        )
        payload = path.read_text()
        rewritten = payload.replace("other-0", "other-x")
        owner, name = MemoryGenerationStore, "segment_rows"
    else:
        path = Path(home.owner_retention_json)
        payload = path.read_text()
        payload += " " * (600 * 1024 - len(payload))
        path.write_text(payload)
        rewritten = payload.replace('"legal_hold": false', '"legal_hold": true ')
        owner, name = scan, "_load_policy"
    assert (
        path.stat().st_size > 256 * 1024 and len(rewritten) == len(payload) and rewritten != payload
    )
    _freeze_pin_file_stat(monkeypatch, path)
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: path.stat().st_ctime_ns)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    if prior == "build-failure":
        unrelated = old_file(home.owner_memory_curator_runs_dir / "z-broken.jsonl", "{bad}\n")
        for _ in range(128):
            errors = []
            try:
                snapshot.current(errors)
            except OSError:
                pass
            if errors:
                break
        assert errors and snapshot.value is None
        unrelated.write_text("{}\n")
    else:
        settled = path.stat().st_ctime_ns + 3_000_000_000
        monkeypatch.setattr(reference_pins.time, "time_ns", lambda: settled)
        assert _r5_finish_verification(snapshot, [])[1]
    # 退休投影，不能从旧全局摘要缓存嫁接先前post结果。
    resident = scan._CURRENT_PIN_BUILDS.pop(Path(home.owner_home_dir), None)
    if resident is not None:
        resident.close()
    original, fired = scan._PinProjection._rows, []
    original_read = scan._PinProjection._read

    def rewrite():
        if not fired:
            path.write_text(rewritten)
            fired.append(True)

    def rows(projection, target_path, **kwargs):
        for row in original(projection, target_path, **kwargs):
            if target_path == path and row is not None:
                rewrite()
            yield row

    def read(projection, target_path):
        raw = yield from original_read(projection, target_path)
        if source == "storage-tombstones" and target_path == path:
            rewrite()
        return raw

    monkeypatch.setattr(scan._PinProjection, "_rows", rows)
    monkeypatch.setattr(scan._PinProjection, "_read", read)
    if source == "policy":
        original_policy = scan._load_policy

        def load(target_path):
            result = original_policy(target_path)
            if target_path == path:
                rewrite()
            return result

        monkeypatch.setattr(scan, "_load_policy", load)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    for _ in range(128):
        errors = []
        try:
            pins, complete = snapshot.current(errors)
        except OSError:
            assert snapshot.value is None
            if fired:
                break
        else:
            assert fired and complete
            fresh, fresh_complete = _complete_pins(home, (), [], now=NOW)
            assert fresh_complete and pins.protects(target, []) == fresh.protects(target, [])
            break
    assert fired
    # 分片/墓碑/策略的改写都不能发布旧代；恢复源后能重建。
    path.write_text(payload)
    monkeypatch.setattr(
        reference_pins.time, "time_ns", lambda: path.stat().st_ctime_ns + 3_000_000_000
    )
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete


# LLM: 262144B正式记忆在同一current第四次摘要读取前被真实追加（不冻结stat，只固定墙钟保持热态）；
#   实际os.read字节不得超过每源1MiB且记账不少于实读，增长组拒绝发布，无增长组照常发布。
# 函数用途: 回归R6-PIN-02，小文件摘要的增长探测读取受剩余预算封顶并按实读记账。
@pytest.mark.parametrize("growth", [0, 32 * 1024, 64 * 1024])
def test_r7_small_source_growth_probe_never_exceeds_digest_budget(tmp_path, monkeypatch, growth):
    home, _, _ = runtime(tmp_path)
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "fact", attributes={"note": "n" * 250000})
    original, total, fired = scan._fingerprint_source, [0], []

    class Reader:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def read1(self, size):
            raw = self.raw.read1(size)
            total[0] += len(raw)
            if growth and not fired:
                with memory.path.open("ab") as output:
                    output.write(b" " * growth)
                fired.append(True)
            return raw

    def opened(source):
        raw = original(source)
        return Reader(raw) if source == memory.path else raw

    monkeypatch.setattr(scan, "_fingerprint_source", opened)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    if growth:
        with pytest.raises(OSError):
            snapshot.current([])
        assert snapshot.value is None
    else:
        assert snapshot.current([])[1]
    assert bool(fired) is bool(growth) and 0 < total[0] <= 1024 * 1024
    assert snapshot.last_budget.size >= total[0]


# LLM: 解析器实际读取期间来源是等长瞬时内容B、返回前即恢复A，且冻结该inode的stat；读后复读与发布前复核只见A，须由解析字节绑定识破。
#   ABA所在current必须失败关闭，之后无改写的current在有限轮内恢复发布并保护目标；策略A为法律保留，任何轮都不得发布。
# 函数用途: 回归R7-PIN-ABA，永久墓碑、旧墓碑、根清单与保留策略的非流式解析均绑定实际解析字节（含>256KiB切片来源）。
@pytest.mark.parametrize(
    ("source", "size_kib"),
    [
        ("storage-tombstones", 64),
        ("storage-tombstones", 600),
        ("legacy-tombstones", None),
        ("manifest", None),
        ("policy", 64),
        ("policy", 600),
    ],
)
def test_r8_non_streaming_parse_aba_never_publishes_transient_bytes(
    tmp_path, monkeypatch, source, size_kib
):
    from agent_py_agent.agent.memory_store import reference_pins
    from agent_py_agent.agent.memory_store.generation_manifest import (
        MemoryGenerationStore,
        StorageTombstones,
    )
    from agent_py_agent.agent.memory_store.jsonl import _tombstone_path

    reference_pins._RESUMABLE_DIGESTS.clear()
    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "still-referenced.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    owner, name = StorageTombstones, "load"
    if source == "storage-tombstones":
        path, transient, payload = _r7_large_storage_tombstones(memory, target)
        if size_kib == 64:
            document = json.loads(transient)
            first = next(iter(document["records"]))
            document["records"] = {first: document["records"][first]}
            transient = json.dumps(document, separators=(",", ":"))
            transient += " " * (64 * 1024 - len(transient))
            payload = transient.replace(first, "x" * len(first))
    elif source == "legacy-tombstones":
        memory.add("user", "fact", attributes={"source_refs": [str(target)]})
        record = json.loads(memory.path.read_text())
        record.update(action="remove", content="", attributes=None)
        path = _tombstone_path(memory.path)
        transient = json.dumps(record) + "\n"
        payload = " " * (len(transient) - 1) + "\n"
    elif source == "manifest":
        memory.add("user", "fact", attributes={"source_refs": [str(target)]})
        memory.migrate_tiers()
        path = MemoryGenerationStore(memory.path).manifest_path
        payload = path.read_text()
        document = json.loads(payload)
        assert document["segments"]
        document["segments"] = []
        transient = json.dumps(document)
        transient += " " * (len(payload) - len(transient))
        owner, name = MemoryGenerationStore, "manifest"
    else:
        path = Path(home.owner_retention_json)
        transient = path.read_text()
        transient += " " * (size_kib * 1024 - len(transient))
        payload = transient.replace('"legal_hold": false', '"legal_hold": true ')
        owner, name = scan, "_load_policy"
    assert len(transient) == len(payload) and transient != payload
    path.write_text(payload)
    fixed = path.stat()
    _freeze_pin_file_stat(monkeypatch, path)
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: fixed.st_ctime_ns)
    if source == "policy":
        original, fired = scan._load_policy, []

        def transient_parse(*args, **kwargs):
            if fired:
                return original(*args, **kwargs)
            path.write_text(transient)
            try:
                result = original(*args, **kwargs)
                fired.append(result)
                return result
            finally:
                path.write_text(payload)

        monkeypatch.setattr(scan, "_load_policy", transient_parse)
    else:
        fired = _transient_pin_source(monkeypatch, path, transient, payload)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    for _ in range(128):
        try:
            pins, complete = snapshot.current([])
        except OSError:
            assert snapshot.value is None
            if fired:
                break
        else:
            pytest.fail("transient source bytes were published")
    assert fired and path.read_text() == payload
    if source == "policy":
        assert fired[0][0].legal_hold is False
        for _ in range(4):
            with pytest.raises(OSError):
                snapshot.current([])
        return
    settled = fixed.st_ctime_ns + 3_000_000_000
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: settled)
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and pins.protects(target, [])


# LLM: <=256KiB线程来源经_pin_rows流式解析；解析流读到等长瞬时B、EOF或关闭时即恢复A，读后复读只见A。
# 函数用途: 回归同类ABA在小文件流式解析上的缺口，发布的pins只能来自读前已核内容A。
@pytest.mark.parametrize("coarse", [False, True], ids=["native-stat", "coarse-stat"])
def test_r8_small_streaming_pin_source_binds_parsed_bytes(tmp_path, monkeypatch, coarse):
    from agent_py_agent.agent.memory_store import reference_pins

    reference_pins._RESUMABLE_DIGESTS.clear()
    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    path = home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads/t.json"
    payload = json.dumps({"thread_id": "t", "source_refs": [str(target)], "summary": "s" * 1000})
    transient = payload.replace(str(target), str(control))
    old_file(path, payload)
    fixed = path.stat()
    if coarse:
        _freeze_pin_file_stat(monkeypatch, path)
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: fixed.st_ctime_ns)
    original, fired = scan._fingerprint_source, []

    # LLM: 只包装首个解析流，读到EOF、整读或关闭即恢复A，其余属性交原reader。
    # 类用途: 让解析器读到瞬时B而读后观察只见A。
    class RestoreAtEof:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, attribute):
            return getattr(self.raw, attribute)

        def read1(self, size=-1):
            data = self.raw.read1(size)
            if not data:
                path.write_text(payload)
            return data

        def read(self, size=-1):
            data = self.raw.read(size)
            path.write_text(payload)
            return data

        def close(self):
            self.raw.close()
            path.write_text(payload)

    def transient_source(source):
        if source != path or fired:
            return original(source)
        path.write_text(transient)
        fired.append(True)
        return RestoreAtEof(original(source))

    monkeypatch.setattr(scan, "_fingerprint_source", transient_source)
    snapshot = _ProjectionSnapshot(home, (), NOW, scan.current_retention_pins)
    pins, complete = _r5_finish_verification(snapshot, [])
    assert fired and path.read_text() == payload
    assert complete and pins.protects(target, []) and not pins.protects(control, [])


# LLM: 冷详情由candidates.py候选读取器解析（不属本轮文件），按热行引用的sha256内容寻址选版本并校验信封摘要；
#   解析期间换成等长篡改正文或重算摘要的另一版本只能失败关闭，之后恢复发布仍保护冷证据而非替换路径。
# 函数用途: 证明冷详情经内容寻址绑定，ABA无法发布与读前内容不符的pins。
@pytest.mark.parametrize("rehash", [False, True])
def test_r8_cold_detail_parse_aba_is_bound_by_content_address(tmp_path, monkeypatch, rehash):
    from agent_py_agent.agent.memory_store import reference_pins
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    reference_pins._RESUMABLE_DIGESTS.clear()
    home, candidates, _ = runtime(tmp_path)
    evidence = old_file(home.owner_memory_daily_dir / "cold-evidence-a.jsonl")
    control = old_file(home.owner_memory_daily_dir / "cold-evidence-b.jsonl")
    row = candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="已晋升的归档事实",
            subject_key="r8.cold",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="lesson",
            evidence_refs=[{"path": str(evidence)}],
        )
    )
    candidates.transition(row.candidate_id, "approved", reviewer="test")
    row = candidates.mark_promoted(
        row.candidate_id, reviewer="test", promotion_ref="memory/lessons/r8"
    )
    candidates.path.write_text(
        json.dumps(replace(row, updated_at="2020-01-01T00:00:00+00:00").to_record()) + "\n"
    )
    assert candidates.compact_terminal_candidates(now=NOW) == 1
    path = next((candidates.path.parent / "candidates_archive").glob("*.jsonl"))
    payload = path.read_text()
    transient = payload.replace(evidence.name, control.name)
    if rehash:
        envelope = json.loads(transient)
        material = {key: envelope[key] for key in ("owner_root", "candidate_id", "record")}
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        transient = transient.replace(envelope["sha256"], digest)
    assert len(transient) == len(payload) and transient != payload
    fixed = path.stat()
    _freeze_pin_file_stat(monkeypatch, path)
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: fixed.st_ctime_ns)
    fired = _transient_pin_source(monkeypatch, path, transient, payload)
    snapshot = _ProjectionSnapshot(home, (), NOW)
    for _ in range(128):
        try:
            pins, complete = snapshot.current([])
        except OSError:
            assert snapshot.value is None
            if fired:
                break
        else:
            pytest.fail("transient cold detail was published")
    assert fired and path.read_text() == payload
    settled = fixed.st_ctime_ns + 3_000_000_000
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: settled)
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and pins.protects(evidence, []) and not pins.protects(control, [])


@pytest.mark.parametrize("budgeted", [False, True])
def test_missing_candidate_index_reports_source_and_recovers_without_discarding_pins(tmp_path, budgeted):
    """真实索引缺叶须具体报错并保护引用；恢复原字节后新扫描自行恢复。"""
    from agent_py_agent.agent.memory_store.candidate_ledger import read_manifest
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

    home, candidates, service = runtime(tmp_path)
    target = _expired_pin_target(home, "referenced-cache.jsonl")
    cache = old_file(home.owner_cache_dir / "independent-cache.jsonl")
    candidates.observe(CandidateObservation(
        candidate_type="lesson", content="保留证据的候选", subject_key="missing-index", origin="model_inferred",
        scope=MemoryScope("personal", "personal"), evidence_refs=({"path": str(target)},),
    ))
    header = read_manifest(candidates.path)
    missing = candidates.path.parent / (candidates.path.name + ".records") / header["root"]["ref"]["ref"]
    original = missing.read_bytes()
    missing.unlink()

    def plan():
        budget = RetentionScanBudget(10000, 8 * 1024 * 1024, float("inf")) if budgeted else None
        return service.plan(now=NOW, scan_budget=budget)

    report = plan()
    pin_error = next(error for error in report.errors if error.error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
    assert pin_error.path == str(missing)
    assert "ImmutableIndexNodeMissing" in pin_error.message
    assert "immutable index node is missing" in pin_error.message
    assert "下一步" in pin_error.message and "其它动作结果见 actions" in pin_error.message
    assert not [action for action in report.actions if action.path == target]
    assert any(action.path == cache for action in report.actions)
    assert target.exists() and cache.exists()  # dry-run 未产生删除副作用。

    missing.write_bytes(original)
    recovered = plan()
    assert not recovered.errors
    assert not [action for action in recovered.actions if action.path == target]
    assert any(action.path == cache for action in recovered.actions)


def test_pin_source_error_retains_cause_path_and_redacts_secrets():
    secret = "sk-" + "a" * 48
    cause = FileNotFoundError(errno.ENOENT, f"missing source token={secret}", "/owner/memory/index/node.json")
    error = ValueError("candidate source wrapper")
    error.__cause__ = cause
    result = scan._pin_source_error(error)
    assert result.path == cause.filename
    assert "ValueError: candidate source wrapper" in result.message
    assert "FileNotFoundError" in result.message and secret not in result.message
    assert "恢复来源完整性后重跑 retention" in result.message


def test_hot_post_from_earlier_entry_slice_cannot_publish_frozen_stat_rewrite(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, path, target, payload = _r5_hot_thread(tmp_path, monkeypatch, 200, 100)
    snapshot = _ProjectionSnapshot(home, (), NOW, entries=8)
    fired = False
    for _ in range(256):
        projection = snapshot.projection
        if not fired and projection is not None and path in projection.post_scopes:
            path.write_text(payload.replace("pin-a.jsonl", "pin-b.jsonl"))
            fired = True
        try:
            pins, complete = snapshot.current([])
        except OSError:
            assert snapshot.value is None
        else:
            assert fired and complete and pins.protects(target, []), (
                "published old hot post from another entry slice"
            )
            break
    assert fired
    settled = path.stat().st_ctime_ns + 3_000_000_000
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: settled)
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and pins.protects(target, [])
