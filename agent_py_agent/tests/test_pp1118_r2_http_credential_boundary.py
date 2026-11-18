"""PP1118 Round-2 Slice 3 (`pp-r2-3`): HTTP 认证经 C1 逐次授权与首个网络发送前回合脱敏登记边界回归。

全部用例使用真实 SimpleAgent.tools.tools['web_fetch']（保持 core.py 默认三参数 credential_authenticator 绑定未改），
仅在本地 127.0.0.1 环回测试服务或末端 fetch_raw_response 传输层观察执行顺序与回显净化。
"""
from __future__ import annotations

import contextlib
import inspect
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote_plus

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.common import log_redaction
from agent_py_agent.agent.common.json_io import locked_json_path, read_json_object_report
from agent_py_agent.agent.common.log_redaction import (
    RedactingFormatter,
    configured_secret_redaction,
)
from agent_py_agent.agent.conversation.host_actor import actor_from_scope
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import web_fetch_tools
from agent_py_agent.agent.tooling.host_output_redaction import (
    credential_echo_variants,
    turn_credential_redaction,
)
from agent_py_agent.agent.user_space import task_credentials
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.task_credential_http import credential_authenticated_request
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    _credential_mutation,
    _save_credential_row,
    credential_path,
    manage_credential,
    request_credential_card,
)
from agent_py_agent.agent.workflows import WorkflowEngine
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


class _LoopbackAuthServer:
    """本机 127.0.0.1 环回测试站点：记录到达服务端的每一跳请求并在处理期调用观察钩子。"""

    def __init__(self) -> None:
        outer = self
        self.events: list[dict[str, object]] = []
        self.on_request = None
        self.record_marker = f"HTTP_AUTH_OK_{uuid.uuid4().hex[:10]}"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def _reply(
                self,
                status: int,
                content_type: str,
                body: bytes,
                extra_headers: list[tuple[str, str]] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                for key, val in extra_headers or ():
                    self.send_header(key, val)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                headers = dict(self.headers.items())
                event = {"method": "GET", "path": self.path, "headers": headers, "body": b""}
                outer.events.append(event)
                if callable(outer.on_request):
                    outer.on_request(event)
                if self.path == "/bearer-record":
                    auth = headers.get("Authorization", "")
                    if not auth.startswith("Bearer "):
                        self._reply(401, "text/plain; charset=utf-8", b"unauthorized")
                        return
                    token = auth[len("Bearer ") :]
                    echo = f"{outer.record_marker} raw={token} url={quote_plus(token, safe='')}"
                    self._reply(200, "text/plain; charset=utf-8", echo.encode())
                    return
                if self.path == "/json-protected":
                    if headers.get("Authorization") != "Bearer IssuedJsonSessionToken-701":
                        self._reply(401, "text/plain; charset=utf-8", b"unauthorized")
                        return
                    body = f"{outer.record_marker} tok=IssuedJsonSessionToken-701".encode()
                    self._reply(200, "text/plain; charset=utf-8", body)
                    return
                if self.path == "/form-protected":
                    cookie = headers.get("Cookie", "")
                    if (
                        headers.get("Authorization") != "Bearer IssuedFormSessionToken-702"
                        or "sid=IssuedFormCookie-703" not in cookie
                        or headers.get("X-CSRF-Token") != "IssuedFormCsrf-704"
                    ):
                        self._reply(401, "text/plain; charset=utf-8", b"unauthorized")
                        return
                    body = (
                        f"{outer.record_marker} IssuedFormSessionToken-702 IssuedFormCookie-703 IssuedFormCsrf-704"
                    ).encode()
                    self._reply(200, "text/plain; charset=utf-8", body)
                    return
                if self.path == "/overflow-protected":
                    self._reply(200, "text/plain; charset=utf-8", outer.record_marker.encode())
                    return
                self._reply(404, "text/plain; charset=utf-8", b"not found")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b""
                headers = dict(self.headers.items())
                event = {"method": "POST", "path": self.path, "headers": headers, "body": raw}
                outer.events.append(event)
                if callable(outer.on_request):
                    outer.on_request(event)
                ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if self.path == "/login-json":
                    if ctype != "application/json":
                        self._reply(415, "application/json", b'{"error":"expected_json"}')
                        return
                    parsed = json.loads(raw.decode("utf-8"))
                    event["parsed"] = parsed
                    self._reply(
                        200,
                        "application/json",
                        b'{"access_token":"IssuedJsonSessionToken-701"}',
                    )
                    return
                if self.path == "/login-form":
                    if ctype != "application/x-www-form-urlencoded":
                        self._reply(415, "application/json", b'{"error":"expected_form"}')
                        return
                    parsed = {
                        k: v[0]
                        for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()
                    }
                    event["parsed"] = parsed
                    self._reply(
                        200,
                        "application/json",
                        b'{"access_token":"IssuedFormSessionToken-702"}',
                        extra_headers=[
                            ("Set-Cookie", "sid=IssuedFormCookie-703; Path=/"),
                            ("Set-Cookie", "csrf=IssuedFormCsrf-704; Path=/"),
                        ],
                    )
                    return
                if self.path == "/login-overflow":
                    self._reply(
                        200,
                        "application/json",
                        b'{"access_token":"IssuedOverflowSessionToken-999"}',
                        extra_headers=[("Set-Cookie", "sid=IssuedOverflowCookie-998; Path=/")],
                    )
                    return
                self._reply(404, "text/plain; charset=utf-8", b"not found")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def loopback_sites():
    primary = _LoopbackAuthServer()
    secondary = _LoopbackAuthServer()
    try:
        yield primary, secondary
    finally:
        primary.close()
        secondary.close()


def _build_real_agent(tmp_path: Path) -> SimpleAgent:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_plugins=False,
            my_agent_home=str(tmp_path / "home"),
            my_agent_owner_provider="local",
            my_agent_owner_kind="main",
            my_agent_owner_id="main",
            gateway_port=0,
        ),
        tmp_path / "workspace",
    )
    agent.config.feishu_session_lock_enabled = False
    bind_admin_channel_identity(agent.home_paths.root, "feishu", "ou_http_owner")
    agent.channel_registry.register_adapter(
        "feishu",
        SimpleNamespace(
            send_control_card=lambda *_args, **_kwargs: {
                "message_id": "om_" + uuid.uuid4().hex,
                "chat_id": "oc_dm_http_owner",
            }
        ),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )
    web_tool = agent.tools.tools["web_fetch"]
    # 验证保持 core.py:1415 原生三参数 credential_authenticator 绑定未做任何测试重绑。
    sig = tuple(inspect.signature(web_tool.credential_authenticator).parameters)
    assert sig == ("ref", "request", "send")
    return agent


