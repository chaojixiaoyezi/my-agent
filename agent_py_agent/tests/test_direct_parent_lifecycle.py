from __future__ import annotations

"""Recursive parent-child lifecycle uses durable events instead of polling."""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate
from agent_py_agent.agent.agent_core.runner.prompt_context_summary import (
    runner_context_summary_payload,
)
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation.models import WakeSignal
from agent_py_agent.agent.conversation.runtime import (
    _successful_completion_waiting_for_batch,
)
from agent_py_agent.agent.subagents.direct_parent_lifecycle import (
    direct_children_context_payload,
    mark_parent_waiting_for_direct_children,
    parent_wait_blocks_dispatch,
    reconcile_all_parent_waits,
    reconcile_parent_wait_for_child,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest
from agent_py_agent.agent.subagents.runner_completion_wake import (
    notify_parent_on_capability_request,
    notify_parent_on_runner_result,
)


def _parent_and_children(tmp_path, statuses=("RUNNING",)):
    manager = SubAgentManager(tmp_path / "subagents")
    parent = manager.create_run(
        goal="整合直属孩子结果",
        thought="等待事件",
        plan=["创建", "整合"],
        role="coordinator",
    )
    parent.status = "PENDING"
    manager.save(parent)
    children = []
    for index, status in enumerate(statuses, start=1):
        child = manager.create_run(
            goal=f"孩子任务 {index}",
            thought="独立执行",
            plan=["执行"],
            role="worker",
        )
        child.parent_id = parent.id
        child.root_id = parent.id
        child.depth = 1
        child.status = status
        manager.save(child)
        manager.add_child(parent.id, child.id)
        children.append(child)
    return manager, manager.load(parent.id), children


def _tool_loop_params(parent_id: str) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="创建孩子",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req",
        run_id=parent_id,
        task_id=parent_id,
        one_shot_tool_calls=set(),
        executed_tools=["create_subagents"],
        archive_tool_calls=[],
        context_scope="task_local",
    )


