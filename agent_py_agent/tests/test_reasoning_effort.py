"""智能程度（推理强度）：档位换算、真实组包、会话/子代理档位解析、/effort、模型档案字段与配置；传输全部为本地 fake。"""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes
from agent_py_agent.agent.agent_core.parameters import subagent_intent_identity
from agent_py_agent.agent.agent_core.tool_model_generation import _provider_request_options
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.reasoning_control import (
    ReasoningPayloadLimits,
    describe_reasoning_effect,
    normalize_reasoning_level,
    reasoning_payload_fields,
    reasoning_request_values,
    resolved_reasoning_control,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.agent_thread_store import ensure_agent_thread_record
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_model_adoption import _payload, _PayloadSurface
from agent_py_agent.agent.gateway_parts.control_service import execute_gateway_conversation_control
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError, validate_model
from agent_py_agent.agent.settings.reasoning_effort import (
    CHILD_ATTR,
    inherit_reasoning_effort,
    run_reasoning_level,
    set_thread_reasoning_level,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests.test_gateway_conversation_control import _command, _scope
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_provider_sampling import capture_payload

DEEPSEEK = "https://api.deepseek.com"


@pytest.mark.parametrize(("declared", "base", "backend", "expected"), [
    ("auto", DEEPSEEK, "openai_compatible", "effort"),
    ("", DEEPSEEK + "/anthropic", "anthropic_compatible", "budget"),
    ("auto", "https://api.minimaxi.com/anthropic", "anthropic_compatible", "none"),
    ("auto", "https://opencode.ai/zen/go/v1", "openai_compatible", "none"),
    ("budget", "https://api.minimaxi.com/anthropic", "anthropic_compatible", "budget"),
    ("none", DEEPSEEK, "openai_compatible", "none"),
    ("effort", DEEPSEEK, "openai_responses", "effort"),  # 09-30 起 Responses 按 reasoning.effort 发送
    ("auto", "https://chatgpt.com/backend-api/codex", "openai_responses", "effort"),
    ("auto", "https://opencode.ai/zen/go/v1", "openai_responses", "none"),
    ("effort", DEEPSEEK, "typesafe_decision", "none"),
])
def test_control_resolution_uses_declaration_then_verified_hosts(declared, base, backend, expected):
    assert resolved_reasoning_control(declared, base, backend) == expected


@pytest.mark.parametrize(("level", "control", "forced", "expected"), [
    ("auto", "effort", False, (False, "")),
    ("high", "none", False, (False, "")),
    ("high", "none", True, (True, "")),
    ("off", "effort", False, (True, "")),
    ("off", "budget", False, (True, "")),
    ("low", "effort", False, (False, "low")),
    ("max", "budget", False, (False, "max")),
    ("low", "effort", True, (True, "")),
    ("bogus", "effort", False, (False, "")),
])
def test_request_values_keep_forced_tool_choice_first(level, control, forced, expected):
    assert reasoning_request_values(level, control, forced=forced) == expected


def test_payload_fields_per_protocol_and_budget_clamp():
    limits = ReasoningPayloadLimits(16314)
    assert reasoning_payload_fields("effort", "low", "openai", limits) == {"reasoning_effort": "low"}
    assert reasoning_payload_fields("effort", "max", "anthropic", limits) == {"output_config": {"effort": "max"}}
    assert reasoning_payload_fields("budget", "low", "anthropic", limits) == {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    assert reasoning_payload_fields("budget", "max", "anthropic", limits)["thinking"]["budget_tokens"] == 15290
    # 输出上限不足以容纳最小预算及正文预留时，不能把空区间强行抬成 1024。
    assert reasoning_payload_fields("budget", "high", "anthropic", ReasoningPayloadLimits(1500)) == {}
    assert reasoning_payload_fields("budget", "medium", "openai", limits) == {"thinking": {"type": "enabled"}}
    assert reasoning_payload_fields("none", "high", "openai", limits) == {}
    assert "不支持调节" in describe_reasoning_effect("high", "none")
    assert describe_reasoning_effect("auto", "effort").startswith("不额外发送")


def _config(backend, base, **extra):
    return AgentConfig(model_backend=backend, model_name="deepseek-v4-flash", api_base=base, api_key="fake",
                       stream_enabled=False, **extra)


def test_openai_wire_carries_effort_or_disables_thinking(monkeypatch):
    backend, payload = capture_payload(monkeypatch, _config("openai_compatible", DEEPSEEK))
    backend.generate("hi", request_options=ProviderRequestOptions(reasoning_effort="low"))
    assert payload["reasoning_effort"] == "low" and "thinking" not in payload
    payload.clear()
    backend.generate("hi", request_options=ProviderRequestOptions(thinking_disabled=True))
    assert payload["thinking"] == {"type": "disabled"} and "reasoning_effort" not in payload
    assert backend.project_generate_payload("hi", request_options=ProviderRequestOptions(reasoning_effort="max"))[
        "reasoning_effort"] == "max"


def test_openai_history_without_reasoning_content_drops_effort(monkeypatch):
    backend, payload = capture_payload(monkeypatch, _config("openai_compatible", DEEPSEEK))
    tools = [{"name": "read_file", "description": "读文件", "input_schema": {"type": "object", "properties": {}}}]
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]},
    ]
    backend.generate("hi", tools=tools, tool_choice=ToolChoice.auto(), messages=messages,
                     request_options=ProviderRequestOptions(reasoning_effort="high"))
    assert payload["thinking"] == {"type": "disabled"} and "reasoning_effort" not in payload