def _start_turn(
    agent: SimpleAgent,
    *,
    actor_id: str = "ou_http_owner",
    conversation_id: str = "oc_dm_http_owner",
) -> tuple[RunParams, dict[str, object]]:
    old = getattr(agent, "_current_run_params", None)
    if old is not None:
        old_tid = (getattr(old, "task_attributes", None) or {}).get("conversation_thread_id", "")
        if old_tid:
            existing_claim = agent.conversation_store.claims.load(old_tid)
            if existing_claim:
                agent.conversation_store.claims.finish(
                    {"thread_id": old_tid, "claim_id": existing_claim["claim_id"], "status": "done"}
                )
    root_id = "gwreq-" + uuid.uuid4().hex
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "channel": "feishu",
            "channel_conversation_id": conversation_id,
            "channel_user_id": actor_id,
            "canonical_user_id": actor_id,
            "owner_id": agent.home_paths.owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
        }
    )
    host_actor = actor_from_scope(
        agent,
        SimpleNamespace(
            channel="feishu",
            channel_user_id=actor_id,
            channel_conversation_id=conversation_id,
        ),
        thread.thread_id,
        root_id,
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": root_id, "goal": "执行受保护 HTTP 请求", "status": "active"},
        host_channel_actor=host_actor,
    )
    claim = store.claims.acquire({"thread_id": thread.thread_id, "task_id": root_id, "reason": "thread_goal_continue"})
    params = RunParams(
        request_id=root_id,
        task_id=root_id,
        run_id=root_id,
        task_attributes={"conversation_thread_id": thread.thread_id},
        host_channel_actor=host_actor,
    )
    agent._current_run_params = params
    return params, claim


def _issue_submitted_card(
    agent: SimpleAgent,
    url: str,
    *,
    authentication: str,
    fields_values: dict[str, str],
    fields_spec: list[dict[str, str]] | None = None,
    auth_config: dict[str, object] | None = None,
    collection_mode: str | None = None,
) -> tuple[str, Path]:
    opt_kwargs: dict[str, object] = {
        "fields": fields_spec,
        "auth_config": auth_config,
        "access_scope": "task",
    }
    if collection_mode is not None:
        opt_kwargs["collection_mode"] = collection_mode
    options = CredentialCardOptions(**opt_kwargs)
    ref, _ = request_credential_card(agent, url=url, authentication=authentication, options=options)
    path = credential_path(agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="r2.http.test").payload
    body = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": dict(fields_values),
    }
    res = resolve_private_interaction(agent, body, channel="feishu", actor_id=row["actor_id"])
    assert res["ok"] is True
    return ref, path


def _invoke_production_web_fetch(agent: SimpleAgent, tool_args: dict[str, object]):
    params = agent._current_run_params
    base_boundary = write_boundary_with_runtime_ledger(agent, params) or {}
    boundary = {
        **base_boundary,
        "allowed_private_hosts": ["127.0.0.1"],
        "allow_private_resolution": True,
        "network_access_mode": "enabled",
    }
    return execute_registry_test_call(
        agent.tools,
        "web_fetch",
        tool_args,
        run_id=str(params.run_id or params.task_id),
        call_id="call-" + uuid.uuid4().hex[:8],
        write_boundary=boundary,
        register_with=agent,
        allowed_tools=["web_fetch"],
    )


