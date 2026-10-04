"""首轮选包组合合同：真实任务库、安装包与原 reader，只有模型传输使用 fake。"""
from __future__ import annotations

import json
from concurrent.futures import CancelledError
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop_service
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import (
    RunCloseoutFacts,
    _bind_main_agent_authority,
    _settle_main_agent_run_status,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.gateway_helpers import _emit_provider_attempt
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn
from agent_py_agent.agent.capability import package_provider
from agent_py_agent.agent.capability.package_selection_runtime import (
    prepare_capability_package_selection,
)
from agent_py_agent.agent.common.cancellation import CancellationToken, ToolCancelled
from agent_py_agent.agent.conversation.capability_selection_state import (
    CAPABILITY_SELECTION_KEY,
    TaskCapabilitySelection,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_capability_package_task_refs import _agent


# LLM: 只替换 provider 传输；不伪造任务、包授权、pins 或主循环 IR，回调可在响应前改变真实执行状态。
# 类用途: 记录结构化选择和主请求，模拟真实响应与取消竞态而不使用网络。
class _Backend:
    name = "anthropic_compatible"
    model_name = "fake-selection-model"
    context_window_tokens = 200000
    max_tokens = 1024

    # LLM: 只保存传输替身的输入与控制回调，不改变产品快照或持久状态。
    # 函数用途: 为一个用例独立记录模型调用并配置返回选择。
    def __init__(self, *, selected=("capability:story-a",)):
        self.selected = selected
        self.calls = []
        self.before_response = None
        self.failure = None

    # LLM: 走真实辅助账本的 observer；返回原 ModelResponse，不把 schema 信封塞入主历史。
    # 函数用途: 代替一次结构化模型传输，并在返回前触发用例约定的取消或故障。
    def generate_structured(self, prompt, *, response_schema, messages=None):
        self.calls.append(("selection", prompt, {"response_schema": response_schema, "messages": messages}))
        _emit_provider_attempt({"attempt_id": "fake-selection-http", "status": "finished", "http_status": 200})
        if self.before_response:
            self.before_response()
        if self.failure:
            raise self.failure
        return ModelResponse(json.dumps({"selected_ids": list(self.selected)}), self.name,
                             usage={"input_tokens": 17, "output_tokens": 9})

    # LLM: 捕获真实主请求组装后的 messages/tools，不能替测试自动补能力入口。
    # 函数用途: 返回普通业务回答，让断言核对主模型实际收到的内容。
    def generate(self, prompt, **kwargs):
        self.calls.append(("primary", prompt, kwargs))
        return ModelResponse("已整理资料。", self.name, usage={"input_tokens": 41, "output_tokens": 7})


# LLM: 固定包使用真实安装夹具和元数据快照；任务 marker 是宿主 typed 初值，不从用户 attributes 接收。
# 函数用途: 装配原 TaskStore 与可观察 reader，默认代表一条已建立但还未选包的主任务。
def _runtime(tmp_path, monkeypatch, *, enabled=True, pending=True, bind=True, selected=("capability:story-a",), config=""):
    agent, installs, entries = _agent(tmp_path)
    path = tmp_path / "capability.yaml"
    path.write_text(f"enable_capability_package_selection: {str(enabled).lower()}\n{config}", encoding="utf-8")
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = None
    agent.config.tool_protocol = "native"
    agent.config.model_backend = "anthropic_compatible"
    agent.backend = _Backend(selected=selected)
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "selection-runtime",
        "channel_user_id": "local/main",
    })
    attrs = {"conversation_thread_id": thread.thread_id}
    if bind:
        agent.conversation_store.tasks.bind(
            {"thread_id": thread.thread_id, "task_id": "task-selection", "goal": "核对资料", "status": "active"},
            capability_selection=TaskCapabilitySelection.pending() if pending else None,
        )
        attrs["conversation_task_id"] = "task-selection"
    authority = _bind_main_agent_authority(agent, RunParams(
        request_id="request-selection", run_id="run-selection", task_id="task-selection", task_attributes=attrs,
    ))
    params = ToolLoopExecuteParams(
        user_prompt="核对资料", root_user_prompt="核对资料", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=["skill_search"], write_boundary=None, task_attributes=attrs, request_id="request-selection",
        run_id=authority.run_id, task_id="task-selection", attempt_id=authority.attempt_id, one_shot_tool_calls=set(),
        executed_tools=[], archive_tool_calls=[], context_scope="conversation", cancellation_token=CancellationToken(),
        tool_runtime_snapshot=agent.tools.runtime_snapshot(allowed_tools=["skill_search"], run_id="run-selection"),
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-selection", source_protocol="native"),
    )
    agent._current_run_params = params
    original = package_provider.read_capability_member
    reads = []

    def observe_read(*args, **kwargs):
        reads.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(package_provider, "read_capability_member", observe_read)
    return SimpleNamespace(agent=agent, params=params, thread=thread, installs=installs, entries=entries, reads=reads)


