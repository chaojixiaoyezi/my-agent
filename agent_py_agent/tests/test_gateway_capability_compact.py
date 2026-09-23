"""Gateway同一真实turn的展示恢复：原设置/Registry/Skill/Compact/DB，只有模型回答与最终循环为fake。"""
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import _render_tool_loop_prompt
from agent_py_agent.agent.agent_core.native_tool_protocol import resolve_native_tools
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.capability import decision_recommendation as recommendations
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactModelSurface,
    conversation_compact_provider_messages,
    conversation_compact_provider_prompt,
    prepare_conversation_compact_provider_surface,
)
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_context import GatewayAskRunContext
from agent_py_agent.agent.gateway_parts.request_history import append_gateway_conversation_message
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.tests.test_decision_capability_consumer import (
    model_input,
    provider,
    surface,  # noqa: F401
)
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_decision_skill_projection import setup_surface
from agent_py_agent.tests.test_gateway_conversation_compact import (
    _agent,
    _context,
    _request,
    _SummaryBackend,
)
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)


# LLM: 同一fixture的原协议事实由fake backend声明；原推荐和Compact仍各自按生产入口准备真实快照。
# 函数用途: 组合准确请求事实与纯展示，检测Compact是否丢失名卡、schema或把carrier当作身份。
def compact_surface(surface, selection, callback=None):  # noqa: F811
    params = surface.params
    surface.host.backend.probe_tool_capability = lambda: params.tool_protocol_snapshot.capability
    return ConversationCompactModelSurface(
        allowed_tools=tuple(params.allowed_tools) if params.allowed_tools is not None else None,
        context_scope=params.context_scope,
        presentation_context=RuntimeContextRequest(
            params.user_prompt, [], False, params.context_scope, allowed_tools=params.allowed_tools,
            request_id=params.request_id, run_id=params.run_id, task_id=params.task_id,
            task_attributes=dict(params.task_attributes),
        ),
        capability_presentation=selection, capability_presentation_turn_id=params.capability_presentation_turn_id,
        capability_presentation_callback=callback,
    )


@pytest.mark.parametrize("choice", [None, "not_needed"])
def test_compact_preserves_selected_stable_prefix_schema_and_typed_dynamic_cards(surface, monkeypatch, choice):  # noqa: F811
    surface.params.capability_presentation_turn_id = "gateway-turn"
    calls = provider(monkeypatch, choice=choice)
    selected = recommendations.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    observed = []
    requested = compact_surface(surface, selected.selection, observed.append)
    prepared = prepare_conversation_compact_provider_surface(surface.host, requested, run_id="summary-operation")
    _, _, loop = model_input(surface, selected)
    normal = _render_tool_loop_prompt(surface.host, loop)
    layout = prompt_cache_layout(normal)
    assert prepared.stable_prompt_prefix == layout.stable_prefix
    assert prepared.tools == tuple(resolve_native_tools(surface.host, loop))
    assert dict(prepared.volatile_sections)["prompt.tool_recommendations"] == dict(layout.volatile_sections)["prompt.tool_recommendations"]
    assert "method-059" not in prepared.stable_prompt_prefix
    assert "workspace:method-001" not in prepared.stable_prompt_prefix
    dynamic = json.dumps(conversation_compact_provider_messages("", 0, [], volatile_sections=prepared.volatile_sections), ensure_ascii=False)
    assert ("workspace:method-001" in dynamic) is (choice is None)
    assert "BODY-ONLY" not in dynamic and "method-059" not in dynamic
    assert str(conversation_compact_provider_prompt(prepared, "压缩要求")).endswith("压缩要求")
    assert observed == [selected.selection] and len(calls) == 1
    assert not {"capability_presentation_callback", "presentation_context"}.intersection(asdict(prepared))
    assert surface.snapshot.snapshot_hash == selected.tool_snapshot.snapshot_hash


def test_compact_keeps_loaded_schema_and_fails_closed_on_changed_scope(surface, monkeypatch):  # noqa: F811
    surface.params.capability_presentation_turn_id = "gateway-turn"
    calls = provider(monkeypatch)
    selected = recommendations.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    requested = compact_surface(surface, selected.selection)
    loaded = replace(requested, loaded_tool_names=(surface.second.model_spec.name,))
    prepared = prepare_conversation_compact_provider_surface(surface.host, loaded, run_id="summary-operation")
    assert surface.second.model_spec.name in {item["name"] for item in prepared.tools}
    observed = []
    changed = replace(requested, allowed_tools=(), capability_presentation_callback=observed.append)
    rejected = prepare_conversation_compact_provider_surface(surface.host, changed, run_id="summary-operation")
    assert rejected.tools is None and rejected.volatile_sections == () and observed == [None]
    assert len(calls) == 1 and surface.first.calls == surface.second.calls == 0


