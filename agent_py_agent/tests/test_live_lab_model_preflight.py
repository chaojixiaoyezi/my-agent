from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from scripts.live_lab.reporter import real_model_preflight


def _config(**overrides):
    values = {
        "model_backend": "openai_compatible",
        "api_key": "present",
        "max_tokens": 4096,
        "request_timeout": 300,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_real_model_preflight_executes_bounded_generation() -> None:
    captured = {}

    class _Backend:
        def generate(self, prompt):
            captured["prompt"] = prompt
            return ModelResponse(text="连接正常", backend="fake", stop_reason="stop")

    def factory(name, config):
        captured["name"] = name
        captured["max_tokens"] = config.max_tokens
        captured["request_timeout"] = config.request_timeout
        return _Backend()

    report = real_model_preflight(_config(), backend_factory=factory)

    assert captured == {
        "name": "openai_compatible",
        "max_tokens": 512,
        "request_timeout": 90,
        "prompt": "请只回答：连接正常",
    }
    assert report["response_chars"] == 4
    assert report["stop_reason"] == "stop"


@pytest.mark.parametrize(
    "config, message",
    [
        (_config(model_backend="echo"), "echo"),
        (_config(api_key=""), "API key"),
    ],
)
def test_real_model_preflight_rejects_non_real_configuration(config, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        real_model_preflight(config, backend_factory=lambda _name, _config: None)


def test_real_model_preflight_rejects_empty_response() -> None:
    backend = SimpleNamespace(generate=lambda _prompt: ModelResponse(text="", backend="fake"))

    with pytest.raises(RuntimeError, match="empty text"):
        real_model_preflight(
            _config(),
            backend_factory=lambda _name, _config: backend,
        )
