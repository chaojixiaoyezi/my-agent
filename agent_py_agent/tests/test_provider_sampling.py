"""采样配置的保存、冻结及真实组包路径；所有传输替换为本地 fake，不访问模型。"""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.sampling import (
    chat_top_p,
    uses_deepseek_flash_sampling,
    validate_top_p,
)
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.settings.model_profiles import (
    inherit_model_profile,
    inherited_model_config,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import migrate_v1
from agent_py_agent.agent.settings.model_scope import _profile_backend, selected_model_scope
from agent_py_agent.agent.settings.services.runtime_config_task import (
    apply_task_runtime_config_overlay,
)
from agent_py_agent.tests.test_model_profiles import Host, add, profile
from agent_py_agent.tests.test_model_provider_management import model, op, provider


@pytest.mark.parametrize("value, expected", [(None, None), ("", None), (0, 0.0), ("0.95", 0.95), (1, 1.0)])
def test_valid_top_p(value, expected):
    assert validate_top_p(value) == expected


@pytest.mark.parametrize("value", [True, {}, [], "private-secret", "nan", float("inf"), -0.1, 1.1, 10 ** 500])
def test_invalid_top_p_is_redacted(value):
    with pytest.raises(ValueError, match="top_p 须留空或填写 0 至 1 的有限数值。"):
        validate_top_p(value)


def test_top_p_config_default_and_yaml_normalization(tmp_path):
    assert AgentConfig().top_p is None
    assert load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml").top_p is None
    path = tmp_path / "agent.yaml"
    path.write_text("top_p: 0.97\n", encoding="utf-8")
    assert load_config(path).top_p == 0.97
    normalized, warnings = normalize_agent_config({"top_p": "0.98"})
    assert normalized["top_p"] == 0.98 and not warnings
    normalized, warnings = normalize_agent_config({"top_p": "private-secret"})
    assert normalized["top_p"] is None
    assert any("top_p" in warning for warning in warnings)
    assert "private-secret" not in str(warnings)


def test_v1_migration_preserves_explicit_sampling():
    before = {"selected": "same-id", "profiles": {"same-id": profile(top_p=0.97, temperature=0.4)}}
    result = migrate_v1(before)
    assert result["selected"] == "same-id"
    assert result["profiles"]["same-id"]["top_p"] == 0.97
    assert result["profiles"]["same-id"]["temperature"] == "0.4"
    assert before["profiles"]["same-id"]["temperature"] == 0.4


@pytest.mark.parametrize("base", ["https://api.deepseek.com", "https://api.deepseek.com/v1/",
                                  "https://opencode.ai/zen/go/v1", "https://opencode.ai/zen/v1"])
@pytest.mark.parametrize("name", ["deepseek-v4-flash", "deepseek-v4-flash-0731", "deepseek-v4-flash:0731"])
def test_known_flash_defaults_and_protocol_lower_bound(base, name):
    assert uses_deepseek_flash_sampling(base, name)
    assert chat_top_p(base, name, None) == 0.95
    assert chat_top_p(base, name, 0.98) == 0.98
    assert chat_top_p(base, name, 0.7) == 0.95
    assert chat_top_p(base, name, 0.98, thinking_disabled=True) == 1.0


@pytest.mark.parametrize("base, name", [
    ("http://api.deepseek.com/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com:8443/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com.evil.test/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com/private/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com/v1?private=1", "deepseek-v4-flash"),
    ("https://opencode.ai/custom/v1", "deepseek-v4-flash"),
    ("https://proxy.test/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com/v1", "deepseek-v4-pro"),
    ("https://opencode.ai/zen/go/v1", "custom-deepseek-v4-flash"),
    ("https://opencode.ai/zen/go/v1", "deepseek-v4-flash-new-version"),
])
def test_unknown_endpoints_and_models_keep_explicit_only(base, name):
    assert not uses_deepseek_flash_sampling(base, name)
    assert chat_top_p(base, name, None) is None
    assert chat_top_p(base, name, 0.7) == 0.7


# LLM: 仅截获生产 generate 组装好的请求；不请求端口、不改模型调用合同。
# 函数用途: 用协议合法的最小回复验证出站采样字段，不把 fake 当真实 TUI 验收。
def capture_payload(monkeypatch, config):
    backend = get_backend(config.model_backend, config)
    captured = {}

    def request_json(path, payload, headers):
        captured.update(payload)
        if config.model_backend == "anthropic_compatible":
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        if config.model_backend == "openai_responses":
            return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}]}]}
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(backend, "request_json", request_json)
    return backend, captured


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic_compatible", "openai_responses"])
@pytest.mark.parametrize("top_p", [None, 0.0, 0.82, 1.0])
def test_three_protocols_explicit_top_p_reaches_wire(monkeypatch, protocol, top_p):
    config = AgentConfig(model_backend=protocol, model_name="custom", api_base="https://example.test/v1",
                         api_key="fake", stream_enabled=False, top_p=top_p)
    backend, payload = capture_payload(monkeypatch, config)
    assert backend.generate("hi").text == "ok"
    if top_p is None:
        assert "top_p" not in payload
    else:
        assert payload["top_p"] == top_p


@pytest.mark.parametrize("explicit", [False, True])
def test_chat_flash_default_preserves_explicit_temperature(monkeypatch, explicit):
    config = AgentConfig(model_backend="openai_compatible", model_name="deepseek-v4-flash",
                         api_base="https://opencode.ai/zen/go/v1", api_key="fake", stream_enabled=False,
                         temperature="0.4", model_temperature_explicit=explicit)
    backend, payload = capture_payload(monkeypatch, config)
    backend.generate("hi")
    assert payload["top_p"] == 0.95
    assert ("temperature" in payload) is explicit
    if explicit:
        assert payload["temperature"] == 0.4


