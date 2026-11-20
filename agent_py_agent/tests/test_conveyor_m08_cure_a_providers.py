"""验证供应商请求数据类与标准库数据类的行为一致。"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import pickle
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.anthropic import (
    AnthropicCompatibleBackend,
    _AnthropicGenerateRequest,
    _StreamCallbacks,
)
from agent_py_agent.agent.backends.anthropic_prompt_cache import (
    AnthropicPromptCacheProjection,
    _normalize_anthropic_block,
)
from agent_py_agent.agent.backends.gateway_request_limits import validate_request_limits
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.backends.model_metadata import (
    ProviderMetadataOptions,
    ProviderModelMetadata,
)
from agent_py_agent.agent.backends.oauth import (
    OAuthChatBackend,
    OAuthMessagesBackend,
    OAuthResponsesBackend,
    _OAuthMixin,
)
from agent_py_agent.agent.backends.openai_chat import (
    OpenAICompatibleBackend,
    _openai_function_call,
    _OpenAIGenerateRequest,
    _StreamObservers,
)
from agent_py_agent.agent.backends.provider_send_gate import (
    ProviderSendAttempt,
    ProviderSendRefused,
)
from agent_py_agent.agent.backends.request_budget import RequestBudgetExceeded, RequestTokenBudget
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend, _GenerationObserver
from agent_py_agent.agent.backends.responses_wire import _CollectedResponse, message_items
from agent_py_agent.agent.common import fast_dataclass


def g1_case_anthropic_generate_request():
    return lambda: _AnthropicGenerateRequest("prompt", max_output_tokens=512)


def g1_case_anthropic_stream_callbacks():
    return lambda: _StreamCallbacks(first_event_timeout_seconds=5.0)


def g1_case_anthropic_cache_projection():
    return lambda: AnthropicPromptCacheProjection("instructions", "prompt")


def g1_case_openai_generate_request():
    return lambda: _OpenAIGenerateRequest("prompt", max_output_tokens=512)


def g1_case_openai_stream_observers():
    return lambda: _StreamObservers(first_event_timeout_seconds=5.0)


def g1_case_provider_model_metadata():
    return lambda: ProviderModelMetadata(context_window_tokens=8192)


def g1_case_provider_metadata_options():
    return lambda: ProviderMetadataOptions("https://example.invalid", "", "model", 10, 5.0)


def g1_case_request_token_budget():
    return lambda: RequestTokenBudget(128, 512, 8192, 2048, True)


def g1_case_provider_send_attempt():
    return lambda: ProviderSendAttempt("POST", "https://example.invalid", "digest", "model", 0)


def _hash_outcome(instance):
    try:
        return hash(instance)
    except TypeError as exc:
        return type(exc), str(exc)


@pytest.mark.parametrize(
    ("cls", "required", "updates"),
    [
        pytest.param(
            _AnthropicGenerateRequest,
            {"prompt": "prompt"},
            {"temperature": 0.5, "messages": [{"role": "user", "content": "你好"}]},
            id="anthropic-generate-request",
        ),
        pytest.param(
            _StreamCallbacks, {}, {"first_event_timeout_seconds": 5.0}, id="anthropic-stream-callbacks",
        ),
        pytest.param(
            AnthropicPromptCacheProjection,
            {"system": "instructions", "prompt": "prompt"},
            {"system": [{"type": "text", "text": "instructions"}], "stable_system_cache_active": True},
            id="anthropic-cache-projection",
        ),
        pytest.param(
            _OpenAIGenerateRequest,
            {"prompt": "prompt"},
            {"max_output_tokens": 512, "response_schema": {"type": "object"}},
            id="openai-generate-request",
        ),
        pytest.param(
            _StreamObservers, {}, {"total_deadline_seconds": 20.0}, id="openai-stream-observers",
        ),
        pytest.param(
            ProviderModelMetadata, {}, {"context_window_tokens": 8192, "record": {"model": "example"}},
            id="provider-model-metadata",
        ),
        pytest.param(
            ProviderMetadataOptions,
            {"api_base": "https://example.invalid", "api_key": "", "model_name": "model",
             "request_timeout": 10, "connect_timeout": 5.0},
            {"custom_headers": {"X-Mode": "test"}},
            id="provider-metadata-options",
        ),
        pytest.param(
            RequestTokenBudget,
            {"input_tokens": 128, "output_tokens": 512, "context_window_tokens": 8192,
             "safety_tokens": 2048, "shared_window": True},
            {"server_managed_output": True},
            id="request-token-budget",
        ),
        pytest.param(
            ProviderSendAttempt,
            {"method": "POST", "url": "https://example.invalid", "body_sha256": "digest",
             "model": "model", "attempt": 0},
            {"attempt": 1},
            id="provider-send-attempt",
        ),
    ],
)
def test_provider_dto_matches_plain_dataclass(cls, required, updates, monkeypatch):
    assert "__slots__" in cls.__dict__
    assert cls in fast_dataclass.CONVERTED_CLASSES

    # 由标准库按同一字段声明生成独立构造器，同时保留一个可 pickle 的模块名。
    fields = dataclasses.fields(cls)
    plain_name = f"_Plain{cls.__name__}"
    plain = dataclasses.make_dataclass(
        plain_name,
        [(field.name, field.type, copy.copy(field)) for field in fields],
        frozen=True,
        namespace={"__module__": __name__},
    )
    monkeypatch.setitem(globals(), plain_name, plain)
    assert inspect.signature(cls) == inspect.signature(plain)

    original = cls(**required)
    plain_original = plain(**required)
    for arguments in (required, required | updates):
        actual, expected = cls(**arguments), plain(**arguments)
        assert dataclasses.astuple(actual) == dataclasses.astuple(expected)
        assert actual == cls(**arguments)
        assert (actual == original) == (expected == plain_original)
        assert _hash_outcome(actual) == _hash_outcome(expected)
        assert repr(actual) == cls.__name__ + repr(expected).removeprefix(plain_name)
        for instance in (actual, expected):
            restored = pickle.loads(pickle.dumps(instance))
            assert type(restored) is type(instance)
            assert restored == instance

    for field in fields:
        if field.default_factory is not dataclasses.MISSING:
            assert getattr(original, field.name) is not getattr(cls(**required), field.name)

    name = fields[0].name
    for instance in (original, plain_original):
        with pytest.raises(dataclasses.FrozenInstanceError, match=f"cannot assign to field '{name}'"):
            setattr(instance, name, None)
        with pytest.raises(dataclasses.FrozenInstanceError, match=f"cannot delete field '{name}'"):
            delattr(instance, name)

    expected = dataclasses.replace(plain_original, **updates)
    replacements = [fast_dataclass.fast_replace, dataclasses.replace]
    if hasattr(copy, "replace"):
        replacements.append(copy.replace)
    for replace in replacements:
        changed = replace(original, **updates)
        assert type(changed) is cls
        assert changed != original
        assert dataclasses.astuple(changed) == dataclasses.astuple(expected)
        with pytest.raises(TypeError) as baseline:
            dataclasses.replace(plain_original, unknown_field=True)
        with pytest.raises(type(baseline.value)) as failure:
            replace(original, unknown_field=True)
        assert str(failure.value) == str(baseline.value).replace(plain_name, cls.__name__)


@pytest.mark.parametrize(
    ("cls", "base"),
    [
        (AnthropicCompatibleBackend, HttpBackend),
        (OpenAIResponsesBackend, OpenAICompatibleBackend),
        (_CollectedResponse, dict),
        (ProviderSendRefused, RuntimeError),
        (RequestBudgetExceeded, ValueError),
    ],
)
def test_explicit_base_matches_provider_subclass_mro(cls, base):
    pending = [cls]
    while pending:
        subclass = pending.pop()
        assert subclass.__mro__[subclass.__mro__.index(cls) + 1] is base
        pending.extend(subclass.__subclasses__())


def test_oauth_mixin_cooperates_with_each_protocol():
    assert OAuthResponsesBackend.__mro__[1] is _OAuthMixin
    for subclass, protocol in (
        (OAuthChatBackend, OpenAICompatibleBackend),
        (OAuthMessagesBackend, AnthropicCompatibleBackend),
        (OAuthResponsesBackend, OpenAIResponsesBackend),
    ):
        assert subclass.__mro__[subclass.__mro__.index(_OAuthMixin) + 1] is protocol


def test_tool_arguments_preserve_provider_wire_bytes():
    value = {"z": ["你好", "🌙"], "a": {"y": 2, "x": 1}}
    block = {"type": "tool_use", "id": "call_id", "name": "lookup", "input": value}
    expected = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert _openai_function_call(block)["function"]["arguments"] == expected
    message = {"role": "assistant", "content": [block]}
    assert message_items(message, "model")[0]["arguments"] == expected
    assert json.dumps(_normalize_anthropic_block(block)["input"], ensure_ascii=False) == expected


def test_generation_observer_keeps_completion_and_custom_attributes():
    callback = SimpleNamespace(complete=lambda text: text, detail="extension")
    observer = _GenerationObserver(callback, [False])
    assert observer.complete("message") == "message"
    assert observer.detail == "extension"
    with pytest.raises(AttributeError):
        _ = observer.missing
    with pytest.raises(AttributeError):
        _ = _GenerationObserver(None, [False]).complete


def test_request_limit_validation_checks_retries_before_response_size():
    invalid_retries = SimpleNamespace(deadline=None, max_retries=True)
    with pytest.raises(ValueError, match="max_retries"):
        validate_request_limits(invalid_retries)
    invalid_size = SimpleNamespace(deadline=None, max_retries=0, max_response_bytes=0)
    with pytest.raises(ValueError, match="max_response_bytes"):
        validate_request_limits(invalid_size)
    validate_request_limits(SimpleNamespace(deadline=None, max_retries=0, max_response_bytes=1))
