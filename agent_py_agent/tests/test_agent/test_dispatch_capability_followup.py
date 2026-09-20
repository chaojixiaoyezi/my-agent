"""LLM: tests for host-owned capability follow-up helpers and lifecycle fencing.

函数/模块用途: 验证底层能力续跑辅助信息，以及已取消任务不会被授权动作复活。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_followup import (
    _capability_followup_instruction,
    run_post_runner_capability_followup,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_dispatch_capability_followup_skips_when_router_missing():
    records = [SimpleNamespace(step="runner", action="ok")]
    result = run_post_runner_capability_followup(
        SimpleNamespace(),
        (
            SimpleNamespace(router=None, cfg=CapabilityConfig(), limit=20),
            SimpleNamespace(apply=True, start_runners=True),
            records,
        ),
    )

    assert result == records


def test_capability_followup_instruction_reports_task_load_error():
    def broken_load(_run_id):
        raise RuntimeError("task ledger unavailable")

    agent = SimpleNamespace(subagents=SimpleNamespace(load=broken_load))
    records = [SimpleNamespace(step="capability_route", action="granted", run_id="child-1")]

    instruction = _capability_followup_instruction(agent, records)

    assert "child-1" in instruction
    assert "dispatch_capability_followup.subagents.load" in instruction
    assert "task ledger unavailable" in instruction


def test_capability_followup_instruction_reports_next_actions_load_error(tmp_path: Path):
    next_actions = tmp_path / "next_actions.json"
    next_actions.write_text("{bad json", encoding="utf-8")
    task = SimpleNamespace(
        capability_grants=[SimpleNamespace(tools=["write_file"])],
        blockers=[],
        next_actions_json=str(next_actions),
        artifact_refs=[],
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda _run_id: task))
    records = [SimpleNamespace(step="capability_route", action="granted", run_id="child-1")]

    instruction = _capability_followup_instruction(agent, records)

    assert "next_actions_load_error" in instruction
    assert "dispatch_capability_followup.next_actions.load" in instruction


def test_capability_grant_does_not_resurrect_stopped_conversation_child():
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityGrantParams

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig( model_backend="echo", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(goal="等待能力后继续", allowed_tools=["read_file"])
        thread = agent.conversation_store.threads.get_or_create(
            {
                "canonical_user_id": "user-1",
                "channel": "feishu",
                "channel_conversation_id": "conversation-1",
                "channel_user_id": "user-1",
            }
        )
        agent.conversation_store.tasks.bind(
            {"thread_id": thread.thread_id, "task_id": task.id, "goal": task.goal}
        )
        blocked = agent.subagents.load(task.id)
        blocked.status = "BLOCKED"
        blocked.failure_type = "capability_request"
        agent.subagents.save(blocked)
        agent.conversation_store.tasks.update_status(
            {"task_id": task.id, "status": "blocked", "expected_status": "active"}
        )
        agent.conversation_store.tasks.update_status(
            {"task_id": task.id, "status": "cancelled", "expected_status": "blocked"}
        )

        agent.subagents.lifecycle.record_capability_grant(
            task.id,
            RecordCapabilityGrantParams(request_id="capreq-1", tools=["write_file"]),
        )

        link = agent.conversation_store.tasks.thread_for(task.id)
        assert link is not None
        links = {
            item.task_id: item for item in agent.conversation_store.tasks.list(thread.thread_id)
        }
        assert links[task.id].status == "cancelled"


def test_capability_grant_advances_settled_child_and_reopens_blocked_link():
    from agent_py_agent.agent.subagents.services.lifecycle import (
        RecordCapabilityGrantParams,
        RecordCapabilityRequestParams,
    )

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(goal="授权后续跑", allowed_tools=["read_file"])
        request = agent.subagents.lifecycle.record_capability_request(
            task.id,
            RecordCapabilityRequestParams(
                problem="需要写自己的任务目录",
                needed_capability="filesystem",
                requested_tools=["write_file"],
            ),
        )
        thread = agent.conversation_store.threads.get_or_create(
            {
                "canonical_user_id": "user-1",
                "channel": "feishu",
                "channel_conversation_id": "conversation-resume",
                "channel_user_id": "user-1",
            }
        )
        agent.conversation_store.tasks.bind(
            {"thread_id": thread.thread_id, "task_id": task.id, "goal": task.goal}
        )
        blocked = agent.subagents.load(task.id)
        blocked.status = "BLOCKED"
        blocked.failure_type = "capability_request"
        blocked.runner_attempts = 1
        blocked.runner_active_attempt_id = ""
        agent.subagents.save(blocked)
        agent.conversation_store.tasks.update_status(
            {"task_id": task.id, "status": "blocked", "expected_status": "active"}
        )

        agent.subagents.lifecycle.record_capability_grant(
            task.id,
            RecordCapabilityGrantParams(request_id=request.id, tools=["write_file"]),
        )

        loaded = agent.subagents.load(task.id)
        links = {
            item.task_id: item for item in agent.conversation_store.tasks.list(thread.thread_id)
        }
        assert loaded.status == "PENDING"
        assert loaded.failure_type == ""
        assert loaded.capability_requests[0].status == "GRANTED"
        assert loaded.runner_active_attempt_id == ""
        assert links[task.id].status == "active"
