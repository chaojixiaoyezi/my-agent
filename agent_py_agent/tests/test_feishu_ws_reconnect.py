"""飞书长连接「进程内断线重连」单测(#2 借鉴 长期助手 每通道看门狗)。不打真网络/不真 sleep。"""

from __future__ import annotations

import logging
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from websockets.protocol import State

from agent_py_agent.agent.adapter import feishu_ws


class _FakeClient:
    """假 lark 客户端:start() 每次自增计数;到第 stop_at 次模拟被 close()(_stopped=True)。"""

    def __init__(self, stop_at: int, raises: bool = True):
        self._stopped = False
        self.starts = 0
        self._stop_at = stop_at
        self._raises = raises

    def start(self):
        self.starts += 1
        if self.starts >= self._stop_at:
            self._stopped = True  # 模拟外部 close() 主动停
        if self._raises:
            raise RuntimeError("ws died")


def _run(client):
    dead = []
    with patch.object(feishu_ws.time, "sleep"), patch.object(feishu_ws.random, "uniform", return_value=0.0):
        feishu_ws._run_ws_blocking(client, lambda: dead.append(1))
    return dead


def test_reconnect_retries_until_stopped():
    # start 连续抛异常,到第3次时被标记主动停 → 共重连到3次、不永久放弃
    c = _FakeClient(stop_at=3)
    dead = _run(c)
    assert c.starts == 3        # 死2次都重连了(没有死一次就退出)
    assert dead == [1]          # 主动停后 on_dead 恰好一次


def test_no_start_when_already_stopped():
    c = _FakeClient(stop_at=1)
    c._stopped = True
    dead = _run(c)
    assert c.starts == 0        # 已停:压根不连
    assert dead == [1]


def test_clean_return_also_reconnects():
    # start() 正常返回(不抛)也当断线重连,直到主动停
    c = _FakeClient(stop_at=2, raises=False)
    dead = _run(c)
    assert c.starts == 2
    assert dead == [1]


def test_backoff_grows_between_retries():
    c = _FakeClient(stop_at=4)
    waits = []
    with patch.object(feishu_ws.time, "sleep", side_effect=lambda w: waits.append(w)), \
         patch.object(feishu_ws.random, "uniform", return_value=0.0):
        feishu_ws._run_ws_blocking(c, lambda: None)
    # 3 次重连等待应指数增长(1,2,4...)且各不减
    assert len(waits) == 3
    assert waits == sorted(waits)
    assert waits[0] < waits[-1]


def test_lark_processor_not_found_filter_drops_only_noise():
    import logging

    f = feishu_ws._LarkProcessorNotFoundFilter()

    def _rec(msg):
        return logging.LogRecord("Lark", logging.ERROR, "", 0, msg, None, None)

    assert f.filter(_rec("handle message failed ... err: processor not found, type: ...")) is False
    assert f.filter(_rec("connect failed: app_id or app_secret is null")) is True  # 真错误保留


def test_suppress_lark_noise_is_idempotent():
    import logging

    lark_logger = logging.getLogger("Lark")
    lark_logger.filters = [x for x in lark_logger.filters if not isinstance(x, feishu_ws._LarkProcessorNotFoundFilter)]
    feishu_ws._lark_noise_filtered = False
    feishu_ws._suppress_lark_processor_not_found()
    feishu_ws._suppress_lark_processor_not_found()  # 第二次应空操作
    cnt = sum(1 for x in lark_logger.filters if isinstance(x, feishu_ws._LarkProcessorNotFoundFilter))
    assert cnt == 1


@pytest.mark.parametrize("proxy", ["", "http://proxy.invalid:8080", "https://proxy.invalid:8443", "socks5://proxy.invalid:1080", "socks5h://proxy.invalid:1080"])
def test_sdk_patch_preserves_proxy_selection_without_connecting(monkeypatch, proxy):
    import websockets

    package, ws, sdk = (ModuleType(name) for name in ("lark_oapi", "lark_oapi.ws", "lark_oapi.ws.client"))
    package.ws, ws.client = ws, sdk
    original = {"proxy": None}
    sdk._ws_connect_kwargs = lambda: original
    for module in (package, ws, sdk):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    def connect(_uri, *, proxy=True, open_timeout=10):
        pytest.fail("参数回归不得启动连接")

    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(feishu_ws, "_ws_connect_patched", False)
    feishu_ws._patch_ws_connect(proxy)
    assert feishu_ws._ws_connect_patched is True
    assert sdk._ws_connect_kwargs() == ({"open_timeout": 45.0, "proxy": proxy} if proxy else {"open_timeout": 45.0})
    assert original == {"proxy": None}
    feishu_ws._patch_ws_connect("http://different.invalid:8080")
    assert sdk._ws_connect_kwargs().get("proxy", "") == proxy