def test_declared_control_on_unknown_host_really_disables_thinking(monkeypatch):
    config = _config("openai_compatible", "https://example.test/v1", model_reasoning_control="effort")
    backend, payload = capture_payload(monkeypatch, config)
    backend.generate("hi", request_options=ProviderRequestOptions(thinking_disabled=True))
    assert payload["thinking"] == {"type": "disabled"}
    undeclared, plain = capture_payload(monkeypatch, _config("openai_compatible", "https://example.test/v1"))
    undeclared.generate("hi", request_options=ProviderRequestOptions(thinking_disabled=True, reasoning_effort="high"))
    assert "thinking" not in plain and "reasoning_effort" not in plain


def test_anthropic_wire_budget_and_disable(monkeypatch):
    config = _config("anthropic_compatible", "https://api.minimaxi.com/anthropic", model_reasoning_control="budget")
    backend, payload = capture_payload(monkeypatch, config)
    backend.generate("hi", request_options=ProviderRequestOptions(reasoning_effort="medium"))
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 6144}
    payload.clear()
    backend.generate("hi", request_options=ProviderRequestOptions(thinking_disabled=True, reasoning_effort="medium"))
    assert payload["thinking"] == {"type": "disabled"}
    none_backend, none_payload = capture_payload(monkeypatch, _config("anthropic_compatible", "https://api.minimaxi.com/anthropic"))
    none_backend.generate("hi", request_options=ProviderRequestOptions(reasoning_effort="high"))
    assert "thinking" not in none_payload and "output_config" not in none_payload


def _thread_agent(tmp_path, level="", default="auto"):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "u", "channel": "chat", "channel_conversation_id": "c",
                                          "channel_user_id": "u"})
    if level:
        thread = set_thread_reasoning_level(store, thread.thread_id, level)
    agent = SimpleNamespace(conversation_store=store, config=AgentConfig(model_reasoning_effort=default))
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    return agent, params, thread


def test_thread_level_overrides_default_and_clears_back(tmp_path):
    agent, params, thread = _thread_agent(tmp_path, "low", default="high")
    assert run_reasoning_level(agent, params) == "low"
    set_thread_reasoning_level(agent.conversation_store, thread.thread_id, "")
    assert run_reasoning_level(agent, params) == "high"
    assert run_reasoning_level(agent, SimpleNamespace(task_attributes={})) == "high"
    agent.conversation_store = SimpleNamespace(threads=SimpleNamespace(load=lambda _tid: 1 / 0))
    assert run_reasoning_level(agent, params) == "high"
    with pytest.raises(ValueError):
        set_thread_reasoning_level(ConversationStore(tmp_path / "other"), thread.thread_id, "turbo")


