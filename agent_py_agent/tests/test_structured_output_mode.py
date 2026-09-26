"""结构化输出方式：DeepSeek 官方 OpenAI 兼容接口拒绝 json_schema（真机 2026-09-26：400 “This response_format type is
unavailable now”），记忆整理与自动总结 Skill 的结构化调用在该默认模型上会整体失败。本测试锁定：方式按 profile 声明、
已知表只对实测确认的供应商改用 json_object 并把 schema 写进提示，其余逐字节保持原生 json_schema；传输全为本地 fake。
"""
import asyncio
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.backends.structured_output_mode import (
    STRUCTURED_OUTPUT_MODES,
    resolved_structured_output,
)
from agent_py_agent.agent.memory_store.curator_backend import call_backend_with_timeout
from agent_py_agent.agent.memory_store.curator_models import curator_response_schema
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError, validate_model
from agent_py_agent.agent.settings.model_scope import _profile_backend
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_provider_sampling import capture_payload

DEEPSEEK = "https://api.deepseek.com"
OPENCODE = "https://opencode.ai/zen/go/v1"
_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
           "additionalProperties": False}


# 函数用途: 造一个 OpenAI 兼容接口的最小运行配置（假密钥、非流式）。
def _config(base, **extra):
    return AgentConfig(model_backend="openai_compatible", model_name="deepseek-v4-flash", api_base=base,
                       api_key="fake", stream_enabled=False, **extra)


# 函数用途: 取出一次请求里第一条用户消息的正文。
def _first_user_text(payload):
    message = next(item for item in payload["messages"] if item["role"] == "user")
    content = message["content"]
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)


@pytest.mark.parametrize(("declared", "base", "backend", "expected"), [
    ("auto", DEEPSEEK, "openai_compatible", "json_object"),
    ("", DEEPSEEK + "/v1", "openai_compatible", "json_object"),
    ("auto", OPENCODE, "openai_compatible", "native"),
    ("auto", "https://example.test/v1", "openai_compatible", "native"),
    ("json_object", "https://example.test/v1", "openai_compatible", "json_object"),
    ("native", DEEPSEEK, "openai_compatible", "native"),
    ("json_object", DEEPSEEK + "/anthropic", "anthropic_compatible", "native"),
    ("auto", DEEPSEEK, "openai_responses", "native"),
])
def test_mode_resolution_uses_declaration_then_verified_hosts(declared, base, backend, expected):
    assert resolved_structured_output(declared, base, backend) == expected


def test_deepseek_structured_request_uses_json_object_with_schema_in_prompt(monkeypatch):
    backend, payload = capture_payload(monkeypatch, _config(DEEPSEEK))

    backend.generate_structured("整理这些记录", response_schema=_SCHEMA)

    assert payload["response_format"] == {"type": "json_object"}
    text = _first_user_text(payload)
    # 供应商不执行 schema，所以 schema 必须完整出现在提示里，原提示保持在后面不变。
    assert "JSON Schema：" + json.dumps(_SCHEMA, ensure_ascii=False, sort_keys=True) in text
    assert text.endswith("整理这些记录")


def test_other_openai_hosts_keep_the_strict_json_schema_request(monkeypatch):
    backend, payload = capture_payload(monkeypatch, _config(OPENCODE))

    backend.generate_structured("整理这些记录", response_schema=_SCHEMA)

    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"] == _SCHEMA
    assert _first_user_text(payload) == "整理这些记录"


def test_declared_mode_overrides_the_known_table(monkeypatch):
    native, strict = capture_payload(monkeypatch, _config(DEEPSEEK, model_structured_output="native"))
    native.generate_structured("x", response_schema=_SCHEMA)
    assert strict["response_format"]["type"] == "json_schema"
    declared, loose = capture_payload(monkeypatch, _config("https://example.test/v1", model_structured_output="json_object"))
    declared.generate_structured("x", response_schema=_SCHEMA)
    assert loose["response_format"] == {"type": "json_object"}


