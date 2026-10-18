"""原HTTP handler离线受理合同：不启动socket、服务、模型或业务worker。"""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import http_handlers as http
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture
def server(tmp_path, monkeypatch):
    root = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    assert root != (Path.home() / ".my-agent").resolve()
    # 这里只测试受理的轻量宿主；chat为原transcript通道，不物化owner Agent或执行模型。
    agent = SimpleNamespace(config=AgentConfig(my_agent_home=str(root)), home_paths=home_paths(root),
                            root=tmp_path / "workspace")
    return SimpleNamespace(agent=agent, paths=gateway_paths_from_root(tmp_path / "gateway"), _test_monkeypatch=monkeypatch)


def _body():
    return {"conversation_id": "fixture-thread", "metadata": {"message_id": "fixture-message",
            "sender_type": "user"}, "input_media": [{"kind": "image", "ref": "fixture-media-ref"}]}


# LLM: Count0保原直接helper；C必须在真实Root/原pool finish_request区间调用原handler，不伪造work/来源；只捕获运输输出，原owner/摘要断言不变。
# 函数用途: 离线提交同一普通请求；显式认证夹具只用原middleware与本例凭据，默认保原单机调用形状。
def _submit(server, body, *, user_id="local-agent", channel="chat", authenticated=False):
    sent = []
    handler = SimpleNamespace(_auth_middleware=None, _send_json=lambda code, payload: sent.append((code, payload)))
    if authenticated:
        from email.message import Message

        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.middleware import AuthMiddleware

        headers = Message()
        headers["X-Gateway-Token"] = "fixture-owned-g1"
        headers["X-User-Id"], headers["X-Channel"] = user_id, channel
        handler._auth_middleware = AuthMiddleware(AuthManager(auth_enabled=True), auth_token="fixture-owned-g1")
        handler.headers, handler.client_address = headers, ("127.0.0.1", 1)
        assert http._request_identity(handler)[0] == user_id
    request_id, digest = http._http_idempotent_request_identity(body, goal="合成正文", user_id=user_id, channel=channel)
    def invoke():
        http._handle_idempotent_ordinary_ask(
            handler, server, body=body, goal="合成正文", user_id=user_id, channel=channel,
            request_id=request_id, client_input_digest=digest, allow_active_turn=False,
        )
    if getattr(server, 'control_plane', None) is None:
        invoke()
    else:
        from agent_py_agent.tests.test_gateway_http_current_work import http_case, run_work

        control = server.control_plane
        errors = []
        with http_case(control, server._test_monkeypatch) as (lower, runtime, _group, socket, actual_handler):
            server.server, server.drain_runtime = lower, runtime
            actual_handler._auth_middleware = handler._auth_middleware
            if authenticated:
                actual_handler.headers = handler.headers
                actual_handler.client_address = handler.client_address
            actual_handler._send_json = handler._send_json
            handler = actual_handler

            def actual():
                try:
                    invoke()
                except BaseException as error:
                    errors.append(error)
            run_work(lower, socket, actual)
        if errors:
            raise errors[0]
    assert len(sent) == 1
    return sent[0], request_id


def test_count_zero_http_replay_never_rebinds_or_rereads_host_preparation(server, monkeypatch):
    (status, _result), request_id = _submit(server, _body())
    assert status == 202
    first = read_gateway_input_receipt(server.paths, request_id)
    assert first.state == "queued"
    assert frozen_request_owner(first.prepared_request) == OwnerIdentity.local_main()
    assert "gateway_admin_binding_version" not in first.prepared_request["metadata"]

    def unexpected(*_args, **_kwargs):
        raise AssertionError("已有HTTP回执不得再读owner/偏好/当前配置")

    monkeypatch.setattr(request_worker, "_resolve_request_owner_identity", unexpected)
    (status, _result), replay_id = _submit(server, _body())
    assert status == 202 and replay_id == request_id
    assert read_gateway_input_receipt(server.paths, request_id).prepared_request == first.prepared_request
    assert len(list(server.paths.inbox.glob("*.json"))) == 1


@pytest.mark.parametrize("fact", ["actor", "metadata", "media"])
def test_replay_still_checks_original_client_digest_for_changed_facts(server, fact):
    body = _body()
    (status, _result), request_id = _submit(server, body)
    assert status == 202
    original = read_gateway_input_receipt(server.paths, request_id)
    changed = copy.deepcopy(body)
    if fact == "actor":
        changed["metadata"]["sender_type"] = "bot"
    elif fact == "metadata":
        changed["metadata"]["other_fact"] = "changed"
    else:
        changed["input_media"][0]["ref"] = "other-media-ref"
    (status, result), same_id = _submit(server, changed)
    assert same_id == request_id and status == 409
    assert result["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert read_gateway_input_receipt(server.paths, request_id) == original


@pytest.mark.parametrize("root_enabled,owner_enabled,subagents,expected", [
    (False, True, True, "original"), (True, False, True, "delegated"),
    (True, True, False, "original"),
])
def test_real_c_http_cold_capture_keeps_original_constructor_config_source(
    server, monkeypatch, root_enabled, owner_enabled, subagents, expected,
):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
    from agent_py_agent.agent.settings.im_task_preferences import set_delegation_preference
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    owner = OwnerIdentity.provider_user("feishu", "fixture-user")
    config, constructor_root = server.agent.config, server.agent.root
    config.enable_subagents = subagents
    scoped_home = home_paths_with_owner(server.agent.home_paths, resolve_owner_home(server.agent.home_paths.root, owner))
    for base, enabled in ((constructor_root, root_enabled), (scoped_home.owner_home_dir, owner_enabled)):
        target = base / "config" / "capability_config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("im_task_delegation_enabled: " + str(enabled).lower() + "\n")
    set_delegation_preference(SimpleNamespace(home_paths=scoped_home, config=_config_with_owner(config, owner)),
                              "feishu", "fixture-user", True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("C受理不能物化业务Agent")

    monkeypatch.setattr(SimpleAgent, "__init__", forbidden)
    with coordinator_guard(server.paths.root, 2):
        prepare_coordinator_takeover(server.paths, 2, "fixture-c")
        control = GatewayControlPlane(config, server.agent.home_paths.root, server.paths, 2, "fixture-c",
                                      agent_root=constructor_root)
        server.agent = None
        server.control_plane = control
        body = _body()
        body["metadata"]["channel_chat_type"] = "p2p"
        (status, _result), request_id = _submit(server, body, channel="feishu", user_id="fixture-user")
        assert status == 202
        prepared = read_gateway_input_receipt(server.paths, request_id).prepared_request
        assert frozen_request_owner(prepared) == owner
        assert prepared["host_im_dispatch.v1"]["mode"] == expected
        assert prepared["metadata"]["gateway_admin_binding_version"] == "none"
        assert prepared["input_media"] == body["input_media"]
        from agent_py_agent.agent.user_space import admin_channel_identity

        monkeypatch.setattr(admin_channel_identity, "read_private_config_json", forbidden)
        assert _submit(server, body, channel="feishu", user_id="fixture-user")[0][0] == 202
        assert read_gateway_input_receipt(server.paths, request_id).prepared_request == prepared