def test_generation_options_and_adoption_projection_agree(tmp_path):
    agent, params, _thread = _thread_agent(tmp_path, "max")
    backend = get_backend("openai_compatible", _config("openai_compatible", DEEPSEEK))
    state = SimpleNamespace(agent=agent, params=params, system_instruction="", first_token_timeout_seconds=5)
    options = _provider_request_options(backend, state, forced=False)
    assert (options.thinking_disabled, options.reasoning_effort) == (False, "max")
    forced = _provider_request_options(backend, state, forced=True)
    assert (forced.thinking_disabled, forced.reasoning_effort) == (True, "")
    surface = _PayloadSurface(None, ToolChoice.auto(), None, "", agent, params)
    assert _payload(backend, "hi", surface)["reasoning_effort"] == "max"
    assert backend.project_generate_payload("hi", request_options=options) == _payload(backend, "hi", surface)
    assert _provider_request_options(SimpleNamespace(), state, forced=False) is None


def test_subagent_effort_is_explicit_or_inherited_and_part_of_identity(tmp_path):
    attrs = create_task_attributes({"goal": "查资料", "effort": "LOW"}, None)
    assert attrs[CHILD_ATTR] == {"level": "low"}
    assert create_task_attributes({"goal": "查资料", "attributes": {CHILD_ATTR: {"level": "max"}}}, None)[CHILD_ATTR] == {"level": "auto"}
    with pytest.raises(ValueError, match="effort"):
        create_task_attributes({"goal": "查资料", "effort": "turbo"}, None)
    agent, params, _thread = _thread_agent(tmp_path, "high")
    agent._current_run_params = params
    inherited = {}
    inherit_reasoning_effort(inherited, agent)
    assert inherited[CHILD_ATTR] == {"level": "high"}
    base = subagent_intent_identity({}, {"goal": "查资料"})
    assert subagent_intent_identity({}, {"goal": "查资料", "effort": "low"}) not in {base, ""}
    assert subagent_intent_identity({"effort": "low"}, {"goal": "查资料"}) == subagent_intent_identity({}, {"goal": "查资料", "effort": "low"})


def test_child_thread_is_seeded_once_with_its_level(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    created = ensure_agent_thread_record(store.threads, {"thread_id": "agent-thread-1", "agent_run_id": "run-1",
                                                          "reasoning_effort": "max"})
    again = ensure_agent_thread_record(store.threads, {"thread_id": "agent-thread-1", "agent_run_id": "run-1",
                                                        "reasoning_effort": "low"})
    assert created.reasoning_effort == again.reasoning_effort == "max"
    assert store.threads.load("agent-thread-1").to_dict()["reasoning_effort"] == "max"


def _deepseek_agent(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                                    gateway_per_user_owner_scoping=False), tmp_path / "ws")
    profile_id = str(uuid4())
    result = execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "model_backend": "openai_compatible", "model_name": "deepseek-v4-flash", "api_base": DEEPSEEK,
        "api_key": "fake-key", "model_context_window_tokens": 128000}})
    assert result["ok"]
    execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})
    return agent


def test_effort_command_sets_views_and_resets_with_real_model_effect(tmp_path):
    agent = _deepseek_agent(tmp_path)
    paths = gateway_paths(agent)

    def run(text):
        return execute_gateway_conversation_control(agent, paths, _command(text), _scope())

    view = run("/effort")
    low = run("/effort low")
    reset = run("/effort default")
    helped = run("/effort help")

    assert view.ok and "自动（服务商默认）（全局默认）" in view.message and "不额外发送推理参数" in view.message
    # 查看回执末尾告诉用户能选哪些档位、怎么改（IM 没有选择菜单）；设置回执不重复这行。
    assert view.message.endswith("可选档位：auto 自动（服务商默认）、off 关闭思考、low 低、medium 中、high 高、xhigh 超高、max 最高、ultra 极限。"
                                 "发送 /effort 加档位只改本会话；/effort default 回到全局默认。")
    assert "可选档位" not in low.message and "可选档位" not in reset.message
    assert low.ok and "低（本会话设置）" in low.message
    assert "deepseek-v4-flash" in low.message and "按推理强度档位发送：低" in low.message
    assert reset.ok and "（全局默认）" in reset.message
    assert helped.ok and helped.message.startswith("用法：/effort [auto|off|low|medium|high|xhigh|max|ultra|default]")
    assert "fake-key" not in json.dumps([view.message, low.message, reset.message], ensure_ascii=False)