def test_memory_curator_call_on_deepseek_sends_json_object(monkeypatch):
    backend, payload = capture_payload(monkeypatch, _config(DEEPSEEK))

    call_backend_with_timeout(backend, prompt="待提炼经历 JSON：[]", response_schema=curator_response_schema(),
                              timeout_seconds=30)

    assert payload["response_format"] == {"type": "json_object"}
    assert '"schema_version"' in _first_user_text(payload)


def test_profile_structured_output_is_validated_persisted_and_resolved(tmp_path):
    host = Host(tmp_path)
    key, result = add(host, model_backend="openai_compatible", structured_output="json_object")
    plain, _ = add(host, model_backend="openai_compatible")
    assert result["ok"]
    row = next(item for item in execute_model_profile_operation(host, "list", {})["profiles"] if item["id"] == key)
    assert row["structured_output"] == "json_object"
    assert selected_model_config(host, profile_id=key).model_structured_output == "json_object"
    assert selected_model_config(host, profile_id=plain).model_structured_output == "auto"
    base = {"model_name": "m", "provider_id": "p", "model_context_window_tokens": 8192}
    assert "structured_output" not in validate_model({**base, "model_backend": "openai_compatible",
                                                      "structured_output": "auto"})
    with pytest.raises(ModelProfileError, match="结构化输出"):
        validate_model({**base, "model_backend": "openai_compatible", "structured_output": "yaml"})
    with pytest.raises(ModelProfileError, match="OpenAI 兼容"):
        validate_model({**base, "model_backend": "anthropic_compatible", "structured_output": "json_object"})
    with pytest.raises(ModelProfileError, match="决策模型"):
        validate_model({**base, "model_backend": "typesafe_decision", "capability": "decision",
                        "structured_output": "native"})


def test_changing_the_declared_mode_rebuilds_the_cached_backend(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host, model_backend="openai_compatible", api_base=DEEPSEEK)
    before = _profile_backend(host, selected_model_config(host, profile_id=key))
    assert before.structured_output == "json_object"
    row = next(item for item in execute_model_profile_operation(host, "list", {})["profiles"] if item["id"] == key)
    execute_model_profile_operation(host, "save_model", {"profile_id": key, "editing": True,
                                                         "profile": {**row, "structured_output": "native"}})
    after = _profile_backend(host, selected_model_config(host, profile_id=key))
    # 缓存键必须含结构化输出方式，否则改了声明仍复用旧后端。
    assert after is not before and after.structured_output == "native"


def test_manage_models_tool_accepts_the_declared_mode():
    from agent_py_agent.agent.capability.model_profile_tool import _PROFILE_SCHEMA

    assert _PROFILE_SCHEMA["properties"]["structured_output"]["enum"] == list(STRUCTURED_OUTPUT_MODES)


def test_tui_form_prefills_and_saves_structured_output(monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_provider_menu as menu

    radios, sent = [], []
    original = menu.RadioList

    def radio(values, **kwargs):
        widget = original(values, **kwargs)
        radios.append(widget)
        return widget

    async def choose_interface(*args, **kwargs):
        return "openai_compatible"

    async def dialog(*args, **kwargs):
        structured = next(widget for widget in radios if any(value == "json_object" for value, _label in widget.values))
        assert structured.current_value == "json_object"
        structured.current_value = "native"
        return True

    async def request(*args):
        sent.append(args[-1])
        return {"ok": True}

    monkeypatch.setattr(menu, "RadioList", radio)
    monkeypatch.setattr(menu, "_choose_interface", choose_interface)
    monkeypatch.setattr(menu, "_dialog", dialog)
    monkeypatch.setattr(menu, "_request", request)
    result = asyncio.run(menu._edit_model(None, None, "session", "service", {
        "id": "saved-id", "model_name": "deepseek-v4-flash", "structured_output": "json_object"}))
    assert "已保存" in result and sent[0]["profile"]["structured_output"] == "native"


def test_config_default_and_normalization():
    shipped = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    assert shipped.model_structured_output == "auto"
    normalized, _warnings = normalize_agent_config({"model_structured_output": "JSON_OBJECT"})
    assert normalized["model_structured_output"] == "json_object"
    normalized, warnings = normalize_agent_config({"model_structured_output": "yaml"})
    assert normalized["model_structured_output"] == "auto"
    assert any("model_structured_output" in warning for warning in warnings)
