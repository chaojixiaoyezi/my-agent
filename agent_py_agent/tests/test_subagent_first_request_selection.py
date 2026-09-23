"""首请求资格及持久发送栅栏：真实 prepare/create/activate/thread，物理业务 I/O 使用替身。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.subagent import compact_recovery
from agent_py_agent.agent.agent_core.subagent.model_selection import (
    mark_subagent_business_request_submitted,
)
from agent_py_agent.agent.agent_core.tool_model_generation import _invoke_backend_generate
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.conversation.agent_thread import ensure_subagent_thread
from agent_py_agent.agent.conversation.agent_thread_store import SUBAGENT_FIRST_REQUEST_KEY
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.settings.thread_model_selection import (
    SUBAGENT_MODEL_ADVICE_KEY,
    PendingSubagentModelAdvice,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.tests.test_tool_model_generation import _tool_loop_params


# LLM: 身份由真实准备/提交/激活产生；测试不手填 RUNNING 或 attempt，建议仅沿 typed 创建参数传递。
# 函数用途: 构造可恢复的 child 及精确请求参数，不调用模型或伪造历史。
@pytest.fixture
def child(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    manager = SubAgentManager(tmp_path / "runs", conversation_store=store)
    params = CreateRunParams(goal="检查材料", thought="", plan=[])
    prepared = manager.base_service.prepare_run(params=params)
    advice = PendingSubagentModelAdvice(
        profile_id="00000000-0000-0000-0000-000000000001", operation_id="create-operation",
        source_owner_ref="owner", source_thread_id="parent-thread", source_run_id="parent-run",
        source_task_id="parent-task", source_owner_revision=1, source_thread_revision=0,
        child_run_id=prepared.task.id, child_thread_id=prepared.task.agent_thread_id,
    )
    task = manager.create_run(params=params, prepared=replace(prepared, model_advice=advice))
    task = manager.lifecycle.prepare_runner_attempt(task.id)
    agent = SimpleNamespace(subagents=manager, conversation_store=store, config=SimpleNamespace(cache_diagnostics_enabled=False))
    request = replace(_tool_loop_params(), context_scope="task_local", source="subagent_run_model_turn",
                      run_id=task.id, attempt_id=task.runner_active_attempt_id, request_id=task.runner_active_attempt_id)
    return agent, task, request


# LLM: 真正生成函数抵达替身时读取 canonical 文件；只有发送前栅栏已提交才允许记录触网次数。
# 函数用途: 验证提交顺序与失败零 I/O，不以模拟返回的文本充当通过证据。
def _send(child, *, fail=False):
    agent, task, params = child
    calls = []

    class Backend:
        model_name = "inherited"
        def generate(self, prompt, **kwargs):
            thread = agent.conversation_store.threads.require(task.agent_thread_id)
            calls.append(thread)
            if fail:
                raise ConnectionError("request outcome unknown")
            return ModelResponse(text="ok", backend="test")

    backend = Backend()
    state = SimpleNamespace(agent=agent, params=params, ledger=None, call_id="call-1", tools=None, messages=None,
                            on_chunk=None, first_token_timeout_seconds=1, system_instruction="", retry_sink=None)
    return lambda: _invoke_backend_generate(backend, "actual prompt", state), calls


def test_new_pending_gets_init_only_eligibility_and_inherited_selection(child):
    agent, task, _ = child
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert (thread.model_selection_revision, thread.model_selection_source, thread.model_selection_last_explicit_revision) == (1, "inherited", 0)
    assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY] == {
        "schema": SUBAGENT_FIRST_REQUEST_KEY, "status": "unsubmitted", "operation_id": "create-operation",
        "child_run_id": task.id, "child_thread_id": task.agent_thread_id,
    }
    ensure_subagent_thread(agent.subagents, task)
    assert agent.conversation_store.threads.require(task.agent_thread_id).metadata == thread.metadata


def test_fence_and_retained_are_durable_before_actual_business_send(child):
    agent, task, params = child
    send, calls = _send(child)
    send()
    assert len(calls) == 1
    first = calls[0]
    assert first.model_profile_id == "default"
    assert first.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
    assert first.metadata[SUBAGENT_MODEL_ADVICE_KEY]["reason"] == "request_validation_unavailable"
    assert first.metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "submitted"
    assert first.metadata[SUBAGENT_FIRST_REQUEST_KEY]["attempt_id"] == params.attempt_id
    assert first.metadata[SUBAGENT_FIRST_REQUEST_KEY]["provider_call_id"] == "call-1"
    mark_subagent_business_request_submitted(agent, params, provider_call_id="call-2")
    assert agent.conversation_store.threads.require(task.agent_thread_id).metadata == first.metadata


def test_failed_or_ambiguous_send_never_restores_pending_or_first_eligibility(child):
    agent, task, _ = child
    send, calls = _send(child, fail=True)
    with pytest.raises(ConnectionError):
        send()
    assert len(calls) == 1
    reopened = ConversationStore(agent.conversation_store.storage.root)
    thread = reopened.threads.require(task.agent_thread_id)
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
    assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "submitted"


def test_old_pending_is_retained_without_backfilled_first_marker(child):
    agent, task, _ = child
    store = agent.conversation_store
    store.threads.update_atomic(task.agent_thread_id, lambda thread: replace(
        thread, metadata={key: value for key, value in thread.metadata.items() if key != SUBAGENT_FIRST_REQUEST_KEY},
    ))
    ensure_subagent_thread(agent.subagents, task)
    send, calls = _send(child)
    send()
    assert SUBAGENT_FIRST_REQUEST_KEY not in calls[0].metadata
    assert calls[0].metadata[SUBAGENT_MODEL_ADVICE_KEY]["reason"] == "first_request_unproven"


@pytest.mark.parametrize("invalid", ["missing_attempt", "stale_attempt", "cancelled"])
def test_unproven_or_cancelled_attempt_does_not_reach_provider(child, invalid):
    agent, task, params = child
    if invalid == "cancelled":
        params.cancellation_token.cancel("stop")
    else:
        params = replace(params, attempt_id="" if invalid == "missing_attempt" else "another-attempt")
    send, calls = _send((agent, task, params))
    with pytest.raises(InterruptedError):
        send()
    assert calls == []
    assert agent.conversation_store.threads.require(task.agent_thread_id).metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "unsubmitted"


def test_thread_write_failure_prevents_provider_io(child, monkeypatch):
    agent, _, _ = child
    def fail(*args, **kwargs):
        raise OSError("durable update failed")
    monkeypatch.setattr(agent.conversation_store.threads, "update_atomic", fail)
    send, calls = _send(child)
    with pytest.raises(OSError):
        send()
    assert calls == []


def test_default_creation_has_no_first_request_marker(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    manager = SubAgentManager(tmp_path / "runs", conversation_store=store)
    task = manager.create_run(goal="普通子任务")
    thread = store.threads.require(task.agent_thread_id)
    assert SUBAGENT_FIRST_REQUEST_KEY not in thread.metadata
    assert SUBAGENT_MODEL_ADVICE_KEY not in thread.metadata


@pytest.mark.parametrize("model_backend, model_name, api_base, window", [
    ("anthropic_compatible", "MiniMax-M2.7", "https://api.minimaxi.com/anthropic", 200_000),
    ("anthropic_compatible", "MiniMax-M3", "https://api.minimaxi.com/anthropic", 1_000_000),
    ("openai_compatible", "deepseek-v4-flash", "https://opencode.ai/zen/go/v1", 1_000_000),
])
@pytest.mark.parametrize("enable_tools", [False, True])
def test_real_child_first_request_capture_matches_actual_provider_payload(tmp_path, monkeypatch, model_backend, model_name, api_base, window, enable_tools):
    import json
    import re

    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.agent_core.tool_request_projection import project_tool_loop_request
    from agent_py_agent.agent.backends import http
    from agent_py_agent.agent.backends.base import ProviderRequestOptions
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(
        model_backend=model_backend, model_name=model_name, api_base=api_base,
        api_key="fake-private-key", stream_enabled=False, enable_tools=enable_tools,
        model_context_window_tokens=window, model_context_window_explicit=True, max_tool_rounds=1,
    ), tmp_path)
    params = CreateRunParams(goal="检查已提供的材料并给出结论", thought="", plan=[], allowed_tools=["read_file"])
    prepared = agent.subagents.base_service.prepare_run(params=params)
    advice = PendingSubagentModelAdvice(
        profile_id="00000000-0000-0000-0000-000000000001", operation_id="create-operation",
        source_owner_ref="owner", source_thread_id="parent-thread", source_run_id="parent-run", source_task_id="parent-task",
        source_owner_revision=1, source_thread_revision=0, child_run_id=prepared.task.id, child_thread_id=prepared.task.agent_thread_id,
    )
    task = agent.subagents.create_run(params=params, prepared=replace(prepared, model_advice=advice))
    observed = []
    probe_calls = []

    def response(text, *, tool=None):
        if model_backend == "anthropic_compatible":
            return {"content": [tool] if tool else [{"type": "text", "text": text}], "stop_reason": "end_turn"}
        message = {"content": text}
        if tool:
            message["tool_calls"] = [{"id": tool["id"], "type": "function", "function": {"name": tool["name"], "arguments": json.dumps(tool["input"])}}]
        return {"choices": [{"message": message, "finish_reason": "stop"}]}

    def send(request):
        wire = json.loads(json.dumps(request.payload, ensure_ascii=False))
        tool_names = [row.get("name") or row.get("function", {}).get("name") for row in wire.get("tools", [])]
        if tool_names == ["my_agent_capability_probe"]:
            probe_calls.append(wire)
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(wire))[1]
            return response("", tool={"type": "tool_use", "id": "probe", "name": "my_agent_capability_probe", "input": {"nonce": nonce}})
        if not observed:
            preparation = model_selection._PREPARATION.get()
            assert preparation is not None and preparation.request_input is not None
            assert preparation.run_id == task.id
            current = agent.subagents.load(task.id)
            assert preparation.attempt_id == current.runner_active_attempt_id
            frozen = preparation.request_input
            projected = project_tool_loop_request(frozen)
            assert projected.status == "ready", projected.missing_fields
            expected = agent.backend.project_generate_payload(
                projected.provider_prompt, tools=list(frozen.native_tools) or None,
                tool_choice=projected.tool_choice if frozen.native_tools else None,
                messages=projected.messages, request_options=ProviderRequestOptions(
                    system_instruction=projected.system_instruction,
                    thinking_disabled=bool(frozen.native_tools) and projected.tool_choice.mode != "auto",
                ),
            )
            assert wire == expected
            thread = agent.conversation_store.threads.require(task.agent_thread_id)
            assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "submitted"
            assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
            assert thread.model_profile_id == "default"
            observed.append((wire, frozen))
        return response("检查完成，当前材料无需修改。")

    monkeypatch.setattr(http, "post_json", send)
    agent.run_subagent(task.id, dry_run=False, probe=False)
    assert observed, "必须抵达真实 child 首次业务发送"
    assert bool(probe_calls) is enable_tools
    assert model_selection._PREPARATION.get() is None


# LLM: 候选目录、设置、父子 thread 与首轮资格走原存储；只假造服务商返回，不伪造 RUNNING/attempt/空历史或直接改有效 profile。
# 函数用途: 构造宿主已经保存语义建议的真实 child，供自动验证/采用和失败矩阵使用。
def _automatic_child(tmp_path, *, backend="anthropic_compatible", model="MiniMax-M3", window=1_000_000, persist_child=True, timeout_seconds=10, output_limit=4096):
    from agent_py_agent.agent.conversation.decision_policy import decision_owner_ref
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.settings.model_profiles import model_profile_generation
    from agent_py_agent.tests.test_decision_model_profiles import decision
    from agent_py_agent.tests.test_decision_settings import patch
    from agent_py_agent.tests.test_model_profiles import add

    agent = SimpleAgent(AgentConfig(
        model_backend="anthropic_compatible", model_name="inherited", api_base="https://inherited.test/anthropic",
        api_key="fake-key", stream_enabled=False, enable_tools=True, enable_subagents=True, max_subagents=10, max_tool_rounds=3,
        max_tokens=output_limit, model_context_window_tokens=200_000, model_context_window_explicit=True,
    ), tmp_path)
    endpoint = "https://api.minimaxi.com/anthropic" if backend == "anthropic_compatible" else "https://opencode.ai/zen/go/v1"
    key, _ = add(agent, model_backend=backend, model_name=model, api_base=endpoint, model_context_window_tokens=window)
    jev, _ = decision(agent)
    source = agent.conversation_store.threads.get_or_create({"canonical_user_id": agent.home_paths.owner_id,
                                                            "owner_id": agent.home_paths.owner_id})
    settings = patch(agent, {"enabled": True, "profile_id": jev, "points.subagent_model.mode": "apply",
                             "points.subagent_model.candidate_profile_ids": [key], "timeout_seconds": timeout_seconds,
                             "stage_timeout_seconds": 10})
    generation = model_profile_generation(agent, key, initialize=True)
    if not persist_child:
        return agent, source, key
    params = CreateRunParams(goal="读取提供的材料并给出结论", thought="", plan=[], allowed_tools=["read_file"],
                             attributes={"conversation_thread_id": source.thread_id})
    prepared = agent.subagents.base_service.prepare_run(params=params)
    advice = PendingSubagentModelAdvice(key, "create-operation", decision_owner_ref(agent), source.thread_id,
        "", "", settings["revision"]["owner"], settings["revision"]["thread"],
        prepared.task.id, prepared.task.agent_thread_id, source_model_generation=generation)
    task = agent.subagents.create_run(params=params, prepared=replace(prepared, model_advice=advice))
    return agent, task, key


# LLM: 捕获原 post_json 最终 payload；探针响应使用原 nonce，业务工具经真实执行器读取文件；测试不替被测 child 执行工作。
# 函数用途: 为两种适配器提供 fake provider，只在实际目录允许时调用读取工具，保留首轮及后续出站证据。
def _install_automatic_provider(monkeypatch, agent, task, material, *, before_candidate_probe=None, fail_probe=False):
    import json
    import re

    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.agent_core.tool_request_projection import project_tool_loop_request
    from agent_py_agent.agent.backends import http
    from agent_py_agent.agent.backends.base import ProviderRequestOptions

    calls, probes = [], []

    def send(request):
        wire = json.loads(json.dumps(request.payload, ensure_ascii=False))
        native = any("input_schema" in row for row in wire.get("tools", [])) or "system" in wire
        names = [row.get("name") or row.get("function", {}).get("name") for row in wire.get("tools", [])]
        tool = None
        if names == ["my_agent_capability_probe"]:
            probes.append(wire)
            if wire["model"] != "inherited":
                if before_candidate_probe:
                    before_candidate_probe()
                if fail_probe:
                    return {"content": [{"type": "text", "text": "unsupported"}], "stop_reason": "end_turn"} if native else {
                        "choices": [{"message": {"content": "unsupported"}, "finish_reason": "stop"}],
                    }
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(wire))[1]
            tool = {"type": "tool_use", "id": "probe", "name": "my_agent_capability_probe", "input": {"nonce": nonce}}
        else:
            thread = agent.conversation_store.threads.require(task.agent_thread_id)
            assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "submitted"
            frozen = model_selection._PREPARATION.get().request_input
            if not calls:
                projection = project_tool_loop_request(frozen)
                expected = agent.backend.project_generate_payload(
                    projection.provider_prompt, tools=list(frozen.native_tools) or None,
                    tool_choice=projection.tool_choice if frozen.native_tools else None,
                    messages=projection.messages, request_options=ProviderRequestOptions(
                        system_instruction=projection.system_instruction,
                        thinking_disabled=bool(frozen.native_tools) and projection.tool_choice.mode != "auto",
                    ),
                )
                assert wire == expected
                if "read_file" in names:
                    tool = {"type": "tool_use", "id": "read-material", "name": "read_file", "input": {"path": str(material)}}
            calls.append((wire, thread))
        if native:
            return {"content": [tool] if tool else [{"type": "text", "text": "材料检查完成。"}],
                    "stop_reason": "tool_use" if tool else "end_turn"}
        message = {"content": "" if tool else "材料检查完成。"}
        if tool:
            message["tool_calls"] = [{"id": tool["id"], "type": "function", "function": {
                "name": tool["name"], "arguments": json.dumps(tool["input"]),
            }}]
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool else "stop"}]}

    monkeypatch.setattr(http, "post_json", send)
    return calls, probes


@pytest.mark.parametrize("backend, model, window", [
    ("anthropic_compatible", "MiniMax-M2.7", 200_000),
    ("anthropic_compatible", "MiniMax-M3", 1_000_000),
    ("openai_compatible", "deepseek-v4-flash", 1_000_000),
])
def test_automatic_adoption_binds_first_and_following_tool_payloads(tmp_path, monkeypatch, backend, model, window):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.agent.settings.model_profiles import inherited_model_config

    evidence = []
    original_evidence = loop_support._tool_runtime_evidence

    def collect_evidence(*args):
        result = original_evidence(*args)
        evidence.append(result)
        return result

    monkeypatch.setattr(loop_support, "_tool_runtime_evidence", collect_evidence)
    agent, task, key = _automatic_child(tmp_path, backend=backend, model=model, window=window, output_limit=65_536)
    material = tmp_path / "material.txt"
    material.write_text("材料内容用于验证真实工具读取。", encoding="utf-8")
    calls, probes = _install_automatic_provider(monkeypatch, agent, task, material)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert len(calls) == 2, result
    assert [wire["model"] for wire, _ in calls] == [model, model]
    assert {wire["model"] for wire in probes} == {"inherited", model}
    first = calls[0][1]
    assert first.model_profile_id == key
    assert (first.model_selection_revision, first.model_selection_source, first.model_selection_last_explicit_revision) == (2, "automatic", 0)
    assert first.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "adopted"
    validation = first.metadata[SUBAGENT_MODEL_ADVICE_KEY]["validation"]
    assert validation["output_cap_tokens"] == calls[0][0]["max_tokens"] == min(65_536, window // 4)
    assert validation["context_window_tokens"] == window
    assert validation["input_tokens_estimate"] + validation["output_cap_tokens"] < window
    assert inherited_model_config(agent, task).model_name == model
    assert agent.config.model_name == "inherited"
    current = agent.subagents.load(task.id)
    assert current.id == task.id and current.created_at == task.created_at
    assert current.attributes.get("host_model_profile.v1") == task.attributes.get("host_model_profile.v1")
    assert "read-material" in str(calls[1][0]["messages"])
    assert result is not None
    assert evidence[-1]["protocol"]["model"] == model
    assert evidence[-1]["protocol"]["native_supported"]


@pytest.mark.parametrize("backend, model", [
    ("anthropic_compatible", "MiniMax-M3"),
    ("openai_compatible", "deepseek-v4-flash"),
])
def test_first_request_auto_compact_precedes_child_model_adoption(tmp_path, monkeypatch, backend, model):
    agent, task, key = _automatic_child(tmp_path, backend=backend, model=model, window=1_000_000)
    agent.config.model_context_window_tokens = 40_000
    agent.backend.context_window_tokens = 40_000
    agent.config.memory_compact_auto_trigger_percent = 50
    agent.config.max_tokens = 1024
    for index in range(12):
        agent.conversation_store.messages.append({
            "thread_id": task.agent_thread_id, "role": "user" if index % 2 == 0 else "assistant",
            "content": f"子代理旧材料第{index}段：" + "资料核对记录。" * 1000,
            "metadata": {"conversation_request_id": f"older-{index // 2}"},
        })
    material = tmp_path / "adopt-after-compact.txt"
    material.write_text("先压缩后采用候选模型。", encoding="utf-8")
    prepares, recoveries = [], []
    original_prepare = runtime_mixin._prepare_runtime_context
    original_recovery = compact_recovery.prepare_subagent_compact_recovery

    def prepare(*args, **kwargs):
        prepares.append(1)
        return original_prepare(*args, **kwargs)

    def recovery(*args, **kwargs):
        value = original_recovery(*args, **kwargs)
        recoveries.append(value)
        return value

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    monkeypatch.setattr(compact_recovery, "prepare_subagent_compact_recovery", recovery)
    monkeypatch.setattr(compact, "_summarize", lambda *_a, **_k: "旧材料已压缩并保留任务边界。")
    calls, probes = _install_automatic_provider(monkeypatch, agent, task, material)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok
    assert len(prepares) == len(recoveries) == 1
    assert recoveries[0].force is False and recoveries[0].committed
    assert len(calls) == 2 and [wire["model"] for wire, _ in calls] == [model, model]
    assert {wire["model"] for wire in probes} == {"inherited", model}
    first_wire, first_thread = calls[0]
    assert first_thread.compact_generation == 1 and first_thread.compact_checkpoint_id
    assert first_thread.model_profile_id == key
    assert first_thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "adopted"
    assert "旧材料已压缩" in str(first_wire)
    assert "read-material" in str(calls[1][0]["messages"])


@pytest.mark.parametrize("enable_tools", [False, True])
@pytest.mark.parametrize("backend,model", [
    ("anthropic_compatible", "MiniMax-M3"),
    ("openai_compatible", "deepseek-v4-flash"),
])
def test_adopted_none_choice_capacity_matches_actual_thinking_options(tmp_path, monkeypatch, enable_tools, backend, model):
    from agent_py_agent.agent.memory_archive import estimate_tokens
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    agent, task, key = _automatic_child(tmp_path, backend=backend, model=model)
    agent.config.enable_tools = enable_tools
    monkeypatch.setattr(
        "agent_py_agent.agent.contracts.required_actions.tool_choice_for_required_actions",
        lambda _snapshot, _tools: ToolChoice.none("host_fixed_choice"),
    )
    calls, _ = _install_automatic_provider(monkeypatch, agent, task, tmp_path / "unused.txt")
    agent.run_subagent(task.id, dry_run=False, probe=False)
    assert len(calls) == 1
    payload, thread = calls[0]
    assert thread.model_profile_id == key
    assert "tools" not in payload
    assert payload.get("tool_choice") == ("none" if backend == "openai_compatible" and enable_tools else None)
    assert payload.get("thinking") == ({"type": "disabled"} if enable_tools else None)
    validation = thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["validation"]
    assert validation["input_tokens_estimate"] == estimate_tokens(payload)


@pytest.mark.parametrize("change", ["catalog", "disabled", "explicit_same", "unsupported_tools", "small_window", "old_generation"])
def test_automatic_validation_failure_retains_inherited_without_user_steps(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.settings.model_profiles import (
        execute_model_profile_operation,
        model_profiles_path,
        read_model_profiles,
    )
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
    from agent_py_agent.tests.test_decision_settings import patch

    agent, task, _ = _automatic_child(tmp_path, window=4096 if change == "small_window" else 1_000_000)
    material = tmp_path / "material.txt"
    material.write_text("可以沿原模型完成读取。", encoding="utf-8")
    if change == "old_generation":
        agent.conversation_store.threads.update_atomic(task.agent_thread_id, lambda thread: replace(thread, metadata={
            **thread.metadata, SUBAGENT_MODEL_ADVICE_KEY: {**thread.metadata[SUBAGENT_MODEL_ADVICE_KEY], "source_model_generation": None},
        }))

    mutations = []

    def mutate():
        if change == "catalog":
            data = read_model_profiles(model_profiles_path(agent.home_paths))
            provider_id = next(iter(data["providers"]))
            execute_model_profile_operation(agent, "save_provider", {"provider_id": provider_id, "editing": True,
                "provider": {**data["providers"][provider_id], "api_key": "changed-private-test-key"}})
        elif change == "disabled":
            patch(agent, {"enabled": False})
        elif change == "explicit_same":
            thread_model_profile_id(agent, task.agent_thread_id, select="default")
        mutations.append(change)

    calls, _ = _install_automatic_provider(monkeypatch, agent, task, material, before_candidate_probe=mutate,
                                           fail_probe=change == "unsupported_tools")
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert len(calls) == 2, result
    assert {wire["model"] for wire, _ in calls} == {"inherited"}
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.model_profile_id == "default"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
    assert thread.model_selection_source == ("explicit" if change == "explicit_same" else "inherited")
    assert thread.model_selection_revision == (2 if change == "explicit_same" else 1)
    reason = thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["reason"]
    if change == "unsupported_tools":
        assert reason == "provider_tool_support_unknown"
    elif change == "old_generation":
        assert reason == "model_catalog_generation_unknown"
    elif change == "small_window":
        assert reason in {"request_capacity_exceeded", "original_context_preflight_rejected"}
    if change in {"catalog", "disabled", "explicit_same"}:
        assert mutations == [change]


def test_claimed_first_preparation_is_not_replayed_after_restart(child):
    from agent_py_agent.agent.agent_core.subagent.model_selection import (
        subagent_first_request_scope,
    )

    agent, task, params = child
    with subagent_first_request_scope(agent, task.id, params.attempt_id) as first:
        assert first is not None
    # 模拟准备后进程退出：持久标记保留 preparing，重进同一 attempt 也不能再次领取。
    with subagent_first_request_scope(agent, task.id, params.attempt_id) as second:
        assert second is None
    send, calls = _send(child)
    send()
    assert calls[0].metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
    assert calls[0].model_profile_id == "default"


def test_committed_selection_is_reused_by_next_canonical_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.subagent.model_selection import (
        canonical_subagent_model_scope,
    )

    agent, task, key = _automatic_child(tmp_path)
    material = tmp_path / "material.txt"
    material.write_text("材料", encoding="utf-8")
    calls, _ = _install_automatic_provider(monkeypatch, agent, task, material)
    agent.run_subagent(task.id, dry_run=False, probe=False)
    assert len(calls) == 2
    assert agent.config.model_name == "inherited"
    with canonical_subagent_model_scope(agent, task.id):
        assert agent.config.model_name == "MiniMax-M3"
        assert agent.config.config_sources["model_name"]["profile_id"] == key
    assert agent.config.model_name == "inherited"


def test_create_jev_advice_automatically_reaches_first_business_model(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.agent_core import orchestration_tools
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
    from agent_py_agent.agent.conversation import decision_model_call

    agent, source, key = _automatic_child(tmp_path, persist_child=False)
    agent.conversation_store.tasks.bind({"thread_id": source.thread_id, "task_id": "parent-task", "goal": "读取材料", "status": "active"})
    agent._current_run_params = RunParams(request_id="parent-request", task_id="parent-task", task_attributes={"conversation_thread_id": source.thread_id})
    monkeypatch.setattr(orchestration_tools, "publish_created_subagents", lambda _: SimpleNamespace(
        auto_start={"status": "not_attempted"}, conversation_bind_errors=[],
    ))
    decisions = []

    def choose(_agent, _params, request, backend, **kwargs):
        body = request.payload(backend.model_name)
        decisions.append(body)
        return parse_typesafe_response(request, backend.model_name, {"model": backend.model_name, "answers": {
            question: {"type": "choice", "choice": key, "confidence": 1,
                       "probabilities": {candidate: float(candidate == key) for candidate in row["criteria"]}}
            for question, row in body["questions"].items()
        }})

    monkeypatch.setattr(decision_model_call, "invoke_decision_model_call", choose)
    created = CreateSubagentsTool(agent).execute({"goal": "读取材料给出结论", "allowed_tools": ["read_file"]})
    assert created.ok, created.output
    task = agent.subagents.load(json.loads(created.output)["created_run_ids"][0])
    assert len(decisions) == 1
    before = agent.conversation_store.threads.require(task.agent_thread_id)
    assert before.model_profile_id == "default" and before.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "pending"
    material = tmp_path / "material.txt"
    material.write_text("由原 child 执行器读取。", encoding="utf-8")
    calls, _ = _install_automatic_provider(monkeypatch, agent, task, material)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert len(calls) == 2, result
    assert {wire["model"] for wire, _ in calls} == {"MiniMax-M3"}, calls[0][1].metadata[SUBAGENT_MODEL_ADVICE_KEY]
    assert calls[0][1].model_profile_id == key
    assert len(decisions) == 1


def test_late_probe_cannot_adopt_after_deadline_retained(tmp_path, monkeypatch):
    import threading

    agent, task, _ = _automatic_child(tmp_path, timeout_seconds=0.15)
    material = tmp_path / "material.txt"
    material.write_text("材料", encoding="utf-8")
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    def delayed():
        entered.set()
        try:
            assert release.wait(3)
        finally:
            exited.set()

    calls, _ = _install_automatic_provider(monkeypatch, agent, task, material, before_candidate_probe=delayed)
    try:
        result = agent.run_subagent(task.id, dry_run=False, probe=False)
        assert entered.is_set()
        assert len(calls) == 2, result
        assert {wire["model"] for wire, _ in calls} == {"inherited"}
    finally:
        release.set()
        assert exited.wait(3)
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.model_profile_id == "default"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["reason"] == "candidate_validation_deadline"


def test_uncertain_adoption_write_never_falls_back_to_inherited_business_io(tmp_path, monkeypatch):
    agent, task, key = _automatic_child(tmp_path)
    material = tmp_path / "material.txt"
    material.write_text("材料", encoding="utf-8")
    calls, probes = _install_automatic_provider(monkeypatch, agent, task, material)
    original = agent.conversation_store.threads.update_atomic
    failed = []

    def fail_after_commit(thread_id, update):
        result = original(thread_id, update)
        if (thread_id == task.agent_thread_id and not failed
                and result.metadata.get(SUBAGENT_FIRST_REQUEST_KEY, {}).get("status") == "reserved"):
            failed.append(True)
            raise OSError("write succeeded but caller lost confirmation")
        return result

    monkeypatch.setattr(agent.conversation_store.threads, "update_atomic", fail_after_commit)
    try:
        agent.run_subagent(task.id, dry_run=False, probe=False)
    except OSError:
        pass
    assert failed == [True]
    assert len(probes) == 2 and calls == []
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.model_profile_id == key
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "adopted"
    assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "reserved"


def test_durable_cancellation_during_candidate_probe_prevents_all_business_io(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.cancellation import (
        CancelSubagentTaskRequest,
        prepare_subagent_stops,
    )

    agent, task, _ = _automatic_child(tmp_path)
    material = tmp_path / "material.txt"
    material.write_text("材料", encoding="utf-8")
    stopped = []

    def cancel():
        current = agent.subagents.load(task.id)
        stopped.append(prepare_subagent_stops(agent, [CancelSubagentTaskRequest(
            current, "conversation_user_stop", kill_process=False, source="test",
        )], include_descendants=False))

    calls, _ = _install_automatic_provider(monkeypatch, agent, task, material, before_candidate_probe=cancel)
    try:
        agent.run_subagent(task.id, dry_run=False, probe=False)
    except InterruptedError:
        pass
    assert len(stopped) == 1
    assert not stopped[0].failed
    assert calls == []
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.model_profile_id == "default"
    assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "preparing"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "pending"
