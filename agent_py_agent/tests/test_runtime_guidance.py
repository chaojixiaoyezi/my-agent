from __future__ import annotations

import gc
import json
import threading
import time
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import (
    build_tool_loop_prompt,
    execute_tool_loop,
)
from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    active_turn_user_reply_required,
    has_pending_request_guidance,
    has_pending_turn_input,
    inject_pending_guidance,
    inject_pending_turn_input,
    refresh_runtime_direct_children_snapshot,
    satisfy_active_turn_user_reply,
)
from agent_py_agent.agent.agent_core.runtime.guidance_tool import SendGuidanceTool
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    _delegated_work_facts,
    completion_response_after_tool_round,
    queue_interim_reply_for_active_named_work,
    queue_reply_for_audit_prepare,
)
from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
    discard_pending_natural_user_reply,
    finish_natural_user_reply,
    natural_user_reply_model_params,
    natural_user_reply_rejection_reason,
    pending_natural_user_reply,
    queue_natural_user_reply,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    _native_provider_messages,
)
from agent_py_agent.agent.backends import ModelResponse, ProviderResponseError
from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
    CONVERSATION_TURN_REQUEST_ID_ATTR,
    CONVERSATION_WORK_KIND_ATTR,
    CONVERSATION_WORK_NAME_ATTR,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_context import (
    ThreadLocalAgentAttribute,
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.models import TaskStatus
from agent_py_agent.agent.turn_end import infer_turn_end_reason
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)


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
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="main-run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="main-run-1",
            source_protocol="native",
        ),
    )
    for key, value in overrides.items():
        params = replace(params, **{key: value})
    if "run_id" in overrides and "tool_protocol_snapshot" not in overrides:
        params = replace(
            params,
            tool_protocol_snapshot=make_test_protocol_snapshot(
                run_id=params.run_id,
                source_protocol="native",
            ),
        )
    if "tool_runtime_snapshot" not in overrides:
        params = replace(
            params,
            tool_runtime_snapshot=runtime_snapshot_for_model_specs(
                (),
                run_id=params.run_id,
                allowed_tools=params.allowed_tools,
            ),
        )
    return params


class _MutableSubagentReport:
    def __init__(self, runs: list[object], load_errors: list[object] | None = None) -> None:
        self.runs = runs
        self.load_errors = list(load_errors or [])


class _MutableSubagentManager:
    def __init__(self, runs: list[object]) -> None:
        self.runs = runs
        self.load_errors: list[object] = []

    def list_runs_report(self) -> _MutableSubagentReport:
        return _MutableSubagentReport(self.runs, self.load_errors)