def _spy_c1_and_cipher(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    stats: dict[str, object] = {"c1_calls": [], "cipher_decrypts": 0}
    orig_c1 = task_credentials.authorized_credential_material
    orig_cipher = task_credentials._cipher

    @contextlib.contextmanager
    def wrapped_c1(agent, reference, *, consumer, target_url=None, expected_generation=None, browser_consumer=None):
        stats["c1_calls"].append(
            {
                "reference": reference,
                "consumer": consumer,
                "target_url": target_url,
                "expected_generation": expected_generation,
            }
        )
        with orig_c1(
            agent,
            reference,
            consumer=consumer,
            target_url=target_url,
            expected_generation=expected_generation,
            browser_consumer=browser_consumer,
        ) as material:
            yield material

    def wrapped_cipher(*args, **kwargs):
        real = orig_cipher(*args, **kwargs)

        class CipherProxy:
            def encrypt(self, data: bytes) -> bytes:
                return real.encrypt(data)

            def decrypt(self, token: bytes) -> bytes:
                stats["cipher_decrypts"] = int(stats["cipher_decrypts"]) + 1
                return real.decrypt(token)

        return CipherProxy()

    monkeypatch.setattr(task_credentials, "authorized_credential_material", wrapped_c1)
    monkeypatch.setattr(task_credentials, "_cipher", wrapped_cipher)
    monkeypatch.setattr("agent_py_agent.agent.user_space.task_credential_http._cipher", wrapped_cipher)
    return stats


def _assert_zero_variants_in_text(text: str, secrets: tuple[str, ...]) -> None:
    for variant in credential_echo_variants(secrets):
        if len(variant) >= 4:
            assert variant not in text


def test_bearer_and_session_login_json_and_form_route_through_c1_and_register_before_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loopback_sites,
) -> None:
    primary, _secondary = loopback_sites
    agent = _build_real_agent(tmp_path)
    _start_turn(agent)
    stats = _spy_c1_and_cipher(monkeypatch)

    # 1) Bearer 请求：未经重绑的生产三参数 web_fetch 走 C1(consumer='http')，且在首个 send() 执行当刻已登记原值与派生头
    bearer_secret = "SyntheticR2BearerSecret-9104!?"
    bearer_url = primary.origin + "/bearer-record"
    bearer_ref, bearer_path = _issue_submitted_card(
        agent,
        bearer_url,
        authentication="bearer",
        fields_values={"secret": bearer_secret},
    )

    send_observations: list[dict[str, object]] = []
    orig_fetch_raw = web_fetch_tools.fetch_raw_response

    def observing_fetch_raw(request, **kwargs):
        turn_red = turn_credential_redaction(agent)
        # 验证网络发送期间绝不持有凭据记录文件锁
        with locked_json_path(bearer_path, blocking=False):
            lock_free = True
        send_observations.append(
            {
                "url": request.url,
                "method": request.method,
                "turn_active": turn_red.active,
                "raw_registered": turn_red.contains_credential_echo(bearer_secret),
                "header_registered": turn_red.contains_credential_echo(f"Bearer {bearer_secret}"),
                "c1_count_at_send": len(stats["c1_calls"]),
                "decrypts_at_send": stats["cipher_decrypts"],
                "lock_free": lock_free,
            }
        )
        return orig_fetch_raw(request, **kwargs)

    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", observing_fetch_raw)
    bearer_result = _invoke_production_web_fetch(
        agent,
        {"url": bearer_url, "credential_ref": bearer_ref, "mode": "text"},
    )
    assert bearer_result.ok is True
    assert primary.record_marker in bearer_result.output
    assert "[private]" in bearer_result.output
    _assert_zero_variants_in_text(bearer_result.output, (bearer_secret, f"Bearer {bearer_secret}"))
    assert len(send_observations) == 1
    assert send_observations[0] == {
        "url": bearer_url,
        "method": "GET",
        "turn_active": True,
        "raw_registered": True,
        "header_registered": True,
        "c1_count_at_send": 1,
        "decrypts_at_send": 1,
        "lock_free": True,
    }
    assert stats["c1_calls"][-1] == {
        "reference": bearer_ref,
        "consumer": "http",
        "target_url": bearer_url,
        "expected_generation": 1,
    }

    # 2) session_login 缺省 JSON 与显式 body_encoding='form'：首跳登录前登记原字段，登录响应后、业务请求前立即补登记新发会话值
    for encoding, login_path, target_path, user_val, pass_val, issued_secrets in (
        (
            None,
            "/login-json",
            "/json-protected",
            "JsonLoginAcct-401",
            "JsonLoginPass-402!#",
            ("IssuedJsonSessionToken-701", "Bearer IssuedJsonSessionToken-701"),
        ),
        (
            "form",
            "/login-form",
            "/form-protected",
            "FormLoginAcct-501",
            "FormLoginPass-502!#",
            (
                "IssuedFormSessionToken-702",
                "Bearer IssuedFormSessionToken-702",
                "IssuedFormCookie-703",
                "IssuedFormCsrf-704",
            ),
        ),
    ):
        _start_turn(agent)
        auth_cfg: dict[str, object] = {
            "url": primary.origin + login_path,
            "body_fields": {"u": "username", "p": "secret"},
            "token_path": ["access_token"],
        }
        if encoding == "form":
            auth_cfg["body_encoding"] = "form"
            auth_cfg["cookies"] = True
            auth_cfg["csrf_cookie"] = "csrf"
        login_ref, login_card_path = _issue_submitted_card(
            agent,
            primary.origin + target_path,
            authentication="login",
            fields_spec=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
            fields_values={"username": user_val, "secret": pass_val},
            auth_config=auth_cfg,
        )
        hop_observations: list[dict[str, object]] = []
        c1_before = len(stats["c1_calls"])
        decrypts_before = int(stats["cipher_decrypts"])

        def observe_login_flow(request, _card_path=login_card_path, _u=user_val, _p=pass_val, _issued=issued_secrets, **kwargs):
            turn_red = turn_credential_redaction(agent)
            with locked_json_path(_card_path, blocking=False):
                unlocked = True
            hop_observations.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "unlocked": unlocked,
                    "raw_user_registered": turn_red.contains_credential_echo(_u),
                    "raw_pass_registered": turn_red.contains_credential_echo(_p),
                    "issued_all_registered": all(turn_red.contains_credential_echo(tok) for tok in _issued),
                    "c1_delta": len(stats["c1_calls"]) - c1_before,
                    "decrypt_delta": int(stats["cipher_decrypts"]) - decrypts_before,
                }
            )
            return orig_fetch_raw(request, **kwargs)

        monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", observe_login_flow)
        login_res = _invoke_production_web_fetch(
            agent,
            {"url": primary.origin + target_path, "credential_ref": login_ref, "mode": "text"},
        )
        assert login_res.ok is True
        assert primary.record_marker in login_res.output
        _assert_zero_variants_in_text(login_res.output, (user_val, pass_val, *issued_secrets))
        assert len(hop_observations) == 2
        # 第 1 跳（POST 登录）：发包当刻已完成 C1 授权且原账号/密码已登记进当前回合
        assert hop_observations[0] == {
            "method": "POST",
            "url": primary.origin + login_path,
            "unlocked": True,
            "raw_user_registered": True,
            "raw_pass_registered": True,
            "issued_all_registered": False,
            "c1_delta": 1,
            "decrypt_delta": 1,
        }
        # 第 2 跳（GET 业务请求）：无需二次解密原卡，且登录签发的所有新 token/Cookie/CSRF 已在发包前完成回合登记
        assert hop_observations[1] == {
            "method": "GET",
            "url": primary.origin + target_path,
            "unlocked": True,
            "raw_user_registered": True,
            "raw_pass_registered": True,
            "issued_all_registered": True,
            "c1_delta": 1,
            "decrypt_delta": 1,
        }
        assert stats["c1_calls"][-1]["consumer"] == "http"
        assert stats["c1_calls"][-1]["reference"] == login_ref

        # 下一回合复用已保存 session_ciphertext：零登录 POST，首个业务 GET 发送当刻已登记原值与缓存会话值
        _start_turn(agent)
        cached_observations: list[dict[str, object]] = []

        def observe_cached_session(request, _u=user_val, _p=pass_val, _issued=issued_secrets, **kwargs):
            turn_red = turn_credential_redaction(agent)
            cached_observations.append(
                {
                    "method": request.method,
                    "raw_registered": turn_red.contains_credential_echo(_u) and turn_red.contains_credential_echo(_p),
                    "cached_session_registered": all(turn_red.contains_credential_echo(tok) for tok in _issued),
                }
            )
            return orig_fetch_raw(request, **kwargs)

        monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", observe_cached_session)
        cached_res = _invoke_production_web_fetch(
            agent,
            {"url": primary.origin + target_path, "credential_ref": login_ref, "mode": "text"},
        )
        assert cached_res.ok is True
        assert cached_observations == [
            {"method": "GET", "raw_registered": True, "cached_session_registered": True}
        ]


