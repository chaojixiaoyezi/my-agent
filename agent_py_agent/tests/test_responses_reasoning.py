"""Responses 接口的智能程度：按模型档案声明的服务商档位换算 reasoning.effort；ChatGPT 订阅目录的档位随勾选带入档案。"""
import json
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import BackendOptions, get_backend
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.reasoning_control import (
    REASONING_LEVELS,
    describe_config_reasoning_effect,
    resolved_reasoning_control,
    responses_reasoning_field,
)
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.settings import model_provider_network as network
from agent_py_agent.agent.settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.tests.test_model_profiles import Host

LUNA = ("low", "medium", "high", "xhigh", "max")
WITH_NONE = ("none", "low", "medium", "high")


@pytest.mark.parametrize(("level", "levels", "disabled", "expected"), [
    ("low", LUNA, False, "low"),
    ("max", LUNA, False, "max"),
    ("max", ("low", "medium", "high", "xhigh"), False, "xhigh"),
    ("max", (), False, "high"),  # 未声明档位：最高只发通用的 high
    ("medium", (), False, "medium"),
    ("off", LUNA, False, ""),  # 模型没声明 none/minimal：不发，交服务商默认
    ("", WITH_NONE, True, "none"),  # 强制工具选择或 /effort off 折成 disabled
    ("auto", LUNA, False, ""),
    ("", LUNA, False, ""),
])
def test_level_mapping_uses_only_declared_or_generic_efforts(level, levels, disabled, expected):
    field = responses_reasoning_field("effort", level, levels, disabled=disabled)
    assert field == ({"reasoning": {"effort": expected}} if expected else {})
    assert responses_reasoning_field("none", level, levels, disabled=disabled) == {}


