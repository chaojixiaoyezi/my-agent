from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.orchestration.dispatch.tool import DispatchSubagentsTool
from agent_py_agent.agent.agent_core.runtime.guidance import (
    inject_pending_guidance,
    render_subagent_guidance_section,
)
from agent_py_agent.agent.agent_core.runtime.guidance_tool import SendGuidanceTool
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.core import SimpleAgent


def _tool_loop_params(**overrides) -> ToolLoopExecuteParams:
    params = ToolLoopExecuteParams(
        user_prompt="继续完成任务",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="main-run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
    for key, value in overrides.items():
        params = replace(params, **{key: value})
    return params


def test_conversation_guidance_can_be_delivered_once(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )

    entry = store.append_guidance(
        {
            "target_type": "thread",
            "target_id": thread.thread_id,
            "message": "请先汇总已有产物，再继续补缺口。",
            "sender": "user",
            "now": 2.0,
        }
    )

    pending = store.pending_guidance("thread", thread.thread_id)
    assert [item.guidance_id for item in pending] == [entry.guidance_id]
    assert pending[0].message == "请先汇总已有产物，再继续补缺口。"

    store.mark_guidance_delivered([entry.guidance_id], now=3.0)

    assert store.pending_guidance("thread", thread.thread_id) == []
    delivered = store.recent_guidance("thread", thread.thread_id)
    assert delivered[0].delivered_at == 3.0


def test_send_guidance_tool_writes_run_guidance(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    result = SendGuidanceTool(agent).execute(
        {
            "target": {"type": "agent_run", "id": "child-1"},
            "message": "换一个数据来源核对，不要重复查同一个页面。",
            "priority": "high",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["target"]["type"] == "agent_run"
    assert payload["target"]["id"] == "child-1"
    pending = agent.conversation_store.pending_guidance("agent_run", "child-1")
    assert pending[0].message == "换一个数据来源核对，不要重复查同一个页面。"
    assert pending[0].priority == "high"


def test_send_guidance_tool_can_target_direct_child_scope(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="", plan=["root"])
    child_a = agent.subagents.create_run(goal="a", thought="", plan=["a"], parent_id=root.id, root_id=root.id, depth=1)
    child_b = agent.subagents.create_run(goal="b", thought="", plan=["b"], parent_id=root.id, root_id=root.id, depth=1)
    grandchild = agent.subagents.create_run(
        goal="grandchild",
        thought="",
        plan=["grandchild"],
        parent_id=child_a.id,
        root_id=root.id,
        depth=2,
    )

    result = SendGuidanceTool(agent).execute(
        {
            "target_scope": "children",
            "run_id": root.id,
            "message": "先按新要求补证据，完成后继续原任务。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["target"]["type"] == "agent_run"
    assert sorted(item["id"] for item in payload["targets"]) == sorted([child_a.id, child_b.id])
    assert agent.conversation_store.pending_guidance("agent_run", child_a.id)
    assert agent.conversation_store.pending_guidance("agent_run", child_b.id)
    assert agent.conversation_store.pending_guidance("agent_run", root.id) == []
    assert agent.conversation_store.pending_guidance("agent_run", grandchild.id) == []


def test_send_guidance_scope_resolution_failure_does_not_target_parent(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="", plan=["root"])

    def broken_kernel_snapshot(query):
        del query
        raise ValueError("broken kernel")

    monkeypatch.setattr(agent.subagents, "kernel_snapshot", broken_kernel_snapshot)

    result = SendGuidanceTool(agent).execute(
        {
            "target_scope": "children",
            "run_id": root.id,
            "message": "请所有孩子补充证据。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "target_scope_resolution_failed"
    assert payload["load_error"]["context"] == "send_guidance.target_scope"
    assert agent.conversation_store.pending_guidance("agent_run", root.id) == []


def test_cli_guidance_send_writes_same_guidance_inbox(tmp_path, capsys) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent_py_agent.cli.guidance_commands import cmd_guidance_send

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    args = SimpleNamespace(
        run_id="child-1",
        thread_id="",
        task_id="",
        case_id="",
        target_type="",
        target_id="",
        message="用户补充：先写草稿，不要一直只读。",
        sender="cli_user",
        priority="normal",
        delivery="next_turn",
        json=False,
    )

    with patch("agent_py_agent.cli.guidance_commands.make_agent", return_value=agent):
        code = cmd_guidance_send(args)

    assert code == 0
    assert "已追加提示" in capsys.readouterr().out
    pending = agent.conversation_store.pending_guidance("agent_run", "child-1")
    assert pending[0].message == "用户补充：先写草稿，不要一直只读。"


def test_tool_loop_injects_pending_guidance_and_marks_delivered(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.append_guidance(
        {
            "target_type": "agent_run",
            "target_id": "main-run-1",
            "message": "先写一个可打开的草稿，再继续完善。",
            "now": 10.0,
        }
    )
    params = _tool_loop_params(run_id="main-run-1")

    updated = inject_pending_guidance(agent, params, now=11.0)

    assert updated is True
    assert any("GUIDANCE_DELIVERED" in str(item) for item in params.tool_context)
    assert any("guidance_id=" in str(item) for item in params.tool_context)
    assert any("先写一个可打开的草稿" in str(item) for item in params.tool_context)
    assert agent.conversation_store.pending_guidance("agent_run", "main-run-1") == []


def test_tool_loop_reports_thread_guidance_lookup_error(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    params = _tool_loop_params(task_id="task-1")

    def broken_thread_for_task(task_id):
        del task_id
        raise OSError("thread binding index missing")

    monkeypatch.setattr(agent.conversation_store, "thread_for_task", broken_thread_for_task)

    updated = inject_pending_guidance(agent, params, now=11.0)

    assert updated is True
    assert any("GUIDANCE_LOOKUP_WARNING" in str(item) for item in params.tool_context)
    assert any("runtime_guidance.thread_for_task" in str(item) for item in params.tool_context)


def test_subagent_runner_prompt_can_render_pending_guidance(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.append_guidance(
        {
            "target_type": "agent_run",
            "target_id": "child-1",
            "message": "上级补充：把命中和未命中都写清楚。",
            "now": 20.0,
        }
    )

    section = render_subagent_guidance_section(store, "child-1", now=21.0)

    assert "上级补充：把命中和未命中都写清楚。" in section
    assert store.pending_guidance("agent_run", "child-1") == []


def test_real_subagent_runner_prompt_includes_guidance(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    child = agent.subagents.create_run(goal="child", thought="", plan=["child"])
    agent.conversation_store.append_guidance(
        {
            "target_type": "agent_run",
            "target_id": child.id,
            "message": "先写阶段文件，再继续扩展。",
        }
    )

    _, prompt = agent._build_subagent_prompt(child.id, 0, "")

    assert "GUIDANCE_DELIVERED" in prompt
    assert "先写阶段文件，再继续扩展。" in prompt
    assert agent.conversation_store.pending_guidance("agent_run", child.id) == []


def test_dispatch_runner_instruction_writes_guidance_for_explicit_run(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    child = agent.subagents.create_run(goal="child", thought="", plan=["child"])
    agent.dispatch_subagents = MagicMock(
        return_value=SimpleNamespace(dry_run=False, summary={"ok": True}, records=[])
    )

    result = DispatchSubagentsTool(agent).execute(
        {
            "run_ids": [child.id],
            "dry_run": False,
            "runner_instruction": "先汇总已有文件，再继续补缺口。",
        }
    )

    assert result.ok is True
    pending = agent.conversation_store.pending_guidance("agent_run", child.id)
    assert pending[0].message == "先汇总已有文件，再继续补缺口。"
    assert pending[0].metadata["legacy_tool"] == "dispatch_subagents"


def test_dispatch_runner_instruction_reports_guidance_persist_error(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    child = agent.subagents.create_run(goal="child", thought="", plan=["child"])
    agent.dispatch_subagents = MagicMock(
        return_value=SimpleNamespace(dry_run=False, summary={"ok": True}, records=[])
    )

    def fail_append_guidance(_payload):
        raise RuntimeError("guidance store unavailable")

    monkeypatch.setattr(agent.conversation_store, "append_guidance", fail_append_guidance)

    result = DispatchSubagentsTool(agent).execute(
        {
            "run_ids": [child.id],
            "dry_run": False,
            "runner_instruction": "先汇总已有文件，再继续补缺口。",
        }
    )

    payload = json.loads(result.output)
    assert result.ok is True
    assert payload["guidance_persist_errors"][0]["run_id"] == child.id
    assert payload["guidance_persist_errors"][0]["context"] == "dispatch.guidance.persist"
