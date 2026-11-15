"""NW2-W4 回归测试: 飞书 webhook 有界 HTTP 服务/体长上限与入站永久资源错误分流。"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu as feishu_module
from agent_py_agent.agent.adapter.feishu import (
    FeishuAdapter,
    _FeishuCallbackHandler,
    _fetch_feishu_resource,
)
from agent_py_agent.agent.adapter.feishu_quoted_media import (
    FeishuQuotedMediaError,
    read_quoted_media,
)
from agent_py_agent.agent.adapter.inbound_images import InboundMediaInputError
from agent_py_agent.agent.adapter.ingress import build_gateway_ingress_record
from agent_py_agent.agent.adapter.manager import ChannelManager, _download_complete_inbound_media
from agent_py_agent.agent.adapter.protocol import feishu_to_incoming
from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.normalize import normalize_agent_config
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin, WritePaths, set_parameter
from agent_py_agent.agent.settings.parameter_metadata import ranges
from agent_py_agent.agent.settings.parameter_registry import (
    DEPLOYMENT_ONLY_KEYS,
    parameter_registry,
)
from agent_py_agent.agent.settings.user_config_capability import TUNABLE_KEYS, set_tunable_value
from agent_py_agent.tests.support.fd_lifetime import assert_fd_unchanged, fd_baseline
from agent_py_agent.tests.test_feishu_native_images import current_event, image_bytes, native_case


def _webhook_adapter(
    *,
    live_config: AgentConfig | None = None,
    config_overrides: dict[str, object] | None = None,
) -> FeishuAdapter:
    cfg: dict[str, object] = {
        "feishu_connection_mode": "webhook",
        "feishu_session_lock_enabled": False,
        "feishu_verification_token": "verify-tok",
    }
    if config_overrides:
        cfg.update(config_overrides)
    return FeishuAdapter(config=cfg, callback_port=0, live_config=live_config)


def _post_raw_http(port: int, raw_bytes: bytes, *, timeout: float = 3.0) -> tuple[int, dict[str, object]]:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw_bytes)
        chunks: list[bytes] = []
        while True:
            part = sock.recv(4096)
            if not part:
                break
            chunks.append(part)
    text = b"".join(chunks).decode("utf-8")
    head, _, body = text.partition("\r\n\r\n")
    status_line = head.splitlines()[0]
    status_code = int(status_line.split()[1])
    return status_code, json.loads(body)


def _http_error(code: int, url: str = "https://open.feishu.cn/open-apis/im/v1/messages/m/resources/k") -> tuple[urllib.error.HTTPError, SimpleNamespace]:
    state = SimpleNamespace(closed=False)

    class _TrackedFp(BytesIO):
        def close(self) -> None:
            state.closed = True
            super().close()

    fp = _TrackedFp(b'{"code":999}')
    err = urllib.error.HTTPError(url, code, f"HTTP {code}", {}, fp)
    return err, state


# ---------------------------------------------------------------------------
# 1. NW-05: GatewayBoundedHTTPServer lifecycle, bounds, timeouts, and FD safety
# ---------------------------------------------------------------------------


def test_webhook_uses_bounded_server_and_stops_with_zero_fd_leak():
    live_cfg = AgentConfig(
        gateway_http_max_request_workers=2,
        gateway_http_max_outstanding_requests=4,
        gateway_http_socket_timeout_seconds=1.5,
        feishu_webhook_max_body_bytes=2048,
    )
    adapter = _webhook_adapter(live_config=live_cfg)
    baseline = fd_baseline()
    adapter.start()
    try:
        server = adapter._server
        assert isinstance(server, GatewayBoundedHTTPServer)
        assert server.max_request_workers == 2
        assert server.max_outstanding_requests == 4
        assert server.request_socket_timeout == pytest.approx(1.5)
        port = server.server_port

        body = json.dumps({"type": "url_verification", "token": "verify-tok", "challenge": "ok-1"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/feishu/callback",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode("utf-8")) == {"challenge": "ok-1"}
    finally:
        result = adapter.stop(timeout=5.0)
        assert result.status == "exited"
    assert_fd_unchanged(baseline)


@pytest.mark.parametrize(
    ("raw_http", "expected_status", "expected_error"),
    [
        (
            b"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
            400,
            "invalid content length",
        ),
        (
            b"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\n\r\n",
            400,
            "invalid content length",
        ),
        (
            b"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\nContent-Length: -4\r\n\r\n",
            400,
            "invalid content length",
        ),
        (
            b"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\nContent-Length: abc\r\n\r\n",
            400,
            "invalid content length",
        ),
        (
            b"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nContent-Length: 8\r\n\r\n{}",
            400,
            "invalid content length",
        ),
        (
            b"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\nContent-Length: 9999999\r\n\r\n",
            413,
            "request body too large",
        ),
    ],
)
def test_webhook_rejects_invalid_or_oversized_length_before_reading_body(raw_http, expected_status, expected_error):
    live_cfg = AgentConfig(feishu_webhook_max_body_bytes=64, gateway_http_socket_timeout_seconds=1.5)
    adapter = _webhook_adapter(live_config=live_cfg)
    baseline = fd_baseline()
    adapter.start()
    try:
        port = adapter._server.server_port
        status, parsed = _post_raw_http(port, raw_http)
        assert status == expected_status
        assert parsed == {"error": expected_error}
    finally:
        assert adapter.stop(timeout=5.0).status == "exited"
    assert_fd_unchanged(baseline)


def test_webhook_handler_rejects_without_calling_rfile_read_or_verify(monkeypatch):
    live_cfg = AgentConfig(feishu_webhook_max_body_bytes=32)
    adapter = _webhook_adapter(live_config=live_cfg)
    read_calls: list[int] = []
    verify_calls: list[bytes] = []

    class _SpyRfile:
        def __init__(self, content: bytes) -> None:
            self._content = content

        def read(self, size: int = -1) -> bytes:
            read_calls.append(size)
            return self._content if size < 0 else self._content[:size]

    monkeypatch.setattr(
        "agent_py_agent.agent.adapter.feishu_crypto.verify_and_decode_webhook",
        lambda req: verify_calls.append(req.raw_body) or None,
    )

    for bad_headers in (
        {"Transfer-Encoding": "chunked"},
        {},
        {"Content-Length": "-1"},
        {"Content-Length": "1_0"},
        {"Content-Length": "33"},
    ):
        responses: list[tuple[int, dict[str, object]]] = []
        handler = object.__new__(_FeishuCallbackHandler)
        handler.path = "/feishu/callback"
        handler.server = SimpleNamespace(adapter=adapter)
        handler.headers = bad_headers
        handler.rfile = _SpyRfile(b"{}")
        handler._write_json = lambda code, body: responses.append((code, body))
        handler.do_POST()
        assert len(responses) == 1
        expected_code = 413 if bad_headers.get("Content-Length") == "33" else 400
        assert responses[0][0] == expected_code
        assert read_calls == []
        assert verify_calls == []

    # Truncated body (fewer bytes than Content-Length) fails before signature decoding
    responses = []
    handler = object.__new__(_FeishuCallbackHandler)
    handler.path = "/feishu/callback"
    handler.server = SimpleNamespace(adapter=adapter)
    handler.headers = {"Content-Length": "20"}
    handler.rfile = _SpyRfile(b"short")
    handler._write_json = lambda code, body: responses.append((code, body))
    handler.do_POST()
    assert read_calls == [20]
    assert responses == [(400, {"error": "incomplete request body"})]
    assert verify_calls == []


def test_webhook_live_truncated_body_returns_400_and_stalled_client_times_out():
    live_cfg = AgentConfig(
        gateway_http_socket_timeout_seconds=0.2,
        feishu_webhook_max_body_bytes=1024,
    )
    adapter = _webhook_adapter(live_config=live_cfg)
    baseline = fd_baseline()
    adapter.start()
    try:
        port = adapter._server.server_port

        # 1) Truncated body where client signals EOF early -> 400 incomplete request body
        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
            sock.sendall(b"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\nContent-Length: 20\r\n\r\npartial")
            sock.shutdown(socket.SHUT_WR)
            raw_resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw_resp += chunk
        assert b"400" in raw_resp.splitlines()[0]
        assert json.loads(raw_resp.partition(b"\r\n\r\n")[2].decode("utf-8")) == {"error": "incomplete request body"}

        # 2) Stalled slow socket without EOF -> closed promptly by socket timeout
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as slow:
            slow.sendall(b"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\nContent-Length: 50\r\n\r\nabc")
            eof = slow.recv(1024)
        elapsed = time.monotonic() - started
        assert eof == b""
        assert elapsed < 1.5

        # 3) Server remains healthy afterwards
        body = json.dumps({"type": "url_verification", "token": "verify-tok", "challenge": "alive"}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        try:
            conn.request("POST", "/feishu/callback", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            assert resp.status == 200
            assert json.loads(resp.read().decode("utf-8")) == {"challenge": "alive"}
        finally:
            conn.close()
    finally:
        assert adapter.stop(timeout=5.0).status == "exited"
    assert_fd_unchanged(baseline)


def test_webhook_concurrency_cap_returns_bounded_503_when_saturated(monkeypatch):
    live_cfg = AgentConfig(
        gateway_http_max_request_workers=1,
        gateway_http_max_outstanding_requests=1,
        gateway_http_socket_timeout_seconds=1.5,
        gateway_http_queue_timeout_seconds=0.2,
    )
    adapter = _webhook_adapter(live_config=live_cfg)
    entered = threading.Event()
    release = threading.Event()
    adapter.on_message(lambda _msg: True)

    def _slow_event(_event):
        entered.set()
        release.wait(5.0)
        return SimpleNamespace(status="accepted", reason_code="OK")

    monkeypatch.setattr(adapter, "_handle_feishu_event", _slow_event)
    baseline = fd_baseline()
    adapter.start()
    try:
        port = adapter._server.server_port
        event_bytes = json.dumps({"token": "verify-tok", "event": {"message": {"message_id": "om_slow"}}}).encode("utf-8")

        def _send_blocking() -> int:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/feishu/callback",
                data=event_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                return resp.status

        worker_result: list[int] = []
        holder = threading.Thread(target=lambda: worker_result.append(_send_blocking()), daemon=True)
        holder.start()
        assert entered.wait(2.0)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/feishu/callback",
                    data=b'{"type":"url_verification","token":"verify-tok","challenge":"c"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=2.0,
            )
        assert exc_info.value.code == 503
        exc_info.value.close()

        release.set()
        holder.join(timeout=3.0)
        assert worker_result == [200]
    finally:
        assert adapter.stop(timeout=5.0).status == "exited"
    assert_fd_unchanged(baseline)


# ---------------------------------------------------------------------------
# 2. NW-05: Config defaults, normalization, registry metadata & YAML wiring
# ---------------------------------------------------------------------------


def test_feishu_webhook_max_body_bytes_config_registry_and_bounds(tmp_path: Path):
    cfg = AgentConfig()
    assert cfg.feishu_webhook_max_body_bytes == 4194304

    with pytest.raises(ValueError, match="feishu_webhook_max_body_bytes"):
        AgentConfig(feishu_webhook_max_body_bytes=0)
    with pytest.raises(ValueError, match="feishu_webhook_max_body_bytes"):
        AgentConfig(feishu_webhook_max_body_bytes=-10)

    valid_out, valid_warn = normalize_agent_config({"feishu_webhook_max_body_bytes": 1024})
    assert valid_out["feishu_webhook_max_body_bytes"] == 1024 and valid_warn == []

    for bad_value in (0, -1, True, "bad-size"):
        bad_out, bad_warn = normalize_agent_config({"feishu_webhook_max_body_bytes": bad_value})
        assert bad_out["feishu_webhook_max_body_bytes"] == 4194304
        assert any("feishu_webhook_max_body_bytes" in item for item in bad_warn)

    spec = parameter_registry()["feishu_webhook_max_body_bytes"]
    assert spec.default == 4194304
    assert spec.value_type == "int"
    assert spec.tier == "deployment"
    assert spec.effect == "restart_gateway"
    assert spec.unit == "字节"
    assert spec.range == "≥1"
    assert ranges()["feishu_webhook_max_body_bytes"] == "≥1"
    assert spec.writable is False
    assert spec.user_editable is False
    assert spec.reader == "agent/adapter/feishu"
    assert spec.owner_module == "adapter"
    assert "feishu_webhook_max_body_bytes" in DEPLOYMENT_ONLY_KEYS
    assert "feishu_webhook_max_body_bytes" in TUNABLE_KEYS
    assert TUNABLE_KEYS["feishu_webhook_max_body_bytes"].validate("1024") == (True, "", 1024)
    assert TUNABLE_KEYS["feishu_webhook_max_body_bytes"].validate("0")[0] is False

    # Host deployment boundary blocks model/chat parameter writes
    user_yaml = tmp_path / "user.yaml"
    user_yaml.write_text("feishu_webhook_max_body_bytes: 2048\n", encoding="utf-8")
    report = set_tunable_value("feishu_webhook_max_body_bytes", "8192", user_path=user_yaml)
    assert report["ok"] is False
    assert "部署配置" in str(report["error"])

    change_res = set_parameter(
        "feishu_webhook_max_body_bytes",
        "8192",
        paths=WritePaths(user_path=user_yaml),
        origin=ChangeOrigin("user"),
    )
    assert change_res["ok"] is False
    assert change_res["code"] == "PARAMETER_SOURCE_READ_ONLY"

    # Dict-configured test adapter fallback without live_config also enforces feishu_webhook_max_body_bytes
    dict_adapter = _webhook_adapter(live_config=None, config_overrides={"feishu_webhook_max_body_bytes": 12})
    assert feishu_module._feishu_webhook_max_body_bytes(dict_adapter) == 12

    # Loaded YAML drives live webhook cap end-to-end
    loaded = load_config(user_yaml)
    assert loaded.feishu_webhook_max_body_bytes == 2048
    small_yaml = tmp_path / "small.yaml"
    small_yaml.write_text("feishu_webhook_max_body_bytes: 16\n", encoding="utf-8")
    small_cfg = load_config(small_yaml)
    adapter = _webhook_adapter(live_config=small_cfg)
    baseline = fd_baseline()
    adapter.start()
    try:
        port = adapter._server.server_port
        body = b'{"type":"url_verification","token":"verify-tok","challenge":"too-long"}'
        status, parsed = _post_raw_http(
            port,
            (
                f"POST /feishu/callback HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n"
            ).encode("ascii")
            + body,
        )
        assert status == 413
        assert parsed == {"error": "request body too large"}
    finally:
        assert adapter.stop(timeout=5.0).status == "exited"
    assert_fd_unchanged(baseline)


# ---------------------------------------------------------------------------
# 3. B-02: Permanent HTTP 400/404/410 rejection on primary inbound media
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 404, 410])
@pytest.mark.parametrize("media_kind", ["image", "file"])
def test_primary_inbound_media_permanent_http_status_raises_terminal_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    media_kind: str,
):
    manager, adapter, host, _, _ = native_case(tmp_path, monkeypatch)
    monkeypatch.undo()
    adapter._get_tenant_access_token = lambda: "tenant-tok"
    err, fp_state = _http_error(status_code)

    def _fail_open(_req, timeout=30):
        raise err

    monkeypatch.setattr(feishu_module, "open_authenticated_request", _fail_open)
    if media_kind == "image":
        event = current_event()
    else:
        event = current_event(items=[{"tag": "file", "file_key": "fk_recalled", "file_name": "doc.pdf"}])
    msg = feishu_to_incoming(event, bot_open_id="ou_bot")
    assert msg is not None

    with pytest.raises(InboundMediaInputError, match="已撤回、过期或无法读取"):
        manager._maybe_download_media(msg, retry_on_error=True)
    assert fp_state.closed is True

    # Full ingress delivery worker routes primary 400/404/410 to terminal INPUT_MEDIA_INVALID (not infinite retry)
    err2, fp_state2 = _http_error(status_code)
    monkeypatch.setattr(feishu_module, "open_authenticated_request", lambda _req, timeout=30: (_ for _ in ()).throw(err2))
    record = build_gateway_ingress_record(
        channel=msg.channel,
        user_id=msg.user_id,
        conversation_id=msg.conversation_id,
        provider_message_id=msg.message_id,
        content=msg.content,
        metadata=msg.metadata,
        timestamp=msg.timestamp,
    )
    manager._delivery_worker.enqueue(record)
    replies: list[tuple[object, ...]] = []
    monkeypatch.setattr(manager, "_send_gateway_reply", lambda *args: replies.append(args) or True)
    assert manager._delivery_worker.run_once() == 1
    assert fp_state2.closed is True
    records = manager._delivery_worker.store.records()
    assert len(records) == 1
    assert records[0].state == "completed"
    assert records[0].submission["error_code"] == "INPUT_MEDIA_INVALID"
    assert replies


@pytest.mark.parametrize("status_code", [401, 403])
def test_primary_inbound_media_401_and_403_stay_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
):
    err, fp_state = _http_error(status_code)
    monkeypatch.setattr(feishu_module, "open_authenticated_request", lambda _req, timeout=30: (_ for _ in ()).throw(err))

    with caplog.at_level("WARNING"):
        assert _fetch_feishu_resource("om_1", "img_1", "image", "SECRET_TOK", strict_resource=True) is None
    assert fp_state.closed is True
    assert "SECRET_TOK" not in caplog.text
    assert f"http_status={status_code}" in caplog.text

    manager, adapter, _, _, _ = native_case(tmp_path, monkeypatch)
    monkeypatch.undo()
    adapter._get_tenant_access_token = lambda: "tenant-tok"
    err2, fp_state2 = _http_error(status_code)
    monkeypatch.setattr(feishu_module, "open_authenticated_request", lambda _req, timeout=30: (_ for _ in ()).throw(err2))
    msg = feishu_to_incoming(current_event(), bot_open_id="ou_bot")
    assert msg is not None

    with pytest.raises(OSError, match="INBOUND_MEDIA_DOWNLOAD_INCOMPLETE"):
        manager._maybe_download_media(msg, retry_on_error=True)
    assert fp_state2.closed is True


@pytest.mark.parametrize("status_code", [400, 404, 410])
def test_quoted_media_400_404_410_stays_non_fatal_when_primary_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
):
    from agent_py_agent.tests.test_feishu_quoted_media import _event as quoted_event
    from agent_py_agent.tests.test_feishu_quoted_media import _parent as quoted_parent

    home_root = tmp_path / "home"
    home_root.mkdir()
    host = SimpleNamespace(home_paths=SimpleNamespace(root=home_root), config=AgentConfig())
    manager = ChannelManager(delivery_state_dir=tmp_path / "delivery")
    manager._inbound_media_agent = host
    adapter = FeishuAdapter({"feishu_session_lock_enabled": False, "feishu_quoted_media_enabled": True})
    adapter._resolve_bot_open_id = lambda: "ou_bot"
    adapter._get_tenant_access_token = lambda: "tenant-tok"
    manager._adapters["feishu"] = adapter

    parent_row = quoted_parent(chat="oc_team", key="fk_gone", name="old.bin")
    monkeypatch.setattr(
        "agent_py_agent.agent.adapter.feishu._feishu_msg_api",
        lambda _m, _p, _b, _t: {"code": 0, "data": {"items": [parent_row]}},
    )
    err, fp_state = _http_error(status_code)

    class _FakeResp(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def _open(req, timeout=30):
        if "om_file" in req.full_url:
            raise err
        return _FakeResp(image_bytes())

    monkeypatch.setattr(feishu_module, "open_authenticated_request", _open)
    ev = quoted_event()
    ev["event"]["message"].update(message_type="image", content=json.dumps({"image_key": "ik_live"}))
    msg = feishu_to_incoming(ev, bot_open_id="ou_bot")
    assert msg is not None

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    quoted = read_quoted_media(adapter, msg)
    assert quoted is not None
    with pytest.raises(FeishuQuotedMediaError, match="FEISHU_QUOTED_MEDIA_RESOURCE_UNAVAILABLE"):
        _download_complete_inbound_media(
            adapter,
            msg,
            msg.metadata["media"],
            dest_dir,
            quoted,
        )
    assert fp_state.closed is True

    # Verify ingress worker degrades quoted media failure to prompt notice instead of terminal error
    err2, fp_state2 = _http_error(status_code)

    def _open_prepare(req, timeout=30):
        if "om_file" in req.full_url:
            raise err2
        return _FakeResp(image_bytes())

    monkeypatch.setattr(feishu_module, "open_authenticated_request", _open_prepare)
    record = build_gateway_ingress_record(
        channel=msg.channel,
        user_id=msg.user_id,
        conversation_id=msg.conversation_id,
        provider_message_id=msg.message_id,
        content=msg.content,
        metadata=msg.metadata,
        timestamp=msg.timestamp,
    )
    prepared = manager._ingress_callbacks.prepare_payload(record)
    assert fp_state2.closed is True
    assert "FEISHU_QUOTED_MEDIA_RESOURCE_UNAVAILABLE" in prepared["prompt"]
