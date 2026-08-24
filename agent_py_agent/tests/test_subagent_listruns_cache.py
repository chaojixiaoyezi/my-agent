"""list_runs mtime 缓存测试:命中复用 + 副本独立(不污染)+ mtime 变重读 + 删除清理。

大数量子代理下 dispatch 每轮全量 list_runs 拖慢(实测 600 个 745ms);mtime 缓存让未变的
子代理只 stat 复用(实测省 ~88%)。这里钉死缓存正确性,尤其副本独立——改返回对象不污染缓存。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(enable_tools=False, memory_path="m.jsonl"), tmp_path)


def test_list_runs_cache_reuses_and_counts_stable(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    for i in range(3):
        agent.subagents.create_run(goal=f"t{i}", agent_name=f"a{i}")
    r1 = agent.subagents.list_runs()
    r2 = agent.subagents.list_runs()  # 第二次走缓存
    assert len(r1) == 3 and len(r2) == 3
    assert {t.id for t in r1} == {t.id for t in r2}


def test_list_runs_returns_independent_copies(tmp_path: Path) -> None:
    """改 list_runs 返回的对象不污染缓存——下次拿到的仍是磁盘真值。"""
    agent = _agent(tmp_path)
    agent.subagents.create_run(goal="t", agent_name="a")
    r1 = agent.subagents.list_runs()
    r1[0].status = "MUTATED"  # 改返回的副本
    r2 = agent.subagents.list_runs()
    assert r2[0].status != "MUTATED"  # 缓存没被污染


def test_list_runs_reloads_on_mtime_change(tmp_path: Path) -> None:
    """子代理被 save(mtime 变)后,list_runs 重读拿到新值,不返回缓存旧值。"""
    agent = _agent(tmp_path)
    rid = agent.subagents.create_run(goal="t", agent_name="a").id
    agent.subagents.list_runs()  # 预热缓存
    task = agent.subagents.load(rid)
    task.status = "RUNNING"
    agent.subagents.save(task)
    time.sleep(0.02)  # 确保 mtime 变化可感知
    reloaded = next(t for t in agent.subagents.list_runs() if t.id == rid)
    assert reloaded.status == "RUNNING"


def test_list_runs_evicts_deleted_run_from_cache(tmp_path: Path) -> None:
    """子代理从磁盘消失后,缓存项被清理,list_runs 不再返回它。"""
    agent = _agent(tmp_path)
    rid = agent.subagents.create_run(goal="t", agent_name="a").id
    agent.subagents.list_runs()  # 缓存它
    assert rid in agent.subagents.persistence._run_cache
    shutil.rmtree(agent.subagents.persistence.workspace / rid)
    assert agent.subagents.list_runs() == []
    assert rid not in agent.subagents.persistence._run_cache


def test_list_runs_by_ids_reads_only_selected_canonical_runs(tmp_path: Path) -> None:
    """活动投影的 exact-id 入口不扫描或缓存未选中的历史 run。"""
    agent = _agent(tmp_path)
    selected = agent.subagents.create_run(goal="selected", agent_name="selected").id
    ignored = agent.subagents.create_run(goal="ignored", agent_name="ignored").id

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