def _responses_effort_run(root, levels, scope=None):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(root / "home"), prompt_files=[],
                                    gateway_per_user_owner_scoping=False), root / "ws")
    profile_id = str(uuid4())
    profile = {"model_backend": "openai_responses", "model_name": "gpt-test", "api_base": "https://responses.example.invalid/v1",
               "api_key": "fake-key", "model_context_window_tokens": 128000, "reasoning_control": "effort"}
    if levels:
        profile["reasoning_levels"] = list(levels)
    assert execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": profile})["ok"]
    execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})
    paths = gateway_paths(agent)
    return lambda text: execute_gateway_conversation_control(agent, paths, _command(text), scope or _scope()).message


def test_effort_command_receipt_follows_responses_levels(tmp_path):
    # /effort 回执与 Responses 发送同一换算：未声明档位时 off 不发、max 发 high；声明了 max 才说“发送 max”。
    undeclared = _responses_effort_run(tmp_path / "undeclared", ())
    assert "没有声明可关闭思考的档位" in undeclared("/effort off")
    assert "实际发送 high" in undeclared("/effort max")
    declared = _responses_effort_run(tmp_path / "declared", ("low", "medium", "high", "xhigh", "max"))
    assert "（发送 max）" in declared("/effort max")


def test_profile_reasoning_control_is_validated_persisted_and_resolved(tmp_path):
    host = Host(tmp_path)
    key, result = add(host, reasoning_control="budget")
    plain, _ = add(host)
    assert result["ok"]
    row = next(item for item in execute_model_profile_operation(host, "list", {})["profiles"] if item["id"] == key)
    assert row["reasoning_control"] == "budget"
    assert selected_model_config(host, profile_id=key).model_reasoning_control == "budget"
    assert selected_model_config(host, profile_id=plain).model_reasoning_control == "auto"
    assert "reasoning_control" not in validate_model({"model_backend": "openai_compatible", "model_name": "m",
                                                      "provider_id": "p", "model_context_window_tokens": 8192,
                                                      "reasoning_control": "auto"})
    with pytest.raises(ModelProfileError, match="思考控制"):
        validate_model({"model_backend": "openai_compatible", "model_name": "m", "provider_id": "p",
                        "model_context_window_tokens": 8192, "reasoning_control": "turbo"})
    with pytest.raises(ModelProfileError, match="决策模型"):
        validate_model({"model_backend": "typesafe_decision", "model_name": "jev", "provider_id": "p",
                        "capability": "decision", "model_context_window_tokens": 8192, "reasoning_control": "effort"})