# LLM: 身份取当前结构化 link；读取原 TaskStore，不缓存 marker 的预期结果。
# 函数用途: 观察当前任务持久状态。
def _task(fixture):
    return fixture.agent.conversation_store.tasks.load(fixture.params.task_attributes.get("conversation_task_id", fixture.params.task_id))


# LLM: 只观察 typed IR 的明确宿主来源，不把模型正文或工具文本猜成选包事实。
# 函数用途: 提取主模型本轮可以收到的入口或告警上下文。
def _facts(fixture, source="capability_package_entries"):
    return [item.text for item in fixture.params.tool_ir_history if isinstance(item, RuntimeFactsTurn) and item.source == source]


# LLM: 旧 cwd 选择来自原 Gateway conversation_runtime 结构，不按新输入词句推断恢复；旧任务保持无选择标记。
# 函数用途: 构造一个明确指定已结束工作区的新请求，并保留 Gateway 实际参数投影供组合检查。
def _terminal_workspace_context(fixture, status):
    from pathlib import Path

    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayConversationLoadRequest,
        gateway_conversation_context,
    )

    store = fixture.agent.conversation_store
    workspace = Path(fixture.agent.home_paths.owner_home_dir) / "tasks" / "old-task"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    store.tasks.bind({"thread_id": fixture.thread.thread_id, "task_id": "task-old", "goal": "旧工作",
                      "status": "active", "task_path": str(workspace)})
    store.tasks.update_status({"task_id": "task-old", "status": status})
    request = {
        "id": fixture.params.request_id,
        "conversation": {"channel": "cli", "channel_conversation_id": "selection-runtime",
                         "channel_user_id": "local/main", "canonical_user_id": "local/main"},
        "conversation_runtime": {"request_id": fixture.params.request_id, "thread_id": fixture.thread.thread_id,
                                 "task_id": "task-old", "task_path": str(workspace)},
    }
    context = gateway_conversation_context(GatewayConversationLoadRequest(
        fixture.agent, request, fixture.params.request_id, fixture.params.user_prompt,
    ))
    return store, workspace, request, context


def test_real_claim_read_pin_finish_and_runtime_facts_preserve_source(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch)
    observed = []

    def before_response():
        task = _task(fixture)
        observed.append(task.capability_selection)
        assert task.capability_selection.status == "claimed"
        assert task.skill_snapshot_refs == ()
        assert fixture.reads == [] and fixture.params.tool_ir_history == []

    fixture.agent.backend.before_response = before_response
    prepare_capability_package_selection(fixture.agent, fixture.params)
    task = _task(fixture)
    marker = task.capability_selection
    assert marker.status == "finished" and marker.outcome == "selected" and marker.selected_count == 1
    assert marker.claim_id == observed[0].claim_id
    assert marker.attempt_id == fixture.params.attempt_id
    assert len(marker.candidate_digest) == len(marker.model_binding_digest) == len(marker.selection_digest) == 64
    assert len(task.skill_snapshot_refs) == 1
    assert task.skill_snapshot_refs[0]["activation_id"] == fixture.entries[0].activation_id
    assert len(fixture.reads) == 1
    text, = _facts(fixture)
    assert "# story-a" in text and "# story-b" not in text
    assert '"source_ref"' in text and fixture.entries[0].package_sha256 in text
    page, = json.loads(text[text.index("{"):])["entries"]
    assert page["body"] == "# story-a" and page["offset"] == 0 and not page["has_more"]
    assert page["total_chars"] == len(page["body"])
    assert page["source_ref"]["activation_id"] == task.skill_snapshot_refs[0]["activation_id"]
    assert not fixture.params.executed_tools and not fixture.params.archive_tool_calls
    assert all(isinstance(item, RuntimeFactsTurn) for item in fixture.params.tool_ir_history)
    with fixture.agent.subagents.runtime_db._runtime_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_operations").fetchone()[0] == 0
    record, = fixture.agent._model_call_ledger.records()
    assert record.metadata["purpose"] == "capability_selection"
    assert record.request_id == fixture.params.request_id and record.run_id == fixture.params.run_id
    assert record.metadata["task_id"] == task.task_id and record.provider_attempt_count == 1


