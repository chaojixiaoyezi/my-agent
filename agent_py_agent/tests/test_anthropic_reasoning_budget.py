"""小输出上限的预算合同；真实工厂、组包与控制回执，传输仅用本地 fake。"""
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import gateway_helpers
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.reasoning_control import (
    describe_config_reasoning_effect,
    describe_reasoning_effect,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import execute_gateway_conversation_control
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests.test_gateway_conversation_control import _command, _scope
from agent_py_agent.tests.test_provider_sampling import capture_payload

_BOUNDARIES = [(1024, None), (2047, None), (2048, 1024), (2049, 1025)]
_BUDGET_LEVELS = ["low", "medium", "high", "xhigh", "max", "ultra"]


# LLM: 所有实际 HTTP 入口默认拒绝，测试必须显式替换传输；未截获的流式请求不能悄悄访问外部网络。
# 函数用途: 让夹具遗漏直接在本地失败，避免把网络错误当成预算行为证据。
@pytest.fixture(autouse=True)
def _forbid_real_provider_transport(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("预算测试没有替换真实 provider 传输")

    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", forbidden)


# LLM: 用窗口夹取制造实际输出 cap，而不是直接修改后端；配置和传输没有真实凭据或服务。
# 函数用途: 准备经生产工厂派生上限的预算模型配置。
def _budget_config(cap):
    return AgentConfig(model_backend="anthropic_compatible", model_name="fixture-budget",
                       api_base="https://budget.example.invalid", api_key="fake", stream_enabled=False,
                       model_reasoning_control="budget", model_context_window_tokens=cap * 4, max_tokens=65536)


# LLM: 断言仅验证回执，不能把文字当发送事实；调用方另外验证 fake 传输截获的完整载荷。
# 函数用途: 要求实际预算或未发送原因准确出现在用户说明中。
def _assert_receipt(message, cap, budget):
    if budget is None:
        assert "未发送 thinking" in message
        assert "reasoning_budget_interval_empty" in message
        assert str(cap) in message
        assert "发送 thinking.enabled" not in message
        return
    assert f"budget_tokens={budget}" in message
    assert "reasoning_budget_interval_empty" not in message


@pytest.mark.parametrize("level", _BUDGET_LEVELS)
@pytest.mark.parametrize("case", _BOUNDARIES)
def test_factory_budget_boundaries_keep_wire_and_projection_exact(monkeypatch, level, case):
    cap, budget = case
    config = _budget_config(cap)
    backend, wire = capture_payload(monkeypatch, config)
    options = ProviderRequestOptions(reasoning_effort=level)
    assert backend.generate("fixture", request_options=options).text == "ok"
    assert backend.max_tokens == wire["max_tokens"] == cap
    assert config.max_tokens == 65536 and config.model_context_window_tokens == cap * 4
    if budget is None:
        assert "thinking" not in wire
    else:
        assert wire["thinking"] == {"type": "enabled", "budget_tokens": budget}
        assert 1024 <= budget <= wire["max_tokens"] - 1024
    assert "reason_code" not in wire and "reasoning_budget_interval_empty" not in json.dumps(wire)
    assert backend.project_generate_payload("fixture", request_options=options) == wire


@pytest.mark.parametrize("case", _BOUNDARIES)
def test_stream_budget_wire_matches_the_same_boundary_projection(monkeypatch, case):
    cap, budget = case
    backend, wire = capture_payload(monkeypatch, replace(_budget_config(cap), stream_enabled=True))

    def stream(request):
        # 截获真实传输已经合入 stream=True 的载荷，不在传输前的后端入口伪造这个字段。
        wire.update(request.payload)
        return [json.dumps(event) for event in [
            {"type": "message_start", "message": {"content": [], "usage": {"input_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]]

    monkeypatch.setattr(gateway_helpers, "_stream_with_watchdog", stream)
    options = ProviderRequestOptions(reasoning_effort="ultra")
    assert backend.generate("fixture", request_options=options).text == "ok"
    assert wire["stream"] is True and wire["max_tokens"] == cap
    assert wire.get("thinking") == (None if budget is None else {"type": "enabled", "budget_tokens": budget})
    assert backend.project_generate_payload("fixture", request_options=options) == wire


@pytest.mark.parametrize("level", _BUDGET_LEVELS)
@pytest.mark.parametrize("case", _BOUNDARIES)
def test_config_budget_receipt_uses_the_same_derived_output_cap(level, case):
    cap, budget = case
    _assert_receipt(describe_config_reasoning_effect(level, _budget_config(cap)), cap, budget)


@pytest.mark.parametrize("case", [("low", 2048), ("medium", 6144), ("high", 12288),
                                  ("xhigh", 24576), ("max", 64512), ("ultra", 64512)])
def test_large_output_budget_stays_unchanged_and_receipt_matches(monkeypatch, case):
    level, budget = case
    config = _budget_config(65536)
    backend, wire = capture_payload(monkeypatch, config)
    backend.generate("fixture", request_options=ProviderRequestOptions(reasoning_effort=level))
    assert wire["max_tokens"] == 65536
    assert wire["thinking"] == {"type": "enabled", "budget_tokens": budget}
    _assert_receipt(describe_config_reasoning_effect(level, config), 65536, budget)


@pytest.mark.parametrize("control", ["effort", "budget", "none"])
@pytest.mark.parametrize("disabled", [False, True])
def test_small_output_cap_keeps_non_budget_and_disabled_priority(monkeypatch, control, disabled):
    config = replace(_budget_config(1024), model_reasoning_control=control)
    backend, wire = capture_payload(monkeypatch, config)
    options = ProviderRequestOptions(reasoning_effort="ultra", thinking_disabled=disabled)
    backend.generate("fixture", request_options=options)
    expected = {"type": "disabled"} if disabled else None
    assert wire.get("thinking") == expected and wire["max_tokens"] == 1024
    assert wire.get("output_config") == ({"effort": "max"} if control == "effort" and not disabled else None)
    assert backend.project_generate_payload("fixture", request_options=options) == wire


# LLM: 所有模型档案写入都在 tmp_path 下的隔离 home；控制命令不启动 Gateway，也不调用真实模型。
# 函数用途: 准备走真实模型选择服务的用户回执夹具。
def _budget_agent(tmp_path, cap):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[], stream_enabled=False,
                                    gateway_per_user_owner_scoping=False), tmp_path / "ws")
    config = _budget_config(cap)
    profile_id = str(uuid4())
    profile = {"model_backend": config.model_backend, "model_name": config.model_name, "api_base": config.api_base,
               "api_key": "fake", "model_context_window_tokens": config.model_context_window_tokens,
               "reasoning_control": "budget"}
    assert execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": profile})["ok"]
    assert execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})["ok"]
    return agent


@pytest.mark.parametrize("channel", ["chat", "feishu"])
@pytest.mark.parametrize("case", _BOUNDARIES)
def test_tui_im_control_receipts_and_real_selected_backend_agree(tmp_path, monkeypatch, channel, case):
    cap, budget = case
    agent = _budget_agent(tmp_path, cap)
    paths, scope = gateway_paths(agent), replace(_scope(), channel=channel)
    changed = execute_gateway_conversation_control(agent, paths, _command("/effort ultra"), scope)
    viewed = execute_gateway_conversation_control(agent, paths, _command("/effort"), scope)
    assert changed.ok and viewed.ok and "极限（本会话设置）" in changed.message
    config = selected_model_config(agent)
    effect = describe_config_reasoning_effect("ultra", config)
    assert effect in changed.message and effect in viewed.message
    _assert_receipt(effect, cap, budget)
    backend, wire = capture_payload(monkeypatch, config)
    backend.generate("fixture", request_options=ProviderRequestOptions(reasoning_effort="ultra"))
    assert wire["max_tokens"] == cap
    assert wire.get("thinking") == (None if budget is None else {"type": "enabled", "budget_tokens": budget})


@pytest.mark.parametrize("case", _BOUNDARIES)
def test_parameter_receipt_uses_selected_model_budget_facts(tmp_path, monkeypatch, case):
    cap, budget = case
    path = tmp_path / "user.yaml"
    path.write_text("model_reasoning_effort: auto\n", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_CONFIG", str(path))
    agent = _budget_agent(tmp_path, cap)
    agent.config = selected_model_config(agent)
    result = UserConfigTool(agent).execute({"action": "set", "key": "model_reasoning_effort", "value": "ultra"})
    assert result.ok, result.output
    _assert_receipt(json.loads(result.output)["applied_value"], cap, budget)


def test_generic_budget_receipt_is_conditional_without_request_limits():
    effect = describe_reasoning_effect("ultra", "budget", protocol="anthropic")
    assert "预算区间为空时不发送 thinking" in effect
    assert "budget_tokens=" not in effect
