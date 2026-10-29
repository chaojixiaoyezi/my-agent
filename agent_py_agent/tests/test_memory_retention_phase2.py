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