def test_first_send_transport_interruption_leaves_zero_echo_in_output_exception_or_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loopback_sites,
) -> None:
    primary, _secondary = loopback_sites
    agent = _build_real_agent(tmp_path)
    _start_turn(agent)

    bearer_secret = "InterruptBearerSecret-6632$%"
    derived_header = f"Bearer {bearer_secret}"
    target_url = primary.origin + "/bearer-record"
    ref, _ = _issue_submitted_card(
        agent,
        target_url,
        authentication="bearer",
        fields_values={"secret": bearer_secret},
    )

    log_stream: list[str] = []
    probe_logger = logging.getLogger("my_agent.test.r2_http_interrupt")
    handler = logging.StreamHandler()
    formatter = RedactingFormatter("%(levelname)s:%(name)s:%(message)s")

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_stream.append(formatter.format(record))

    list_handler = _ListHandler()
    probe_logger.addHandler(list_handler)
    probe_logger.setLevel(logging.WARNING)
    probe_logger.propagate = False

    def interrupted_fetch(request, **_kwargs):
        auth_val = request.headers.get("Authorization", "")
        probe_logger.warning("mid-flight socket drop hdr=%s raw=%s", auth_val, bearer_secret)
        raise ConnectionResetError(54, f"peer reset while sending {auth_val} raw={bearer_secret}")

    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", interrupted_fetch)
    try:
        # 1) 经完整 ToolExecutor 执行链触发首次 send() 传输中断
        tool_res = _invoke_production_web_fetch(
            agent,
            {"url": target_url, "credential_ref": ref, "mode": "text"},
        )
        assert tool_res.ok is False
        assert tool_res.error_code == "NETWORK_REQUEST_FAILED"
        assert tool_res.effect_outcome == "failed"
        combined_tool_surface = json.dumps(
            {"output": tool_res.output, "metadata": tool_res.metadata, "to_dict": tool_res.to_dict()},
            ensure_ascii=False,
        )
        _assert_zero_variants_in_text(combined_tool_surface, (bearer_secret, derived_header))

        # 2) 直接调用底层 credential_authenticated_request 触发未捕获异常传播时，异常文本同样零原值/头回显
        with pytest.raises(RuntimeError) as exc_info:
            credential_authenticated_request(
                agent,
                ref,
                web_fetch_tools.FetchRawRequest("web_fetch", target_url, "GET", {}, None, 5),
                send=lambda req: (_ for _ in ()).throw(
                    RuntimeError(f"transport broken hdr={req.headers.get('Authorization')} raw={bearer_secret}")
                ),
            )
        _assert_zero_variants_in_text(str(exc_info.value), (bearer_secret, derived_header))
        assert "[private]" in str(exc_info.value)

        # 3) 发送中途实时写出的日志记录已被回合级日志脱敏规则覆盖，零原值/派生头回显
        assert log_stream
        for formatted_line in log_stream:
            _assert_zero_variants_in_text(formatted_line, (bearer_secret, derived_header))
    finally:
        probe_logger.removeHandler(list_handler)
        handler.close()