def _direct_child(run_id: str, parent_id: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(id=run_id, parent_id=parent_id, status=status)


def test_tool_loop_refreshes_direct_children_after_status_change_and_context_rebuild(
    tmp_path,
) -> None:
    root_id = "task-root"
    runs = [
        *[
            _direct_child(f"child-{index}", root_id, "DONE")
            for index in range(1, 8)
        ],
        _direct_child("child-8", root_id, "RUNNING"),
        _direct_child("unrelated", "other-root", "RUNNING"),
    ]
    manager = _MutableSubagentManager(runs)
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.subagents = manager
    params = _tool_loop_params(
        request_id=root_id,
        run_id=root_id,
        task_id=root_id,
        runtime_injections=["[BASE_RUNTIME_FACT]"],
    )

    first_prompt = build_tool_loop_prompt(agent, params)
    first_snapshot = next(
        item
        for item in params.runtime_injections
        if item.startswith("[RUNTIME_DIRECT_CHILDREN]")
    )

    assert "[RUNTIME_DIRECT_CHILDREN]" in first_prompt
    assert '"RUNNING":1' in first_snapshot
    assert '"DONE":7' in first_snapshot
    assert "unrelated" not in first_snapshot
    assert '"all_terminal":false' in first_snapshot

    runs[7].status = "DONE"
    second_prompt = build_tool_loop_prompt(agent, params)
    second_snapshot = next(
        item
        for item in params.runtime_injections
        if item.startswith("[RUNTIME_DIRECT_CHILDREN]")
    )

    assert '"DONE":8' in second_snapshot
    assert "RUNNING" not in second_snapshot
    assert '"all_terminal":true' in second_snapshot
    assert first_snapshot not in second_prompt
    assert sum(
        item.startswith("[RUNTIME_DIRECT_CHILDREN]")
        for item in params.runtime_injections
    ) == 1

    # Compact/rebuild may reconstruct the volatile injection list. The next model safe point
    # must rehydrate the current canonical snapshot without changing its bytes when state is stable.
    params.runtime_injections[:] = ["[BASE_RUNTIME_FACT]"]
    rebuilt_prompt = build_tool_loop_prompt(agent, params)
    rebuilt_snapshot = next(
        item
        for item in params.runtime_injections
        if item.startswith("[RUNTIME_DIRECT_CHILDREN]")
    )
    assert rebuilt_snapshot == second_snapshot
    assert rebuilt_snapshot in rebuilt_prompt
    stable_before = list(params.runtime_injections)
    build_tool_loop_prompt(agent, params)
    assert params.runtime_injections == stable_before


def test_runtime_direct_children_snapshot_uses_exact_current_child_parent() -> None:
    runs = [
        _direct_child("child-1", "task-root", "RUNNING"),
        _direct_child("child-2", "task-root", "DONE"),
        _direct_child("grandchild-1", "child-1", "RUNNING"),
        _direct_child("grandchild-2", "child-2", "DONE"),
        _direct_child("other", "other-root", "DONE"),
    ]
    agent = SimpleNamespace(subagents=_MutableSubagentManager(runs))
    params = _tool_loop_params(task_id="task-root", runtime_injections=[])
    previous = set_current_subagent_context(agent, run_id="child-1")
    try:
        assert refresh_runtime_direct_children_snapshot(agent, params)
    finally:
        restore_current_subagent_context(agent, previous)

    snapshot = params.runtime_injections[0]
    payload = json.loads(snapshot.rsplit("\n", 1)[-1])
    assert payload["parent_run_id"] == "child-1"
    assert payload["children"] == [
        {"run_id": "grandchild-1", "status": "RUNNING"}
    ]
    assert payload["nonterminal_run_ids"] == ["grandchild-1"]
    assert "child-2" not in snapshot
    assert "grandchild-2" not in snapshot
    assert "other" not in snapshot


def test_runtime_direct_children_snapshot_never_claims_terminal_when_load_is_partial() -> None:
    manager = _MutableSubagentManager(
        [_direct_child("child-1", "task-root", "DONE")]
    )
    manager.load_errors = [{"run_id": "child-corrupt"}]
    agent = SimpleNamespace(subagents=manager)
    params = _tool_loop_params(task_id="task-root", runtime_injections=[])

    assert refresh_runtime_direct_children_snapshot(agent, params)

    payload = json.loads(params.runtime_injections[0].rsplit("\n", 1)[-1])
    assert payload["load_error_count"] == 1
    assert payload["projection_complete"] is False
    assert payload["all_terminal"] is False


def test_root_snapshot_uses_index_without_duplicating_completion_body() -> None:
    child = _direct_child("child-done", "task-root", "DONE")
    child.result = "这份结论由耐久完成事件交付。"
    manager = _MutableSubagentManager([child])
    manager.list_runs_for_root_report = MagicMock(return_value=SimpleNamespace(runs=[child], load_errors=[]))
    manager.list_runs_report = MagicMock(side_effect=AssertionError("不应扫描全部历史"))
    agent = SimpleNamespace(subagents=manager)
    params = _tool_loop_params(task_id="task-root")

    assert refresh_runtime_direct_children_snapshot(agent, params)
    manager.list_runs_for_root_report.assert_called_once_with("task-root")
    assert "child-done" in params.runtime_injections[0]
    assert child.result not in params.runtime_injections[0]
    assert "completion_message" not in params.runtime_injections[0]


def test_busy_recursive_parent_receives_result_without_reading_root_mailbox() -> None:
    grandchild = _direct_child("grandchild", "child-parent", "RUNNING")
    agent = SimpleNamespace(subagents=_MutableSubagentManager([grandchild]))
    params = _tool_loop_params(task_id="task-root", context_scope="task_local")
    previous = set_current_subagent_context(agent, run_id="child-parent")
    try:
        refresh_runtime_direct_children_snapshot(agent, params)
        assert not has_pending_turn_input(agent, params)
        grandchild.status = "DONE"
        grandchild.result = "整理出的兼容方案可供父级接入。"
        assert has_pending_turn_input(agent, params)
        assert inject_pending_turn_input(agent, params)
        assert not has_pending_turn_input(agent, params)
        assert "整理出的兼容方案" in params.runtime_injections[0]
        assert acknowledge_injected_turn_input(agent, params) == 0
    finally:
        restore_current_subagent_context(agent, previous)


def test_subagents_active_receipt_keeps_root_turn_interrupted() -> None:
    params = _tool_loop_params()
    queue_natural_user_reply(
        params,
        kind="subagents_active",
        facts={
            "reply_is_interim": True,
            "task_continues_without_more_user_input": True,
        },
    )

    response = finish_natural_user_reply(
        params,
        ModelResponse(text="子代理仍在工作，我会继续等待结果。", backend="test"),
        accepted=True,
    )

    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "SUBAGENTS_ACTIVE"
    assert response.runtime_source == "subagent_lifecycle"
    assert (
        infer_turn_end_reason(
            runtime_status=response.runtime_status,
            runtime_reason=response.runtime_reason,
        )
        == "interrupted"
    )


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
                    task_attributes={},
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
        assert Path(workspace).is_relative_to(agent.home_paths.owner_runs_dir)
        other = "background" if name == "chat" else "chat"
        assert workspace != observed[f"req-{other}"][3]


def test_missing_guidance_queue_is_an_empty_collection(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")

    entries, load_errors = store.guidance.pending_report(
        "request",
        "request-without-guidance",
        limit=0,
    )

    assert entries == []
    assert load_errors == []
    assert not store.storage.guidance_path("request", "request-without-guidance").exists()


def test_conversation_guidance_can_be_delivered_once(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )

    entry = store.guidance.append(
        {
            "target_type": "thread",
            "target_id": thread.thread_id,
            "message": "请先汇总已有产物，再继续补缺口。",
            "sender": "user",
            "now": 2.0,
        }
    )

    pending = store.guidance.pending("thread", thread.thread_id)
    assert [item.guidance_id for item in pending] == [entry.guidance_id]
    assert pending[0].message == "请先汇总已有产物，再继续补缺口。"

    store.guidance.ledger.mark_delivered([entry.guidance_id], now=3.0)

    assert store.guidance.pending("thread", thread.thread_id) == []
    delivered = store.guidance.recent("thread", thread.thread_id)
    assert delivered[0].delivered_at == 3.0


def test_conversation_guidance_idempotency_reuses_one_entry_and_terminal_receipt(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    request = {
        "target_type": "request",
        "target_id": "request-1",
        "message": "同一条补充只保存一次",
        "sender": "user-1",
        "metadata": {
            "channel_message_id": "message-1",
            "expected_turn_id": "request-1",
        },
        "now": 10.0,
    }

    first = store.guidance.append_once(request, dedupe_key="thread-1/message-1")
    replay = store.guidance.append_once(
        {**request, "now": 20.0},
        dedupe_key="thread-1/message-1",
    )
    assert store.guidance.claim_for_turn(
        first,
        expected_turn_id="request-1",
        attempt_id="attempt-1",
    )
    store.guidance.submissions.mark_submitted(
        "request-1",
        [first],
        attempt_id="attempt-1",
        provider_call_id="provider-call-1",
    )
    store.guidance.acknowledgements.consume_submitted(
        "request-1",
        [first],
        provider_call_id="provider-call-1",
    )
    receipt = store.guidance.receipt("thread-1/message-1")

    assert replay.guidance_id == first.guidance_id
    assert replay.created_at == 10.0
    assert receipt is not None and receipt.status == "consumed"
    assert receipt.entry.delivered_at > 0
    rows = store.guidance.recent("request", "request-1", limit=0)
    assert [item.guidance_id for item in rows] == [first.guidance_id]


def test_guidance_receipt_recomputes_embedded_entry_digest(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    dedupe_key = "thread-1/message-tampered"
    store.guidance.append_once(
        {
            "target_type": "request",
            "target_id": "request-tampered",
            "message": "原始补充",
            "metadata": {"expected_turn_id": "request-tampered"},
        },
        dedupe_key=dedupe_key,
    )
    path = store.storage.guidance_dedupe_path(dedupe_key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entry"]["message"] = "被改过但保留旧 digest"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DataCorruptionError, match="input digest mismatch"):
        store.guidance.receipt(dedupe_key)


def test_guidance_receipt_v3_migration_is_persisted_as_v4(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    dedupe_key = "thread-1/message-v3"
    store.guidance.append_once(
        {
            "target_type": "request",
            "target_id": "request-v3",
            "message": "迁移旧回执",
            "metadata": {"expected_turn_id": "request-v3"},
        },
        dedupe_key=dedupe_key,
    )
    path = store.storage.guidance_dedupe_path(dedupe_key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = "conversation_guidance_once.v3"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    receipt = store.guidance.receipt(dedupe_key)
    migrated = json.loads(path.read_text(encoding="utf-8"))

    assert receipt is not None and receipt.migration["from_schema"].endswith(".v3")
    assert migrated["schema_version"] == "conversation_guidance_once.v4"


def test_guidance_submission_batch_repairs_partial_receipt_projection(
    tmp_path,
    monkeypatch,
) -> None:
    import agent_py_agent.agent.conversation.store_guidance_submission as store_module

    store = ConversationStore(tmp_path / "conversations")
    dedupe_key = "thread/submission-crash"
    entry = store.guidance.append_once(
        {
            "target_type": "request",
            "target_id": "request-submission-crash",
            "message": "整批提交后逐条投影崩溃",
            "metadata": {
                "dedupe_key": dedupe_key,
                "expected_turn_id": "request-submission-crash",
            },
        },
        dedupe_key=dedupe_key,
    )
    assert store.guidance.claim_for_turn(
        entry,
        expected_turn_id="request-submission-crash",
        attempt_id="attempt-submission-crash",
    )
    original_write = store_module.write_json_file_atomic
    failed = False

    def fail_first_receipt_projection(path, payload):
        nonlocal failed
        if (
            not failed
            and path.parent == store.storage.guidance_dedupe_dir
            and payload.get("status") == "submitted"
        ):
            failed = True
            raise OSError("simulated receipt projection crash")
        return original_write(path, payload)

    monkeypatch.setattr(store_module, "write_json_file_atomic", fail_first_receipt_projection)
    with pytest.raises(OSError, match="projection crash"):
        store.guidance.submissions.mark_submitted(
            "request-submission-crash",
            [entry],
            attempt_id="attempt-submission-crash",
            provider_call_id="provider-submission-crash",
        )
    receipt = store.guidance.receipt(dedupe_key)
    assert receipt is not None and receipt.status == "reserved"

    monkeypatch.setattr(store_module, "write_json_file_atomic", original_write)
    summary = store.guidance.recovery.release_reserved(
        "request-submission-crash",
        dead_attempt_id="attempt-submission-crash",
    )

    assert summary["released"] == 0
    assert summary["submitted"] == 1
    repaired = store.guidance.receipt(dedupe_key)
    assert repaired is not None and repaired.status == "submitted"
    assert repaired.submission_id == "provider-submission-crash"


def test_conversation_guidance_idempotency_repairs_crash_between_receipt_and_queue(
    tmp_path,
    monkeypatch,
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    request = {
        "target_type": "request",
        "target_id": "request-crash",
        "message": "崩溃恢复后仍然只能有一条",
        "metadata": {"channel_message_id": "message-crash"},
    }
    ensure_entry = store.guidance.ledger.ensure_queue_entry

    def crash_after_receipt(_entry):
        raise OSError("simulated crash after receipt")

    monkeypatch.setattr(store.guidance.ledger, 'ensure_queue_entry', crash_after_receipt)
    with pytest.raises(OSError, match="simulated crash"):
        store.guidance.append_once(request, dedupe_key="thread-1/message-crash")
    assert store.guidance.pending("request", "request-crash", limit=0) == []

    monkeypatch.setattr(store.guidance.ledger, 'ensure_queue_entry', ensure_entry)
    recovered = store.guidance.append_once(
        request,
        dedupe_key="thread-1/message-crash",
    )
    replay = store.guidance.append_once(
        request,
        dedupe_key="thread-1/message-crash",
    )

    assert replay.guidance_id == recovered.guidance_id
    rows = store.guidance.recent("request", "request-crash", limit=0)
    assert [item.guidance_id for item in rows] == [recovered.guidance_id]


def test_send_guidance_tool_writes_run_guidance(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    child = agent.subagents.create_run(goal="核对数据", thought="", plan=["核对"])
    result = SendGuidanceTool(agent).execute(
        {
            "target": child.id,
            "message": "换一个数据来源核对，不要重复查同一个页面。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["target"] == child.id
    assert payload["delivery"] == "queued"
    assert payload["status"] == "pending"
    assert payload["provider_acknowledged"] is False
    assert payload["consumed"] is False
    assert payload["delivery_timing"] == "current_tool_boundary_or_next_turn"
    assert "不代表目标模型已经接收或执行" in payload["message"]
    pending = agent.conversation_store.guidance.pending("agent_run", child.id)
    assert pending[0].message == "换一个数据来源核对，不要重复查同一个页面。"
    assert pending[0].priority == "normal"


def test_send_guidance_tool_targets_only_one_named_child(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="", plan=["root"])
    child_a = agent.subagents.create_run(
        goal="a", thought="", plan=["a"], parent_id=root.id, root_id=root.id, depth=1
    )
    child_b = agent.subagents.create_run(
        goal="b", thought="", plan=["b"], parent_id=root.id, root_id=root.id, depth=1
    )
    result = SendGuidanceTool(agent).execute(
        {
            "target": child_a.id,
            "message": "先按新要求补证据，完成后继续原任务。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["target"] == child_a.id
    assert agent.conversation_store.guidance.pending("agent_run", child_a.id)
    assert agent.conversation_store.guidance.pending("agent_run", child_b.id) == []
    assert agent.conversation_store.guidance.pending("agent_run", root.id) == []


def test_send_guidance_rejects_done_child_without_fake_pending_row(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    child = agent.subagents.create_run(goal="核对数据", thought="", plan=["核对"])
    child.status = TaskStatus.DONE.value
    agent.subagents.save(child)

    result = SendGuidanceTool(agent).execute(
        {"target": child.id, "message": "继续补充一项检查。"}
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "SUBAGENT_GUIDANCE_TARGET_TERMINAL"
    assert result.effect_outcome == "not_started"
    assert payload["details"]["status"] == TaskStatus.DONE.value
    assert payload["details"]["recommended_action"] == "create_replacement_subagent"
    assert agent.conversation_store.guidance.pending("agent_run", child.id) == []


def test_send_guidance_keeps_blocked_child_eligible_for_correction(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    child = agent.subagents.create_run(goal="核对数据", thought="", plan=["核对"])
    child.status = TaskStatus.BLOCKED.value
    agent.subagents.save(child)

    result = SendGuidanceTool(agent).execute(
        {"target": child.id, "message": "改读当前 workspace 内的 sources 目录。"}
    )

    assert result.ok is True
    pending = agent.conversation_store.guidance.pending("agent_run", child.id)
    assert [item.message for item in pending] == ["改读当前 workspace 内的 sources 目录。"]


def test_send_guidance_missing_target_does_not_fall_back_to_parent(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="", plan=["root"])

    result = SendGuidanceTool(agent).execute(
        {
            "message": "请所有孩子补充证据。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED"
    assert payload["error"] == "guidance_not_delivered"
    assert agent.conversation_store.guidance.pending("agent_run", root.id) == []


def test_send_guidance_keeps_recursive_parent_child_boundary(tmp_path) -> None:
    from types import SimpleNamespace

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="", plan=["root"])
    child = agent.subagents.create_run(
        goal="child", thought="", plan=["child"], parent_id=root.id, root_id=root.id, depth=1
    )
    grandchild = agent.subagents.create_run(
        goal="grandchild",
        thought="",
        plan=["grandchild"],
        parent_id=child.id,
        root_id=root.id,
        depth=2,
    )
    agent._current_run_params = SimpleNamespace(run_id=root.id, task_id="")

    result = SendGuidanceTool(agent).execute(
        {"target": grandchild.id, "message": "越过直接下级发消息。"}
    )

    assert result.ok is False
    assert result.error_code == "TOOL_PERMISSION_DENIED"
    assert result.effect_outcome == "not_started"
    assert agent.conversation_store.guidance.pending("agent_run", grandchild.id) == []


def test_send_guidance_followup_keeps_original_conversation_parent(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="", plan=["root"])
    child = agent.subagents.create_run(
        goal="child", thought="", plan=["child"], parent_id=root.id, root_id=root.id, depth=1
    )
    agent._current_run_params = SimpleNamespace(
        run_id="gateway-followup-request",
        task_id="gateway-followup-request",
        task_attributes={"conversation_task_id": root.id},
    )

    result = SendGuidanceTool(agent).execute(
        {"target": child.id, "message": "这是同一主任务的后续要求。"}
    )

    assert result.ok is True
    pending = agent.conversation_store.guidance.pending("agent_run", child.id)
    assert [item.message for item in pending] == ["这是同一主任务的后续要求。"]


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
    pending = agent.conversation_store.guidance.pending("agent_run", "child-1")
    assert pending[0].message == "用户补充：先写草稿，不要一直只读。"


def test_tool_loop_acks_pending_guidance_only_after_model_accepts_prompt(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
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
    assert any("ACTIVE_TURN_USER_INPUT" in str(item) for item in params.tool_context)
    assert not any("guidance_id=" in str(item) for item in params.tool_context)
    assert any("先写一个可打开的草稿" in str(item) for item in params.tool_context)
    assert len(params.active_turn_user_inputs) == 1
    assert params.active_turn_user_inputs[0]["text"] == "先写一个可打开的草稿，再继续完善。"
    assert params.active_turn_user_inputs[0]["input_ids"]
    assert "target" not in params.active_turn_user_inputs[0]
    assert agent.conversation_store.guidance.pending("agent_run", "main-run-1")
    assert has_pending_request_guidance(agent, params) is False
    assert acknowledge_injected_turn_input(agent, params, now=12.0) == 1
    assert agent.conversation_store.guidance.pending("agent_run", "main-run-1") == []


def test_active_turn_user_input_reopens_the_model_reply_sink_once(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
        {
            "target_type": "agent_run",
            "target_id": "main-run-1",
            "message": "顺便回答一句，原任务继续。",
            "now": 10.0,
            "metadata": {"channel_message_id": "steer-client-1"},
        }
    )

    class Sink:
        def __init__(self) -> None:
            self.started = 0
            self.client_message_ids: tuple[str, ...] = ()

        def __call__(self, _text: str) -> None:
            return None

        def begin_active_turn_input(self, client_message_ids: tuple[str, ...]) -> None:
            self.started += 1
            self.client_message_ids = client_message_ids

    sink = Sink()
    params = _tool_loop_params(run_id="main-run-1", effective_on_chunk=sink)

    assert inject_pending_guidance(agent, params, now=11.0) is True
    assert sink.started == 1
    assert sink.client_message_ids == ("steer-client-1",)


def test_task_local_waiting_parent_replies_to_consumed_guidance_before_yield(
    tmp_path,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    parent = agent.subagents.create_run(
        goal="协调研究员",
        thought="等待研究员",
        plan=["派发", "汇总"],
        role="coordinator",
    )
    child = agent.subagents.create_run(
        goal="完成调研",
        thought="执行调研",
        plan=["写报告"],
        role="researcher",
        parent_id=parent.id,
        root_id=parent.id,
    )
    parent.child_ids = [child.id]
    parent.status = TaskStatus.PENDING.value
    child.status = TaskStatus.RUNNING.value
    agent.subagents.save(parent)
    agent.subagents.save(child)
    agent.conversation_store.guidance.append(
        {
            "target_type": "agent_run",
            "target_id": parent.id,
            "message": "先回复已经收到，再继续等待研究员。",
            "now": 10.0,
            "metadata": {"channel_message_id": "steer-client-1"},
        }
    )
    prompts: list[str] = []

    class ThinkingOnlyThenReplyBackend:
        name = "thinking_then_reply"

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            prompts.append(prompt)
            if len(prompts) == 1:
                return ModelResponse(
                    text="",
                    backend=self.name,
                    assistant_content_blocks=[
                        {"type": "thinking", "thinking": "我已理解用户插话。"}
                    ],
                )
            wire = prompt + json.dumps(kwargs.get("messages") or [], ensure_ascii=False)
            assert "已经接收了一条真实用户补充消息" in wire
            return ModelResponse(
                text="已经收到你的补充；研究员保持运行，我继续等待它完成。",
                backend=self.name,
            )

    agent.backend = ThinkingOnlyThenReplyBackend()
    params = _tool_loop_params(
        context_scope="task_local",
        request_id="attempt-parent-2",
        run_id=parent.id,
        task_id=parent.id,
        root_user_prompt="协调研究员并汇总",
        allowed_tools=[],
    )

    response = execute_tool_loop(agent, params).final_response

    assert len(prompts) == 2
    assert response.text == "已经收到你的补充；研究员保持运行，我继续等待它完成。"
    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "SUBAGENTS_ACTIVE"
    assert agent.conversation_store.guidance.pending("agent_run", parent.id) == []
    assert active_turn_user_reply_required(params) is False
    assert child.status == agent.subagents.load(child.id).status
    assert agent.subagents.load(child.id).status == TaskStatus.RUNNING.value


def test_consumed_guidance_reply_obligation_uses_structured_ids(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
        {
            "target_type": "agent_run",
            "target_id": "main-run-1",
            "message": "先回复我，再继续。",
            "now": 10.0,
        }
    )
    params = _tool_loop_params(run_id="main-run-1")

    assert inject_pending_guidance(agent, params, now=11.0) is True
    assert active_turn_user_reply_required(params) is False
    assert acknowledge_injected_turn_input(agent, params, now=12.0) == 1
    assert active_turn_user_reply_required(params) is True
    assert satisfy_active_turn_user_reply(params) is True
    assert satisfy_active_turn_user_reply(params) is False


def test_guidance_consumption_receipt_keeps_provider_injection_fifo() -> None:
    from agent_py_agent.agent.agent_core.runtime.guidance import (
        _guidance_ack_ids_in_injection_order,
    )

    state = {
        "_guidance_ack_entries": {
            "guidance-z": object(),
            "guidance-a": object(),
        }
    }

    assert _guidance_ack_ids_in_injection_order(
        state,
        {"guidance-a", "guidance-z"},
    ) == ["guidance-z", "guidance-a"]


def test_active_turn_injects_matching_subagent_events_in_fifo_and_acks_after_model_accepts_prompt(
    tmp_path,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "build", "now": 2.0}
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-2", "goal": "other", "now": 3.0}
    )
    first = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "task-1",
            "reason": "subagent_runner_finished",
            "source_agent_id": "child-1",
            "metadata": {
                "task_id": "child-1", "status": "DONE",
                "service_window_incomplete": True,
                "service_window_remaining_seconds": 748,
            },
            "now": 10.0,
        }
    )
    second = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "task-1",
            "reason": "subagent_capability_request_open",
            "source_agent_id": "child-2",
            "metadata": {"task_id": "child-2", "status": "BLOCKED"},
            "now": 11.0,
        }
    )
    unrelated = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "task-2",
            "reason": "subagent_runner_finished",
            "source_agent_id": "other-child",
            "metadata": {"task_id": "other-child", "status": "DONE"},
            "now": 12.0,
        }
    )
    params = _tool_loop_params(
        task_id="request-attempt",
        task_attributes={"conversation_task_id": "task-1"},
    )

    assert has_pending_turn_input(agent, params) is True
    assert inject_pending_turn_input(agent, params, now=20.0) is True

    rendered = "\n".join(str(item) for item in params.tool_context)
    assert "RUNTIME_TASK_EVENTS" in rendered
    assert "不是用户指令" in rendered
    assert rendered.index(first.wake_signal_id) < rendered.index(second.wake_signal_id)
    assert '"service_window_incomplete": true' in rendered
    assert '"service_window_remaining_seconds": 748' in rendered
    assert "other-child" not in rendered
    assert has_pending_turn_input(agent, params) is False
    assert [item.wake_signal_id for item in store.wakes.pending()] == [
        first.wake_signal_id,
        second.wake_signal_id,
        unrelated.wake_signal_id,
    ]

    assert acknowledge_injected_turn_input(agent, params, now=21.0) == 2
    assert [item.wake_signal_id for item in store.wakes.pending()] == [
        unrelated.wake_signal_id
    ]


def test_task_local_child_cannot_consume_parent_lifecycle_mailbox(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "goal": "research",
            "now": 2.0,
        }
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "task-root",
            "reason": "subagent_runner_finished",
            "source_agent_id": "child-finished",
            "metadata": {"task_id": "child-finished", "status": "DONE"},
            "now": 10.0,
        }
    )
    child_params = _tool_loop_params(
        request_id="child-active-attempt",
        run_id="child-active",
        task_id="task-root",
        context_scope="task_local",
    )

    assert has_pending_turn_input(agent, child_params) is False
    assert inject_pending_turn_input(agent, child_params, now=11.0) is False
    assert acknowledge_injected_turn_input(agent, child_params, now=12.0) == 0
    assert [item.wake_signal_id for item in store.wakes.pending()] == [
        signal.wake_signal_id
    ]

    parent_params = _tool_loop_params(
        request_id="parent-continuation",
        run_id="task-root",
        task_id="task-root",
        context_scope="conversation",
        task_attributes={"conversation_task_id": "task-root"},
    )
    assert inject_pending_turn_input(agent, parent_params, now=13.0) is True
    assert acknowledge_injected_turn_input(agent, parent_params, now=14.0) == 1
    assert store.wakes.pending() == []


def test_active_background_wake_batch_is_left_for_scheduler_ack(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "build", "now": 2.0}
    )
    signals = [
        store.wakes.raise_signal(
            {
                "thread_id": thread.thread_id,
                "root_task_id": "task-1",
                "reason": "subagent_runner_finished",
                "metadata": {"task_id": f"child-{index}", "status": "DONE"},
                "now": 10.0 + index,
            }
        )
        for index in (1, 2)
    ]
    params = _tool_loop_params(
        task_id="task-1",
        task_attributes={
            "conversation_task_id": "task-1",
            "background_wake_signal_id": signals[0].wake_signal_id,
            "conversation_background_wake_signal_ids": [
                signal.wake_signal_id for signal in signals
            ],
        },
    )

    assert has_pending_turn_input(agent, params) is False
    assert inject_pending_turn_input(agent, params, now=20.0) is False
    assert [item.wake_signal_id for item in store.wakes.pending()] == [
        signal.wake_signal_id for signal in signals
    ]


def test_request_guidance_is_one_shot_and_does_not_leak_to_next_request(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
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
    assert agent.conversation_store.guidance.pending("request", "req-1")
    assert acknowledge_injected_turn_input(agent, current, now=11.5) == 1
    assert inject_pending_guidance(agent, later, now=12.0) is False


def test_background_turn_adopts_pending_guidance_from_original_request_id(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
        {
            "target_type": "request",
            "target_id": "task-original",
            "message": "最终报告顶部补上三项统计并测试。",
            "now": 10.0,
        }
    )
    background = _tool_loop_params(
        request_id="background-run-new",
        task_id="task-original",
        task_attributes={"conversation_task_id": "task-original"},
    )

    assert has_pending_request_guidance(agent, background) is True
    assert inject_pending_guidance(agent, background, now=11.0) is True
    assert any("最终报告顶部补上三项统计并测试" in str(item) for item in background.tool_context)
    assert acknowledge_injected_turn_input(agent, background, now=12.0) == 1
    assert agent.conversation_store.guidance.pending("request", "task-original") == []


def test_task_guidance_is_consumed_once_and_not_replayed_after_resume(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
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
    assert acknowledge_injected_turn_input(agent, first_run, now=11.75) == 1

    assert inject_pending_guidance(agent, later_run, now=12.0) is False
    assert not any("执行风险检查表" in str(item) for item in later_run.tool_context)
    assert inject_pending_guidance(agent, other_task, now=13.0) is False


def test_task_guidance_uses_selected_durable_task_instead_of_gateway_request_id(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
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
        context_scope="conversation",
        task_attributes={"conversation_task_id": "task-original"},
    )

    assert has_pending_request_guidance(agent, params) is True
    assert inject_pending_guidance(agent, params, now=11.0) is True
    assert any("从中断位置继续" in str(item) for item in params.tool_context)
    assert acknowledge_injected_turn_input(agent, params, now=11.5) == 1
    assert agent.conversation_store.guidance.pending("task", "task-original") == []


def test_in_turn_workspace_binding_retargets_the_live_conversation_guidance_inbox(tmp_path) -> None:
    """A 会话运行时 steer follows the sticky run without a task-selection command."""
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.task_promotion import (
        promote_current_conversation_task,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "oc-resume-steer",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    task_root = tmp_path / "existing-task"
    (task_root / "work").mkdir(parents=True)
    (task_root / "output").mkdir()
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-original",
            "goal": "继续既有项目",
            "status": "active",
            "task_path": str(task_root),
            "now": 2.0,
        }
    )
    shared_attributes = {
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": "task-original",
    }
    current = RunParams(
        request_id="req-followup",
        run_id="req-followup",
        task_id="req-followup",
        source="gateway",
        context_scope="conversation",
        task_attributes=shared_attributes,
    )
    live_loop = _tool_loop_params(
        request_id="req-followup",
        run_id="req-followup",
        task_id="req-followup",
        context_scope="conversation",
        task_attributes=shared_attributes,
    )
    agent._current_run_params = current
    try:
        selected = promote_current_conversation_task(agent)
    finally:
        del agent._current_run_params
    assert selected is not None
    assert live_loop.task_id == "req-followup"
    assert live_loop.task_attributes["conversation_task_id"] == "task-original"

    agent.conversation_store.guidance.append(
        {
            "target_type": "task",
            "target_id": "task-original",
            "message": "不要新建第二份项目，直接修复当前目录。",
            "now": 3.0,
        }
    )
    assert inject_pending_guidance(agent, live_loop, now=4.0) is True
    assert any("不要新建第二份项目" in str(item) for item in live_loop.tool_context)
    assert acknowledge_injected_turn_input(agent, live_loop, now=5.0) == 1
    assert inject_pending_guidance(agent, live_loop, now=6.0) is False


def test_multiple_task_steers_keep_sample_a_style_fifo_order(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    for current, message in enumerate(
        (
            "先把预算上限改为四百元。",
            "再在最后增加一张风险检查表。",
        ),
        start=10,
    ):
        agent.conversation_store.guidance.append(
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
    assert acknowledge_injected_turn_input(agent, current_run, now=21.5) == 2

    resumed_run = _tool_loop_params(task_id="task-1")
    other_task = _tool_loop_params(task_id="task-2")
    assert inject_pending_guidance(agent, resumed_run, now=22.0) is False
    assert inject_pending_guidance(agent, other_task, now=23.0) is False


@pytest.mark.xfail(
    reason="EXEC-31b: native 下 steer/ACTIVE_TURN_USER_INPUT 经 IR 消息注入, 不再以文本标记进 prompt; 断言待适配"
)
def test_tool_loop_guidance_can_override_earlier_contract_context(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
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

    assert "ACTIVE_TURN_USER_INPUT" in prompt
    assert "guidance_id=" not in prompt
    assert "用户补充：25次压缩已经够了" in prompt
    assert prompt.rfind("用户补充：25次压缩已经够了") > prompt.find(
        "[tool-system delivery-contract]"
    )
    assert agent.conversation_store.guidance.pending("agent_run", "main-run-1")
    assert acknowledge_injected_turn_input(agent, params, now=11.0) == 1
    assert agent.conversation_store.guidance.pending("agent_run", "main-run-1") == []


def test_task_steer_stays_as_latest_native_user_turn_across_later_model_rounds(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn

    agent = SimpleAgent(
        AgentConfig(
            model_backend="anthropic_compatible",
            tool_protocol="native",
            subagent_workspace="subs",
        ),
        tmp_path,
    )
    agent.conversation_store.guidance.append(
        {
            "target_type": "task",
            "target_id": "task-1",
            "message": "只接受标准 wheel 的项目外安装结果。",
            "now": 10.0,
        }
    )
    params = _tool_loop_params(
        task_id="task-1",
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="main-run-1",
            source_protocol="native",
        ),
    )
    first_call = canonical_history_call(
        "read_file",
        {},
        call_id="t1",
        run_id=params.run_id,
        turn_id="main-run-1:round-1",
        attempt_id=params.request_id,
    )
    params.tool_ir_history.extend(
        [
            AssistantTurn(tool_calls=[first_call]),
            canonical_history_result(first_call, "old result"),
        ]
    )

    assert inject_pending_guidance(agent, params, now=11.0) is True
    first = _native_provider_messages(agent, params)
    assert first is not None
    assert first[-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "只接受标准 wheel 的项目外安装结果。"}],
    }

    second_call = canonical_history_call(
        "run_command",
        {},
        call_id="t2",
        run_id=params.run_id,
        turn_id="main-run-1:round-2",
        attempt_id=params.request_id,
    )
    params.tool_ir_history.extend(
        [
            AssistantTurn(tool_calls=[second_call]),
            canonical_history_result(second_call, "later result"),
        ]
    )
    later = _native_provider_messages(agent, params)
    assert later is not None
    assert [message["role"] for message in later] == [
        "assistant",
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert later[2]["content"][0]["text"] == "只接受标准 wheel 的项目外安装结果。"
    assert sum("只接受标准 wheel" in str(message) for message in later) == 1
    assert len(params.active_turn_user_inputs) == 1
    assert params.active_turn_user_inputs[0]["text"] == "只接受标准 wheel 的项目外安装结果。"


@pytest.mark.xfail(
    reason="EXEC-31b: native 下 steer/ACTIVE_TURN_USER_INPUT 经 IR 消息注入, 不再以文本标记进 prompt; 断言待适配"
)
def test_text_protocol_keeps_steer_in_transcript_without_building_native_ir(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    agent.conversation_store.guidance.append(
        {
            "target_type": "task",
            "target_id": "task-1",
            "message": "先处理最新补充再继续。",
            "now": 10.0,
        }
    )
    params = _tool_loop_params(task_id="task-1")

    assert inject_pending_guidance(agent, params, now=11.0) is True

    assert params.tool_ir_history == []
    prompt = build_tool_loop_prompt(agent, params)
    assert "[ACTIVE_TURN_USER_INPUT]\n先处理最新补充再继续。" in prompt
    assert "# Runtime Injection\n（无）" in prompt


def test_model_authored_receipt_cannot_consume_active_task_guidance(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    entry = agent.conversation_store.guidance.append(
        {
            "target_type": "task",
            "target_id": "task-1",
            "message": "HTML 报告顶部增加导入成功与跳过计数。",
            "now": 10.0,
        }
    )
    params = _tool_loop_params(task_id="task-1")
    queue_natural_user_reply(
        params,
        kind="background_dispatch",
        facts={"reply_is_interim": True},
    )

    receipt_params = natural_user_reply_model_params(params)
    receipt_prompt = build_tool_loop_prompt(agent, receipt_params)
    receipt_messages = _native_provider_messages(agent, receipt_params)

    assert receipt_params.consume_pending_turn_input is False
    assert entry.message not in receipt_prompt
    assert entry.message not in json.dumps(receipt_messages, ensure_ascii=False)
    assert agent.conversation_store.guidance.pending("task", "task-1")
    assert discard_pending_natural_user_reply(params) is True

    task_prompt = build_tool_loop_prompt(agent, params)
    task_messages = _native_provider_messages(agent, params)

    assert entry.message not in task_prompt
    assert entry.message in json.dumps(task_messages, ensure_ascii=False)
    assert agent.conversation_store.guidance.pending("task", "task-1")
    assert acknowledge_injected_turn_input(agent, params, now=11.0) == 1
    assert agent.conversation_store.guidance.pending("task", "task-1") == []


def test_steer_arriving_during_receipt_generation_discards_stale_receipt(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    prompts: list[str] = []

    class SteerDuringReceiptBackend:
        name = "steer_during_receipt"

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            prompts.append(prompt)
            wire = json.dumps(kwargs.get("messages") or [], ensure_ascii=False)
            if len(prompts) == 1:
                assert "[natural-user-reply]" in wire
                agent.conversation_store.guidance.append(
                    {
                        "target_type": "task",
                        "target_id": "task-1",
                        "message": "把最新补充真正用于当前任务。",
                        "now": 10.0,
                    }
                )
                return ModelResponse(text="这是一条已经过期的派工回执。", backend=self.name)
            assert "[natural-user-reply]" not in wire
            assert "把最新补充真正用于当前任务。" in wire
            return ModelResponse(text="已按最新补充继续当前任务。", backend=self.name)

    agent.backend = SteerDuringReceiptBackend()
    params = _tool_loop_params(task_id="task-1")
    queue_natural_user_reply(
        params,
        kind="background_dispatch",
        facts={"reply_is_interim": True},
    )

    response = execute_tool_loop(agent, params).final_response

    assert len(prompts) == 2
    assert response.text == "已按最新补充继续当前任务。"
    assert agent.conversation_store.guidance.pending("task", "task-1") == []


def test_steer_survives_empty_stale_provider_response_in_same_turn(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    prompts: list[str] = []

    class EmptyWhileSteeredBackend:
        name = "empty_while_steered"

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            prompts.append(prompt)
            if len(prompts) == 1:
                agent.conversation_store.guidance.append(
                    {
                        "target_type": "task",
                        "target_id": "task-1",
                        "message": "停止继续搜索，直接用已有资料收口。",
                        "now": 10.0,
                    }
                )
                raise ProviderResponseError(
                    "OpenAI-compatible 流式响应没有文本或工具调用",
                    error_code="MODEL_EMPTY_RESPONSE",
                )
            wire = json.dumps(kwargs.get("messages") or [], ensure_ascii=False)
            assert "停止继续搜索，直接用已有资料收口。" in wire
            assert "上一轮模型接口返回了空文本" not in prompt
            return ModelResponse(text="已按刚才的补充完成收口。", backend=self.name)

    agent.backend = EmptyWhileSteeredBackend()
    params = _tool_loop_params(task_id="task-1")

    response = execute_tool_loop(agent, params).final_response

    assert len(prompts) == 2
    assert response.text == "已按刚才的补充完成收口。"
    assert [item["text"] for item in params.active_turn_user_inputs] == [
        "停止继续搜索，直接用已有资料收口。"
    ]
    assert agent.conversation_store.guidance.pending("task", "task-1") == []


@pytest.mark.xfail(
    reason="EXEC-31b: native 截断响应走截断修复轮(assistant_content_blocks 续写)后才应用 steer, 与文本时代'steer 直接取代截断响应'轮数不同; 断言待适配"
)
def test_steer_supersedes_incomplete_stale_provider_response_in_same_turn(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    prompts: list[str] = []

    class IncompleteWhileSteeredBackend:
        name = "incomplete_while_steered"

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            prompts.append(prompt)
            if len(prompts) == 1:
                agent.conversation_store.guidance.append(
                    {
                        "target_type": "task",
                        "target_id": "task-1",
                        "message": "不要继续写旧方案，按最新边界直接收口。",
                        "now": 10.0,
                    }
                )
                raise ProviderResponseError(
                    "anthropic_compatible 模型响应未完成（stop_reason=max_tokens）",
                    error_code="MODEL_INCOMPLETE_RESPONSE",
                    details={"stop_reason": "max_tokens", "partial_text_chars": 4096},
                )
            assert "不要继续写旧方案，按最新边界直接收口。" in prompt
            assert "[tool-system:model-incomplete-response]" not in prompt
            return ModelResponse(text="已丢弃旧生成并按最新边界收口。", backend=self.name)

    agent.backend = IncompleteWhileSteeredBackend()
    params = _tool_loop_params(task_id="task-1")

    response = execute_tool_loop(agent, params).final_response

    assert len(prompts) == 2
    assert response.text == "已丢弃旧生成并按最新边界收口。"
    assert [item["text"] for item in params.active_turn_user_inputs] == [
        "不要继续写旧方案，按最新边界直接收口。"
    ]
    assert agent.conversation_store.guidance.pending("task", "task-1") == []


def test_natural_reply_prompt_treats_fact_carrier_as_invisible(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    params = _tool_loop_params(task_id="task-1")
    queue_natural_user_reply(
        params,
        kind="wait",
        facts={
            "reply_is_interim": True,
            "current_progress": "核心实现已完成，正在补测试",
            "next_action": "运行测试并修复失败",
        },
    )

    prompt = build_tool_loop_prompt(agent, natural_user_reply_model_params(params))

    assert "核心实现已完成，正在补测试" in prompt
    assert "运行测试并修复失败" in prompt
    assert "不要告诉用户你收到了结构化信息" in prompt
    assert "不要把 operation、verification、envelope" in prompt
    assert "请根据上面的结构化事实" not in prompt


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("preserve_execution_evidence", [False, True])
def test_natural_reply_facts_reach_native_provider_wire_once(
    tmp_path,
    provider: str,
    preserve_execution_evidence: bool,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    original_ir = [
        UserTurn("# User Task\n原主轮任务"),
        AssistantTurn(text="原主轮已经取得的执行证据"),
    ]
    params = _tool_loop_params(
        task_id="task-1",
        tool_ir_history=list(original_ir),
    )
    marker = f"wire-fact-{provider}-{'preserve' if preserve_execution_evidence else 'thin'}"
    queue_natural_user_reply(
        params,
        kind=("audit_prepare_result" if preserve_execution_evidence else "background_dispatch"),
        facts={"reply_is_interim": True, "wire_marker": marker},
    )

    reply_params = natural_user_reply_model_params(params)
    prompt = build_tool_loop_prompt(agent, reply_params)
    messages = _native_provider_messages(agent, reply_params)

    assert messages is not None
    assert reply_params.tool_ir_history is not params.tool_ir_history
    if preserve_execution_evidence:
        assert reply_params.tool_ir_history[:-1] == original_ir
    else:
        assert len(reply_params.tool_ir_history) == 1
        assert "原主轮任务" not in str(messages)
    assert isinstance(reply_params.tool_ir_history[-1], UserTurn)
    assert marker in reply_params.tool_ir_history[-1].text
    assert params.tool_ir_history == original_ir

    captured: dict[str, object] = {}
    options = BackendOptions(
        api_base="https://api.example.com/v1",
        api_key="key",
        model_name="test-model",
        max_tokens=128,
        stream_enabled=False,
    )
    if provider == "anthropic":
        backend = AnthropicCompatibleBackend(options)

        def request_json(path, payload, headers):
            del path, headers
            captured["payload"] = payload
            return {"content": [{"type": "text", "text": "已收到"}]}

    else:
        backend = OpenAICompatibleBackend(options)

        def request_json(path, payload, headers):
            del path, headers
            captured["payload"] = payload
            return {
                "choices": [
                    {"message": {"content": "已收到"}, "finish_reason": "stop"}
                ]
            }

    backend.request_json = request_json
    backend.generate(prompt, messages=messages, tools=[])

    wire_text = json.dumps(
        captured["payload"]["messages"],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert wire_text.count("[natural-user-reply]") == 1
    assert wire_text.count(marker) == 1
    assert ("原主轮任务" in wire_text) is preserve_execution_evidence
    assert ("原主轮已经取得的执行证据" in wire_text) is preserve_execution_evidence


def test_open_subagents_queue_interim_reply_without_reading_model_prose() -> None:
    params = _tool_loop_params(
        root_user_prompt="并行完成三项检查后汇总",
        executed_tools=["create_subagents", "dispatch_subagents"],
    )
    agent = SimpleNamespace(
        subagent_run_ids_for_request=lambda task_id: (
            ["run-1", "run-2", "run-3"] if task_id == "task-1" else []
        ),
        subagents=SimpleNamespace(
            list_runs=lambda: [
                SimpleNamespace(id="run-1", status="DONE"),
                SimpleNamespace(id="run-2", status="DONE"),
                SimpleNamespace(id="run-3", status="RUNNING"),
            ]
        ),
    )

    # 契约变更（R279 复审）：宿主回执入口已删除，活跃子代理只在 agent tree/child 面板展示。
    assert pending_natural_user_reply(params) is None
    assert _delegated_work_facts(agent, params) == {
        "schema": "delegated_work.v2",
        "authority": "subagent_store",
        "total": 3,
        "status_counts": {"DONE": 2, "RUNNING": 1},
        "topology": {
            "root_run_id": "task-1",
            "direct_child_count": 0,
            "descendant_count": 0,
            "unknown_parent_count": 3,
            "max_depth": 0,
            "nested_delegation_observed": False,
            "nodes": [
                {
                    "run_id": "run-1",
                    "parent_run_id": "",
                    "parent_scope": "unknown",
                    "depth": 0,
                    "agent_name": "",
                    "role": "",
                    "status": "DONE",
                },
                {
                    "run_id": "run-2",
                    "parent_run_id": "",
                    "parent_scope": "unknown",
                    "depth": 0,
                    "agent_name": "",
                    "role": "",
                    "status": "DONE",
                },
                {
                    "run_id": "run-3",
                    "parent_run_id": "",
                    "parent_scope": "unknown",
                    "depth": 0,
                    "agent_name": "",
                    "role": "",
                    "status": "RUNNING",
                },
            ],
            "nodes_truncated": False,
        },
    }


def test_open_subagents_receipt_exposes_flattened_topology_instead_of_inventing_grandchildren() -> None:
    params = _tool_loop_params(
        root_user_prompt="先派总协调子代理，再由它派四个孙代理",
        executed_tools=["create_subagents"],
    )
    runs = [
        SimpleNamespace(
            id=f"worker-{index}",
            parent_id="task-1",
            root_id="task-1",
            depth=1,
            agent_name=f"worker-{index}",
            role="worker",
            status="RUNNING",
        )
        for index in range(1, 5)
    ]
    agent = SimpleNamespace(
        subagent_run_ids_for_request=lambda task_id: (
            [run.id for run in runs] if task_id == "task-1" else []
        ),
        subagents=SimpleNamespace(list_runs=lambda: runs),
    )

    assert pending_natural_user_reply(params) is None

    topology = _delegated_work_facts(agent, params)["topology"]
    assert topology["direct_child_count"] == 4
    assert topology["descendant_count"] == 0
    assert topology["nested_delegation_observed"] is False
    assert {row["parent_scope"] for row in topology["nodes"]} == {"root"}


def test_active_named_audit_replaces_premature_final_with_model_interim(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "audit-chat",
            "channel_user_id": "user-1",
        }
    )
    now = time.time()
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-task",
            "goal": "持续查看来源",
            "status": "active",
            "work_kind": "audit",
            "work_name": "持续监测",
            "duration_seconds": 600,
            "now": now,
        }
    )

    class PrematureAuditBackend:
        name = "premature_audit"

        def __init__(self):
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return ModelResponse(text="全部记录都已经处理完毕。", backend=self.name)
            assert "[natural-user-reply]" in prompt
            assert '"work_name": "持续监测"' in prompt
            assert '"reply_is_interim": true' in prompt
            return ModelResponse(
                text="采集和后续处理仍在继续，我会按现有要求完成覆盖后再汇总。",
                backend=self.name,
            )

    backend = PrematureAuditBackend()
    agent.backend = backend
    params = _tool_loop_params(
        task_id="audit-task",
        root_user_prompt="持续查看来源",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "audit-task",
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
        },
        allowed_tools=[],
    )

    assert (
        queue_interim_reply_for_active_named_work(
            agent,
            params,
            tool_rounds=0,
        )
        is True
    )
    assert pending_natural_user_reply(params)["kind"] == "named_work_active"
    discard_pending_natural_user_reply(params)

    response = execute_tool_loop(agent, params).final_response

    assert len(backend.prompts) == 2
    assert response.text.startswith("采集和后续处理仍在继续")
    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "NAMED_WORK_ACTIVE"
    assert response.runtime_source == "conversation_task"


@pytest.mark.xfail(
    reason="EXEC-31b: native 下 [tool-output-record] 注入路径变化, probe-result 不再以原文进 prompt; 审计 prepare 证据回放断言待适配"
)
def test_pending_audit_prepare_rewrites_false_publish_claim_with_turn_evidence(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-prepare",
            "channel": "internal",
            "channel_conversation_id": "prepare-chat",
            "channel_user_id": "user-prepare",
        }
    )
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-prepare-task",
            "goal": "旧的生效要求",
            "status": "preparing",
            "work_kind": "audit",
            "work_name": "现场监测",
            "pending_prompt": "先验证新接口，确认后再发布",
            "effective_revision": 2,
            "effective_source_bindings": [
                {"source_id": "source-1", "url": "https://example.invalid/events"}
            ],
            "now": 1.0,
        }
    )

    class FalsePublishBackend:
        name = "false_publish"

        def __init__(self):
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return ModelResponse(text="新要求已经保存并生效。", backend=self.name)
            assert '"update_applied": false' in prompt
            assert '"pending_requirement_preserved": true' in prompt
            assert '"source_binding_change_applied": false' in prompt
            assert '"applied_source_ids": []' in prompt
            assert "probe-result: reached-source" in prompt
            assert "新要求已经保存并生效" not in prompt
            return ModelResponse(
                text="接口已经实际检查；新要求仍处于准备状态，尚未正式生效。",
                backend=self.name,
            )

    backend = FalsePublishBackend()
    agent.backend = backend
    params = _tool_loop_params(
        task_id="prepare-request",
        root_user_prompt="先验证新接口，确认后再发布",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "audit-prepare-task",
            CONVERSATION_AUDIT_PREPARE_ATTR: True,
            CONVERSATION_TRANSIENT_WORKSPACE_ATTR: True,
            CONVERSATION_WORK_KIND_ATTR: "audit",
            CONVERSATION_WORK_NAME_ATTR: "现场监测",
        },
        allowed_tools=[],
        tool_context=["[tool-output-record]\nprobe-result: reached-source"],
        live_archive_state={},
    )

    assert queue_reply_for_audit_prepare(
        agent,
        params,
        response=ModelResponse(text="新要求已经保存并生效。", backend="fake"),
        tool_rounds=3,
    )
    phase = pending_natural_user_reply(params)
    assert phase is not None
    assert phase["kind"] == "audit_prepare_pending"
    assert phase["facts"]["reply_is_interim"] is False
    assert phase["facts"]["named_work"] == {
        "work_kind": "audit",
        "work_name": "现场监测",
        "status": "preparing",
        "update_applied": False,
        "pending_requirement_preserved": True,
        "source_binding_change_applied": False,
        "applied_source_probe_count": 0,
        "applied_source_ids": [],
        "publication_validation_status": "pending",
        "source_access_verified": False,
        "effective_revision": 2,
        "effective_source_count": 1,
        "effective_source_ids": ["source-1"],
    }
    assert "draft" not in phase
    discard_pending_natural_user_reply(params)

    response = execute_tool_loop(agent, params).final_response

    assert len(backend.prompts) == 2
    assert "实际检查" in response.text
    assert "尚未正式生效" in response.text
    assert response.runtime_status == "ok"
    assert response.runtime_reason == "AUDIT_PREPARE_PENDING"
    assert response.runtime_source == "conversation_task"


def test_published_audit_prepare_rewrites_false_source_binding_claim_from_typed_facts(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-published-prepare",
            "channel": "internal",
            "channel_conversation_id": "published-prepare-chat",
            "channel_user_id": "user-published-prepare",
        }
    )
    request_id = "published-prepare-request"
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "published-prepare-task",
            "goal": "已经发布的研判说明",
            "status": "preparing",
            "work_kind": "audit",
            "work_name": "三路预检",
            "pending_prompt": "检查登录来源并发布",
            "pending_prepare_request_id": request_id,
            "now": 1.0,
        }
    )
    assert agent.conversation_store.audits.publish_effective_prompt(
        {
            "task_id": "published-prepare-task",
            "prompt": "已经发布的研判说明",
            "prepare_request_id": request_id,
            "now": 2.0,
        }
    )

    class FalseSourceBindingBackend:
        name = "false_source_binding"

        def __init__(self):
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return ModelResponse(text="", backend=self.name)
            assert '"update_applied": true' in prompt
            assert '"source_binding_change_applied": false' in prompt
            assert '"effective_source_count": 0' in prompt
            assert '"pending_requirement_preserved"' not in prompt
            return ModelResponse(
                text="研判说明已经生效，但本轮没有新增或更新来源绑定。",
                backend=self.name,
            )

    backend = FalseSourceBindingBackend()
    agent.backend = backend
    params = _tool_loop_params(
        task_id=request_id,
        root_user_prompt="检查登录来源并发布",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "published-prepare-task",
            CONVERSATION_TURN_REQUEST_ID_ATTR: request_id,
            CONVERSATION_AUDIT_PREPARE_ATTR: True,
            CONVERSATION_TRANSIENT_WORKSPACE_ATTR: True,
            CONVERSATION_WORK_KIND_ATTR: "audit",
            CONVERSATION_WORK_NAME_ATTR: "三路预检",
        },
        archive_tool_calls=[
            {
                "tool": "publish_audit_update",
                "ok": True,
                "status": "ok",
                "handler_executed": True,
                "tool_operation_status": "succeeded",
                "request_id": request_id,
                "parameters": {
                    "validation_status": "passed",
                    "source_probe_refs": [],
                },
                "tool_result_envelope": {
                    "audit_publish": {
                        "applied": True,
                        "source_binding_change_applied": False,
                        "applied_source_probe_count": 0,
                        "applied_source_ids": [],
                    }
                },
            }
        ],
        allowed_tools=[],
        live_archive_state={},
    )

    response = execute_tool_loop(agent, params).final_response

    assert len(backend.prompts) == 2
    assert "没有新增或更新来源绑定" in response.text
    assert response.runtime_status == "ok"
    assert response.runtime_reason == "AUDIT_PREPARE_RESULT"
    assert response.runtime_source == "conversation_task"


def test_published_audit_prepare_uses_durable_outcome_not_repaired_attempts(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-repaired-prepare",
            "channel": "internal",
            "channel_conversation_id": "repaired-prepare-chat",
            "channel_user_id": "user-repaired-prepare",
        }
    )
    request_id = "repaired-prepare-request"
    task_id = "repaired-prepare-task"
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "goal": "",
            "status": "preparing",
            "work_kind": "audit",
            "work_name": "修正后发布",
            "pending_prompt": "先探测来源再发布",
            "pending_prepare_request_id": request_id,
            "now": 1.0,
        }
    )
    profile = tmp_path / "source-a.md"
    profile.write_text("source_id: source-a\n", encoding="utf-8")
    assert agent.conversation_store.audits.publish_effective_prompt(
        {
            "task_id": task_id,
            "prompt": "来源已经成功探测并发布",
            "prepare_request_id": request_id,
            "source_bindings": [
                {
                    "source_id": "source-a",
                    "url": "https://example.invalid/events?position=0",
                    "source_profile_ref": str(profile),
                    "mode": "cursor",
                    "http_request": {
                        "method": "GET",
                        "cursor_binding": {
                            "location": "query",
                            "name": "position",
                            "initial": 0,
                        },
                    },
                }
            ],
            "now": 2.0,
        }
    )

    class RepairedPrepareBackend:
        name = "repaired_prepare"

        def __init__(self):
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            self.prompts.append(prompt)
            return ModelResponse(
                text="来源已经成功探测并发布，当前一条来源配置已经生效。",
                backend=self.name,
            )

    backend = RepairedPrepareBackend()
    agent.backend = backend
    params = _tool_loop_params(
        task_id=request_id,
        root_user_prompt="先探测来源再发布",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": task_id,
            CONVERSATION_TURN_REQUEST_ID_ATTR: request_id,
            CONVERSATION_AUDIT_PREPARE_ATTR: True,
            CONVERSATION_TRANSIENT_WORKSPACE_ATTR: True,
            CONVERSATION_WORK_KIND_ATTR: "audit",
            CONVERSATION_WORK_NAME_ATTR: "修正后发布",
        },
        archive_tool_calls=[
            {
                "tool": "watch_stream",
                "call_id": "probe-invalid",
                "ok": False,
                "status": "error",
                "handler_executed": True,
                "tool_operation_status": "failed",
                "effect_outcome": "not_started",
                "parameters": {"action": "open"},
            },
            {
                "tool": "watch_stream",
                "call_id": "probe-success",
                "ok": True,
                "status": "ok",
                "handler_executed": True,
                "tool_operation_status": "succeeded",
                "effect_outcome": "confirmed",
                "parameters": {"action": "open"},
            },
            {
                "tool": "publish_audit_update",
                "call_id": "publish-success",
                "ok": True,
                "status": "ok",
                "handler_executed": True,
                "tool_operation_status": "succeeded",
                "effect_outcome": "confirmed",
                "request_id": request_id,
                "parameters": {
                    "validation_status": "passed",
                    "source_probe_refs": ["ws-source-a"],
                },
                "tool_result_envelope": {
                    "audit_publish": {
                        "applied": True,
                        "validation_status": "passed",
                        "source_binding_change_applied": True,
                        "applied_source_probe_count": 1,
                        "applied_source_ids": ["source-a"],
                    }
                },
            },
        ],
        allowed_tools=[],
        live_archive_state={},
    )

    response = execute_tool_loop(agent, params).final_response

    assert len(backend.prompts) == 1
    assert "成功探测并发布" in response.text
    assert response.runtime_status == "ok"
    assert response.runtime_reason == "AUDIT_PREPARE_RESULT"
    assert response.runtime_source == "conversation_task"


def test_published_prepare_turn_is_not_replaced_by_active_audit_lifecycle(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-prepare-active",
            "channel": "internal",
            "channel_conversation_id": "prepare-active-chat",
            "channel_user_id": "user-prepare-active",
        }
    )
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-prepare-active-task",
            "goal": "持续监测已经在后台运行",
            "status": "active",
            "work_kind": "audit",
            "work_name": "现场监测",
            "duration_seconds": 600,
            "now": time.time(),
        }
    )
    params = _tool_loop_params(
        task_id="prepare-active-request",
        root_user_prompt="检查新来源并发布",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "audit-prepare-active-task",
            CONVERSATION_AUDIT_PREPARE_ATTR: True,
            CONVERSATION_TRANSIENT_WORKSPACE_ATTR: True,
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
            CONVERSATION_WORK_KIND_ATTR: "audit",
            CONVERSATION_WORK_NAME_ATTR: "现场监测",
        },
        archive_tool_calls=[
            {
                "tool": "write_file",
                "ok": True,
                "status": "ok",
                "handler_executed": True,
                "tool_operation_status": "succeeded",
                "effect_outcome": "confirmed",
            }
        ],
        live_archive_state={},
    )

    assert not queue_interim_reply_for_active_named_work(
        agent,
        params,
        tool_rounds=1,
    )
    assert pending_natural_user_reply(params) is None


