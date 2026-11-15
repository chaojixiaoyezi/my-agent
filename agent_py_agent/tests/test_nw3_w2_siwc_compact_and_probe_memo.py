"""NW3 W2 回归测试：SIWC configuration_update 防护/400 回退、手动 /compact scope 与探针 LRU 缓存。"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import (
    BackendOptions,
    ModelResponse,
    ProviderRequestOptions,
)
from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
from agent_py_agent.agent.backends.http import invalidate_capability_probe_memo
from agent_py_agent.agent.backends.oauth import OAuthResponsesBackend
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.provider_headers import (
    chatgpt_session_uuid,
    current_provider_affinity,
    current_provider_session,
    provider_runtime_scope,
    request_headers,
)
from agent_py_agent.agent.backends.responses import (
    OpenAIResponsesBackend,
    _is_reasoning_update_rejection,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayConversationLoadRequest,
    gateway_conversation_context,
)
from agent_py_agent.agent.gateway_parts.request_history import append_gateway_conversation_message
from agent_py_agent.agent.settings import AgentConfig, model_oauth
from agent_py_agent.agent.settings.model_oauth_schema import CHATGPT_BASE, SIWC_BASE
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability


@pytest.fixture(autouse=True)
def _reset_probe_memo():
    invalidate_capability_probe_memo()
    yield
    invalidate_capability_probe_memo()


def _siwc_auth_ref(provider_id: str = "openai-siwc") -> dict[str, object]:
    return {
        "mode": "chatgpt",
        "provider_id": provider_id,
        "path": "/tmp/nonexistent-oauth.json",
        "generation": 1,
        "binding": "bind-1",
    }


def _probe_ok_response(prompt: str) -> ModelResponse:
    nonce = str(prompt).split("with nonce ", 1)[1].split(".", 1)[0].strip()
    return ModelResponse(
        text="",
        backend="probe-test",
        tool_use_blocks=[
            {
                "id": "call-probe-1",
                "name": "my_agent_capability_probe",
                "input": {"nonce": nonce},
            }
        ],
    )


def _wire_probe_json_handler(calls: list[dict[str, object]], *, native: bool = True):
    def _post(request):
        body = dict(request.payload)
        calls.append(body)
        prompt = str(body["messages"][-1]["content"])
        nonce = prompt.split("with nonce ", 1)[1].split(".", 1)[0].strip()
        if not native:
            return {
                "choices": [{"message": {"role": "assistant", "content": "plain text"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-probe-1",
                                "type": "function",
                                "function": {
                                    "name": "my_agent_capability_probe",
                                    "arguments": json.dumps({"nonce": nonce}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
        }

    return _post


@pytest.mark.parametrize("model_name", ["gpt-6.1-sol", "gpt-6-astra"])
def test_siwc_auto_skips_configuration_update_while_explicit_on_and_apikey_keep_contract(monkeypatch, model_name):
    siwc_backend = OAuthResponsesBackend(
        BackendOptions(
            api_base=SIWC_BASE,
            api_key="",
            model_name=model_name,
            reasoning_control="effort",
            reasoning_levels=("low", "medium", "high", "xhigh"),
            reasoning_update_items="auto",
            stream_enabled=False,
        ),
        auth_ref=_siwc_auth_ref(),
    )
    assert siwc_backend.supports_reasoning_update_items() is False

    # Intercept request_credentials + request_stream_iter to inspect exact outbound JSON body sent on SIWC.
    monkeypatch.setattr(model_oauth, "request_credentials", lambda *_a: ("oauth-test-token", {}))
    outbound_bodies: list[dict[str, object]] = []

    def _stream_lines(_path, body, _headers, **_kw):
        outbound_bodies.append(dict(body))
        yield json.dumps({
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}
                ],
            },
        })

    monkeypatch.setattr(siwc_backend, "request_stream_iter", _stream_lines)
    siwc_backend.generate(
        "hello",
        request_options=ProviderRequestOptions(reasoning_update_effort="low"),
    )
    assert len(outbound_bodies) == 1
    assert not any(
        isinstance(item, dict) and item.get("type") == "configuration_update"
        for item in outbound_bodies[0].get("input", [])
    )

    # Explicit "on" overrides auto SIWC suppression.
    siwc_on = OAuthResponsesBackend(
        BackendOptions(
            api_base=SIWC_BASE,
            api_key="",
            model_name=model_name,
            reasoning_update_items="on",
            stream_enabled=False,
        ),
        auth_ref=_siwc_auth_ref(),
    )
    assert siwc_on.supports_reasoning_update_items() is True

    # Plain API-key OpenAI backend at SIWC_BASE keeps auto support enabled.
    apikey_backend = OpenAIResponsesBackend(
        BackendOptions(
            api_base=SIWC_BASE,
            api_key="sk-test",
            model_name=model_name,
            reasoning_update_items="auto",
            stream_enabled=False,
        )
    )
    assert apikey_backend.supports_reasoning_update_items() is True

    # ChatGPT Codex base keeps auto support enabled.
    codex_backend = OAuthResponsesBackend(
        BackendOptions(
            api_base=CHATGPT_BASE,
            api_key="",
            model_name=model_name,
            reasoning_update_items="auto",
            stream_enabled=True,
        ),
        auth_ref=_siwc_auth_ref(),
    )
    assert codex_backend.supports_reasoning_update_items() is True


@pytest.mark.parametrize(
    ("error_detail", "expected"),
    [
        (
            {
                "error": {
                    "code": "subscription_sharing_unsupported_capability",
                    "message": "configuration_update item is not supported in this mode",
                }
            },
            True,
        ),
        (
            {
                "error": {
                    "code": "subscription_sharing_unsupported_capability",
                    "message": "unsupported item",
                    "param": "input[1].type",
                }
            },
            True,
        ),
        (
            {
                "error": {
                    "code": "subscription_sharing_unsupported_capability",
                    "message": "unsupported item",
                    "param": "input.1",
                }
            },
            True,
        ),
        (
            {
                "error": {
                    "code": "subscription_sharing_unsupported_capability",
                    "message": "capability disabled",
                    "param": "configuration_update",
                }
            },
            True,
        ),
        (
            {
                "error": {
                    "code": "subscription_sharing_unsupported_capability",
                    "message": "web_search is not supported",
                    "param": "tools[0]",
                }
            },
            False,
        ),
        (
            {
                "error": {
                    "code": "subscription_sharing_unsupported_capability",
                    "message": "wrong item index",
                    "param": "input[0].type",
                }
            },
            False,
        ),
    ],
)
def test_subscription_sharing_rejection_matching(error_detail, expected):
    request_body = {
        "input": [
            {"role": "developer", "content": "sys"},
            {"type": "configuration_update", "configuration": {"reasoning_effort": "low"}},
            {"role": "user", "content": "hi"},
        ]
    }
    exc = ProviderRequestRejectedError(
        "HTTP 400: unsupported capability",
        status_code=400,
        details={"status_code": 400, "provider_error": error_detail},
    )
    assert _is_reasoning_update_rejection(exc, payload=request_body, inserted_index=1) is expected


def test_subscription_sharing_rejection_triggers_single_clean_retry_and_latches(monkeypatch):
    backend = OpenAIResponsesBackend(
        BackendOptions(
            api_base="https://api.example.test/v1",
            api_key="sk-test",
            model_name="gpt-6.1-sol",
            reasoning_control="effort",
            reasoning_levels=("low", "medium", "high", "xhigh"),
            reasoning_update_items="auto",
            stream_enabled=False,
        )
    )
    attempts: list[dict[str, object]] = []

    def _send(body: dict[str, object]):
        attempts.append(dict(body))
        if len(attempts) == 1:
            raise ProviderRequestRejectedError(
                "HTTP 400: configuration_update unsupported",
                status_code=400,
                details={
                    "status_code": 400,
                    "provider_error": {
                        "error": {
                            "code": "subscription_sharing_unsupported_capability",
                            "message": "Capability configuration_update is unavailable",
                        }
                    },
                },
            )
        return {
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}],
        }

    monkeypatch.setattr(backend, "request_json", lambda _path, body, _headers, **_kw: _send(body))
    response = backend.generate(
        "hello",
        request_options=ProviderRequestOptions(reasoning_update_effort="low"),
    )
    assert response.text == "done"
    assert len(attempts) == 2
    assert any(isinstance(x, dict) and x.get("type") == "configuration_update" for x in attempts[0]["input"])
    assert not any(isinstance(x, dict) and x.get("type") == "configuration_update" for x in attempts[1]["input"])
    assert backend.supports_reasoning_update_items() is False


def test_manual_compact_binds_provider_runtime_scope_matching_auto_compact(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import compact_request_budget

    monkeypatch.setattr(compact_request_budget, "resolve_model_context_window_tokens", lambda _agent: 1_000_000)
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        prompt_files=[],
        max_tokens=64,
        model_context_window_tokens=1_000_000,
        memory_compact_trigger_percent=50,
        conversation_history_max_turns=20,
    )
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    agent = SimpleAgent(config, tmp_path)

    observed_manual: dict[str, object] = {}

    class _RecordingCompactBackend:
        name = "recording-compact-test"

        def probe_tool_capability(self) -> ProviderToolCapability:
            observed_manual["probe_session"] = current_provider_session()
            observed_manual["probe_affinity"] = current_provider_affinity()
            observed_manual["probe_chatgpt_uuid"] = chatgpt_session_uuid()
            return ProviderToolCapability(
                provider=self.name,
                endpoint="local://manual-compact-scope-test",
                model="gpt-6.1-sol",
                stream=False,
                native_supported=True,
                evidence="test_declared_native",
            )

        def generate(self, _prompt: str, **_kwargs) -> ModelResponse:
            observed_manual["session"] = current_provider_session()
            observed_manual["affinity"] = current_provider_affinity()
            observed_manual["chatgpt_uuid"] = chatgpt_session_uuid()
            observed_manual["headers_custom"] = request_headers(
                {"Content-Type": "application/json"}, {}, "x-session",
            )
            observed_manual["headers_default"] = request_headers(
                {"Content-Type": "application/json"}, {}, "",
            )
            return ModelResponse(
                text="手动压缩保留项目上下文与待办结论。",
                backend=self.name,
            )

    agent.backend = _RecordingCompactBackend()
    gateway_req = {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "oc_manual_scope",
            "channel_user_id": "ou_manual_scope",
            "canonical_user_id": "ou_manual_scope",
        }
    }
    ctx = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, gateway_req, "gw-create-scope", "开始")
    )
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent, {}, ctx, request_id=f"msg-{role}", role=role, content="关键历史内容" * 4_000,
        )
    thread = agent.conversation_store.threads.require(ctx.thread_id)

    monkeypatch.setattr(control_service, "_live_window_request", lambda *_a: None)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda *_a: agent)
    monkeypatch.setattr(control_service, "_conversation_thread_for_scope", lambda *_a: thread)

    result = control_service._execute_registered_compact_control(
        agent,
        None,
        SimpleNamespace(value=""),
        SimpleNamespace(),
    )
    assert result.ok, (result.error_code, result)

    with provider_runtime_scope(agent, SimpleNamespace(thread_id=thread.thread_id)):
        expected_session = current_provider_session()
        expected_affinity = current_provider_affinity()
        expected_uuid = chatgpt_session_uuid()
        expected_custom = request_headers({"Content-Type": "application/json"}, {}, "x-session")
        expected_default = request_headers({"Content-Type": "application/json"}, {}, "")

    assert expected_session and observed_manual["probe_session"] == expected_session
    assert expected_affinity and observed_manual["probe_affinity"] == expected_affinity
    assert expected_uuid and observed_manual["probe_chatgpt_uuid"] == expected_uuid
    assert observed_manual["session"] == expected_session
    assert observed_manual["affinity"] == expected_affinity
    assert observed_manual["chatgpt_uuid"] == expected_uuid
    assert observed_manual["headers_custom"]["x-session"] == expected_custom["x-session"]
    assert observed_manual["headers_default"] == expected_default


def test_capability_probe_memo_hits_across_instances_and_isolates_key_dimensions(monkeypatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(http, "post_json", _wire_probe_json_handler(calls, native=True))

    def _make_openai(*, api_base="https://relay.example.test/v1", api_key="secret-a", model_name="m-1", stream=False):
        return OpenAICompatibleBackend(
            BackendOptions(
                api_base=api_base,
                api_key=api_key,
                model_name=model_name,
                stream_enabled=stream,
            )
        )

    b1 = _make_openai()
    b2 = _make_openai()
    cap1 = b1.probe_tool_capability()
    cap2 = b2.probe_tool_capability()
    assert cap1.native_supported is True
    assert cap2 == cap1
    assert len(calls) == 1

    # Varying any single key field triggers a fresh probe.
    b_model = _make_openai(model_name="m-2")
    b_key = _make_openai(api_key="secret-b")
    b_base = _make_openai(api_base="https://other.example.test/v1")
    b_anthropic = AnthropicCompatibleBackend(
        BackendOptions(
            api_base="https://relay.example.test/v1",
            api_key="secret-a",
            model_name="m-1",
            stream_enabled=False,
        )
    )
    b_auth = _make_openai()
    b_auth.auth_ref = {"mode": "oauth", "profile_id": "prof-2"}

    assert b_model.probe_tool_capability().native_supported is True
    assert b_key.probe_tool_capability().native_supported is True
    assert b_base.probe_tool_capability().native_supported is True
    # Anthropic backend expects content tool_use format; patch request_json cleanly for variant check.
    anth_calls = 0

    def _anth_probe(prompt, **_kw):
        nonlocal anth_calls
        anth_calls += 1
        return _probe_ok_response(prompt)

    monkeypatch.setattr(b_anthropic, "generate", _anth_probe)
    assert b_anthropic.probe_tool_capability().native_supported is True
    assert anth_calls == 1
    assert b_auth.probe_tool_capability().native_supported is True
    assert len(calls) == 5

    # Inspect memo keys to confirm raw secret string never leaks into keys.
    for key in http._capability_probe_entries:
        assert len(key[-1]) == 16
        assert "secret-a" not in repr(key)
        assert "secret-b" not in repr(key)


def test_capability_probe_memo_excludes_negative_and_evicts_lru_above_256(monkeypatch):
    fail_calls: list[dict[str, object]] = []
    monkeypatch.setattr(http, "post_json", _wire_probe_json_handler(fail_calls, native=False))
    neg = OpenAICompatibleBackend(
        BackendOptions(
            api_base="https://neg.example.test/v1",
            api_key="secret-neg",
            model_name="neg-model",
            stream_enabled=False,
        )
    )
    neg_cap = neg.probe_tool_capability()
    assert neg_cap.native_supported is False
    assert len(http._capability_probe_entries) == 0

    monkeypatch.setattr(http, "post_json", _wire_probe_json_handler([], native=True))
    probe_count = 0

    def _fast_backend(index: int) -> OpenAICompatibleBackend:
        backend = OpenAICompatibleBackend(
            BackendOptions(
                api_base="https://lru.example.test/v1",
                api_key="secret-lru",
                model_name=f"lru-model-{index}",
                stream_enabled=False,
            )
        )

        def _gen(prompt, **_kw):
            nonlocal probe_count
            probe_count += 1
            return _probe_ok_response(prompt)

        monkeypatch.setattr(backend, "generate", _gen)
        return backend

    for idx in range(256):
        assert _fast_backend(idx).probe_tool_capability().native_supported is True
    assert len(http._capability_probe_entries) == 256
    assert probe_count == 256

    # Re-touch model-0 to move it to MRU so model-1 becomes LRU victim.
    assert _fast_backend(0).probe_tool_capability().native_supported is True
    assert probe_count == 256

    # Insert entry #257 -> model-1 is evicted, model-0 & model-256 remain.
    assert _fast_backend(256).probe_tool_capability().native_supported is True
    assert len(http._capability_probe_entries) == 256
    assert probe_count == 257

    assert _fast_backend(0).probe_tool_capability().native_supported is True
    assert probe_count == 257
    assert _fast_backend(1).probe_tool_capability().native_supported is True
    assert probe_count == 258


def test_capability_probe_memo_invalidation_and_rejection_eviction(monkeypatch):
    probes = 0
    monkeypatch.setattr(http, "post_json", _wire_probe_json_handler([], native=True))

    def _make_backend(model_name: str = "m-evict") -> OpenAICompatibleBackend:
        return OpenAICompatibleBackend(
            BackendOptions(
                api_base="https://evict.example.test/v1",
                api_key="secret-evict",
                model_name=model_name,
                stream_enabled=False,
            )
        )

    b1 = _make_backend("m-evict")
    b_keep = _make_backend("m-keep")
    assert b1.probe_tool_capability().native_supported is True
    assert b_keep.probe_tool_capability().native_supported is True
    assert len(http._capability_probe_entries) == 2

    # Targeted invalidation clears only target backend's entry and instance cache.
    invalidate_capability_probe_memo(b1)
    assert len(http._capability_probe_entries) == 1
    assert b1._provider_tool_capability_cache is None

    # Re-populate b1, then test unrelated 400 configuration_update does NOT evict tool memo.
    assert b1.probe_tool_capability().native_supported is True
    assert len(http._capability_probe_entries) == 2

    def _config_update_400(_request):
        raise ProviderRequestRejectedError(
            "HTTP 400: configuration_update unsupported",
            status_code=400,
            details={
                "status_code": 400,
                "provider_error": {
                    "error": {
                        "code": "subscription_sharing_unsupported_capability",
                        "message": "configuration_update not supported",
                    }
                },
            },
        )

    monkeypatch.setattr(http, "post_json", _config_update_400)
    with pytest.raises(ProviderRequestRejectedError):
        b1.request_json("/chat/completions", {"messages": [], "tools": [{"name": "t"}]}, {})
    assert len(http._capability_probe_entries) == 2

    # HTTP 400 tool rejection evicts matching backend's entry.
    def _tool_400(_request):
        raise ProviderRequestRejectedError(
            "HTTP 400: unsupported tool_choice",
            status_code=400,
            details={
                "status_code": 400,
                "provider_error": {"error": {"code": "unsupported_parameter", "param": "tools"}},
            },
        )

    monkeypatch.setattr(http, "post_json", _tool_400)
    with pytest.raises(ProviderRequestRejectedError):
        b1.request_json("/chat/completions", {"messages": [], "tools": [{"name": "t"}]}, {})
    assert len(http._capability_probe_entries) == 1

    # Repopulate and test HTTP 401/403 via request_json and streaming evicts memo.
    for status in (401, 403):
        monkeypatch.setattr(http, "post_json", _wire_probe_json_handler([], native=True))
        assert _make_backend("m-evict").probe_tool_capability().native_supported is True
        assert len(http._capability_probe_entries) == 2

        def _auth_fail(_request, code=status):
            raise ProviderRequestRejectedError(f"HTTP {code}: denied", status_code=code)

        if status == 401:
            monkeypatch.setattr(http, "post_json", _auth_fail)
            with pytest.raises(ProviderRequestRejectedError):
                b1.request_json("/chat/completions", {"messages": []}, {})
        else:
            monkeypatch.setattr(http, "post_stream_iter", _auth_fail)
            with pytest.raises(ProviderRequestRejectedError):
                list(b1.request_stream_iter("/chat/completions", {"messages": []}, {}))
        assert len(http._capability_probe_entries) == 1

    # Degraded probe outcome on fresh instance evicts stale positive memo.
    monkeypatch.setattr(http, "post_json", _wire_probe_json_handler([], native=True))
    b_deg = _make_backend("m-evict")
    assert b_deg.probe_tool_capability().native_supported is True
    assert len(http._capability_probe_entries) == 2
    monkeypatch.setattr(
        b_deg,
        "_probe_tool_capability_uncached",
        lambda: ProviderToolCapability(
            provider="openai_compatible",
            endpoint=b_deg._tool_endpoint(),
            model="m-evict",
            stream=False,
            native_supported=False,
            evidence="live_probe_returned_no_valid_structured_tool_call",
        ),
    )
    b_deg._provider_tool_capability_cache = None
    # Simulate stale-local re-probe returning degraded result; entry must be removed from memo.
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(http, "_lookup_capability_probe_memo", lambda _key: (None, 0))
        assert b_deg.probe_tool_capability().native_supported is False
    assert len(http._capability_probe_entries) == 1

    # Concurrent multi-thread hits against memo stay consistent.
    monkeypatch.setattr(http, "post_json", _wire_probe_json_handler([], native=True))

    def _counted_gen(backend_inst):
        def _gen(prompt, **_kw):
            nonlocal probes
            probes += 1
            return _probe_ok_response(prompt)

        return _gen

    seed_backend = _make_backend("m-conc")
    monkeypatch.setattr(seed_backend, "generate", _counted_gen(seed_backend))
    assert seed_backend.probe_tool_capability().native_supported is True
    assert probes == 1

    results: list[bool] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def _worker():
        try:
            barrier.wait(timeout=2)
            fresh = _make_backend("m-conc")
            results.append(fresh.probe_tool_capability().native_supported)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert errors == []
    assert results == [True] * 6
    assert probes == 1

    # Global invalidation clears all remaining entries including still-live b_keep.
    invalidate_capability_probe_memo()
    assert len(http._capability_probe_entries) == 0
    assert b_keep._cached_instance_capability() is None
