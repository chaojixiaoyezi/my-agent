"""冻结请求与真实原生发送等价；投影不准备运行、不消费状态、不读盘或发送模型请求。"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as service
from agent_py_agent.agent.agent_core import tool_request_projection as projection
from agent_py_agent.agent.agent_core.model.context_pressure import (
    model_visible_context_snapshot,
    projected_model_context_components,
)
from agent_py_agent.agent.agent_core.native_tool_protocol import (
    model_turn_tool_choice,
    resolve_native_tools,
)
from agent_py_agent.agent.agent_core.runner import prompts as runner
from agent_py_agent.agent.agent_core.runtime.conversation_state import (
    conversation_runtime_state_section,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _do_backend_generate,
    _materialize_native_prompt_facts,
    _native_provider_messages,
)
from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import BackendOptions, ProviderRequestOptions
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
from agent_py_agent.agent.backends.tool_protocol_adapter import tools_for_choice
from agent_py_agent.agent.model_guidance import provider_system_instruction
from agent_py_agent.agent.prompting_parts import builder as prompting
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents import SubAgentExecutionContext
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)
from agent_py_agent.tests.test_decision_skill_projection import setup_surface
from agent_py_agent.tests.test_native_tool_use_ir_messages_flow import _params
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)


# LLM: 全部来源沿原 runner、PromptBuilder、Skill/ToolRegistry；只有最后 HTTP 发送换成内存捕获。
# 函数用途: 构造可对照完整原生出站的子代理材料，包含真实系统、动态文件、名卡、历史与工具参数。
@pytest.fixture
def request_surface(tmp_path, skill_catalog_factory, tool_surface):  # noqa: F811
    registry, snapshot, *_ = tool_surface
    _catalog, builder = setup_surface(tmp_path, skill_catalog_factory, 3)
    dynamic = tmp_path / "dynamic.md"
    dynamic.write_text("动态规则：保留原授权与来源。", encoding="utf-8")
    context = SubAgentExecutionContext(
        run_id="run-1", generated_at=1.0, goal="检查表格并回交真实证据", thought="核对来源",
        plan=["检查", "交回"], allowed_tools=list(snapshot.available_tool_names),
        parent_id="parent", root_id="parent", role_template={"prompt_zh": "本轮角色规则"},
        context_bundle={"gate": {"ok": True}},
    )
    params = replace(
        _params(), user_prompt=runner._build_subagent_runner_prompt(context),
        system_prompt_override=runner.subagent_runner_system_prompt(context),
        prompt_files=[str(dynamic)], tool_runtime_snapshot=snapshot,
        tool_catalog_section=registry.render_catalog_section(runtime_snapshot=snapshot),
        selected_skill_ids=("workspace:method-001",),
        required_skill_ids=("workspace:method-002",),
        workspace_context_snapshot="本轮冻结时间与 cwd",
        conversation_history_seed=SimpleNamespace(compact_generation=3),
    )
    backend = AnthropicCompatibleBackend(BackendOptions(
        api_base="https://example.invalid", api_key="fake-test-key", model_name="fixture-model",
        request_timeout=1, max_tokens=64, temperature=0.1, stream_enabled=False,
    ))
    host = SimpleNamespace(prompts=builder, tools=registry, backend=backend, config=builder.config)
    request = projection.tool_loop_prompt_request(
        params, runtime_injections=("完整运行时注入",), workspace_context=params.workspace_context_snapshot,
        execution_facts="本轮已核对的结构化执行事实",
    )
    return host, params, request


# LLM: 只复制原参数中的已知事实；None 不被当作空历史或空工具，生产调用方必须同样显式提供。
# 函数用途: 为回归测试冻结一个完整请求，避免借投影构造额外宿主身份或探测工具能力。
def freeze_request(host, params, request):
    tools = resolve_native_tools(host, params)
    return projection.ToolLoopRequestInput(
        prompt_input=host.prompts.prepare_render_input(request),
        system_instruction=provider_system_instruction(host.backend),
        tool_protocol_snapshot=params.tool_protocol_snapshot,
        native_tools=tuple(tools or ()), tool_choice=model_turn_tool_choice(params, tools),
        tool_ir_history=tuple(params.tool_ir_history),
        provider_history_messages=tuple(params.provider_history_messages),
        tool_context=tuple(params.tool_context),
        forwarded_guidance=frozenset(params.live_archive_state.get("_forwarded_runtime_guidance", set())),
        conversation_state=conversation_runtime_state_section(params),
    )


@pytest.mark.parametrize("history_kind", ["first", "complete", "orphan"])
@pytest.mark.parametrize("choice", [ToolChoice.auto(), ToolChoice.none(), ToolChoice.specific("presentation_optional_a")])
def test_pure_projection_matches_real_outbound_payload_and_preserves_sources(request_surface, monkeypatch, history_kind, choice):
    host, params, request = request_surface
    params.live_archive_state["tool_choice"] = choice
    params.tool_ir_history.append(UserTurn(params.user_prompt))
    params.provider_history_messages.append({"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aW1hZ2U="}},
        {"type": "text", "text": "之前的真实问题"},
    ]})
    if history_kind != "first":
        call = canonical_history_call("presentation_optional_a", {"text": "完整参数" * 80})
        params.tool_ir_history.append(AssistantTurn(text="已读", tool_calls=[call], content_blocks=[
            {"type": "thinking", "thinking": "原完整推理", "signature": "original-signature"},
        ]))
        if history_kind == "complete":
            params.tool_ir_history.append(canonical_history_result(call, "完整结果" * 90))
    params.tool_context.extend(["已转发引导", "待转发引导", "[tool-record]不是另一份原生工具输入"])
    params.live_archive_state["_forwarded_runtime_guidance"] = {"已转发引导"}
    frozen = freeze_request(host, params, request)
    before = deepcopy((params.tool_ir_history, params.provider_history_messages, params.live_archive_state))
    projected = projection.project_tool_loop_request(frozen)
    assert projected.status == "ready" and not projected.missing_fields
    assert before == (params.tool_ir_history, params.provider_history_messages, params.live_archive_state)
    assert "SubAgent Runner Task" in projected.prompt and "本轮角色规则" in projected.prompt
    assert "动态规则" in projected.prompt and "workspace:method-001" in projected.prompt
    assert "workspace:method-002" in projected.prompt and "完整运行时注入" in projected.prompt
    assert "当前工具工作目录" not in projected.prompt  # 使用冻结工作区，未重新读取时钟/路径。

    actual_prompt = host.prompts.build(request=request)
    snapshot = model_visible_context_snapshot(host, params, actual_prompt)
    projected_tokens, components = projected_model_context_components(projected)
    assert snapshot.raw_estimated_tokens == projected_tokens
    assert sum(components.values()) == projected_tokens
    assert before[:2] == (params.tool_ir_history, params.provider_history_messages)
    assert params.live_archive_state["_forwarded_runtime_guidance"] == {"已转发引导"}
    materialized = _materialize_native_prompt_facts(ModelGenerateParams(host, params, actual_prompt, 0))
    actual_messages = _native_provider_messages(host, params)
    assert model_visible_context_snapshot(host, params, materialized.prompt).raw_estimated_tokens == projected_tokens
    assert projected.prompt == actual_prompt
    assert projected.provider_prompt == materialized.prompt
    assert prompt_cache_layout(projected.provider_prompt) == prompt_cache_layout(materialized.prompt)
    assert projected.messages == actual_messages
    assert projected.tools == (tools_for_choice(resolve_native_tools(host, params), choice) or None)
    assert projected.tool_choice == choice
    captured = []
    monkeypatch.setattr(host.backend, "request_json", lambda _path, payload, _headers: (
        captured.append(deepcopy(payload)) or {"content": [{"type": "text", "text": "完成"}]}
    ))
    actual_tools = resolve_native_tools(host, params)
    _do_backend_generate(host.backend, materialized.prompt, SimpleNamespace(
        params=params, tools=actual_tools, tool_choice=choice, messages=actual_messages,
        on_chunk=None, first_token_timeout_seconds=0,
        system_instruction=provider_system_instruction(host.backend),
    ))
    host.backend.generate(projected.provider_prompt, tools=projected.tools, messages=projected.messages, tool_choice=projected.tool_choice,
                          request_options=ProviderRequestOptions(
                              system_instruction=projected.system_instruction,
                              thinking_disabled=bool(frozen.native_tools) and choice.mode != "auto",
                          ))
    assert captured[0] == captured[1]
    assert captured[0].get("thinking") == ({"type": "disabled"} if choice.mode != "auto" else None)
    if choice.mode == "none":
        assert captured[0]["tools"] and captured[0]["tool_choice"] == {"type": "none"}
        assert snapshot.tool_schema_tokens > 0
    else:
        assert captured[0]["tools"]
        assert captured[0]["tool_choice"]["type"] == ("tool" if choice.mode == "specific" else "auto")


@pytest.mark.parametrize("missing", [
    "prompt_input", "system_instruction", "tool_protocol_snapshot", "native_tools", "tool_choice", "tool_ir_history",
    "provider_history_messages", "tool_context", "forwarded_guidance", "conversation_state",
])
def test_missing_input_is_typed_unknown_without_entering_renderer(request_surface, monkeypatch, missing):
    host, params, request = request_surface
    frozen = replace(freeze_request(host, params, request), **{missing: None})
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unknown 不得继续渲染或触碰宿主")
    monkeypatch.setattr(projection, "render_prepared_prompt", forbidden)
    monkeypatch.setattr(projection, "project_native_provider_messages", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    result = projection.project_tool_loop_request(frozen)
    assert result.status == "unknown" and missing in result.missing_fields
    assert result.prompt is None and result.messages is None and result.tools is None
    with pytest.raises(ValueError, match="complete model request projection required"):
        projected_model_context_components(result)


def test_explicit_empty_native_facts_are_known_and_invalid_choice_keeps_original_error(request_surface):
    host, params, request = request_surface
    frozen = freeze_request(host, params, request)
    empty = replace(frozen, native_tools=(), tool_ir_history=(), provider_history_messages=(),
                    tool_context=(), forwarded_guidance=frozenset(), conversation_state="")
    result = projection.project_tool_loop_request(empty)
    assert result.status == "ready" and result.tools is None and isinstance(result.messages, list)
    missing_choice = ToolChoice.specific("not-in-this-snapshot")
    with pytest.raises(ValueError) as original:
        tools_for_choice(list(frozen.native_tools), missing_choice)
    with pytest.raises(ValueError) as projected:
        projection.project_tool_loop_request(replace(frozen, tool_choice=missing_choice))
    assert str(projected.value) == str(original.value)


def test_frozen_render_has_no_host_reads_and_detaches_nested_containers(request_surface, monkeypatch):
    host, params, request = request_surface
    frozen = freeze_request(host, params, request)
    expected = projection.project_tool_loop_request(frozen)
    params.tool_ir_history.append(UserTurn("冻结后新插话不得被偷偷消费"))
    params.tool_context.append("冻结后的新引导")
    host.prompts.config.system_prompt = "冻结后配置"
    Path(request.prompt_files[0]).write_text("冻结后文件", encoding="utf-8")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("纯投影禁止读取、准备、刷新或发送")
    for method in ("read_text", "exists", "open"):
        monkeypatch.setattr(Path, method, forbidden)
    monkeypatch.setattr(host.prompts, "prepare_render_input", forbidden)
    monkeypatch.setattr(host.backend, "generate", forbidden)
    monkeypatch.setattr(service, "refresh_runtime_direct_children_snapshot", forbidden)
    monkeypatch.setattr(service, "inject_pending_turn_input", forbidden)
    monkeypatch.setattr(service, "_runtime_injections_with_delivery_contract", forbidden)
    projected = projection.project_tool_loop_request(frozen)
    assert projected == expected
    assert projected_model_context_components(projected) == projected_model_context_components(expected)
    projected.tools[0]["name"] = "只改返回值"
    projected.messages[0]["content"] = "只改返回消息"
    assert projection.project_tool_loop_request(frozen) == expected


def test_actual_tool_loop_preserves_preparation_order_then_uses_same_builder(request_surface, monkeypatch):
    host, params, _request = request_surface
    order, captured = [], []
    def injections(*_args, **_kwargs):
        order.append("goal")
        return ["原 Goal 注入"]
    def workspace(*_args):
        order.append("workspace")
        return "原工作区"
    def facts(*_args):
        order.append("facts")
        return "原执行事实"
    original_prepare = host.prompts.prepare_render_input
    def prepare(request):
        order.append("builder")
        frozen = original_prepare(request)
        captured.append(frozen)
        return frozen
    monkeypatch.setattr(service, "_runtime_injections_with_delivery_contract", injections)
    monkeypatch.setattr(service, "_runtime_workspace_context", workspace)
    monkeypatch.setattr(service, "render_current_turn_execution_facts", facts)
    monkeypatch.setattr(host.prompts, "prepare_render_input", prepare)
    actual = service._render_tool_loop_prompt(host, params)
    assert order == ["goal", "workspace", "facts", "builder"]
    assert actual == prompting.render_prepared_prompt(captured[0])


@pytest.mark.parametrize("native", [False, True])
def test_builder_legacy_and_prepared_render_match_frozen_bytes(tmp_path, monkeypatch, native):
    builder = prompting.PromptBuilder(AgentConfig(prompt_files=[]), tmp_path)
    request = prompting.PromptBuildRequest(
        "任务原文", [], inject=["注入原文"], system_prompt_override="完整 system 原文",
        workspace_context_override="已冻结 cwd 与时间",
        tools=prompting.ToolSections(native_tool_use=native, tool_context=["工具结果原文"]),
    )
    expected = builder.build(request=request)
    prepared = builder.prepare_render_input(request)
    monkeypatch.setattr(prompting, "_workspace_context_text", lambda *_a, **_k: pytest.fail("纯 render 读时钟"))
    assert prompting.render_prepared_prompt(prepared) == expected
    assert prompt_cache_layout(prompting.render_prepared_prompt(prepared)) == prompt_cache_layout(expected)


@pytest.mark.parametrize("native", [False, True])
def test_prepared_injection_replacement_preserves_unrelated_fragments_and_cache(tmp_path, monkeypatch, native):
    builder = prompting.PromptBuilder(AgentConfig(prompt_files=[]), tmp_path)
    # 用户可以输入和宿主完全相同的文字，甚至内嵌段落标题；替换只能依赖宿主已知位置。
    original = ["重复历史\n# Conversation Context", "", "重复历史\n# Conversation Context", "Goal 原文"]
    request = prompting.PromptBuildRequest(
        "任务原文", [], inject=original, system_prompt_override="固定 system",
        workspace_context_override="冻结的时间与目录",
        tools=prompting.ToolSections(native_tool_use=native),
    )
    prepared = builder.prepare_render_input(request)
    before = prompting.render_prepared_prompt(prepared)
    expected = builder.build(request=replace(request, inject=[*original[:2], "新历史\n第二行", original[3]]))
    original[:] = ["准备后调用方改变了容器"]
    monkeypatch.setattr(builder, "prepare_render_input", lambda *_a, **_k: pytest.fail("候选重新准备提示"))
    monkeypatch.setattr(prompting, "_workspace_context_text", lambda *_a, **_k: pytest.fail("候选重新读时间"))
    fragments = prepared.injection_fragments
    candidate = replace(prepared, injection_fragments=(*fragments[:2], "新历史\n第二行", *fragments[3:]))
    actual = prompting.render_prepared_prompt(candidate)
    assert actual == expected
    assert prepared.injection_fragments == ("重复历史\n# Conversation Context", "", "重复历史\n# Conversation Context", "Goal 原文")
    assert prompting.render_prepared_prompt(prepared) == before
    assert candidate.injected == "重复历史\n# Conversation Context\n\n新历史\n第二行\nGoal 原文"
    if native:
        assert prompt_cache_layout(actual).stable_prefix == prompt_cache_layout(before).stable_prefix
        assert prompt_cache_layout(actual) == prompt_cache_layout(expected)


@pytest.mark.parametrize("kind", ["normal", "audit_source_binding", "audit_source_worker"])
def test_runner_prepared_render_does_not_read_context_or_refs(monkeypatch, kind):
    context = SubAgentExecutionContext("child", 1.0, "原派工", "原思考", ["原计划"],
        context_bundle=({} if kind == "normal" else {"runtime_profile": {"kind": kind}}))
    expected = runner._build_subagent_runner_prompt(context, "本轮额外要求")
    prepared = runner.prepare_subagent_runner_prompt(context, "本轮额外要求")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("纯 runner render 不得重读上下文或 refs")
    monkeypatch.setattr(runner, "runner_context_summary_payload", forbidden)
    monkeypatch.setattr(runner, "_runner_execution_contract_lines", forbidden)
    monkeypatch.setattr(runner, "_build_audit_source_runner_prompt", forbidden)
    monkeypatch.setattr(Path, "exists", forbidden)
    context.goal = "准备后变化不能影响原快照"
    assert runner.render_subagent_runner_prompt(prepared) == expected
