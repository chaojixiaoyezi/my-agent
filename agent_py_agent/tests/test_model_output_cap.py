"""模型单次输出上限统一为 64K，构造后端时按已知窗口夹取（2026-09-27，用户要求“所有模型统一 64K”）。

锁定：随包 YAML、AgentConfig 默认值与常量同值；已知窗口（是否显式都算）时输出上限 = min(配置, 窗口 ÷ 4)，未知时按配置值；
backend.max_tokens 就是实际发送值（请求体直接读它）；没有 max_tokens 的后端估算按同一公式，不能回退到未夹取的 65536
（第一版只夹显式窗口，“窗口×0.8−输出上限”的 compact 预算在 8 万以下窗口变成 1，8 个 compact 用例回归）。
没有读取方的 vision_* 配置已删除，用户配置里残留时只告警、不影响加载。
"""
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import max_output_tokens
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.config_io import load_simple_yaml
from agent_py_agent.agent.settings.defaults import (
    DEFAULT_MODEL_MAX_TOKENS,
    effective_max_output_tokens,
)

_PACKAGED = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"


def test_packaged_yaml_dataclass_and_constant_agree_on_64k():
    assert DEFAULT_MODEL_MAX_TOKENS == 65_536
    assert AgentConfig().max_tokens == DEFAULT_MODEL_MAX_TOKENS
    assert int(load_simple_yaml(_PACKAGED)["max_tokens"]) == DEFAULT_MODEL_MAX_TOKENS


@pytest.mark.parametrize("window,explicit,expected", [
    (1_000_000, True, 65_536), (262_144, True, 65_536), (204_800, True, 51_200), (128_000, True, 32_000),
    (32_768, True, 8_192), (128_000, False, 32_000), (64_000, False, 16_000), (0, True, 65_536),
])
def test_output_cap_is_clamped_by_any_known_window(window, explicit, expected):
    config = replace(AgentConfig(), model_context_window_tokens=window, model_context_window_explicit=explicit)
    assert effective_max_output_tokens(config) == expected


def test_backend_factory_clamps_by_any_known_window():
    config = replace(AgentConfig(), model_backend="openai_compatible", api_base="http://127.0.0.1:9/v1",
                     api_key="test-key", model_name="m", model_context_window_tokens=128_000)
    backend = get_backend(config.model_backend, config)
    assert backend.max_tokens == 32_000 and max_output_tokens(SimpleNamespace(backend=backend, config=config)) == 32_000
    assert get_backend(config.model_backend, replace(config, model_context_window_tokens=0)).max_tokens == 65_536


def test_output_estimate_for_other_backends_is_clamped_too():
    # 测试替身和非 HTTP 后端没有 output_token_cap；原来直接回退到配置的 65536，compact 预算因此变成 1。
    config = replace(AgentConfig(), model_context_window_tokens=64_000)
    assert max_output_tokens(SimpleNamespace(backend=SimpleNamespace(context_window_tokens=64_000), config=config)) == 16_000
    assert max_output_tokens(SimpleNamespace(backend=None, config=SimpleNamespace(max_tokens=0))) == 0


def test_dead_vision_config_is_removed_and_leftovers_only_warn(tmp_path):
    assert not [item.name for item in fields(AgentConfig) if item.name.startswith("vision_")]
    assert not [key for key in load_simple_yaml(_PACKAGED) if key.startswith("vision_")]
    user_config = tmp_path / "user.yaml"
    user_config.write_text("vision_max_tokens: 1024\nmax_tokens: 65536\n", encoding="utf-8")
    config = load_config(user_config)
    assert config.max_tokens == 65_536
    assert any("vision_max_tokens" in warning for warning in config.config_warnings)


@pytest.mark.parametrize("backend_name", ["openai_compatible", "anthropic_compatible"])
def test_request_payload_sends_the_backend_cap(monkeypatch, backend_name):
    # 输出预留与 compact 预算读 backend.max_tokens；实际请求体必须是同一个值，否则“预留”和“发送”对不上。
    from agent_py_agent.agent.backends import http

    sent = []

    def send(request):
        sent.append(request.payload)
        if backend_name == "anthropic_compatible":
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(http, "post_json", send)
    config = replace(AgentConfig(), model_backend=backend_name, api_base="https://example.invalid/v1", api_key="test-key",
                     model_name="m", stream_enabled=False, model_context_window_tokens=40_000)
    backend = get_backend(backend_name, config)
    backend.generate("hi")
    assert sent and sent[-1]["max_tokens"] == 10_000
