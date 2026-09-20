from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace as _StoreDomain

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ModelNotConfiguredError, ProviderUsageLimitError
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation import background_delivery as delivery_module
from agent_py_agent.agent.conversation import background_execution as execution_module
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR,
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.model_profiles import ModelProfileError


# LLM: 只为公开回复断言分离空正文原生事实行；事实行必须有合法信封，不能借此隐藏漏写或空白 final。
# 函数用途: 测试真实公开聊天内容，同时验证后台原生历史仍保存在同一个 transcript。
def _public_background_messages(store, thread_id, *, limit=0):
    rows = store.messages.recent(thread_id, limit=limit)
    for row in rows:
        if not row.content:
            assert row.metadata.get("assistant_part_id") == "native"
            assert row.metadata.get("canonical_native_messages", {}).get("schema") == "conversation_native_messages.v1"
    return [row for row in rows if row.content]



# LLM: 测试直接调用独立交付入口；任务状态仍读原 runtime 查询，不能用常量替代取消与终态守卫。
# 函数用途: 为已有后台场景绑定同一个 Agent、store、渠道和状态读取能力。
def _delivery_dependencies(runtime):
    from functools import partial

    from agent_py_agent.agent.conversation.runtime import _background_task_link_status

    return delivery_module.BackgroundDeliveryDependencies(
        agent=runtime.agent, store=runtime.store, channels=runtime.channels,
        task_status=partial(_background_task_link_status, runtime.agent, store=runtime.store),
    )

def test_background_run_params_carry_structured_conversation_task_identity() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    request = BackgroundRunRequest(
        thread_id="thread-1",
        task_id="task-1",
        reason="scheduled_progress_report",
    )

    params = _run_params(request.thread_id, request)

    assert params.source == "background_main_agent"
    assert params.save is False
    assert params.run_id == "task-1"
    assert params.request_id == "task-1"
    assert params.task_id == "task-1"
    assert params.task_attributes == {
        "conversation_thread_id": "thread-1",
        "conversation_task_id": "task-1",
        CONVERSATION_REQUEST_ID_ATTR: "task-1",
        CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
    }


def test_background_context_overflow_compacts_and_retries_same_slice(monkeypatch) -> None:
    """后台主代理必须像前台/子代理一样在同一工作片 Compact 后续跑并携带已完成工具。"""
    from dataclasses import replace

    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation import compact as compact_module
    from agent_py_agent.agent.conversation import runtime as runtime_module
    from agent_py_agent.agent.conversation.compact import ConversationCompactResult
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest

    original = ConversationThread(thread_id="thread-bg", canonical_user_id="owner-bg")
    compacted = replace(original, compact_generation=1, summary="已压缩旧历史")
    observed_params: list[RunParams] = []
    rejection = {"tool_name": "run_command", "args_hash": "sha256:denied", "decision": "denied"}

    class Agent:
        # 历史种子需要读会话范围配置；生产 AgentConfig 一直有这些字段，桩必须同样提供。
        config = SimpleNamespace(
            conversation_context_recent_limit=0,
            background_context_max_total_tokens=8000,
        )

        def run(self, _prompt, *, params):
            observed_params.append(params)
            if len(observed_params) == 1:
                params.runtime_rejected_actions.append(rejection)
                return SimpleNamespace(
                    runtime_status="context_overflow",
                    archive_tool_calls=[{"tool": "read_file", "ok": True}],
                    active_turn_user_inputs=[],
                )
            return SimpleNamespace(runtime_status="ok", response="继续完成")

    class Store:
        def __init__(self, *args, **kwargs):
            self.threads = _StoreDomain(load_report=self._fake_load_thread_report)
            self.messages = _StoreDomain(after_compact_report=self._fake_messages_after_compact_report)

        def _fake_load_thread_report(self, thread_id):
            assert thread_id == original.thread_id
            return original, None

        def context_bundle_report(self, thread_id, *, recent_limit=0):
            del recent_limit
            return {"thread": {"thread_id": thread_id}}, []

        def _fake_messages_after_compact_report(self, _thread):
            return [], []


    class Sink:
        def begin_model_attempt(self, _attempt):
            pass

        def __init__(self):
            self.compact_rows = []

        def write_conversation_compact_progress(self, value):
            self.compact_rows.append(dict(value))
            return True

    def fake_run_params(*_args, **_kwargs):
        return RunParams(
            carried_archive_tool_calls=[{"tool": "prior", "ok": True}],
            task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        )

    compact_calls = []

    def fake_prepare(_agent, _store, _thread, *, options):
        compact_calls.append(options)
        return ConversationCompactResult(
            thread=compacted,
            messages=(),
            projected_tokens=1_000,
            trigger_tokens=9_000,
            compacted=True,
        )

    monkeypatch.setattr(runtime_module, "_run_params", fake_run_params)
    monkeypatch.setattr(
        execution_module,
        "context_markdown",
        lambda **values: f"ctx-generation-{values['thread'].compact_generation}",
    )
    monkeypatch.setattr(compact_module, "prepare_conversation_context", fake_prepare)
    monkeypatch.setattr(
        runtime_mixin,
        "release_active_turn_inputs_for_compact",
        lambda *_args, **_kwargs: (),
    )
    sink = Sink()
    result = execution_module.run_background_turn_with_compact(
        execution_module.BackgroundExecutionDependencies(agent=Agent(), store=Store(), prepare_run=runtime_module._run_params),
        original,
        BackgroundRunRequest(thread_id=original.thread_id, task_id='task-bg', reason='subagent_runner_finished'),
        user_prompt='继续原任务',
        continuation_injection=['child completed'],
        proactive_delivery_available=False,
        activity_sink=sink,
    )

    assert result.runtime_status == "ok"
    assert len(observed_params) == 2
    assert compact_calls and compact_calls[0].force is True
    assert observed_params[0].inject == ["ctx-generation-0", "child completed"]
    assert observed_params[1].inject == ["ctx-generation-1", "child completed"]
    assert observed_params[1].carried_archive_tool_calls == [{"tool": "read_file", "ok": True}]
    assert observed_params[1].runtime_rejected_actions == [rejection]
    assert observed_params[1].runtime_rejected_actions is observed_params[0].runtime_rejected_actions
    assert RunParams().runtime_rejected_actions == []
    assert observed_params[0].on_chunk is sink
    assert observed_params[1].on_chunk is sink


def test_background_compact_slice_yields_after_eight_progressful_generations(monkeypatch) -> None:
    """连续压缩达到公平性上限时应让出调度片，而不是伪造程序崩溃。"""
    from dataclasses import replace

    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation import runtime as runtime_module
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    BackgroundCompactSliceYield = execution_module.BackgroundCompactSliceYield

    original = ConversationThread(thread_id="thread-yield", canonical_user_id="owner-yield")
    generations: list[int] = []

    class Agent:
        # 历史种子需要读会话范围配置；生产 AgentConfig 一直有这些字段，桩必须同样提供。
        config = SimpleNamespace(
            conversation_context_recent_limit=0,
            background_context_max_total_tokens=8000,
        )

        def run(self, _prompt, *, params):
            del params
            return SimpleNamespace(
                runtime_status="context_overflow",
                archive_tool_calls=[{"call_id": "call-live", "tool": "read_file", "ok": True}],
                active_turn_user_inputs=[],
            )

    class Store:
        def context_bundle_report(self, thread_id, *, recent_limit=0):
            del recent_limit
            return {"thread": {"thread_id": thread_id}}, []

        def __init__(self, *args, **kwargs):
            self.messages = _StoreDomain(after_compact_report=self._fake_messages_after_compact_report)

        def _fake_messages_after_compact_report(self, _thread):
            return [], []


    class Sink:
        def begin_model_attempt(self, _attempt):
            pass

        def write_conversation_compact_progress(self, _value):
            return True

    def fake_run_params(*_args, **_kwargs):
        return RunParams(task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True})

    def advancing_compact(_runtime, current, **_kwargs):
        generation = current.compact_generation + 1
        generations.append(generation)
        return replace(current, compact_generation=generation)

    monkeypatch.setattr(runtime_module, "_run_params", fake_run_params)
    monkeypatch.setattr(execution_module, "context_markdown", lambda **_values: "context")
    monkeypatch.setattr(execution_module, "_compact_background_main_thread", advancing_compact)
    monkeypatch.setattr(
        runtime_mixin,
        "release_active_turn_inputs_for_compact",
        lambda *_args, **_kwargs: (),
    )

    with pytest.raises(BackgroundCompactSliceYield):
        execution_module.run_background_turn_with_compact(
            execution_module.BackgroundExecutionDependencies(agent=Agent(), store=Store(), prepare_run=runtime_module._run_params),
            original,
            BackgroundRunRequest(thread_id=original.thread_id, task_id='task-yield', reason='subagent_runner_finished'),
            user_prompt='继续原任务',
            continuation_injection=['child completed'],
            proactive_delivery_available=False,
            activity_sink=Sink(),
        )

    assert generations == list(range(1, 9))


def test_background_scheduler_treats_compact_slice_yield_as_clean_continuation(
    monkeypatch,
) -> None:
    """让出只结束当前 claim；原来源保持待处理且不写失败账。"""
    BackgroundCompactSliceYield = execution_module.BackgroundCompactSliceYield

    finished: list[dict[str, object]] = []

    class Runtime:
        agent = SimpleNamespace()

        def run_once(self, _kwargs):
            raise BackgroundCompactSliceYield("continue")

    class Claims:
        def finish(self, request):
            finished.append(dict(request))

    class Store:
        claims = Claims()

        def context_bundle_report(self, thread_id, *, recent_limit=0):
            del recent_limit
            return {"thread": {"thread_id": thread_id}}, []

        def __init__(self, *args, **kwargs):
            self.messages = _StoreDomain(after_compact_report=self._fake_messages_after_compact_report)

        def _fake_messages_after_compact_report(self, _thread):
            return [], []


    class Heartbeat:
        def stop(self):
            return None

    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": Runtime(),
            "store": Store(),
            "claim_ttl_seconds": 30,
        }
    )
    monkeypatch.setattr(scheduler, "_start_heartbeat", lambda *_args, **_kwargs: Heartbeat())
    monkeypatch.setattr(scheduler, "_runtime_facts", lambda: {"current_tool": ""})

    result = scheduler._run_with_heartbeat(
        "claim-yield",
        {
            "thread_id": "thread-yield",
            "task_id": "task-yield",
            "reason": "subagent_runner_finished",
            "now": 10.0,
        },
        claim_scope_id="thread-yield",
    )

    assert result is None
    assert finished[0]["status"] == "finished"
    assert finished[0]["error"] is None


def test_background_overflow_compacts_carried_active_turn_when_transcript_is_empty(
    monkeypatch,
) -> None:
    """真实溢出不能因 transcript 暂无可压消息而账外重启，必须推进本轮工具 Compact。"""

    from dataclasses import replace

    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation import active_turn_compact as active_module
    from agent_py_agent.agent.conversation import compact as compact_module
    from agent_py_agent.agent.conversation import runtime as runtime_module
    from agent_py_agent.agent.conversation.active_turn_compact import (
        ActiveTurnArchiveCompactResult,
    )
    from agent_py_agent.agent.conversation.compact import ConversationCompactResult
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest

    original = ConversationThread(thread_id="thread-active", canonical_user_id="owner-bg")
    compacted = replace(
        original,
        compact_generation=1,
        summary="本轮工具历史已正式压缩",
        compact_checkpoint_id="checkpoint-active-1",
    )
    observed_params: list[RunParams] = []

    class Agent:
        # 历史种子需要读会话范围配置；生产 AgentConfig 一直有这些字段，桩必须同样提供。
        config = SimpleNamespace(
            conversation_context_recent_limit=0,
            background_context_max_total_tokens=8000,
        )

        def run(self, _prompt, *, params):
            observed_params.append(params)
            if len(observed_params) == 1:
                return SimpleNamespace(
                    runtime_status="context_overflow",
                    archive_tool_calls=[{"call_id": "call-old", "tool": "read_file", "ok": True}],
                    active_turn_user_inputs=[],
                )
            return SimpleNamespace(runtime_status="ok", response="继续完成")

    class Store:
        def __init__(self, *args, **kwargs):
            self.threads = _StoreDomain(load_report=self._fake_load_thread_report)
            self.messages = _StoreDomain(after_compact_report=self._fake_messages_after_compact_report)

        def _fake_load_thread_report(self, thread_id):
            assert thread_id == original.thread_id
            return original, None

        def context_bundle_report(self, thread_id, *, recent_limit=0):
            del recent_limit
            return {"thread": {"thread_id": thread_id}}, []

        def _fake_messages_after_compact_report(self, _thread):
            return [], []

    class Sink:
        def begin_model_attempt(self, _attempt):
            pass

        def write_conversation_compact_progress(self, _value):
            return True

    def fake_run_params(*_args, **_kwargs):
        return RunParams(
            task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        )

    def no_transcript_compact(_agent, _store, _thread, *, options):
        assert options.force is True
        return ConversationCompactResult(
            thread=original,
            messages=(),
            projected_tokens=100_000,
            trigger_tokens=90_000,
            compacted=False,
        )

    active_calls: list[object] = []

    def active_compact(*args, **_kwargs):
        active_calls.append(args[-1])
        return ActiveTurnArchiveCompactResult(
            thread=compacted,
            compacted=True,
            source_call_ids=("call-old",),
        )

    monkeypatch.setattr(runtime_module, "_run_params", fake_run_params)
    monkeypatch.setattr(
        execution_module,
        "context_markdown",
        lambda **values: f"ctx-generation-{values['thread'].compact_generation}",
    )
    monkeypatch.setattr(compact_module, "prepare_conversation_context", no_transcript_compact)
    monkeypatch.setattr(active_module, "compact_carried_active_turn_archive", active_compact)
    monkeypatch.setattr(
        runtime_mixin,
        "release_active_turn_inputs_for_compact",
        lambda *_args, **_kwargs: (),
    )

    result = execution_module.run_background_turn_with_compact(
        execution_module.BackgroundExecutionDependencies(agent=Agent(), store=Store(), prepare_run=runtime_module._run_params),
        original,
        BackgroundRunRequest(thread_id=original.thread_id, task_id='task-bg', reason='subagent_runner_finished'),
        user_prompt='继续原任务',
        continuation_injection=['child completed'],
        proactive_delivery_available=False,
        activity_sink=Sink(),
    )

    assert result.runtime_status == "ok"
    assert len(observed_params) == 2
    assert len(active_calls) == 1
    assert getattr(active_calls[0], "task_prompt", "") == "继续原任务"
    assert observed_params[1].inject == ["ctx-generation-1", "child completed"]
    assert observed_params[1].carried_archive_tool_calls == [
        {"call_id": "call-old", "tool": "read_file", "ok": True}
    ]


def test_ordinary_resume_wake_preserves_foreground_request_generation() -> None:
    """普通任务 policy 的持久 task id 与前台 Todo 展示代次必须同时保留。"""

    from agent_py_agent.agent.conversation.models import ProgressPolicy
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_task_attributes,
        _progress_policy_wake_payload,
    )

    policy = ProgressPolicy(
        policy_id="policy-1",
        thread_id="thread-1",
        task_id="task-1",
        interval_seconds=60,
        next_due_at=61.0,
        metadata={
            "kind": "ordinary_task_resume",
            CONVERSATION_REQUEST_ID_ATTR: "gwreq-2",
        },
    )
    wake = _progress_policy_wake_payload(policy)
    attrs = _background_task_attributes(
        policy.thread_id,
        BackgroundRunRequest(
            thread_id=policy.thread_id,
            task_id=policy.task_id,
            reason="scheduled_progress_report",
            wake_signal=wake,
        ),
        None,
    )

    assert wake["metadata"] == {CONVERSATION_REQUEST_ID_ATTR: "gwreq-2"}
    assert attrs is not None
    assert attrs["conversation_task_id"] == "task-1"
    assert attrs[CONVERSATION_REQUEST_ID_ATTR] == "gwreq-2"


@pytest.mark.parametrize("explicit_cwd", [False, True])
def test_background_continuation_separates_owner_cwd_from_run_archive(tmp_path, explicit_cwd) -> None:
    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        _primary_workspace_root,
    )
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    service_root = tmp_path / "gateway-service"
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        service_root,
    )
    owner_home = Path(agent.home_paths.owner_home_dir)
    project_root = owner_home / "projects" / "tui-project"
    project_root.mkdir(parents=True)
    expected_cwd = project_root if explicit_cwd else owner_home
    task_root = Path(agent.home_paths.owner_home_dir) / "tasks" / "task-1"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "local-agent",
            "channel": "gateway-cli",
            "channel_conversation_id": "cwd-background-session",
            "channel_user_id": "local-agent",
            "cwd": str(project_root.resolve()) if explicit_cwd else "",
            "runtime_workspace_roots": [str(project_root.resolve())] if explicit_cwd else [],
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "继续 TUI 项目",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    persisted_thread = store.threads.load(thread.thread_id)
    assert persisted_thread is not None

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="task-1",
            reason="subagent_runner_finished",
        ),
        agent,
        thread=persisted_thread,
    )

    assert params.task_attributes[CONVERSATION_EXECUTION_CWD_ATTR] == str(expected_cwd.resolve())
    assert params.task_attributes[CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR] == [
        str(expected_cwd.resolve())
    ]
    assert params.task_attributes["run_workspace"]["task_root"] == str(task_root.resolve())
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert boundary["execution_cwd"] == str(expected_cwd.resolve())
    agent._current_run_params = params
    try:
        assert _primary_workspace_root(agent) == expected_cwd.resolve()
    finally:
        delattr(agent, "_current_run_params")


def test_subagent_wake_restores_original_task_and_root_tool_history(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        GoalRuntimeContext,
        _goal_runtime_context,
        _run_params,
    )

    task_root = tmp_path / "home" / "tasks" / "task-continue"
    index = task_root / "work" / "blobs" / "tool_outputs" / "index.jsonl"
    index.parent.mkdir(parents=True)
    root_id = "task-continue"
    foreground_request_id = "foreground-request"
    rows = [
        {
            "kind": "tool_call",
            "tool": "task_progress",
            "call_id": "call-plan",
            "scoped_call_id": f"{root_id}:call-plan",
            "request_id": foreground_request_id,
            "run_id": foreground_request_id,
            "task_id": foreground_request_id,
            "ok": True,
            "status": "ok",
            "parameters": {"summary": "使用 Rust 复刻", "action": "create"},
        },
        {
            "kind": "tool_output",
            "tool": "create_subagents",
            "call_id": "call-child",
            "scoped_call_id": f"{root_id}:call-child",
            "request_id": foreground_request_id,
            "run_id": foreground_request_id,
            "task_id": foreground_request_id,
            "ok": True,
            "status": "ok",
            "parameters": {"goal": "实现 Rust 命令层"},
            "path": str(index.parent / "create-subagent.json"),
            "sha256": "abc123",
            "size_bytes": 42,
        },
        {
            "kind": "tool_call",
            "tool": "write_file",
            "call_id": "call-child-private",
            "scoped_call_id": "child-1:call-child-private",
            "request_id": "child-request",
            "run_id": "child-1",
            "task_id": "child-1",
            "ok": True,
            "status": "ok",
            "parameters": {"path": "child-only.txt"},
        },
    ]
    index.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-continue",
            "channel": "internal",
            "channel_conversation_id": "thread-continue",
            "channel_user_id": "user-continue",
        }
    )
    objective = "换一种编程语言完整复刻现有项目，只由子代理编写功能代码。"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": root_id,
            "goal": "上一个被截断的调研追问",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    store.messages.append({
        "thread_id": thread.thread_id, "role": "user", "content": objective,
        "metadata": {"conversation_request_id": foreground_request_id},
    })
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id=root_id,
        reason="subagent_runner_finished",
        wake_signal={
            "metadata": {
                "conversation_request_id": foreground_request_id,
            }
        },
    )
    context = _goal_runtime_context(agent, store, request)

    params = _run_params(
        thread.thread_id,
        request,
        agent,
        goal_context=context,
        thread=thread,
    )
    baseline = _run_params(
        thread.thread_id,
        request,
        agent,
        goal_context=GoalRuntimeContext(task_objective=objective),
        thread=thread,
    )

    assert context.task_objective == objective
    assert context.task_path == str(task_root)
    assert params.root_user_prompt == objective
    assert params.task_attributes[CONVERSATION_REQUEST_ID_ATTR] == foreground_request_id
    assert [row["call_id"] for row in params.carried_archive_tool_calls] == [
        "call-plan",
        "call-child",
    ]
    assert params.carried_archive_tool_calls[1]["artifact_ref"].endswith("create-subagent.json")
    assert params.task_attributes["max_tool_rounds"] == (
        baseline.task_attributes["max_tool_rounds"] + 2
    )


