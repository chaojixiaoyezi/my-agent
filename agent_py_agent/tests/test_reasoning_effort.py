"""智能程度（推理强度）：档位换算、真实组包、会话/子代理档位解析、/effort、模型档案字段与配置；传输全部为本地 fake。"""
import asyncio
import json
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
    describe_reasoning_effect,
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
    ("effort", DEEPSEEK, "openai_responses", "none"),
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
    assert reasoning_payload_fields("effort", "low", "openai", 16314) == {"reasoning_effort": "low"}
    assert reasoning_payload_fields("effort", "max", "anthropic", 16314) == {"output_config": {"effort": "max"}}
    assert reasoning_payload_fields("budget", "low", "anthropic", 16314) == {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    assert reasoning_payload_fields("budget", "max", "anthropic", 16314)["thinking"]["budget_tokens"] == 15290
    assert reasoning_payload_fields("budget", "high", "anthropic", 1500)["thinking"]["budget_tokens"] == 1024
    assert reasoning_payload_fields("budget", "medium", "openai", 16314) == {"thinking": {"type": "enabled"}}
    assert reasoning_payload_fields("none", "high", "openai", 16314) == {}
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
    assert low.ok and "低（本会话设置）" in low.message
    assert "deepseek-v4-flash" in low.message and "按推理强度档位发送：低" in low.message
    assert reset.ok and "（全局默认）" in reset.message
    assert helped.ok and helped.message.startswith("用法：/effort [auto|off|low|medium|high|max|default]")
    assert "fake-key" not in json.dumps([view.message, low.message, reset.message], ensure_ascii=False)


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
        assert radios[0].current_value == "budget"
        radios[0].current_value = "none"
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
