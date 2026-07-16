from __future__ import annotations

import gc
import json
import threading
import weakref
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import build_tool_loop_prompt
from agent_py_agent.agent.agent_core.orchestration.dispatch.tool import DispatchSubagentsTool
from agent_py_agent.agent.agent_core.runner.context import ThreadLocalAgentAttribute
from agent_py_agent.agent.agent_core.runtime.guidance import (
    has_pending_request_guidance,
    inject_pending_guidance,
    render_subagent_guidance_section,
)
from agent_py_agent.agent.agent_core.runtime.guidance_tool import SendGuidanceTool
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


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


def test_shared_owner_agent_keeps_transient_run_context_per_worker_thread(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    barrier = threading.Barrier(2)
    observed: dict[str, tuple[str, str]] = {}

    def worker(name: str) -> None:
        params = _tool_loop_params(request_id=f"req-{name}", task_id=f"task-{name}")
        agent._current_run_params = params
        agent._current_run_task_workspace = str(tmp_path / name)
        barrier.wait(timeout=2)
        observed[name] = (
            agent._current_run_params.request_id,
            agent._current_run_task_workspace,
        )
        del agent._current_run_params
        del agent._current_run_task_workspace

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("chat", "background")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert observed == {
        "chat": ("req-chat", str(tmp_path / "chat")),
        "background": ("req-background", str(tmp_path / "background")),
    }
    assert getattr(agent, "_current_run_params", None) is None
    assert getattr(agent, "_current_run_task_workspace", "") == ""


def test_thread_local_agent_attribute_releases_destroyed_agent_identity() -> None:
    class Holder:
        current = ThreadLocalAgentAttribute("current")

    holder = Holder()
    holder.current = "stale-workspace"
    reference = weakref.ref(holder)
    descriptor = Holder.current

    del holder
    gc.collect()

    assert reference() is None
    assert len(descriptor._values()) == 0


def test_two_real_agent_runs_do_not_cross_prompt_task_or_workspace(tmp_path, monkeypatch) -> None:
    """同一 owner 的前台聊天与后台任务真实进入 run 主链时，临时上下文仍严格隔离。"""
    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / ".my-agent"),
            memory_path="memory.jsonl",
            prompt_files=[],
        ),
        tmp_path / "repo",
    )
    barrier = threading.Barrier(2)
    observed: dict[str, tuple[str, str, str, str]] = {}
    failures: list[BaseException] = []

    class ProbeComplete(RuntimeError):
        pass

    def probe_runtime_loop(shared_agent, loop_params):
        barrier.wait(timeout=5)
        current = shared_agent._current_run_params
        observed[loop_params.request_id] = (
            shared_agent._current_user_prompt,
            current.request_id,
            current.task_id,
            shared_agent._current_run_task_workspace,
        )
        raise ProbeComplete(loop_params.request_id)

    monkeypatch.setattr(runtime_mixin, "_execute_runtime_loop", probe_runtime_loop)

    def worker(name: str) -> None:
        request_id = f"req-{name}"
        try:
            agent.run(
                f"{name} 的独立提示",
                params=RunParams(
                    request_id=request_id,
                    run_id=f"run-{name}",
                    task_id=f"task-{name}",
                    source="gateway",
                    save=False,
                    resume_context=False,
                    task_attributes={"conversation_lane": "task"},
                ),
            )
        except ProbeComplete:
            return
        except BaseException as exc:  # pragma: no cover - failure evidence is asserted below
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("chat", "background")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)

    assert not failures
    assert not any(thread.is_alive() for thread in threads)
    for name in ("chat", "background"):
        request_id = f"req-{name}"
        prompt, actual_request, task_id, workspace = observed[request_id]
        assert prompt == f"{name} 的独立提示"
        assert actual_request == request_id
        assert task_id == f"task-{name}"
        assert name in workspace
        other = "background" if name == "chat" else "chat"
        assert other not in workspace


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
            "root_id": root.id,
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
            "root_id": root.id,
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