def test_subagent_wake_keeps_objective_in_user_task_slot() -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_model_inputs,
    )

    request = BackgroundRunRequest(
        thread_id="thread-1",
        task_id="task-1",
        reason="subagent_runner_finished",
    )

    prompt, injections = _background_model_inputs(
        request,
        task_objective="原始用户任务",
        wake_prompt="读取本次子代理完成事件后继续",
    )

    assert prompt == "原始用户任务"
    assert injections == ["[active-turn-continuation]\n读取本次子代理完成事件后继续"]


@pytest.mark.parametrize("tail_count", [0, 180])
def test_lifecycle_objective_pages_exact_requests_without_rewriting_history(tmp_path, tail_count):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_request_objective,
    )

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "tui", "channel_conversation_id": "same"})
    for request_id, content in (("old", "旧目标"), ("first", "第一轮原话"), ("second", "第二轮原话"), ("unrelated", "不要猜最近一轮")):
        store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": content,
                              "metadata": {"conversation_request_id": request_id}})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "这是插话不是原任务",
                          "metadata": {"gateway_request_id": "second", "kind": "active_turn_user_input"}})
    for index in range(tail_count):
        store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": f"工具片 {index}",
                              "metadata": {"conversation_request_id": f"different-{index}"}})
    path = store.storage.message_path(thread.thread_id)
    before = path.read_bytes()
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="old", reason="subagent_runner_finished",
        wake_signal={"metadata": {"conversation_request_id": "second", "events": [
            {"metadata": {"conversation_request_id": "first"}},
            {"metadata": {"conversation_request_id": "second"}},
        ]}})

    assert _background_request_objective(store, request) == "第一轮原话\n\n第二轮原话"
    assert path.read_bytes() == before
    assert store.threads.load(thread.thread_id).compact_generation == thread.compact_generation


@pytest.mark.parametrize("goal_status", ["active", "complete"])
@pytest.mark.parametrize("ordinary_input", ["none", "present", "missing"])
def test_goal_child_wake_does_not_require_a_synthetic_user_request(tmp_path, goal_status, ordinary_input):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _goal_runtime_context,
    )

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "tui", "channel_conversation_id": "same"})
    task_id = "durable-task-with-no-user-request"
    objective = "完成知识库整理并整合子代理成果"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": objective, "status": "active"})
    goal = store.goals.create({"thread_id": thread.thread_id, "task_id": task_id, "objective": objective})
    if goal_status == "complete":
        store.goals.update({"thread_id": thread.thread_id, "goal_id": goal.goal_id, "status": "complete"})
    metadata = {"conversation_request_id": task_id}
    if ordinary_input != "none":
        metadata["events"] = [{"metadata": {"conversation_request_id": "real-user-request"}}]
    if ordinary_input == "present":
        store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "补充报告格式",
                              "metadata": {"conversation_request_id": "real-user-request"}})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id=task_id, reason="subagent_runner_finished",
                                   wake_signal={"metadata": metadata})
    if ordinary_input == "missing":
        with pytest.raises(DataCorruptionError, match="background request input is missing"):
            _goal_runtime_context(SimpleNamespace(), store, request)
        return
    context = _goal_runtime_context(SimpleNamespace(), store, request)
    assert context.task_objective == ("补充报告格式" if goal_status == "complete" and ordinary_input == "present" else objective)
    assert (context.goal is not None) == (goal_status == "active")
    rows = store.messages.recent(thread.thread_id, limit=20)
    assert all(row.metadata.get("conversation_request_id") != task_id for row in rows)


@pytest.mark.parametrize("failure", ["missing", "foreign_thread", "corrupt"])
def test_lifecycle_explicit_input_failure_cannot_fall_back_to_old_task(tmp_path, failure):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _goal_runtime_context,
    )

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "tui", "channel_conversation_id": "same"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "old", "goal": "不得接回的旧目标", "status": "active"})
    if failure == "foreign_thread":
        other = store.threads.get_or_create({"canonical_user_id": "other", "channel": "tui", "channel_conversation_id": "other"})
        store.messages.append({"thread_id": other.thread_id, "role": "user", "content": "其他会话的同编号",
                              "metadata": {"conversation_request_id": "new"}})
    if failure == "corrupt":
        store.storage.message_path(thread.thread_id).write_text('{"bad":\n')
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="old", reason="subagent_runner_finished",
                                   wake_signal={"metadata": {"conversation_request_id": "new"}})
    with pytest.raises(DataCorruptionError, match="background request"):
        _goal_runtime_context(SimpleNamespace(), store, request)
    assert store.tasks.load("old").goal == "不得接回的旧目标"


@pytest.mark.parametrize("explicit_request", [False, True])
def test_subagent_wake_provider_prompt_keeps_original_task_after_runtime_injection(
    tmp_path, explicit_request,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-continuation",
            "channel": "internal",
            "channel_conversation_id": "thread-continuation",
            "channel_user_id": "user-continuation",
        }
    )
    task_root = tmp_path / "tasks" / "root-continuation"
    task_root.mkdir(parents=True)
    objective = "换一种编程语言完整复刻现有项目，只由子代理编写功能代码。"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "root-continuation",
            "goal": "旧任务未完成，不能冒充当前请求" if explicit_request else objective,
            "status": "active",
            "task_path": str(task_root),
        }
    )
    if explicit_request:
        store.messages.append({
            "thread_id": thread.thread_id, "role": "user", "content": objective,
            "metadata": {"conversation_request_id": "current-request"},
        })
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )

    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "root-continuation",
            "reason": "subagent_runner_finished",
            "wake_signal": {"metadata": {"conversation_request_id": "current-request"}} if explicit_request else {},
            "now": 20.0,
        }
    )

    assert len(backend.prompts) == 1
    prompt = backend.provider_texts[0]
    continuation_at = prompt.index("[active-turn-continuation]")
    user_task_at = prompt.index(f"# User Task\n{objective}")
    assert user_task_at < continuation_at
    assert "A subagent lifecycle event resumed this same task" in prompt
    assert "# User Task\n旧任务未完成，不能冒充当前请求" not in prompt


def test_claimed_background_turn_can_use_its_own_task_workspace(tmp_path) -> None:
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.tool_call_runtime import (
        ToolCallRuntimeRequest,
        _promote_conversation_task_for_work_tool,
    )
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        ToolCallExecuteParams,
    )
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
    from agent_py_agent.tests._tool_runtime_harness import canonical_test_call

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "后台续跑",
        }
    )
    task_root = tmp_path / "home" / "tasks" / "task-1"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "继续既有任务",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    claim = agent.conversation_store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "subagent_runner_finished",
            "lease_seconds": 90,
        }
    )
    assert claim is not None
    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="task-1",
            reason="subagent_runner_finished",
        ),
        agent,
    )
    snapshot = agent.tools.runtime_snapshot(run_id=params.run_id)
    loop_params = ToolLoopExecuteParams(
        user_prompt="继续既有任务",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=params.allowed_tools,
        write_boundary=params.write_boundary,
        task_attributes=params.task_attributes,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_runtime_snapshot=snapshot,
    )
    agent._current_run_params = params
    call = canonical_test_call(snapshot, "write_file", {})
    execute_request = ToolCallExecuteParams(loop_params, 1, 1, call)
    try:
        result = _promote_conversation_task_for_work_tool(
            ToolCallRuntimeRequest(agent, execute_request, call)
        )
    finally:
        delattr(agent, "_current_run_params")
        agent.conversation_store.claims.finish(
            {
                "thread_id": thread.thread_id,
                "claim_id": claim["claim_id"],
                "task_id": "task-1",
                "status": "finished",
            }
        )

    assert result is None
    assert params.task_attributes[CONVERSATION_TASK_TURN_ACTIVE_ATTR] is True


def test_background_run_without_task_does_not_invent_conversation_task_identity() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    request = BackgroundRunRequest(thread_id="thread-chat", task_id="", reason="observation_batch")

    params = _run_params(request.thread_id, request)

    assert params.run_id == "bg-main-thread-chat"
    assert params.task_id == ""
    assert params.task_attributes is None


def test_internal_background_run_uses_bounded_existing_tool_loop_controls() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    agent = SimpleNamespace(
        config=SimpleNamespace(background_main_agent_allowed_tools=[]),
        runtime_guard_policy=RuntimeGuardPolicy(
            values={
                "background_max_tool_rounds": 7,
                "background_max_tool_calls_per_round": 3,
            }
        ),
        owner_policy=None,
        home_paths=None,
        conversation_store=None,
    )
    internal = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="task-1",
            reason="scheduled_progress_report",
        ),
        agent,
    )
    assert internal.task_attributes["max_tool_rounds"] == 7
    assert internal.task_attributes["max_tool_calls_per_round"] == 3

    incoming = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="task-1",
            reason="incoming_channel_message",
        ),
        agent,
    )
    assert "max_tool_rounds" not in incoming.task_attributes
    assert "max_tool_calls_per_round" not in incoming.task_attributes


def test_internal_background_run_fallback_allows_a_complete_work_slice() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    agent = SimpleNamespace(
        config=SimpleNamespace(background_main_agent_allowed_tools=[]),
        runtime_guard_policy=RuntimeGuardPolicy(values={}),
        owner_policy=None,
        home_paths=None,
        conversation_store=None,
    )

    params = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="task-1",
            reason="scheduled_progress_report",
        ),
        agent,
    )

    assert params.task_attributes["max_tool_rounds"] == 32
    assert params.task_attributes["max_tool_calls_per_round"] == 4


def test_background_material_progress_uses_structured_tool_effect_variants() -> None:
    material_tool_success_count = execution_module.material_tool_success_count
    from agent_py_agent.agent.tooling import BaseTool, ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import (
        make_test_model_spec,
        make_test_runtime_policy,
        runtime_snapshot_for_tools,
    )

    class _Tool(BaseTool):
        def __init__(self, name, effect, effect_by_parameter=()):
            self.model_spec = make_test_model_spec(
                name,
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "path": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            )
            self.runtime_policy = make_test_runtime_policy(
                effect,
                effect_by_parameter=effect_by_parameter,
            )

        def execute(self, params):
            return ToolHandlerOutcome(self.model_spec.name, True, str(params))

    tools = {
        "task_progress": _Tool(
            "task_progress",
            "mutating",
            (
                (
                    "action",
                    (("", "read_only"), ("read", "read_only"), ("update", "mutating")),
                ),
            ),
        ),
        "read_file": _Tool("read_file", "read_only"),
    }
    snapshot = runtime_snapshot_for_tools(tools)
    agent = SimpleNamespace(tools=SimpleNamespace(runtime_snapshot=lambda: snapshot))
    readonly_calls = [
        {
            "tool": "task_progress",
            "ok": True,
            "parameters": {"tool": "task_progress", "action": "read"},
        },
        {
            "tool": "read_file",
            "ok": True,
            "parameters": {"tool": "read_file", "path": "README.md"},
        },
    ]
    assert material_tool_success_count(agent, readonly_calls) == 0

    update = {
        "tool": "task_progress",
        "ok": True,
        "parameters": {
            "tool": "task_progress",
            "action": "update",
            "summary": "checkpoint",
        },
    }
    assert material_tool_success_count(agent, [*readonly_calls, update]) == 1


def test_background_response_persists_public_operation_verification(tmp_path) -> None:
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    public_result_operation_verification = execution_module.public_result_operation_verification

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-operation",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    internal = {
        "schema": "operation_verification.v1",
        "status": "succeeded",
        "operation_count": 1,
        "counts": {"succeeded": 1},
        "operations": [
            {
                "tool": "remember",
                "action": "add",
                "verification_status": "succeeded",
                "call_id": "private-call",
                "operation_id": "private-operation",
                "attempt_count": 1,
                "replayed": False,
            }
        ],
    }
    public = public_result_operation_verification(
        type("Result", (), {"operation_verification": internal})()
    )

    delivery_module.record_background_response(_delivery_dependencies(runtime),
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            reason="scheduled_progress_report",
            now=11.0,
        ),
        DeliveryContext(
            channel="internal",
            target="thread-operation",
            thread_id=thread.thread_id,
        ),
        "我已经保存。",
        operation_verification=public,
        deliver=True,
        delivery_reason="scheduled_progress_report",
    )

    row = store.messages.recent(thread.thread_id, limit=1)[0]
    serialized = json.dumps(row.metadata["operation_verification"], ensure_ascii=False)
    assert row.metadata["operation_verification"]["groups"][0]["label"] == "remember/add"
    assert "private-call" not in serialized
    assert "private-operation" not in serialized


@pytest.mark.parametrize("end_reason", ["", "max-tokens", "completed"])
def test_background_response_persists_commentary_before_final(tmp_path, end_reason) -> None:
    """后台主代理的工具边界过程回复与 final 按 typed part 顺序进入同一会话。"""
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-parts",
            "channel": "internal",
            "channel_conversation_id": "thread-parts",
            "channel_user_id": "user-parts",
        }
    )
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())

    delivery_module.record_background_response(_delivery_dependencies(runtime),
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            reason="scheduled_progress_report",
        ),
        DeliveryContext(
            channel="internal",
            target="thread-parts",
            thread_id=thread.thread_id,
        ),
        "最终报告",
        assistant_commentaries=("先读代码", "再核对测试"),
        display_snapshot={"turn_end_reason": end_reason},
        deliver=True,
        delivery_reason="scheduled_progress_report",
    )

    rows = store.messages.recent(thread.thread_id, limit=0)
    assert [row.content for row in rows] == ["先读代码", "再核对测试", "最终报告"]
    assert [row.metadata.get("assistant_part_id") for row in rows] == [
        "commentary:1",
        "commentary:2",
        "final",
    ]
    assert rows[-1].metadata.get("turn_end_reason", "") == end_reason
    assert all("turn_end_reason" not in row.metadata for row in rows[:-1])


def test_background_model_result_keeps_typed_end_reason_in_frozen_snapshot(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import runtime as module

    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat",
        "channel_conversation_id": "background-length", "channel_user_id": "local-agent",
    })
    result = SimpleNamespace(response="继续修改", runtime_status="unfinished", runtime_reason="MODEL_RESPONSE_TRUNCATED")
    monkeypatch.setattr(execution_module, 'run_background_turn_with_compact', lambda *args, **kwargs: result)
    returned, snapshot = module._invoke_background_main_agent(
        BackgroundMainAgentRuntime(agent=agent, store=store), thread,
        module.BackgroundRunRequest(thread_id=thread.thread_id, task_id="task-length", reason="subagent_runner_finished"),
        module.GoalRuntimeContext(task_objective="继续当前项目"), (False, True),
    )
    assert returned is result
    assert snapshot["turn_end_reason"] == "max-tokens"
    assert snapshot["complete"] is True and snapshot["thread_id"] == thread.thread_id


def test_tui_background_final_is_canonical_history_and_retry_is_idempotent(tmp_path) -> None:
    """TUI notice 只是展示投影；后台 final 必须先进入下一轮模型会读取的同一历史。"""
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.conversation.history_projection import conversation_history_rows
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-tui-final",
            "channel": "tui",
            "channel_conversation_id": "session-tui-final",
            "channel_user_id": "owner-tui-final",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=DeliveryService(build_default_channel_registry(agent.config)),
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-tui-final",
        reason="subagent_runner_finished",
        wake_signal={"wake_signal_id": "wake-tui-final"},
    )
    context = DeliveryContext(
        channel="tui",
        target="session-tui-final",
        thread_id=thread.thread_id,
        task_id=request.task_id,
    )

    first = delivery_module.record_background_response(_delivery_dependencies(runtime),
        request,
        context,
        "八个子代理已完成，最终报告是 architecture_comparison_report.md。",
        deliver=True,
        delivery_reason="root_subagents_terminal",
    )
    second = delivery_module.record_background_response(_delivery_dependencies(runtime),
        request,
        context,
        "八个子代理已完成，最终报告是 architecture_comparison_report.md。",
        deliver=True,
        delivery_reason="root_subagents_terminal",
    )

    assert first.content == second.content
    assert first.delivery_status == second.delivery_status == "not_applicable"
    assert first.commit_kind == "canonical_record" and first.persisted
    assert second.message_id == first.message_id
    rows = store.messages.recent(thread.thread_id, limit=0)
    assert [row.content for row in rows] == [first.content]
    assert rows[0].channel == "tui"
    assert rows[0].metadata["assistant_part_id"] == "final"
    load_errors: list[dict] = []
    history_rows = conversation_history_rows(
        agent,
        thread.thread_id,
        "next-foreground-request",
        load_errors,
    )
    assert load_errors == []
    assert tuple((row.role, row.content) for row in history_rows) == (("assistant", first.content),)


def test_internal_audit_report_commits_source_refs_after_transcript_append(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.channels import (
        DeliveryContext,
        DeliveryReceipt,
    )
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    from agent_py_agent.agent.ingestion import harvester

    class _InternalDelivery:
        def supports_proactive(self, _channel: str) -> bool:
            return False

        def deliver(self, context, envelope):
            return DeliveryReceipt(
                channel=context.channel,
                target=context.target,
                content=envelope.content,
                thread_id=context.thread_id,
                task_id=context.task_id,
                delivery_status="not_applicable",
                evidence_refs=envelope.evidence_refs,
            )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-local-delivery",
            "channel_user_id": "owner-a",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_InternalDelivery(),
    )
    source_ref = "audit://watch-a/candidate/7:0"
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="audit-a",
        reason="audit_finding",
        wake_signal={
            "wake_signal_id": "wake-a",
            "root_task_id": "audit-a",
            "evidence_refs": [source_ref],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-a",
                "watch_id": "watch-a",
                "finding_id": "finding-a",
                "requires_llm_report": True,
                "delivery_evidence_refs": [source_ref],
            },
        },
    )
    recorded: list[tuple[tuple[str, ...], str, str]] = []

    def record(_owner_home, refs, *, receipt_id, channel, delivered_at=None):
        del delivered_at
        recorded.append((tuple(refs), receipt_id, channel))
        return list(refs)

    monkeypatch.setattr(harvester, "record_audit_delivery_refs", record)
    context = DeliveryContext(
        channel="internal",
        target=thread.thread_id,
        thread_id=thread.thread_id,
        task_id="audit-a",
    )

    first = delivery_module.record_background_response(_delivery_dependencies(runtime),
        request,
        context,
        "发现一项高风险事件。",
        deliver=True,
        delivery_reason="audit_finding_report",
    )
    second = delivery_module.record_background_response(_delivery_dependencies(runtime),
        request,
        context,
        "发现一项高风险事件。",
        deliver=True,
        delivery_reason="audit_finding_report",
    )

    assert first.content == "发现一项高风险事件。"
    assert first.delivery_status == "not_applicable"
    assert first.commit_kind == "canonical_record"
    assert second.content == first.content
    assert second.message_id == first.message_id
    messages = store.messages.recent(thread.thread_id, limit=0)
    assert len(messages) == 1
    assert recorded == [
        ((source_ref,), messages[0].message_id, "internal"),
        ((source_ref,), messages[0].message_id, "internal"),
    ]