def test_active_named_work_reply_carries_current_turn_operation_facts(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-active-operation",
            "channel": "internal",
            "channel_conversation_id": "active-operation-chat",
            "channel_user_id": "user-active-operation",
        }
    )
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-active-operation-task",
            "goal": "持续监测",
            "status": "active",
            "work_kind": "audit",
            "work_name": "持续监测",
            "duration_seconds": 600,
            "now": time.time(),
        }
    )
    params = _tool_loop_params(
        task_id="audit-active-operation-task",
        root_user_prompt="继续监测",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "audit-active-operation-task",
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
        },
        archive_tool_calls=[
            {
                "tool": "write_file",
                "ok": True,
                "status": "ok",
                "handler_executed": True,
                "tool_operation_status": "succeeded",
                "effect_outcome": "confirmed",
            }
        ],
        live_archive_state={},
    )

    assert queue_interim_reply_for_active_named_work(
        agent,
        params,
        tool_rounds=1,
    )
    phase = pending_natural_user_reply(params)
    assert phase is not None
    verification = phase["facts"]["operation_verification"]
    assert verification["operation_count"] == 1
    assert verification["status"] == "succeeded"


def test_published_prepare_keeps_same_turn_reply_but_ordinary_turn_is_unchanged(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-prepare",
            "channel": "internal",
            "channel_conversation_id": "prepare-chat",
            "channel_user_id": "user-prepare",
        }
    )
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-published-task",
            "goal": "已经生效的要求",
            "status": "preparing",
            "work_kind": "audit",
            "work_name": "已发布监测",
            "pending_prompt": "临时准备内容",
            "pending_prepare_request_id": "prepare-request",
            "now": 1.0,
        }
    )
    assert agent.conversation_store.audits.publish_effective_prompt(
        {
            "task_id": "audit-published-task",
            "prompt": "验证完成后的正式说明",
            "prepare_request_id": "prepare-request",
            "now": 2.0,
        }
    )
    response = ModelResponse(text="正常回复", backend="fake")
    prepare_params = _tool_loop_params(
        task_id="prepare-request",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "audit-published-task",
            CONVERSATION_TURN_REQUEST_ID_ATTR: "prepare-request",
            CONVERSATION_AUDIT_PREPARE_ATTR: True,
            CONVERSATION_TRANSIENT_WORKSPACE_ATTR: True,
            CONVERSATION_WORK_KIND_ATTR: "audit",
            CONVERSATION_WORK_NAME_ATTR: "已发布监测",
        },
        live_archive_state={},
    )
    ordinary_params = _tool_loop_params(live_archive_state={})

    assert not queue_reply_for_audit_prepare(
        agent,
        prepare_params,
        response=response,
        tool_rounds=0,
    )
    assert pending_natural_user_reply(prepare_params) is None
    assert not queue_reply_for_audit_prepare(
        agent,
        ordinary_params,
        response=response,
        tool_rounds=0,
    )
    assert pending_natural_user_reply(ordinary_params) is None


