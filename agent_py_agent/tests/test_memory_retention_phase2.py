# LLM: 只操作临时owner、真实SQLite和文件树；不清理生产任务或宿主运行记录。
# 模块用途: 锁定留存定深扫描、追加竞态隔离、任务tmp与DB终态回填。
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.tests.test_memory_retention_wave5 import NOW, old_file, runtime


# LLM: 默认值兼容旧策略，显式值必须严格非负整数。
# 函数用途: 验证task tmp新策略的缺省及坏值门。
def test_task_tmp_policy_defaults_and_strict_values():
    payload = MemoryRetentionPolicy().to_dict()
    assert payload.get("task_tmp_idle_days") == 3
    payload.pop("task_tmp_idle_days")
    assert MemoryRetentionPolicy.from_payload(payload).task_tmp_idle_days == 3
    for invalid in (-1, True, "3"):
        with pytest.raises(ValueError):
            MemoryRetentionPolicy.from_payload({**payload, "task_tmp_idle_days": invalid})


# LLM: 无state的用户工作目录也应只清tmp；最近文件保护整棵tmp，兄弟目录不受影响。
# 函数用途: 验证三日闲置和空目录清扫严格不越过scratch边界。
@pytest.mark.parametrize("fresh", [False, True])
def test_stateless_task_tmp_cleans_only_idle_scratch(tmp_path, fresh):
    home, _candidates, service = runtime(tmp_path)
    task = home.owner_tasks_dir / "2026-10-01" / "sample"
    scratch = old_file(task / "tmp" / "nested" / "generated.bin", "scratch")
    (task / "tmp" / "empty" / "child").mkdir(parents=True)
    sentinels = [old_file(task / name, "must keep") for name in ("output/result.txt", "inputs/source.txt", "README.md")]
    if fresh:
        os.utime(scratch, (NOW.timestamp(), NOW.timestamp()))
    before = {path: path.read_bytes() for path in sentinels}
    planned = service.plan(now=NOW)
    assert bool([a for a in planned.actions if a.category == "task_tmp"]) is not fresh
    service.apply(now=NOW)
    assert scratch.exists() is fresh
    if not fresh:
        assert not list((task / "tmp").iterdir())
    assert {path: path.read_bytes() for path in sentinels} == before


