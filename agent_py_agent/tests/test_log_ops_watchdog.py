"""看门狗自愈集成测试(组件③):PID 复用检测 + 自愈边界(尊重喊停/不擅起/重拉崩溃)。"""

import os
import time

import agent.tooling.log_ops.manager as mgr
from agent.common.heartbeat import process_start_time
from agent.tooling.log_ops.store import LogOpsStore, build_source_specs


def test_liveness_detects_pid_reuse(tmp_path):
    """daemon.json 记的 start_time 与当前 PID 实际启动指纹不符 → 判 process_gone(防 PID 复用误判活)。"""
    store = LogOpsStore(tmp_path, "m1")
    store.ensure_dirs()
    store.write_daemon({
        "status": "running", "pid": os.getpid(),  # 活 PID
        "start_time": "FAKE-fingerprint-not-this-process", "heartbeat_at": time.time(),
    })
    if process_start_time(os.getpid()) is None:
        return  # 平台取不到启动指纹→保守判活,跳过
    live = mgr.daemon_liveness(store)
    assert live["alive"] is False
    assert live["reason"] == "process_gone"  # 指纹不符=不是当初那个进程


def test_ensure_respects_user_stop(tmp_path):
    """用户主动 stop 的 daemon,看门狗不复活(尊重喊停,和无限期值守"只认喊停"一致)。"""
    store = LogOpsStore(tmp_path, "m2")
    store.ensure_dirs()
    store.write_daemon({"status": "stopped", "pid": os.getpid(), "start_time": process_start_time(os.getpid())})
    result = mgr.ensure_daemon_alive(store)
    assert result["restarted"] is False
    assert result["reason"] == "stopped"  # 不复活


def test_ensure_skips_never_configured(tmp_path):
    """从没配置/起过的 monitor,看门狗不擅自起(no_pid 不是 process_gone)。"""
    store = LogOpsStore(tmp_path, "m3")
    store.ensure_dirs()
    result = mgr.ensure_daemon_alive(store)
    assert result["restarted"] is False


def test_ensure_restarts_process_gone(tmp_path, monkeypatch):
    """曾起过(有 pid)但进程消失 + 有源配置 → 自动重拉(monkeypatch 拦真进程启动)。"""
    store = LogOpsStore(tmp_path, "m4")
    store.ensure_dirs()
    store.write_config(build_source_specs(["/tmp/nonexistent_watchdog_test.log"]), poll_interval_seconds=2)
    store.write_daemon({"status": "running", "pid": 2_000_000_000, "start_time": None, "heartbeat_at": 1})

    class _FakeProc:
        pid = 312345

    monkeypatch.setattr(mgr, "_spawn_daemon", lambda *a, **k: _FakeProc())
    result = mgr.ensure_daemon_alive(store)
    assert result["restarted"] is True
    assert result["pid"] == 312345
    assert result["recovered_from"] == "process_gone"