def test_disabled_thinking_flash_sampling_is_request_scoped(monkeypatch):
    config = AgentConfig(model_backend="openai_compatible", model_name="deepseek-v4-flash",
                         api_base="https://api.deepseek.com/v1", api_key="fake", stream_enabled=False)
    backend, payload = capture_payload(monkeypatch, config)
    backend.generate("hi", request_options=ProviderRequestOptions(thinking_disabled=True))
    assert payload["top_p"] == 1.0 and payload["thinking"] == {"type": "disabled"}
    payload.clear()
    backend.generate("hi")
    assert payload["top_p"] == 0.95 and "thinking" not in payload


def test_stream_and_non_stream_share_sampling_payload(monkeypatch):
    config = AgentConfig(model_backend="openai_compatible", model_name="deepseek-v4-flash",
                         api_base="https://opencode.ai/zen/go/v1", api_key="fake", stream_enabled=False, top_p=0.98)
    backend, expected = capture_payload(monkeypatch, config)
    backend.generate("hi")
    stream_backend = get_backend(config.model_backend, replace(config, stream_enabled=True))
    observed = {}

    def request_stream_iter(path, payload, headers):
        observed.update(payload)
        yield json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        yield "[DONE]"

    monkeypatch.setattr(stream_backend, "request_stream_iter", request_stream_iter)
    assert stream_backend.generate("hi", on_chunk=lambda _: None).text == "ok"
    assert observed["top_p"] == expected["top_p"] == 0.98
    assert "temperature" not in observed and "temperature" not in expected
    assert observed["messages"] == expected["messages"]


def test_profile_save_clear_and_backend_freeze(tmp_path):
    host = Host(tmp_path)
    host.config.top_p = 0.7
    provider(host)
    key = model(host, top_p="0.97")
    op(host, "select", {"profile_id": key})
    first = selected_model_config(host)
    before = _profile_backend(host, first)
    assert first.top_p == before.top_p == 0.97
    row = next(row for row in op(host, "list", {})["profiles"] if row["id"] == key)
    assert row["top_p"] == 0.97
    op(host, "save_model", {"profile_id": key, "editing": True, "profile": {**row, "top_p": 0.98}})
    after = _profile_backend(host, selected_model_config(host))
    assert after is not before and after.top_p == 0.98 and before.top_p == 0.97
    assert _profile_backend(host, first) is before
    op(host, "save_model", {"profile_id": key, "editing": True, "profile": {**row, "top_p": ""}})
    assert selected_model_config(host).top_p == 0.7
    assert first.top_p == 0.97


def test_profile_scope_and_child_inherit_top_p(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host, top_p=0.93)
    op(host, "select", {"profile_id": key})
    attrs = {}
    with selected_model_scope(host):
        assert host.config.top_p == host.backend.top_p == 0.93
        inherit_model_profile(attrs, host)
        op(host, "select", {"profile_id": "default"})
        assert host.config.top_p == host.backend.top_p == 0.93
    assert host.config.top_p is None
    child = inherited_model_config(host, SimpleNamespace(attributes=attrs))
    assert child.top_p == 0.93
    assert replace(child, top_p=0.8).top_p == 0.8 and child.top_p == 0.93


def test_task_overlay_does_not_replace_selected_sampling(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host, top_p=0.91, temperature=0.4)
    config = selected_model_config(host, profile_id=key)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("top_p: 0.5\ntemperature: 1.0\n", encoding="utf-8")
    task = SimpleNamespace(runtime_identity=SimpleNamespace(config_overlay_ref=str(overlay), config_scope="task"))
    updated = apply_task_runtime_config_overlay(config, task, workspace_root=tmp_path)
    assert updated.top_p == 0.91 and updated.temperature == "0.4"


def test_tui_form_saves_and_clears_top_p(monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_provider_menu as menu

    fields = []
    original = menu._field
    sent = []

    def field(value="", **kwargs):
        widget = original(value, **kwargs)
        fields.append(widget)
        return widget

    async def choose_interface(*args, **kwargs):
        return "openai_compatible"

    async def dialog(*args, **kwargs):
        assert fields[3].text == "0.98"
        fields[3].text = ""
        return True

    async def request(*args):
        sent.append(args[-1])
        return {"ok": True}

    monkeypatch.setattr(menu, "_field", field)
    monkeypatch.setattr(menu, "_choose_interface", choose_interface)
    monkeypatch.setattr(menu, "_dialog", dialog)
    monkeypatch.setattr(menu, "_request", request)
    result = asyncio.run(menu._edit_model(None, None, "session", "service", {
        "id": "saved-id", "model_name": "model", "top_p": 0.98}))
    assert "已保存" in result and sent[0]["profile"]["top_p"] == ""
    assert sent[0]["profile_id"] == "saved-id" and sent[0]["editing"]


@pytest.mark.parametrize("value", [True, "nan", "inf", -0.1, 1.1, "private-secret"])
def test_invalid_profile_sampling_never_saved(tmp_path, value):
    host = Host(tmp_path)
    provider(host)
    with pytest.raises(ValueError, match="top_p"):
        model(host, top_p=value)
    assert [row for row in op(host, "list", {})["profiles"] if row["id"] != "default"] == []
