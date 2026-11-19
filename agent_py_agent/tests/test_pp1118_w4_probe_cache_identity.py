"""订阅登录令牌轮换不使能力探针缓存失效，并记录未命中原因与单次告警日志；静态路由头参与探针身份（C4）。"""
from __future__ import annotations

import json
import logging
import re
import threading

import pytest

from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.base import BackendOptions, ModelResponse
from agent_py_agent.agent.backends.errors import (
    ProviderConfigurationError,
    ProviderRequestRejectedError,
)
from agent_py_agent.agent.backends.http import (
    _credential_fingerprint,
    invalidate_capability_probe_memo,
)
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.provider_headers import (
    ProbeAccountingScope,
    probe_accounting_scope,
)
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger


def _reset_probe_state() -> None:
    invalidate_capability_probe_memo()
    with http._capability_probe_lock:
        http._capability_probe_miss_reasons.clear()


@pytest.fixture(autouse=True)
def _clean_probe_memo():
    _reset_probe_state()
    yield
    _reset_probe_state()


class _RotatingOAuth:
    def __init__(self, generation: str = "g1") -> None:
        self.auth_ref = {
            "path": "p",
            "provider_id": "sub",
            "generation": generation,
            "binding": "b",
            "mode": "chatgpt",
        }
        self.reads = 0

    @property
    def api_key(self) -> str:
        self.reads += 1
        return f"access-token-{self.reads}"


class _RotatingOAuthHttpBackend(OpenAICompatibleBackend):
    def __init__(self, generation: str = "g1", *, model_name: str = "oauth-probe-model") -> None:
        self.auth_ref = {
            "path": "/tmp/oauth-test.json",
            "provider_id": "sub",
            "generation": generation,
            "binding": "b1",
            "mode": "chatgpt",
        }
        self.token_reads = 0
        self.current_token = f"oauth-tok-{generation}-1"
        super().__init__(
            BackendOptions(
                api_base="https://oauth.example.test/v1",
                api_key="",
                model_name=model_name,
                stream_enabled=False,
            )
        )

    @property
    def api_key(self) -> str:
        self.token_reads += 1
        return self.current_token

    @api_key.setter
    def api_key(self, value: str) -> None:
        if value:
            raise ValueError("OAuth backend does not accept static api_key")


def _probe_ok_response(prompt: str, *, native: bool = True) -> ModelResponse:
    if not native:
        return ModelResponse(text="plain text", backend="probe-fake", tool_use_blocks=[])
    nonce = re.search(r"nonce ([0-9a-f]+)", prompt).group(1)
    return ModelResponse(
        text="",
        backend="probe-fake",
        tool_use_blocks=[
            {
                "id": "call-probe-1",
                "name": "my_agent_capability_probe",
                "input": {"nonce": nonce},
            }
        ],
    )


def _make_apikey_backend(
    *,
    api_base: str = "https://probe.example.test/v1",
    api_key: str = "sk-probe-secret-value",
    model_name: str = "probe-model",
    stream_enabled: bool = False,
    custom_headers: dict[str, str] | None = None,
) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(
        BackendOptions(
            api_base=api_base,
            api_key=api_key,
            model_name=model_name,
            stream_enabled=stream_enabled,
            custom_headers=dict(custom_headers or {}),
        )
    )


def test_oauth_token_rotation_keeps_probe_fingerprint_and_reads_no_credentials() -> None:
    backend = _RotatingOAuth()
    first = _credential_fingerprint(backend)
    assert _credential_fingerprint(backend) == first
    assert backend.reads == 0
    assert _credential_fingerprint(_RotatingOAuth("g2")) != first


def test_api_key_backend_fingerprint_still_tracks_the_key() -> None:
    class _Key:
        auth_ref = None

        def __init__(self, key: str) -> None:
            self.api_key = key

    assert _credential_fingerprint(_Key("a")) != _credential_fingerprint(_Key("b"))