def test_collected_card_preflight_performs_zero_c1_decrypts_and_zero_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loopback_sites,
) -> None:
    primary, _secondary = loopback_sites
    agent = _build_real_agent(tmp_path)
    _start_turn(agent)
    stats = _spy_c1_and_cipher(monkeypatch)

    target_url = primary.origin + "/bearer-record"
    collected_ref, _ = _issue_submitted_card(
        agent,
        target_url,
        authentication="collected",
        collection_mode="password",
        fields_values={"secret": "CollectedUnusedSecret-3109"},
    )
    # 排除提交加密阶段产生的 cipher 调用，只统计 HTTP 预检阶段
    stats["c1_calls"].clear()
    stats["cipher_decrypts"] = 0
    send_calls = 0

    def fail_if_sent(_request, **_kwargs):
        nonlocal send_calls
        send_calls += 1
        raise AssertionError("collected 预检禁止发起任何网络请求")

    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", fail_if_sent)
    res = _invoke_production_web_fetch(
        agent,
        {"url": target_url, "credential_ref": collected_ref, "mode": "text"},
    )
    assert res.ok is False
    assert res.error_code == "CREDENTIAL_BINDING_INVALID"
    assert res.effect_outcome == "not_started"
    assert len(stats["c1_calls"]) == 0
    assert stats["cipher_decrypts"] == 0
    assert send_calls == 0
    assert collected_ref in res.output
    assert 'authentication="bearer"' in res.output
    assert "browser-lite" in res.output
    assert "run_command" in res.output


def test_presend_revocation_wrong_actor_origin_mismatch_and_signature_fail_closed_zero_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loopback_sites,
) -> None:
    primary, secondary = loopback_sites
    agent = _build_real_agent(tmp_path)
    _start_turn(agent, actor_id="ou_http_owner", conversation_id="oc_dm_http_owner")
    stats = _spy_c1_and_cipher(monkeypatch)

    target_url = primary.origin + "/bearer-record"
    ref, card_path = _issue_submitted_card(
        agent,
        target_url,
        authentication="bearer",
        fields_values={"secret": "FailClosedBearerSecret-5510"},
    )
    stats["cipher_decrypts"] = 0
    send_calls = 0

    def forbid_send(_request, **_kwargs):
        nonlocal send_calls
        send_calls += 1
        raise AssertionError("授权失败必须在发包前阻断")

    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", forbid_send)

    # 1) 跨站点 origin 不匹配 -> 0 解密、0 发包、CREDENTIAL_NOT_AUTHORIZED / not_started
    mismatch_res = _invoke_production_web_fetch(
        agent,
        {"url": secondary.origin + "/bearer-record", "credential_ref": ref, "mode": "text"},
    )
    assert mismatch_res.ok is False
    assert mismatch_res.error_code == "CREDENTIAL_NOT_AUTHORIZED"
    assert mismatch_res.effect_outcome == "not_started"
    assert stats["cipher_decrypts"] == 0 and send_calls == 0

    # 2) 非原发起人 / 跨会话调用 -> 0 解密、0 发包、CREDENTIAL_NOT_AUTHORIZED / not_started
    _start_turn(agent, actor_id="ou_other_caller", conversation_id="oc_dm_other_caller")
    wrong_actor_res = _invoke_production_web_fetch(
        agent,
        {"url": target_url, "credential_ref": ref, "mode": "text"},
    )
    assert wrong_actor_res.ok is False
    assert wrong_actor_res.error_code == "CREDENTIAL_NOT_AUTHORIZED"
    assert wrong_actor_res.effect_outcome == "not_started"
    assert stats["cipher_decrypts"] == 0 and send_calls == 0

    # 3) 恢复本人原会话但记录签名损坏 -> C1 签名门拒绝、0 解密、0 发包
    _start_turn(agent, actor_id="ou_http_owner", conversation_id="oc_dm_http_owner")
    saved_row = read_json_object_report(card_path, context="r2.sig.backup").payload
    with _credential_mutation(agent, card_path) as admission:
        corrupted = {**saved_row, "reference": saved_row["reference"][:-4] + "0000"}
        _save_credential_row(admission, card_path, corrupted)
    bad_sig_res = _invoke_production_web_fetch(
        agent,
        {"url": target_url, "credential_ref": ref, "mode": "text"},
    )
    assert bad_sig_res.ok is False
    assert bad_sig_res.error_code == "CREDENTIAL_NOT_AUTHORIZED"
    assert bad_sig_res.effect_outcome == "not_started"
    assert stats["cipher_decrypts"] == 0 and send_calls == 0

    # 恢复原签名后执行显式撤销 -> 0 解密、0 发包、CREDENTIAL_INPUT_UNAVAILABLE / not_started
    with _credential_mutation(agent, card_path) as admission:
        _save_credential_row(admission, card_path, saved_row)
    assert manage_credential(agent, ref, operation="revoke")["status"] == "revoked"
    stats["cipher_decrypts"] = 0
    revoked_res = _invoke_production_web_fetch(
        agent,
        {"url": target_url, "credential_ref": ref, "mode": "text"},
    )
    assert revoked_res.ok is False
    assert revoked_res.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert revoked_res.effect_outcome == "not_started"
    assert stats["cipher_decrypts"] == 0 and send_calls == 0


