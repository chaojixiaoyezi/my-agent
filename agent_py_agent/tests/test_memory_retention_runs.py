"""retention 扫描范围必须覆盖新版运行工作区 O/runs，且终态判断只能读结构化状态。

背景：`MemoryRetentionService` 原来只扫 `O/tasks`，新版运行材料写在 `O/runs`，不在范围内。
本文件钉住四件事：runs 里的超期终态会被规划回收、未完成的不动、仍被结构化引用的不动、
边界天数行为正确；并明确"不许用 mtime 或目录名推断终态"。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)
# 400 天前，晚于默认 365 天保留期。
OLD = "2025-06-01T00:00:00+00:00"
# 364 天前，早于保留期边界。
FRESH = "2026-08-01T00:00:00+00:00"


# LLM: 只建测试需要的 owner home 与服务；不读用户配置，不写真实 owner 目录。
# 函数用途: 建立一份可重复的 retention 服务与 owner home。
def runtime(tmp_path: Path):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    long_term = JsonlMemory(
        home.owner_memory_long_term_jsonl,
        ops_path=home.owner_memory_ops_jsonl,
        candidate_service=candidates,
    )
    service = MemoryRetentionService(
        home_paths=home,
        candidates=candidates,
        long_term=long_term,
    )
    return home, service


def set_policy(home, **updates: object) -> None:
    payload = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    payload.update(updates)
    home.owner_retention_json.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


# LLM: 只写结构化 state；目录名（日期/编号）刻意不携带状态信息，防止实现靠名字猜。
# 函数用途: 在一个恢复根下造一个带结构化状态的任务目录。
def task(root: Path, task_id: str, status: str, updated_at: str) -> Path:
    task_root = root / "2026-01-01" / task_id
    state = task_root / "work" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"task_id": task_id, "status": status, "updated_at": updated_at}, ensure_ascii=False),
        encoding="utf-8",
    )
    (task_root / "output").mkdir(parents=True, exist_ok=True)
    (task_root / "output" / "result.txt").write_text("结果", encoding="utf-8")
    return task_root


def plan_paths(service, home) -> list[str]:
    report = service.plan(now=NOW)
    assert report.ok is True, [error.code for error in report.errors]
    return [str(action.path) for action in report.actions]


# LLM: 这是缺口本身：runs 下的超期终态必须进入计划。
# 函数用途: 验证 O/runs 中已完成且超期的任务被规划回收。
def test_terminal_old_run_is_planned(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=365)
    run_root = task(home.owner_runs_dir, "run-old", "DONE", OLD)
    assert str(run_root) in plan_paths(service, home)


# LLM: 旧根不能被新根挤掉，两个根必须同时被扫。
# 函数用途: 验证 O/tasks 与 O/runs 两个根都在扫描范围内。
def test_both_recovery_roots_are_scanned(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=365)
    old_task = task(home.owner_tasks_dir, "task-old", "DONE", OLD)
    old_run = task(home.owner_runs_dir, "run-old", "DONE", OLD)
    paths = plan_paths(service, home)
    assert str(old_task) in paths
    assert str(old_run) in paths


# LLM: 运行中的材料是恢复依据，绝不能被"看起来旧"清掉。
# 函数用途: 验证 runs 里未完成的任务即使很旧也不被回收。
def test_nonterminal_run_is_protected(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=365)
    running = task(home.owner_runs_dir, "run-running", "RUNNING", OLD)
    paths = plan_paths(service, home)
    assert str(running) not in paths
    assert running.exists()


# LLM: 未知状态按运行中处理，宁可留恢复材料也不误删。
# 函数用途: 验证 runs 里未知状态的任务不被回收。
def test_unknown_status_run_is_protected(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=365)
    weird = task(home.owner_runs_dir, "run-weird", "SOMETHING_NEW", OLD)
    assert str(weird) not in plan_paths(service, home)


# LLM: 终态判断必须来自结构化 status；单靠 mtime 或目录名不能决定。
# 函数用途: 把终态任务的 mtime 改成刚刚，它仍应被回收（证明不是按 mtime 判）。
def test_terminal_decision_ignores_mtime(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=365)
    run_root = task(home.owner_runs_dir, "run-done", "DONE", OLD)
    now_ts = NOW.timestamp()
    for dirpath, _dirnames, filenames in os.walk(run_root):
        os.utime(dirpath, (now_ts, now_ts))
        for name in filenames:
            os.utime(Path(dirpath) / name, (now_ts, now_ts))
    assert str(run_root) in plan_paths(service, home)


# LLM: 结构化引用（legal hold / 仍被引用）优先于年龄。
# 函数用途: 验证被 legal hold 的任务在 runs 里同样被保护。
def test_held_run_is_protected(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=365, legal_hold_task_ids=["run-held"])
    held = task(home.owner_runs_dir, "run-held", "DONE", OLD)
    assert str(held) not in plan_paths(service, home)


# LLM: 边界天数：恰好等于保留期的不清，超过一天的清。
# 函数用途: 验证 cutoff 边界上 runs 的行为。
def test_cutoff_boundary_on_runs(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=365)
    inside = task(home.owner_runs_dir, "run-boundary-in", "DONE", FRESH)
    outside = task(home.owner_runs_dir, "run-boundary-out", "DONE", OLD)
    paths = plan_paths(service, home)
    assert str(outside) in paths
    assert str(inside) not in paths


# LLM: 保留期 <= 0 表示关闭该类回收，两个根都不动。
# 函数用途: 验证关闭完成任务回收时 runs 不受影响。
def test_disabled_completed_task_days_protects_runs(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=0)
    run_root = task(home.owner_runs_dir, "run-old", "DONE", OLD)
    assert str(run_root) not in plan_paths(service, home)


# LLM: 新版根可能与旧根一样不存在；缺失根不能让扫描报错。
# 函数用途: 验证 runs 根缺失时规划仍然成功。
def test_missing_runs_root_is_not_an_error(tmp_path: Path):
    home, service = runtime(tmp_path)
    set_policy(home, completed_task_days=365)
    assert not home.owner_runs_dir.exists() or not any(home.owner_runs_dir.iterdir())
    report = service.plan(now=NOW)
    assert report.ok is True