@pytest.mark.parametrize("case", ["empty", "input_budget", "model_failure"])
def test_nonselection_finishes_once_without_read_or_pin(tmp_path, monkeypatch, case):
    fixture = _runtime(tmp_path, monkeypatch, selected=() if case == "empty" else ("capability:story-a",),
                       config="capability_package_selection_max_input_tokens: 1\n" if case == "input_budget" else "")
    if case == "model_failure":
        fixture.agent.backend.failure = ValueError("secret-model-body")
    prepare_capability_package_selection(fixture.agent, fixture.params)
    task = _task(fixture)
    assert task.capability_selection.status == "finished"
    assert task.capability_selection.outcome == ("empty" if case == "empty" else "failed")
    assert not fixture.reads and not task.skill_snapshot_refs and not _facts(fixture)
    assert len(fixture.agent.backend.calls) == (0 if case == "input_budget" else 1)
    serialized = json.dumps(task.to_dict(), ensure_ascii=False) + str(fixture.params.tool_ir_history)
    assert "secret-model-body" not in serialized
    assert bool(_facts(fixture, "capability_package_selection_warning")) == (case != "empty")
    prepare_capability_package_selection(fixture.agent, fixture.params)
    assert _task(fixture) == task
    assert len(fixture.agent.backend.calls) == (0 if case == "input_budget" else 1)


def test_new_attempt_and_store_reopen_do_not_repeat_finished_selection(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch)
    prepare_capability_package_selection(fixture.agent, fixture.params)
    original = _task(fixture)
    fixture.agent.conversation_store = ConversationStore(fixture.agent.conversation_store.storage.root)
    params = replace(fixture.params, attempt_id="attempt-selection-2", tool_ir_history=[], tool_context=[])
    fixture.agent._current_run_params = params
    prepare_capability_package_selection(fixture.agent, params)
    assert _task(fixture) == original
    assert len(fixture.agent.backend.calls) == len(fixture.reads) == 1
    assert params.tool_ir_history == []


@pytest.mark.parametrize("marker", ["absent", "corrupt"])
def test_existing_absent_or_corrupt_marker_is_not_repaired_or_selected(tmp_path, monkeypatch, marker):
    fixture = _runtime(tmp_path, monkeypatch, pending=False)
    path = fixture.agent.conversation_store.storage.task_path(fixture.params.task_id)
    if marker == "corrupt":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[CAPABILITY_SELECTION_KEY] = {"schema": CAPABILITY_SELECTION_KEY, "status": "unexpected"}
        path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    prepare_capability_package_selection(fixture.agent, fixture.params)
    assert path.read_bytes() == before
    assert not fixture.agent.backend.calls and not fixture.reads and not _task(fixture).skill_snapshot_refs
    assert not _facts(fixture)
    assert bool(_facts(fixture, "capability_package_selection_warning")) == (marker == "corrupt")


@pytest.mark.parametrize("change", ["cancel", "new_attempt", "runtime_stop"])
def test_late_selection_after_stop_or_attempt_change_never_reads_pins_or_injects(tmp_path, monkeypatch, change):
    fixture = _runtime(tmp_path, monkeypatch)

    def stop_before_response():
        if change == "cancel":
            fixture.params.cancellation_token.cancel("explicit-stop")
        elif change == "new_attempt":
            fixture.agent._current_run_params = replace(fixture.params, attempt_id="replacement-attempt")
        else:
            _settle_main_agent_run_status(
                fixture.agent,
                run_id=fixture.params.run_id,
                attempt_id=fixture.params.attempt_id,
                facts=RunCloseoutFacts(runtime_status="cancelled"),
            )

    fixture.agent.backend.before_response = stop_before_response
    with pytest.raises(ToolCancelled):
        prepare_capability_package_selection(fixture.agent, fixture.params)
    task = _task(fixture)
    assert task.capability_selection.status == "claimed"
    assert not task.skill_snapshot_refs and not fixture.reads and fixture.params.tool_ir_history == []


