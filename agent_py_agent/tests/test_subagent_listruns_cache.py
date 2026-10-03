"""list_runs mtime 缓存测试:命中复用 + 副本独立(不污染)+ mtime 变重读 + 删除清理。

大数量子代理下 dispatch 每轮全量 list_runs 拖慢(实测 600 个 745ms);mtime 缓存让未变的
子代理只 stat 复用(实测省 ~88%)。这里钉死缓存正确性,尤其副本独立——改返回对象不污染缓存。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(enable_tools=False, memory_path="m.jsonl"), tmp_path)


def _age_task_file(agent: SimpleAgent, run_id: str, age_seconds: int = 10) -> None:
    task_file = agent.subagents.workspace / run_id / "task.json"
    task_stat = task_file.stat()
    old_mtime_ns = time.time_ns() - age_seconds * 1_000_000_000
    os.utime(task_file, ns=(task_stat.st_atime_ns, old_mtime_ns))


def test_list_runs_cache_reuses_and_counts_stable(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    for i in range(3):
        run_id = agent.subagents.create_run(goal=f"t{i}", agent_name=f"a{i}").id
        _age_task_file(agent, run_id)
    r1 = agent.subagents.list_runs()
    r2 = agent.subagents.list_runs()  # 第二次走缓存
    assert len(r1) == 3 and len(r2) == 3
    assert {t.id for t in r1} == {t.id for t in r2}


def test_recent_window_read_is_not_cached_aba_probe(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.subagents.services.persistence import service as persistence_service

    agent = _agent(tmp_path)
    run_id = agent.subagents.create_run(goal="窗口内不缓存", agent_name="worker").id
    task_file = agent.subagents.workspace / run_id / "task.json"
    first_stat = task_file.stat()
    mtime_seconds = first_stat.st_mtime_ns / 1_000_000_000

    with monkeypatch.context() as clock:
        clock.setattr(persistence_service, "_now", lambda: mtime_seconds + 1.0)
        first_read = next(task for task in agent.subagents.list_runs() if task.id == run_id)
    cached_in_window = run_id in agent.subagents.persistence._run_cache
    assert first_read.status == "PLANNING"

    running = agent.subagents.load(run_id)
    running.status = "RUNNING"
    agent.subagents.save(running)
    saved_stat = task_file.stat()
    assert saved_stat.st_ino != first_stat.st_ino
    assert agent.subagents.load(run_id).status == "RUNNING"

    path_type = type(task_file)
    real_stat = path_type.stat

    def frozen_first_generation_stat(path, *args, **kwargs):
        if path == task_file:
            return first_stat
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as clock:
        clock.setattr(path_type, "stat", frozen_first_generation_stat)
        clock.setattr(persistence_service, "_now", lambda: mtime_seconds + 3.0)
        after_window = next(task for task in agent.subagents.list_runs() if task.id == run_id)

    assert after_window.status == "RUNNING"
    assert cached_in_window is False


def test_list_runs_returns_independent_copies(tmp_path: Path) -> None:
    """改 list_runs 返回的对象不污染缓存——下次拿到的仍是磁盘真值。"""
    agent = _agent(tmp_path)
    run_id = agent.subagents.create_run(goal="t", agent_name="a").id
    _age_task_file(agent, run_id)
    r1 = agent.subagents.list_runs()
    r1[0].status = "MUTATED"  # 改返回的副本
    r2 = agent.subagents.list_runs()
    assert r2[0].status != "MUTATED"  # 缓存没被污染


def test_list_runs_reloads_on_mtime_change(tmp_path: Path) -> None:
    """子代理被 save(mtime 变)后，窗口外的缓存读取应识别新指纹并重读。"""
    agent = _agent(tmp_path)
    run_id = agent.subagents.create_run(goal="t", agent_name="a").id
    task_file = agent.subagents.workspace / run_id / "task.json"
    _age_task_file(agent, run_id, age_seconds=10)
    cached_mtime_ns = task_file.stat().st_mtime_ns
    agent.subagents.list_runs()  # 在粗 mtime 窗口外建立旧缓存。
    task = agent.subagents.load(run_id)
    task.status = "RUNNING"
    agent.subagents.save(task)
    _age_task_file(agent, run_id, age_seconds=5)
    assert task_file.stat().st_mtime_ns != cached_mtime_ns
    reloaded = next(item for item in agent.subagents.list_runs() if item.id == run_id)
    assert reloaded.status == "RUNNING"


def test_list_runs_evicts_deleted_run_from_cache(tmp_path: Path) -> None:
    """子代理从磁盘消失后,缓存项被清理,list_runs 不再返回它。"""
    agent = _agent(tmp_path)
    rid = agent.subagents.create_run(goal="t", agent_name="a").id
    _age_task_file(agent, rid)
    agent.subagents.list_runs()  # 窗口外才应进入缓存。
    assert rid in agent.subagents.persistence._run_cache
    shutil.rmtree(agent.subagents.persistence.workspace / rid)
    assert agent.subagents.list_runs() == []
    assert rid not in agent.subagents.persistence._run_cache


def test_list_runs_by_ids_reads_only_selected_canonical_runs(tmp_path: Path) -> None:
    """活动投影的 exact-id 入口不扫描或缓存未选中的历史 run。"""
    agent = _agent(tmp_path)
    selected = agent.subagents.create_run(goal="selected", agent_name="selected").id
    ignored = agent.subagents.create_run(goal="ignored", agent_name="ignored").id
    _age_task_file(agent, selected)
    _age_task_file(agent, ignored)

    report = agent.subagents.list_runs_by_ids_report([selected, selected])

    assert [task.id for task in report.runs] == [selected]
    assert report.load_errors == []
    assert selected in agent.subagents.persistence._run_cache
    assert ignored not in agent.subagents.persistence._run_cache


def test_list_runs_by_ids_keeps_missing_run_as_structured_error(tmp_path: Path) -> None:
    """索引若指向已丢失记录，不能把它静默当成空列表。"""
    agent = _agent(tmp_path)

    report = agent.subagents.list_runs_by_ids_report(["subagent-missing"])

    assert report.runs == []
    assert len(report.load_errors) == 1
    assert report.load_errors[0]["run_id"] == "subagent-missing"


def test_list_runs_for_root_uses_index_then_exact_canonical_reads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """后台 root 查询不能为一棵小树复制其它历史 run。"""
    agent = _agent(tmp_path)
    selected = agent.subagents.create_run(goal="selected", agent_name="selected")
    selected.root_id = "root-selected"
    selected.parent_id = "root-selected"
    selected.depth = 1
    agent.subagents.save(selected)
    ignored = agent.subagents.create_run(goal="ignored", agent_name="ignored")
    ignored.root_id = "root-ignored"
    ignored.parent_id = "root-ignored"
    ignored.depth = 1
    agent.subagents.save(ignored)
    agent.subagents.persistence._run_cache.clear()
    _age_task_file(agent, selected.id)
    _age_task_file(agent, ignored.id)

    def fail_full_scan():
        raise AssertionError("managed root lookup must not scan every run")

    monkeypatch.setattr(agent.subagents.persistence, "list_runs_report", fail_full_scan)

    report = agent.subagents.list_runs_for_root_report("root-selected")

    assert [task.id for task in report.runs] == [selected.id]
    assert report.load_errors == []
    assert selected.id in agent.subagents.persistence._run_cache
    assert ignored.id not in agent.subagents.persistence._run_cache