def payload_for(monkeypatch, *, control, levels, effort):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "key", "model", stream_enabled=False,
                                                    reasoning_control=control, reasoning_levels=levels))
    sent = []
    monkeypatch.setattr(backend, "request_json", lambda path, payload, headers: sent.append(payload) or {
        "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "好"}]}]})
    backend.generate("你好", request_options=ProviderRequestOptions(reasoning_effort=effort))
    return sent[0]


def test_payload_carries_reasoning_effort_only_when_supported(monkeypatch):
    assert payload_for(monkeypatch, control="effort", levels=LUNA, effort="max")["reasoning"] == {"effort": "max"}
    assert payload_for(monkeypatch, control="effort", levels=(), effort="high")["reasoning"] == {"effort": "high"}
    assert "reasoning" not in payload_for(monkeypatch, control="none", levels=LUNA, effort="max")
    assert "reasoning" not in payload_for(monkeypatch, control="effort", levels=LUNA, effort="")


def test_subscription_catalog_levels_flow_into_the_profile_and_backend(tmp_path, monkeypatch):
    host = Host(tmp_path)
    execute_model_profile_operation(host, "save_provider", {"provider_id": "account", "provider": {
        "display_name": "ChatGPT 订阅", "api_base": "https://chatgpt.com/backend-api/codex", "api_key": "k"}})
    item = {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "context_window": 272000, "visibility": "list",
            "supported_reasoning_levels": [{"effort": level, "description": "d"} for level in LUNA]}
    row = network._subscription_row(item)
    assert row["reasoning_levels"] == list(LUNA)
    assert "reasoning_levels" not in network._subscription_row({**item, "supported_reasoning_levels": [{"effort": "Bad Level"}]})
    key = str(uuid4())
    execute_model_profile_operation(host, "add_models", {"provider_id": "account", "model_backend": "openai_responses", "models": [
        {"profile_id": key, "model_name": row["model_name"], "model_context_window_tokens": row["model_context_window_tokens"],
         "reasoning_levels": row["reasoning_levels"]}]})
    config = selected_model_config(host, profile_id=key)
    assert config.model_reasoning_levels == list(LUNA)
    assert resolved_reasoning_control(config.model_reasoning_control, config.api_base, config.model_backend) == "effort"
    backend = get_backend(config.model_backend, config)
    assert backend.reasoning_levels == LUNA and backend.reasoning_control == "effort"


def test_profiles_without_levels_resolve_to_an_empty_list_and_bad_levels_are_rejected(tmp_path):
    host = Host(tmp_path)
    result = execute_model_profile_operation(host, "add_models", {"connection": {
        "model_backend": "openai_compatible", "api_base": "https://api.example.test/v1", "api_key": "k",
        "custom_headers": {}, "session_header": ""}, "models": [
        {"profile_id": str(uuid4()), "model_name": "m", "model_context_window_tokens": 128000}]})
    key = next(row["id"] for row in result["profiles"] if row["model_name"] == "m")
    assert selected_model_config(host, profile_id=key).model_reasoning_levels == []
    with pytest.raises(ModelProfileError, match="思考档位"):
        execute_model_profile_operation(host, "add_models", {"connection": {
            "model_backend": "openai_compatible", "api_base": "https://api.example.test/v1", "api_key": "k",
            "custom_headers": {}, "session_header": ""}, "models": [
            {"profile_id": str(uuid4()), "model_name": "n", "model_context_window_tokens": 128000, "reasoning_levels": ["High!"]}]})
    assert "reasoning_levels" not in json.dumps(result["profiles"])


def test_re_adding_an_existing_model_only_refreshes_its_declared_levels(tmp_path):
    host = Host(tmp_path)
    execute_model_profile_operation(host, "save_provider", {"provider_id": "account", "provider": {
        "display_name": "ChatGPT 订阅", "api_base": "https://chatgpt.com/backend-api/codex", "api_key": "k"}})
    key = str(uuid4())
    base = {"model_name": "gpt-6-luna", "model_context_window_tokens": 272000}
    execute_model_profile_operation(host, "add_models", {"provider_id": "account", "model_backend": "openai_responses",
                                                          "models": [{"profile_id": key, **base}]})
    again = execute_model_profile_operation(host, "add_models", {"provider_id": "account", "model_backend": "openai_responses",
        "models": [{"profile_id": str(uuid4()), **base, "model_context_window_tokens": 999999, "reasoning_levels": list(LUNA)}]})
    assert again["added_models"] == []
    row = next(row for row in again["profiles"] if row["id"] == key)
    assert row["reasoning_levels"] == list(LUNA) and row["model_context_window_tokens"] == 272000
    assert len([row for row in again["profiles"] if row.get("model_name") == "gpt-6-luna"]) == 1


def _chatgpt(levels=()):
    from types import SimpleNamespace

    return SimpleNamespace(model_backend="openai_responses", api_base="https://chatgpt.com/backend-api/codex",
                           model_reasoning_control="auto", model_reasoning_levels=list(levels))


def test_receipt_tells_the_truth_about_off_and_max_on_chatgpt():
    # 2026-09-30 真实核对：订阅目录没有任何模型声明 none/minimal，旧档案没有 reasoning_levels 时 max 实际发 high。
    assert "不改变请求" in describe_config_reasoning_effect("off", _chatgpt())
    assert "不改变请求" in describe_config_reasoning_effect("off", _chatgpt(LUNA))
    assert "实际发送 high" in describe_config_reasoning_effect("max", _chatgpt())
    assert describe_config_reasoning_effect("max", _chatgpt(LUNA)).endswith("（发送 max）。")
    assert "关闭思考（发送 none）" in describe_config_reasoning_effect("off", _chatgpt(WITH_NONE))
    assert "没有对应「低」" in describe_config_reasoning_effect("low", _chatgpt(("xhigh", "max")))


@pytest.mark.parametrize("levels", [(), LUNA, WITH_NONE, ("low", "medium", "high", "xhigh"), ("xhigh", "max")])
def test_receipt_names_the_effort_actually_sent(levels):
    # 回执与发送必须同一换算：回执里写出的服务商档位就是请求体里的 reasoning.effort，不发字段时回执说“不改变请求”。
    for level in REASONING_LEVELS:
        if level == "auto":
            continue
        text = describe_config_reasoning_effect(level, _chatgpt(levels))
        sent = responses_reasoning_field("effort", level, levels, disabled=level == "off").get("reasoning", {}).get("effort", "")
        if sent:
            assert f"发送 {sent}" in text and "不改变请求" not in text
        else:
            assert "不改变请求" in text and "发送" not in text.replace("不额外发送", "")
