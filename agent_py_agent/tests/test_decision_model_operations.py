"""决策设置和显式原生探测走原模型操作入口；只使用临时 owner 与本机 HTTP。"""
import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.gateway_parts import http_handlers, model_profile_service
from agent_py_agent.agent.settings import thread_model_selection
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation as op
from agent_py_agent.agent.settings.model_profiles import model_profiles_path
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests import test_decision_service_http as http_fixture
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_service_http import server as server
from agent_py_agent.tests.test_decision_settings import host_at
from agent_py_agent.tests.test_gateway_model_profiles import Handler


# LLM: 使用正式原生HTTP和供应商解析，只替换本机服务的自有响应；不调用真实决策或生成模型。
# 函数用途: 提供可观测连接测试上下文并禁止普通模型初始化。
def setup_probe(tmp_path, server, monkeypatch, *, usage=None):
    host = host_at(tmp_path)
    key, _ = decision(host, api_base=server.url)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("决策操作不得初始化生成模型")

    monkeypatch.setattr(thread_model_selection, "thread_model_profile_id", forbidden)
    monkeypatch.setattr(http_fixture, "response", lambda: {"model": "jev-test", "answers": {
        "connection": {"type": "choice", "choice": "ready", "confidence": 1.0,
                       "probabilities": {"ready": 1.0, "unknown": 0.0}}}, **({"usage": usage} if usage is not None else {})})
    return host, key, thread


def test_model_operation_settings_share_cas_without_generation_setup(tmp_path, monkeypatch):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    monkeypatch.setattr(thread_model_selection, "thread_model_profile_id", lambda *_a, **_kw: pytest.fail("初始化了生成模型"))
    read = op(host, "decision_read", {"decision": {"scope": "thread"}}, thread_id=thread.thread_id)
    payload = {"decision": {"scope": "thread", "expected_revision": read["revision"], "changes": {"timeout_seconds": 6.5}}}
    saved = op(host, "decision_patch", payload, thread_id=thread.thread_id)
    assert saved["effective"]["timeout_seconds"] == 6.5
    with pytest.raises(ModelProfileError):
        op(host, "decision_patch", payload, thread_id=thread.thread_id)
    reset = op(host, "decision_reset", {"decision": {"scope": "thread", "expected_revision": saved["revision"],
        "fields": ["timeout_seconds"]}}, thread_id=thread.thread_id)
    assert reset["effective"]["timeout_seconds"] == 2
    assert host.conversation_store.threads.load(thread.thread_id).model_profile_id == thread.model_profile_id


@pytest.mark.parametrize("usage, expected", [(None, None), ({"input_tokens": 0}, 0), ({"input_tokens": 32, "output_tokens": 2}, 32)])
def test_explicit_native_probe_while_off_preserves_settings_and_uses_original_ledger(tmp_path, server, monkeypatch, usage, expected):
    host, key, thread = setup_probe(tmp_path, server, monkeypatch, usage=usage)
    path = model_profiles_path(host.home_paths)
    before = path.read_bytes()
    saved_thread = host.conversation_store.threads.load(thread.thread_id)
    result = op(host, "decision_probe", {"profile_id": key, "timeout_seconds": 1}, thread_id=thread.thread_id)
    assert result["ok"] and result["usage"]["input_tokens"] == expected
    assert result["error_type"] == "" and result["elapsed_seconds"] < 1
    assert path.read_bytes() == before
    assert host.conversation_store.threads.load(thread.thread_id) == saved_thread
    assert len(server.requests) == 1 and server.requests[0][0] == "/v1/systemone"
    body = server.requests[0][1]
    assert set(body) == {"model", "state", "questions"}
    assert body["state"] == {"purpose": "explicit_connection_test", "value": "ready"}
    assert "price" not in json.dumps(result) and "only-private-secret" not in json.dumps(result)
    record = host._model_call_ledger.records()[0]
    summary = model_call_summary(host, request_id=record.request_id)
    assert summary["purpose_breakdown"]["decision"]["physical_model_attempt_count"] == 1
    totals = host.conversation_store.model_usage.summary(thread.thread_id)
    assert totals["event_count"] == 1
    assert totals["purpose_breakdown"]["decision"]["physical_model_attempt_count"] == 1


def test_native_probe_timeout_is_bounded_and_does_not_retry(tmp_path, server, monkeypatch):
    host, key, thread = setup_probe(tmp_path, server, monkeypatch)
    server.block = True
    started = time.monotonic()
    result = op(host, "decision_probe", {"profile_id": key, "timeout_seconds": 0.08}, thread_id=thread.thread_id)
    assert time.monotonic() - started < 0.8
    assert not result["ok"] and result["error_type"]
    assert host._model_call_ledger.records()[0].status == "timed_out"
    assert len(server.requests) == 1 and result["usage"]["input_tokens"] is None
    server.release.set()


