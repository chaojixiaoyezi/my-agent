"""LLM: tests for dispatch capability-request follow-up reruns.

函数/模块用途: 验证 runner 写出能力申请后，同一次 dispatch 能先授权再重跑 worker。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_followup import (
    _capability_followup_instruction,
    run_post_runner_capability_followup,
)
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig

from .backends import CapabilityThenAcceptedBackend, IncompleteOutputThenAcceptedBackend


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


def test_dispatch_routes_new_capability_request_then_reruns_worker(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = CapabilityThenAcceptedBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="需要受控 shell 能力，授权后再继续执行并报告 refs。",
            thought="先申请授权，再执行。",
            plan=["申请能力", "授权后执行", "等待收口"],
            allowed_tools=["write_file"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            start_runners=True,
            max_runners=1,
            probe=False,
            reviewer="dispatch-test",
        )
        loaded = agent.subagents.load(task.id)

        assert backend.calls == 2
        assert [item.step for item in report.records].count("runner") == 2
        assert any(item.step == "capability_route" and item.action == "granted" for item in report.records)
        assert loaded.capability_requests[0].status == "GRANTED"
        assert loaded.capability_requests[0].requested_commands == ["pwd", "rm"]
        assert loaded.capability_grants[0].command_allowlist == ["pwd"]
        assert "controlled_exec" in loaded.allowed_tools
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"


def test_capability_grant_reopens_exact_blocked_conversation_child(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = CapabilityThenAcceptedBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="需要受控 shell 能力，授权后继续同一个子任务。",
            thought="先申请授权，再执行。",
            plan=["申请能力", "授权后执行", "等待收口"],
            allowed_tools=["write_file"],
        )
        thread = agent.conversation_store.get_or_create_thread(
            {
                "canonical_user_id": "user-1",
                "channel": "feishu",
                "channel_conversation_id": "conversation-1",
                "channel_user_id": "user-1",
            }
        )
        root_task_id = "req-root"
        agent.conversation_store.bind_task(
            {"thread_id": thread.thread_id, "task_id": root_task_id, "goal": "根任务"}
        )
        current = agent.subagents.load(task.id)
        current.parent_id = root_task_id
        current.root_id = root_task_id
        current.attributes.update(
            {
                "conversation_thread_id": thread.thread_id,
                "conversation_task_id": root_task_id,
            }
        )
        agent.subagents.save(current)
        agent.conversation_store.bind_task(
            {"thread_id": thread.thread_id, "task_id": task.id, "goal": task.goal}
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            start_runners=True,
            max_runners=1,
            probe=False,
            reviewer="dispatch-test",
        )
        loaded = agent.subagents.load(task.id)

        assert backend.calls == 2
        assert [item.step for item in report.records].count("runner") == 2
        assert not any(
            item.action == "conversation_lifecycle_blocked" for item in report.records
        )
        assert loaded.status == "DONE"
        child_links = {
            link.task_id: link for link in agent.conversation_store.task_links(thread.thread_id)
        }
        assert child_links[task.id].status == "DONE"


def test_capability_grant_does_not_resurrect_stopped_conversation_child():
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityGrantParams

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", subagent_workspace="subs"),
            Path(td),
        )
        task = agent.subagents.create_run(goal="等待能力后继续", allowed_tools=["read_file"])
        thread = agent.conversation_store.get_or_create_thread(
            {
                "canonical_user_id": "user-1",
                "channel": "feishu",
                "channel_conversation_id": "conversation-1",
                "channel_user_id": "user-1",
            }
        )
        agent.conversation_store.bind_task(
            {"thread_id": thread.thread_id, "task_id": task.id, "goal": task.goal}
        )
        blocked = agent.subagents.load(task.id)
        blocked.status = "BLOCKED"
        blocked.failure_type = "capability_request"
        agent.subagents.save(blocked)
        agent.conversation_store.update_task_status(
            {"task_id": task.id, "status": "blocked", "expected_status": "active"}
        )
        agent.conversation_store.update_task_status(
            {"task_id": task.id, "status": "cancelled", "expected_status": "blocked"}
        )

        agent.subagents.lifecycle.record_capability_grant(
            task.id,
            RecordCapabilityGrantParams(request_id="capreq-1", tools=["write_file"]),
        )

        link = agent.conversation_store.thread_for_task(task.id)
        assert link is not None
        links = {
            item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)
        }
        assert links[task.id].status == "cancelled"


def test_dispatch_reruns_incomplete_output_after_write_grant(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = IncompleteOutputThenAcceptedBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="写一个完整的单文件 HTML。",
            thought="先写文件，必要时继续补齐。",
            plan=["写文件", "补齐", "等待收口"],
            allowed_tools=["write_file"],
            acceptance_checks=["HTML 文件完整闭合"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            start_runners=True,
            max_runners=1,
            probe=False,
            reviewer="dispatch-test",
        )
        loaded = agent.subagents.load(task.id)

        assert backend.calls == 2
        assert [item.step for item in report.records].count("runner") == 2
        assert any(item.step == "capability_route" and item.action == "granted" for item in report.records)
        assert "授权后续跑" in backend.prompts[1]
        assert "不要从头重做任务" in backend.prompts[1]
        assert loaded.capability_requests[0].status == "GRANTED"
        assert "apply_patch" in loaded.allowed_tools
        assert "HTML已补齐" in loaded.result