def test_internal_audit_finding_run_uses_transcript_fallback_and_handles_wake(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.channels import DeliveryReceipt
    from agent_py_agent.agent.ingestion import harvester

    class _InternalDelivery:
        def supports_proactive(self, _channel: str) -> bool:
            return False

        def deliver(self, context, envelope):
            return DeliveryReceipt(
                channel=context.channel,
                target=context.target,
                content=envelope.content,
                thread_id=context.thread_id,
                task_id=context.task_id,
                delivery_status="not_applicable",
                evidence_refs=envelope.evidence_refs,
            )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-local-delivery",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-a",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "本地审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_InternalDelivery(),
    )
    source_ref = "audit://watch-a/candidate/7:0"
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: execution_module.BackgroundExecutionResult(
            response='发现一项高风险事件。',
            tool_call_count=0,
            tool_success_count=0,
            material_progress_count=0,
            delivery_artifacts=(),
            message_tool_deliveries=(),
            operation_verification={},
            assistant_commentaries=(),
            display_snapshot={},
        ),
    )
    recorded: list[tuple[tuple[str, ...], str, str]] = []

    def record(_owner_home, refs, *, receipt_id, channel, delivered_at=None):
        del delivered_at
        recorded.append((tuple(refs), receipt_id, channel))
        return list(refs)

    monkeypatch.setattr(harvester, "record_audit_delivery_refs", record)
    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-a",
            "reason": "audit_finding",
            "route_channel": "internal",
            "wake_signal": {
                "wake_signal_id": "wake-a",
                "root_task_id": "audit-a",
                "evidence_refs": [source_ref],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "audit_id": "audit-a",
                    "watch_id": "watch-a",
                    "finding_id": "finding-a",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [source_ref],
                },
            },
        }
    )

    assert report.delivery_reason == "audit_finding_transcript"
    assert report.delivery_status == "not_applicable"
    assert report.wake_handled is True
    messages = store.messages.recent(thread.thread_id, limit=0)
    assert [row.content for row in messages] == ["发现一项高风险事件。"]
    assert recorded == [((source_ref,), messages[0].message_id, "internal")]


def test_chat_audit_finding_commits_to_transcript_and_handles_wake(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.channels import DeliveryReceipt
    from agent_py_agent.agent.ingestion import harvester

    class _ChatTranscriptDelivery:
        def supports_proactive(self, _channel: str) -> bool:
            return False

        def supports_transcript(self, channel: str) -> bool:
            return channel == "chat"

        def deliver(self, context, envelope):
            return DeliveryReceipt(
                channel=context.channel,
                target=context.target,
                content=envelope.content,
                thread_id=context.thread_id,
                task_id=context.task_id,
                delivery_status="not_applicable",
                evidence_refs=envelope.evidence_refs,
            )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "cli-session-a",
            "channel_user_id": "local-agent",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-chat",
            "goal": "持续审计",
            "work_kind": "audit",
            "work_name": "CLI 审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_ChatTranscriptDelivery(),
    )
    source_ref = "audit://watch-chat/candidate/9:0"
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: execution_module.BackgroundExecutionResult(
            response='发现一项高风险事件。',
            tool_call_count=0,
            tool_success_count=0,
            material_progress_count=0,
            delivery_artifacts=(),
            message_tool_deliveries=(),
            operation_verification={},
            assistant_commentaries=(),
            display_snapshot={},
        ),
    )
    recorded: list[tuple[tuple[str, ...], str, str]] = []

    def record(_owner_home, refs, *, receipt_id, channel, delivered_at=None):
        del delivered_at
        recorded.append((tuple(refs), receipt_id, channel))
        return list(refs)

    monkeypatch.setattr(harvester, "record_audit_delivery_refs", record)
    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-chat",
            "reason": "audit_finding",
            "route_channel": "chat",
            "route_target": "cli-session-a",
            "wake_signal": {
                "wake_signal_id": "wake-chat",
                "root_task_id": "audit-chat",
                "evidence_refs": [source_ref],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "audit_id": "audit-chat",
                    "watch_id": "watch-chat",
                    "finding_id": "finding-chat",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [source_ref],
                },
            },
        }
    )

    assert report.delivery_reason == "audit_finding_transcript"
    assert report.delivery_status == "not_applicable"
    assert report.wake_handled is True
    messages = store.messages.recent(thread.thread_id, limit=0)
    assert [row.content for row in messages] == ["发现一项高风险事件。"]
    assert recorded == [((source_ref,), messages[0].message_id, "chat")]


def test_chat_transcript_persists_delivery_service_redaction(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
    from agent_py_agent.agent.ingestion import harvester

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "cli-session-redaction",
            "channel_user_id": "local-agent",
        }
    )
    task_id = "audit-chat-private-123456"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "goal": "持续审计",
            "work_kind": "audit",
            "work_name": "CLI 审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=DeliveryService(build_default_channel_registry(agent.config)),
    )
    source_ref = "audit://watch-chat/candidate/10:0"
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: execution_module.BackgroundExecutionResult(
            response=f'当前任务 {task_id}，会话 {thread.thread_id} 已发现高风险事件。',
            tool_call_count=0,
            tool_success_count=0,
            material_progress_count=0,
            delivery_artifacts=(),
            message_tool_deliveries=(),
            operation_verification={},
            assistant_commentaries=(),
            display_snapshot={},
        ),
    )
    monkeypatch.setattr(
        harvester,
        "record_audit_delivery_refs",
        lambda *_args, **_kwargs: [source_ref],
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "reason": "audit_finding",
            "route_channel": "chat",
            "route_target": "cli-session-redaction",
            "wake_signal": {
                "wake_signal_id": "wake-chat-redaction",
                "root_task_id": task_id,
                "evidence_refs": [source_ref],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "finding_id": "finding-chat-redaction",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [source_ref],
                },
            },
        }
    )

    assert report.wake_handled is True
    assert task_id not in report.response
    assert thread.thread_id not in report.response
    assert "当前任务" in report.response
    assert "当前会话" in report.response
    assert store.messages.recent(thread.thread_id, limit=1)[0].content == report.response


def test_unknown_external_audit_route_remains_retryable() -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    request = BackgroundRunRequest(
        thread_id="thread-a",
        task_id="audit-a",
        reason="audit_finding",
        wake_signal={
            "root_task_id": "audit-a",
            "evidence_refs": ["audit://watch-a/candidate/1:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "finding_id": "finding-a",
                "requires_llm_report": True,
                "delivery_evidence_refs": ["audit://watch-a/candidate/1:0"],
            },
        },
    )

    assert _background_delivery_decision(
        object(),
        request,
        resolved_channel="future-im",
        resolved_route_supports_proactive=False,
        resolved_route_supports_transcript=False,
    ) == (False, "audit_finding_delivery_unavailable")


@pytest.mark.parametrize(
    "reason",
    ["subagent_capability_request_open", "subagent_capability_granted"],
)
def test_capability_lifecycle_turns_are_internal_until_terminal_child_report(
    tmp_path,
    reason,
) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "capability-internal",
            "channel_user_id": "user-1",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "goal": "完成最小权限写入",
            "status": "active",
        }
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-root",
        reason=reason,
        wake_signal={
            "root_task_id": "task-root",
            "source_agent_id": "child-1",
            "metadata": {"run_id": "child-1"},
        },
    )

    assert _background_delivery_decision(agent, request, store=store) == (
        False,
        f"{reason}_internal",
    )


def test_internal_audit_finding_empty_reply_remains_retryable(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-local-empty",
            "channel_user_id": "owner-a",
        }
    )
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: execution_module.BackgroundExecutionResult(
            response='',
            tool_call_count=0,
            tool_success_count=0,
            material_progress_count=0,
            delivery_artifacts=(),
            message_tool_deliveries=(),
            operation_verification={},
            assistant_commentaries=(),
            display_snapshot={},
        ),
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-a",
            "reason": "audit_finding",
            "route_channel": "internal",
            "wake_signal": {
                "wake_signal_id": "wake-empty",
                "root_task_id": "audit-a",
                "evidence_refs": ["audit://watch-a/candidate/7:0"],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "finding_id": "finding-a",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": ["audit://watch-a/candidate/7:0"],
                },
            },
        }
    )

    assert report.delivery_status == "suppressed"
    assert report.wake_handled is False
    assert store.messages.recent(thread.thread_id, limit=0) == []


def test_background_run_restores_authoritative_task_workspace_and_title(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    workspace = tmp_path / "task-library"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-library",
            "goal": "图书馆运营方案",
            "task_path": str(workspace),
            "now": 11.0,
        }
    )

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(thread_id=thread.thread_id, task_id="task-library"),
        agent,
    )

    assert params.task_attributes["task_title"] == "图书馆运营方案"
    assert params.task_attributes["run_workspace"] == {
        "task_root": str(workspace),
        "output_dir": str(workspace / "output"),
        "work_dir": str(workspace / "work"),
    }


def test_named_background_run_keeps_work_name_as_workspace_title(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    workspace = tmp_path / "audit-workspace"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-stable-title",
            "goal": "一段会随 prepare 更新且可能很长的 Audit 生效要求",
            "task_path": str(workspace),
            "work_kind": "audit",
            "work_name": "生产安全巡检",
            "now": 11.0,
        }
    )

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="audit-stable-title",
            reason="audit_finding",
        ),
        agent,
    )

    assert params.task_attributes["task_title"] == "生产安全巡检"
    assert params.task_attributes["conversation_work_name"] == "生产安全巡检"


def _native_probe(self):
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

    return ProviderToolCapability(
        provider=str(self.name or "bg-test"),
        endpoint="local://bg-test",
        model="",
        stream=False,
        native_supported=True,
        evidence="test_backend_declares_native_tools",
        observed_at=_utc_now_iso(),
    )


@pytest.mark.parametrize("outcome", ["completed", "aborted", "error"])
def test_background_native_history_survives_each_execution_exit(tmp_path, monkeypatch, outcome):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation import runtime as module
    from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"channel": "tui", "channel_conversation_id": "background-native"})
    source = agent.home_paths.owner_home_dir / "background-fact.txt"
    source.write_text("后台事实 Cedar-47", encoding="utf-8")

    class Backend:
        name = "background-native-history-test"
        calls = 0
        probe_tool_capability = _native_probe

        def generate(self, _prompt, on_chunk=None, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text="读取事实", backend=self.name, tool_use_blocks=[{
                    "id": "background-read", "name": "read_file", "input": {"path": str(source)},
                }])
            if outcome != "completed":
                raise (InterruptedError if outcome == "aborted" else ValueError)("背景测试退出")
            return ModelResponse(text="已读取并核对 Cedar-47。", backend=self.name)

    agent.backend = Backend()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    request = module.BackgroundRunRequest(thread_id=thread.thread_id)
    monkeypatch.setattr(module, "_run_params", lambda *args, **kwargs: RunParams(
        source="background_main_agent", request_id="background-request", allowed_tools=["read_file"],
        task_attributes={"conversation_thread_id": thread.thread_id},
        conversation_history_seed=kwargs["history_seed"],
    ))
    sink = execution_module.BackgroundMainActivitySink(agent, thread_id=thread.thread_id, task_id="")

    def invoke():
        return execution_module.run_background_turn_with_compact(
            execution_module.BackgroundExecutionDependencies(agent=runtime.agent, store=runtime.store, prepare_run=module._run_params),
            thread,
            request,
            user_prompt='继续核对文件',
            continuation_injection=[],
            proactive_delivery_available=False,
            activity_sink=sink,
        )

    if outcome == "completed":
        result = invoke()
        delivery_module.record_background_response(_delivery_dependencies(runtime), request, module.DeliveryContext(channel="tui", target="", thread_id=thread.thread_id),
            result.response, deliver=True, delivery_reason="root_subagents_terminal")
    else:
        with pytest.raises(InterruptedError if outcome == "aborted" else ValueError):
            invoke()
    rows = store.messages.recent(thread.thread_id, limit=0)
    native_rows = [row for row in rows if row.metadata.get("canonical_native_messages")]
    assert len(native_rows) == 1
    assert native_rows[0].content == ""
    assert native_rows[0].metadata["turn_end_reason"] == outcome
    projected = provider_history_messages_from_rows(rows)
    blocks = [b for m in projected for b in m["content"] if isinstance(b, dict)]
    assert sum(b.get("type") == "tool_use" for b in blocks) == 1
    assert any(b.get("type") == "tool_result" and "Cedar-47" in str(b) for b in blocks)
    if outcome == "completed":
        assert sum("已读取并核对 Cedar-47。" in str(b) for b in blocks) == 1


def _provider_message_text(kwargs: dict) -> str:
    """把测试后端实际收到的 native messages 展开成可断言文本。"""
    parts: list[str] = []
    for message in list(kwargs.get("messages") or []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        for block in list(content or []):
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, dict):
                parts.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def _provider_tool_names(kwargs: dict) -> set[str]:
    """读取真实 native tools schema 中的工具名，不从诊断 prompt 猜工具可用性。"""
    return {
        str(item.get("name") or "")
        for item in list(kwargs.get("tools") or [])
        if isinstance(item, dict) and str(item.get("name") or "")
    }


class _CapturingBackend:
    name = "capturing"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.provider_texts: list[str] = []
        self.tool_names: list[set[str]] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.prompts.append(prompt)
        self.provider_texts.append(_provider_message_text(kwargs))
        self.tool_names.append(_provider_tool_names(kwargs))
        return ModelResponse(text="后台主代理已检查任务树，并给出阶段汇报。", backend=self.name)


class _NaturalCompletionBackend:
    name = "natural-completion"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.provider_texts: list[str] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.prompts.append(prompt)
        self.provider_texts.append(_provider_message_text(kwargs))
        return ModelResponse(text="任务全部完成。", backend=self.name)


class _SettlesLastChildBackend:
    name = "settles-last-child"

    def __init__(self, agent: SimpleAgent, child_id: str) -> None:
        self.agent = agent
        self.child_id = child_id

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        del prompt, on_chunk, kwargs
        self.agent.subagents.lifecycle.set_status(self.child_id, "DONE")
        return ModelResponse(text="当前仍在等待最后一个子代理。", backend=self.name)


class _BlockedCollaborationBackend:
    name = "blocked-collaboration"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "collaboration_case_closed" in prompt
            assert "不可达=1" in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "collection_result" in prompt
        assert "ready_to_report" in prompt
        return ModelResponse(text="协作阻塞已确认：需要主代理调整策略。", backend=self.name)


class _PlainLanguageCollaborationBackend:
    name = "plain-language-collaboration"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "帮我协调几个后台代理，有阻塞就继续安排或告诉我" in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            assert "collection_result" in prompt
            assert "ready_to_report" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"update_collaboration","case_id":"{self.case_id}",'
                    '"status":"needs_replan","summary":"已看到阻塞请求，下一步需要换来源或补派代理。"}'
                    "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "needs_replan" in prompt
        return ModelResponse(
            text="我已经看到阻塞点，会换来源或补派代理继续推进。", backend=self.name
        )


class _SlowBackend:
    name = "slow"

    def __init__(self, *, sleep_seconds: float):
        self.sleep_seconds = sleep_seconds

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        time.sleep(self.sleep_seconds)
        return ModelResponse(text="后台主代理慢速检查完成。", backend=self.name)


class _FailingBackend:
    name = "failing"

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        raise RuntimeError("backend boom")


class _GoalToolProgressBackend:
    name = "goal-tool-progress"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[
                    {
                        "id": "call-goal-list-1",
                        "name": "list_files",
                        "input": {"path": "."},
                    }
                ],
            )
        return ModelResponse(text="本轮已经根据目录事实继续推进。", backend=self.name)


class _GoalCompletingBackend:
    name = "goal-completing"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []
        self.provider_texts: list[str] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        self.provider_texts.append(_provider_message_text(kwargs))
        if self.calls == 1:
            return ModelResponse(
                text="", backend=self.name,
                tool_use_blocks=[{"id": "complete-goal", "name": "update_goal", "input": {"status": "complete"}}],
            )
        return ModelResponse(text="已经完成整合和验证。", backend=self.name)


class _MidTurnLifecycleBackend:
    name = "mid-turn-lifecycle"

    def __init__(self, *, store, thread_id: str, task_id: str, fail_after_injection: bool = False):
        self.store = store
        self.thread_id = thread_id
        self.task_id = task_id
        self.fail_after_injection = fail_after_injection
        self.calls = 0
        self.prompts: list[str] = []
        self.provider_texts: list[str] = []
        self.signal = None

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        self.provider_texts.append(_provider_message_text(kwargs))
        if self.calls == 1:
            self.signal = self.store.wakes.raise_signal(
                {
                    "thread_id": self.thread_id,
                    "root_task_id": self.task_id,
                    "reason": "subagent_runner_finished",
                    "source_agent_id": "child-mid-turn",
                    "metadata": {"task_id": "child-mid-turn", "status": "DONE"},
                    "now": 20.5,
                }
            )
            return ModelResponse(text="这是子代理完成前生成的旧状态。", backend=self.name)
        provider_text = _provider_message_text(kwargs)
        assert "[RUNTIME_TASK_EVENTS]" in provider_text
        assert "child-mid-turn" in provider_text
        if self.fail_after_injection:
            raise RuntimeError("provider failed after runtime event injection")
        return ModelResponse(text="已接收子代理的新结果并继续整合。", backend=self.name)


class _InternalStatusBackend:
    name = "internal-status"

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        return ModelResponse(
            text=(
                "[RUN_TOOL_EVIDENCE_BLOCKED]\n"
                '{"reason":"scheduled_progress_report","private":"must-not-enter-chat"}'
            ),
            backend=self.name,
        )


def test_background_runtime_reports_corrupt_thread_before_running_model(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.storage.thread_path(thread.thread_id).write_text("{bad-json", encoding="utf-8")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())

    try:
        runtime.run_once({"thread_id": thread.thread_id, "reason": "scheduled_progress_report"})
    except DataCorruptionError as exc:
        assert "conversation.thread.read" in str(exc)
        assert thread.thread_id in str(exc)
    else:
        raise AssertionError("corrupt thread should be reported as data corruption")


def test_due_progress_policy_wakes_background_main_agent_and_sends_message(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "长期后台任务",
            "now": 10.0,
        }
    )
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "每小时帮我看一次进展，有问题就调度。",
            "channel": "internal",
            "metadata": {"gateway_request_id": "task-1"},
            "now": 11.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "观察子代理任务树",
            "now": 12.0,
        }
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 13.0,
        }
    )

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert "每小时帮我看一次进展" in backend.provider_texts[0]
    assert "inspect_agent_tree" not in backend.tool_names[0]
    assert "create_subagents" in backend.tool_names[0]
    assert "dispatch_subagents" not in backend.tool_names[0]
    sent = channels.adapter("internal").sent_messages
    assert sent[0].target == "thread-1"
    assert "后台主代理已检查任务树" in sent[0].content
    from agent_py_agent.agent.conversation.message_stream import read_background_response_page

    rows, cursor, ok = read_background_response_page(store, thread.thread_id)
    assert ok and cursor > 0
    assert rows[-1]["content"] == reports[0].response
    assert not (store.storage.root / "notices").exists()


def test_thread_goal_turn_with_no_tool_calls_keeps_active_goal_continuation(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    goal = store.goals.create(
        {"thread_id": thread.thread_id, "objective": "持续推进同一件工作", "now": 11.0}
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
            "now": 12.0,
        }
    )
    first_wake = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
            "now": 13.0,
        }
    )

    reports = scheduler.tick(now=14.0)

    assert len(reports) == 1
    assert "Continue working toward the active thread goal" in backend.prompts[0]
    updated = store.goals.load(thread.thread_id)
    assert updated is not None and updated.status == "active"
    pending = store.wakes.pending()
    assert len(pending) == 1
    assert pending[0].reason == "thread_goal_continue"
    assert pending[0].metadata["goal_id"] == goal.goal_id
    assert pending[0].wake_signal_id != first_wake.wake_signal_id
    assert first_wake.status == "pending"
    assert reports[0].delivery_status == "sent"
    assert reports[0].delivery_reason == "thread_goal_progress"
    assert [item.content for item in runtime.channels.adapter("internal").sent_messages] == [
        "后台主代理已检查任务树，并给出阶段汇报。"
    ]
    rows = _public_background_messages(store, thread.thread_id)
    assert [(item.role, item.content) for item in rows] == [
        ("assistant", "后台主代理已检查任务树，并给出阶段汇报。")
    ]


def test_thread_goal_with_tool_progress_schedules_exactly_one_next_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.backend = _GoalToolProgressBackend()
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-progress",
            "channel_user_id": "user-1",
        }
    )
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "持续检查目录并推进"})
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    first = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    reports = scheduler.tick()

    assert len(reports) == 1 and reports[0].tool_call_count == 1
    pending = store.wakes.pending()
    assert len(pending) == 1
    assert pending[0].wake_signal_id != first.wake_signal_id
    assert pending[0].reason == "thread_goal_continue"