def test_oauth_probe_tool_capability_ignores_token_rotation_and_reprobes_on_generation_change(
    monkeypatch,
) -> None:
    probe_calls = 0

    def _wire_post(request):
        nonlocal probe_calls
        probe_calls += 1
        prompt = str(request.payload["messages"][-1]["content"])
        nonce = prompt.split("with nonce ", 1)[1].split(".", 1)[0].strip()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
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
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }

    monkeypatch.setattr(http, "post_json", _wire_post)
    backend = _RotatingOAuthHttpBackend(generation="gen-1")
    assert backend.probe_tool_capability().native_supported is True
    assert probe_calls == 1
    reads_after_first = backend.token_reads
    assert reads_after_first >= 1

    # Rotate access token in-place and also rebuild a fresh backend with same auth_ref.
    backend.current_token = "oauth-tok-gen-1-rotated"
    assert backend.probe_tool_capability().native_supported is True
    assert probe_calls == 1
    assert backend.token_reads == reads_after_first

    rebuilt = _RotatingOAuthHttpBackend(generation="gen-1")
    assert rebuilt.probe_tool_capability().native_supported is True
    assert probe_calls == 1
    assert rebuilt.token_reads == 0

    # Re-login changes auth_ref generation -> new identity -> triggers fresh probe.
    relogged = _RotatingOAuthHttpBackend(generation="gen-2")
    assert relogged.probe_tool_capability().native_supported is True
    assert probe_calls == 2


def test_cold_process_probe_records_reason_and_logs_warning_once(monkeypatch, caplog) -> None:
    backend = _make_apikey_backend()
    monkeypatch.setattr(backend, "generate", lambda prompt, **_kw: _probe_ok_response(prompt))
    ledger = ModelCallLedger()
    scope = ProbeAccountingScope(ledger, request_id="req-cold", run_id="run-cold")

    with caplog.at_level(logging.WARNING, logger=http.__name__):
        with probe_accounting_scope(scope):
            assert backend.probe_tool_capability().native_supported is True
            assert backend.probe_tool_capability().native_supported is True
        fresh = _make_apikey_backend()
        with probe_accounting_scope(scope):
            assert fresh.probe_tool_capability().native_supported is True

    records = ledger.records()
    assert len(records) == 1
    assert records[0].metadata["cache_miss_reason"] == "cold_process"

    warnings = [rec for rec in caplog.records if "PROVIDER_TOOL_PROBE_UNCACHED" in rec.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert warnings[0].levelno == logging.WARNING
    assert "reason=cold_process" in msg
    assert "model=probe-model" in msg
    assert "duration_ms=" in msg
    assert "native=True" in msg
    assert "probe.example.test" not in msg
    assert "sk-probe-secret-value" not in msg


@pytest.mark.parametrize(
    ("status_code", "details", "expected_reason", "via_stream"),
    [
        (401, None, "http_401", False),
        (403, None, "http_403", True),
        (
            400,
            {"provider_error": {"error": {"code": "unsupported_parameter", "param": "tools"}}},
            "http_400_tool",
            False,
        ),
    ],
)
def test_http_rejection_invalidation_records_cache_miss_reason(
    monkeypatch,
    caplog,
    status_code: int,
    details: dict | None,
    expected_reason: str,
    via_stream: bool,
) -> None:
    backend = _make_apikey_backend(model_name=f"model-{expected_reason}")
    monkeypatch.setattr(backend, "generate", lambda prompt, **_kw: _probe_ok_response(prompt))
    ledger = ModelCallLedger()

    with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="req-1", run_id="run-1")):
        assert backend.probe_tool_capability().native_supported is True

    # Non-tool 400 (configuration_update) must not invalidate or record miss reason.
    def _config_update_400(_request):
        raise ProviderRequestRejectedError(
            "HTTP 400: configuration_update unsupported",
            status_code=400,
            details={"provider_error": {"error": {"message": "configuration_update not supported"}}},
        )

    monkeypatch.setattr(http, "post_json", _config_update_400)
    with pytest.raises(ProviderRequestRejectedError):
        backend.request_json("/chat/completions", {"messages": [], "tools": [{"name": "t"}]}, {})
    assert backend._provider_tool_capability_cache is not None

    def _reject(_request):
        raise ProviderRequestRejectedError(
            f"HTTP {status_code}: rejected",
            status_code=status_code,
            details=details,
        )

    if via_stream:
        monkeypatch.setattr(http, "post_stream_iter", _reject)
        with pytest.raises(ProviderRequestRejectedError):
            list(backend.request_stream_iter("/chat/completions", {"messages": []}, {}))
    else:
        monkeypatch.setattr(http, "post_json", _reject)
        with pytest.raises(ProviderRequestRejectedError):
            backend.request_json("/chat/completions", {"messages": [], "tools": [{"name": "t"}]}, {})

    assert backend._provider_tool_capability_cache is None

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=http.__name__):
        with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="req-2", run_id="run-2")):
            assert backend.probe_tool_capability().native_supported is True

    second_record = ledger.records()[1]
    assert second_record.metadata["cache_miss_reason"] == expected_reason
    warnings = [rec.getMessage() for rec in caplog.records if "PROVIDER_TOOL_PROBE_UNCACHED" in rec.getMessage()]
    assert len(warnings) == 1
    assert f"reason={expected_reason}" in warnings[0]


