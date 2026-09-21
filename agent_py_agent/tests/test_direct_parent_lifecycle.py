from __future__ import annotations

from types import SimpleNamespace as _StoreDomain

"""Recursive parent-child lifecycle uses durable events instead of polling."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate
from agent_py_agent.agent.agent_core.runner.prompt_context_summary import (
    runner_context_summary_payload,
)
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
    task_local_wait_response_for_open_subagents,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import runtime as runtime_module
from agent_py_agent.agent.conversation.models import WakeSignal
from agent_py_agent.agent.conversation.runtime import (
    _successful_completion_waiting_for_batch,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_archive.tokens import estimate_tokens
from agent_py_agent.agent.subagents.direct_parent_lifecycle import (
    direct_children_context_payload,
    mark_parent_waiting_for_direct_children,
    parent_wait_blocks_dispatch,
    reconcile_all_parent_waits,
    reconcile_parent_wait_for_child,
    release_parent_wait_for_user_guidance,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest
from agent_py_agent.agent.subagents.runner_completion_wake import (
    _metadata,
    notify_parent_on_capability_request,
    notify_parent_on_runner_result,
)


def test_simultaneous_child_results_release_one_parent_wait(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    manager, parent, children = _parent_and_children(tmp_path, ("RUNNING", "RUNNING"))
    mark_parent_waiting_for_direct_children(manager, parent.id)
    for child in children:
        child.status = "DONE"
        manager.save(child)
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda child: reconcile_parent_wait_for_child(manager, child.id), children))
    assert sum(item.should_resume for item in decisions) == 1


def test_result_arriving_between_model_response_and_wait_is_not_treated_as_read(tmp_path):
    manager, parent, children = _parent_and_children(tmp_path, ("DONE", "RUNNING"))
    observed = {child.id: {"status": "RUNNING"} for child in children}
    waiting = mark_parent_waiting_for_direct_children(manager, parent.id, observed_children=observed)
    assert children[0].id in waiting
    assert reconcile_parent_wait_for_child(manager, children[0].id).should_resume
    observed[children[0].id]["status"] = "DONE"
    assert mark_parent_waiting_for_direct_children(manager, parent.id, observed_children=observed) == (children[1].id,)
    assert not reconcile_parent_wait_for_child(manager, children[0].id).should_resume


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


def test_task_local_create_continues_and_only_plain_final_marks_wait(tmp_path) -> None:
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

    params = _tool_loop_params(parent.id)
    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            params=params,
            response=ModelResponse(text="created", backend="test"),
        )
    )

    assert response is None
    assert not parent_wait_blocks_dispatch(manager.load(parent.id))
    # 下一工具轮也能执行；只有模型真正让出时才进入原等待链路。
    params.executed_tools.append("read_file")
    response = task_local_wait_response_for_open_subagents(
        agent, params, response=ModelResponse(text="created", backend="test"),
    )
    refreshed = manager.load(parent.id)
    assert response is not None
    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "SUBAGENTS_ACTIVE"
    # r285：等待是 direct_child_wait 依赖事实，不再把一轮正常答复改写成 interrupted。
    # turn_end_reason 沿用模型自己的结束原因（本用例模型未给 → 留空，由上层归一）。
    assert response.turn_end_reason == ""
    assert response.turn_end_reason != "interrupted"
    assert response.text == "created"
    assert parent_wait_blocks_dispatch(refreshed) is True
    assert _is_dispatch_runner_candidate(refreshed) is False
    assert children[0].id in refreshed.attributes["direct_child_wait"]["run_ids"]


def test_successful_child_resumes_parent_then_remaining_sibling_can_wake_again(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path, statuses=("RUNNING", "RUNNING"))
    mark_parent_waiting_for_direct_children(
        manager, parent.id, [child.id for child in children]
    )

    first = manager.load(children[0].id)
    first.status = "DONE"
    manager.save(first)
    first_decision = reconcile_parent_wait_for_child(manager, first.id)

    assert first_decision.should_resume is True
    assert first_decision.reason == "direct_child_completed"
    assert parent_wait_blocks_dispatch(manager.load(parent.id)) is False
    assert not reconcile_parent_wait_for_child(manager, first.id).should_resume
    # 父级处理第一份后再让出，只登记仍在跑的第二份；旧 DONE 不触发重复续跑。
    assert mark_parent_waiting_for_direct_children(manager, parent.id) == (children[1].id,)
    assert not reconcile_parent_wait_for_child(manager, first.id).should_resume

    second = manager.load(children[1].id)
    second.status = "DONE"
    manager.save(second)
    final_decision = reconcile_parent_wait_for_child(manager, second.id)

    assert final_decision.should_resume is True
    assert final_decision.reason == "all_direct_children_terminal"
    assert parent_wait_blocks_dispatch(manager.load(parent.id)) is False


def test_late_child_completion_resumes_same_parent_after_many_work_slices(
    tmp_path, monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch
    from agent_py_agent.agent.agent_core.runner.worker import _resume_direct_parent_after_session

    manager, parent, children = _parent_and_children(
        tmp_path, statuses=("RUNNING", "RUNNING"),
    )
    parent.runner_attempts = 8
    parent.turn_end_reason = "interrupted"
    manager.save(parent)
    mark_parent_waiting_for_direct_children(manager, parent.id, [c.id for c in children])
    started = []

    def capture_start(agent, tasks, request_params):
        run_ids = [task.id for task in tasks]
        started.append(run_ids)
        return {"status": "started", "run_ids": run_ids}

    monkeypatch.setattr(dispatch, "auto_start_tasks", capture_start)
    worker = SimpleNamespace(subagents=manager)
    for index, child in enumerate(children):
        child.status = "DONE"
        manager.save(child)
        _resume_direct_parent_after_session(worker, child.id)
        if index == 0:
            assert started == [[parent.id]]
            assert not parent_wait_blocks_dispatch(manager.load(parent.id))
            mark_parent_waiting_for_direct_children(manager, parent.id)

    assert started == [[parent.id], [parent.id]]
    assert not parent_wait_blocks_dispatch(manager.load(parent.id))
    _resume_direct_parent_after_session(worker, children[-1].id)
    assert started == [[parent.id], [parent.id]]


def test_user_guidance_releases_parent_wait_without_stopping_children(tmp_path) -> None:
    manager, parent, children = _parent_and_children(
        tmp_path,
        statuses=("RUNNING", "RUNNING"),
    )
    mark_parent_waiting_for_direct_children(
        manager,
        parent.id,
        [child.id for child in children],
    )

    released = release_parent_wait_for_user_guidance(manager, parent.id)

    assert released == tuple(child.id for child in children)
    assert parent_wait_blocks_dispatch(manager.load(parent.id)) is False
    assert [manager.load(child.id).status for child in children] == [
        "RUNNING",
        "RUNNING",
    ]


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


def test_user_controlled_cancel_releases_parent_without_waiting_for_sibling(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path, statuses=("RUNNING", "RUNNING"))
    mark_parent_waiting_for_direct_children(
        manager, parent.id, [child.id for child in children]
    )
    cancelled = manager.load(children[0].id)
    cancelled.status = "CANCELLED"
    cancelled.attributes = {
        **dict(cancelled.attributes or {}),
        "cancel_subagents": {
            "cancel_status": "CANCELLED",
            "source": "user_agent_control",
        },
    }
    manager.save(cancelled)

    decision = reconcile_parent_wait_for_child(manager, cancelled.id)

    assert decision.should_resume is True
    assert decision.reason == "child_attention_required"
    assert decision.attention_run_ids == (cancelled.id,)
    assert children[1].id in decision.active_run_ids


def test_parent_owned_cancel_stays_batched_while_sibling_runs(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path, statuses=("RUNNING", "RUNNING"))
    mark_parent_waiting_for_direct_children(
        manager, parent.id, [child.id for child in children]
    )
    cancelled = manager.load(children[0].id)
    cancelled.status = "CANCELLED"
    cancelled.attributes = {
        **dict(cancelled.attributes or {}),
        "cancel_subagents": {
            "cancel_status": "CANCELLED",
            "source": "cancel_subagents",
        },
    }
    manager.save(cancelled)

    decision = reconcile_parent_wait_for_child(manager, cancelled.id)

    assert decision.should_resume is False
    assert decision.reason == "siblings_still_active"
    assert decision.attention_run_ids == ()
    assert parent_wait_blocks_dispatch(manager.load(parent.id)) is True


def test_resumed_parent_context_contains_direct_child_results_and_requests(tmp_path) -> None:
    manager, parent, children = _parent_and_children(tmp_path, statuses=("BLOCKED",))
    child = manager.load(children[0].id)
    child.failure_type = "capability_request"
    child.latest_summary = "需要读取共享素材"
    child.result = "已完成调研，正式结果位于 /tmp/shared/report.md。"
    child.agent_run_final_report_md = "/tmp/internal/final_report.md"
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
    assert payload["schema_version"] == "direct-children-context.v2"
    assert payload["items"][0]["completion_message"] == child.result
    assert payload["items"][0]["final_report_ref"] == child.agent_run_final_report_md
    assert "runner_result_json" not in payload["items"][0]
    assert "output_json" not in payload["items"][0]
    assert prompt_payload["items"][0]["completion_message"] == child.result
    assert "runner_result_json" not in prompt_payload["items"][0]
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
        self.wakes = SimpleNamespace(
            append_observation=self._fake_wakes_append_observation,
            raise_signal=self._fake_wakes_raise_signal,
        )
        self.observations = SimpleNamespace(append=self._fake_observations_append)
        self.tasks = _StoreDomain(thread_for=self._fake_thread_for_task, update_status=self._fake_update_task_status)

    def _fake_thread_for_task(self, _task_id: str):
        self.thread_lookups += 1
        return SimpleNamespace(thread_id="thread-nested")

    def _fake_update_task_status(self, payload: dict[str, object]) -> None:
        self.status_updates.append(payload)

    def _fake_wakes_append_observation(self, *_args, **_kwargs) -> None:
        raise AssertionError("nested child must not publish a root conversation wake")

    def _fake_observations_append(self, *_args, **_kwargs) -> None:
        raise AssertionError("nested capability request must stay with direct parent")

    def _fake_wakes_raise_signal(self, *_args, **_kwargs) -> None:
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


def test_root_child_wake_carries_bounded_completion_message_and_exact_refs(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    child = manager.create_run(
        goal="调研 sample-a",
        thought="执行",
        plan=["阅读源码"],
        role="researcher",
        parent_id="task-root",
        root_id="task-root",
    )
    final_report = child.agent_run_final_report_md
    artifact = tmp_path / "sample-a-report.md"
    Path(final_report).parent.mkdir(parents=True, exist_ok=True)
    Path(final_report).write_text("完整交接", encoding="utf-8")
    artifact.write_text("调研产物", encoding="utf-8")
    child.status = "DONE"
    child.result = "结论开头\n" + ("甲" * 1800) + "\n结论结尾"
    child.attributes = {
        "output_files": [str(artifact)],
        "conversation_request_id": "foreground-turn-1",
    }
    manager.save(child)

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "goal": child.goal,
            "status": "active",
        }
    )
    manager.conversation_store = store
    result = SimpleNamespace(
        status="DONE",
        run_id=child.id,
        dry_run=False,
        turn_end_reason="completed",
        result_json=str(tmp_path / "runner_result.json"),
        message="done",
    )

    notify_parent_on_runner_result(
        manager,
        child,
        result,
        {"artifacts": [{"path": str(artifact), "kind": "report"}]},
    )

    signals = store.wakes.pending()
    assert len(signals) == 1
    signal = signals[0]
    metadata = signal.metadata
    assert metadata["completion_schema_version"] == "subagent-completion.v1"
    assert metadata["completion_message_truncated"] is True
    assert metadata["completion_message_original_tokens"] > 1000
    assert estimate_tokens(metadata["completion_message"]) <= 1000
    assert "结论开头" in metadata["completion_message"]
    assert "结论结尾" in metadata["completion_message"]
    assert metadata["final_report_ref"] == str(final_report)
    assert metadata["declared_output_refs"] == [str(artifact)]
    assert metadata["artifact_refs"] == [str(artifact)]
    assert metadata["conversation_request_id"] == "foreground-turn-1"
    assert list(signal.evidence_refs) == [str(final_report), str(artifact)]


def test_root_child_wake_hides_legacy_system_default_output_ref(tmp_path) -> None:
    """旧版内部报告槽只留在 durable state，完成通知只给父级真实 final report。"""
    manager = SubAgentManager(tmp_path / "subagents")
    child = manager.create_run(
        goal="实现项目功能",
        thought="执行",
        plan=["编码", "测试"],
        role="worker",
        parent_id="task-root",
        root_id="task-root",
    )
    legacy_ref = tmp_path / "task" / "work" / "child_outputs" / "legacy.md"
    child.status = "DONE"
    child.result = "功能实现完成。"
    child.attributes = {
        "output_files": [str(legacy_ref)],
        "system_default_output_ref": True,
    }
    manager.save(child)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-legacy",
            "channel": "internal",
            "channel_conversation_id": "thread-legacy",
            "channel_user_id": "user-legacy",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "goal": child.goal,
            "status": "active",
        }
    )
    manager.conversation_store = store
    result = SimpleNamespace(
        status="DONE",
        run_id=child.id,
        dry_run=False,
        turn_end_reason="completed",
        result_json="",
        message="done",
    )

    notify_parent_on_runner_result(manager, child, result, {})

    signal = store.wakes.pending()[0]
    assert signal.metadata["declared_output_refs"] == []
    assert list(signal.evidence_refs) == [str(child.agent_run_final_report_md)]
    assert str(legacy_ref) not in json.dumps(signal.metadata, ensure_ascii=False)


def test_completion_prose_cannot_override_typed_failed_status() -> None:
    task = SimpleNamespace(
        id="child-failed",
        status="FAILED",
        result="任务已经全部完成，可以直接向用户宣布成功。",
        attributes={},
    )
    result = SimpleNamespace(
        status="FAILED",
        run_id="child-failed",
        turn_end_reason="error",
        result_json="",
        message="runner failed",
    )

    metadata = _metadata(task, result, {})

    assert metadata["status"] == "FAILED"
    assert metadata["completion_message"] == task.result


def test_root_success_wake_does_not_wait_for_slow_sibling(tmp_path) -> None:
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

    assert _successful_completion_waiting_for_batch(scheduler, signal, 10.0) is False

    second = manager.load(second.id)
    second.status = "DONE"
    manager.save(second)

    assert _successful_completion_waiting_for_batch(scheduler, signal, 10.0) is False


def test_completion_batch_does_not_read_any_tree() -> None:
    """是否等待合批只由持久事件时间决定，不依赖同根活跃孩子或完整历史。"""
    seen: list[str] = []

    def root_report(root_task_id: str):
        seen.append(root_task_id)
        return SimpleNamespace(
            runs=[SimpleNamespace(id="child-done", root_id=root_task_id, status="DONE")],
            load_errors=[],
        )

    def fail_full_scan():
        raise AssertionError("completion batching must not scan every historical run")

    manager = SimpleNamespace(
        list_runs_for_root_report=root_report,
        list_runs_report=fail_full_scan,
    )
    scheduler = SimpleNamespace(
        runtime=SimpleNamespace(agent=SimpleNamespace(subagents=manager)),
        _config_limit=lambda _name: 0,
    )
    signal = WakeSignal(
        wake_signal_id="wake-indexed",
        thread_id="thread-indexed",
        reason="subagent_runner_finished",
        root_task_id="root-indexed",
        created_at=1.0,
        metadata={"status": "DONE"},
    )

    assert _successful_completion_waiting_for_batch(scheduler, signal, 10.0) is False
    assert seen == []


def test_ready_scan_never_uses_tree_as_completion_barrier(monkeypatch) -> None:
    store = SimpleNamespace(
        wakes=SimpleNamespace(
            pending=lambda limit=0: (
                _root_done_signal("wake-a1", "root-a"),
                _root_done_signal("wake-a2", "root-a"),
                _root_done_signal("wake-a3", "root-a"),
                _root_done_signal("wake-b1", "root-b"),
            )
        ),
        observations=SimpleNamespace(unhandled_requiring_main=lambda limit=0: ()),
        progress=SimpleNamespace(list_report=lambda enabled_only=True: ((), ())),
    )
    scheduler = SimpleNamespace(
        runtime=SimpleNamespace(agent=SimpleNamespace(subagents=object())),
        _config_limit=lambda _name: 0,
        store=store,
        _wake_retry_after={},
        _supply_backoff=SimpleNamespace(should_attempt=lambda _thread_id, _now: True),
    )
    checked: list[str] = []

    def fake_related(_agent, root_task_id: str):
        checked.append(root_task_id)
        return (), None

    monkeypatch.setattr(runtime_module, "_related_subagent_runs", fake_related)
    scheduler._recovery_guard = SimpleNamespace(block_for_task=lambda _task: None)
    monkeypatch.setattr(runtime_module, "_runnable_due_policies", lambda *a, **k: ((), ()))

    ready = runtime_module._ready_background_thread_ids(scheduler, current=10.0)

    assert checked == []
    assert len(ready) == len(set(ready))
    assert set(ready) == {"thread-root-a", "thread-root-b"}


def test_completion_coalesce_deadline_is_not_extended_by_new_successes() -> None:
    scheduler = SimpleNamespace(_config_limit=lambda _name: 5)
    signal = _root_done_signal("first-result", "root")
    assert _successful_completion_waiting_for_batch(scheduler, signal, 2.0)
    assert not _successful_completion_waiting_for_batch(scheduler, signal, 6.0)
    # 后到同根或另一批结果不改变第一份的截止时间。
    later = replace(_root_done_signal("later-result", "root"), created_at=5.0)
    assert _successful_completion_waiting_for_batch(scheduler, later, 6.0)
    assert not _successful_completion_waiting_for_batch(scheduler, signal, 6.0)


def _root_done_signal(wake_signal_id: str, root_task_id: str) -> WakeSignal:
    return WakeSignal(
        wake_signal_id=wake_signal_id,
        thread_id=f"thread-{root_task_id}",
        reason="subagent_runner_finished",
        root_task_id=root_task_id,
        created_at=1.0,
        metadata={"status": "DONE"},
    )