@pytest.mark.parametrize("mode", ["off", "child"])
def test_off_and_child_leave_original_task_bytes_and_do_no_snapshot_io(tmp_path, monkeypatch, mode):
    fixture = _runtime(tmp_path, monkeypatch, enabled=mode != "off")
    path = fixture.agent.conversation_store.storage.task_path(fixture.params.task_id)
    before = path.read_bytes()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("此范围不应触发包快照或任务读取")

    monkeypatch.setattr(fixture.agent, "current_skill_snapshot", forbidden)
    monkeypatch.setattr(fixture.agent.conversation_store.tasks, "load", forbidden)
    previous = set_current_subagent_context(fixture.agent, run_id="child-selection", task_attributes={}) if mode == "child" else None
    try:
        prepare_capability_package_selection(fixture.agent, fixture.params)
    finally:
        if previous is not None:
            restore_current_subagent_context(fixture.agent, previous)
    assert path.read_bytes() == before
    assert not fixture.agent.backend.calls and not fixture.reads and fixture.params.tool_ir_history == []


def test_actual_first_model_request_sees_entry_before_capture_without_selector_envelope(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch)
    from agent_py_agent.agent import model_request_selection

    original_build = loop_service.build_tool_loop_prompt
    original_capture = model_request_selection.prepare_request_context
    edges = []

    def build(agent, params):
        assert _task(fixture).capability_selection.status == "finished"
        assert "# story-a" in _facts(fixture)[0]
        edges.append("build")
        return original_build(agent, params)

    def capture(agent, params, prompt):
        assert "# story-a" in _facts(fixture)[0]
        edges.append("capture")
        return original_capture(agent, params, prompt)

    monkeypatch.setattr(loop_service, "build_tool_loop_prompt", build)
    monkeypatch.setattr(model_request_selection, "prepare_request_context", capture)
    turn = loop_service.next_tool_loop_model_response(fixture.agent, fixture.params, 0)
    assert turn.response.text == "已整理资料。"
    assert [call[0] for call in fixture.agent.backend.calls] == ["selection", "primary"]
    assert edges[:2] == ["build", "capture"]
    primary = fixture.agent.backend.calls[-1]
    payload = json.dumps({"prompt": primary[1], "messages": primary[2].get("messages")}, ensure_ascii=False)
    assert "# story-a" in payload and "source_ref" in payload
    assert "selected_ids" not in payload and "my_agent_structured_output" not in payload
    assert "host-read-policy:" not in payload
    assert not fixture.params.executed_tools and not fixture.params.archive_tool_calls
    assert all(isinstance(item, RuntimeFactsTurn) for item in fixture.params.tool_ir_history)


@pytest.mark.parametrize("enabled", [False, True])
def test_tool_free_first_reply_keeps_empty_selection_task_without_creating_goal(tmp_path, monkeypatch, enabled):
    from agent_py_agent.agent import model_request_selection

    fixture = _runtime(tmp_path, monkeypatch, enabled=enabled, bind=False, selected=())
    fixture.params = replace(fixture.params, user_prompt="二加三等于多少？", root_user_prompt="二加三等于多少？")
    fixture.agent._current_run_params = fixture.params
    store = fixture.agent.conversation_store
    assert fixture.entries and store.tasks.list(fixture.thread.thread_id) == []
    assert store.goals.list(fixture.thread.thread_id) == []
    goal_path = store.storage.goal_path(fixture.thread.thread_id)
    assert not goal_path.exists()
    original_build = loop_service.build_tool_loop_prompt
    original_capture = model_request_selection.prepare_request_context
    original_generate = fixture.agent.backend.generate
    observed = []

    # LLM: 观察原请求组装前的真实任务列表，不替宿主绑定任务或创建 Goal。
    # 函数用途: 证明空选标记在业务请求组装前持久化，关闭时仍没有会话任务。
    def build(agent, params):
        observed.append(("build", store.tasks.list(fixture.thread.thread_id)))
        return original_build(agent, params)

    # LLM: 保留真实选模捕获路径，仅记录该结构边界的任务状态。
    # 函数用途: 核对捕获与发送沿同一首轮准备结果，不伪造选包载荷。
    def capture(agent, params, prompt):
        observed.append(("capture", store.tasks.list(fixture.thread.thread_id)))
        return original_capture(agent, params, prompt)

    # LLM: 只替模型传输的正文；原 fake 仍记录真实组装载荷，响应不携带工具调用。
    # 函数用途: 模拟一个直接答复的普通问题，不执行终态收尾或修改任务状态。
    def generate(prompt, **kwargs):
        return replace(original_generate(prompt, **kwargs), text="五。")

    monkeypatch.setattr(loop_service, "build_tool_loop_prompt", build)
    monkeypatch.setattr(model_request_selection, "prepare_request_context", capture)
    monkeypatch.setattr(fixture.agent.backend, "generate", generate)
    turn = loop_service.next_tool_loop_model_response(fixture.agent, fixture.params, 0)

    assert turn.response.text == "五。" and not turn.response.tool_use_blocks
    assert [kind for kind, _links in observed] == ["build", "capture"]
    assert [call[0] for call in fixture.agent.backend.calls] == (
        ["selection", "primary"] if enabled else ["primary"]
    )
    links = store.tasks.list(fixture.thread.thread_id)
    assert all(edge_links == links for _kind, edge_links in observed)
    if enabled:
        task, = links
        assert task.task_id == fixture.params.task_attributes["conversation_task_id"]
        assert task.work_kind != "goal" and task.status == "active"
        assert task.capability_selection.status == "finished"
        assert task.capability_selection.outcome == "empty"
        assert task.capability_selection.selected_count == 0 and not task.skill_snapshot_refs
        reopened = ConversationStore(store.storage.root)
        assert reopened.tasks.list(fixture.thread.thread_id) == [task]
    else:
        assert links == [] and "conversation_task_id" not in fixture.params.task_attributes
    assert store.goals.list(fixture.thread.thread_id) == [] and not goal_path.exists()
    assert not fixture.reads and not _facts(fixture)
    assert not fixture.params.executed_tools and not fixture.params.archive_tool_calls
    with fixture.agent.subagents.runtime_db._runtime_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_operations").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["later_round", "receipt", "committed"])
