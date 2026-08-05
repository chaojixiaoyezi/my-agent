from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.finalization_compact_auto import (
    _compact_auto_continue_depth_exhausted,
)
from agent_py_agent.agent.agent_core.orchestration.create_policy import _create_attributes
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.common.audit_activation import AUDIT_ATTR
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.conversation.runtime import (
    BackgroundToolPolicyRequest,
    background_prompt,
    background_tool_policy_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.ingestion.watch_state import (
    new_state,
    persist_state,
    state_dir,
    watch_id_for,
)
from agent_py_agent.agent.settings import AgentConfig


def test_scheduled_wake_toolset_includes_work_tools() -> None:
    decision = background_tool_policy_decision(
        None,
        request=BackgroundToolPolicyRequest(reason="scheduled_progress_report"),
    )

    assert decision.profile == "scheduled_progress"
    for tool in ("read_file", "list_files", "write_file", "run_command", "task_progress"):
        assert tool in decision.allowed_tools
    assert "create_subagents" in decision.allowed_tools
    assert "wait" in decision.allowed_tools


def test_default_wake_toolset_includes_work_tools_and_create() -> None:
    decision = background_tool_policy_decision(None, request=BackgroundToolPolicyRequest(reason=""))

    assert "read_file" in decision.allowed_tools
    assert "create_subagents" in decision.allowed_tools


def test_goal_wake_has_goal_lifecycle_and_work_tools() -> None:
    decision = background_tool_policy_decision(
        None,
        request=BackgroundToolPolicyRequest(reason="thread_goal_continue"),
    )

    assert decision.profile == "thread_goal"
    for tool in ("get_goal", "update_goal", "read_file", "write_file", "run_command"):
        assert tool in decision.allowed_tools

    goal = SimpleNamespace(objective="完成测试", tokens_used=10, token_budget=100)
    prompt = background_prompt("thread_goal_continue", goal=goal)
    assert "Continue working toward the active thread goal" in prompt
    assert 'update_goal with status "complete"' in prompt
    assert "Completion audit" in prompt


def test_all_wake_profiles_can_resolve_and_cancel_stuck_children() -> None:
    for reason in ("scheduled_progress_report", "subagent_runner_finished", "", "urgent_wake_signal"):
        decision = background_tool_policy_decision(None, request=BackgroundToolPolicyRequest(reason=reason))
        assert "resolve_capability_requests" in decision.allowed_tools
        assert "cancel_subagents" in decision.allowed_tools


def test_background_prompt_scheduled_wake_is_self_continuation() -> None:
    prompt = background_prompt("scheduled_progress_report")

    assert "same durable task" in prompt
    assert "Active Wake Signal" in prompt
    assert "do not prescribe" in prompt


def test_due_policy_wake_carries_wait_reason_into_prompt(tmp_path: Path) -> None:
    captured: list[str] = []

    class _Backend:
        name = "capturing"

        def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
            captured.append(prompt)
            return ModelResponse(text="本轮检查完成。", backend=self.name)

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _Backend()
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-watch", "goal": "盯日志", "now": 11.0})
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-watch",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "metadata": {"kind": "subagent_progress_watch", "tool": "wait", "reason": "盯日志增量有目标行才上报"},
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(captured) == 1
    assert "盯日志增量有目标行才上报" in captured[0]
    assert "same durable task" in captured[0]


def _depth_ctx(depth: int, attrs: dict | None):
    return SimpleNamespace(compact_auto_continue_depth=depth, task_attributes=attrs)


def test_compact_depth_cap_holds_for_regular_runs() -> None:
    agent = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_continue_max_depth=50))

    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, None)) is True
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(49, None)) is False


def test_compact_depth_cap_exempts_declared_long_running() -> None:
    agent = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_continue_max_depth=50))

    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, {"long_running": True})) is False
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(5000, {"long_running": True})) is False
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, {"long_running": "yes"})) is True


def test_create_subagents_long_running_lands_in_task_attributes() -> None:
    assert _create_attributes({"long_running": True, "goal": "持续盯守日志"}, None).get("long_running") is True
    assert "long_running" not in _create_attributes({"goal": "普通任务"}, None)