def test_instance_and_lifecycle_miss_reasons(monkeypatch, caplog) -> None:
    backend = _make_apikey_backend(model_name="m-first")
    monkeypatch.setattr(backend, "generate", lambda prompt, **_kw: _probe_ok_response(prompt))
    ledger = ModelCallLedger()

    with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="r1", run_id="u1")):
        assert backend.probe_tool_capability().native_supported is True

    # Key change on same instance records exact changed key dimensions.
    backend.model_name = "m-second"
    with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="r2", run_id="u2")):
        assert backend.probe_tool_capability().native_supported is True
    assert ledger.records()[-1].metadata["cache_miss_reason"] == "key_changed:model"

    backend.api_base = "https://changed.example.test/v1"
    backend.stream_enabled = True
    with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="r3", run_id="u3")):
        assert backend.probe_tool_capability().native_supported is True
    assert ledger.records()[-1].metadata["cache_miss_reason"] == "key_changed:endpoint,stream"

    # Global clear marks existing instance as generation_changed and fresh instances as explicit.
    invalidate_capability_probe_memo()
    with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="r4", run_id="u4")):
        assert backend.probe_tool_capability().native_supported is True
    assert ledger.records()[-1].metadata["cache_miss_reason"] == "generation_changed"

    fresh = _make_apikey_backend(model_name="m-fresh-after-global")
    monkeypatch.setattr(fresh, "generate", lambda prompt, **_kw: _probe_ok_response(prompt))
    with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="r5", run_id="u5")):
        assert fresh.probe_tool_capability().native_supported is True
    assert ledger.records()[-1].metadata["cache_miss_reason"] == "explicit"

    # Negative result logs once (across 3 internal attempts) and records negative_result for next probe.
    neg = _make_apikey_backend(model_name="m-negative")
    monkeypatch.setattr(neg, "generate", lambda prompt, **_kw: _probe_ok_response(prompt, native=False))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=http.__name__):
        with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="r6", run_id="u6")):
            assert neg.probe_tool_capability().native_supported is False
    neg_warnings = [rec.getMessage() for rec in caplog.records if "PROVIDER_TOOL_PROBE_UNCACHED" in rec.getMessage()]
    assert len(neg_warnings) == 1
    assert "native=False" in neg_warnings[0]

    monkeypatch.setattr(neg, "generate", lambda prompt, **_kw: _probe_ok_response(prompt, native=True))
    with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="r7", run_id="u7")):
        assert neg.probe_tool_capability().native_supported is True
    assert ledger.records()[-1].metadata["cache_miss_reason"] == "negative_result"

    # ProviderConfigurationError invalidation records provider_configuration for next probe.
    cfg_err_backend = _make_apikey_backend(model_name="m-cfg-err")

    def _raise_cfg(_prompt, **_kw):
        raise ProviderConfigurationError("bad configuration")

    monkeypatch.setattr(cfg_err_backend, "generate", _raise_cfg)
    with pytest.raises(ProviderConfigurationError):
        cfg_err_backend.probe_tool_capability()
    monkeypatch.setattr(cfg_err_backend, "generate", lambda prompt, **_kw: _probe_ok_response(prompt, native=True))
    with probe_accounting_scope(ProbeAccountingScope(ledger, request_id="r8", run_id="u8")):
        assert cfg_err_backend.probe_tool_capability().native_supported is True
    assert ledger.records()[-1].metadata["cache_miss_reason"] == "provider_configuration"


def test_capability_probe_miss_reasons_is_bounded_at_256() -> None:
    for idx in range(300):
        backend = _make_apikey_backend(model_name=f"bounded-model-{idx}")
        invalidate_capability_probe_memo(backend, reason="http_401")
    with http._capability_probe_lock:
        assert len(http._capability_probe_miss_reasons) == 256