def test_request_guidance_is_one_shot_and_does_not_leak_to_next_request(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.append_guidance(
        {
            "target_type": "request",
            "target_id": "req-1",
            "message": "先别写文件，先确认输入范围。",
            "now": 10.0,
        }
    )
    current = _tool_loop_params(request_id="req-1")
    later = _tool_loop_params(request_id="req-2")

    assert has_pending_request_guidance(agent, current) is True
    assert has_pending_request_guidance(agent, later) is False
    assert inject_pending_guidance(agent, current, now=11.0) is True
    assert any("先别写文件" in str(item) for item in current.tool_context)
    assert has_pending_request_guidance(agent, current) is False
    assert inject_pending_guidance(agent, later, now=12.0) is False


def test_task_guidance_is_consumed_once_and_not_replayed_after_resume(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.append_guidance(
        {
            "target_type": "task",
            "target_id": "task-1",
            "message": "最终文档增加执行风险检查表。",
            "now": 10.0,
        }
    )
    first_run = _tool_loop_params(task_id="task-1")
    later_run = _tool_loop_params(task_id="task-1")
    other_task = _tool_loop_params(task_id="task-2")

    assert has_pending_request_guidance(agent, first_run) is True
    assert inject_pending_guidance(agent, first_run, now=11.0) is True
    assert inject_pending_guidance(agent, first_run, now=11.5) is False
    assert sum("执行风险检查表" in str(item) for item in first_run.tool_context) == 1
    assert has_pending_request_guidance(agent, first_run) is False

    assert inject_pending_guidance(agent, later_run, now=12.0) is False
    assert not any("执行风险检查表" in str(item) for item in later_run.tool_context)
    assert inject_pending_guidance(agent, other_task, now=13.0) is False


def test_task_guidance_uses_selected_durable_task_instead_of_gateway_request_id(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.append_guidance(
        {
            "target_type": "task",
            "target_id": "task-original",
            "message": "从中断位置继续，不要新建第二份项目。",
            "now": 10.0,
        }
    )
    params = _tool_loop_params(
        request_id="req-followup",
        task_id="req-followup",
        task_attributes={"conversation_task_id": "task-original"},
    )

    assert has_pending_request_guidance(agent, params) is True
    assert inject_pending_guidance(agent, params, now=11.0) is True
    assert any("从中断位置继续" in str(item) for item in params.tool_context)
    assert agent.conversation_store.pending_guidance("task", "task-original") == []


def test_multiple_task_steers_keep_codex_style_fifo_order(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    for current, message in enumerate(
        (
            "先把预算上限改为四百元。",
            "再在最后增加一张风险检查表。",
        ),
        start=10,
    ):
        agent.conversation_store.append_guidance(
            {
                "target_type": "task",
                "target_id": "task-1",
                "message": message,
                "now": float(current),
            }
        )
    current_run = _tool_loop_params(task_id="task-1")

    assert inject_pending_guidance(agent, current_run, now=20.0) is True
    rendered = "\n".join(str(item) for item in current_run.tool_context)
    assert rendered.index("先把预算上限") < rendered.index("再在最后增加")
    assert inject_pending_guidance(agent, current_run, now=21.0) is False

    resumed_run = _tool_loop_params(task_id="task-1")
    other_task = _tool_loop_params(task_id="task-2")
    assert inject_pending_guidance(agent, resumed_run, now=22.0) is False
    assert inject_pending_guidance(agent, other_task, now=23.0) is False


def test_tool_loop_guidance_can_override_earlier_contract_context(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.append_guidance(
        {
            "target_type": "agent_run",
            "target_id": "main-run-1",
            "message": "用户补充：25次压缩已经够了，现在停止继续读，写收口总结。",
            "now": 10.0,
        }
    )
    params = _tool_loop_params(
        run_id="main-run-1",
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [{"artifact_id": "report", "path": "output/report.md"}],
        },
    )

    prompt = build_tool_loop_prompt(agent, params)

    assert "GUIDANCE_DELIVERED" in prompt
    assert "只作为普通补充消息进入上下文" in prompt
    assert "不会把这些文字解释成新的硬门" in prompt
    assert "用户补充：25次压缩已经够了" in prompt
    assert prompt.rfind("用户补充：25次压缩已经够了") > prompt.find("[tool-system delivery-contract]")
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
    assert "只作为普通补充消息进入上下文" in prompt
    assert "不会把这些文字解释成新的硬门" in prompt
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
    assert pending[0].metadata["tool_name"] == "dispatch_subagents"


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
