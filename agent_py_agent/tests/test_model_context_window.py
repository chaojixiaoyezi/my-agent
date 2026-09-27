from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_window import resolve_model_context_window_tokens


def _agent(config: object, backend: object | None = None) -> object:
    return SimpleNamespace(config=config, backend=backend)


def test_configured_context_window_is_used_without_backend() -> None:
    agent = _agent(SimpleNamespace(model_context_window_tokens=200_000), None)

    assert resolve_model_context_window_tokens(agent) == 200_000


def test_filled_config_window_beats_provider_even_when_larger() -> None:
    """参数减量第 2 批：填了数字就用这个数（原 model_context_window_explicit 已并入），不再被服务商元数据改掉。"""
    agent = _agent(
        SimpleNamespace(model_context_window_tokens=200_000),
        SimpleNamespace(context_window_tokens=128_000),
    )

    assert resolve_model_context_window_tokens(agent) == 200_000
    blank = _agent(SimpleNamespace(model_context_window_tokens=None), SimpleNamespace(context_window_tokens=128_000))
    assert resolve_model_context_window_tokens(blank) == 128_000


def test_blank_config_window_uses_the_provider_window() -> None:
    class Backend:
        def provider_context_window_tokens(self) -> int:
            return 320_000

    agent = _agent(SimpleNamespace(model_context_window_tokens=None), Backend())

    assert resolve_model_context_window_tokens(agent) == 320_000
    filled = _agent(SimpleNamespace(model_context_window_tokens=200_000), Backend())
    assert resolve_model_context_window_tokens(filled) == 200_000


def test_missing_provider_window_falls_back_to_config() -> None:
    class Backend:
        def provider_context_window_tokens(self) -> int:
            return 0

    agent = _agent(SimpleNamespace(model_context_window_tokens=96_000), Backend())

    assert resolve_model_context_window_tokens(agent) == 96_000


def test_backend_context_window_attribute_is_used() -> None:
    agent = _agent(
        SimpleNamespace(model_name="MiniMax-M2.7", max_tokens=16_000),
        SimpleNamespace(context_window_tokens=270_000),
    )

    assert resolve_model_context_window_tokens(agent) == 270_000


def test_backend_provider_max_context_tokens_is_used() -> None:
    agent = _agent(
        SimpleNamespace(model_name="other-model", max_tokens=16_000),
        SimpleNamespace(max_context_tokens=128_000),
    )

    assert resolve_model_context_window_tokens(agent) == 128_000


def test_backend_metadata_window_is_used() -> None:
    agent = _agent(
        SimpleNamespace(model_name="metadata-model", max_tokens=16_000),
        SimpleNamespace(model_metadata={"context_window": "200000"}),
    )

    assert resolve_model_context_window_tokens(agent) == 200_000


def test_backend_metadata_method_window_is_used() -> None:
    class MetadataBackend:
        def metadata(self) -> dict[str, object]:
            return {"max_input_tokens": 96_000}

    assert resolve_model_context_window_tokens(_agent(SimpleNamespace(), MetadataBackend())) == 96_000


def test_unknown_model_window_uses_128k_fallback() -> None:
    """没填窗口、服务商也不报时按 128000 兜底，与合并前的默认配置值一致（原 200K 兜底实际走不到）。"""
    agent = _agent(SimpleNamespace(model_name="unknown", max_tokens=4096), SimpleNamespace())

    assert resolve_model_context_window_tokens(agent) == 128_000


def test_http_backend_model_metadata_fills_a_blank_config_window(monkeypatch) -> None:
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend

    monkeypatch.setattr(
        "agent_py_agent.agent.backends.model_metadata.get_json",
        lambda _request: {"data": [{"id": "provider-model", "context_window": 128_000}]},
    )
    backend = OpenAICompatibleBackend(
        BackendOptions(
            api_base="https://provider.example/v1",
            api_key="test-key",
            model_name="provider-model",
        )
    )
    agent = _agent(SimpleNamespace(model_context_window_tokens=None), backend)

    assert resolve_model_context_window_tokens(agent) == 128_000
    assert backend.provider_context_window_tokens() == 128_000
    filled = _agent(SimpleNamespace(model_context_window_tokens=200_000), backend)
    assert resolve_model_context_window_tokens(filled) == 200_000


def test_http_backend_metadata_without_window_uses_config(monkeypatch) -> None:
    from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
    from agent_py_agent.agent.backends.base import BackendOptions

    monkeypatch.setattr(
        "agent_py_agent.agent.backends.model_metadata.get_json",
        lambda _request: {"data": [{"id": "MiniMax-M2.7", "type": "model"}]},
    )
    backend = AnthropicCompatibleBackend(
        BackendOptions(
            api_base="https://api.minimaxi.com/anthropic",
            api_key="test-key",
            model_name="MiniMax-M2.7",
            context_window_tokens=200_000,
        )
    )
    agent = _agent(SimpleNamespace(model_context_window_tokens=200_000), backend)

    assert resolve_model_context_window_tokens(agent) == 200_000
