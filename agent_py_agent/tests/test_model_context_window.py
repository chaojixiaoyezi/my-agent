from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_window import resolve_model_context_window_tokens


def _agent(config: object, backend: object | None = None) -> object:
    return SimpleNamespace(config=config, backend=backend)


def test_backend_context_window_attribute_is_used() -> None:
    agent = _agent(
        SimpleNamespace(model_name="MiniMax-M2.7", max_tokens=16_000),
        SimpleNamespace(context_window_tokens=270_000),
    )

    assert resolve_model_context_window_tokens(agent) == 270_000


def test_backend_context_window_aliases_are_used() -> None:
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


def test_unknown_model_window_uses_generic_128k_fallback() -> None:
    agent = _agent(SimpleNamespace(model_name="unknown", max_tokens=4096), SimpleNamespace())

    assert resolve_model_context_window_tokens(agent) == 128_000