def test_ws_kwargs_keeps_sdk_timeout_and_explicit_proxy():
    assert feishu_ws._fixed_ws_kwargs({"open_timeout": 60, "proxy": "http://sdk.invalid"}, True, "") == {
        "open_timeout": 60, "proxy": "http://sdk.invalid",
    }
    assert feishu_ws._fixed_ws_kwargs({}, False, "") == {}


def test_lark_log_filter_redacts_proxy_password_and_ws_token():
    record = logging.LogRecord("Lark", logging.ERROR, "", 0,
        "connect failed %s wss://msg-frontier.feishu.cn/ws?token=%s", ("socks5h://synthetic:proxy-secret@proxy.invalid:1080", "ws-secret"), None)
    assert feishu_ws._LarkProcessorNotFoundFilter().filter(record) is True
    rendered = record.getMessage()
    assert "proxy-secret" not in rendered and "ws-secret" not in rendered
    assert "connect failed" in rendered


def test_outer_reconnect_never_logs_raw_proxy_error(caplog):
    client = _FakeClient(stop_at=2)

    def start():
        client.starts += 1
        client._stopped = client.starts >= 2
        raise RuntimeError("socks5h://synthetic:proxy-secret@proxy.invalid:1080 app-secret")

    client.start = start
    with caplog.at_level(logging.WARNING):
        _run(client)
    assert "proxy-secret" not in caplog.text and "app-secret" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


@pytest.mark.parametrize(("connection", "status", "reason"), [
    (None, "degraded", "FEISHU_WS_CONNECTING"),
    (SimpleNamespace(state=State.CONNECTING), "degraded", "FEISHU_WS_CONNECTING"),
    (SimpleNamespace(state=State.OPEN), "healthy", "FEISHU_WS_CONNECTED"),
    (SimpleNamespace(state=State.CLOSING, closed=False), "degraded", "FEISHU_WS_DISCONNECTED"),
    (SimpleNamespace(state=State.CLOSED), "degraded", "FEISHU_WS_DISCONNECTED"),
    (SimpleNamespace(closed=False), "healthy", "FEISHU_WS_CONNECTED"),
    (SimpleNamespace(closed=True), "degraded", "FEISHU_WS_DISCONNECTED"),
    (SimpleNamespace(state="OPEN", closed=False), "degraded", "FEISHU_WS_STATE_UNKNOWN"),
    (SimpleNamespace(), "degraded", "FEISHU_WS_STATE_UNKNOWN"),
])
def test_transport_health_reads_sdk_connection_fact(connection, status, reason):
    client = feishu_ws.FeishuWsClient(app_id="synthetic", app_secret="synthetic-secret", on_payload=lambda _p: None)
    assert client.transport_health() == {"status": "degraded", "reason_code": "FEISHU_WS_CONNECTING"}
    client._client = SimpleNamespace(_conn=connection)
    assert client.transport_health() == {"status": status, "reason_code": reason}
    client._stopped = True
    assert client.transport_health() == {"status": "stopped", "reason_code": "FEISHU_WS_STOPPED"}


def test_transport_health_projects_live_transition_without_second_state():
    connection = SimpleNamespace(state=State.OPEN)
    client = feishu_ws.FeishuWsClient(app_id="synthetic", app_secret="synthetic-secret", on_payload=lambda _p: None)
    client._client = SimpleNamespace(_conn=connection)
    assert client.transport_health()["status"] == "healthy"
    connection.state = State.CLOSED
    assert client.transport_health()["status"] == "degraded"
    client._client._conn = None
    assert client.transport_health()["status"] == "degraded"
    connection.state = State.OPEN
    client._client._conn = connection
    assert client.transport_health()["status"] == "healthy"