def test_presend_and_post_login_turn_redaction_budget_overflow(
    tmp_path: Path,
    loopback_sites,
) -> None:
    primary, _secondary = loopback_sites
    agent = _build_real_agent(tmp_path)

    # 1) 首个 send() 前回合脱敏预算已满（256 项）：零发包拒绝（not_started），且既有 256 项仍受保护
    _start_turn(agent)
    bearer_url = primary.origin + "/bearer-record"
    bearer_secret = "BudgetFullPreSendSecret-801"
    bearer_ref, _ = _issue_submitted_card(
        agent,
        bearer_url,
        authentication="bearer",
        fields_values={"secret": bearer_secret},
    )
    turn_red = turn_credential_redaction(agent)
    existing_256 = [f"ExistingProtectedSecret-{idx:03d}" for idx in range(256)]
    turn_red.register_values(existing_256)
    assert turn_red.withheld is False

    events_before = len(primary.events)
    pre_res = _invoke_production_web_fetch(
        agent,
        {"url": bearer_url, "credential_ref": bearer_ref, "mode": "text"},
    )
    assert pre_res.ok is False
    assert pre_res.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert pre_res.effect_outcome == "not_started"
    assert len(primary.events) == events_before
    _assert_zero_variants_in_text(pre_res.output, (bearer_secret,))

    # 2) 登录 POST 前恰在预算内（254 + 2 = 256），登录响应新签发的会话 token/Cookie 超出 256 上限：
    #    不发送后续业务 GET，扣留不可验证输出，同时忠实保留已发生登录网络请求的副作用事实（effect_outcome == 'failed'）
    _start_turn(agent)
    protected_url = primary.origin + "/overflow-protected"
    login_user = "OverflowLoginUser-901"
    login_pass = "OverflowLoginPass-902"
    login_ref, _ = _issue_submitted_card(
        agent,
        protected_url,
        authentication="login",
        fields_spec=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
        fields_values={"username": login_user, "secret": login_pass},
        auth_config={
            "url": primary.origin + "/login-overflow",
            "body_fields": {"u": "username", "p": "secret"},
            "token_path": ["access_token"],
            "cookies": True,
        },
    )
    login_turn_red = turn_credential_redaction(agent)
    login_turn_red.register_values([f"PreRegisteredSlot-{idx:03d}" for idx in range(254)])
    assert login_turn_red.withheld is False

    events_before_login = len(primary.events)
    post_res = _invoke_production_web_fetch(
        agent,
        {"url": protected_url, "credential_ref": login_ref, "mode": "text"},
    )
    observed_events = primary.events[events_before_login:]
    # 仅发出了 1 次 POST /login-overflow，后续 GET /overflow-protected 未发出
    assert [(ev["method"], ev["path"]) for ev in observed_events] == [("POST", "/login-overflow")]
    assert post_res.ok is False
    assert post_res.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert post_res.effect_outcome == "failed"
    assert post_res.effect_outcome != "not_started"
    handler_details = post_res.metadata["handler_details"]
    assert handler_details["outbound_requests"] == 1
    assert handler_details["credential_error"]["login_request_sent"] is True
    assert handler_details["credential_error"]["business_request_sent"] is False
    assert handler_details["credential_error"]["output_withheld"] is True
    assert login_turn_red.withheld is True
    assert primary.record_marker not in post_res.output
    _assert_zero_variants_in_text(
        json.dumps({"output": post_res.output, "metadata": post_res.metadata}, ensure_ascii=False),
        (
            login_user,
            login_pass,
            "IssuedOverflowSessionToken-999",
            "IssuedOverflowCookie-998",
        ),
    )

    # 3) 模拟 pp-r2-1 合入后的 try_register_values 原子容量拒绝接口（返回 None 且不污染当前回合 withheld）
    _start_turn(agent)
    atomic_turn = turn_credential_redaction(agent)
    atomic_turn.register_values(["AtomicExistingSecret-001"])
    object.__setattr__(atomic_turn, "try_register_values", lambda _vals: None)
    events_before_atomic = len(primary.events)
    atomic_ref, _ = _issue_submitted_card(
        agent,
        bearer_url,
        authentication="bearer",
        fields_values={"secret": "AtomicRefusedSecret-002"},
    )
    atomic_res = _invoke_production_web_fetch(
        agent,
        {"url": bearer_url, "credential_ref": atomic_ref, "mode": "text"},
    )
    assert atomic_res.ok is False
    assert atomic_res.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert atomic_res.effect_outcome == "not_started"
    assert len(primary.events) == events_before_atomic
    assert atomic_turn.withheld is False
    assert "[REDACTED]" in atomic_turn.public_text("check AtomicExistingSecret-001")