def test_thread_goal_waits_for_child_events_without_polling_or_chat_noise(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _GoalToolProgressBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-child",
            "channel_user_id": "user-1",
        }
    )
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "完成一个并行项目"})
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    agent.subagents.create_run(
        goal="实现模块甲",
        thought="",
        plan=["实现"],
        parent_id=goal.task_id,
        root_id=goal.task_id,
    )
    first = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id=goal.task_id,
            reason="thread_goal_continue",
        ),
        agent,
    )
    reports = scheduler.tick()

    assert "inspect_agent_tree" not in params.allowed_tools
    assert "create_subagents" in params.allowed_tools
    assert reports == []
    assert backend.prompts == []
    assert store.wakes.pending() == []
    assert first.status == "pending"
    assert channels.adapter("internal").sent_messages == []
    assert store.messages.recent(thread.thread_id) == []

    guidance = store.guidance.append(
        {
            "target_type": "task",
            "target_id": goal.task_id,
            "message": "补充一个当前任务要求",
            "sender": "user",
        }
    )
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal-guided:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id, "guidance_id": guidance.guidance_id},
        }
    )

    guided_reports = scheduler.tick()

    assert len(guided_reports) == 1
    assert guided_reports[0].delivery_status == "sent"
    assert guided_reports[0].delivery_reason == "thread_goal_progress"
    assert [item.content for item in channels.adapter("internal").sent_messages] == [
        "本轮已经根据目录事实继续推进。"
    ]
    assert "Their lifecycle events will wake this same goal again" in backend.prompts[0]
    assert store.wakes.pending() == []


def test_terminal_goal_children_trigger_one_integrating_closeout(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _GoalCompletingBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-closeout",
            "channel_user_id": "user-1",
        }
    )
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "完成并验证整个项目"})
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    child = agent.subagents.create_run(
        goal="完成实现",
        thought="",
        plan=["实现"],
        parent_id=goal.task_id,
        root_id=goal.task_id,
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "subagent_runner_finished",
            "dedupe_key": f"goal-child:{child.id}",
            "metadata": {"task_id": child.id, "status": "DONE"},
        }
    )

    reports = scheduler.tick(now=signal.created_at + 100)

    assert len(reports) == 1
    assert reports[0].delivery_status == "sent"
    assert reports[0].delivery_reason == "thread_goal_completion"
    assert "Completion audit" in backend.provider_texts[0]
    # 孩子终态不能直接代替目标完成：核对模型实际调用的原生工具，而非已删除的软提示句子。
    assert backend.calls == 2
    native = [message for row in store.messages.recent(thread.thread_id, limit=0)
              for message in row.metadata.get("canonical_native_messages", {}).get("messages", [])]
    goal_calls = [block for message in native for block in message.get("content", [])
                  if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "update_goal"]
    assert len(goal_calls) == 1 and goal_calls[0]["input"]["status"] == "complete"
    assert store.goals.load(thread.thread_id).status == "complete"
    links = {item.task_id: item for item in store.tasks.list(thread.thread_id)}
    assert links[goal.task_id].status == "completed"
    assert [item.content for item in channels.adapter("internal").sent_messages] == [
        "已经完成整合和验证。"
    ]
    final_row = store.messages.recent(thread.thread_id, limit=1)[0]
    assert final_row.metadata["operation_verification"]["groups"][0]["label"] == "update_goal"

    stale = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "subagent_runner_finished",
            "dedupe_key": f"goal-child-stale:{child.id}",
            "metadata": {"task_id": child.id, "status": "DONE"},
        }
    )
    assert scheduler._run_wake_signal(stale, now=time.time()) is None
    assert backend.calls == 2
    assert signal.status == "pending"


def test_thread_goal_provider_usage_limit_maps_to_usage_limited(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-limit",
            "channel_user_id": "user-1",
        }
    )
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "持续推进"})
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    def fail_run(_request):
        raise ProviderUsageLimitError("HTTP 429")

    monkeypatch.setattr(scheduler, "_run_claimed", fail_run)
    with pytest.raises(ProviderUsageLimitError):
        scheduler._run_wake_signal(signal, now=20.0)

    updated = store.goals.load(thread.thread_id)
    assert updated is not None and updated.status == "usage_limited"
    links = {item.task_id: item for item in store.tasks.list(thread.thread_id)}
    assert links[goal.task_id].status == "interrupted"
    assert store.wakes.pending() == []


def test_completed_task_drops_queued_scheduled_continuation(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "foreground-terminal-wake",
            "channel_user_id": "user-1",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-complete",
            "goal": "完成长任务",
            "status": "active",
        }
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "task-complete",
            "reason": "scheduled_progress_report",
            "dedupe_key": "foreground-task-complete",
        }
    )
    store.tasks.update_status({"task_id": "task-complete", "status": "completed"})

    assert scheduler._run_wake_signal(signal, now=time.time()) is None
    assert backend.prompts == []
    assert store.wakes.pending() == []


def test_task_continuation_uses_the_same_thread_history_and_compact(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "请完成任务甲的七天晚餐方案。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "task-1"},
            "now": 11.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "完成任务甲的七天晚餐方案",
            "now": 12.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-2",
            "goal": "任务乙私有目标-不应出现在任务甲",
            "now": 13.0,
        }
    )
    store.threads.update_summary(thread.thread_id, "普通聊天压缩摘要-青柚47", now=14.0)
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "普通聊天核对词青柚47，不要把它写进任务。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "chat-request-2"},
            "now": 15.0,
        }
    )
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "预算控制在三百元内。",
            "channel": "feishu",
            "metadata": {"kind": "active_turn_user_input"},
            "now": 16.0,
        }
    )
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "第二步只实现营养评分、时间衰减和对应测试。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "chat-request-3"},
            "now": 16.75,
        }
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 17.0,
        }
    )

    reports = scheduler.tick(now=77.0)
    prompt = backend.provider_texts[0]

    assert len(reports) == 1
    assert "请完成任务甲的七天晚餐方案" in prompt
    assert "完成任务甲的七天晚餐方案" in prompt
    assert "预算控制在三百元内" in prompt
    assert "第二步只实现营养评分、时间衰减和对应测试" in prompt
    assert "青柚47" in prompt
    assert "普通聊天压缩摘要-青柚47" in prompt
    # The shared transcript/compact stays visible, but an unrelated task link is
    # operational metadata rather than conversation history.
    assert "任务乙私有目标" not in prompt
    assert '"ordinary_thread_messages_included": false' not in prompt
    assert '"conversation_compact_included": false' not in prompt


def test_detached_named_task_excludes_future_ordinary_turns_from_background_context(
    tmp_path,
) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.threads.update_summary(thread.thread_id, "创建前安全摘要-银杏31", now=10.25)
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "创建前约定：使用已确认的五个来源。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "chat-before"},
            "now": 11.0,
        }
    )
    link = store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判五个来源",
            "work_kind": "audit",
            "work_name": "五路监测",
            "cancellation_scope": "detached",
            "now": 12.0,
        }
    )
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "启动五路监测。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "audit-1"},
            "now": 12.25,
        }
    )
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "五路来源工作者已建立。",
            "channel": "feishu",
            "metadata": {"task_id": "audit-1"},
            "now": 12.5,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "ordinary-code-task",
            "goal": "实现普通代码任务-不应进入 Audit",
            "now": 13.0,
        }
    )
    store.threads.update_summary(thread.thread_id, "后来污染摘要-红杉99", now=14.0)
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "后来普通任务：请新建 LRU 缓存项目-红杉99。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "ordinary-code-task"},
            "now": 15.0,
        }
    )
    guidance = store.guidance.append(
        {
            "target_type": "task",
            "target_id": "audit-1",
            "message": "只给高置信发现发消息。",
            "metadata": {
                "record_in_transcript": True,
                "thread_id": thread.thread_id,
                "channel": "feishu",
            },
            "now": 16.0,
        }
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "interval_seconds": 60,
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 17.0,
        }
    )

    reports = scheduler.tick(now=77.0)
    prompt = backend.provider_texts[0]
    rows = store.messages.recent(thread.thread_id, limit=0)
    guidance_row = next(
        row for row in rows if row.metadata.get("guidance_id") == guidance.guidance_id
    )

    assert len(reports) == 1
    assert link.context_anchor_message_id
    assert "创建前约定：使用已确认的五个来源" in prompt
    assert "创建前安全摘要-银杏31" not in prompt
    assert "持续研判五个来源" in prompt
    assert "启动五路监测" in prompt
    assert "五路来源工作者已建立" in prompt
    assert "只给高置信发现发消息" in prompt
    assert "后来普通任务" not in prompt
    assert "LRU 缓存" not in prompt
    assert "红杉99" not in prompt
    assert "实现普通代码任务" not in prompt
    assert guidance_row.metadata["task_id"] == "audit-1"


def test_removed_automatic_supervision_policy_is_retired_without_model_turn(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(
        goal="后台做长任务",
        thought="",
        plan=["执行"],
        parent_id="task-1",
        root_id="task-1",
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "后台做长任务",
            "now": 11.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "now": 12.0,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "dispatch_supervision_auto",
                "watched_run_ids": [child.id],
            },
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    assert scheduler.tick(now=73.0) == []
    assert backend.prompts == []
    checked = store.progress.load(policy.policy_id)
    assert checked is not None and checked.enabled is False
    assert checked.last_report_at == 73.0


def test_periodic_policy_for_durable_audit_source_worker_is_retired(tmp_path) -> None:
    from agent_py_agent.agent.common.audit_activation import AUDIT_SOURCE_WORKER_ATTR
    from agent_py_agent.agent.conversation.runtime import (
        _runnable_due_policies,
        _snooze_suppressed_policies,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    child = agent.subagents.create_run(
        goal="持续处理一个来源",
        thought="",
        plan=["按租约处理批次"],
        parent_id="audit-1",
        root_id="audit-1",
        attributes={AUDIT_SOURCE_WORKER_ATTR: True},
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 9.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "interval_seconds": 60,
            "now": 10.0,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "dispatch_supervision_auto",
                "watched_run_ids": [child.id],
            },
        }
    )

    runnable, suppressed = _runnable_due_policies(
        store,
        [policy],
        now=71.0,
        agent=agent,
    )
    rows = _snooze_suppressed_policies(
        store,
        suppressed,
        now=71.0,
        agent=agent,
    )

    assert runnable == []
    assert suppressed == [(policy, "removed_dispatch_supervision_policy")]
    assert rows[0]["reason"] == "removed_dispatch_supervision_policy"
    retired = store.progress.load(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_subagent_owned_resume_policy_is_retired_without_root_model_turn(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        _runnable_due_policies,
        _snooze_suppressed_policies,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    child = agent.subagents.create_run(
        goal="继续完成子代理自己的模块",
        thought="沿 child thread 续跑",
        plan=["完成模块"],
        parent_id="task-root",
        root_id="task-root",
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-child-policy",
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
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "interval_seconds": 60,
            "now": 10.0,
            "metadata": {
                "kind": "ordinary_task_resume",
                "tool": "task_round_resume",
            },
        }
    )

    runnable, suppressed = _runnable_due_policies(
        store,
        [policy],
        now=71.0,
        agent=agent,
    )
    rows = _snooze_suppressed_policies(
        store,
        suppressed,
        now=71.0,
        agent=agent,
    )

    assert runnable == []
    assert suppressed == [(policy, "removed_ordinary_task_resume_policy")]
    assert rows[0]["reason"] == "removed_ordinary_task_resume_policy"
    retired = store.progress.load(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_child_bound_wake_is_acknowledged_without_root_model_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(
        goal="继续完成子代理自己的模块",
        thought="沿 child thread 续跑",
        plan=["完成模块"],
        parent_id="task-root",
        root_id="task-root",
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-child-wake",
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
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": child.id,
            "reason": "scheduled_progress_report",
            "dedupe_key": f"legacy-child-resume:{child.id}",
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=time.time())

    assert len(reports) == 1
    assert reports[0].delivery_reason == "subagent_runner_owns_continuation"
    assert reports[0].wake_handled is True
    assert backend.prompts == []
    assert store.wakes.pending() == []


def test_partial_successful_subagent_wake_stays_out_of_ordinary_chat_until_batch_finishes(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    first = agent.subagents.create_run(
        goal="完成第一部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    second = agent.subagents.create_run(
        goal="完成第二部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "RUNNING")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "分两部分完成", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    partial = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": first.id,
                "metadata": {"task_id": first.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )

    assert partial.delivery_status == "suppressed"
    assert partial.delivery_reason == "partial_subagent_success"
    assert channels.adapter("internal").sent_messages == []
    assert _public_background_messages(store, thread.thread_id) == []

    agent.backend = _NaturalCompletionBackend()
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    final = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": second.id,
                "metadata": {"task_id": second.id, "status": "DONE"},
            },
            "now": 30.0,
        }
    )

    assert final.delivery_status == "sent"
    assert final.delivery_reason == "root_subagents_terminal"
    assert len(channels.adapter("internal").sent_messages) == 1
    assert [row.content for row in _public_background_messages(store, thread.thread_id)] == [final.response]


def test_child_settling_during_background_work_does_not_require_an_extra_wake(
    tmp_path,
) -> None:
    """开始快照不能否定当前子树和空邮箱；正常结束后不靠用户补一句才能看到汇报。"""
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent, store, thread, runtime, first, second = _child_settlement_fixture(tmp_path)
    agent.backend = _SettlesLastChildBackend(agent, second.id)
    partial = _run_child_done_wake(runtime, thread.thread_id, first.id, now=20.0)

    assert partial.delivery_status == "sent"
    assert partial.delivery_reason == "root_subagents_terminal"
    assert partial.response
    assert store.tasks.load("task-root").status == "completed"
    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="task-root",
            reason="subagent_runner_finished",
        ),
        agent,
    )
    assert (
        params.task_attributes[CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR] == "subagents_terminal"
    )

def test_terminal_subagent_reply_does_not_wait_for_root_task_status(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    child = agent.subagents.create_run(
        goal="完成交付",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-terminal-delivery",
            "channel_user_id": "user-1",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "goal": "完成全部工作",
            "status": "active",
        }
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-root",
        reason="subagent_runner_finished",
        wake_signal={
            "root_task_id": "task-root",
            "source_agent_id": child.id,
            "metadata": {"task_id": child.id, "status": "DONE"},
        },
    )

    assert store.tasks.load("task-root").status == "active"
    assert _background_delivery_decision(
        agent,
        request,
        store=store,
    ) == (True, "root_subagents_terminal")


def _child_settlement_fixture(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    first = agent.subagents.create_run(
        goal="完成第一部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    second = agent.subagents.create_run(
        goal="完成第二部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "分两部分完成", "now": 11.0}
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    return agent, store, thread, runtime, first, second


def _run_child_done_wake(runtime, thread_id: str, child_id: str, *, now: float):
    return runtime.run_once(
        {
            "thread_id": thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": child_id,
                "metadata": {"task_id": child_id, "status": "DONE"},
            },
            "now": now,
        }
    )


def test_exact_root_subagent_load_error_suppresses_until_root_task_is_completed(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    child = agent.subagents.create_run(
        goal="完成当前部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    (agent.subagents.workspace / child.id / "task.json").write_text(
        "{ broken",
        encoding="utf-8",
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "完成全部工作", "now": 11.0}
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-root",
        reason="subagent_runner_finished",
        wake_signal={
            "root_task_id": "task-root",
            "source_agent_id": child.id,
            "metadata": {"task_id": child.id, "status": "DONE"},
        },
    )

    deliver, reason = _background_delivery_decision(agent, request, store=store)

    assert deliver is False
    assert reason == "subagent_state_load_error"

    store.tasks.update_status({"task_id": "task-root", "status": "completed", "now": 20.0})
    deliver, reason = _background_delivery_decision(agent, request, store=store)

    assert deliver is True
    assert reason == "root_task_completed_with_subagent_state_load_error"


def test_unrelated_broken_subagent_history_does_not_block_exact_root_delivery(tmp_path) -> None:
    """另一棵历史树损坏时，当前 root 的完整 canonical 状态仍可正常收口。"""
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    child = agent.subagents.create_run(
        goal="完成当前部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    unrelated = agent.subagents.workspace / "unrelated-broken-run"
    unrelated.mkdir(parents=True)
    (unrelated / "task.json").write_text("{ broken", encoding="utf-8")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "完成全部工作", "now": 11.0}
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-root",
        reason="subagent_runner_finished",
        wake_signal={
            "root_task_id": "task-root",
            "source_agent_id": child.id,
            "metadata": {"task_id": child.id, "status": "DONE"},
        },
    )

    deliver, reason = _background_delivery_decision(agent, request, store=store)

    assert deliver is True
    assert reason == "root_subagents_terminal"


@pytest.mark.parametrize(
    ("requires_llm_report", "evidence_refs", "expected"),
    [
        (
            True,
            ["audit://watch-1/candidate/1:0"],
            (False, "audit_finding_message_tool_only"),
        ),
        (False, ["audit://watch-1/candidate/1:0"], (False, "audit_finding_not_reportable")),
        (True, [], (False, "audit_finding_not_reportable")),
    ],
)
def test_audit_finding_delivery_requires_typed_report_request_and_evidence(
    tmp_path,
    requires_llm_report,
    evidence_refs,
    expected,
) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="audit-1",
        reason="audit_finding",
        wake_signal={
            "root_task_id": "audit-1",
            "source_agent_id": "source-worker-1",
            "evidence_refs": evidence_refs,
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "af-1",
                "requires_llm_report": requires_llm_report,
                "delivery_evidence_refs": evidence_refs,
            },
        },
    )

    assert _background_delivery_decision(agent, request, store=store) == expected


def test_audit_finding_tool_profile_uses_typed_message_delivery() -> None:
    from agent_py_agent.agent.conversation.background_tool_policy import (
        BackgroundToolPolicyRequest,
        background_tool_policy_decision,
    )

    decision = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(reason="audit_finding")
    )

    assert decision.profile == "audit_finding"
    assert "send_message" in decision.allowed_tools
    assert "watch_stream" in decision.allowed_tools
    assert "record_finding" not in decision.allowed_tools


def test_subagent_lifecycle_continuation_keeps_skill_body_access() -> None:
    """同一任务被 child 事件唤醒后仍能继续读取该任务已选 Skill 的正文。"""
    from agent_py_agent.agent.conversation.background_tool_policy import (
        BackgroundToolPolicyRequest,
        background_tool_policy_decision,
    )

    decision = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(reason="subagent_runner_finished")
    )

    assert decision.profile == "subagent_integration"
    assert "skill_search" in decision.allowed_tools


def test_subagent_lifecycle_continuation_keeps_owner_memory_tools() -> None:
    """等待 child 时收到的新偏好与长期事实仍由同一 owner Agent 自主落账。"""
    from agent_py_agent.agent.conversation.background_tool_policy import (
        BackgroundToolPolicyRequest,
        background_tool_policy_decision,
    )

    decision = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(reason="subagent_runner_finished")
    )

    assert decision.profile == "subagent_integration"
    assert "remember" in decision.allowed_tools
    assert "update_persona" in decision.allowed_tools


@pytest.mark.parametrize("reason", [
    "subagent_runner_finished", "thread_goal_continue", "scheduled_job_due", "audit_finding",
])
def test_background_runtime_snapshot_contains_continuation_tools(tmp_path, reason) -> None:
    """策略名单、主代理 registry 与冻结快照必须给同一后台续轮完全相同的能力。"""
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            execution_mode="local_unmanaged",
        ),
        tmp_path / "service-root",
    )
    params = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="task-1",
            reason=reason,
        ),
        agent,
    )
    snapshot = agent.tools.runtime_snapshot(
        allowed_tools=params.allowed_tools,
        run_id=params.run_id,
    )

    expected = {
        "skill_search", "remember", "update_persona",
        "run_command", "process_session", "terminal_session",
    }
    if params.allowed_tools is not None:
        assert expected.issubset(params.allowed_tools)
    assert expected.issubset(snapshot.available_tool_names)
    assert expected.issubset(
        {
            spec.name
            for spec in agent.tools.model_visible_specs(
                allowed_tools=params.allowed_tools,
                runtime_snapshot=snapshot,
            )
        }
    )