def _with_probe_answer(backend: OpenAICompatibleBackend, monkeypatch, *, native: bool) -> OpenAICompatibleBackend:
    monkeypatch.setattr(backend, "generate", lambda prompt, **_kw: _probe_ok_response(prompt, native=native))
    return backend


def test_static_route_headers_split_probe_identity_and_in_flight_results(monkeypatch) -> None:
    # 同入口/模型/凭据的两个 profile 只差一个静态路由头：并发冷探测各自物理探测，结论互不冒充。
    native = {"a": False, "b": True}
    routes = {
        name: _with_probe_answer(
            _make_apikey_backend(custom_headers={"X-Route-Variant": f"route-secret-{name}"}), monkeypatch, native=flag)
        for name, flag in native.items()
    }
    original = OpenAICompatibleBackend._probe_tool_capability_uncached
    physical, guard, overlap = [], threading.Lock(), threading.Event()

    def counting_probe(self):
        with guard:
            physical.append(id(self))
            if len(physical) >= 2:
                overlap.set()
        # 两条物理探测互相等到对方在途；回归时第二个调用并入在途 flight，此处有界等待后独自完成。
        overlap.wait(2.0)
        return original(self)

    monkeypatch.setattr(OpenAICompatibleBackend, "_probe_tool_capability_uncached", counting_probe)
    outcomes, barrier = {}, threading.Barrier(len(routes))

    def call(name: str) -> None:
        barrier.wait(5)
        outcomes[name] = routes[name].probe_tool_capability().native_supported

    threads = [threading.Thread(target=call, args=(name,)) for name in routes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert outcomes == native
    assert len(physical) == 2
    key_a, key_b = (http._capability_probe_key(routes[name]) for name in ("a", "b"))
    assert key_a != key_b and "route-secret" not in repr((key_a, key_b))

    # 顺序读（阴性按凭据级逻辑身份摘除，并发阶段的发布先后不定，故只量本段增量）：同路由新实例（头名大小写
    #   不同）命中阳性 memo；另一路由的新实例不被它冒充，阴性不缓存而重新物理探测。
    same_b = _with_probe_answer(
        _make_apikey_backend(custom_headers={"x-route-variant": "route-secret-b"}), monkeypatch, native=True)
    assert same_b.probe_tool_capability().native_supported is True
    before = len(physical)
    again_b = _with_probe_answer(
        _make_apikey_backend(custom_headers={"X-ROUTE-VARIANT": "route-secret-b"}), monkeypatch, native=True)
    assert again_b.probe_tool_capability().native_supported is True
    assert len(physical) == before
    fresh_a = _with_probe_answer(
        _make_apikey_backend(custom_headers={"X-Route-Variant": "route-secret-a"}), monkeypatch, native=False)
    assert fresh_a.probe_tool_capability().native_supported is False
    assert len(physical) == before + 1

    # 实例路由头变更按字段名记账，原因里不含头值。
    assert same_b._cached_instance_capability() is not None
    same_b.custom_headers = {"X-Route-Variant": "route-secret-c"}
    assert same_b._cached_instance_capability() is None
    assert same_b._provider_tool_capability_miss_reason == "key_changed:route_headers"


def test_route_header_identity_ignores_name_case_and_order(monkeypatch) -> None:
    first = _make_apikey_backend(custom_headers={"X-Route-Variant": "v1", "X-Tenant": "t1"})
    second = _make_apikey_backend(custom_headers={"x-tenant": "t1", "X-ROUTE-VARIANT": "v1"})
    plain = _make_apikey_backend()
    assert http._route_header_fingerprint(first) == http._route_header_fingerprint(second) != ""
    assert http._route_header_fingerprint(plain) == ""
    assert http._capability_probe_key(first) == http._capability_probe_key(second) != http._capability_probe_key(plain)
    # 凭据指纹仍在键尾，逻辑身份（凭据级失效）覆盖所有路由头变体。
    assert http._capability_probe_key(first)[-1] == _credential_fingerprint(plain)
    probes = []
    for backend in (first, second):
        monkeypatch.setattr(backend, "generate",
                            lambda prompt, **_kw: probes.append(prompt) or _probe_ok_response(prompt))
    assert first.probe_tool_capability().native_supported is True
    assert second.probe_tool_capability().native_supported is True
    assert len(probes) == 1