# LLM: 监测路径枚举本身，不用耗时阈值猜遍历是否进入百万文件树。
# 函数用途: 证明任务状态发现与pin扫描不递归进入tmp/inputs。
def test_task_state_discovery_never_rglobs_scratch(tmp_path, monkeypatch):
    home, _candidates, service = runtime(tmp_path)
    old_file(home.owner_tasks_dir / "2026-10-01" / "sample" / "tmp" / "deep" / "x")
    original = Path.rglob

    # LLM: 仅监视路径枚举，不改变真实发现结果；禁止递归扫描任务根。
    # 函数用途: 发现错误递归入口时让测试立即失败。
    def guarded(path, pattern, *args, **kwargs):
        assert path != home.owner_tasks_dir, "task root recursively traversed"
        return original(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", guarded)
    service.plan(now=NOW)


# LLM: 首次读取后真实append制造代次漂移；第二次稳定才可接受，持续漂移有界关闭。
# 函数用途: 验证追加竞态重试与失败不阻断独立task tmp。
def test_pin_append_retry_and_domain_isolation(tmp_path, monkeypatch):
    home, _candidates, service = runtime(tmp_path)
    path = home.owner_memory_curator_runs_dir / "racing.jsonl"
    old_file(path)
    scratch = old_file(home.owner_tasks_dir / "2026-10-01" / "sample" / "tmp" / "x")
    original = scan._fingerprint_source
    calls = []

    # _pin_rows使用TextIOWrapper，底层read1是实际读取口；每次打开后直接append一次，可靠触发首末FD变化。
    # LLM: 保留真实FD读写与异常，只有指定来源在打开后真实追加。
    # 函数用途: 确定性制造pin读取前后的代次漂移。
    def racing_open(target):
        source = original(target)
        if target == path:
            calls.append(target)
            with path.open("ab") as writer:
                writer.write(b"{}\n")
        return source

    monkeypatch.setattr(scan, "_fingerprint_source", racing_open)
    report = service.apply(now=NOW)
    assert report.applied and report.errors
    assert not scratch.exists(), "unrelated task tmp must not be blocked by racing pin"
    assert len(calls) >= 3, "concurrent append was not retried"


# LLM: 使用产品runtime schema建临时库；RUNNING档案的终态只可由规范DB精确身份证明。
# 函数用途: 构造一个终态DB行及可选活跃资源锁。
def terminal_db(home, *, active=False):
    from agent_py_agent.agent.runtime_db.schema import _BASE_RUNTIME_SQL

    path = Path(home.owner_home_dir) / "runtime.db"
    with sqlite3.connect(path) as db:
        for sql in _BASE_RUNTIME_SQL:
            db.execute(sql)
        db.execute("INSERT INTO task_runs(task_run_id, task_id, status, created_at, updated_at, closed_at) VALUES(?, ?, ?, ?, ?, ?)",
            ("task-run-1", "task-1", "done", 1, 2, 2))
        db.execute("INSERT INTO agent_runs(agent_run_id, task_run_id, run_id, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            ("agent-1", "task-run-1", "run-1", "done", 1, 2))
        if active:
            db.execute("INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance, attempt_id, attempt_generation, workspace_epoch, lease_expires_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("lock-1", "workspace", "instance", "attempt", 1, 1, NOW.timestamp() + 3600, 1, 1))
    return path


# LLM: 只收回scratch，不回写运行档案或DB；活跃锁以及执行前重新激活都必须保护。
# 函数用途: 验证runs规范两层目录终态回填与只读DB重核。
@pytest.mark.parametrize("active", [False, True])
def test_running_run_state_reconciles_terminal_db_for_scratch(tmp_path, active):
    home, _candidates, service = runtime(tmp_path)
    root = Path(home.owner_home_dir) / "runs" / "2026-10-01" / "run-1"
    state = old_file(root / "work" / "state.json", json.dumps({"task_id": "task-1", "run_id": "run-1", "status": "RUNNING", "updated_at": 1}))
    scratch = old_file(root / "tmp" / "large.bin")
    output = old_file(root / "output" / "result.txt", "keep")
    db = terminal_db(home, active=active)
    before = db.read_bytes(), state.read_bytes(), output.read_bytes()
    service.apply(now=NOW)
    assert scratch.exists() is active
    assert (db.read_bytes(), state.read_bytes(), output.read_bytes()) == before


# LLM: 用旧计划后真实改DB，不替换执行器的CAS或删除门。
# 函数用途: 确保计划后重开运行不会被旧终态证明删除。
def test_run_reactivation_after_plan_preserves_scratch(tmp_path):
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    root = Path(home.owner_home_dir) / "runs" / "run-1"
    old_file(root / "work/state.json", json.dumps({"task_id": "task-1", "run_id": "run-1", "status": "RUNNING", "updated_at": 1}))
    scratch = old_file(root / "tmp/x")
    db = terminal_db(home)
    plan = service.plan(now=NOW)
    assert [a for a in plan.actions if a.category == "run_scratch"]
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE agent_runs SET status='running'")
    execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert scratch.exists()


# LLM: DB终态与JSON终态都必须再次检查新增资源锁，不能只在规划时检查。
# 函数用途: 防止计划后取得活跃锁的运行仍被清空scratch。
def test_json_terminal_run_respects_lock_added_after_plan(tmp_path):
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    root = Path(home.owner_home_dir) / "runs" / "run-1"
    old_file(root / "work/state.json", json.dumps({"task_id": "task-1", "run_id": "run-1", "status": "DONE", "updated_at": NOW.timestamp() - 20 * 86400}))
    scratch = old_file(root / "tmp/x")
    db = terminal_db(home)
    plan = service.plan(now=NOW)
    assert [a for a in plan.actions if a.category == "run_scratch"]
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance, attempt_id, attempt_generation, workspace_epoch, lease_expires_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("new-lock", "workspace", "instance", "attempt", 1, 1, NOW.timestamp() + 3600, 1, 1))
    execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert scratch.exists()


# LLM: 在owner锚定后移动整棵tmp进output，文件inode/stat不变，再替换任务祖先链接。
# 函数用途: 验证实际unlink始终受owner逐段nofollow约束，不能删到output里的同名scratch。
def test_scratch_ancestor_swap_cannot_redirect_unlink_into_output(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_apply as apply

    home, _candidates, service = runtime(tmp_path)
    task = home.owner_tasks_dir / "2026-10-01" / "sample"
    scratch = old_file(task / "tmp/x", "must survive outside tmp")
    (task / "output").mkdir()
    original = apply.open_directory_beneath
    moved = task.with_name("moved")
    swapped = False

    # LLM: 仅在真实目录FD返回后移动目录，不替换删除器或指纹门。
    # 函数用途: 注入祖先替换并验证output字节仍保留。
    def swapping(root, parts, **kwargs):
        nonlocal swapped
        descriptor = original(root, parts, **kwargs)
        if not swapped and Path(root) == Path(home.owner_home_dir) and tuple(parts)[-1:] == ("tmp",):
            task.rename(moved)
            (moved / "tmp").rename(moved / "output/tmp")
            task.symlink_to(moved / "output", target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(apply, "open_directory_beneath", swapping)
    service.apply(now=NOW)
    assert swapped, "must actually inject the ancestor replacement"
    assert (moved / "output/tmp/x").read_text() == "must survive outside tmp"


# LLM: 只构造真实临时候选；approved和pending履约两种状态均须持久并保护原证据。
# 函数用途: 为scratch回归写入一个不能回收的候选证据引用。
def obligated_evidence(candidates, path, status):
    from dataclasses import replace

    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.candidates import (
        _NullAdmission,
        _write_candidates_unlocked,
    )

    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="证据必须保留",
        subject_key="scratch.evidence", scope=MemoryScope("personal", "personal"),
        origin="model_inferred", promotion_target="lesson", evidence_refs=({"path": str(path)},)))
    if status == "approved":
        return candidates.transition(row.candidate_id, "approved", reviewer="test")
    row = replace(row, commit_obligation=True)
    _write_candidates_unlocked(candidates.path, [row], admission=_NullAdmission())
    return row


# LLM: 两类scratch使用真实plan/apply；新增引用要在旧计划执行前同样生效，不可只改规划。
# 函数用途: 验证待履约候选及其证据目录在规划和执行两端均受保护。
@pytest.mark.parametrize("kind", ["task_tmp", "run_scratch"])
@pytest.mark.parametrize("status", ["approved", "pending_review"])
@pytest.mark.parametrize("late", [False, True])
def test_obligated_scratch_evidence_is_pinned_in_plan_and_apply(tmp_path, kind, status, late):
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    if kind == "task_tmp":
        root = home.owner_tasks_dir / "2026-10-01/sample/tmp"
    else:
        task = Path(home.owner_home_dir) / "runs/2026-10-01/run-1"
        old_file(task / "work/state.json", json.dumps({"task_id": "task-1", "run_id": "run-1",
            "status": "DONE", "updated_at": NOW.timestamp() - 20 * 86400}))
        root = task / "work/blobs/tool_outputs"
    evidence = old_file(root / "nested/proof.bin", "proof bytes")
    old_file(root / "control.bin", "unreferenced")
    plan = service.plan(now=NOW)
    assert [a for a in plan.actions if a.path == root and a.category == kind]
    obligated_evidence(candidates, evidence, status)
    pins, complete = scan.current_retention_pins(home, (), [], now=NOW)
    assert complete and pins.protects(root, []) and pins.protects(evidence, [])
    if not late:
        plan = service.plan(now=NOW)
        assert not [a for a in plan.actions if a.path == root]
    report = execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert evidence.read_text() == "proof bytes"
    assert not [a for a in report.actions if a.path == root and a.status == "deleted"]


# LLM: 来源损坏在规划及执行前分别发生，不接受把无法解析的候选当空引用。
# 函数用途: 验证未知候选证据使scratch清理关闭，但不要求停掉独立cache清理。
@pytest.mark.parametrize("late", [False, True])
def test_corrupt_candidate_pins_preserve_scratch(tmp_path, late):
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    evidence = old_file(home.owner_tasks_dir / "2026-10-01/sample/tmp/x", "keep")
    plan = service.plan(now=NOW)
    old_file(candidates.path, "{broken\n")
    if not late:
        plan = service.plan(now=NOW)
        assert not [a for a in plan.actions if a.path == evidence.parent]
    report = execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert evidence.read_text() == "keep" and (plan.errors or report.errors)


# LLM: 在逐文件/逐目录真实open返回后移动，覆盖旧测试第一次探查以外的危险接缝。
# 函数用途: 验证FD定位后重新核父链，移入output的文件和目录不得被删除。
@pytest.mark.parametrize("directory", [False, True])
def test_move_after_per_member_open_preserves_output(tmp_path, monkeypatch, directory):
    from agent_py_agent.agent.memory_store import retention_apply as apply

    home, _candidates, service = runtime(tmp_path)
    task = home.owner_tasks_dir / "2026-10-01/sample"
    old_file(task / "tmp/nested/x", "published")
    (task / "output").mkdir()
    original = apply.open_directory_beneath
    calls, moved = [], task / "output/moved"

    # LLM: 保留真实FD；第二次nested打开是rmdir接缝，第一次是unlink接缝。
    # 函数用途: 在已拿到句柄后真实发布目录至output。
    def moving(root, parts, **kwargs):
        descriptor = original(root, parts, **kwargs)
        if Path(root) == Path(home.owner_home_dir):
            calls.append(tuple(parts))
            wanted = tuple(parts)[-1:] == (("tmp",) if directory else ("nested",))
            if wanted and not moved.exists() and (not directory or len(calls) >= 3):
                (task / "tmp").rename(moved)
        return descriptor

    monkeypatch.setattr(apply, "open_directory_beneath", moving)
    report = service.apply(now=NOW)
    assert moved.exists(), calls
    if directory:
        assert (moved / "nested").is_dir()
    else:
        assert (moved / "nested/x").read_text() == "published"
    assert not [a for a in report.actions if a.category == "task_tmp" and a.status == "deleted"]


# LLM: 两个真实Daily动作之间首次发表tasks或runs状态；不得重新生成计划掩盖缓存漏监视。
# 函数用途: 锁定已有work首次state写入后下一动作重建pins。
@pytest.mark.parametrize("namespace", ["tasks", "runs"])
def test_first_state_publication_invalidates_next_action_snapshot(tmp_path, monkeypatch, namespace):
    from agent_py_agent.agent.memory_store import retention_apply as apply

    home, _candidates, service = runtime(tmp_path)
    first = old_file(home.owner_memory_daily_dir / "a-first.jsonl")
    second = old_file(home.owner_memory_daily_dir / "b-pinned.jsonl")
    work = Path(home.owner_home_dir) / namespace / "2026-10-01/sample/work"
    work.mkdir(parents=True)
    original = apply._apply_action
    published = []

    # LLM: 第一个真实动作完成后发布state，不修改第二个动作或pin函数。
    # 函数用途: 模拟同阶段任务首次启动并新增证据引用。
    def publish(**kwargs):
        result = original(**kwargs)
        if kwargs["action"].path == first:
            assert result.status == "trashed"
            old_file(work / "state.json", json.dumps({"task_id": "sample", "status": "RUNNING",
                "updated_at": NOW.timestamp(), "source_refs": [{"path": str(second)}]}))
            published.append(True)
        return result

    monkeypatch.setattr(apply, "_apply_action", publish)
    report = service.apply(now=NOW)
    assert published and not first.exists() and second.exists()
    assert next(a for a in report.actions if a.path == second).status == "state_changed"


# LLM: 仅日期/任务目录的scandir失败，必须真的经过注入且complete=False，不靠chmod行为猜权限。
# 函数用途: 验证有界任务发现传播权限与IO错误而非零引用。
@pytest.mark.parametrize("level", ["date", "task"])
def test_unreadable_task_discovery_is_incomplete(tmp_path, monkeypatch, level):
    home, _candidates, _service = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "pin.jsonl")
    task = home.owner_tasks_dir / "2026-10-01/sample"
    old_file(task / "work/state.json", json.dumps({"task_id": "sample", "status": "RUNNING",
        "updated_at": NOW.timestamp(), "source_refs": [str(target)]}))
    original, calls = os.scandir, []
    original_stat = Path.stat
    blocked = task.parent if level == "date" else task

    # LLM: 精确拒绝选中的发现目录，其它文件系统读取原样执行。
    # 函数用途: 注入不会被Path.glob吞掉的目录权限错误。
    def denied(path):
        if level == "date" and Path(path) == blocked:
            calls.append(path)
            raise PermissionError(13, "denied", str(path))
        return original(path)

    # LLM: 精确任务后缀stat模拟真实父目录搜索权不足，不要求发现器为列名额外递归任务内容。
    # 函数用途: 注入规范work父链不可读取的失败。
    def denied_stat(path, *args, **kwargs):
        if level == "task" and path == task / "work":
            calls.append(path)
            raise PermissionError(13, "denied", str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", denied)
    monkeypatch.setattr(Path, "stat", denied_stat)
    errors = []
    _pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
    assert calls and not complete and errors


# LLM: 真实过期trash容器在计划和执行均须独立验证；只瞬时追加不等于坏源，损坏/相关引用仍保留。
# 函数用途: 锁定不依赖无关Curator竞态的purge以及相关故障关闭边界。
@pytest.mark.parametrize("source_kind", ["unrelated", "referenced", "corrupt", "unreadable", "mid_read"])
def test_trash_scoped_pins_revalidate_racing_source(tmp_path, monkeypatch, source_kind):
    from datetime import timedelta

    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    source = old_file(home.owner_memory_daily_dir / "expired.jsonl")
    moved = next(a for a in service.apply(now=NOW).actions if a.path == source)
    later = NOW + timedelta(days=31)
    old_plan = service.plan(now=later)
    assert [a for a in old_plan.actions if a.path == moved.destination]
    path = home.owner_memory_curator_runs_dir / "racing.jsonl"
    content = json.dumps({"source_refs": [str(source)]}) if source_kind == "referenced" else "{}"
    old_file(path, content + "\n")
    original, calls = scan._fingerprint_source, []

    # LLM: 包装真实二进制FD，首个读取块后才追加，不能用FD打开前的竞态冒充读中变化。
    # 类用途: 制造持久读中不稳定来源以检验分域复核仍失败关闭。
    class MutatingReader:
        # LLM: 借用真实原文件对象，仅用于测试有界读取，不修改关闭所有权。
        # 函数用途: 保存被观察的实际读取器。
        def __init__(self, raw):
            self.raw, self.changed = raw, False

        # LLM: TextIOWrapper需要原始reader属性，全部透传原对象。
        # 函数用途: 保留真实二进制句柄接口。
        def __getattr__(self, name):
            return getattr(self.raw, name)

        # LLM: 每次read1取得数据后真实追加，首末fstat必须看到变化。
        # 函数用途: 在读取途中制造一次追加。
        def read1(self, size=-1):
            data = self.raw.read1(size)
            if not self.changed:
                self.changed = True
                with path.open("ab") as writer:
                    writer.write(b"{}\n")
            return data

    # LLM: 仅目标来源在open后/读中改变，其它来源与真实删除器不替换。
    # 函数用途: 精确区分瞬时追加、真正损坏和持续读中变化。
    def racing(target):
        if target == path and source_kind == "unreadable":
            calls.append(target)
            raise PermissionError(13, "denied", str(target))
        raw = original(target)
        if target == path:
            calls.append(target)
            if source_kind == "corrupt":
                with path.open("ab") as writer:
                    writer.write(b"{broken\n")
            elif source_kind == "mid_read":
                return MutatingReader(raw)
            else:
                with path.open("ab") as writer:
                    writer.write(b"{}\n")
        return raw

    monkeypatch.setattr(scan, "_fingerprint_source", racing)
    plan = service.plan(now=later)
    assert bool([a for a in plan.actions if a.path == moved.destination]) is (source_kind == "unrelated")
    report = execute_retention_plan(home=home, candidates=candidates, plan=old_plan, now=later)
    assert calls
    assert moved.destination.exists() is (source_kind != "unrelated")
    action = next(a for a in report.actions if a.path == moved.destination)
    assert (action.status == "deleted") is (source_kind == "unrelated")


# LLM: DB终态只解除非hold运行的隐式活跃保护；法律保留仍保护整棵规范恢复根。
# 函数用途: 防止终态回填绕过legal_hold_task_ids的引用保护。
def test_terminal_db_reconciliation_preserves_held_run_pin(tmp_path):
    home, _candidates, _service = runtime(tmp_path)
    root = Path(home.owner_home_dir) / "runs/2026-10-01/run-1"
    old_file(root / "work/state.json", json.dumps({"task_id": "task-1", "run_id": "run-1",
        "status": "RUNNING", "updated_at": 1}))
    target = old_file(root / "tmp/x")
    terminal_db(home)
    policy = json.loads(home.owner_retention_json.read_text())
    home.owner_retention_json.write_text(json.dumps({**policy, "legal_hold_task_ids": ["task-1"]}))
    pins, complete = scan.current_retention_pins(home, (), [], now=NOW)
    assert complete and pins.protects(target, [])


# LLM: 单个已消失成员不能把同目录剩余任务吞成空；夹具仅替换一个DirEntry的stat，真实活跃state仍解析。
# 函数用途: 验证有界发现遇到局部ENOENT时继续读取其他引用。
def test_disappearing_discovery_member_does_not_hide_active_sibling(tmp_path, monkeypatch):
    home, _candidates, _service = runtime(tmp_path)
    date = home.owner_tasks_dir / "2026-10-01"
    target = old_file(home.owner_memory_daily_dir / "active.jsonl")
    old_file(date / "sample/work/state.json", json.dumps({"task_id": "sample", "status": "RUNNING",
        "updated_at": NOW.timestamp(), "source_refs": [str(target)]}))
    original, calls = os.scandir, []

    # LLM: 模拟枚举后恰好消失的兄弟条目，不改变活跃任务本身。
    # 类用途: 提供明确ENOENT的目录成员元数据。
    class Vanished:
        # LLM: 不提供不存在对象的身份，精确报告本次成员消失。
        # 函数用途: 制造局部而非整层目录缺失。
        def stat(self, **kwargs):
            calls.append(True)
            raise FileNotFoundError(2, "gone", str(date / "gone"))

    # LLM: 管理真实scandir寿命，仅在真实成员之前插入一个消失对象。
    # 类用途: 保留全部真实任务枚举结果并注入局部竞态。
    class Entries:
        # LLM: 构造仅借用既有目录读取器。
        # 函数用途: 保存实际scandir对象。
        def __init__(self, raw):
            self.raw = raw

        # LLM: 真实条目完整保留，不能借测试替身抹掉后续活跃状态。
        # 函数用途: 在枚举首项制造已消失成员。
        def __enter__(self):
            return iter([Vanished(), *self.raw.__enter__()])

        # LLM: 原上下文负责关闭目录流，不吞异常。
        # 函数用途: 释放真实scandir句柄。
        def __exit__(self, *args):
            return self.raw.__exit__(*args)

    # LLM: 仅选中日期层包装，根及其它目录仍沿原os.scandir。
    # 函数用途: 注入一个目录成员消失，不制造真实权限故障。
    def vanishing(path):
        raw = original(path)
        return Entries(raw) if Path(path) == date else raw

    monkeypatch.setattr(os, "scandir", vanishing)
    pins, complete = scan.current_retention_pins(home, (), [], now=NOW)
    assert calls and complete and pins.protects(target, [])


# LLM: 日期层的任务名不受内部目录名称限制；沿真实plan与旧plan执行同时核验新引用。
# 函数用途: 验证合法任务名tmp/work/inputs/output不会被发现器当作内部目录忽略。
@pytest.mark.parametrize("task_name", ["tmp", "work", "inputs", "output"])
def test_internal_directory_names_are_legal_task_names(tmp_path, task_name):
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    target = old_file(home.owner_memory_daily_dir / "2000-01-01.jsonl")
    old_plan = service.plan(now=NOW)
    assert any(action.path == target for action in old_plan.actions)
    old_file(home.owner_tasks_dir / "2026-10-01" / task_name / "work/state.json", json.dumps({
        "task_id": task_name, "status": "RUNNING", "updated_at": NOW.timestamp(), "source_refs": [str(target)]}))
    pins, complete = scan.current_retention_pins(home, (), [], now=NOW)
    assert complete and pins.protects(target, [])
    assert not any(action.path == target for action in service.plan(now=NOW).actions)
    execute_retention_plan(home=home, candidates=candidates, plan=old_plan, now=NOW)
    assert target.exists()


# LLM: 只计数原严格扫描器的调用，保留真实引用读取与所有规划结果，不改变完整性门。
# 函数用途: 验证同一次规划只构建一次共享pin视图。
def test_plan_builds_reference_pins_once(tmp_path, monkeypatch):
    home, _candidates, service = runtime(tmp_path)
    old_file(home.owner_tasks_dir / "2026-10-01/sample/tmp/x")
    original, calls = scan.current_retention_pins, []

    # LLM: 原扫描完整执行，额外记录次数用于防止按类别重复解析与重复报错。
    # 函数用途: 观察本次规划的pin构建次数。
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(scan, "current_retention_pins", counted)
    plan = service.plan(now=NOW)
    assert any(action.category == "task_tmp" for action in plan.actions)
    assert len(calls) == 1


# LLM: 使用真实删除与快照，只观察代次复核入口；文件数不能放大全源代次扫描次数。
# 函数用途: 验证每个scratch动作只复核一次来源代次而非逐子文件复核。
@pytest.mark.parametrize("kind", ["task_tmp", "run_scratch"])
def test_scratch_revalidates_pin_generation_once_per_action(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    if kind == "task_tmp":
        root = home.owner_tasks_dir / "2026-10-01/sample/tmp"
    else:
        task = Path(home.owner_home_dir) / "runs/2026-10-01/run-1"
        old_file(task / "work/state.json", json.dumps({"task_id": "task-1", "run_id": "run-1",
            "status": "DONE", "updated_at": NOW.timestamp() - 20 * 86400}))
        root = task / "tmp"
    for number in range(32):
        old_file(root / "nested" / f"{number}.bin")
    plan = service.plan(now=NOW)
    assert len([action for action in plan.actions if action.path == root]) == 1
    original, calls = ReferencePinSnapshot.current, []

    # LLM: 真实快照仍验证全部来源，计数不得替代原执行结果断言。
    # 函数用途: 记录单个动作触发的来源代次复核。
    def counted(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ReferencePinSnapshot, "current", counted)
    report = execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert next(action for action in report.actions if action.path == root).status == "deleted"
    assert not list(root.iterdir())
    assert len(calls) == 1


# LLM: 归档由真实外置器产生，候选只带运行和调用ID、不带路径；旧计划必须重核后保留整棵证据。
# 函数用途: 验证七天后按ID引用的运行工具索引与外置信封仍可供晋升核验。
@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("owner_identity", [False, True])
def test_id_only_tool_evidence_pins_run_scratch(tmp_path, late, owner_identity):
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.promotion import LocalStoreToolEvidenceVerifier
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    task = Path(home.owner_home_dir) / "runs/2026-10-01/run-1"
    old_file(task / "work/state.json", json.dumps({"task_id": "task-1", "run_id": "run-1",
        "status": "DONE", "updated_at": NOW.timestamp() - 20 * 86400}))
    # 异名运行仍能在规范索引中精确核验，迫使清理器检查子文件而不只匹配父目录ID。
    run_id = "run-1" if owner_identity else "archived-run"
    record = externalize_tool_output_record(ExternalizeToolOutputRequest(
        root=task / "work", tool="read_file", call_id="call-proof", output='{"proof":"retained"}',
        ok=True, run_id=run_id, task_id="task-1", force_externalize=True,
        result_envelope={"tool_execution": {"handler_executed": True, "duration_ms": 3},
            "tool_operation": {"operation_id": "op-proof", "status": "succeeded"}}))
    root = task / "work/blobs/tool_outputs"
    envelope = Path(record["output_path"])
    index = root / "index.jsonl"
    old_file(root / "control.bin", "unreferenced sibling")
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (1, 1))
    ref = {"owner_id": "owner-1", "run_id": run_id, "tool_call_id": "call-proof", "tool_name": "read_file"}
    verifier = LocalStoreToolEvidenceVerifier(object(), owner_id="owner-1", owner_root=home.owner_home_dir)
    verified = verifier.verify(ref)
    assert verified is not None and verified.successful
    before = {path: path.read_bytes() for path in (index, envelope, root / "control.bin")}
    plan = service.plan(now=NOW)
    assert any(action.path == root and action.category == "run_scratch" for action in plan.actions)
    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="仅ID引用也必须保留真实工具证据",
        subject_key="scratch.id.evidence", scope=MemoryScope("personal", "personal"),
        origin="tool_verified", promotion_target="lesson", source_tool_refs=(ref,)))
    candidates.transition(row.candidate_id, "approved", reviewer="test")
    errors = []
    pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
    assert complete and not errors
    assert pins.protects(index, []) and pins.protects(envelope, [])
    assert not pins.protects(root, []), "fixture must not accidentally contain a directory path pin"
    if not late:
        plan = service.plan(now=NOW)
        assert not any(action.path == root for action in plan.actions)
    report = execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert not any(action.path == root and action.status == "deleted" for action in report.actions)
    assert {path: path.read_bytes() for path in before} == before
    verified = verifier.verify(ref)
    assert verified is not None and verified.successful


