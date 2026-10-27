"""M5：坏指纹、读取竞态、引用保护及配额扫描的回归合同。"""
# LLM: 只在pytest临时home运行真实保留/配额链；故障注入不替换授权或删除CAS，禁止访问真实owner。
# 模块用途: 覆盖M5误删反例、引用与回收宽限保护、流式读取和展示缓存不绕过配额。
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


# LLM: 原候选删除锁内CAS保持；仅观察借用入口与合法状态筛选。
# 函数用途: 防止执行端恢复全账本深复制。
def test_apply_candidate_phase_borrows_filtered_readonly_rows(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    home, candidates, service = runtime(tmp_path)
    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="old",
        subject_key="test.old", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson"))
    candidates.path.write_text(json.dumps(replace(row, updated_at="2020-01-01T00:00:00+00:00").to_record()) + "\n")
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


# LLM: 两次真实展示统计之间不改目录，新增文件后必须失效缓存。
# 函数用途: 验证展示档案热扫描减少stat且识别新文件。
def test_display_archive_observation_cache_is_bounded_and_invalidated(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota as quota

    root = tmp_path / "owner"
    path = old_file(root / "memory_archive/display_archives/2026-01-01/a.txt", "1234")
    original, calls = Path.stat, []

    # LLM: 观察真实stat调用；不替换字节数或准入结果。
    # 函数用途: 统计热目录是否重复逐文件stat。
    def tracked(self, *args, **kwargs):
        if self == path:
            calls.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", tracked)
    assert quota.owner_logical_usage_bytes(root) == 4
    calls.clear()
    assert quota.owner_logical_usage_bytes(root) == 4
    assert not calls
    old_file(path.parent / "b.txt", "567")
    assert quota.owner_logical_usage_bytes(root) == 7
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

    monkeypatch.setattr(quota, "_DISPLAY_USAGE_CACHE_LIMIT", 2)
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