def test_tui_form_prefills_and_saves_reasoning_control(monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_provider_menu as menu

    radios, sent = [], []
    original = menu.RadioList

    def radio(values, **kwargs):
        widget = original(values, **kwargs)
        radios.append(widget)
        return widget

    async def choose_interface(*args, **kwargs):
        return "anthropic_compatible"

    async def dialog(*args, **kwargs):
        # 按选项值找「思考控制」单选框：表单里还有接口类型、用途等单选框，不依赖创建顺序。
        reasoning = next(widget for widget in radios if any(value == "budget" for value, _label in widget.values))
        assert reasoning.current_value == "budget"
        reasoning.current_value = "none"
        return True

    async def request(*args):
        sent.append(args[-1])
        return {"ok": True}

    monkeypatch.setattr(menu, "RadioList", radio)
    monkeypatch.setattr(menu, "_choose_interface", choose_interface)
    monkeypatch.setattr(menu, "_dialog", dialog)
    monkeypatch.setattr(menu, "_request", request)
    result = asyncio.run(menu._edit_model(None, None, "session", "service", {
        "id": "saved-id", "model_name": "MiniMax-M3", "reasoning_control": "budget"}))
    assert "已保存" in result and sent[0]["profile"]["reasoning_control"] == "none"


def test_config_defaults_and_normalization(tmp_path):
    shipped = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    assert (shipped.model_reasoning_effort, shipped.model_reasoning_control) == ("auto", "auto")
    normalized, warnings = normalize_agent_config({"model_reasoning_effort": "HIGH", "model_reasoning_control": "turbo"})
    assert normalized["model_reasoning_effort"] == "high" and normalized["model_reasoning_control"] == "auto"
    assert any("model_reasoning_control" in warning for warning in warnings)


@pytest.mark.parametrize("level", ["xhigh", "ultra"])
@pytest.mark.parametrize("control", ["effort", "budget", "none"])
def test_extended_levels_keep_control_and_forced_choice_contract(level, control):
    assert normalize_reasoning_level(f" {level.upper()} ") == level
    assert reasoning_request_values(level, control, forced=False) == (False, level if control != "none" else "")
    assert reasoning_request_values(level, control, forced=True) == (True, "")


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic_compatible"])
@pytest.mark.parametrize("case", [
    ("xhigh", (), "high"), ("ultra", (), "max"),
    ("xhigh", ("high", "xhigh", "max", "ultra"), "high"),
    ("ultra", ("high", "xhigh", "max", "ultra"), "max"),
    ("ultra", ("high",), "high"), ("xhigh", ("ultra",), ""),
])
def test_extended_effort_wire_uses_protocol_and_declared_levels(monkeypatch, protocol, case):
    level, levels, expected = case
    config = _config(protocol, "https://example.test/v1", model_reasoning_control="effort", model_reasoning_levels=list(levels))
    backend, payload = capture_payload(monkeypatch, config)
    backend.generate("hi", request_options=ProviderRequestOptions(reasoning_effort=level))
    sent = payload.get("reasoning_effort", "") if protocol == "openai_compatible" else payload.get("output_config", {}).get("effort", "")
    assert sent == expected
    effect = describe_reasoning_effect(level, "effort", protocol="openai" if protocol == "openai_compatible" else "anthropic", levels=levels)
    assert f"发送 {expected}" in effect if expected else "不改变请求" in effect
    assert backend.project_generate_payload("hi", request_options=ProviderRequestOptions(reasoning_effort=level)) == payload


@pytest.mark.parametrize("case", [("xhigh", 65536, 24576), ("ultra", 65536, 64512),
                                  ("xhigh", 16314, 15290), ("ultra", 1500, None)])
@pytest.mark.parametrize("levels", [(), ("high", "xhigh", "max", "ultra")])
def test_extended_budget_values_and_existing_clamp(case, levels):
    level, cap, expected = case
    context = ReasoningPayloadLimits(cap, levels)
    expected_fields = {} if expected is None else {"thinking": {"type": "enabled", "budget_tokens": expected}}
    assert reasoning_payload_fields("budget", level, "anthropic", context) == expected_fields
    assert reasoning_payload_fields("budget", level, "openai", context) == {"thinking": {"type": "enabled"}}
    assert reasoning_payload_fields("none", level, "anthropic", context) == {}


@pytest.mark.parametrize("case", [("xhigh", 24576), ("ultra", 64512)])
@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic_compatible"])
@pytest.mark.parametrize("levels", [(), ("high", "xhigh", "max", "ultra")])
def test_extended_budget_actual_wire_and_projection_agree(monkeypatch, case, protocol, levels):
    level, budget = case
    config = _config(protocol, "https://example.test/v1", model_reasoning_control="budget",
                     model_reasoning_levels=list(levels), max_tokens=65536, model_context_window_tokens=262144)
    backend, payload = capture_payload(monkeypatch, config)
    options = ProviderRequestOptions(reasoning_effort=level)
    backend.generate("hi", request_options=options)
    expected = {"type": "enabled", "budget_tokens": budget} if protocol == "anthropic_compatible" else {"type": "enabled"}
    assert payload["thinking"] == expected and "reasoning_effort" not in payload and "output_config" not in payload
    assert backend.project_generate_payload("hi", request_options=options) == payload


@pytest.mark.parametrize("level", ["xhigh", "ultra"])
@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic_compatible"])
def test_extended_level_reaches_thread_child_and_gateway_projection(tmp_path, level, protocol):
    assert normalize_reasoning_level(level) == level
    agent, params, thread = _thread_agent(tmp_path, level)
    attrs = create_task_attributes({"goal": "查资料", "effort": level}, None)
    assert attrs[CHILD_ATTR] == {"level": level}
    store = agent.conversation_store
    child = ensure_agent_thread_record(store.threads, {"thread_id": "child-1", "agent_run_id": "run-1", "reasoning_effort": level})
    assert child.reasoning_effort == level and store.threads.load(thread.thread_id).reasoning_effort == level
    backend = get_backend(protocol, _config(protocol, "https://example.test/v1", model_reasoning_control="effort",
                           model_reasoning_levels=["high", "xhigh", "max", "ultra"]))
    state = SimpleNamespace(agent=agent, params=params, system_instruction="", first_token_timeout_seconds=5)
    options = _provider_request_options(backend, state, forced=False)
    projected = _payload(backend, "hi", _PayloadSurface(None, ToolChoice.auto(), None, "", agent, params))
    expected = "high" if level == "xhigh" else "max"
    sent = projected.get("reasoning_effort") if protocol == "openai_compatible" else projected["output_config"]["effort"]
    assert options.reasoning_effort == level and sent == expected
    assert projected == backend.project_generate_payload("hi", request_options=options)


def test_responses_adoption_still_rejects_unavailable_projection(tmp_path):
    # Responses 当前没有容量投影，不能为 C7 冒充 Chat 载荷而扩大自动采用范围；其发送字段由真实组包测试覆盖。
    agent, params, _thread = _thread_agent(tmp_path, "ultra")
    backend = get_backend("openai_responses", _config("openai_responses", "https://chatgpt.com/backend-api/codex"))
    with pytest.raises(ValueError, match="provider_request_surface_unknown"):
        _payload(backend, "hi", _PayloadSurface(None, ToolChoice.auto(), None, "", agent, params))


@pytest.mark.parametrize("channel", ["chat", "feishu"])
@pytest.mark.parametrize("case", [("xhigh", ("high", "xhigh", "max", "ultra"), "xhigh"),
                                  ("ultra", ("high", "xhigh", "max", "ultra"), "ultra"),
                                  ("ultra", ("high", "xhigh", "max"), "max"), ("xhigh", (), "high")])
def test_extended_effort_command_on_tui_and_im_reports_actual_wire(tmp_path, channel, case):
    level, levels, expected = case
    run = _responses_effort_run(tmp_path, levels, replace(_scope(), channel=channel))
    message = run(f"/effort {level}")
    assert "本会话设置" in message and f"发送 {expected}" in message
    assert f"发送 {expected}" in run("/effort")
    assert "全局默认" in run("/effort default")


@pytest.mark.parametrize("level", ["xhigh", "ultra"])
def test_user_config_accepts_extended_default_and_reports_mapping(tmp_path, monkeypatch, level):
    user_path = tmp_path / "user.yaml"
    user_path.write_text("model_reasoning_effort: auto\n", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_CONFIG", str(user_path))
    agent = _deepseek_agent(tmp_path)
    agent.config = selected_model_config(agent)
    tool = UserConfigTool(agent)
    result = tool.execute({"action": "set", "key": "model_reasoning_effort", "value": level})
    assert result.ok, result.output
    assert load_config(user_path).model_reasoning_effort == level
    assert "restart_gateway" in result.output
    assert f"实际发送 {'high' if level == 'xhigh' else 'max'}" in json.loads(result.output)["applied_value"]


def test_orchestration_native_schema_has_all_eight_levels():
    from agent_py_agent.agent.agent_core.orchestration.tool_spec_schemas import (
        _CREATE_PARAMETER_SCHEMA,
    )

    expected = ["auto", "off", "low", "medium", "high", "xhigh", "max", "ultra"]
    assert _CREATE_PARAMETER_SCHEMA["effort"]["enum"] == expected
    assert _CREATE_PARAMETER_SCHEMA["items"]["items"]["properties"]["effort"]["enum"] == expected
