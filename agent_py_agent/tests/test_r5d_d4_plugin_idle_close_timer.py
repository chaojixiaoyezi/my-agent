"""A07/D4: an idle plugin connection is closed by the pool's own deadline check after the last session/panel leaves,
without waiting for that owner's next panel query or event drain."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from agent_py_agent.agent.plugin_channel import ChannelCall, PluginChannelPool
from agent_py_agent.agent.plugin_channel import pool as pool_module

# 测试里把空闲期限缩短到这么多秒。
IDLE_SECONDS = 0.4
# 等待关闭或确认未关闭的上限（秒）。
WAIT_SECONDS = 3.0


# 类用途: 假传输：可按 gate 阻塞一次请求，模拟在途。
class _Transport:
    def __init__(self, gate: threading.Event | None) -> None:
        self.gate, self.entered = gate, threading.Event()

    # 函数用途: 发送前复核，按需阻塞，返回固定结果。
    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(WAIT_SECONDS)
        return {"ok": True}


# 类用途: 假插件客户端：记录 stop 次数（插件子进程是否被关）。
class _Client:
    def __init__(self, installation, gate: threading.Event | None = None) -> None:
        self.installation, self.transport, self.stopped = installation, _Transport(gate), threading.Event()
        self.activation_ref = SimpleNamespace(require=lambda: installation)

    # 函数用途: 返回假传输。
    def start(self):
        return self.transport

    # 函数用途: 记录关闭。
    def stop(self):
        self.stopped.set()


# 函数用途: 建一个只有一个激活的池，返回（池，安装，已建客户端列表）。
def _pool(gate: threading.Event | None = None):
    installation = SimpleNamespace(activation=SimpleNamespace(activation_id="act-1"), enabled=True)
    clients: list[_Client] = []

    def factory(_owner, inst):
        clients.append(_Client(inst, gate))
        return clients[-1]

    return PluginChannelPool(client_factory=factory), installation, clients


# 函数用途: 按 B2 用法先 acquire 再 request 一次。
def _use(pool: PluginChannelPool, installation) -> None:
    connection = pool.acquire("owner-a", installation, time.monotonic())
    pool.request(connection, ChannelCall(owner=object(), method="panel/render", params={}))


# 函数用途: 最后一次使用之后没有任何调用方再来，池自己到期关掉客户端（保留连接条目）。
def test_idle_client_is_closed_without_another_caller(monkeypatch):
    monkeypatch.setattr(pool_module, "IDLE_CLOSE_SECONDS", IDLE_SECONDS)
    pool, installation, clients = _pool()
    try:
        _use(pool, installation)
        assert clients[0].stopped.wait(WAIT_SECONDS), "最后一个面板离开后，空闲插件客户端必须被关"
        connection = pool.get(("owner-a", "act-1"))
        assert connection is not None and connection.client is None
        _use(pool, installation)
        assert len(clients) == 2 and not clients[1].stopped.is_set(), "再次使用会重建客户端"
    finally:
        pool.close()


# 函数用途: 期限内再次 acquire 会把关闭顺延到新期限，而不是按第一次的期限关掉。
def test_reuse_before_the_deadline_postpones_the_close(monkeypatch):
    monkeypatch.setattr(pool_module, "IDLE_CLOSE_SECONDS", IDLE_SECONDS)
    pool, installation, clients = _pool()
    try:
        _use(pool, installation)
        time.sleep(IDLE_SECONDS * 0.5)
        _use(pool, installation)
        assert not clients[0].stopped.wait(IDLE_SECONDS * 0.75), "顺延后原期限不能关"
        assert clients[0].stopped.wait(WAIT_SECONDS), "顺延后的期限到了仍须关"
    finally:
        pool.close()


# 函数用途: 在途请求超过空闲期限也不被关；请求结束、再空闲满一个周期后才关。
def test_in_flight_request_is_never_closed(monkeypatch):
    monkeypatch.setattr(pool_module, "IDLE_CLOSE_SECONDS", IDLE_SECONDS)
    gate = threading.Event()
    pool, installation, clients = _pool(gate)
    worker = threading.Thread(target=_use, args=(pool, installation), daemon=True)
    try:
        worker.start()
        assert _wait_for_client(clients).transport.entered.wait(WAIT_SECONDS)
        assert not clients[0].stopped.wait(IDLE_SECONDS * 2.5), "在途请求的连接不能被空闲关闭"
        gate.set()
        worker.join(WAIT_SECONDS)
        assert clients[0].stopped.wait(WAIT_SECONDS), "请求结束后空闲满一个周期须关"
    finally:
        gate.set()
        pool.close()


# 函数用途: 等后台启动线程建出第一个客户端。
def _wait_for_client(clients: list[_Client]) -> _Client:
    deadline = time.monotonic() + WAIT_SECONDS
    while not clients and time.monotonic() < deadline:
        time.sleep(0.01)
    return clients[0]
