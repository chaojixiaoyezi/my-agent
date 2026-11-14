"""R1：真实技能引用、调度记录与模型回合，防止只读监控阻断主链或重复扫描。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets import monitor_adapter as monitors
from agent_py_agent.agent.assets import skill_asset_id
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
from agent_py_agent.agent.assets.workspace_assets import WorkspaceAssetOverlay
from agent_py_agent.agent.capability.decision_candidates import (
    asset_cards_for_agent,
    method_descriptors_for_agent,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
)
from agent_py_agent.agent.scheduler.tool import _skill_refs
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.tests.test_heavy_user_cache_and_recommendation_gate import (
    _CaptureProvider,
    _heavy_case,
    _run_turn,
    _write_json,
)


def _logical_skill_case(tmp_path, name):
    """所有样本经真实发现和仓库创建，供应商之外不替代产品实现。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"),
        memory_path=str(tmp_path / "memory.jsonl"), enable_tools=True,
        enable_self_learning=False, prompt_files=[], model_context_window_tokens=262144,
        max_tokens=128, memory_compact_remote_enabled=False,
    ), workspace)
    provider = _CaptureProvider()
    agent.backend = provider
    home = agent.home_paths.owner_home_dir
    _write_json(agent.home_paths.owner_skill_policy_json,
                {"enabled": True, "enabled_sources": ["owner"]})
    for skill_name in ("triage", name):
        path = home / "skills" / skill_name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            f"---\nname: {json.dumps(skill_name, ensure_ascii=False)}\n"
            "description: 核对来源与证据\n---\n# TRIAGE_BODY\n只读取正式来源。\n",
            encoding="utf-8",
        )
    agent.skills_service.clear_cache()
    refs = _skill_refs(agent, [f"owner:{name}"])
    assert isinstance(refs, list) and len(refs) == 1
    assert refs[0]["stable_id"] == f"owner:{name}"
    repository = SchedulerRepository(home / "data/scheduler", owner_provider="local",
                                     owner_kind="main", owner_id="local/main")
    job, _ = repository.create_job(SchedulerJobCreateRequest(
        name="技能监控", prompt="核对来源", thread_id="thread_fixture",
        schedule={"kind": "every", "every_seconds": 3600, "anchor_at": 1000, "timezone": "UTC"},
        skill_refs=refs, now=1000,
    ))
    thread = agent.conversation_store.threads.get_or_create(
        {"channel": "test", "channel_conversation_id": "r1"})
    return agent, provider, thread, job