# LLM: 候选仅保存调用ID，证据身份只在子JSON中；不能把目录根无路径引用误判为整棵可删。
# 函数用途: 验证task临时目录的按内容ID引用在新旧计划中都保护兄弟文件。
@pytest.mark.parametrize("late", [False, True])
def test_id_only_task_tmp_member_protects_whole_tree(tmp_path, late):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, service = runtime(tmp_path)
    root = home.owner_tasks_dir / "2026-10-01/sample/tmp"
    evidence = old_file(root / "nested/proof.json", json.dumps({"artifact_id": "artifact-proof"}))
    control = old_file(root / "a-control.bin", "whole tree must survive")
    plan = service.plan(now=NOW)
    assert any(action.path == root for action in plan.actions)
    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="任务子证据按ID保护",
        subject_key="scratch.task.id", scope=MemoryScope("personal", "personal"),
        origin="model_inferred", promotion_target="lesson", evidence_refs=({"artifact_id": "artifact-proof"},)))
    candidates.transition(row.candidate_id, "approved", reviewer="test")
    if not late:
        plan = service.plan(now=NOW)
        assert not any(action.path == root for action in plan.actions)
    execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert json.loads(evidence.read_text()) == {"artifact_id": "artifact-proof"}
    assert control.read_text() == "whole tree must survive"


