"""Gateway 进程身份与主机名变化（2026-10-02，step17a 切换时的真实缺陷）。

macOS 没有 /etc/machine-id，process_host_id 原来退回 socket.gethostname()；换网络后主机名会变（这次变成 anonymous）。
SIGTERM 处理函数现算身份，host_id 和启动时记下的对不上，主循环把停止请求当成发给别人的，网关停不下来。锁定：
1. 信号处理函数用启动时记下的本代身份，和主循环比对的是同一份：之后无论怎样现算身份，SIGTERM 都能让主循环退出；
2. 主机身份一个进程只算一次（缓存）：主机名中途变化，本进程前后身份一致；
3. macOS 优先用硬件 UUID（libc gethostuuid，与 IOPlatformUUID 同值，不起子进程），取不到才退回主机名；
4. 跨进程：有稳定来源时主机名变化不影响存活核验；只剩主机名时，主机名变后新起的进程对旧记录给 None
   （无法判断、按 TTL 兜底），不会误判已死。
只改本进程内的函数替身，不改系统主机名，不对测试进程真发信号。
"""
from __future__ import annotations

import os
import re
import signal
import sys
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.cli import gateway_process
from agent_py_agent.cli.models import GatewayRunContext

_UUID = "12345678-9ABC-DEF0-1234-56789ABCDEF0"


@pytest.fixture
def fresh_host_cache():
    daemon_metadata._cached_process_host_id.cache_clear()
    yield
    daemon_metadata._cached_process_host_id.cache_clear()


def _hostname(monkeypatch, name: str) -> None:
    monkeypatch.setattr(daemon_metadata.socket, "gethostname", lambda: name)


def _new_process() -> None:
    # 只模拟“新起一个进程”：丢掉本进程缓存，下一次取身份重新从来源计算。
    daemon_metadata._cached_process_host_id.cache_clear()


# ---------------------------------------------------------------- 1. 信号处理函数用启动身份


def test_sigterm_still_stops_the_service_loop_after_identity_drifts(tmp_path, monkeypatch, fresh_host_cache):
    stop_path = tmp_path / "gateway.stop"
    paths = SimpleNamespace(stop_request=stop_path)
    startup = {"host_id": "host-at-startup", "pid": os.getpid(), "start_time": "started"}
    context = GatewayRunContext(agent=SimpleNamespace(), paths=paths, config_path=tmp_path / "config.yaml",
                                process_identity=startup, process_started_at=1.0)
    # 启动之后再现算身份，主机名已变：host_id 不同（step17a 现场）。
    monkeypatch.setattr(gateway_process, "build_process_identity",
                        lambda pid=None: {**startup, "host_id": "host-after-network-change"})
    monkeypatch.setattr(gateway_process, "drain_for_requested_restart", lambda _context, _identity: None)
    previous = gateway_process._install_gateway_signal_handlers(
        paths, gateway_process._gateway_context_process_identity(context))
    try:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # 直接调已安装的 handler，不对测试进程真发信号
    finally:
        gateway_process._restore_gateway_signal_handlers(previous)
    result: list[object] = []
    loop = threading.Thread(target=lambda: result.append(gateway_process._run_gateway_service_loop(context)),
                            daemon=True)
    loop.start()
    loop.join(5)
    assert not loop.is_alive() and result[0]["summary"] == "stop requested"
    assert result[0]["stop_request"]["target_process"]["host_id"] == "host-at-startup"


# ---------------------------------------------------------------- 2. 一个进程只算一次


def test_host_id_stays_fixed_inside_a_process_when_hostname_changes(monkeypatch, fresh_host_cache):
    monkeypatch.setattr(daemon_metadata, "_stable_host_source", lambda: "")  # 只剩主机名可用（无 machine-id、无 UUID）
    _hostname(monkeypatch, "mac-a.local")
    before = daemon_metadata.process_host_id()
    _hostname(monkeypatch, "anonymous")
    assert daemon_metadata.process_host_id() == before
    assert daemon_metadata.build_process_identity()["host_id"] == before


# ---------------------------------------------------------------- 3. macOS 用硬件 UUID


def test_macos_uses_platform_uuid_before_hostname(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_metadata, "_MACHINE_ID_PATHS", (tmp_path / "no-machine-id",))
    monkeypatch.setattr(daemon_metadata, "_macos_platform_uuid", lambda: _UUID)
    assert daemon_metadata._stable_host_source("darwin") == _UUID
    assert daemon_metadata._stable_host_source("linux") == ""  # 只有 macOS 取硬件 UUID
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("0123456789abcdef0123456789abcdef\n", encoding="utf-8")
    monkeypatch.setattr(daemon_metadata, "_MACHINE_ID_PATHS", (machine_id,))
    assert daemon_metadata._stable_host_source("darwin") == "0123456789abcdef0123456789abcdef"  # machine-id 优先


@pytest.mark.skipif(sys.platform != "darwin", reason="gethostuuid 只在 macOS 上有")
def test_macos_platform_uuid_is_read_without_a_subprocess(monkeypatch):
    def no_subprocess(*args, **kwargs):
        raise AssertionError("取硬件 UUID 不应起子进程")

    monkeypatch.setattr(daemon_metadata.subprocess, "run", no_subprocess)
    monkeypatch.setattr(daemon_metadata.subprocess, "Popen", no_subprocess)
    value = daemon_metadata._macos_platform_uuid()
    assert re.fullmatch(r"[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}", value)
    assert daemon_metadata._macos_platform_uuid() == value


def test_platform_uuid_unavailable_falls_back_to_hostname(tmp_path, monkeypatch, fresh_host_cache):
    monkeypatch.setattr(daemon_metadata, "_MACHINE_ID_PATHS", (tmp_path / "no-machine-id",))
    monkeypatch.setattr(daemon_metadata, "_macos_platform_uuid", lambda: "")
    assert daemon_metadata._stable_host_source("darwin") == ""
    monkeypatch.setattr(daemon_metadata, "_stable_host_source", lambda: "")
    _hostname(monkeypatch, "mac-a.local")
    first = daemon_metadata.process_host_id()
    _new_process()
    _hostname(monkeypatch, "mac-b.local")
    assert daemon_metadata.process_host_id() != first  # 退回主机名时，新进程跟着主机名走


# ---------------------------------------------------------------- 4. 跨进程核验


def test_cross_process_liveness_is_unaffected_by_hostname_with_a_stable_source(monkeypatch, fresh_host_cache):
    monkeypatch.setattr(daemon_metadata, "_stable_host_source", lambda: _UUID)
    _hostname(monkeypatch, "mac-a.local")
    record = daemon_metadata.build_process_identity()
    _new_process()
    _hostname(monkeypatch, "anonymous")
    assert daemon_metadata.process_identity_is_live(record) is True  # 同一台机器、同一个活进程


def test_cross_process_liveness_is_unknown_after_hostname_change_without_a_stable_source(monkeypatch, fresh_host_cache):
    monkeypatch.setattr(daemon_metadata, "_stable_host_source", lambda: "")
    _hostname(monkeypatch, "mac-a.local")
    record = daemon_metadata.build_process_identity()
    _new_process()
    _hostname(monkeypatch, "anonymous")
    # 新进程算出另一个 host_id：无法证明是同一主机，给 None（按 TTL 兜底），绝不判成“已死”。
    assert daemon_metadata.process_identity_is_live(record) is None