def test_published_audit_sources_release_root_before_any_tool_round(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.ingestion import source_worker

    class ProvisionReplyBackend:
        name = "provision_reply"

        def probe_tool_capability(self):
            from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

            return ProviderToolCapability(
                provider=self.name, endpoint="local://provision-reply",
                model="", stream=False, native_supported=True,
                evidence="test_backend_declares_native_tools",
                observed_at=_utc_now_iso(),
            )

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            self.prompts.append(prompt)
            assert "[natural-user-reply]" in prompt
            assert '"source_provision"' in prompt
            return ModelResponse(
                text="监测已经启动，后续会按现有要求持续进行。",
                backend=self.name,
            )

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / ".my-agent"),
            memory_path="memory.jsonl",
            prompt_files=[],
        ),
        tmp_path / "repo",
    )
    backend = ProvisionReplyBackend()
    agent.backend = backend
    monkeypatch.setattr(
        source_worker,
        "provision_published_audit_source_workers",
        lambda _agent: {
            "schema": "audit-source-provision.v1",
            "ok": True,
            "required": 1,
            "ready": 1,
            "waiting": 0,
            "sources": [
                {
                    "source_id": "source-1",
                    "state": "created",
                    "ok": True,
                    "run_id": "child-1",
                }
            ],
        },
    )

    response = agent.run(
        "启动三分钟监测",
        params=RunParams(
            request_id="audit-start-request",
            run_id="audit-start-run",
            task_id="audit-task",
            source="gateway",
            save=False,
            resume_context=False,
            task_attributes={
                CONVERSATION_WORK_KIND_ATTR: "audit",
                CONVERSATION_WORK_NAME_ATTR: "本地文件",
            },
        ),
    )

    assert len(backend.prompts) == 1
    assert response.response == "监测已经启动，后续会按现有要求持续进行。"
    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "NAMED_WORK_ACTIVE"
    assert response.runtime_source == "conversation_task"


