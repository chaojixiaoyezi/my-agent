"""schedule_child_subagents runner-context regression tests."""
from __future__ import annotations

import json
import threading

from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: test_runner_context_schedule_bare_lineage_name_returns_payload_not_index_error guards R32 prefix-only names.
# 函数用途: 验证真实模型只写“小小傻妞”这类层级前缀时，runner 工具会补稳定后缀并返回 JSON，而不是抛裸 IndexError。
def test_runner_context_schedule_bare_lineage_name_returns_payload_not_index_error(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    child = agent.subagents.create_run(
        goal="child",
        thought="child",
        plan=["child"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )
    agent._current_subagent_run_id = child.id
    tool = ScheduleChildSubagentsTool(agent)

    result = tool.execute(
        {
            "dry_run": False,
            "max_depth": 2,
            "children": [{"goal": "继续协调页面任务", "role": "coordinator", "agent_name": "小小傻妞"}],
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["created_run_ids"]
    assert agent.subagents.load(payload["created_run_ids"][0]).agent_name == "小小傻妞-coordinator-1"


def test_runner_context_schedule_auto_starts_created_children(tmp_path, monkeypatch):
    """runner 内创建下一层后应像 create_subagents 一样后台启动，并返回状态。"""
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    agent._current_subagent_run_id = root.id
    launched: list[str] = []

    def fake_start(_agent, run_ids):
        launched.extend(run_ids)
        return {"status": "started", "dispatch_mode": "background", "run_ids": list(run_ids)}

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration_background_dispatch._start_background_dispatch",
        fake_start,
    )

    result = ScheduleChildSubagentsTool(agent).execute({
        "children": [{"goal": "查一份文件并写结果", "role": "worker", "agent_name": "reader"}]
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["created_run_ids"]
    assert launched == payload["created_run_ids"]
    assert payload["auto_start"]["status"] == "started"
    assert payload["auto_start"]["run_ids"] == payload["created_run_ids"]


def test_runner_context_schedule_hides_legacy_subagent_paths(tmp_path, monkeypatch):
    """schedule_child_subagents 返回给模型的 payload 不应暴露旧 data/subagents 路径。"""
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="data/subagents"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    agent._current_subagent_run_id = root.id

    def fake_start(_agent, run_ids):
        return {
            "status": "started",
            "dispatch_mode": "background",
            "run_ids": list(run_ids),
            "agent_tree": {
                "nodes": [
                    {
                        "run_id": run_ids[0],
                        "workspace_refs": {
                            "final_report": str(tmp_path / "data" / "subagents" / "tasks" / root.id / "report.md")
                        },
                    }
                ]
            },
        }

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration_background_dispatch._start_background_dispatch",
        fake_start,
    )

    result = ScheduleChildSubagentsTool(agent).execute({
        "children": [{"goal": "查一份文件并写结果", "role": "worker", "agent_name": "reader"}]
    })

    assert result.ok is True
    assert "/data/subagents/" not in result.output
    assert "[internal_legacy_subagent_path_hidden]" not in result.output


def test_auto_start_dispatch_keeps_current_runner_parent_scope(tmp_path):
    """后台启动参数要保留当前 runner 作用域，避免越过父子边界。"""
    from agent_py_agent.agent.agent_core.orchestration_background_dispatch import (
        _auto_start_dispatch_args,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    agent._current_subagent_run_id = root.id

    _, _, params = _auto_start_dispatch_args(agent, ["child-run"])

    assert params.parent_run_id == root.id
    assert params.include_run_ids == ["child-run"]


def test_runner_context_is_thread_local_for_background_autostart(tmp_path):
    """后台 runner 身份不能污染父线程后续 create_subagents 的作用域。"""
    from agent_py_agent.agent.agent_core.orchestration_background_dispatch import (
        _auto_start_dispatch_args,
    )
    from agent_py_agent.agent.agent_core.runner_context import (
        current_subagent_run_id,
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    started = threading.Event()
    release = threading.Event()
    worker_result: dict[str, str] = {}

    def worker() -> None:
        previous = set_current_subagent_context(agent, run_id=root.id, attempt_id="attempt-1")
        try:
            started.set()
            release.wait(timeout=2)
            _, _, params = _auto_start_dispatch_args(agent, ["grandchild-run"])
            worker_result["current"] = current_subagent_run_id(agent)
            worker_result["parent"] = params.parent_run_id
        finally:
            restore_current_subagent_context(agent, previous)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(timeout=2)

    assert current_subagent_run_id(agent) == ""
    _, _, params = _auto_start_dispatch_args(agent, ["sibling-run"])
    assert params.parent_run_id == ""

    release.set()
    thread.join(timeout=2)
    assert worker_result == {"current": root.id, "parent": root.id}


# LLM: schedule_child_subagents without children can return LLM advice instead of forcing a fixed flow.
# 函数用途: worker 已可测试但模型还没决定 QA 波次时，工具返回 quality_advice，让 LLM 选择 tester/bug_finder。
def test_runner_context_schedule_without_children_returns_quality_advice(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    build = tmp_path / "deliverables" / "shop" / "build"
    root = agent.subagents.create_run(
        goal="示例网站需要 tester / bug_finder，但由 LLM 决定 QA scope。",
        thought="root",
        plan=["root"],
        role="coordinator",
        extra_write_roots=[str(build)],
        attributes={"required_qa_roles": ["tester", "bug_finder", "bug_finder"]},
    )
    worker = agent.subagents.create_run(
        goal=f"实现示例网站到 {build}",
        thought="work",
        plan=["write"],
        parent_id=root.id,
        root_id=root.id,
        role="worker",
        extra_write_roots=[str(build)],
    )
    worker.status = "DONE"
    worker.verification_status = "VERIFIED"
    agent.subagents.save(worker)
    agent._current_subagent_run_id = root.id

    result = ScheduleChildSubagentsTool(agent).execute({"dry_run": False})
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["blocked"] is False
    assert payload["reason"] == "created"
    assert payload["quality_advice"]["phase"] == "quality_wave_ready"
    assert set(payload["quality_advice"]["suggested_roles"]) == {"tester", "bug_finder"}
    assert payload["quality_advice"]["ready_work_refs"][0]["run_id"] == worker.id
    assert payload["quality_advice"]["suggested_children"][0]["source_run_ids"] == [worker.id]
    assert payload["created_run_ids"] == []