@pytest.mark.parametrize("scope", ["run", "task", "thread", "turn", "missing"])
def test_compact_never_uses_carrier_binding_as_current_scope(surface, monkeypatch, scope):  # noqa: F811
    surface.params.capability_presentation_turn_id = "gateway-turn"
    calls = provider(monkeypatch)
    selected = recommendations.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    observed = []
    requested = compact_surface(surface, selected.selection, observed.append)
    if scope == "turn":
        requested = replace(requested, capability_presentation_turn_id="other-turn")
    elif scope == "missing":
        requested = replace(requested, presentation_context=None)
    else:
        context = requested.presentation_context
        values = {scope + "_id": "other-scope"} if scope != "thread" else {"task_attributes": {"agent_thread_id": "other-thread"}}
        requested = replace(requested, presentation_context=replace(context, **values))
    prepared = prepare_conversation_compact_provider_surface(surface.host, requested, run_id="summary-operation")
    assert observed == [None] and prepared.volatile_sections == () and len(calls) == 1
    assert "method-059" in prepared.stable_prompt_prefix


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_unselected_compact_surface_retains_original_bytes_without_extra_decision(surface, monkeypatch, mode):  # noqa: F811
    patch(surface.host, {"points.skill_tool.mode": mode})
    calls = provider(monkeypatch)
    surface.params.capability_presentation_turn_id = "gateway-turn"
    selected = recommendations.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert selected.selection is None
    requested = compact_surface(surface, None)
    actual = prepare_conversation_compact_provider_surface(surface.host, requested, run_id=surface.params.run_id)
    baseline = prepare_conversation_compact_provider_surface(surface.host, ConversationCompactModelSurface(), run_id=surface.params.run_id)
    assert asdict(actual) == asdict(baseline)
    assert str(conversation_compact_provider_prompt(actual, "压缩要求")) == str(conversation_compact_provider_prompt(baseline, "压缩要求"))
    assert len(calls) == (mode == "observe")


# LLM: 原SimpleAgent.run/RuntimeDB/设置/快照/渲染/Compact均不替换；fake只供应商答复和最后工具循环边界。
# 函数用途: 准备真实Gateway请求与已完成历史，使一次模拟overflow经过原压缩再生成新的DB attempt。
@pytest.fixture
def gateway_surface(tmp_path, tool_surface, skill_catalog_factory):  # noqa: F811
    agent = _agent(tmp_path, context_tokens=1_000_000)
    registry, _, first, second, _ = tool_surface
    catalog, builder = setup_surface(tmp_path / "capabilities", skill_catalog_factory)
    builder.config = agent.config
    agent.tools, agent.prompts, agent.capability_router = registry, builder, catalog.router
    agent.current_skill_snapshot = lambda: catalog.snapshot
    agent.skill_snapshot_for_run_scope = lambda _: catalog.snapshot
    registry.register(SkillSearchTool(agent))
    backend = _SummaryBackend()
    backend.model_name = "test-model"
    agent.backend = backend
    profile, _ = decision(agent)
    patch(agent, {"enabled": True, "profile_id": profile, "points.skill_tool.mode": "apply"})
    request = {**_request(), "id": "gateway-display", "execution_attempt_id": "gateway-turn-1", "status": "processing"}
    conversation = _context(agent, request, request["id"], "核对来源")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(agent, {}, conversation, request_id="old-" + role,
                                                   role=role, content="保留来源事实" + role)
    conversation = _context(agent, request, request["id"], "核对来源")
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    path = paths.processing / (request["id"] + ".json")
    path.write_text(json.dumps(request), encoding="utf-8")
    context = GatewayAskRunContext(agent, request, path, paths.responses / "response.json", request["id"], lambda _: None)
    return SimpleNamespace(agent=agent, context=context, conversation=conversation, first=first, second=second, backend=backend)


# LLM: 捕获真实RuntimeToolLoopParams并使用原renderer/schema；仅用typed response决定模拟压力，不伪造身份或选中值。
# 函数用途: 在同一Gateway请求中留下原始和恢复后的模型面，允许调用方在首次失败后改变原配置。
def capture_gateway_loop(monkeypatch, *, after_overflow=None):
    captured = []
    def execute(service, params):
        prompt = _render_tool_loop_prompt(service._agent, params)
        captured.append((params, prompt, resolve_native_tools(service._agent, params)))
        overflow = len(captured) == 1
        if overflow and after_overflow:
            after_overflow()
        return prompt, ModelResponse(text="继续核对", backend="fixture",
                                     runtime_status="context_overflow" if overflow else "ok"), 0
    monkeypatch.setattr(loop_support.ToolLoopService, "execute", execute)
    return captured