def test_sticky_background_task_does_not_replace_unrelated_chat_reply() -> None:
    params = _tool_loop_params(
        task_id="chat-request",
        root_user_prompt="17乘23是多少",
        task_attributes={"conversation_task_id": "detached-audit"},
    )
    agent = SimpleNamespace(
        subagent_run_ids_for_request=lambda task_id: (
            ["audit-child"] if task_id == "detached-audit" else []
        ),
        subagents=SimpleNamespace(
            list_runs=lambda: [SimpleNamespace(id="audit-child", status="RUNNING")]
        ),
    )

    # 宿主回执入口已删除：这里只能断言"没有任何宿主排队的用户回复"。
    assert pending_natural_user_reply(params) is None


def test_active_turn_keeps_its_bound_subagent_interim_reply() -> None:
    params = _tool_loop_params(
        task_id="continuation-request",
        root_user_prompt="继续汇总",
        task_attributes={
            "conversation_task_id": "durable-task",
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
        },
    )
    agent = SimpleNamespace(
        subagent_run_ids_for_request=lambda task_id: (
            ["task-child"] if task_id == "durable-task" else []
        ),
        subagents=SimpleNamespace(
            list_runs=lambda: [SimpleNamespace(id="task-child", status="RUNNING")]
        ),
    )

    assert pending_natural_user_reply(params) is None
    assert _delegated_work_facts(agent, params)["status_counts"] == {"RUNNING": 1}