def test_direct_execute_401_refresh_overflow_and_non_card_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loopback_sites,
) -> None:
    primary, _secondary = loopback_sites
    agent = _build_real_agent(tmp_path)
    _start_turn(agent)
    web_tool = agent.tools.tools["web_fetch"]
    web_tool.allow_private_resolution = True
    web_tool.allowed_private_hosts = ("127.0.0.1",)

    # 1) 无卡普通 HTTP 请求保持原行为，零凭据访问
    plain_res = web_tool.execute({"url": primary.origin + "/overflow-protected", "mode": "text"})
    assert plain_res.ok is True and primary.record_marker in plain_res.output

    # 2) 直接调用 web_tool.execute（无 ToolInvocationContext / credential_use=None）同样走 C1 且发包前登记
    bearer_url = primary.origin + "/bearer-record"
    direct_secret = "DirectExecuteSecret-4412"
    direct_ref, _ = _issue_submitted_card(
        agent,
        bearer_url,
        authentication="bearer",
        fields_values={"secret": direct_secret},
    )
    direct_seen_before_send: list[bool] = []
    orig_fetch_raw = web_fetch_tools.fetch_raw_response

    def check_direct(request, **kwargs):
        tr = turn_credential_redaction(agent)
        direct_seen_before_send.append(
            tr.contains_credential_echo(direct_secret)
            and tr.contains_credential_echo(f"Bearer {direct_secret}")
        )
        return orig_fetch_raw(request, **kwargs)

    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", check_direct)
    direct_res = web_tool.execute({"url": bearer_url, "credential_ref": direct_ref, "mode": "text"})
    assert direct_res.ok is True
    assert direct_seen_before_send == [True]
    _assert_zero_variants_in_text(direct_res.output, (direct_secret,))

    # 3) GET 401 触发有限会话刷新时，若刷新登录签发的新令牌超出预算，不发送第二次业务 GET 且如实记录 business_request_sent=True
    _start_turn(agent)
    refresh_ref, _ = _issue_submitted_card(
        agent,
        primary.origin + "/json-protected",
        authentication="login",
        fields_spec=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
        fields_values={"username": "RefreshUser-11", "secret": "RefreshPass-22"},
        auth_config={
            "url": primary.origin + "/login-json",
            "body_fields": {"u": "username", "p": "secret"},
            "token_path": ["access_token"],
        },
    )
    # 留出首次登录（2 个原字段 + 2 个首次会话值 = 4 项），使 401 刷新签发不同新令牌时恰越过 256 上限
    refresh_turn = turn_credential_redaction(agent)
    refresh_turn.register_values([f"RefreshSlot-{idx:03d}" for idx in range(252)])
    hop_calls: list[tuple[str, str]] = []

    def simulated_refresh_transport(request, **_kwargs):
        hop_calls.append((request.method, request.url))
        if request.url.endswith("/login-json"):
            tok = "FirstIssuedTok-101" if len(hop_calls) == 1 else "SecondIssuedTokOverflow-202"
            return web_fetch_tools.RawResponseParts(
                200,
                {"Content-Type": "application/json"},
                json.dumps({"access_token": tok}).encode("utf-8"),
                request.url,
            )
        # 首次业务 GET 返回 401 触发刷新；刷新超限后不应再进入第二次业务 GET
        return web_fetch_tools.RawResponseParts(401, {}, b"stale token", request.url)

    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", simulated_refresh_transport)
    refresh_res = web_tool.execute(
        {"url": primary.origin + "/json-protected", "credential_ref": refresh_ref, "mode": "text"}
    )
    assert refresh_res.ok is False
    assert refresh_res.error_code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert refresh_res.effect_outcome == "failed"
    assert len(hop_calls) == 3  # POST login -> GET business(401) -> POST refresh login（停止，未发第 4 跳重试 GET）
    assert refresh_res.result_envelope["outbound_requests"] == 3
    assert refresh_res.result_envelope["credential_error"]["business_request_sent"] is True
    assert refresh_res.result_envelope["credential_error"]["output_withheld"] is True
    _assert_zero_variants_in_text(
        json.dumps({"output": refresh_res.output, "envelope": refresh_res.result_envelope}, ensure_ascii=False),
        ("RefreshUser-11", "RefreshPass-22", "FirstIssuedTok-101", "SecondIssuedTokOverflow-202"),
    )