def test_task_local_create_yields_and_wait_marker_blocks_orphan_restart(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path)
    agent = type(
        "Agent",
        (),
        {
            "subagents": manager,
            "_current_subagent_run_id": parent.id,
            "backend": type("Backend", (), {"name": "test"})(),
        },
    )()

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=_tool_loop_params(parent.id),
            response=ModelResponse(text="created", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    refreshed = manager.load(parent.id)
    assert response is not None
    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "SUBAGENTS_ACTIVE"
    assert response.turn_end_reason == "interrupted"
    assert parent_wait_blocks_dispatch(refreshed) is True
    assert _is_dispatch_runner_candidate(refreshed) is False
    assert children[0].id in refreshed.attributes["direct_child_wait"]["run_ids"]


def test_successful_siblings_resume_parent_only_after_last_child(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path, statuses=("RUNNING", "RUNNING"))
    mark_parent_waiting_for_direct_children(
        manager, parent.id, [child.id for child in children]
    )

    first = manager.load(children[0].id)
    first.status = "DONE"
    manager.save(first)
    first_decision = reconcile_parent_wait_for_child(manager, first.id)

    assert first_decision.should_resume is False
    assert first_decision.reason == "siblings_still_active"
    assert parent_wait_blocks_dispatch(manager.load(parent.id)) is True

    second = manager.load(children[1].id)
    second.status = "DONE"
    manager.save(second)
    final_decision = reconcile_parent_wait_for_child(manager, second.id)

    assert final_decision.should_resume is True
    assert final_decision.reason == "all_direct_children_terminal"
    assert parent_wait_blocks_dispatch(manager.load(parent.id)) is False


def test_child_failure_releases_parent_without_waiting_for_successful_sibling(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path, statuses=("RUNNING", "RUNNING"))
    mark_parent_waiting_for_direct_children(
        manager, parent.id, [child.id for child in children]
    )
    failed = manager.load(children[0].id)
    failed.status = "FAILED"
    failed.failure_type = "runner_error"
    manager.save(failed)

    decision = reconcile_parent_wait_for_child(manager, failed.id)

    assert decision.should_resume is True
    assert decision.reason == "child_attention_required"
    assert decision.attention_run_ids == (failed.id,)
    assert children[1].id in decision.active_run_ids


def test_resumed_parent_context_contains_direct_child_results_and_requests(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path, statuses=("BLOCKED",))
    child = manager.load(children[0].id)
    child.failure_type = "capability_request"
    child.latest_summary = "需要读取共享素材"
    child.runner_result_json = "/tmp/result.json"
    child.output_json = "/tmp/output.json"
    child.capability_requests = [
        CapabilityRequest(
            id="cap-1",
            from_run_id=child.id,
            problem="读不到素材",
            needed_capability="读取共享素材目录",
            requested_tools=["read_file"],
            path_scope=["/tmp/shared"],
        )
    ]
    manager.save(child)
    child = manager.load(child.id)

    payload = direct_children_context_payload(manager, parent.id)
    context = manager.runner_context.build_execution_context(parent.id)
    prompt_payload = runner_context_summary_payload(context)["direct_children"]

    assert payload["attention_run_ids"] == [child.id]
    assert payload["items"][0]["runner_result_json"] == child.runner_result_json
    assert payload["items"][0]["output_json"] == child.output_json
    assert prompt_payload["items"][0]["open_capability_requests"][0] == {
        "request_id": "cap-1",
        "capability_type": "generic",
        "needed_capability": "读取共享素材目录",
        "tools": ["read_file"],
        "skills": [],
        "path_scope": ["/tmp/shared"],
    }


def test_crash_recovery_releases_parent_after_children_already_finished(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path, statuses=("RUNNING",))
    mark_parent_waiting_for_direct_children(manager, parent.id, [children[0].id])
    child = manager.load(children[0].id)
    child.status = "DONE"
    manager.save(child)

    summary = reconcile_all_parent_waits(manager)

    assert summary["released_run_ids"] == [parent.id]
    assert parent_wait_blocks_dispatch(manager.load(parent.id)) is False


class _NoRootWakeStore:
    def __init__(self) -> None:
        self.status_updates: list[dict[str, object]] = []
        self.thread_lookups = 0

    def thread_for_task(self, _task_id: str):
        self.thread_lookups += 1
        return SimpleNamespace(thread_id="thread-nested")

    def update_task_status(self, payload: dict[str, object]) -> None:
        self.status_updates.append(payload)

    def append_observation_with_wake(self, *_args, **_kwargs) -> None:
        raise AssertionError("nested child must not publish a root conversation wake")

    def append_observation(self, *_args, **_kwargs) -> None:
        raise AssertionError("nested capability request must stay with direct parent")

    def raise_wake_signal(self, *_args, **_kwargs) -> None:
        raise AssertionError("nested capability request must not wake root")


def test_nested_child_result_and_capability_do_not_wake_root_conversation(tmp_path) -> None:
    manager, _parent, children = _parent_and_children(tmp_path, statuses=("DONE",))
    store = _NoRootWakeStore()
    manager.conversation_store = store
    child = manager.load(children[0].id)
    result = SimpleNamespace(
        status="DONE",
        run_id=child.id,
        dry_run=False,
        turn_end_reason="completed",
        result_json="",
    )

    notify_parent_on_runner_result(manager, child, result, {})
    notify_parent_on_capability_request(
        manager,
        child,
        CapabilityRequest(
            id="cap-nested",
            from_run_id=child.id,
            problem="需要权限",
            needed_capability="读取一个目录",
        ),
    )

    assert store.status_updates == [{"task_id": child.id, "status": "DONE"}]
    assert store.thread_lookups == 1


def test_root_success_wake_waits_model_free_until_same_tree_settles(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    first = manager.create_run(goal="第一块", thought="执行", plan=["做"], role="worker")
    second = manager.create_run(goal="第二块", thought="执行", plan=["做"], role="worker")
    for task, status in ((first, "DONE"), (second, "RUNNING")):
        task.root_id = "root-conversation-task"
        task.parent_id = "root-conversation-task"
        task.status = status
        manager.save(task)
    scheduler = SimpleNamespace(
        runtime=SimpleNamespace(agent=SimpleNamespace(subagents=manager)),
        _config_limit=lambda _name: 0,
    )
    signal = WakeSignal(
        wake_signal_id="wake-first",
        thread_id="thread-root",
        reason="subagent_runner_finished",
        root_task_id="root-conversation-task",
        created_at=1.0,
        metadata={"status": "DONE"},
    )

    assert _successful_completion_waiting_for_batch(scheduler, signal, 10.0) is True

    second = manager.load(second.id)
    second.status = "DONE"
    manager.save(second)

    assert _successful_completion_waiting_for_batch(scheduler, signal, 10.0) is False