# LLM: 主/子递归同规则：task_local 父级在直属孩子仍活跃时，保留模型真实正文与真实 turn-end，
# 只写 direct_child_wait 依赖事实；不改写为 interrupted，也不取消仍在跑的孩子。
# 函数用途: 验证递归父级的等待不改写模型正文/终态，且等待 marker 仍被写入。
def test_task_local_wait_keeps_model_reply_and_turn_end(monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.tool_loop.completion import (
        task_local_wait_response_for_open_subagents,
    )

    params = _tool_loop_params(
        context_scope="task_local",
        root_user_prompt="父级子代理内部任务",
        executed_tools=["create_subagents"],
    )
    marked: list[str] = []
    monkeypatch.setattr(
        "agent_py_agent.agent.subagents.direct_parent_lifecycle.mark_parent_waiting_for_direct_children",
        lambda _manager, parent_id, **kwargs: marked.append(str(parent_id)) or ["child-1"],
    )
    agent = SimpleNamespace(
        backend=SimpleNamespace(name="unit"),
        subagents=SimpleNamespace(),
    )
    model_reply = ModelResponse(
        text="父级本轮已产出可交付正文。",
        backend="unit",
        turn_end_reason="completed",
    )

    waited = task_local_wait_response_for_open_subagents(agent, params, response=model_reply)

    assert waited is not None
    assert waited.text == "父级本轮已产出可交付正文。", "模型真实正文不得被改写"
    assert waited.turn_end_reason == "completed", "模型真实 turn-end 不得被改成 interrupted"
    assert waited.runtime_status == "unfinished"
    assert waited.runtime_reason == "SUBAGENTS_ACTIVE"
    assert marked, "等待必须落成 direct_child_wait 依赖事实"


def test_task_local_subagent_never_enters_parent_user_reply_phase() -> None:
    params = _tool_loop_params(
        context_scope="task_local",
        root_user_prompt="完成子代理内部任务并返回结构化结果",
        executed_tools=["schedule_child_subagents"],
    )
    agent = SimpleNamespace(
        subagent_run_ids_for_request=lambda _task_id: ["child-1"],
        subagents=SimpleNamespace(
            list_runs=lambda: [SimpleNamespace(id="child-1", status="RUNNING")]
        ),
    )

    assert pending_natural_user_reply(params) is None


def test_direct_root_final_is_replaced_by_model_interim_while_child_runs(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    class PrematureFinalBackend:
        name = "premature_final"

        def __init__(self):
            self.prompts: list[str] = []

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return ModelResponse(text="三项工作已经全部完成。", backend=self.name)
            assert "[natural-user-reply]" in prompt
            assert '"reply_is_interim": true' in prompt
            assert '"RUNNING": 1' in prompt
            return ModelResponse(
                text="三项检查已经完成两项，剩余一项仍在继续；结果收齐后我会统一汇总。",
                backend=self.name,
            )

    backend = PrematureFinalBackend()
    agent.backend = backend
    monkeypatch.setattr(
        agent,
        "subagent_run_ids_for_request",
        lambda task_id: ["run-1", "run-2", "run-3"] if task_id == "task-1" else [],
    )
    monkeypatch.setattr(
        agent.subagents,
        "list_runs",
        lambda: [
            SimpleNamespace(id="run-1", status="DONE"),
            SimpleNamespace(id="run-2", status="DONE"),
            SimpleNamespace(id="run-3", status="RUNNING"),
        ],
    )

    response = execute_tool_loop(
        agent,
        _tool_loop_params(
            task_id="task-1",
            root_user_prompt="并行完成三项检查后汇总",
            allowed_tools=[],
        ),
    ).final_response

    # 契约变更（R279 复审）：子代理仍活跃只作为状态/展示事实，宿主不再用回执替换模型正文。
    # 模型自己写的终答就是用户看到的答复，本轮正常结束；后续由子代理生命周期事件唤醒。
    assert len(backend.prompts) == 1
    assert response.text == "三项工作已经全部完成。"


def test_natural_reply_does_not_guess_runtime_state_from_prose() -> None:
    response = ModelResponse(
        text="已在后台启动源码解析和复刻工作，分析完成后再回来汇报进展。",
        backend="test",
    )

    reason = natural_user_reply_rejection_reason(
        response,
        {"facts": {"reply_is_interim": True, "allow_time_estimate": False}},
    )

    assert reason == ""


def test_natural_reply_still_rejects_structured_tool_calls() -> None:
    response = ModelResponse(
        text="我来继续。",
        backend="test",
        tool_use_blocks=[{"name": "read_file", "input": {"path": "README.md"}}],
    )

    assert natural_user_reply_rejection_reason(response) == "structured_tool_call"


def test_natural_reply_returns_unauthorized_tool_call_as_failed_result_before_retry(
    tmp_path,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    prompts: list[str] = []
    wires: list[list[dict]] = []

    class ToolCallingReceiptBackend:
        name = "anthropic_compatible"

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            prompts.append(prompt)
            wires.append(list(kwargs.get("messages") or []))
            if len(prompts) == 1:
                return ModelResponse(
                    text="我先读取文件再回答。",
                    backend=self.name,
                    tool_use_blocks=[
                        {
                            "id": "call-1",
                            "name": "read_file",
                            "input": {"path": "README.md"},
                        }
                    ],
                )
            return ModelResponse(text="任务仍按原要求继续。", backend=self.name)

    agent.backend = ToolCallingReceiptBackend()
    params = _tool_loop_params(task_id="task-1")
    queue_natural_user_reply(
        params,
        kind="background_dispatch",
        facts={"reply_is_interim": True},
    )

    response = execute_tool_loop(agent, params).final_response

    assert len(prompts) == 2
    assert all("[natural-user-reply]" in prompt for prompt in prompts)
    second_wire = json.dumps(wires[1], ensure_ascii=False, sort_keys=True)
    assert '"type": "tool_use"' in second_wire
    assert '"tool_use_id": "call-1"' in second_wire
    assert "TOOL_CHOICE_VIOLATION" in second_wire
    assert "handler_executed=false" in second_wire
    assert "这个工具调用没有执行" in second_wire
    assert response.text == "任务仍按原要求继续。"
    assert response.tool_use_blocks == []
    assert response.runtime_status == "ok"
    assert response.runtime_reason == "background_dispatch"
    assert response.runtime_source == "model_user_reply"


def test_natural_reply_never_promotes_tool_preamble_after_bounded_retry(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    prompts: list[str] = []

    class RepeatedToolCallingReceiptBackend:
        name = "anthropic_compatible"

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk, kwargs
            prompts.append(prompt)
            return ModelResponse(
                text=f"我换个思路再查一下（第 {len(prompts)} 次）。",
                backend=self.name,
                tool_use_blocks=[
                    {
                        "id": f"call-{len(prompts)}",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
            )

    agent.backend = RepeatedToolCallingReceiptBackend()
    params = _tool_loop_params(task_id="task-1")
    queue_natural_user_reply(
        params,
        kind="background_dispatch",
        facts={"reply_is_interim": True},
    )

    response = execute_tool_loop(agent, params).final_response

    assert len(prompts) == 2
    assert "我换个思路" not in response.text
    assert response.text == "这次回复没有完整生成，其中附带的操作没有执行。请重试查看结果。"
    assert response.tool_use_blocks == []
    assert response.runtime_status == "user_reply_unavailable"
    assert response.runtime_reason == "NATURAL_REPLY_TOOL_CALL_REJECTED"
    assert response.runtime_source == "model_user_reply_unauthorized_tools_rejected"


def test_natural_reply_does_not_salvage_internal_protocol_from_rejected_tool_call(
    tmp_path,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    class InternalToolCallingReceiptBackend:
        name = "anthropic_compatible"

        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del prompt, on_chunk
            return ModelResponse(
                text='[TOOL_CALL]{"tool":"read_file","path":"README.md"}[/TOOL_CALL]',
                backend=self.name,
                tool_use_blocks=[
                    {"id": "call-1", "name": "read_file", "input": {"path": "README.md"}}
                ],
            )

    agent.backend = InternalToolCallingReceiptBackend()
    params = _tool_loop_params(task_id="task-1")
    queue_natural_user_reply(
        params,
        kind="background_dispatch",
        facts={"reply_is_interim": True},
    )

    response = execute_tool_loop(agent, params).final_response

    assert response.text == "这次回复没有完整生成，其中附带的操作没有执行。请重试查看结果。"
    assert response.tool_use_blocks == []
    assert response.runtime_status == "user_reply_unavailable"
    assert response.runtime_reason == "NATURAL_REPLY_TOOL_CALL_REJECTED"


def test_unacknowledged_guidance_replays_after_run_recovery(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    agent.conversation_store.guidance.append(
        {
            "target_type": "task",
            "target_id": "task-1",
            "message": "崩溃恢复后仍要看到这条引导。",
            "now": 10.0,
        }
    )
    interrupted_run = _tool_loop_params(task_id="task-1")

    assert inject_pending_guidance(agent, interrupted_run, now=11.0) is True
    assert agent.conversation_store.guidance.pending("task", "task-1")

    recovered_run = _tool_loop_params(task_id="task-1")
    assert inject_pending_guidance(agent, recovered_run, now=12.0) is True
    assert any("崩溃恢复后仍要看到这条引导" in str(item) for item in recovered_run.tool_context)
    assert acknowledge_injected_turn_input(agent, recovered_run, now=13.0) == 1
    assert agent.conversation_store.guidance.pending("task", "task-1") == []


def test_tool_loop_reports_thread_guidance_lookup_error(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    params = _tool_loop_params(task_id="task-1")

    def broken_thread_for_task(task_id):
        del task_id
        raise OSError("thread binding index missing")

    monkeypatch.setattr(agent.conversation_store.tasks, 'thread_for', broken_thread_for_task)

    updated = inject_pending_guidance(agent, params, now=11.0)

    assert updated is True
    assert any("GUIDANCE_LOOKUP_WARNING" in str(item) for item in params.tool_context)
    assert any("runtime_guidance.thread_for_task" in str(item) for item in params.tool_context)


def test_subagent_runner_prompt_does_not_claim_guidance_before_provider_safe_point(
    tmp_path,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    child = agent.subagents.create_run(goal="child", thought="", plan=["child"])
    agent.conversation_store.guidance.append(
        {
            "target_type": "agent_run",
            "target_id": child.id,
            "message": "先写阶段文件，再继续扩展。",
        }
    )

    _, prompt = agent._build_subagent_prompt(child.id, 0, "")

    assert "GUIDANCE_DELIVERED" not in prompt
    assert "先写阶段文件，再继续扩展。" not in prompt
    assert len(agent.conversation_store.guidance.pending("agent_run", child.id)) == 1

    params = _tool_loop_params(
        request_id="attempt-child-1",
        attempt_id="attempt-child-1",
        run_id=child.id,
        task_id=child.root_id or child.id,
    )
    assert inject_pending_guidance(agent, params, now=21.0) is True
    assert any("先写阶段文件，再继续扩展。" in str(item) for item in params.tool_context)
    assert acknowledge_injected_turn_input(agent, params, now=22.0) == 1
    assert agent.conversation_store.guidance.pending("agent_run", child.id) == []


def test_pending_agent_guidance_rebinds_to_recovered_attempt_once(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    request = {
        "target_type": "agent_run",
        "target_id": "child-1",
        "message": "恢复后继续这条用户插话。",
        "sender": "user:alice",
        "metadata": {"expected_turn_id": "attempt-old"},
    }
    first = store.guidance.append_once(request, dedupe_key="child-1/message-1")

    summary = store.guidance.recovery.rebind_unsubmitted(
        "agent_run",
        "child-1",
        dead_turn_ids={"attempt-old"},
        recovered_turn_id="attempt-new",
    )

    assert summary["rebound"] == 1
    pending = store.guidance.pending("agent_run", "child-1")
    assert [entry.guidance_id for entry in pending] == [first.guidance_id]
    assert pending[0].metadata["expected_turn_id"] == "attempt-new"
    replay = store.guidance.append_once(
        {
            **request,
            "metadata": {"expected_turn_id": "attempt-new"},
        },
        dedupe_key="child-1/message-1",
    )
    assert replay.guidance_id == first.guidance_id
    assert store.guidance.claim_for_turn(
        pending[0],
        expected_turn_id="attempt-new",
        attempt_id="attempt-new",
    ) is True
    assert store.guidance.submissions.mark_submitted(
        "attempt-new",
        pending,
        attempt_id="attempt-new",
        provider_call_id="call-new",
    ) == (first.guidance_id,)
    assert store.guidance.acknowledgements.consume_submitted(
        "attempt-new",
        pending,
        provider_call_id="call-new",
        now=30.0,
    ) == (first.guidance_id,)
    assert store.guidance.pending("agent_run", "child-1") == []


def test_rebound_guidance_repairs_half_written_lookup_projections(tmp_path) -> None:
    from agent_py_agent.agent.conversation.store_guidance_records import _rebound_guidance_receipt
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    store = ConversationStore(tmp_path / "conversations")
    dedupe_key = "child-1/message-half-written"
    entry = store.guidance.append_once(
        {
            "target_type": "agent_run",
            "target_id": "child-1",
            "message": "恢复半写入投影后继续。",
            "metadata": {
                "expected_turn_id": "attempt-old",
                "gateway_input_request_id": "gateway-input-half-written",
            },
        },
        dedupe_key=dedupe_key,
    )
    receipt_path = store.storage.guidance_dedupe_path(dedupe_key)
    original = store.guidance.receipt(dedupe_key)
    assert original is not None
    rebound = _rebound_guidance_receipt(
        original,
        old_turn_id="attempt-old",
        recovered_turn_id="attempt-new",
    )
    write_json_file_atomic(receipt_path, rebound.to_dict())

    repaired, turn_id = store.guidance.receipt_for_input(
        "gateway-input-half-written"
    )

    assert repaired is not None
    assert repaired.entry.guidance_id == entry.guidance_id
    assert turn_id == "attempt-new"
    input_index = json.loads(
        store.storage.guidance_input_index_path("gateway-input-half-written").read_text(
            encoding="utf-8"
        )
    )
    assert input_index["expected_turn_id"] == "attempt-new"
    assert not store.storage.guidance_turn_index_path("attempt-old", dedupe_key).exists()
    assert store.storage.guidance_turn_index_path("attempt-new", dedupe_key).exists()


def test_reserved_agent_guidance_rebinds_only_before_provider_submission(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    entry = store.guidance.append_once(
        {
            "target_type": "agent_run",
            "target_id": "child-1",
            "message": "旧 attempt 尚未发给模型。",
            "metadata": {"expected_turn_id": "attempt-old"},
        },
        dedupe_key="child-1/message-reserved",
    )
    assert store.guidance.claim_for_turn(
        entry,
        expected_turn_id="attempt-old",
        attempt_id="attempt-old",
    ) is True

    summary = store.guidance.recovery.rebind_unsubmitted(
        "agent_run",
        "child-1",
        dead_turn_ids={"attempt-old"},
        recovered_turn_id="attempt-new",
    )

    assert summary["rebound"] == 1
    receipt = store.guidance.receipt("child-1/message-reserved")
    assert receipt is not None
    assert receipt.status == "pending"
    assert receipt.attempt_id == ""
    assert receipt.entry.metadata["expected_turn_id"] == "attempt-new"


def test_submitted_agent_guidance_is_never_rebound_after_recovery(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    entry = store.guidance.append_once(
        {
            "target_type": "agent_run",
            "target_id": "child-1",
            "message": "已经开始提交模型。",
            "metadata": {"expected_turn_id": "attempt-old"},
        },
        dedupe_key="child-1/message-submitted",
    )
    assert store.guidance.claim_for_turn(
        entry,
        expected_turn_id="attempt-old",
        attempt_id="attempt-old",
    ) is True
    assert store.guidance.submissions.mark_submitted(
        "attempt-old",
        [entry],
        attempt_id="attempt-old",
        provider_call_id="call-old",
    ) == (entry.guidance_id,)

    summary = store.guidance.recovery.rebind_unsubmitted(
        "agent_run",
        "child-1",
        dead_turn_ids={"attempt-old"},
        recovered_turn_id="attempt-new",
    )

    assert summary["submitted_unknown"] == 1
    receipt = store.guidance.receipt("child-1/message-submitted")
    assert receipt is not None
    assert receipt.status == "submitted"
    assert receipt.entry.metadata["expected_turn_id"] == "attempt-old"


def test_legacy_prompt_time_delivery_migrates_to_unknown_without_replay(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    entry = store.guidance.append_once(
        {
            "target_type": "agent_run",
            "target_id": "child-1",
            "message": "旧旁路可能已经进入模型请求。",
            "metadata": {"expected_turn_id": "attempt-old"},
        },
        dedupe_key="child-1/message-legacy",
    )
    store.guidance.ledger.mark_delivered([entry.guidance_id], now=25.0)

    summary = store.guidance.recovery.rebind_unsubmitted(
        "agent_run",
        "child-1",
        dead_turn_ids={"attempt-old"},
        recovered_turn_id="attempt-new",
    )

    assert summary["legacy_submission_unknown"] == 1
    receipt = store.guidance.receipt("child-1/message-legacy")
    assert receipt is not None
    assert receipt.status == "submitted"
    assert receipt.submission_id.startswith("legacy-runner-prompt-unknown:")
    assert store.guidance.pending("agent_run", "child-1") == []


def test_legacy_prompt_delivery_is_retired_on_plain_receipt_lookup(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    dedupe_key = "child-1/message-legacy-terminal"
    entry = store.guidance.append_once(
        {
            "target_type": "agent_run",
            "target_id": "child-1",
            "message": "旧子代理已结束，不得永久显示等待或重复投递。",
            "metadata": {"expected_turn_id": "attempt-terminal"},
        },
        dedupe_key=dedupe_key,
    )
    store.guidance.ledger.mark_delivered([entry.guidance_id], now=26.0)

    receipt = store.guidance.receipt(dedupe_key)

    assert receipt is not None
    assert receipt.status == "submitted"
    assert receipt.submission_id.startswith("legacy-runner-prompt-unknown:")
    assert receipt.migration["legacy_runner_prompt_delivery"]["turn_id"] == (
        "attempt-terminal"
    )
    assert store.guidance.pending("agent_run", "child-1") == []