def test_nonfirst_or_recovery_or_receipt_skips_prepare_but_uses_original_request(tmp_path, monkeypatch, mode):
    from contextlib import nullcontext

    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        queue_natural_user_reply,
    )
    from agent_py_agent.agent.capability import package_selection_runtime
    from agent_py_agent.agent.model_request_selection import model_request_selection_scope

    fixture = _runtime(tmp_path, monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("不应重复准备能力选择")

    class CommittedHost:
        def committed_selection(self, agent, params):
            assert agent is fixture.agent and params is fixture.params
            return params, "原已提交请求"

        def select(self, agent, params, prompt):
            return params, prompt

    monkeypatch.setattr(package_selection_runtime, "prepare_capability_package_selection", forbidden)
    if mode == "receipt":
        queue_natural_user_reply(fixture.params, kind="background_dispatch", facts={"status": "running"})
    context = model_request_selection_scope(CommittedHost()) if mode == "committed" else nullcontext()
    with context:
        turn = loop_service.next_tool_loop_model_response(fixture.agent, fixture.params, 1 if mode == "later_round" else 0)
    assert turn.response.text == "已整理资料。"
    assert [call[0] for call in fixture.agent.backend.calls] == ["primary"]
    assert not fixture.reads and not _task(fixture).skill_snapshot_refs and not _facts(fixture)
    assert _task(fixture).capability_selection == TaskCapabilitySelection.pending()
    if mode == "committed":
        assert turn.prompt == "原已提交请求"
    elif mode == "receipt":
        assert not fixture.agent.backend.calls[0][2].get("tools")


def test_claim_left_by_interrupted_attempt_is_not_retried_by_new_attempt(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch)
    fixture.agent.backend.failure = InterruptedError("stop-selection")
    with pytest.raises(InterruptedError):
        prepare_capability_package_selection(fixture.agent, fixture.params)
    claimed = _task(fixture).capability_selection
    assert claimed.status == "claimed"
    params = replace(fixture.params, attempt_id="attempt-next", cancellation_token=CancellationToken(), tool_ir_history=[])
    fixture.agent._current_run_params = params
    prepare_capability_package_selection(fixture.agent, params)
    assert _task(fixture).capability_selection == claimed
    assert len(fixture.agent.backend.calls) == 1 and not fixture.reads and not params.tool_ir_history


def test_body_budget_failure_finishes_selected_with_no_read_or_pin(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch, config="capability_bundle_max_tokens: 1\n")
    prepare_capability_package_selection(fixture.agent, fixture.params)
    task = _task(fixture)
    assert task.capability_selection.outcome == "selected"
    assert "CAPABILITY_SELECTION_ENTRY_BUDGET_EXHAUSTED" in task.capability_selection.warning_codes
    assert not task.skill_snapshot_refs and not _facts(fixture) and not fixture.reads
    assert len(fixture.agent.backend.calls) == 1


def test_unpromoted_conversation_uses_original_promotion_then_claim(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch, bind=False)
    assert fixture.agent.conversation_store.tasks.list(fixture.thread.thread_id) == []
    transitions = []
    from agent_py_agent.agent.conversation.store_tasks import TaskStore

    original = TaskStore.claim_capability_selection

    def claim(store, **kwargs):
        link = store.load(kwargs["task_id"])
        transitions.append((link.capability_selection.status, link.thread_id, link.status))
        return original(store, **kwargs)

    monkeypatch.setattr(TaskStore, "claim_capability_selection", claim)
    prepare_capability_package_selection(fixture.agent, fixture.params)
    task = _task(fixture)
    assert transitions == [("pending", fixture.thread.thread_id, "active")]
    assert fixture.params.task_attributes["conversation_task_id"] == task.task_id
    assert task.task_id == fixture.params.task_id and task.capability_selection.status == "finished"
    assert len(task.skill_snapshot_refs) == 1 and len(fixture.reads) == 1
    assert len(fixture.agent.conversation_store.tasks.list(fixture.thread.thread_id)) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_goal_create_initializes_optional_marker_without_model_or_body(tmp_path, monkeypatch, enabled):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.goal_control_service import (
        GoalControlRequest,
        execute_goal_control_operation,
    )

    fixture = _runtime(tmp_path, monkeypatch, enabled=enabled, bind=False)
    result = execute_goal_control_operation(GoalControlRequest(
        owner_agent=fixture.agent, store=fixture.agent.conversation_store, thread=fixture.thread,
        command=ConversationControlCommand("goal", operation="create", name="资料整理", value="核对资料", duration_seconds=60),
        scope=GatewayControlScope("local/main", "cli", "selection-runtime"), resume_registry=lambda _task: None,
        initial_cwd=str(fixture.agent.root), initial_workspace_roots=(str(fixture.agent.root),),
    ))
    assert result.ok, result.message
    task = fixture.agent.conversation_store.tasks.load(result.request_id)
    assert task.work_kind == "goal" and task.thread_id == fixture.thread.thread_id
    assert task.capability_selection == (TaskCapabilitySelection.pending() if enabled else None)
    assert (CAPABILITY_SELECTION_KEY in task.to_dict()) is enabled
    assert not fixture.reads and not fixture.agent.backend.calls and not task.skill_snapshot_refs


def test_cancel_during_original_member_read_never_commits_late_pin_or_context(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch)
    original = package_provider.read_capability_member

    def interrupted_read(*args, **kwargs):
        result = original(*args, **kwargs)
        fixture.params.cancellation_token.cancel("stop-during-read")
        return result

    monkeypatch.setattr(package_provider, "read_capability_member", interrupted_read)
    with pytest.raises(ToolCancelled, match="stop-during-read"):
        prepare_capability_package_selection(fixture.agent, fixture.params)
    assert len(fixture.reads) == 1 and len(fixture.agent.backend.calls) == 1
    assert not _task(fixture).skill_snapshot_refs and not fixture.params.tool_ir_history
    assert _task(fixture).capability_selection.status == "claimed"


@pytest.mark.parametrize("error", [InterruptedError, ToolCancelled, CancelledError])
def test_cancelled_selection_propagates_without_converting_to_optional_warning(tmp_path, monkeypatch, error):
    fixture = _runtime(tmp_path, monkeypatch)
    failure = error("selection-cancelled")
    fixture.agent.backend.failure = failure
    with pytest.raises(error) as caught:
        prepare_capability_package_selection(fixture.agent, fixture.params)
    assert caught.value is failure
    assert _task(fixture).capability_selection.status == "claimed"
    assert not _task(fixture).skill_snapshot_refs and not fixture.reads and not fixture.params.tool_ir_history


@pytest.mark.parametrize("status", ["completed", "interrupted"])
def test_exact_terminal_workspace_new_execution_gets_pending_only_on_new_link(tmp_path, monkeypatch, status):
    from agent_py_agent.agent.conversation.task_promotion import promote_conversation_task_for_run
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_task_attributes

    fixture = _runtime(tmp_path, monkeypatch, bind=False)
    store, workspace, _request, context = _terminal_workspace_context(fixture, status)
    old_bytes = store.storage.task_path("task-old").read_bytes()
    attrs = _gateway_task_attributes(context)
    assert attrs["conversation_workspace_task_id"] == "task-old"
    assert len(store.tasks.list(fixture.thread.thread_id)) == 1
    assert attrs.get("conversation_task_id") == ("task-old" if status == "interrupted" else None)
    params = replace(fixture.params, task_attributes=attrs)
    fixture.agent._current_run_params = params
    successor = promote_conversation_task_for_run(fixture.agent, params)
    assert successor is not None and successor.task_id == fixture.params.task_id
    assert successor.task_id != "task-old" and successor.task_path == str(workspace)
    assert successor.capability_selection == TaskCapabilitySelection.pending()
    assert store.storage.task_path("task-old").read_bytes() == old_bytes
    assert not fixture.agent.backend.calls and not fixture.reads


def test_selected_packages_share_one_selection_and_keep_independent_receipts(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch, selected=("capability:story-b", "capability:story-a"))
    prepare_capability_package_selection(fixture.agent, fixture.params)
    task = _task(fixture)
    assert task.capability_selection.selected_count == len(task.skill_snapshot_refs) == 2
    assert len(fixture.agent.backend.calls) == 1 and len(fixture.reads) == 2
    text, = _facts(fixture)
    pages = json.loads(text[text.index("{"):])["entries"]
    assert {page["package_id"]: page["body"] for page in pages} == {"story-a": "# story-a", "story-b": "# story-b"}
    assert {page["source_ref"]["activation_id"] for page in pages} == {entry.activation_id for entry in fixture.entries}


def test_host_entry_loading_obeys_original_tool_approval_policy(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch)
    snapshot = fixture.params.tool_runtime_snapshot
    runtime = snapshot.runtime("skill_search")
    guarded = replace(runtime, runtime_policy=replace(
        runtime.runtime_policy, approval_policy=replace(runtime.runtime_policy.approval_policy, mode="always"),
    ))
    fixture.params = replace(fixture.params, tool_runtime_snapshot=replace(snapshot, runtimes=(guarded,), snapshot_hash=""))
    fixture.agent._current_run_params = fixture.params
    prepare_capability_package_selection(fixture.agent, fixture.params)
    marker = _task(fixture).capability_selection
    assert marker.status == "finished" and marker.outcome == "selected"
    assert "CAPABILITY_SELECTION_ENTRY_NOT_AUTHORIZED" in marker.warning_codes
    assert not fixture.reads and not _task(fixture).skill_snapshot_refs and not _facts(fixture)
    assert len(fixture.agent.backend.calls) == 1


def test_original_turn_transition_guards_claim_and_read_but_not_model_io(tmp_path, monkeypatch):
    from threading import Lock

    fixture = _runtime(tmp_path, monkeypatch)
    lock, phases, read_lock_states = Lock(), [], []

    def transition(phase, operation):
        with lock:
            phases.append(phase)
            return operation()

    fixture.params = replace(fixture.params, active_turn_transition_callback=transition)
    fixture.agent._current_run_params = fixture.params
    original = package_provider.read_capability_member

    def read(*args, **kwargs):
        read_lock_states.append(lock.locked())
        return original(*args, **kwargs)

    def response():
        assert not lock.locked()
        assert _task(fixture).capability_selection.status == "claimed"

    monkeypatch.setattr(package_provider, "read_capability_member", read)
    fixture.agent.backend.before_response = response
    prepare_capability_package_selection(fixture.agent, fixture.params)
    assert phases == ["capability_selection", "capability_selection"]
    assert read_lock_states == [True]
    assert _task(fixture).capability_selection.status == "finished" and len(_facts(fixture)) == 1


@pytest.mark.parametrize("already_promoted", [False, True])
def test_exact_interrupted_gateway_workspace_preparation_retains_valid_runtime_authority(tmp_path, monkeypatch, already_promoted):
    from agent_py_agent.agent.agent_core.tool_loop.recovery import runtime_run_scope
    from agent_py_agent.agent.capability import package_selection_context
    from agent_py_agent.agent.conversation.task_promotion import promote_conversation_task_for_run
    from agent_py_agent.agent.gateway_parts.request_context import GatewayAskRunContext
    from agent_py_agent.agent.gateway_parts.request_execution import (
        _gateway_run_params,
        _GatewayRunParamsRequest,
    )

    fixture = _runtime(tmp_path, monkeypatch, bind=False)
    store, _workspace, request, conversation = _terminal_workspace_context(fixture, "interrupted")
    ask = GatewayAskRunContext(fixture.agent, request, tmp_path / "request.json", tmp_path / "response.json",
                               fixture.params.request_id, None)
    gateway = _gateway_run_params(_GatewayRunParamsRequest(request, ask, conversation, fixture.params.user_prompt))
    assert gateway.task_id == "task-old"
    # 仅隔离队列发布宿主；执行身份仍经过真实 RuntimeDB 登记，不能用新 task 的假 authority 掩盖换代失配。
    gateway = replace(gateway, run_id="gateway-selection-run", conversation_task_binding_callback=None,
                      active_turn_transition_callback=None)
    bound = _bind_main_agent_authority(fixture.agent, gateway)
    fixture.params = replace(
        fixture.params, task_id=bound.task_id, run_id=bound.run_id, attempt_id=bound.attempt_id,
        task_attributes=bound.task_attributes,
        tool_runtime_snapshot=fixture.agent.tools.runtime_snapshot(allowed_tools=["skill_search"], run_id=bound.run_id),
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=bound.run_id, source_protocol="native"),
    )
    fixture.agent._current_run_params = bound
    decisions = []
    policy = package_selection_context.package_entry_policy

    def observe_policy(*args, **kwargs):
        decision = policy(*args, **kwargs)
        decisions.append(decision.to_dict())
        return decision

    monkeypatch.setattr(package_selection_context, "package_entry_policy", observe_policy)
    if already_promoted:
        successor = promote_conversation_task_for_run(fixture.agent, fixture.params)
        assert successor.task_id != "task-old" and successor.capability_selection == TaskCapabilitySelection.pending()
        assert runtime_run_scope(fixture.agent, fixture.params).task_id == "task-old"
        assert fixture.agent.subagents.runtime_db.task_id_for_run_id(bound.run_id) == "task-old"
    prepare_capability_package_selection(fixture.agent, fixture.params)
    link = store.tasks.load(fixture.params.task_attributes["conversation_task_id"])
    assert link.capability_selection is not None, (link.to_dict(), bound.task_attributes, fixture.params.tool_ir_history)
    assert link.capability_selection.status == "finished"
    assert link.capability_selection.outcome == "selected" and len(link.skill_snapshot_refs) == 1, decisions
    assert len(decisions) == 1 and not decisions[0]["reason_codes"]
    assert fixture.agent.subagents.runtime_db.task_id_for_run_id(bound.run_id) == "task-old"