@pytest.mark.parametrize("restriction", ["configured", "owner", "task"])
def test_background_terminal_continuation_respects_explicit_restrictions(restriction) -> None:
    """默认目录完整不等于扩权；显式工具清单或禁用策略仍收紧快照与提示。"""
    from agent_py_agent.agent.conversation.background_tool_policy import (
        BackgroundToolPolicyRequest,
        background_control_action_lines,
        background_tool_policy_decision,
    )

    request = BackgroundToolPolicyRequest(
        reason="thread_goal_continue",
        config=SimpleNamespace(background_main_agent_allowed_tools=["run_command"])
        if restriction == "configured" else None,
        owner_policy=SimpleNamespace(disabled_tools=["terminal_session"])
        if restriction == "owner" else None,
        policy_snapshot={"allowed_tools": ["run_command", "process_session"]}
        if restriction == "task" else None,
    )

    decision = background_tool_policy_decision(request=request)

    assert {"run_command", "process_session"}.issubset(decision.allowed_tools)
    assert "terminal_session" not in decision.allowed_tools
    assert not any("terminal_session" in line for line in background_control_action_lines(request=request))


def test_audit_finding_internal_route_hides_proactive_delivery_tool() -> None:
    from agent_py_agent.agent.conversation.background_tool_policy import (
        BackgroundToolPolicyRequest,
        background_tool_policy_decision,
    )
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _run_params,
        background_prompt,
    )

    decision = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(
            reason="audit_finding",
            proactive_delivery_available=False,
        )
    )
    params = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="audit-1",
            reason="audit_finding",
        ),
        proactive_delivery_available=False,
    )
    prompt = background_prompt(
        "audit_finding",
        proactive_delivery_available=False,
    )

    assert decision.profile == "audit_finding"
    assert "send_message" not in decision.allowed_tools
    assert "send_message" in decision.removed_tools
    assert "delivery_route_capability" in decision.sources
    assert "send_message" not in (params.allowed_tools or [])
    assert "conversation transcript" in prompt
    assert "final assistant text" in prompt


def test_audit_finding_message_tool_delivery_is_mirrored_once_with_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=channels,
    )
    request = {
        "thread_id": thread.thread_id,
        "task_id": "audit-1",
        "reason": "audit_finding",
        "wake_signal": {
            "wake_signal_id": "wake-af-1",
            "root_task_id": "audit-1",
            "evidence_refs": ["audit://watch-1/candidate/1:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "af-1",
                "requires_llm_report": True,
                "delivery_evidence_refs": ["audit://watch-1/candidate/1:0"],
            },
        },
        "now": 20.0,
    }

    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: execution_module.BackgroundExecutionResult(
            response='这段内部收口文字不能成为第二条用户消息。',
            tool_call_count=0,
            tool_success_count=0,
            material_progress_count=0,
            delivery_artifacts=(),
            message_tool_deliveries=({'schema_version': 'message_tool_delivery.v1', 'delivery_status': 'sent', 'source_owner_delivery': True, 'channel': 'feishu', 'content': '发现一项明确事件，证据已保留。', 'receipt_id': 'receipt-af-1', 'evidence_refs': ['audit://watch-1/candidate/1:0'], 'deduplicated': False, 'attachments': []},),
            operation_verification={},
            assistant_commentaries=(),
            display_snapshot={},
        ),
    )
    sent = runtime.run_once(request)

    assert sent.delivery_status == "sent"
    assert sent.delivery_reason == "audit_finding_report"
    assert sent.wake_handled is True
    messages = store.messages.recent(thread.thread_id)
    assert [row.content for row in messages] == ["发现一项明确事件，证据已保留。"]
    assert messages[0].metadata["evidence_refs"] == ["audit://watch-1/candidate/1:0"]
    # send_message already performed the external side effect. The runtime only
    # mirrors its typed receipt and must not call the channel a second time.
    assert channels.adapter("feishu").sent_messages == []


def test_audit_finding_without_message_tool_delivery_stays_internal_and_retryable(
    tmp_path,
    monkeypatch,
) -> None:
    class _FailedDelivery:
        def supports_proactive(self, channel: str) -> bool:
            return channel == "feishu"

        def deliver(self, context, envelope):
            raise AssertionError("internal final text must not reach the channel")

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_FailedDelivery(),
    )
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: execution_module.BackgroundExecutionResult(
            response='发现一项明确事件，证据已保留。',
            tool_call_count=0,
            tool_success_count=0,
            material_progress_count=0,
            delivery_artifacts=(),
            message_tool_deliveries=(),
            operation_verification={},
            assistant_commentaries=(),
            display_snapshot={},
        ),
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "reason": "audit_finding",
            "wake_signal": {
                "wake_signal_id": "wake-af-failed",
                "root_task_id": "audit-1",
                "evidence_refs": ["audit://watch-1/candidate/1:0"],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "finding_id": "af-1",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": ["audit://watch-1/candidate/1:0"],
                },
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "suppressed"
    assert report.delivery_reason == "audit_finding_message_tool_only"
    assert report.wake_handled is False
    assert store.messages.recent(thread.thread_id) == []


def test_audit_finding_context_projects_exact_event_without_supervision_noise(
    tmp_path,
) -> None:
    from agent_py_agent.agent.conversation.background_context import context_markdown
    from agent_py_agent.agent.conversation.runtime import (
        _AUDIT_FINDING_REPORT_PROMPT,
        BackgroundRunRequest,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判五路安全数据并及时报告真实事件",
            "work_kind": "audit",
            "work_name": "安全审计",
            "cancellation_scope": "detached",
            "run_prompt": "继续当前五路监控并及时报告真实事件",
            "effective_revision": 5,
            "effective_source_bindings": [
                {
                    "source_id": "proc-source",
                    "url": "https://events.invalid/proc",
                    "source_profile_ref": "/owner/audits/audit-1/work/sources/proc.txt",
                }
            ],
        }
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="audit-1",
        reason="audit_finding",
        wake_signal={
            "wake_signal_id": "wake-af-1",
            "root_task_id": "audit-1",
            "summary": "检测到命令执行并获得稳定回显",
            "evidence_refs": ["audit://watch-1/candidate/97:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "report_scope": "incremental",
                "finding_id": "af-1",
                "requires_llm_report": True,
                "source_id": "proc-source",
                "score": 95,
                "verdict": "hit",
                "evidence_records": [
                    {
                        "source_ref": "audit://watch-1/candidate/97:0",
                        "inline": True,
                        "raw_complete": True,
                        "raw_event": {"process_event_id": "PROC-0097"},
                    }
                ],
            },
        },
    )

    rendered = context_markdown(
        agent=agent,
        store=store,
        thread=thread,
        request=request,
    )

    assert "检测到命令执行并获得稳定回显" in rendered
    assert "audit://watch-1/candidate/97:0" in rendered
    assert "PROC-0097" in rendered
    assert "finding_id is an internal delivery" in _AUDIT_FINDING_REPORT_PROMPT
    assert "report_scope is incremental" in _AUDIT_FINDING_REPORT_PROMPT
    assert "继续当前五路监控并及时报告真实事件" in rendered
    assert "/owner/audits/audit-1/work/sources/proc.txt" in rendered
    # A prepare-derived cross-source summary is not the reporting authority for
    # one exact finding; the matching source binding and typed wake are.
    assert "持续研判五路安全数据并及时报告真实事件" not in rendered
    assert "## Audit Task Objective" in rendered
    assert "## Recent Messages" not in rendered
    assert "## Bound Tasks" not in rendered
    assert "## Guidance" not in rendered
    assert "## Agent Tree Snapshot" not in rendered
    assert "## Pending Wake Signals" not in rendered
    assert "## Recovery Snapshot" not in rendered
    assert "## Task Runtime State" not in rendered


def test_failed_audit_finding_delivery_does_not_consume_wake(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_finding",
            "root_task_id": "audit-1",
            "evidence_refs": ["audit://watch-1/candidate/1:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "af-1",
                "requires_llm_report": True,
                "delivery_evidence_refs": ["audit://watch-1/candidate/1:0"],
            },
            "now": 10.0,
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    attempts = []

    def pending_report(_kwargs):
        attempts.append(1)
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="",
            route_channel="internal",
            route_target="owner-a",
            created_at=20.0,
            delivery_status="failed",
            delivery_reason="audit_finding_report",
            wake_handled=False,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", pending_report)

    assert scheduler._run_wake_signal(signal, now=20.0) is None
    assert [item.wake_signal_id for item in store.wakes.pending()] == [signal.wake_signal_id]
    assert scheduler.tick(now=21.0) == []
    assert len(attempts) == 1
    assert scheduler.tick(now=50.0) == []
    assert len(attempts) == 2
    assert scheduler._wake_retry_after[signal.wake_signal_id] == 80.0


def test_completed_audit_keeps_unreceipted_typed_finding_until_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
            "status": "active",
        }
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_finding",
            "root_task_id": "audit-1",
            "source_agent_id": "source-worker-1",
            "evidence_refs": ["audit://watch-1/candidate/97:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "finding-97",
                "requires_llm_report": True,
                "delivery_evidence_refs": ["audit://watch-1/candidate/97:0"],
            },
            "now": 10.0,
        }
    )
    store.tasks.update_status({"task_id": "audit-1", "status": "completed", "now": 20.0})
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    attempts: list[str] = []

    def failed_delivery(kwargs):
        attempts.append("failed")
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="",
            route_channel="feishu",
            route_target="owner-a",
            created_at=21.0,
            delivery_status="failed",
            delivery_reason="audit_finding_report",
            wake_handled=False,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", failed_delivery)

    assert scheduler.tick(now=21.0) == []
    assert attempts == ["failed"]
    assert store.wakes.pending_one(signal.wake_signal_id) is not None

    def delivered(kwargs):
        attempts.append("sent")
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="已发送。",
            route_channel="feishu",
            route_target="owner-a",
            created_at=51.0,
            delivery_status="sent",
            delivery_reason="audit_finding_report",
            wake_handled=True,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", delivered)

    reports = scheduler.tick(now=51.0)
    assert [item.delivery_status for item in reports] == ["sent"]
    assert attempts == ["failed", "sent"]
    assert store.wakes.pending_one(signal.wake_signal_id) is None


def test_completed_root_retires_ordinary_late_child_wake(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-1", "goal": "旧任务"})
    store.tasks.update_status({"task_id": "task-1", "status": "completed", "now": 20.0})
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": "task-1",
            "source_agent_id": "child-1",
            "metadata": {"task_id": "child-1", "status": "DONE"},
            "now": 21.0,
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    monkeypatch.setattr(
        scheduler,
        "_run_claimed",
        lambda _kwargs: pytest.fail("stale child wake must not start a model turn"),
    )

    assert scheduler.tick(now=30.0) == []
    assert store.wakes.pending_one(signal.wake_signal_id) is None


def test_linked_observation_does_not_fork_while_delivery_wake_is_pending(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    observation, signal = store.wakes.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "audit_capacity_alert",
            "summary": "容量已超过阈值",
            "requires_main_agent": True,
            "root_task_id": "audit-1",
        },
        {
            "thread_id": thread.thread_id,
            "reason": "audit_capacity_alert",
            "root_task_id": "audit-1",
            "metadata": {
                "schema_version": "audit-capacity-event.v2",
                "audit_id": "audit-1",
                "capacity_state": "alert",
                "pending": 100,
            },
        },
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    attempts: list[str] = []

    def failed_delivery(kwargs):
        attempts.append(str(kwargs.get("reason") or ""))
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason=str(kwargs.get("reason") or ""),
            response="",
            route_channel="feishu",
            route_target="owner-a",
            created_at=20.0,
            delivery_status="failed",
            delivery_reason="channel_delivery_failed",
            wake_handled=False,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", failed_delivery)

    assert scheduler.tick(now=20.0) == []
    assert attempts == ["audit_capacity_alert"]
    assert store.wakes.pending_one(signal.wake_signal_id) is not None
    assert [
        item.observation_id for item in store.observations.unhandled_requiring_main(limit=10)
    ] == [observation.observation_id]


# LLM: 该 helper 只种结构化 runtime 状态，不创建 wake/observation；消费行为仍由测试正文验证。
# 函数用途: 为恢复阻塞回归创建一个 run/current attempt 都为 unknown 的旧主代理。
def _record_unknown_main_run(agent, *, task_id: str, thread_id: str):
    repo = agent.subagents.runtime_db
    run = repo.record_run_creation(
        owner_id="local/main",
        goal="旧任务",
        conversation_task_id=task_id,
        thread_id=thread_id,
        run_id="request-old",
        role="main",
    )
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET status = 'unknown' WHERE attempt_id = ?",
            (run["attempt_id"],),
        )
        conn.execute(
            "UPDATE agent_runs SET status = 'unknown' WHERE agent_run_id = ?",
            (run["agent_run_id"],),
        )
    return repo, run


def test_unknown_old_task_preserves_event_without_blocking_new_task_on_same_thread(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            conversation_unhandled_observation_limit=1,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "same-thread",
            "channel_user_id": "owner-a",
        }
    )
    old_task_id = "task-old-unknown"
    new_task_id = "task-new-runnable"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": old_task_id, "goal": "旧任务"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": new_task_id, "goal": "新任务"})
    repo, old_run = _record_unknown_main_run(
        agent,
        task_id=old_task_id,
        thread_id=thread.thread_id,
    )
    old_observation = store.observations.append(
        {
            "thread_id": thread.thread_id,
            "event_type": "subagent_runner_finished",
            "summary": "旧任务子代理已经结束。",
            "root_task_id": old_task_id,
            "requires_main_agent": True,
            "now": 1.0,
        }
    )
    new_observation = store.observations.append(
        {
            "thread_id": thread.thread_id,
            "event_type": "subagent_runner_finished",
            "summary": "新任务子代理已经结束。",
            "root_task_id": new_task_id,
            "requires_main_agent": True,
            "now": 2.0,
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    first_reports = scheduler.tick(now=20.0)

    assert len(first_reports) == 1
    assert first_reports[0].task_id == new_task_id
    assert len(backend.prompts) == 1
    assert "新任务子代理已经结束" in backend.provider_texts[0]
    assert [
        item.observation_id for item in store.observations.unhandled_requiring_main(limit=0)
    ] == [old_observation.observation_id]
    assert new_observation.observation_id != old_observation.observation_id

    recovered = repo.recover_attempt_unknown(
        old_run["attempt_id"],
        operator="human-checker",
        effect_disposition="confirmed_noop",
    )
    assert recovered["recovered"] is True
    second_reports = scheduler.tick(now=30.0)
    assert len(second_reports) == 1
    assert second_reports[0].task_id == old_task_id
    assert len(backend.prompts) == 2
    assert store.observations.unhandled_requiring_main(limit=0) == []


def test_completion_observation_fallback_uses_same_partial_delivery_policy(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    first = agent.subagents.create_run(
        goal="完成第一部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.create_run(
        goal="完成第二部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "分两部分完成", "now": 11.0}
    )
    store.observations.append(
        {
            "thread_id": thread.thread_id,
            "event_type": "subagent_runner_finished",
            "summary": "第一部分已完成。",
            "source_agent_id": first.id,
            "root_task_id": "task-root",
            "requires_main_agent": True,
            "metadata": {"task_id": first.id, "status": "DONE"},
            "now": 20.0,
        }
    )
    channels = FakeDeliveryService()
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels),
            "store": store,
        }
    )

    reports = scheduler.tick(now=21.0)

    assert len(reports) == 1
    assert reports[0].reason == "subagent_runner_finished"
    assert reports[0].delivery_status == "suppressed"
    assert reports[0].delivery_reason == "partial_subagent_success"
    assert channels.adapter("internal").sent_messages == []
    assert _public_background_messages(store, thread.thread_id) == []