@pytest.mark.parametrize("seconds", [0, -1, True, float("nan"), float("inf"), "4"])
def test_invalid_probe_budget_never_calls_service(tmp_path, server, monkeypatch, seconds):
    host, key, thread = setup_probe(tmp_path, server, monkeypatch)
    with pytest.raises(ModelProfileError):
        op(host, "decision_probe", {"profile_id": key, "timeout_seconds": seconds}, thread_id=thread.thread_id)
    assert not server.requests


def test_probe_rejects_foreign_thread_before_request(tmp_path, server, monkeypatch):
    host, key, thread = setup_probe(tmp_path, server, monkeypatch)
    from dataclasses import replace

    host.conversation_store.threads.update_atomic(thread.thread_id, lambda t: replace(t, owner_id="bob"))
    result = op(host, "decision_probe", {"profile_id": key}, thread_id=thread.thread_id)
    assert not result["ok"] and not server.requests


def test_cold_gateway_decision_settings_use_authenticated_owner_without_agent(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_worker
    from agent_py_agent.agent.settings.config import AgentConfig

    monkeypatch.setattr(request_worker, "_owner_pool", lambda *_a: pytest.fail("不能创建完整Agent"))
    monkeypatch.setattr(thread_model_selection, "thread_model_profile_id", lambda *_a, **_kw: pytest.fail("不能初始化生成选择"))
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))
    server_host = SimpleNamespace(agent=SimpleNamespace(home_paths=home_paths(tmp_path), config=AgentConfig(gateway_per_user_owner_scoping=True)))
    handler = Handler({"operation": "decision_read", "conversation_id": "settings", "decision": {"scope": "thread"}})
    model_profile_service.handle_client_models(handler, server_host)
    assert handler.reply[0] == 200 and handler.reply[1]["ok"]
    patcher = Handler({"operation": "decision_patch", "conversation_id": "settings", "user_id": "bob", "decision": {
        "scope": "owner", "expected_revision": handler.reply[1]["revision"], "changes": {"timeout_seconds": 3.5}}})
    model_profile_service.handle_client_models(patcher, server_host)
    assert patcher.reply[0] == 200 and patcher.reply[1]["effective"]["timeout_seconds"] == 3.5
    other = Handler({"operation": "decision_read", "conversation_id": "settings"}, user="bob")
    model_profile_service.handle_client_models(other, server_host)
    assert other.reply[0] == 200 and other.reply[1]["effective"]["timeout_seconds"] == 2


@pytest.mark.parametrize("operation, payload, timeout", [
    ("decision_probe", {"timeout_seconds": 25.0}, 35.0), ("decision_probe", {}, 14.0),
    ("probe", {}, 180.0), ("decision_read", {}, 10.0), ("discover", {}, 30.0),
])
def test_thin_client_probe_transport_preserves_user_budget_and_other_operations(operation, payload, timeout):
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    seen = []
    host = SimpleNamespace(post_gateway_json=lambda path, body, **kwargs: (seen.append((path, body, kwargs)) or (200, {"ok": True})))
    assert GatewayChatClientAgent.request_models(host, session_id="s", operation=operation, payload=payload)["ok"]
    assert seen == [("/client/models", {**payload, "operation": operation, "conversation_id": "s"}, {"timeout": timeout})]


def test_explicit_success_clears_only_matching_decision_connection_cooldown(tmp_path, server, monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderTransientError
    from agent_py_agent.agent.conversation.decision_policy import (
        connection_revision,
        cooldown_state,
        decision_owner_ref,
        record_failure,
    )
    from agent_py_agent.agent.settings.decision_settings_projection import decision_profile
    from agent_py_agent.agent.settings.model_profiles import read_model_profiles

    host, key, thread = setup_probe(tmp_path, server, monkeypatch)
    config = decision_profile(host, read_model_profiles(model_profiles_path(host.home_paths)), key)
    revision = connection_revision(config)
    blocked = (decision_owner_ref(host), key, revision)
    unrelated = ("other-owner", key, revision)
    for identity in (blocked, unrelated):
        record_failure(identity, "policy", ProviderTransientError("temporary"))
    assert cooldown_state(blocked, "policy")[0] == "cooldown"
    assert op(host, "decision_probe", {"profile_id": key}, thread_id=thread.thread_id)["ok"]
    assert cooldown_state(blocked, "policy") == ("", 0.0)
    assert cooldown_state(unrelated, "policy")[0] == "cooldown"
    cooldown_state(unrelated, "policy", retry=True)