@pytest.mark.parametrize("choice", [None, "not_needed"])
def test_real_gateway_retry_rotates_db_attempt_but_keeps_one_decision_and_compact_surface(gateway_surface, monkeypatch, choice):
    fixture = gateway_surface
    calls = provider(monkeypatch, choice=choice)
    captured = capture_gateway_loop(monkeypatch)
    starting = []
    original_run = fixture.agent.run
    def run(prompt, *, params):
        starting.append((params.capability_presentation, params.capability_presentation_evaluated,
                         params.capability_presentation_turn_id))
        return original_run(prompt, params=params)
    monkeypatch.setattr(fixture.agent, "run", run)
    result, refreshed = request_execution._run_gateway_turn_with_conversation_compact(
        fixture.context, "核对来源", fixture.conversation,
    )
    assert result.runtime_status == "ok" and refreshed.compact_generation == 1
    assert len(calls) == 1 and len(captured) == 2 and fixture.backend.calls == 1
    first, second = captured
    assert first[0].attempt_id != second[0].attempt_id
    repo = fixture.agent.subagents.runtime_db
    assert repo.get_attempt(first[0].attempt_id) is not None and repo.get_attempt(second[0].attempt_id) is not None
    assert first[0].run_id == second[0].run_id == fixture.context.request["runtime_authority"]["run_id"]
    assert first[0].selected_skill_ids == second[0].selected_skill_ids == (() if choice else ("workspace:method-001",))
    assert first[2] == second[2] == fixture.backend.kwargs[0]["tools"]
    first_layout, second_layout = prompt_cache_layout(first[1]), prompt_cache_layout(second[1])
    assert first_layout.stable_prefix == second_layout.stable_prefix == prompt_cache_layout(fixture.backend.prompts[0]).stable_prefix
    cards = dict(first_layout.volatile_sections)["prompt.tool_recommendations"]
    assert cards == dict(second_layout.volatile_sections)["prompt.tool_recommendations"]
    assert cards in [block["text"] for message in fixture.backend.kwargs[0]["messages"] for block in message["content"] if block["type"] == "text"]
    assert "capability_presentation" not in fixture.context.request_path.read_text(encoding="utf-8")
    assert "capability_presentation" not in str(asdict(result))
    assert fixture.first.calls == fixture.second.calls == 0
    assert starting[0] == (None, False, "gateway-turn-1")
    assert starting[1][0] is not None and starting[1][1:] == (True, "gateway-turn-1")
    new_request = {**_request(), "id": "gateway-display-next", "execution_attempt_id": "gateway-turn-2", "status": "processing"}
    new_path = fixture.context.request_path.with_name("gateway-display-next.json")
    new_path.write_text(json.dumps(new_request), encoding="utf-8")
    next_context = replace(fixture.context, request=new_request, request_id=new_request["id"], request_path=new_path)
    next_conversation = _context(fixture.agent, new_request, new_request["id"], "核对来源")
    next_result, _ = request_execution._run_gateway_turn_with_conversation_compact(next_context, "核对来源", next_conversation)
    assert next_result.runtime_status == "ok" and starting[2] == (None, False, "gateway-turn-2")
    assert len(calls) == 2


def test_compact_rejects_then_never_revives_display_or_redecides_after_configuration_recovers(gateway_surface, monkeypatch):
    fixture = gateway_surface
    calls = provider(monkeypatch)
    captured = capture_gateway_loop(monkeypatch, after_overflow=lambda: setattr(fixture.backend, "api_base", "https://changed.example.test"))
    original = fixture.backend.generate
    def summary(prompt, **kwargs):
        response = original(prompt, **kwargs)
        del fixture.backend.api_base
        return response
    monkeypatch.setattr(fixture.backend, "generate", summary)
    result, _ = request_execution._run_gateway_turn_with_conversation_compact(fixture.context, "核对来源", fixture.conversation)
    assert result.runtime_status == "ok" and len(calls) == 1 and len(captured) == 2
    assert captured[0][0].selected_skill_ids == ("workspace:method-001",)
    assert captured[1][0].selected_skill_ids is None
    assert "method-059" in prompt_cache_layout(fixture.backend.prompts[0]).stable_prefix
    assert "method-059" in prompt_cache_layout(captured[1][1]).stable_prefix
    assert fixture.second.model_spec.name in {item["name"] for item in captured[1][2]}


@pytest.mark.parametrize("mode", ["off", "observe", "failed"])
def test_same_gateway_turn_retains_baseline_after_no_selection_without_repeating_decision(gateway_surface, monkeypatch, mode):
    fixture = gateway_surface
    if mode != "failed":
        patch(fixture.agent, {"points.skill_tool.mode": mode})
    calls = provider(monkeypatch, fail=RuntimeError("fake decision failure") if mode == "failed" else None)
    captured = capture_gateway_loop(monkeypatch)
    original = fixture.backend.generate
    def summary(prompt, **kwargs):
        response = original(prompt, **kwargs)
        patch(fixture.agent, {"points.skill_tool.mode": "apply"})
        return response
    monkeypatch.setattr(fixture.backend, "generate", summary)
    result, _ = request_execution._run_gateway_turn_with_conversation_compact(fixture.context, "核对来源", fixture.conversation)
    assert result.runtime_status == "ok" and len(captured) == 2
    assert len(calls) == (mode != "off")
    assert all(params.selected_skill_ids is None for params, _, _ in captured)
    assert captured[0][2] == captured[1][2] == fixture.backend.kwargs[0]["tools"]
    assert prompt_cache_layout(captured[0][1]).stable_prefix == prompt_cache_layout(captured[1][1]).stable_prefix