def test_internal_wait_continuation_stays_out_of_chat_while_child_runs(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "suppressed"
    assert report.delivery_reason == "internal_scheduled_continuation"
    assert channels.adapter("internal").sent_messages == []
    assert _public_background_messages(store, thread.thread_id) == []


def test_internal_wait_completion_delivers_model_authored_final_reply(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "internal_scheduled_completion"
    assert channels.adapter("internal").sent_messages[0].content == report.response
    assert [row.content for row in _public_background_messages(store, thread.thread_id)] == [report.response]


def test_scheduled_turn_accepts_mid_turn_child_event_without_starting_second_main_run(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    backend = _MidTurnLifecycleBackend(
        store=store,
        thread_id=thread.thread_id,
        task_id="task-root",
    )
    agent.backend = backend
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=72.0)

    assert len(reports) == 1
    assert backend.calls == 2
    assert "RUNTIME_TASK_EVENTS" not in backend.prompts[0]
    assert backend.signal is not None
    assert backend.signal.wake_signal_id in backend.provider_texts[1]
    assert reports[0].response == "已接收子代理的新结果并继续整合。"
    assert store.wakes.pending() == []
    claim = store.claims.load(thread.thread_id)
    assert claim["status"] == "finished"


def test_mid_turn_child_event_stays_retryable_when_provider_fails_after_injection(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    backend = _MidTurnLifecycleBackend(
        store=store,
        thread_id=thread.thread_id,
        task_id="task-root",
        fail_after_injection=True,
    )
    agent.backend = backend
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    try:
        scheduler.tick(now=72.0)
    except RuntimeError as exc:
        assert "provider failed after runtime event injection" in str(exc)
    else:
        raise AssertionError("provider failure should leave the runtime event retryable")

    assert backend.signal is not None
    assert [item.wake_signal_id for item in store.wakes.pending()] == [
        backend.signal.wake_signal_id
    ]
    assert store.claims.load(thread.thread_id)["status"] == "failed"


def test_internal_continuation_delivers_natural_runtime_completion(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _NaturalCompletionBackend()
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "internal_scheduled_completion"
    messages = _public_background_messages(store, thread.thread_id)
    assert [row.content for row in messages] == ["任务全部完成。"]
    assert messages[0].created_at > 20.0


def test_done_child_wake_delivers_natural_final_response(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": child.id,
                "metadata": {"task_id": child.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "root_subagents_terminal"
    assert channels.adapter("internal").sent_messages[0].content == report.response
    assert [row.content for row in _public_background_messages(store, thread.thread_id)] == [report.response]


def test_done_child_wake_with_carried_successful_spawn_closes_root_task(
    tmp_path,
) -> None:
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )

    home = tmp_path / "home"
    task_root = home / "tasks" / "2026-08-25" / "task-root"
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=task_root / "work",
            tool="create_subagents",
            call_id="create-child-1",
            output="created child",
            ok=True,
            request_id="foreground-request",
            run_id="foreground-request",
            task_id="foreground-request",
            min_chars=0,
            parameters={"items": [{"goal": "完成实现", "role": "worker"}]},
            result_envelope={
                "tool_execution": {
                    "handler_executed": True,
                    "duration_ms": 25,
                },
                "tool_operation": {
                    "schema_version": "tool_operation.v1",
                    "operation_id": "operation-create-child-1",
                    "status": "succeeded",
                    "action": "execute",
                    "replayed": False,
                    "idempotency_scope": "turn",
                },
            },
        )
    )
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            my_agent_home=str(home),
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    agent.backend = _NaturalCompletionBackend()
    child = agent.subagents.create_run(
        goal="完成实现",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
        attributes={"conversation_request_id": "foreground-request"},
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-carried-operation",
            "channel_user_id": "user-1",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "goal": "由子代理完成实现后汇总",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    store.messages.append({
        "thread_id": thread.thread_id, "role": "user", "content": "由子代理完成实现后汇总",
        "metadata": {"conversation_request_id": "foreground-request"},
    })
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": child.id,
                "metadata": {
                    "task_id": child.id,
                    "status": "DONE",
                    "conversation_request_id": "foreground-request",
                },
            },
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "root_subagents_terminal"
    assert store.tasks.load("task-root").status == "completed"
    final_row = store.messages.recent(thread.thread_id, limit=1)[0]
    assert final_row.metadata["operation_verification"]["status"] == "succeeded"
    assert final_row.metadata["operation_verification"]["counts"]["succeeded"] == 1


def test_successful_result_deadline_is_not_extended_by_a_running_sibling(tmp_path) -> None:
    from agent_py_agent.agent.conversation.models import WakeSignal
    from agent_py_agent.agent.conversation.runtime import _successful_completion_waiting_for_batch

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    fast = agent.subagents.create_run(goal="较快部分", thought="", plan=[], parent_id="root", root_id="root")
    slow = agent.subagents.create_run(goal="较慢部分", thought="", plan=[], parent_id="root", root_id="root")
    agent.subagents.lifecycle.set_status(fast.id, "DONE")
    agent.subagents.lifecycle.set_status(slow.id, "RUNNING")
    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=agent), _config_limit=lambda _name: 5)
    signal = WakeSignal(
        wake_signal_id="fast-result", thread_id="thread", root_task_id="root",
        reason="subagent_runner_finished", source_agent_id=fast.id,
        created_at=100.0, metadata={"status": "DONE"},
    )

    assert _successful_completion_waiting_for_batch(scheduler, signal, 104.9)
    assert not _successful_completion_waiting_for_batch(scheduler, signal, 105.0)
    assert not _successful_completion_waiting_for_batch(scheduler, signal, 5000.0)
    assert agent.subagents.load(slow.id).status == "RUNNING"


def test_successful_sibling_completion_wakes_are_coalesced_before_one_llm_turn(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_completion_coalesce_seconds=5,
        ),
        tmp_path,
    )
    backend = _NaturalCompletionBackend()
    agent.backend = backend
    for goal in ("第一部分", "第二部分", "第三部分", "第四部分"):
        child = agent.subagents.create_run(
            goal=goal,
            thought="",
            plan=["执行"],
            parent_id="task-root",
            root_id="task-root",
        )
        agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "两路并行", "now": 11.0}
    )
    for index, created_at in enumerate((20.0, 21.0, 22.0, 23.0), start=1):
        store.wakes.raise_signal(
            {
                "thread_id": thread.thread_id,
                "reason": "subagent_runner_finished",
                "root_task_id": "task-root",
                "source_agent_id": f"child-{index}",
                "metadata": {
                    "task_id": f"child-{index}",
                    "status": "DONE",
                    "completion_schema_version": "subagent-completion.v1",
                    "completion_message": f"第{index}个子代理的最终结论：" + ("甲" * 500),
                    "final_report_ref": f"/tmp/child-{index}/final_report.md",
                },
                "now": created_at,
            }
        )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    assert scheduler.tick(now=23.0) == []
    assert backend.prompts == []
    assert len(store.wakes.pending()) == 4

    reports = scheduler.tick(now=26.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    for index in range(1, 5):
        assert f"第{index}个子代理的最终结论" in backend.provider_texts[0]
        assert f"/tmp/child-{index}/final_report.md" in backend.provider_texts[0]
    assert '"event_count": 4' in backend.provider_texts[0]
    assert '"events": [' in backend.provider_texts[0]
    assert store.wakes.pending() == []
    assert reports[0].delivery_status == "sent"


def test_scheduled_continuation_keeps_direct_child_results_after_wakes_are_consumed(
    tmp_path,
) -> None:
    """后续进度轮仍应拿到 exact child 结果，不能退回内部目录猜测。"""

    from agent_py_agent.agent.conversation.background_context import context_markdown
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            background_context_max_total_tokens=2200,
            background_context_max_string_chars=240,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-scheduled-child-results",
            "channel_user_id": "user-1",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "goal": "整合四名子代理的研究结论",
        }
    )
    completion_observation_ids: list[str] = []
    for index in range(1, 5):
        event = store.observations.append(
            {
                "thread_id": thread.thread_id,
                "event_type": "subagent_runner_finished",
                "summary": f"第 {index} 名子代理完成",
                "source_agent_id": f"child-{index}",
                "parent_agent_id": "task-root",
                "root_task_id": "task-root",
                "requires_main_agent": True,
                "metadata": {
                    "task_id": f"child-{index}",
                    "status": "DONE",
                    "completion_schema_version": "subagent-completion.v1",
                    "completion_message": f"第 {index} 份精确研究结论" + ("甲" * 600),
                    "final_report_ref": f"/workspace/child-{index}/final_report.md",
                    "runner_result_json": "private-runner-payload",
                    "service_window_incomplete": True,
                    "service_window_remaining_seconds": 700 + index,
                },
                "now": 20.0 + index,
            }
        )
        completion_observation_ids.append(event.observation_id)
    store.observations.mark_handled(completion_observation_ids, now=30.0)
    # 把完成事件挤出普通 Recent Observations 的尾部窗口，复现真实长任务中的后续定时轮。
    for index in range(30):
        store.observations.append(
            {
                "thread_id": thread.thread_id,
                "event_type": "progress_snapshot",
                "summary": "后续运行状态" + ("乙" * 300),
                "root_task_id": "task-root",
                "requires_main_agent": False,
                "now": 40.0 + index,
            }
        )
    store.observations.append(
        {
            "thread_id": thread.thread_id,
            "event_type": "subagent_runner_finished",
            "summary": "孙代理完成但不得越级",
            "source_agent_id": "grandchild-1",
            "parent_agent_id": "child-1",
            "root_task_id": "task-root",
            "requires_main_agent": True,
            "metadata": {
                "task_id": "grandchild-1",
                "status": "DONE",
                "completion_schema_version": "subagent-completion.v1",
                "completion_message": "孙代理私有结果",
                "final_report_ref": "/workspace/grandchild-1/final_report.md",
            },
            "now": 80.0,
        }
    )

    rendered = context_markdown(
        agent=agent,
        store=store,
        thread=store.threads.load(thread.thread_id),
        request=BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="task-root",
            reason="scheduled_progress_report",
            wake_signal={
                "wake_signal_id": "wake-progress",
                "thread_id": thread.thread_id,
                "root_task_id": "task-root",
                "reason": "scheduled_progress_report",
            },
        ),
    )

    assert "## Subagent Completion Inputs" in rendered
    completion_text = rendered.split("## Subagent Completion Inputs\n```json\n", 1)[1].split("\n```", 1)[0]
    completion_items = json.loads(completion_text)["items"]
    for index in range(1, 5):
        assert f'"task_id": "child-{index}"' in rendered
        assert f"/workspace/child-{index}/final_report.md" in rendered
        item = next(row for row in completion_items if row["task_id"] == f"child-{index}")
        assert item["status"] == "DONE"
        assert item["service_window_incomplete"] is True
        assert item["service_window_remaining_seconds"] == 700 + index
    assert "孙代理私有结果" not in rendered
    assert "/workspace/grandchild-1/final_report.md" not in rendered
    assert "private-runner-payload" not in rendered


def _seed_seven_completion_wakes(agent, store, thread_id: str) -> None:
    """写入七个终态 child 和七份带长正文/精确报告引用的完成信封。"""
    for index in range(1, 8):
        child = agent.subagents.create_run(
            goal=f"第{index}部分",
            thought="",
            plan=["执行"],
            parent_id="task-root",
            root_id="task-root",
        )
        agent.subagents.lifecycle.set_status(child.id, "DONE")
    for index in range(1, 8):
        store.wakes.raise_signal(
            {
                "thread_id": thread_id,
                "reason": "subagent_runner_finished",
                "root_task_id": "task-root",
                "source_agent_id": f"child-{index}",
                "metadata": {
                    "task_id": f"child-{index}",
                    "status": "DONE",
                    "completion_schema_version": "subagent-completion.v1",
                    "completion_message": f"第{index}个子代理的最终结论：" + ("甲" * 900),
                    "final_report_ref": f"/tmp/child-{index}/final_report.md",
                },
                "now": 20.0 + index,
            }
        )


def _seven_completion_mailbox_fixture(tmp_path):
    """建立七份长 completion 和一个 4k/5 条有界后台消费者。"""
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_completion_coalesce_seconds=0,
            background_pending_wake_prompt_limit=5,
            background_context_max_total_tokens=4096,
        ),
        tmp_path,
    )
    backend = _NaturalCompletionBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-seven-results",
            "channel_user_id": "user-1",
        }
    )
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-root", "goal": "七路并行"})
    _seed_seven_completion_wakes(agent, store, thread.thread_id)
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=channels,
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    return backend, channels, store, scheduler


def test_successful_completion_mailbox_drains_every_sibling_under_prompt_pressure(
    tmp_path,
) -> None:
    """长结果可以分批，但没读完前不收口，最终每个 child 引用都必须交付。"""
    backend, channels, store, scheduler = _seven_completion_mailbox_fixture(tmp_path)

    reports = scheduler.tick(now=40.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert reports[0].delivery_status == "suppressed"
    assert reports[0].delivery_reason == "subagent_completion_mailbox_pending"
    assert store.tasks.load("task-root").status == "active"
    assert 0 < len(store.wakes.pending()) < 7

    all_reports = list(reports)
    for tick in range(1, 8):
        if not store.wakes.pending():
            break
        all_reports.extend(scheduler.tick(now=40.0 + tick))

    rendered_prompts = "\n".join(backend.provider_texts)
    for index in range(1, 8):
        assert f"第{index}个子代理的最终结论" in rendered_prompts
        assert f"/tmp/child-{index}/final_report.md" in rendered_prompts
    assert store.wakes.pending() == []
    assert store.tasks.load("task-root").status == "completed"
    assert all_reports[-1].delivery_status == "sent"
    assert all_reports[-1].delivery_reason == "root_subagents_terminal"
    assert all(report.delivery_status == "suppressed" for report in all_reports[:-1])
    assert len(channels.adapter("internal").sent_messages) == 1


def test_completion_coalescing_acknowledges_only_the_selected_wake_snapshot(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-fresh-completion",
            "channel_user_id": "user-1",
        }
    )
    signals = []
    for index, created_at in enumerate((20.0, 21.0, 31.0), start=1):
        signals.append(
            store.wakes.raise_signal(
                {
                    "thread_id": thread.thread_id,
                    "reason": "subagent_runner_finished",
                    "root_task_id": "task-root",
                    "source_agent_id": f"child-{index}",
                    "metadata": {"task_id": f"child-{index}", "status": "DONE"},
                    "now": created_at,
                }
            )
        )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(agent=agent, store=store),
            "store": store,
        }
    )
    handled: set[str] = set()

    scheduler._mark_sibling_signals(
        signals[:2],
        signals[0],
        40.0,
        handled,
        sampled_at=30.0,
    )

    assert signals[1].wake_signal_id in handled
    assert store.wakes.pending_one(signals[1].wake_signal_id) is None
    assert signals[2].wake_signal_id not in handled
    assert store.wakes.pending_one(signals[2].wake_signal_id) is not None


def test_two_audit_findings_on_one_thread_share_one_receipted_model_turn(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-findings-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "goal": "ordinary-root-for-delivery-test",
            "status": "active",
        }
    )
    signals = []
    for index in (1, 2):
        signals.append(
            store.wakes.raise_signal(
                {
                    "thread_id": thread.thread_id,
                    "reason": "audit_finding",
                    "root_task_id": "task-root",
                    "evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                    "metadata": {
                        "schema_version": "audit-finding-event.v1",
                        "audit_id": "task-root",
                        "watch_id": "watch-1",
                        "finding_id": f"af-{index}",
                        "revision": 1,
                        "requires_llm_report": True,
                        "delivery_evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                    },
                }
            )
        )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    processed: list[dict[str, object]] = []

    def delivered(kwargs):
        wake = kwargs["wake_signal"]
        processed.append(wake.to_dict())
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="task-root",
            reason="audit_finding",
            response="sent",
            route_channel="internal",
            route_target="owner-a",
            created_at=time.time(),
            delivery_status="sent",
            delivery_reason="audit_finding_report",
            wake_handled=True,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", delivered)

    assert len(scheduler.tick()) == 1
    assert len(processed) == 1
    assert processed[0]["wake_signal_id"] == signals[0].wake_signal_id
    assert processed[0]["evidence_refs"] == [
        "audit://watch-1/candidate/1:1",
        "audit://watch-1/candidate/1:2",
    ]
    assert processed[0]["metadata"]["finding_count"] == 2
    assert processed[0]["metadata"]["report_scope"] == "incremental"
    assert processed[0]["metadata"]["delivery_evidence_refs"] == [
        "audit://watch-1/candidate/1:1",
        "audit://watch-1/candidate/1:2",
    ]
    assert processed[0]["metadata"]["batched_wake_signal_ids"] == [
        signal.wake_signal_id for signal in signals
    ]
    assert store.wakes.pending() == []


def test_reported_audit_wake_ignores_supplementary_evidence_for_delivery_identity(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.ingestion import harvester

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-receipt-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
        }
    )
    canonical = "audit://watch-1/candidate/1601:0"
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_finding",
            "root_task_id": "audit-1",
            "evidence_refs": [canonical, "SOAK-C-000001600"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "af-mixed-evidence",
                "requires_llm_report": True,
                "delivery_evidence_refs": [canonical],
            },
        }
    )
    checked: list[tuple[str, ...]] = []

    def already_sent(_owner_home, refs):
        checked.append(tuple(refs))
        return tuple(refs) == (canonical,)

    monkeypatch.setattr(harvester, "audit_source_refs_reported", already_sent)
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick() == []
    assert checked == [(canonical,)]
    assert store.wakes.pending() == []


def test_failed_audit_finding_batch_stays_pending_and_shares_retry_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-findings-failed-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
        }
    )
    signals = [
        store.wakes.raise_signal(
            {
                "thread_id": thread.thread_id,
                "reason": "audit_finding",
                "root_task_id": "audit-1",
                "evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "audit_id": "audit-1",
                    "run_epoch": 1,
                    "watch_id": "watch-1",
                    "finding_id": f"af-{index}",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                },
            }
        )
        for index in (1, 2)
    ]
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    attempts: list[list[str]] = []

    def failed(kwargs):
        wake = kwargs["wake_signal"]
        attempts.append(list(wake.evidence_refs))
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="",
            route_channel="internal",
            route_target="owner-a",
            created_at=time.time(),
            delivery_status="failed",
            delivery_reason="audit_finding_report",
            wake_handled=False,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", failed)
    current = time.time()

    assert scheduler.tick(now=current) == []
    assert attempts == [
        [
            "audit://watch-1/candidate/1:1",
            "audit://watch-1/candidate/1:2",
        ]
    ]
    assert [item.wake_signal_id for item in store.wakes.pending()] == [
        signal.wake_signal_id for signal in signals
    ]
    assert all(
        scheduler._wake_retry_after[signal.wake_signal_id] == current + 30.0 for signal in signals
    )

    assert scheduler.tick(now=current + 1.0) == []
    assert len(attempts) == 1


def test_audit_finding_batch_honors_existing_wake_projection_limit(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_pending_wake_prompt_limit=1,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-findings-bounded-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
        }
    )
    for index in (1, 2):
        store.wakes.raise_signal(
            {
                "thread_id": thread.thread_id,
                "reason": "audit_finding",
                "root_task_id": "audit-1",
                "evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "audit_id": "audit-1",
                    "run_epoch": 1,
                    "watch_id": "watch-1",
                    "finding_id": f"af-{index}",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                },
            }
        )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    processed: list[list[str]] = []

    def delivered(kwargs):
        wake = kwargs["wake_signal"]
        processed.append(list(wake.evidence_refs))
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="sent",
            route_channel="internal",
            route_target="owner-a",
            created_at=time.time(),
            delivery_status="sent",
            delivery_reason="audit_finding_report",
            wake_handled=True,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", delivered)

    assert len(scheduler.tick(now=time.time())) == 1
    assert processed == [["audit://watch-1/candidate/1:1"]]
    assert len(store.wakes.pending()) == 1


def test_failed_subagent_completion_wake_is_not_delayed_by_success_coalescing(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_completion_coalesce_seconds=30,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "失败立即处理", "now": 11.0}
    )
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": "task-root",
            "source_agent_id": "child-failed",
            "metadata": {"task_id": "child-failed", "status": "FAILED"},
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    reports = scheduler.tick(now=20.1)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert reports[0].delivery_reason == "subagent_non_success_terminal"


def test_audit_source_worker_lifecycle_is_supervised_without_owner_model_turn(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.common.audit_activation import (
        audit_source_worker_key,
    )

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-1"
    watch_id = "watch-1"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-worker-1",
            "metadata": {
                "task_id": "source-worker-1",
                "status": "DONE",
                "audit_source_worker": True,
                "audit_id": audit_id,
                "watch_id": watch_id,
                "worker_key": audit_source_worker_key(audit_id, watch_id),
            },
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    swept: list[str] = []
    completed: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "_pre_wake_capability_sweep",
        lambda reason, _signal: swept.append(reason),
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.task_promotion.complete_named_audit_task_if_settled",
        lambda _agent, task_id: completed.append(task_id) or True,
    )

    report = scheduler._run_wake_signal(signal, now=time.time())

    assert report is None
    assert swept == ["subagent_runner_finished"]
    assert completed == [audit_id]
    assert backend.prompts == []
    assert store.wakes.pending() == []
    assert store.messages.recent(thread.thread_id, limit=10) == []


def test_audit_source_worker_terminal_failure_is_supervisor_internal(tmp_path) -> None:
    from agent_py_agent.agent.common.audit_activation import audit_source_worker_key

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-failed",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-failed"
    watch_id = "watch-failed"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-worker-failed",
            "metadata": {
                "task_id": "source-worker-failed",
                "status": "FAILED",
                "failure_type": "unknown_error",
                "audit_source_worker": True,
                "audit_id": audit_id,
                "watch_id": watch_id,
                "worker_key": audit_source_worker_key(audit_id, watch_id),
            },
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=time.time())

    assert reports == []
    assert backend.prompts == []
    assert store.wakes.pending() == []


def test_pending_audit_source_binding_failure_is_supervisor_internal(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-pending-failed",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-pending-failed"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-binding-pending",
            "metadata": {
                "task_id": "source-binding-pending",
                "status": "BLOCKED",
                "failure_type": "status_blocked",
                "audit_source_worker": True,
                "audit_source_worker_phase": "binding_pending",
                "audit_id": audit_id,
                "source_id": "source-pending",
                "watch_id": "",
                "worker_key": "",
            },
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick(now=time.time()) == []
    assert backend.prompts == []
    assert store.wakes.pending() == []


def test_legacy_pending_audit_source_wake_uses_durable_task_identity() -> None:
    from agent_py_agent.agent.common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_SOURCE_BINDING_PENDING_ATTR,
    )
    from agent_py_agent.agent.conversation.runtime import (
        _is_internal_audit_source_worker_lifecycle_signal,
    )

    audit_id = "audit-legacy-pending"
    task = SimpleNamespace(
        attributes={
            AUDIT_ATTR: True,
            AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
        }
    )
    agent = SimpleNamespace(
        subagents=SimpleNamespace(
            load=lambda task_id: (
                task
                if task_id == "source-legacy-pending"
                else (_ for _ in ()).throw(KeyError(task_id))
            )
        )
    )
    signal = SimpleNamespace(
        reason="subagent_runner_finished",
        root_task_id=audit_id,
        source_agent_id="source-legacy-pending",
        metadata={
            "task_id": "source-legacy-pending",
            "status": "BLOCKED",
            "failure_type": "status_blocked",
            "audit_source_worker": True,
            "audit_id": audit_id,
            "watch_id": "",
            "worker_key": "",
        },
    )

    assert _is_internal_audit_source_worker_lifecycle_signal(signal, agent) is True


def test_audit_source_worker_provider_timeout_is_supervisor_internal(
    tmp_path,
) -> None:
    from agent_py_agent.agent.common.audit_activation import audit_source_worker_key
    from agent_py_agent.agent.subagents.models import FailureType

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-provider-timeout",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-provider-timeout"
    watch_id = "watch-provider-timeout"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-worker-timeout",
            "metadata": {
                "task_id": "source-worker-timeout",
                "status": "BLOCKED",
                "failure_type": FailureType.PROVIDER_TIMEOUT.value,
                "audit_source_worker": True,
                "audit_id": audit_id,
                "watch_id": watch_id,
                "worker_key": audit_source_worker_key(audit_id, watch_id),
            },
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick(now=time.time()) == []
    assert backend.prompts == []
    assert store.wakes.pending() == []


