"""B2 生命周期回归（复审 be 的 4 个探针，2026-10-03 转正）：后台启动与撤销/关闭交错时，插件进程不能漏关。

假客户端按先后记录 start_begin / start_end / stop；“启动完成之后没有再 stop” = 插件进程没人收。
原探针来自 ~/.my-agent/decision-evidence/m1-b2-review-20261003/test_b2_lifecycle_probe.py，
在修复前的 B2 头上 4 个全部失败；这里保留同一观察方式，防止回归。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_channel import ChannelCall, PluginChannelPool, PluginChannelTimeout


class _Transport:
    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        return {"ok": True}


class _Client:
    def __init__(self, installation, gate, log):
        self.installation = installation
        self.gate = gate
        self.log = log
        self.activation_ref = SimpleNamespace(require=lambda: self.installation)

    def start(self):
        self.log.append("start_begin")
        self.gate.wait(5)
        self.log.append("start_end")
        return _Transport()

    def stop(self):
        self.log.append("stop")


def _setup():
    gate = threading.Event()
    log: list[str] = []
    installation = SimpleNamespace(activation=SimpleNamespace(activation_id="act-1"))
    pool = PluginChannelPool(client_factory=lambda _o, inst: _Client(inst, gate, log), clock=lambda: 100.0)
    conn = pool.acquire("owner", installation, 100.0)
    with pytest.raises(PluginChannelTimeout):
        pool.request(conn, ChannelCall(owner=object(), method="m", params={}, timeout=0.05))
    assert log == ["start_begin"]
    return pool, conn, gate, log


def _stopped_after_start(log: list[str]) -> bool:
    return "start_end" in log and "stop" in log[log.index("start_end"):]


def test_retire_during_background_start_stops_started_client():
    pool, conn, gate, log = _setup()
    pool.retire_stale("owner", set())
    gate.set()
    assert conn.start_event.wait(5)
    assert _stopped_after_start(log), log


def test_close_during_background_start_stops_started_client():
    pool, conn, gate, log = _setup()
    pool.close()
    gate.set()
    assert conn.start_event.wait(5)
    assert _stopped_after_start(log), log
    assert conn.transport is None, "关闭后的池上不应再发布可用传输"


def test_request_on_connection_after_close_does_not_start_new_client():
    gate = threading.Event()
    gate.set()
    log: list[str] = []
    installation = SimpleNamespace(activation=SimpleNamespace(activation_id="act-1"))
    pool = PluginChannelPool(client_factory=lambda _o, inst: _Client(inst, gate, log), clock=lambda: 100.0)
    conn = pool.acquire("owner", installation, 100.0)
    pool.close()
    try:
        pool.request(conn, ChannelCall(owner=object(), method="m", params={}, timeout=1.0))
    except Exception:  # noqa: BLE001 期望行为是拒绝；这里只看有没有起新客户端
        pass
    assert "start_begin" not in log, log


class _HookTransport:
    def __init__(self, hook):
        self.hook = hook

    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        self.hook()
        return {"ok": True}


def test_detach_during_request_is_reported_as_revoked():
    from agent_py_agent.agent.plugin_channel import PluginChannelRevoked

    installation = SimpleNamespace(activation=SimpleNamespace(activation_id="act-1"))
    holder = {}

    class _C:
        activation_ref = SimpleNamespace(require=lambda: installation)

        def start(self):
            return _HookTransport(lambda: holder["pool"].retire_stale("owner", set()))

        def stop(self):
            pass

    pool = PluginChannelPool(client_factory=lambda _o, _i: _C(), clock=lambda: 100.0)
    holder["pool"] = pool
    conn = pool.acquire("owner", installation, 100.0)
    with pytest.raises(PluginChannelRevoked):
        pool.request(conn, ChannelCall(owner=object(), method="m", params={}, timeout=1.0))