def test_direct_execute_restores_log_contextvars_for_same_worker_thread_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loopback_sites,
) -> None:
    primary, _secondary = loopback_sites
    agent = _build_real_agent(tmp_path)
    web_tool = agent.tools.tools["web_fetch"]
    web_tool.allow_private_resolution = True
    web_tool.allowed_private_hosts = ("127.0.0.1",)
    # 卡片账号值与后续工作流 owner 同名：线程上下文一旦滞留该值，owner 就会被打码成 [REDACTED]。
    owner_name = "user"
    login_secret = "ContextRestoreLoginPass-6201!"
    target_url = primary.origin + "/json-protected"
    orig_fetch_raw = web_fetch_tools.fetch_raw_response
    transport: dict[str, object] = {"mode": "pass", "in_flight": []}

    def switchable_transport(request, **kwargs):
        bound = log_redaction._CONFIGURED_CREDENTIAL_VALUES.get()
        transport["in_flight"].append((
            transport["mode"],
            log_redaction._CONFIGURED_REDACTION_DEPTH.get(),
            owner_name in bound and login_secret in bound,
        ))
        if transport["mode"] == "reset":
            raise ConnectionResetError(54, "synthetic loopback reset")
        if transport["mode"] == "crash":
            raise RuntimeError("synthetic transport crash")
        return orig_fetch_raw(request, **kwargs)

    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", switchable_transport)

    def contextvar_state() -> tuple[object, ...]:
        return (
            log_redaction._CONFIGURED_SECRET_VALUES.get(),
            log_redaction._CONFIGURED_CREDENTIAL_VALUES.get(),
            log_redaction._CONFIGURED_REDACTION_DEPTH.get(),
        )

    def credential_phase() -> int:
        before = contextvar_state()
        _start_turn(agent)
        try:
            ref, _ = _issue_submitted_card(
                agent,
                target_url,
                authentication="login",
                fields_spec=[{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}],
                fields_values={"username": owner_name, "secret": login_secret},
                auth_config={
                    "url": primary.origin + "/login-json",
                    "body_fields": {"u": "username", "p": "secret"},
                    "token_path": ["access_token"],
                },
            )
            args = {"url": target_url, "credential_ref": ref, "mode": "text"}
            # 1) 直接 execute 正常 GET：返回后线程上下文逐值回到调用前。
            ok_res = web_tool.execute(args)
            assert ok_res.ok is True and primary.record_marker in ok_res.output
            assert contextvar_state() == before
            # 2) 传输异常被转成失败回执：同样零滞留。
            transport["mode"] = "reset"
            reset_res = web_tool.execute(args)
            assert reset_res.ok is False and reset_res.error_code == "NETWORK_REQUEST_FAILED"
            assert contextvar_state() == before
            # 3) 嵌套在外层脱敏帧且异常穿出 execute：只回到外层帧原值，外层退出后回到调用前。
            transport["mode"] = "crash"
            with configured_secret_redaction(("OuterFrameOnlySecret-6202",)):
                outer_state = contextvar_state()
                with pytest.raises(RuntimeError, match="synthetic transport crash"):
                    web_tool.execute(args)
                assert contextvar_state() == outer_state
            assert contextvar_state() == before
        finally:
            transport["mode"] = "pass"
            turn_credential_redaction(agent, agent._current_run_params).close()
            del agent._current_run_params
        assert contextvar_state() == before
        # 发包当刻拥有帧已绑定卡值（直接调用深度 1，嵌套深度 2），在途日志仍被遮蔽。
        in_flight = transport["in_flight"]
        assert {mode for mode, _depth, _bound in in_flight} == {"pass", "reset", "crash"}
        assert all(depth == (2 if mode == "crash" else 1) and bound for mode, depth, bound in in_flight)
        return threading.get_ident()

    def workflow_phase() -> int:
        wf_home = tmp_path / "workflow_owner_home"
        script_dir = wf_home / "scripts" / "collect"
        script_dir.mkdir(parents=True)
        (script_dir / "script.json").write_text(json.dumps({
            "id": "collect", "kind": "script", "name": "collect", "owner_id": owner_name,
            "io_contract": {"outputs": ["value"]}, "entrypoint": "main.py",
            "interpreter": "python3", "timeout_seconds": 10,
        }))
        (script_dir / "main.py").write_text(
            'import json,sys; print(json.dumps({"value": json.load(sys.stdin)["number"]+1}))'
        )
        definition = {
            "descriptor": {"id": "owner-flow", "kind": "workflow", "name": "owner-flow", "owner_id": owner_name},
            "steps": [
                {"step_id": "collect", "name": "collect", "mode": "deterministic_script", "script_id": "collect"},
                {"step_id": "second", "name": "second", "mode": "deterministic_script", "script_id": "collect",
                 "inputs_map": {"number": {"step_id": "collect", "key": "value"}}},
            ],
        }
        run = WorkflowEngine(wf_home).run_workflow(definition, {"number": 1}, owner_id=owner_name)
        assert run.status == "succeeded" and run.owner_id == owner_name
        assert [s.status for s in run.steps] == ["succeeded", "succeeded"]
        assert run.final_output == {"value": 3}
        return threading.get_ident()

    # 单工作线程池复用同一线程：凭据阶段若滞留 owner 值，随后的工作流会报 WORKFLOW_OWNER_MISMATCH。
    with ThreadPoolExecutor(max_workers=1) as pool:
        credential_thread = pool.submit(credential_phase).result(timeout=60)
        workflow_thread = pool.submit(workflow_phase).result(timeout=60)
    assert credential_thread == workflow_thread