def test_legacy_per_source_capacity_wake_is_retired_without_model_turn(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "legacy-capacity",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-legacy-capacity"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_capacity_alert",
            "root_task_id": audit_id,
            "metadata": {
                "schema_version": "audit-capacity-event.v1",
                "audit_id": audit_id,
                "watch_id": "watch-legacy",
                "capacity_state": "alert",
            },
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick(now=time.time()) == []
    assert backend.prompts == []
    assert store.wakes.pending() == []


def test_aggregate_capacity_wake_runs_one_owner_model_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "aggregate-capacity",
            "channel_user_id": "open-id-capacity",
        }
    )
    audit_id = "audit-aggregate-capacity"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    agent.subagents.create_run(
        goal="持续处理一个来源",
        thought="",
        plan=["继续"],
        parent_id=audit_id,
        root_id=audit_id,
    )
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "OLD_FALSE_NO_BACKLOG: 当前没有积压。",
            "channel": "feishu",
        }
    )
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_capacity_alert",
            "root_task_id": audit_id,
            "metadata": {
                "schema_version": "audit-capacity-event.v2",
                "audit_id": audit_id,
                "run_epoch": 3,
                "capacity_state": "alert",
                "source_count": 10,
                "alert_source_count": 7,
                "pending": 8642,
                "oldest_pending_age_seconds": 123.0,
                "ingest_records_per_second": 50.0,
                "processing_throughput": {"records_per_second": 41.0},
                "processing_latency": {"p95_seconds": 80.0},
                "reasons": ["backlog_threshold"],
                "sources": [],
            },
        }
    )
    channels = FakeDeliveryService()
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=channels,
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=time.time())

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert "typed Audit capacity event" in backend.provider_texts[0]
    assert '"capacity_state": "alert"' in backend.provider_texts[0]
    assert '"pending": 8642' in backend.provider_texts[0]
    assert '"records_per_second": 41.0' in backend.provider_texts[0]
    assert "OLD_FALSE_NO_BACKLOG" not in backend.provider_texts[0]
    assert "## Recent Messages" not in backend.provider_texts[0]
    assert "## Agent Tree Snapshot" not in backend.provider_texts[0]
    assert reports[0].delivery_status == "sent"
    assert len(channels.adapter("feishu").sent_messages) == 1


def test_capacity_wake_channel_failure_stays_retryable_and_out_of_transcript(
    tmp_path,
) -> None:
    class _RejectedDelivery:
        def supports_proactive(self, channel: str) -> bool:
            return channel == "feishu"

        def deliver(self, context, envelope):
            del envelope
            return SimpleNamespace(
                delivery_status="rejected",
                channel=context.channel,
                evidence_refs=(),
                receipt_id="",
            )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "aggregate-capacity",
            "channel_user_id": "open-id-capacity",
        }
    )
    audit_id = "audit-aggregate-capacity"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_RejectedDelivery(),
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "reason": "audit_capacity_alert",
            "route_channel": "feishu",
            "route_target": "open-id-capacity",
            "wake_signal": {
                "wake_signal_id": "wake-capacity-rejected",
                "root_task_id": audit_id,
                "metadata": {
                    "schema_version": "audit-capacity-event.v2",
                    "audit_id": audit_id,
                    "capacity_state": "alert",
                    "pending": 8642,
                },
            },
        }
    )

    assert report.delivery_status == "rejected"
    assert report.wake_handled is False
    assert store.messages.recent(thread.thread_id) == []


def test_capacity_wake_retry_reuses_frozen_reply_without_second_model_turn(
    tmp_path,
) -> None:
    class _RejectedDelivery:
        def supports_proactive(self, channel: str) -> bool:
            return channel == "feishu"

        def deliver(self, context, envelope):
            del envelope
            return SimpleNamespace(
                delivery_status="rejected",
                channel=context.channel,
                evidence_refs=(),
                receipt_id="",
            )

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "aggregate-capacity",
            "channel_user_id": "open-id-capacity",
            "now": 1.0,
        }
    )
    audit_id = "audit-aggregate-capacity"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
            "now": 2.0,
        }
    )
    agent.subagents.create_run(
        goal="持续处理一个来源",
        thought="",
        plan=["继续"],
        parent_id=audit_id,
        root_id=audit_id,
    )
    observation, signal = store.wakes.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "audit_capacity_alert",
            "summary": "容量已超过阈值",
            "requires_main_agent": True,
            "root_task_id": audit_id,
            "now": 3.0,
        },
        {
            "thread_id": thread.thread_id,
            "reason": "audit_capacity_alert",
            "root_task_id": audit_id,
            "metadata": {
                "schema_version": "audit-capacity-event.v2",
                "audit_id": audit_id,
                "capacity_state": "alert",
                "pending": 8642,
            },
            "now": 3.0,
        },
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=_RejectedDelivery(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick(now=20.0) == []
    assert len(backend.prompts) == 1
    cached = store.wakes.pending_one(signal.wake_signal_id)
    assert cached is not None
    assert cached.metadata["owner_delivery"]["schema_version"] == ("wake-owner-delivery.v2")
    assert scheduler.tick(now=51.0) == []
    assert len(backend.prompts) == 1
    assert store.wakes.pending_one(signal.wake_signal_id) is not None
    assert [
        item.observation_id for item in store.observations.unhandled_requiring_main(limit=10)
    ] == [observation.observation_id]
    assert store.messages.recent(thread.thread_id) == []


def test_audit_source_worker_quota_wakes_owner_model_and_is_delivered(tmp_path) -> None:
    from agent_py_agent.agent.common.audit_activation import audit_source_worker_key
    from agent_py_agent.agent.subagents.models import FailureType

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-id-1",
        }
    )
    audit_id = "audit-quota"
    watch_id = "watch-quota"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-worker-1",
            "metadata": {
                "task_id": "source-worker-1",
                "status": "BLOCKED",
                "failure_type": FailureType.PROVIDER_QUOTA_EXHAUSTED.value,
                "audit_source_worker": True,
                "audit_id": audit_id,
                "watch_id": watch_id,
                "worker_key": audit_source_worker_key(audit_id, watch_id),
            },
        }
    )
    channels = FakeDeliveryService()
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=channels,
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=time.time())

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert "exhausted its usable account or plan quota" in backend.prompts[0]
    assert reports[0].delivery_status == "sent"
    assert len(channels.adapter("feishu").sent_messages) == 1


def test_background_internal_status_is_not_saved_as_ordinary_chat(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _InternalStatusBackend()
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-id-1",
            "now": 10.0,
        }
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "scheduled_progress_report",
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 20.0,
        }
    )

    assert report.response == ""
    assert report.delivery_status == "suppressed"
    assert _public_background_messages(store, thread.thread_id, limit=1) == []
    assert channels.adapter("feishu").sent_messages == []


def test_scheduler_records_bad_progress_policy_without_blocking_due_policy(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 13.0,
        }
    )
    bad_path = store.storage.policies_dir / "broken.json"
    bad_path.write_text("[]", encoding="utf-8")

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert scheduler.last_progress_policy_load_errors
    assert (
        scheduler.last_progress_policy_load_errors[0]["context"]
        == "conversation.progress_policy.read"
    )
    assert scheduler.last_progress_policy_load_errors[0]["policy_id"] == "broken"
    assert channels.adapter("internal").sent_messages


def test_scheduler_retires_stale_missed_progress_policy_without_model_call(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "陈年提醒退休不复活",
            "now": 11.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=12.0 + 7200 + 61)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["policy_id"] == policy.policy_id
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "stale_missed_interval"
    # 早已超出 catchup 宽限(>2h)的 stale 策略应被退休(enabled=False),不再续命。
    # 旧行为 mark_progress_reported 把 next_due 重置成 now+interval,下个间隔又变 runnable 发 LLM
    # 进度汇报,无限 churn 占满 gateway worker。退休=从 due 扫描里彻底消失。
    retired = store.progress.load(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_scheduler_retires_ordinary_resume_when_exact_task_link_is_missing(tmp_path) -> None:
    """普通续跑 policy 不能在任务链接已清理后继续唤醒旧线程。"""

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-orphan-resume",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "gwreq-finished-and-removed",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-orphan-resume",
            "now": 11.0,
            "metadata": {
                "kind": "ordinary_task_resume",
                "tool": "task_round_resume",
                "resume_used": 1,
                "resume_limit": 3,
            },
        }
    )

    reports = scheduler.tick(now=71.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed == [
        {
            "policy_id": policy.policy_id,
            "thread_id": thread.thread_id,
            "task_id": "gwreq-finished-and-removed",
            "reason": "removed_ordinary_task_resume_policy",
        }
    ]
    retired = store.progress.load(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_only_explicit_goal_progress_keeps_background_continuation_chain(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.conversation.runtime import (
        _ensure_goal_progress_wake_chain,
        ledger_open_progress_item_count,
    )
    from agent_py_agent.agent.task_progress import write_task_progress

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-plain-items",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-plain-items",
            "goal": "完成普通任务清单",
            "now": 11.0,
        }
    )
    write_task_progress(
        runtime_owner_root(agent),
        "task-plain-items",
        {
            "items": [
                {"id": "read", "title": "读源码", "status": "done"},
                {"id": "report", "title": "写报告", "status": "pending"},
            ]
        },
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": "task-plain-items",
            "source_agent_id": "child-1",
            "now": 12.0,
        }
    )

    assert ledger_open_progress_item_count(agent, "task-plain-items") == 1
    _ensure_goal_progress_wake_chain(scheduler, signal, now=13.0)

    assert store.progress.list(enabled_only=True) == []
    store.goals.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-plain-items",
            "objective": "完成显式持续目标清单",
            "now": 13.5,
        }
    )
    _ensure_goal_progress_wake_chain(scheduler, signal, now=14.0)

    assert store.progress.list(enabled_only=True) == []
    pending = [item for item in store.wakes.pending() if item.reason == "thread_goal_continue"]
    assert len(pending) == 1 and pending[0].root_task_id == "task-plain-items"
    write_task_progress(
        runtime_owner_root(agent),
        "task-plain-items",
        {"items": [{"id": "report", "status": "done"}]},
    )
    assert ledger_open_progress_item_count(agent, "task-plain-items") == 0
    store.wakes.mark_handled(pending[0].wake_signal_id)
    _ensure_goal_progress_wake_chain(scheduler, signal, now=15.0)
    pending = [item for item in store.wakes.pending() if item.reason == "thread_goal_continue"]
    assert len(pending) == 1  # Todo 已结束不等于 Goal 已完成。


def test_scheduler_renews_stale_policy_while_coverage_open(tmp_path) -> None:
    """g8 问题B·stale 不杀活任务:任务清单还有未闭环项时,错过追赶窗(唤醒轮长期领不到
    claim/网关中断)只把排期推进到下一 interval 继续追,不许永久退休——账没对完唤醒链不许死。
    无清单的 stale(上一测试)仍照旧退休,churn 防护不变。"""
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.task_progress import write_task_progress

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "活任务的续推提醒",
            "now": 11.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    write_task_progress(
        runtime_owner_root(agent),
        "task-1",
        {"coverage": {"targets": [{"id": "req-01", "title": "模块1", "status": "pending"}]}},
    )
    stale_now = 12.0 + 7200 + 61

    reports = scheduler.tick(now=stale_now)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "stale_missed_interval"
    renewed = store.progress.load(policy.policy_id)
    assert renewed is not None and renewed.enabled is True, "清单未闭环的 stale 提醒只续命不退休"
    assert renewed.next_due_at > stale_now, "排期推进到下一 interval,下轮照常追"


@pytest.mark.parametrize("terminal_status", ["DONE", "completed", "superseded"])
def test_scheduler_retires_terminal_task_progress_policy_without_model_call(
    tmp_path, terminal_status
) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "终态任务退休watch",
            "now": 11.0,
        }
    )
    store.tasks.update_status({"task_id": "task-1", "status": terminal_status, "now": 70.0})
    # 模拟升级前残留账本或终态提交后的极窄竞态：调度器仍须结构化兜底退休。
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    reports = scheduler.tick(now=100.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["policy_id"] == policy.policy_id
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "terminal_task_link"
    # 被观察任务已终态时，watch 策略应退休(enabled=False),不再每个间隔唤醒后台主代理发
    # LLM 进度汇报(churn 根因)。这里 now=100 未到 stale 窗口,确保抑制原因是终态而非陈旧。
    retired = store.progress.load(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_terminal_child_watch_policy_is_not_revived_by_owner_backlog(tmp_path) -> None:
    # Child-bound policy belongs to the removed fixed watch route. Pending input
    # is now recovered by an exact root-task policy, so a terminal child link
    # must not be revived merely because the owner has unrelated backlog.
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _runnable_due_policies
    from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state, state_dir

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "conv:run-w",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "run-w", "goal": "盯守", "now": 11.0}
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "run-w",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "wait",
                "watch_run_id": "run-w",
            },
            "now": 12.0,
        }
    )
    store.tasks.update_status({"task_id": "run-w", "status": "DONE", "now": 70.0})
    owner_home = tmp_path / "owner"
    lane = new_state(owner_home, "http://127.0.0.1:9/pull", {"watch_window_seconds": 600})
    lane.opened_at = time.time() - 900.0  # 窗口已走完
    lane.totals["spool_candidates"] = 7  # 已抬 7 条、无人 ack = 未清账
    persist_state(lane)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home)))

    runnable, suppressed = _runnable_due_policies(store, [policy], now=time.time(), agent=agent)
    assert runnable == []
    assert [reason for _p, reason in suppressed] == ["terminal_task_link"]

    # Clearing the backlog does not change the terminal-link decision.
    sidecar = state_dir(owner_home) / f"{lane.watch_id}.read.json"
    sidecar.write_text(
        json.dumps(
            {
                "read_seq": 9,
                "candidates_consumed": 7,
                "candidates_acked": 7,
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    runnable2, suppressed2 = _runnable_due_policies(store, [policy], now=time.time(), agent=agent)
    assert runnable2 == []
    assert [reason for _p, reason in suppressed2] == ["terminal_task_link"]


def test_scheduler_retires_legacy_child_bound_watch_backstop(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(goal="判读一路数据")
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-child-watch",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "goal": "判读一路数据",
            "now": 11.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "watch_backlog_backstop",
                "watch_run_id": child.id,
            },
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=100.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed == [
        {
            "policy_id": policy.policy_id,
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "reason": "child_watch_backstop_policy",
        }
    ]
    retired = store.progress.load(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_scheduler_retires_legacy_audit_root_poll_without_model_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-audit-root",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "audit-one",
            "cancellation_scope": "detached",
            "now": 11.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "named_work_progress",
                "tool": "audit_durable_backstop",
                "scope": "exact_named_task",
            },
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=100.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed == [
        {
            "policy_id": policy.policy_id,
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "reason": "audit_root_poll_policy",
        }
    ]
    retired = store.progress.load(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_scheduler_retires_running_durable_audit_root_wait_without_model_turn(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-audit-root-wait",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-running",
            "goal": "opaque objective",
            "status": "active",
            "work_kind": "audit",
            "work_name": "audit-one",
            "duration_seconds": 600,
            "expires_at": 611.0,
            "cancellation_scope": "detached",
            "effective_source_bindings": [{"source_id": "source-1", "profile_ref": "profile-1"}],
            "run_epoch": 1,
            "now": 11.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-running",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "wait",
                "watch_run_id": "audit-running",
            },
            "now": 12.0,
        }
    )

    assert scheduler.tick(now=100.0) == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed == [
        {
            "policy_id": policy.policy_id,
            "thread_id": thread.thread_id,
            "task_id": "audit-running",
            "reason": "durable_audit_root_policy",
        }
    ]
    retired = store.progress.load(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_due_policy_backs_off_on_no_progress_rounds_and_recovers(tmp_path) -> None:
    # §6-B4 退避钉子:唤醒轮【零成功物质变更】(卡死空转,真机=BLOCKED 子代理让主代理每分钟
    # 醒来空转解阻、饿死并发建站用户)→ 间隔按 2^streak 拉长、封顶 8×,让出调度资源但永不
    # 停机;一有成功写入/调度立即归零复原。只读成功不算推进，判据不看模型文本。
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 0.0,
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 0.0,
        }
    )

    class _FakeRuntime:
        agent = None

        def __init__(self) -> None:
            self.tool_success_count = 0
            self.material_progress_count = 0

        def run_once(self, params: dict) -> BackgroundMainAgentReport:
            return BackgroundMainAgentReport(
                thread_id=str(params.get("thread_id") or ""),
                task_id=str(params.get("task_id") or ""),
                reason=str(params.get("reason") or ""),
                response="轮次完成",
                route_channel="internal",
                route_target="thread-1",
                created_at=float(params.get("now") or 0.0),
                tool_call_count=2,
                tool_success_count=self.tool_success_count,
                material_progress_count=self.material_progress_count,
            )

    runtime = _FakeRuntime()
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    # 第 1 轮无进展:streak=1 → 间隔 60 → 120
    scheduler.tick(now=61.0)
    after_first = store.progress.load(policy.policy_id)
    assert after_first.metadata["no_progress_streak"] == 1
    assert after_first.next_due_at == 61.0 + 120

    # 第 2 轮无进展:streak=2 → ×4
    scheduler.tick(now=after_first.next_due_at + 1)
    after_second = store.progress.load(policy.policy_id)
    assert after_second.metadata["no_progress_streak"] == 2
    assert after_second.next_due_at == after_first.next_due_at + 1 + 240

    # 连续无进展只封顶不停机:streak 再涨,倍数封在 8×
    scheduler.tick(now=after_second.next_due_at + 1)
    scheduler.tick(now=store.progress.load(policy.policy_id).next_due_at + 1)
    capped = store.progress.load(policy.policy_id)
    assert capped.metadata["no_progress_streak"] == 4
    assert capped.next_due_at == capped.last_report_at + 480  # 60 × 8 封顶
    assert capped.enabled is True  # 退避≠退休

    # 只有只读成功仍要退避；不能靠重复 read/list 冒充推进。
    runtime.tool_success_count = 1
    scheduler.tick(now=capped.next_due_at + 1)
    readonly = store.progress.load(policy.policy_id)
    assert readonly.metadata["no_progress_streak"] == 5

    # 有成功物质变更 → streak 归零、间隔复原
    runtime.material_progress_count = 1
    scheduler.tick(now=readonly.next_due_at + 1)
    recovered = store.progress.load(policy.policy_id)
    assert recovered.metadata["no_progress_streak"] == 0
    assert recovered.next_due_at == recovered.last_report_at + 60


def test_scheduler_runs_one_duplicate_progress_policy_per_target(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "重复提醒只跑一次",
            "now": 11.0,
        }
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 13.0,
        }
    )

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert [item["reason"] for item in scheduler.last_progress_policy_suppressed] == [
        "duplicate_policy"
    ]


def test_urgent_wake_uses_full_background_tool_profile(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "紧急事件",
            "now": 10.0,
        }
    )

    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "urgent_wake_signal",
            "wake_signal": {
                "wake_signal_id": "wake-1",
                "thread_id": thread.thread_id,
                "urgency": "urgent",
                "summary": "需要主代理马上处理。",
            },
            "now": 20.0,
        }
    )
    assert "create_subagents" in backend.tool_names[0]
    assert "dispatch_subagents" not in backend.tool_names[0]


def test_background_runtime_uses_configured_allowed_tools(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            background_main_agent_allowed_tools=["inspect_agent_tree", "send_guidance"],
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "长期后台任务",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "只读看树并提醒", "now": 12.0}
    )
    store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 13.0,
        }
    )

    scheduler.tick(now=73.0)
    assert "inspect_agent_tree" not in backend.tool_names[0]
    assert "send_guidance" in backend.tool_names[0]
    assert "dispatch_subagents" not in backend.tool_names[0]
    assert "create_subagents" not in backend.tool_names[0]


def test_background_runtime_applies_owner_disabled_tools(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.owner_policy = type(
        "OwnerPolicy", (), {"disabled_tools": ("create_subagents", "dispatch_subagents")}
    )()
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "紧急事件",
            "now": 10.0,
        }
    )

    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "urgent_wake_signal",
            "wake_signal": {"urgency": "urgent", "summary": "需要处理。"},
            "now": 20.0,
        }
    )
    assert "create_subagents" not in backend.tool_names[0]
    assert "dispatch_subagents" not in backend.tool_names[0]
    assert "removed_tools" in backend.provider_texts[0]


def test_background_runtime_applies_wake_policy_snapshot(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "策略快照",
            "now": 10.0,
        }
    )

    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "urgent_wake_signal",
            "wake_signal": {
                "urgency": "urgent",
                "summary": "只允许观察。",
                "policy_snapshot": {"allowed_tools": ["inspect_agent_tree"]},
            },
            "now": 20.0,
        }
    )
    # inspect_agent_tree 已从主链删除；快照不能把一个不存在的旧工具重新注入。
    assert backend.tool_names[0] == set()


