"""W2 模型摘要、菜单真实结构和结果正文合同；组件测试不冒充HTTP接线验收。"""

import http.client
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.models import AgentRunResult
from agent_py_agent.agent.gateway_parts import http_handlers, model_profile_service, recovery
from agent_py_agent.agent.gateway_parts import status_model_summary as summary_module
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_execution import _update_response_from_result
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)
from agent_py_agent.tests.test_gateway_coordinator_client_history import (
    case as coordinator_case,  # noqa: F401
)
from agent_py_agent.tests.test_gateway_identity_trust import _TOKEN, _server
from agent_py_agent.tests.test_gateway_model_profiles import Handler
from agent_py_agent.tests.test_model_profiles import profile


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    # 外层pytest也以项目tmp作HOME；每例再隔离环境，禁止依赖个人配置。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))


def _host(tmp_path):
    config = AgentConfig()
    home = home_paths(tmp_path / "home")
    owner = resolve_owner_home(home.root, owner_identity_from_config(config))
    return SimpleNamespace(config=config, home_paths=home_paths_with_owner(home, owner))


def _configured_host(tmp_path):
    host = _host(tmp_path)
    added = execute_model_profile_operation(host, "add", {"profile_id": str(uuid4()), "profile": profile()})
    key = added["profiles"][0]["id"]
    execute_model_profile_operation(host, "set_default", {"profile_id": key})
    return host


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("coordinator", [False, True])
def test_status_configured_read_only_without_agent_or_backend(tmp_path, monkeypatch, trusted, coordinator):
    from agent_py_agent.agent import backends, core

    host = _configured_host(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("只读摘要不能物化执行宿主")

    monkeypatch.setattr(core.SimpleAgent, "__init__", forbidden)
    monkeypatch.setattr(backends, "get_backend", forbidden)
    control = SimpleNamespace(config=host.config, home_root=host.home_paths.root)
    server = SimpleNamespace(agent=None if coordinator else host,
                             control_plane=control if coordinator else None)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = summary_module.status_model_summary(server, trusted=trusted)
    assert result == {"model_configured": True, "default_model": "MiniMax-M2.7" if trusted else ""}
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_status_unconfigured_does_not_use_deployment_model(tmp_path):
    host = _host(tmp_path)
    host.config.model_name = "deployment-only"
    host.config.api_key = "not-a-profile-key"
    assert summary_module.status_model_summary(SimpleNamespace(agent=host), trusted=True) == {
        "model_configured": False, "default_model": ""}


@pytest.mark.parametrize("damage", ["missing_key", "disabled", "corrupt"])
def test_status_unavailable_and_errors_are_structured_and_secret_free(tmp_path, damage):
    host = _configured_host(tmp_path)
    path = model_profiles_path(host.home_paths)
    data = json.loads(path.read_text())
    if damage == "corrupt":
        path.write_text("{private-secret-invalid-json", encoding="utf-8")
    else:
        provider = next(iter(data["providers"].values()))
        if damage == "missing_key":
            provider["api_key"] = ""
        else:
            provider["enabled"] = False
        path.write_text(json.dumps(data), encoding="utf-8")
    result = summary_module.status_model_summary(SimpleNamespace(agent=host), trusted=False)
    assert result["model_configured"] is False and result["default_model"] == ""
    if damage == "corrupt":
        assert result["model_summary_error"] == "MODEL_SUMMARY_READ_FAILED"
    assert "private-secret" not in json.dumps(result)


def test_status_missing_source_and_read_failure_keep_required_keys(monkeypatch):
    assert summary_module.status_model_summary(None) == {
        "model_configured": False, "default_model": "",
        "model_summary_error": "MODEL_SUMMARY_SOURCE_UNAVAILABLE"}

    def broken(_):
        raise PermissionError("private-secret-path")

    monkeypatch.setattr(summary_module, "_configuration_host", broken)
    assert summary_module.status_model_summary(object(), trusted=True) == {
        "model_configured": False, "default_model": "",
        "model_summary_error": "MODEL_SUMMARY_READ_FAILED"}


@pytest.mark.parametrize("conversation", [None, "explicit-conversation"])
def test_models_list_real_schema_with_and_without_conversation(tmp_path, monkeypatch, conversation):
    base = _host(tmp_path)
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda _: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda _: ("alice", "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda _: ("alice", None))
    server = SimpleNamespace(agent=base)
    body = {} if conversation is None else {"conversation_id": conversation}
    handler = Handler({**body, "operation": "add", "profile_id": str(uuid4()), "profile": profile()})
    model_profile_service.handle_client_models(handler, server)
    assert handler.reply[0] == 200, handler.reply
    handler = Handler({**body, "operation": "list"})
    model_profile_service.handle_client_models(handler, server)
    status, result = handler.reply
    assert status == 200 and result["ok"] is True
    assert isinstance(result["profiles"], list) and len(result["profiles"]) == 1
    assert {"id", "model_name", "api_base", "provider_name", "has_key", "auth_mode",
            "available", "available_for"} <= result["profiles"][0].keys()
    assert isinstance(result["model_execution"], dict)
    assert "only-private-secret" not in json.dumps(result)


@pytest.mark.parametrize("content", ["# 已完成\n\n正文。", ""])
def test_result_done_component_roundtrip_pins_projected_body(tmp_path, monkeypatch, content):
    request_id = "w2-result"
    request = {"id": request_id, "kind": "ask", "user_id": "alice", "prompt": "测试"}
    result = AgentRunResult(prompt="测试", response="不得越过交付边界的内部候选", backend="echo", used_memories=0,
                            channel_delivery={"content": content})
    response = {"id": request_id}
    _update_response_from_result(response, result, request)
    assert response["status"] == "done"
    paths = gateway_paths_from_root(tmp_path / "gateway")
    terminal = recovery._terminal_gateway_request_payload(request_id, request, response)
    recovery._commit_gateway_terminal_request(paths, paths.processing / f"{request_id}.json",
                                               paths.done, request_id, terminal)
    handler = Handler({})
    handler.path = f"/result/{request_id}"
    monkeypatch.setattr(http_handlers, "_request_identity", lambda _: ("alice", None))
    http_handlers.handle_result(handler, SimpleNamespace(paths=paths, agent=None))
    status, payload = handler.reply
    assert status == 200 and payload["status"] == "done"
    assert payload["response"] == payload["channel_delivery"]["content"] == content
    assert "result" not in payload
    assert "内部候选" not in json.dumps(payload, ensure_ascii=False)


def _wire(server, path, body=None, *, credential=_TOKEN):
    """使用真实回环 HTTP，不替换路由、中间件或客户端解析。"""
    connection = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=5)
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["X-Gateway-Token"] = credential
    try:
        connection.request("GET" if body is None else "POST", path,
                           body=None if body is None else json.dumps(body).encode(), headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _configure(server):
    host = summary_module._configuration_host(server)
    added = execute_model_profile_operation(host, "add", {"profile_id": str(uuid4()), "profile": profile()})
    execute_model_profile_operation(host, "set_default", {"profile_id": added["profiles"][0]["id"]})
    return host


@pytest.mark.parametrize("coordinator", [False, True])
def test_status_http_public_trust_and_no_agent_construction(tmp_path, monkeypatch, request, coordinator):
    from agent_py_agent.agent import backends, core

    if coordinator:
        server = request.getfixturevalue("coordinator_case").server
        credential = server._local_client_credential
    else:
        server, credential = _server(tmp_path), _TOKEN
        server.start()
    try:
        status, unconfigured = _wire(server, "/status", credential=credential)
        assert status == 200 and unconfigured["model_configured"] is False
        host = _configure(server)

        def forbidden(*args, **kwargs):
            raise AssertionError("状态查询不能构造执行宿主或后端")

        monkeypatch.setattr(core.SimpleAgent, "__init__", forbidden)
        monkeypatch.setattr(backends, "get_backend", forbidden)
        for trusted in (False, True):
            status, result = _wire(server, "/status", credential=credential if trusted else "")
            assert status == 200 and result["model_configured"] is True
            assert result["default_model"] == ("MiniMax-M2.7" if trusted else "")
        model_profiles_path(host.home_paths).write_text("{synthetic-corrupt-catalog", encoding="utf-8")
        status, result = _wire(server, "/status", credential=credential)
        assert status == 200 and result["model_configured"] is False
        assert result["model_summary_error"] == "MODEL_SUMMARY_READ_FAILED"
        assert "synthetic-corrupt" not in json.dumps(result)
    finally:
        if not coordinator:
            server.stop()


@pytest.mark.parametrize("conversation", [None, "explicit-conversation"])
def test_models_list_http_roundtrip_without_active_chat(tmp_path, conversation):
    server = _server(tmp_path)
    _configure(server)
    server.start()
    try:
        body = {"operation": "list"}
        if conversation is not None:
            body["conversation_id"] = conversation
        status, result = _wire(server, "/client/models", body)
        assert status == 200 and result["ok"] is True
        assert isinstance(result["profiles"], list) and len(result["profiles"]) == 1
        assert {"id", "model_name", "api_base", "provider_name", "has_key", "auth_mode", "available", "available_for"} <= result["profiles"][0].keys()
        assert isinstance(result["model_execution"], dict)
    finally:
        server.stop()


def test_result_done_http_roundtrip_uses_canonical_terminal_writer(tmp_path):
    server = _server(tmp_path)
    server.start()
    try:
        status, queued = _wire(server, "/ask", {"goal": "结果字段测试"})
        assert status == 202, queued
        request_id = queued["request_id"]
        pending = server.paths.inbox / f"{request_id}.json"
        request = json.loads(pending.read_text())
        status, pending_result = _wire(server, f"/result/{request_id}")
        assert status == 202 and pending_result["status"] == "queued"
        processing = server.paths.processing / pending.name
        pending.replace(processing)
        content = "# 已完成\n\n真实 HTTP 合同正文。"
        response = {"id": request_id}
        result = AgentRunResult(prompt="结果字段测试", response="内部候选不能回退显示", backend="echo", used_memories=0,
                                channel_delivery={"content": content})
        _update_response_from_result(response, result, request)
        terminal = recovery._terminal_gateway_request_payload(request_id, request, response)
        recovery._commit_gateway_terminal_request(server.paths, processing, server.paths.done, request_id, terminal)
        status, result = _wire(server, f"/result/{request_id}")
        assert status == 200 and result["status"] == "done", result
        assert result["response"] == result["channel_delivery"]["content"] == content
        assert "result" not in result and "内部候选" not in json.dumps(result, ensure_ascii=False)
    finally:
        server.stop()
