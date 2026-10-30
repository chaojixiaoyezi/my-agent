# LLM: 只在pytest临时home运行真实保留/配额链；故障注入不替换授权或删除CAS，禁止访问真实owner。
# 模块用途: 覆盖M5误删反例、引用与回收宽限保护、流式读取和展示缓存不绕过配额。
"""M5：坏指纹、读取竞态、引用保护及配额扫描的回归合同。"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
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


# LLM: 已晋升候选冷详情仍须保护其证据；冷源损坏时关闭相关清理，完好时仍清理无引用对照文件，不能全停冒充保护。
# 函数用途: 验证冷候选证据在真实引用扫描、规划及执行中的保护与坏源失败边界。
@pytest.mark.parametrize("damage", [False, True])
def test_r2_promoted_cold_details_pin_expired_evidence_or_fail_closed(tmp_path, damage):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    home, candidates, service = runtime(tmp_path)
    path = old_file(home.owner_memory_daily_dir / "cold-evidence.jsonl")
    control = old_file(home.owner_memory_daily_dir / "unreferenced.jsonl")
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
    pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
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
        assert [item for item in plan.actions if item.path == control]
    report = service.apply(now=NOW)
    assert path.exists() and not [item for item in report.actions if item.path == path and item.status == "trashed"]
    assert control.exists() is damage


# LLM: 真实运行账的检查点与增量跨日期形成恢复链；旧分片必须保留且无引用对照分片可回收，不能损坏日志可读性。
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
    control = old_file(old.with_name("2020-01-01.jsonl"), "{}\n")
    os.utime(old, (1, 1))
    errors = []
    pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
    assert complete and not errors and old.resolve() in pins.paths
    assert not [item for item in service.plan(now=NOW).actions if item.path == old]
    assert [item for item in service.plan(now=NOW).actions if item.path == control]
    report = service.apply(now=NOW)
    assert report.ok and old.exists() and not control.exists()
    assert len(log.list()) == 3 and len(log.recent_finished(3)) == 3


# LLM: 所有写入限于pytest临时home；返回真实服务，不替换授权和执行链。
# 函数用途: 构造最小保留服务，便于验证实际文件是否被保护。
def runtime(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    return home, candidates, MemoryRetentionService(home_paths=home, candidates=candidates)


def test_generation_formal_pins_invalidate_same_apply_snapshot(tmp_path):
    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot

    home, _, _ = runtime(tmp_path)
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    first = memory.add("user", "fact", attributes={"source_refs": ["evidence-before"]})
    memory.remove(first.entry_id)
    builds = []

    def builder(*args, **kwargs):
        builds.append(True)
        return scan.current_retention_pins(*args, **kwargs)

    snapshot = ReferencePinSnapshot(home, (), NOW, builder)
    errors = []
    old = snapshot.current(errors)
    assert old[1] and not errors and len(builds) == 1
    assert snapshot.current(errors) is old
    target = old_file(home.owner_memory_daily_dir / "new-generation-evidence.jsonl")
    memory.add("user", "new fact", attributes={"source_refs": [str(target)]})
    pins, complete = snapshot.current(errors)
    assert complete and not errors and len(builds) == 2
    assert pins.protects(target, errors)


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
    _, complete = scan.current_retention_pins(home, (), errors, now=NOW)
    assert not complete
    assert any(error.error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" for error in errors)
    assert not [action for action in service.plan(now=NOW).actions if action.path == target]
    report = service.apply(now=NOW)
    assert target.exists()
    assert not [action for action in report.actions if action.path == target and action.status == "trashed"]


@pytest.mark.parametrize("version", [1, 3])
def test_legacy_tombstone_remains_a_permanent_identity_barrier(tmp_path, version):
    from agent_py_agent.agent.memory_store.jsonl import (
        _tombstone_path,
        read_memory_records_from_path,
    )

    home, _, service = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "deleted-source.jsonl")
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
    pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
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
    from agent_py_agent.agent.memory_store.generation_manifest import StorageTombstones
    from agent_py_agent.agent.memory_store.jsonl import _tombstone_path
    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot

    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "source.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "active fact", attributes={"source_refs": [str(target)]})
    row = json.loads(memory.path.read_text().splitlines()[0])
    path = memory.path
    if source == "legacy-tombstone":
        path = _tombstone_path(path)
        row.update(action="remove", content="", entry_id="unrelated-deletion", attributes=None)
        path.write_text(json.dumps(row) + "\n")
    if freeze_stat:
        _freeze_pin_file_stat(monkeypatch, path)
    changed = []
    original_load = StorageTombstones.load

    def change_after_read(store, *args, **kwargs):
        records = original_load(store, *args, **kwargs)
        if store.memory_path == memory.path and not changed:
            changed.append(path)
            path.write_text(json.dumps({**row, "version": 2}) + "\n")
        return records

    monkeypatch.setattr(StorageTombstones, "load", change_after_read)
    snapshot = ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current(errors)
    assert changed == [path] and snapshot.value is None
    assert any(error.error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" for error in errors)
    errors = []
    pins, complete = snapshot.current(errors)
    assert complete and not errors and pins.protects(target, errors)


# LLM: 零有效期遵循正式写入的永久语义；实际plan/apply都要保护证据而非全局停清理。
# 函数用途: 验证legacy正式事件与删除账接受显式零expiry。
@pytest.mark.parametrize("source", ["formal", "legacy-tombstone"])
@pytest.mark.parametrize("expiry", [0, 0.0])
def test_legacy_zero_expiry_is_permanent(tmp_path, source, expiry):
    from agent_py_agent.agent.memory_store.jsonl import _tombstone_path

    home, _, service = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "permanent.jsonl")
    control = old_file(home.owner_memory_daily_dir / "unreferenced.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "permanent", attributes={"source_refs": [str(target)]})
    row = json.loads(memory.path.read_text().splitlines()[0])
    path = memory.path if source == "formal" else _tombstone_path(memory.path)
    if source == "legacy-tombstone":
        row.update(action="remove", content="", entry_id="unrelated-deletion", attributes=None)
    path.write_text(json.dumps({**row, "expires_at": expiry}) + "\n")
    errors = []
    pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
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
    target = old_file(home.owner_memory_daily_dir / "generation-source.jsonl")
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
    assert any(
        error.error_code == "MEMORY_RETENTION_ACTION_FAILED"
        and "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" in error.message
        for error in report.errors
    )
    assert next(item for item in report.actions if item.path == target).status == "failed"
    assert target.exists()


# LLM: 目录元数据冻结但新增线程及引用均真实；必须重建pin，不能由无关文件变化代替触发。
# 函数用途: 守护同tick新线程引用的缓存失效。
def test_same_tick_new_thread_invalidates_pin_snapshot(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot

    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "daily.jsonl", '{"event_id":"daily-id"}\n')
    directory = home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads"
    old_file(directory / "a.json", json.dumps({"thread_id": "a", "status": "active"}))
    _freeze_pin_file_stat(monkeypatch, directory)
    snapshot = ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    first, complete = snapshot.current(errors)
    assert complete and not errors and not first.protects(target, errors)
    (directory / "new.json").write_text(json.dumps({"thread_id": "new", "status": "active",
                                                  "source_refs": [{"event_id": "daily-id"}]}))
    second, complete = snapshot.current(errors)
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


# LLM: 将真实文件代次移到规范2秒窗口之外；冷文件的重验证只能查元数据，不重读正文。
# 函数用途: 用读取次数而非易抖动耗时约束多动作保留成本。
def test_apply_revalidation_does_not_reread_settled_thread_bodies(tmp_path, monkeypatch):
    import time

    from agent_py_agent.agent.conversation.models import ConversationThread

    home, _, service = runtime(tmp_path)
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
    original_object, parsed = scan._pin_object, []

    # LLM: 统计真实线程对象解析，防止只停hash却为每个动作重解析全部线程的假优化。
    # 函数用途: 给冷源成本合同同时约束内容解析次数。
    def pin_object(path):
        if path in source_paths:
            parsed.append(path)
        return original_object(path)

    monkeypatch.setattr(scan, "_pin_object", pin_object)
    report = service.apply(now=NOW)
    assert report.ok
    assert len([item for item in report.actions if item.category == "daily" and item.status == "trashed"]) == 20
    assert not reads, "settled thread content must not be rehashed per retention action"
    assert set(parsed) == source_paths and len(parsed) <= 4 * len(source_paths)


# LLM: 热源缓存只能省hash，仍须逐次核真实字节；相同stat下改写必须重建引用视图。
# 函数用途: 防止2秒窗口内不变线程重复做昂贵摘要，同时守住冻结stat的改写。
def test_recent_thread_digest_is_reused_but_equal_length_changes_are_not(tmp_path, monkeypatch):
    import gc

    from agent_py_agent.agent.memory_store import reference_pins
    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot

    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    directory = home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads"
    path = old_file(directory / "t.json", json.dumps({"thread_id": "t", "updated_at": NOW.timestamp(),
        "summary": "s" * 200000, "source_refs": [str(control)]}))
    _freeze_pin_file_stat(monkeypatch, path)
    original_hash, large_chunks = hashlib.blake2b, []
    original_open, descriptors = reference_pins.open_readonly_file_beneath, []

    # LLM: 观察真实nofollow文件入口，返回原FD以验证复核复用与对象销毁后的清理。
    # 函数用途: 记录线程源描述符的实际打开次数。
    def opened(root, parts):
        descriptor = original_open(root, parts)
        if Path(root) / parts[0] == path:
            descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(reference_pins, "open_readonly_file_beneath", opened)
    original_read, read_bytes = os.read, [0]

    # LLM: 只计活跃线程FD的实际读取字节，不把调用了current当成读回成功。
    # 函数用途: 证明热源省hash时仍真实逐块核对正文。
    def read(descriptor, size):
        raw = original_read(descriptor, size)
        if descriptor in descriptors:
            read_bytes[0] += len(raw)
        return raw

    monkeypatch.setattr(os, "read", read)

    # LLM: 代理实际hash对象，既不替代摘要也不漏掉其他hash方法。
    # 类用途: 观察大正文块被实际hash的次数。
    class ObservedHash:
        # LLM: 保存原hash对象，不复制正文。
        # 函数用途: 准备观察真实摘要更新。
        def __init__(self, *args, **kwargs):
            self.value = original_hash(*args, **kwargs)

        # LLM: 只观察大块更新，其余更新和算法保持原实现。
        # 函数用途: 区分目录小条目摘要和线程大正文摘要。
        def update(self, raw):
            if len(raw) >= 64000:
                large_chunks.append(len(raw))
            self.value.update(raw)

        # LLM: 未观察的hash接口均原样转发。
        # 函数用途: 保留真实hash返回合同。
        def __getattr__(self, name):
            return getattr(self.value, name)

    monkeypatch.setattr(hashlib, "blake2b", ObservedHash)
    snapshot = ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    first, complete = snapshot.current(errors)
    assert complete and not errors and not first.protects(target, errors)
    initial_chunks = len(large_chunks)
    assert initial_chunks
    for _ in range(5):
        before_bytes = read_bytes[0]
        assert snapshot.current(errors)[0] is first
        assert read_bytes[0] > before_bytes
    assert len(large_chunks) == initial_chunks
    assert len(descriptors) == 1
    path.write_text(path.read_text().replace("pin-a.jsonl", "pin-b.jsonl"))
    second, complete = snapshot.current(errors)
    assert complete and not errors and second is not first and second.protects(target, errors)
    del snapshot
    gc.collect()
    for descriptor in descriptors:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


# LLM: 热源转冷仍须最后比对真实字节；同tick变化才重建，未变化不得因跨时钟片而失败。
# 函数用途: 验证跨2秒窗口时仍可靠地识别是否有新引用。
@pytest.mark.parametrize("changed", [False, True])
def test_recent_pin_snapshot_is_rebuilt_after_settling(tmp_path, monkeypatch, changed):
    import time

    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot

    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "fact", attributes={"source_refs": [str(control)]})
    _freeze_pin_file_stat(monkeypatch, memory.path)
    snapshot = ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    first, complete = snapshot.current(errors)
    assert complete and not errors and not first.protects(target, errors)
    if changed:
        memory.path.write_text(memory.path.read_text().replace("pin-a.jsonl", "pin-b.jsonl"))
    later = time.time_ns() + 3_000_000_000
    monkeypatch.setattr(time, "time_ns", lambda: later)
    second, complete = snapshot.current(errors)
    assert complete and not errors and second.protects(target, errors) is changed
    assert (second is not first) is changed


# LLM: 通用pin解析期间真实改写引用但冻结stat；读后才登记来源会把旧pins和新摘要错误配对。
# 函数用途: 验证线程/策展源都具备读前读后的内容代次门。
@pytest.mark.parametrize("kind", ["thread", "curator"])
def test_generic_pin_source_mid_read_change_is_not_cached(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot

    home, _, _ = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin-b.jsonl")
    control = old_file(home.owner_memory_daily_dir / "pin-a.jsonl")
    directory = home.owner_workspace_dir / "runtime/workspaces/demo/conversations/threads"
    path = directory / "t.json" if kind == "thread" else home.owner_memory_curator_runs_dir / "run.jsonl"
    row = {"thread_id": "t", "updated_at": NOW.timestamp(), "mutation_probe": True, "source_refs": [str(control)]}
    old_file(path, json.dumps(row) + "\n")
    _freeze_pin_file_stat(monkeypatch, path)
    original, changed = scan._RetentionPins.collect, []
    original_load = json.load

    # LLM: 对JSON线程在实际解析完成、生成器尚未登记读后摘要时改写，不以collect之后替代读中竞态。
    # 函数用途: 注入线程解析返回与来源登记之间的等长改写。
    def loaded(source, *args, **kwargs):
        result = original_load(source, *args, **kwargs)
        if kind == "thread" and isinstance(result, dict) and result.get("mutation_probe") and not changed:
            changed.append(path)
            path.write_text(path.read_text().replace("pin-a.jsonl", "pin-b.jsonl"))
        return result

    # LLM: 保留真实引用收集，仅在首个完整顶层行已消费后改写同一文件。
    # 函数用途: 精确落在解析行返回与读后来源登记之间制造竞态。
    def collect(pins, value, source, *, reference=False):
        result = original(pins, value, source, reference=reference)
        if kind == "curator" and source == path and isinstance(value, dict) and value.get("mutation_probe") and not changed:
            changed.append(path)
            path.write_text(path.read_text().replace("pin-a.jsonl", "pin-b.jsonl"))
        return result

    monkeypatch.setattr(scan._RetentionPins, "collect", collect)
    monkeypatch.setattr(json, "load", loaded)
    snapshot = ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current(errors)
    assert changed == [path] and snapshot.value is None
    assert any(error.error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" for error in errors)
    errors = []
    pins, complete = snapshot.current(errors)
    assert complete and not errors and pins.protects(target, errors)


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
    snapshot = reference_pins.ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    assert snapshot.current(errors)[1] and not errors
    assert len(snapshot.contents["files"]) <= 128 and len(snapshot.contents["fds"]) <= 128
    assert snapshot.contents["bytes"] <= 4096
    assert snapshot.contents["bytes"] == sum(item[3] for item in snapshot.contents["files"].values())
    other_home, _, _ = runtime(tmp_path / "other")
    other = reference_pins.ReferencePinSnapshot(other_home, (), NOW, scan.current_retention_pins)
    assert other.current(errors)[1] and other.contents is not snapshot.contents
    assert not set(paths).intersection(other.contents["files"])
    for path in paths:
        path.unlink()
    assert snapshot.current(errors)[1] and not errors
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
    path = old_file(home.owner_memory_daily_dir / "old.jsonl")
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


# LLM: 正式记忆走原仓库；未引用文件仍须可回收，防止全停假绿。
# 函数用途: 同时验证引用保护与30天回收宽限。
def test_formal_source_pin_and_trash_grace(tmp_path):
    home, _, service = runtime(tmp_path)
    pinned = old_file(home.owner_memory_daily_dir / "pinned.jsonl")
    unpinned = old_file(home.owner_compact_dir / "expired.json")
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
    path = old_file(home.owner_memory_daily_dir / "old.jsonl")
    plan = service.plan(now=NOW)
    JsonlMemory(home.owner_memory_long_term_jsonl).add("user", "fact", attributes={"source_refs": [str(path)]})
    report = apply.execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert path.exists()
    assert next(a for a in report.actions if a.path == path).status == "state_changed"


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
    results, errors = apply._apply_candidate_phase(candidates, plan)
    assert not errors and results[0].status == "deleted"
    assert calls == [{"statuses": scan.RETENTION_ELIGIBLE_CANDIDATE_STATUSES}]


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
        pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
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
    path = old_file(home.owner_memory_daily_dir / "daily.jsonl")
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
    path = old_file(home.owner_memory_daily_dir / "old.jsonl")
    plan = service.plan(now=NOW)
    old_file(home.owner_memory_long_term_jsonl, "{bad")
    report = apply.execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert path.exists() and report.errors
    assert next(a for a in report.actions if a.path == path).status == "failed"


# LLM: 目录发现EACCES不能被glob静默洗成无会话。
# 函数用途: 验证来源发现阶段也遵守失败关闭。
def test_unreadable_conversation_discovery_is_not_zero_pins(tmp_path, monkeypatch):
    home, _, service = runtime(tmp_path)
    path = old_file(home.owner_memory_daily_dir / "old.jsonl")
    root = home.owner_workspace_dir / "runtime/workspaces"
    root.mkdir(parents=True, exist_ok=True)
    original = os.scandir

    # LLM: 仅发现根返回EACCES，其余真实文件系统观察保持。
    # 函数用途: 模拟目录不可枚举但仍存在的来源故障。
    def blocked(target):
        if Path(target) == root:
            raise PermissionError(errno.EACCES, "denied", str(root))
        return original(target)

    monkeypatch.setattr(os, "scandir", blocked)
    report = service.plan(now=NOW)
    assert report.errors and not [a for a in report.actions if a.path == path]


# LLM: 旧计划允许purge后再新增原路径/ID引用，执行仍保护实际payload。
# 函数用途: 验证回收宽限后的永久删除也重核pins。
@pytest.mark.parametrize("kind", ["path", "id", "unreadable"])
def test_trash_purge_rechecks_original_source_pins(tmp_path, kind):
    from datetime import timedelta

    home, candidates, service = runtime(tmp_path)
    source = old_file(home.owner_memory_daily_dir / "old.jsonl", '{"event_id":"daily-id"}\n')
    moved = next(a for a in service.apply(now=NOW).actions if a.path == source)
    later = NOW + timedelta(days=31)
    if kind == "unreadable":
        old_file(home.owner_memory_long_term_jsonl, "{bad")
        plan = service.plan(now=later)
        assert not [a for a in plan.actions if a.path == moved.destination]
    else:
        # 先生成允许purge的旧计划，再新增引用，执行仍须重新保护回收内容。
        plan = service.plan(now=later)
        assert [a for a in plan.actions if a.path == moved.destination]
        ref = str(source) if kind == "path" else {"event_id": "daily-id"}
        JsonlMemory(home.owner_memory_long_term_jsonl).add("user", "fact", attributes={"source_refs": [ref]})
        report = apply.execute_retention_plan(home=home, candidates=candidates, plan=plan, now=later)
        assert next(a for a in report.actions if a.path == moved.destination).status == "state_changed"
    assert (moved.destination / "payload").exists()


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
    from agent_py_agent.agent.user_space import owner_quota as quota

    monkeypatch.setattr(quota, "_DISPLAY_USAGE_CACHE_MAX_ENTRIES", 2)
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
    target = old_file(home.owner_memory_daily_dir / "old.jsonl")
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

    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot

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
    snapshot = ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
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
    else:
        with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
            snapshot.current(errors)
        assert snapshot.value is None
    second, complete = _r5_finish_verification(snapshot, errors)
    assert complete and not errors and second is not first and second.protects(target, errors)


# LLM: 仅预算待续可失败关闭，必须在有限轮数内恢复；不会吞掉最终失败或把complete断言改成仅存在。
# 函数用途: 为新双阶段预算合同完成有界续验，逐轮确保未发布不完整快照。
def _r5_finish_verification(snapshot, errors):
    for _ in range(8):
        errors.clear()
        try:
            result = snapshot.current(errors)
        except OSError as exc:
            assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" in str(exc)
            assert snapshot.value is None
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
    for index in range(24):
        old_file(home.owner_memory_daily_dir / f"a-drive-{index:02d}.jsonl")
    changed, parsed, stream_rejected = [], [], []
    original_watch, original_load = scan.watch_pin_source, json.load

    # LLM: 真实JSON加载返回后仅记接缝，不改变解析结果或complete门。
    # 函数用途: 标记即将执行读后来源检查。
    def loaded(source, *args, **kwargs):
        try:
            result = original_load(source, *args, **kwargs)
        except scan._PinReadChanged:
            stream_rejected.append(True)
            raise
        if isinstance(result, dict) and result.get("thread_id") == "t" and reference_pins._SOURCES.get() is not None:
            parsed.append(True)
        return result

    # LLM: 原地等长写入只发生一次；分别落在完整读前观察后和真实解析后的读后观察前。
    # 函数用途: 注入不能由stat识别的中段引用改写。
    def watched(source):
        sources = reference_pins._SOURCES.get()
        if source == path and timing == "after_parse" and parsed:
            if not changed and sources is not None and reference_pins._is_complete_stamp(sources.get(path)):
                path.write_text(payload.replace("pin-a.jsonl", "pin-b.jsonl"))
                changed.append(True)
            parsed.clear()
        result = original_watch(source)
        sources = reference_pins._SOURCES.get()
        if source == path and timing == "before_parse" and not changed and sources is not None:
            stamp = sources.get(path)
            if stamp is not None and reference_pins._is_complete_stamp(stamp):
                path.write_text(payload.replace("pin-a.jsonl", "pin-b.jsonl"))
                changed.append(True)
        return result

    monkeypatch.setattr(json, "load", loaded)
    monkeypatch.setattr(scan, "watch_pin_source", watched)
    original_source = scan._fingerprint_source

    # LLM: 真实第一块已交给解析流后改写后续切片，FD/stat均不变，必须由流摘要而非路径身份发现。
    # 类用途: 注入JSON读取途中的同代中段变化。
    class MidReadSource:
        # LLM: 不替换内容或描述符，只借用真实源。
        # 函数用途: 保存实际reader。
        def __init__(self, raw):
            self.raw = raw

        # LLM: 非读取接口全部透传。
        # 函数用途: 保留原始reader生命周期与FD接口。
        def __getattr__(self, name):
            return getattr(self.raw, name)

        # LLM: 仅当前snapshot的第一次真实分块读取触发改写，不在plan阶段提前注入。
        # 函数用途: 精确制造解析中途的内容竞态。
        def read1(self, size=-1):
            raw = self.raw.read1(size)
            if raw and not changed and reference_pins._SOURCES.get() is not None:
                path.write_text(payload.replace("pin-a.jsonl", "pin-b.jsonl"))
                changed.append(True)
            return raw

    # LLM: 只包被测文件，其它读取和授权链保持真实。
    # 函数用途: 将读中故障注入接到实际JSON解析入口。
    def opened(source):
        raw = original_source(source)
        return MidReadSource(raw) if source == path and timing == "during_parse" else raw

    monkeypatch.setattr(scan, "_fingerprint_source", opened)
    snapshot = reference_pins.ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    published = []
    for _ in range(24):
        errors = []
        try:
            pins, complete = snapshot.current(errors)
        except OSError as exc:
            assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" in str(exc)
            assert snapshot.value is None
            continue
        assert changed and complete and pins.protects(target, [])
        published.append(pins)
        break
    assert changed and published, "stable rewritten source must eventually publish its actual pins"
    if timing in {"before_parse", "during_parse"}:
        assert stream_rejected, "stream verification must reject mismatch before JSON parsing returns"
    # 再走真实plan/apply；计划可看到旧引用，但执行时同一竞态不能回收新引用目标。
    path.write_text(payload)
    reference_pins._RESUMABLE_DIGESTS.pop(path, None)
    changed.clear()
    parsed.clear()
    stream_rejected.clear()
    report = service.apply(now=NOW)
    assert changed and target.exists()
    assert not any(item.path == target and item.status == "trashed" for item in report.actions)
    if timing in {"before_parse", "during_parse"}:
        assert stream_rejected


# LLM: OS摘要读取与C层解析读取分别计数；预验partial禁止打开解析器，解析流不占额外摘要预算。
# 函数用途: 验证每current统一1MiB摘要硬门，防止17MiB来源每次partial被重复解析。
@pytest.mark.parametrize("size_kib", [200, 600, 2048, 17 * 1024])
def test_r5_hot_source_current_read_bytes_strictly_bounded_to_one_mib(tmp_path, monkeypatch, size_kib):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, path, _, _ = _r5_hot_thread(tmp_path, monkeypatch, size_kib, 100)
    identity = path.stat().st_dev, path.stat().st_ino
    digest_bytes, parser_bytes, opens = [0], [0], []
    original_read, original_source = os.read, scan._fingerprint_source

    # LLM: 统计真实返回字节而非请求长度，只计被测来源。
    # 函数用途: 观察摘要FD读取。
    def read(fd, size):
        raw = original_read(fd, size)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            digest_bytes[0] += len(raw)
        return raw

    # LLM: 透传真实reader，防止仅计os.read漏掉json.load的C层全文件读取。
    # 类用途: 统计实际解析流字节。
    class ParserReader:
        # LLM: 借用实际源，不复制或伪造字节。
        # 函数用途: 保存真实读取器。
        def __init__(self, raw):
            self.raw = raw

        # LLM: 其余接口全交原对象。
        # 函数用途: 保留关闭、fileno等合同。
        def __getattr__(self, name):
            return getattr(self.raw, name)

        # LLM: 不忽略read(-1)导致的整文件读取。
        # 函数用途: 统计JSON解析实际字节。
        def read(self, size=-1):
            raw = self.raw.read(size)
            parser_bytes[0] += len(raw)
            return raw

        # LLM: 流式读取仍取真实read1返回量。
        # 函数用途: 统计文本包装器的分块读取。
        def read1(self, size=-1):
            raw = self.raw.read1(size)
            parser_bytes[0] += len(raw)
            return raw

    # LLM: partial须在打开解析器前关闭，不能靠丢弃解析结果掩盖无界读取。
    # 函数用途: 断言解析入口已有完整预验，随后包装真实reader。
    def opened(source):
        raw = original_source(source)
        if source != path:
            return raw
        stamp = reference_pins._SOURCES.get().get(path)
        assert reference_pins._is_complete_stamp(stamp), "partial source was opened for parsing"
        opens.append(path)
        return ParserReader(raw)

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(scan, "_fingerprint_source", opened)
    snapshot = reference_pins.ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    for _ in range(4):
        digest_bytes[0] = parser_bytes[0] = 0
        try:
            snapshot.current([])
        except OSError as exc:
            assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" in str(exc)
        assert 0 < digest_bytes[0] <= 1_048_576
        if not parser_bytes[0]:
            assert digest_bytes[0] + parser_bytes[0] <= 1_048_576
    if size_kib == 17 * 1024:
        assert not opens and parser_bytes[0] == 0


# LLM: 多来源进度可交错但不能让已完成post的来源每轮重开，导致全源永远无法同时完成。
# 函数用途: 验证多个大源的pending可收敛，且续验不会重复解析。
@pytest.mark.parametrize("sizes", [(600, 600), (1100, 1100), (1100, 2048)])
def test_r5_multiple_hot_sources_pending_finishes_without_reparsing(tmp_path, monkeypatch, sizes):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, first, target, payload = _r5_hot_thread(tmp_path, monkeypatch, sizes[0])
    second = first.with_name("u.json")
    prefix = '{"thread_id":"u","summary":"'
    second.write_text(prefix + "s" * (sizes[1] * 1024 - len(prefix) - 2) + '"}')
    _freeze_pin_file_stat(monkeypatch, second)
    original, parsed = json.load, []

    # LLM: 计数真实线程解析，不能用构建器调用次数代替实际正文消费。
    # 函数用途: 观察pending续验是否重复加载JSON。
    def loaded(source, *args, **kwargs):
        row = original(source, *args, **kwargs)
        if isinstance(row, dict) and row.get("thread_id") in {"t", "u"}:
            parsed.append(row["thread_id"])
        return row

    monkeypatch.setattr(json, "load", loaded)
    snapshot = reference_pins.ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    for _ in range(24):
        pending, count = snapshot.pending is not None, len(parsed)
        try:
            pins, complete = snapshot.current([])
        except OSError as exc:
            assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" in str(exc)
            assert snapshot.value is None
        else:
            assert complete and not pins.protects(target, [])
            break
        finally:
            if pending:
                assert len(parsed) == count, "pending must advance digests without parsing again"
    else:
        pytest.fail("stable multiple-source post verification never converged")
    assert set(parsed) == {"t", "u"}


# LLM: 解析后的待验源滑出热窗口仍须把剩余切片验完，不能把冷元数据当完成证明或重读正文。
# 函数用途: 验证pending热转冷安全收敛。
def test_r5_pending_hot_source_settles_without_reparse(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, path, target, _ = _r5_hot_thread(tmp_path, monkeypatch, 2048)
    snapshot = reference_pins.ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    for _ in range(8):
        with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
            snapshot.current([])
        if snapshot.pending is not None:
            break
    assert snapshot.pending is not None and snapshot.value is None
    later = reference_pins.time.time_ns() + 3_000_000_000
    monkeypatch.setattr(reference_pins.time, "time_ns", lambda: later)
    original = scan._fingerprint_source

    # LLM: 待验期来源必须只走摘要，不允许重新打开JSON解析器。
    # 函数用途: 把热转冷pending的重复解析变成明确失败。
    def opened(source):
        assert source != path, "settling pending source was reparsed"
        return original(source)

    monkeypatch.setattr(scan, "_fingerprint_source", opened)
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and not pins.protects(target, [])
    assert len(snapshot.sources[path]) == 6


# LLM: 全局LRU状态可被回收并被另一snapshot重建，pending不能仅凭重置为零的generation接受另一份解析基线。
# 函数用途: 验证切片状态替换不会使旧待发布pins嫁接到新内容。
def test_r5_pending_does_not_accept_replaced_digest_state(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import reference_pins

    home, _, path, target, payload = _r5_hot_thread(tmp_path, monkeypatch, 600)
    snapshot = reference_pins.ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current([])
    assert snapshot.pending is not None
    # 模拟LRU回收；另一份快照在相同冻结stat下完整读取新内容并重建全局摘要状态。
    reference_pins._RESUMABLE_DIGESTS.pop(path)
    path.write_text(payload.replace("pin-a.jsonl", "pin-b.jsonl"))
    other = reference_pins.ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    other_pins, complete = _r5_finish_verification(other, [])
    assert complete and other_pins.protects(target, [])
    pins, complete = _r5_finish_verification(snapshot, [])
    assert complete and pins.protects(target, []), "old pending pins adopted a replacement digest baseline"


# LLM: 超过单轮1MiB快照预算的热源在未完成全部切片前必须失败关闭，下一轮可续完成并捕获1.2MiB处改写。
# 函数用途: 验证超1MiB热源的分轮失败关闭可续验证与超远偏移等长改写检测。
def test_r4_oversized_hot_pin_source_fails_closed_until_resumable_slices_complete(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot

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
    snapshot = ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins)
    errors = []
    # 第 1 轮仅核验前 4 片（<= 1 MiB），剩余切片未核完必须失败关闭。
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current(errors)
    # 完成读前剩余切片后仍须完整读后验证，全部完成之前不得放行。
    errors = []
    first, complete = _r5_finish_verification(snapshot, errors)
    assert complete and not errors and not first.protects(target, errors)
    # 在 1.2 MiB 偏移处做等长原地改写：下轮先因超过 1 MiB 复核预算失败关闭，再于续验轮检出并重建。
    rewritten = payload.replace("pin-a.jsonl", "pin-b.jsonl")
    assert len(rewritten) == len(payload)
    path.write_text(rewritten)
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current(errors)
    errors = []
    second, complete = _r5_finish_verification(snapshot, errors)
    assert complete and not errors and second is not first and second.protects(target, errors)