# LLM: 异常类型前缀仅跳过该范围，不影响同消息其它合法范围或批内有效锚点，不写游标。
# 函数用途: 验证一条脏宿主ID不会使整批锚点收集抛错或扩域。
@pytest.mark.parametrize("source", ["thread", "message_task", "audit_task"])
@pytest.mark.parametrize("bad", ["session:", "project:task:x", "company:wrong"])
def test_curator_skips_invalid_anchor_scope_without_losing_valid_scopes(tmp_path, source, bad):
    from types import SimpleNamespace

    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.curator_inputs import (
        CuratorInputBatch,
        attach_candidate_anchors,
    )

    _home, candidates, _service = runtime(tmp_path)
    rows = [candidates.observe(CandidateObservation(candidate_type="lesson", content=f"合法锚点{scope}",
        subject_key=f"anchor.{scope}", scope=MemoryScope(scope, key), origin="model_inferred", promotion_target="lesson"))
        for scope, key in (("personal", "personal"), ("session", "session:valid-thread"), ("project", "project:valid-task"))]
    messages = [SimpleNamespace(thread_id="valid-thread", metadata={"task_id": "task:valid-task"})]
    events = []
    if source == "audit_task":
        events.append(SimpleNamespace(task_id=bad))
    else:
        messages.append(SimpleNamespace(thread_id=bad if source == "thread" else "valid-thread",
            metadata={"task_id": bad if source == "message_task" else "task:valid-task"}))
    batch = CuratorInputBatch(messages=tuple(messages), audit_events=tuple(events))
    result = attach_candidate_anchors(batch, candidates)
    assert {anchor["candidate_id"] for anchor in result.candidate_anchors} == {row.candidate_id for row in rows}
    assert result.messages == batch.messages and result.audit_events == batch.audit_events