@pytest.mark.parametrize("name", ["Daily Report", "\u65e5\u62a5"])
def test_logical_skill_monitor_allows_real_turn(tmp_path, monkeypatch, name):
    agent, provider, thread, job = _logical_skill_case(tmp_path, name)
    tool = agent.tools.tools["skill_search"]
    execute = tool.execute
    outcomes = []

    def capture(*args, **kwargs):
        outcome = execute(*args, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(tool, "execute", capture)
    try:
        calls = _run_turn(agent, provider, thread, "logical-skill", "核对来源",
                          actions=(("skill_search", {"action": "get", "skill_id": "owner:triage"}),))
    finally:
        print(f"R1_TURN name={name!r} provider_requests={len(provider.calls)}")
    assert len(calls) == 2
    assert "catalog_state: degraded" not in calls[0]["layout"].stable_prefix
    assert len(outcomes) == 1 and outcomes[0].ok, [outcome.output for outcome in outcomes]
    assert "TRIAGE_BODY" in outcomes[0].output
    rows = monitors.list_monitor_descriptors(agent.home_paths.owner_home_dir)
    assert len(rows) == 1 and rows[0]["id"] == job["job_id"]
    assert rows[0]["metadata"]["asset_refs"]["skills"] == [skill_asset_id(name)]
    descriptors = method_descriptors_for_agent(agent)
    assert next(row["id"] for row in descriptors
                if row["kind"] == "skill" and row["name"] == name) == skill_asset_id(name)


@pytest.mark.parametrize("tool_name,identifier,expected", [
    ("workflows", "Patrol Flow", []),
    ("scripts", "Daily Script", []),
    ("workflows", "../flow", []),
    ("scripts", "\u65e5\u62a5", []),
    ("workflows", "", []),
    ("scripts", None, []),
    ("workflows", "safe-flow", [{"kind": "workflow", "id": "safe-flow"}]),
])
def test_rule_tool_target_projection(tmp_path, tool_name, identifier, expected):
    repository = SchedulerRepository(tmp_path / "data/scheduler", owner_provider="local",
                                     owner_kind="main", owner_id="local/main")
    schedule = {"kind": "every", "every_seconds": 3600, "anchor_at": 1000, "timezone": "UTC"}
    argument = "workflow_id" if tool_name == "workflows" else "script_id"
    job, _ = repository.create_job(SchedulerJobCreateRequest(
        name="规则监控", prompt="", thread_id="thread_fixture", schedule=None, now=1000,
        rules=[{"id": "patrol", "name": "巡检", "enabled": True, "use_llm": False,
                "schedule": schedule,
                "tool": {"name": tool_name, "arguments": {"action": "run", argument: identifier}}}],
    ))
    rows = monitors.list_monitor_descriptors(tmp_path)
    assert len(rows) == 1 and rows[0]["id"] == job["job_id"]
    assert rows[0]["refs"] == expected


@pytest.mark.parametrize("name", ["Daily Report", "\u65e5\u62a5"])
def test_unrelated_asset_tools_with_logical_skill_monitor(tmp_path, name):
    agent, _, _, _ = _logical_skill_case(tmp_path, name)
    home = agent.home_paths.owner_home_dir
    ScriptStore(home).save_script(AssetDescriptor("unrelated", "script", "无关脚本"), "print(1)\n")
    WorkflowStore(home).save_workflow(WorkflowDefinition(
        AssetDescriptor("unrelated", "workflow", "无关流程"), (), body_markdown="# 无关流程\n"))
    scripts, workflows = ScriptsTool(home), WorkflowsTool(home)
    for tool, arguments in (
        (scripts, {"action": "get", "script_id": "unrelated"}),
        (scripts, {"action": "update", "script_id": "unrelated", "code": "print(2)\n"}),
        (workflows, {"action": "get", "workflow_id": "unrelated"}),
    ):
        result = tool.execute(arguments)
        assert result.ok, result.output
        assert "TOOL_INVALID_ARGUMENTS" not in result.output
    assert ScriptStore(home).get_script("unrelated")["code"] == "print(2)\n"


def test_per_call_scan_budget(tmp_path, monkeypatch):
    agent, provider, _, _ = _heavy_case(tmp_path, (20, 30, 60))
    counts = {"list_assets": 0, "list_monitor_descriptors": 0}
    list_assets = WorkspaceAssetOverlay.list_assets
    list_monitors = monitors.list_monitor_descriptors

    def count_assets(*args, **kwargs):
        counts["list_assets"] += 1
        return list_assets(*args, **kwargs)

    def count_monitors(*args, **kwargs):
        counts["list_monitor_descriptors"] += 1
        return list_monitors(*args, **kwargs)

    monkeypatch.setattr(WorkspaceAssetOverlay, "list_assets", count_assets)
    monkeypatch.setattr(monitors, "list_monitor_descriptors", count_monitors)
    # p09 的单回合入口；带 conversation_thread_id 的持久工具挂载另有一次库存读取。
    result = agent.run("核对当前步骤", params=RunParams(save=False))
    assert result.response == "GATE1完成"
    assert len(provider.calls) == 1
    print("R1_SCAN_COUNTS " + json.dumps(counts, sort_keys=True))
    assert counts["list_assets"] <= 6, counts
    assert counts["list_monitor_descriptors"] <= 2, counts


@pytest.mark.parametrize("reference,expected", [
    ("Daily Report", [skill_asset_id("Daily Report")]),
    ("\u65e5\u62a5", [skill_asset_id("\u65e5\u62a5")]),
    ("", []),
    ({"stable_id": "owner:"}, []),
    ({"stable_id": "owner:triage"}, ["triage"]),
    ({"stable_id": "capability:example"}, []),
    ({"stable_id": "owner:example", "kind": "capability_package"}, []),
])
def test_skill_reference_projection(reference, expected):
    assert monitors._scheduler_skill_ids([reference]) == expected


def _tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _fd_count():
    # 测量当前进程，不把文件枚举迭代器留在测量窗口之外。
    for root in (Path("/proc/self/fd"), Path("/dev/fd")):
        if root.is_dir():
            return len(os.listdir(root))
    from ctypes import byref, c_ulong, windll
    count = c_ulong()
    assert windll.kernel32.GetProcessHandleCount(windll.kernel32.GetCurrentProcess(), byref(count))
    return count.value


def test_projection_preserves_source_bytes_and_descriptors(tmp_path):
    agent, _, _, _ = _logical_skill_case(tmp_path, "\u65e5\u62a5")
    home = agent.home_paths.owner_home_dir
    monitors.save_monitor_binding(home, "binding", name="只读绑定")
    # 先准备真实只读 SQLite 的 WAL/SHM 设施，再比较包括辅助文件在内的完整树。
    # 持有读连接固定设施寿命，不给投影增加任何生产写入路径。
    index = home / "data/scheduler/history.index.sqlite3"
    fd_before = _fd_count()
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("SELECT COUNT(*) FROM history_state").fetchone()
        before = _tree_bytes(home)
        for _ in range(5):
            assert len(monitors.list_monitor_descriptors(home)) == 2
            assert len(method_descriptors_for_agent(agent)) == 4
            assert asset_cards_for_agent(agent) == []
        assert _tree_bytes(home) == before
    # SQLite 可在其它连接仍存活时延迟归还共享 FD；连同 fixture 租约一起关闭后再对账。
    fd_delta = _fd_count() - fd_before
    print(f"R1_PROJECTION fd_delta={fd_delta}")
    assert fd_delta == 0


def test_corrupt_monitor_badges_do_not_remove_methods(tmp_path):
    agent, _, _, _ = _logical_skill_case(tmp_path, "triage-extra")
    home = agent.home_paths.owner_home_dir
    ScriptStore(home).save_script(AssetDescriptor("keep", "script", "保留方法"), "print(1)\n")
    monitors.save_monitor_binding(home, "broken")
    (home / "data/monitors/broken.json").write_text("[]", encoding="utf-8")
    before = _tree_bytes(home)
    rows = method_descriptors_for_agent(agent)
    assert {row["kind"] for row in rows} == {"skill", "script"}
    assert [card.id for card in asset_cards_for_agent(agent)] == ["script:owner:keep"]
    assert _tree_bytes(home) == before