@pytest.mark.parametrize("outcome", ["empty", "input_budget", "model_failure"])
def test_optional_selection_failure_or_empty_still_reaches_original_primary_request(tmp_path, monkeypatch, outcome):
    fixture = _runtime(tmp_path, monkeypatch, selected=(),
                       config="capability_package_selection_max_input_tokens: 1\n" if outcome == "input_budget" else "")
    if outcome == "model_failure":
        fixture.agent.backend.failure = RuntimeError("untrusted-private-selector-output")
    turn = loop_service.next_tool_loop_model_response(fixture.agent, fixture.params, 0)
    assert turn.response.text == "已整理资料。"
    assert [call[0] for call in fixture.agent.backend.calls] == (
        ["primary"] if outcome == "input_budget" else ["selection", "primary"]
    )
    primary = fixture.agent.backend.calls[-1]
    payload = json.dumps({"prompt": primary[1], "messages": primary[2].get("messages")}, ensure_ascii=False)
    assert "selected_ids" not in payload and "untrusted-private-selector-output" not in payload
    assert "my_agent_structured_output" not in payload and "# story-a" not in payload
    assert not fixture.reads and not _task(fixture).skill_snapshot_refs and not _facts(fixture)
    assert _task(fixture).capability_selection.outcome == ("empty" if outcome == "empty" else "failed")


def test_model_failure_cause_is_persisted_in_receipt_but_not_given_to_the_model(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError

    fixture = _runtime(tmp_path, monkeypatch)
    fixture.agent.backend.failure = ProviderRequestRejectedError(
        "HTTP 400: secret-provider-message", status_code=400,
        details={"status_code": 400, "provider_error": {"error": {
            "code": "invalid_json_schema", "param": "text.format.schema", "message": "secret-provider-message"}}})
    prepare_capability_package_selection(fixture.agent, fixture.params)
    selection = _task(fixture).capability_selection
    assert selection.outcome == "failed"
    assert selection.failure == {"error_type": "ProviderRequestRejectedError", "error_code": "PROVIDER_REQUEST_REJECTED",
                                 "http_status": 400, "provider_error_code": "invalid_json_schema",
                                 "provider_error_param": "text.format.schema"}
    serialized = json.dumps(_task(fixture).to_dict(), ensure_ascii=False) + str(fixture.params.tool_ir_history)
    assert "secret-provider-message" not in serialized
    model_facts = str(_facts(fixture, "capability_package_selection_warning"))
    assert "CAPABILITY_SELECTION_MODEL_FAILED" in model_facts and "invalid_json_schema" not in model_facts