def test_background_context_budget_truncates_large_messages(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "长上下文后台任务",
            "now": 10.0,
        }
    )
    long_message = "A" * 12000
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": long_message,
            "channel": "internal",
            "metadata": {"gateway_request_id": "task-1"},
            "now": 11.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "检查长上下文裁剪",
            "now": 12.0,
        }
    )
    store.progress.create(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 60, "now": 13.0}
    )

    reports = scheduler.tick(now=73.0)
    prompt = backend.provider_texts[0]

    assert len(reports) == 1
    # 后台工作片现在与前台共用同一份 canonical 历史投影：长正文按会话权威原样续接，
    # 不再被 8000 token 的有界摘要副本截断；同时 markdown 里不得再重复一份 Recent Messages。
    assert "A" * 2000 in prompt
    assert "## Recent Messages" not in prompt
    assert '"estimated": true' in prompt or "A" * 2000 in prompt


def test_scheduler_recovers_due_policy_after_process_restart(tmp_path) -> None:
    first_store = ConversationStore(tmp_path / "conversations")
    thread = first_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-id-1",
            "now": 100.0,
        }
    )
    first_store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "一小时后继续检查。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "task-1"},
            "now": 101.0,
        }
    )
    first_store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "重启后继续", "now": 102.0}
    )
    first_store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 3600,
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 103.0,
        }
    )

    restarted_agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path
    )
    backend = _CapturingBackend()
    restarted_agent.backend = backend
    restarted_store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(
        agent=restarted_agent,
        store=restarted_store,
        channels=channels,
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": restarted_store})

    reports = scheduler.tick(now=3703.0)

    assert len(reports) == 1
    assert "一小时后继续检查" in backend.provider_texts[0]
    assert channels.adapter("feishu").sent_messages[0].target == "chat-1"


def test_scheduler_skips_thread_with_active_background_claim(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "避免重复唤醒", "now": 2.0}
    )
    store.progress.create(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 60, "now": 3.0}
    )
    claim = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "reason": "already_running",
            "lease_seconds": 300,
            "now": 63.0,
        }
    )

    reports = scheduler.tick(now=64.0)

    assert claim is not None
    assert reports == []
    assert backend.prompts == []


def test_scheduler_renews_background_claim_while_runtime_is_still_running(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _SlowBackend(sleep_seconds=1.2)
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": runtime,
            "store": store,
            "claim_ttl_seconds": 1,
            "claim_heartbeat_interval_seconds": 0.2,
        }
    )
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "长后台运行要续租", "now": 2.0}
    )
    store.progress.create(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 1, "now": 3.0}
    )

    reports = scheduler.tick(now=4.0)

    assert reports[0].response == "后台主代理慢速检查完成。"
    claim_path = store.storage.background_claims_dir / f"{thread.thread_id}.json"
    claim = claim_path.read_text(encoding="utf-8")
    assert '"status": "finished"' in claim
    assert '"heartbeat_at": 4.0' not in claim


def test_scheduler_default_heartbeat_interval_stays_below_small_ttl(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())

    scheduler = BackgroundMainAgentScheduler(
        {"runtime": runtime, "store": store, "claim_ttl_seconds": 9}
    )

    assert scheduler.claim_heartbeat_interval_seconds == 3.0


def test_detached_task_claim_does_not_occupy_foreground_thread_lane(tmp_path) -> None:
    from agent_py_agent.agent.conversation.run_claim import (
        detached_task_claim_scope_id,
    )
    from agent_py_agent.agent.conversation.runtime import _background_claim_scope_id

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
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续检查来源",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全巡检",
            "duration_seconds": 600,
            "cancellation_scope": "detached",
            "now": 1.0,
        }
    )
    scope_id = _background_claim_scope_id(
        store,
        thread.thread_id,
        "audit-1",
    )
    assert scope_id == detached_task_claim_scope_id(thread.thread_id, "audit-1")

    task_claim = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "claim_scope_id": scope_id,
            "task_id": "audit-1",
            "reason": "audit_progress",
            "lease_seconds": 90,
            "now": 2.0,
        }
    )
    foreground_claim = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "foreground-1",
            "reason": "gateway_foreground_turn",
            "lease_seconds": 90,
            "now": 2.0,
        }
    )

    assert task_claim is not None
    assert foreground_claim is not None
    assert (
        store.claims.acquire(
            {
                "thread_id": thread.thread_id,
                "claim_scope_id": scope_id,
                "task_id": "audit-1",
                "reason": "duplicate_audit_turn",
                "lease_seconds": 90,
                "now": 3.0,
            }
        )
        is None
    )
    assert (
        store.claims.load(thread.thread_id)["claim_id"]
        == foreground_claim["claim_id"]
    )
    assert (
        store.claims.load(
            thread.thread_id,
            claim_scope_id=scope_id,
        )["claim_id"]
        == task_claim["claim_id"]
    )


def test_background_claim_immediately_takes_over_dead_same_host_owner(tmp_path) -> None:
    from agent_py_agent.agent.gateway_parts.daemon_metadata import process_host_id

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
    first = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 900, "now": 2.0}
    )
    assert first is not None
    claim_path = store.storage.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload["owner_process"] = {
        "host_id": process_host_id(),
        "pid": 999_999_999,
        "start_time": 1,
    }
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    second = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
    )

    assert second is not None
    assert second["claim_id"] != first["claim_id"]
    assert second["acquisition"]["reason"] == "owner_process_stale"
    assert second["previous_claim"]["expired"] is False


@pytest.mark.parametrize("expired", [False, True])
def test_foreground_claim_recovery_remains_with_exact_request(tmp_path, monkeypatch, expired):
    from agent_py_agent.agent.conversation import store_claims as store_module

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "owner", "channel": "chat",
        "channel_conversation_id": "session", "channel_user_id": "owner",
    })
    first = store.claims.acquire({
        "thread_id": thread.thread_id, "task_id": "gateway:request-1",
        "reason": "gateway_foreground_turn", "recover_same_task_only": True,
        "lease_seconds": 10, "now": 10,
    })
    monkeypatch.setattr(store_module, "process_identity_is_live", lambda _: False)
    current = 30 if expired else 11
    for task_id in ("request-1", "gateway:request-2"):
        assert store.claims.acquire({
            "thread_id": thread.thread_id, "task_id": task_id,
            "reason": "subagent_runner_finished", "now": current,
        }) is None
    assert store.claims.load(thread.thread_id)["claim_id"] == first["claim_id"]
    recovered = store.claims.acquire({
        "thread_id": thread.thread_id, "task_id": "gateway:request-1",
        "reason": "gateway_foreground_turn", "recover_same_task_only": True, "now": current,
    })
    assert recovered and recovered["claim_id"] != first["claim_id"]
    store.claims.finish({
        "thread_id": thread.thread_id, "claim_id": recovered["claim_id"], "status": "finished",
    })
    assert store.claims.acquire({
        "thread_id": thread.thread_id, "task_id": "request-1",
        "reason": "subagent_runner_finished", "now": current + 1,
    }) is not None


def test_background_claim_legacy_owner_waits_for_ttl_instead_of_guessing(tmp_path) -> None:
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
    first = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 90, "now": 2.0}
    )
    assert first is not None
    claim_path = store.storage.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload.pop("owner_process", None)
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        store.claims.acquire(
            {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
        )
        is None
    )


def test_background_claim_different_process_domain_waits_for_ttl(tmp_path) -> None:
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
    first = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 90, "now": 2.0}
    )
    assert first is not None
    claim_path = store.storage.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload["owner_process"] = {"host_id": "another-process-domain", "pid": 999_999_999}
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        store.claims.acquire(
            {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
        )
        is None
    )


def test_scheduler_marks_background_claim_failed_when_runtime_raises(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _FailingBackend()
    agent._current_tool = "web_fetch"
    agent._last_progress_summary = "正在核对来源"
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler(
        {"runtime": runtime, "store": store, "claim_ttl_seconds": 30}
    )
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "失败时留下可接手事实",
            "now": 2.0,
        }
    )
    store.progress.create(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 1, "now": 3.0}
    )

    try:
        scheduler.tick(now=4.0)
    except RuntimeError:
        pass

    claim = json.loads(
        (store.storage.background_claims_dir / f"{thread.thread_id}.json").read_text(encoding="utf-8")
    )
    assert claim["status"] == "failed"
    assert claim["task_id"] == "task-1"
    assert claim["last_error"]["type"] == "RuntimeError"
    assert "backend boom" in claim["last_error"]["message"]
    assert claim["takeover"]["allowed"] is True
    assert claim["takeover"]["reason"] == "runtime_failed"
    assert claim["phase"] == "failed"
    assert claim["last_runtime_facts"]["current_tool"] == "web_fetch"
    assert claim["last_runtime_facts"]["last_progress_summary"] == "正在核对来源"
    assert "tree_status_buckets" in claim["last_runtime_facts"]


def test_background_prompt_includes_recovery_snapshot_for_previous_failed_claim(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "接手时先对账", "now": 2.0}
    )
    failed = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "previous_run", "lease_seconds": 10, "now": 3.0}
    )
    assert failed is not None
    store.claims.finish(
        {
            "thread_id": thread.thread_id,
            "claim_id": failed["claim_id"],
            "status": "failed",
            "task_id": "task-1",
            "error": {"type": "RuntimeError", "message": "previous run crashed"},
            "now": 4.0,
        }
    )
    store.progress.create(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 1, "now": 5.0}
    )

    scheduler.tick(now=7.0)
    prompt = backend.provider_texts[0]

    assert "Recovery Snapshot" in prompt
    assert '"previous_claim_status": "failed"' in prompt
    assert (
        '"takeover_advice": "接手前先核对 claim、任务树和产物登记；不要把模型文本里的完成声明当成事实。"'
        in prompt
    )


def test_background_claim_unknown_finish_status_is_explicit_protocol_error(tmp_path) -> None:
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
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "unknown_status", "lease_seconds": 10, "now": 2.0}
    )
    assert claim is not None

    finished = store.claims.finish(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "status": "succeeded",
            "now": 3.0,
        }
    )

    assert finished is not None
    assert finished["status"] == "invalid_status"
    assert finished["takeover"] == {"allowed": True, "reason": "runtime_invalid_status"}
    assert finished["last_error"]["type"] == "InvalidBackgroundClaimStatus"


def test_cancelled_background_claim_is_not_recovery_takeover_candidate(tmp_path) -> None:
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
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "user_work", "lease_seconds": 10, "now": 2.0}
    )
    assert claim is not None

    finished = store.claims.finish(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "status": "cancelled",
            "now": 3.0,
        }
    )

    assert finished is not None
    assert finished["takeover"] == {"allowed": False, "reason": "user_interrupted"}


# ── 后台 claim 心跳:线程缺失不得裸崩 daemon 线程(修多 owner ticking 下 KeyError 崩心跳) ──


def test_renew_background_run_claim_present_thread_still_renews(tmp_path) -> None:
    """行为保持:线程在时续租照常成功、写入新的 heartbeat_at/expires_at。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "u1",
            "channel": "internal",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
            "now": 1.0,
        }
    )
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "wake_signal", "lease_seconds": 30, "now": 2.0}
    )
    assert claim is not None

    renewed = store.claims.renew(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "lease_seconds": 30,
            "now": 5.0,
        }
    )

    assert renewed is not None
    assert renewed["heartbeat_at"] == 5.0
    assert renewed["expires_at"] == 35.0


def test_renew_background_run_claim_missing_thread_returns_none_not_keyerror(tmp_path) -> None:
    """根因修:线程文件在长跑中消失(边缘/竞态)时,续租返回 None 让心跳优雅停机,绝不抛 KeyError。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "u1",
            "channel": "internal",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
            "now": 1.0,
        }
    )
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "wake_signal", "lease_seconds": 30, "now": 2.0}
    )
    assert claim is not None
    # 模拟真机现象:claim 成功后线程文件不再可读(store 根竞态/外部清理/长跑中消失)。
    (store.storage.threads_dir / f"{thread.thread_id}.json").unlink()

    renewed = store.claims.renew(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "lease_seconds": 30,
            "now": 5.0,
        }
    )

    assert renewed is None  # 修前:此处抛 KeyError('unknown conversation thread') 崩心跳线程


def test_finish_background_run_missing_thread_finalizes_claim_without_crash(tmp_path) -> None:
    """收尾在 _run_with_heartbeat 的 finally 跑:线程缺失也要能释放已存在的 claim 租约,绝不二次抛 KeyError。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "u1",
            "channel": "internal",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
            "now": 1.0,
        }
    )
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "reason": "wake_signal", "lease_seconds": 30, "now": 2.0}
    )
    assert claim is not None
    (store.storage.threads_dir / f"{thread.thread_id}.json").unlink()

    finished = store.claims.finish(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "status": "finished",
            "now": 5.0,
        }
    )

    assert finished is not None
    assert finished["status"] == "finished"


def test_finish_background_run_no_claim_file_returns_none_without_crash(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    assert (
        store.claims.finish(
            {"thread_id": "thread-never", "claim_id": "x", "status": "finished", "now": 1.0}
        )
        is None
    )


def test_background_claim_heartbeat_stops_gracefully_when_renew_raises() -> None:
    """防御纵深:renew 抛任何异常时,daemon 心跳线程记账后优雅停机,不把未捕获异常抛出杀线程。"""
    import threading as _threading

    from agent_py_agent.agent.conversation.run_claim import ConversationRunClaimHeartbeat

    class _RaisingClaims:
        def renew(self, request: dict):
            raise KeyError("unknown conversation thread: thread-boom")

    heartbeat = ConversationRunClaimHeartbeat(
        {
            "store": SimpleNamespace(claims=_RaisingClaims()),
            "thread_id": "thread-boom",
            "claim_id": "c1",
            "lease_seconds": 1,
            "interval_seconds": 0.05,
        }
    )
    uncaught: list[type] = []
    previous_hook = _threading.excepthook
    _threading.excepthook = lambda args: uncaught.append(args.exc_type)
    try:
        heartbeat.start()
        time.sleep(0.3)
        heartbeat.join(timeout=2.0)
    finally:
        _threading.excepthook = previous_hook

    assert not heartbeat.is_alive()  # 线程已优雅退出
    assert uncaught == []  # 没有未捕获异常杀线程(修前:KeyError 裸崩 "Exception in thread")


def test_policy_failure_backoff_is_deterministic_exponential_and_bounded() -> None:
    """失败退避纯函数:5min×2^(n-1) 上限 1h,抖动 0.90~1.10 按 policy_id 确定性派生。

    同一 policy 每次失败同值(可复现、可精确断言);不同 policy 抖动错峰(防齐醒)。
    """
    from agent_py_agent.agent.conversation.runtime import _policy_failure_backoff

    def base_seconds(n: int) -> float:
        return min(300 * (2 ** (n - 1)), 3600)

    def ratio(failures: int, policy_id: str) -> float:
        return _policy_failure_backoff(failures, policy_id) / base_seconds(failures)

    assert _policy_failure_backoff(1, "p-a") == _policy_failure_backoff(1, "p-a")
    for n in (1, 2, 3, 4, 5, 8, 9, 20):
        assert base_seconds(n) <= 3600  # 封顶 1h,永不超
        for pid in ("p-a", "p-b", "p-c"):
            assert 0.9 <= ratio(n, pid) <= 1.1  # 抖动区间
    assert base_seconds(1) == 300
    assert base_seconds(2) == 600
    assert base_seconds(4) == 2400
    assert base_seconds(5) == 3600
    assert base_seconds(20) == 3600


def test_failed_policy_run_records_backoff_and_retires_after_three(tmp_path) -> None:
    """问题6:失败 run 后 policy 记账(退避顺延),连续 3 次失败退休,绝不无限重试。

    修前:失败异常被 _consume_with_supply_guard 吸收 → policy.next_due_at 不动 →
    下个 tick 又 due = 无限重试。修后:失败落账 failure_count/last_failure_at,
    next_due_at 退避顺延;第 3 次失败 → enabled=False 退休,离开 due 扫描等用户。
    """
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        _policy_failure_backoff,
    )

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=False,
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    agent.backend = _FailingBackend()
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-fail-policy",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    task_root = tmp_path / "home" / "tasks" / "task-fail-policy"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-fail-policy",
            "goal": "失败续跑记账",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-fail-policy",
            "interval_seconds": 30,
            "route_channel": "internal",
            "route_target": thread.thread_id,
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    # 第 1、2 次失败:记账 + 退避顺延,policy 仍 enabled。
    for attempt, tick in ((1, 30.0), (2, 40.0)):
        with pytest.raises(RuntimeError, match="backend boom"):
            scheduler._run_due_policy(policy, now=tick)
        after = store.progress.load(policy.policy_id)
        assert after is not None and after.enabled is True
        assert after.metadata["failure_count"] == attempt
        assert after.metadata["last_failure_at"] == tick
        expected_backoff = _policy_failure_backoff(attempt, policy.policy_id)
        assert after.metadata["last_backoff_seconds"] == expected_backoff
        assert after.next_due_at == tick + expected_backoff
        assert after.next_due_at > tick + 30  # 比原 interval 退避更长(1 次=5min 基数)

    # 第 3 次失败:退休(enabled=False),离开 due 扫描,账目保留供复盘。
    with pytest.raises(RuntimeError, match="backend boom"):
        scheduler._run_due_policy(policy, now=50.0)
    retired = store.progress.load(policy.policy_id)
    assert retired is not None
    assert retired.enabled is False
    assert retired.metadata["failure_count"] == 3
    assert retired.metadata["retired_at"] == 50.0
    assert retired.policy_id not in {p.policy_id for p in store.progress.due(now=60.0)}


def test_successful_policy_run_resets_failure_accounting(tmp_path) -> None:
    """问题6成功半边:policy run 成功 → failure_count 清零，终态任务立即退休策略。

    先造 1 次失败账,再换能成功的 backend 跑一轮 → metadata.failure_count==0,
    同时由任务终态提交点停用 policy，不等 scheduler 下一轮扫描。
    """
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", enable_tools=False, my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-reset-policy",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    task_root = tmp_path / "home" / "tasks" / "task-reset-policy"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-reset-policy",
            "goal": "成功清零失败账",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-reset-policy",
            "interval_seconds": 30,
            "route_channel": "internal",
            "route_target": thread.thread_id,
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    agent.backend = _FailingBackend()
    with pytest.raises(RuntimeError, match="backend boom"):
        scheduler._run_due_policy(policy, now=30.0)
    assert store.progress.load(policy.policy_id).metadata["failure_count"] == 1

    agent.backend = _CapturingBackend()
    report = scheduler._run_due_policy(policy, now=40.0)
    assert report is not None  # run 成功
    after = store.progress.load(policy.policy_id)
    assert after.metadata["failure_count"] == 0  # 失败账清零复原
    assert after.enabled is False
    # 排期只受既有「无进展退避」(echo backend 无工具调用 → streak=1 → interval×2)
    # 影响，失败账已归零不带退避；任务已经终态，因此该时间只作历史审计。
    assert after.metadata["no_progress_streak"] == 1
    assert after.next_due_at == 40.0 + 30 * 2


@pytest.mark.parametrize("error", [ProviderUsageLimitError("限流"), ModelNotConfiguredError(), ModelProfileError("模型引用已撤销")])
def test_supply_or_configuration_failure_does_not_retire_policy(tmp_path, monkeypatch, error) -> None:
    """供应等待与本地模型依赖均不累计策略退休次数；真实执行错误仍由相邻测试覆盖。"""
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", enable_tools=False, my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-supply-fail",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    task_root = tmp_path / "home" / "tasks" / "task-supply-fail"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-supply-fail",
            "goal": "429 不算任务失败",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    policy = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-supply-fail",
            "interval_seconds": 30,
            "route_channel": "internal",
            "route_target": thread.thread_id,
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    # 直接让执行片抛出 typed 异常，验证原 claim 收口不会误退休策略。
    def quota_boom(_request):
        raise error

    monkeypatch.setattr(runtime, "run_once", quota_boom)
    with pytest.raises(type(error)):
        scheduler._run_due_policy(policy, now=30.0)

    after = store.progress.load(policy.policy_id)
    assert after is not None
    assert after.enabled is True  # 不退休
    assert after.metadata.get("failure_count") in (None, 0)  # 不记失败账
    assert after.metadata == {}  # 连失败账字段都没写:供应错误完全不碰 policy
    assert after.next_due_at == 50.0  # 初始排期(20+30)原样,无失败退避