def _audit_wait_agent(
    tmp_path: Path,
    *,
    task_id: str,
) -> tuple[SimpleAgent, Path]:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            subagent_workspace="subs",
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path / "workspace",
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": f"conv:{task_id}",
            "channel_user_id": "user-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "goal": "opaque audit objective",
            "status": "active",
            "work_kind": "audit",
            "work_name": task_id,
            "duration_seconds": 600,
            "cancellation_scope": "detached",
        }
    )
    agent._current_run_params = SimpleNamespace(
        context_scope="conversation",
        task_id=task_id,
        run_id=f"run-{task_id}",
        request_id=task_id,
        root_user_prompt="opaque audit objective",
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: task_id,
            "conversation_task_id": task_id,
            "conversation_thread_id": thread.thread_id,
            "conversation_work_kind": "audit",
        },
    )
    return agent, Path(agent.home_paths.owner_home_dir)


def _persist_audit_backlog(
    owner_home: Path,
    *,
    task_id: str,
    written: int,
    acknowledged: int,
) -> str:
    url = f"http://source.invalid/{task_id}"
    watch_id = watch_id_for(owner_home, url, task_id)
    state = new_state(
        owner_home,
        url,
        {"watch_window_seconds": 600},
        watch_id=watch_id,
    )
    state.audit_guarantee = True
    state.audit_root_task_id = task_id
    state.totals["spool_candidates"] = written
    persist_state(state)
    (state_dir(owner_home) / f"{watch_id}.read.json").write_text(
        json.dumps(
            {
                "candidates_consumed": acknowledged,
                "candidates_acked": acknowledged,
            }
        ),
        encoding="utf-8",
    )
    return watch_id


def test_wait_rejects_exact_audit_with_durable_unacknowledged_input(tmp_path: Path) -> None:
    agent, owner_home = _audit_wait_agent(tmp_path, task_id="audit-a")
    watch_id = _persist_audit_backlog(
        owner_home,
        task_id="audit-a",
        written=12,
        acknowledged=4,
    )

    result = agent.tools.tools["wait"].execute(
        {"seconds": 120, "reason": "later"}
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "WAIT_ACTIONABLE_INPUT_PENDING"
    assert payload["facts"]["task_id"] == "audit-a"
    assert payload["facts"]["pending_records"] == 8
    assert payload["facts"]["sources"] == [
        {
            "watch_id": watch_id,
            "pending": 8,
            "collection_active": True,
            "window_complete": False,
        }
    ]
    assert agent.conversation_store.list_progress_policies(enabled_only=True) == []


def test_wait_allows_exact_audit_after_durable_input_is_acknowledged(tmp_path: Path) -> None:
    agent, owner_home = _audit_wait_agent(tmp_path, task_id="audit-a")
    _persist_audit_backlog(
        owner_home,
        task_id="audit-a",
        written=12,
        acknowledged=12,
    )

    result = agent.tools.tools["wait"].execute(
        {"seconds": 120, "reason": "later"}
    )

    assert result.ok is True
    assert json.loads(result.output)["scheduled"] is True


def test_wait_does_not_read_another_audits_backlog(tmp_path: Path) -> None:
    agent, owner_home = _audit_wait_agent(tmp_path, task_id="audit-a")
    _persist_audit_backlog(
        owner_home,
        task_id="audit-b",
        written=12,
        acknowledged=0,
    )

    result = agent.tools.tools["wait"].execute(
        {"seconds": 120, "reason": "later"}
    )

    assert result.ok is True
    assert json.loads(result.output)["scheduled"] is True


def test_descendant_wait_uses_inherited_audit_root_not_child_run_id(tmp_path: Path) -> None:
    agent, owner_home = _audit_wait_agent(tmp_path, task_id="audit-a")
    _persist_audit_backlog(
        owner_home,
        task_id="audit-a",
        written=5,
        acknowledged=0,
    )
    agent._current_run_params.context_scope = "subagent"
    agent._current_run_params.task_id = "child-run-1"
    agent._current_run_params.run_id = "child-run-1"
    agent._current_run_params.request_id = "child-run-1"

    result = agent.tools.tools["wait"].execute(
        {"seconds": 120, "reason": "later"}
    )

    assert result.ok is False
    assert result.error_code == "WAIT_ACTIONABLE_INPUT_PENDING"
    assert json.loads(result.output)["facts"]["task_id"] == "audit-a"


def test_non_audit_wait_is_unchanged_even_when_owner_has_audit_backlog(tmp_path: Path) -> None:
    agent, owner_home = _audit_wait_agent(tmp_path, task_id="audit-a")
    _persist_audit_backlog(
        owner_home,
        task_id="audit-a",
        written=5,
        acknowledged=0,
    )
    agent._current_run_params.task_attributes.pop(AUDIT_ATTR)

    result = agent.tools.tools["wait"].execute(
        {"seconds": 120, "reason": "later"}
    )

    assert result.ok is True
    assert json.loads(result.output)["scheduled"] is True
