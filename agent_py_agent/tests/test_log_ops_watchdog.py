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


def test_daemon_record_uses_own_start_time_not_inherited(tmp_path):
    """[R1-T 回归]重启后新 daemon 写自己的 start_time,绝不继承 daemon.json 里旧进程的指纹。
    否则新 daemon 顶着死进程指纹,liveness 拿真实指纹一比就误判 PID 复用→反复误重启。"""
    from agent.tooling.log_ops.daemon import _DaemonRunState, _write_daemon_record
    store = LogOpsStore(tmp_path, "m5")
    store.ensure_dirs()
    # 旧 daemon 残留记录(旧 pid + 旧指纹 + 监控起始时间)
    store.write_daemon({"status": "running", "pid": 11111, "start_time": "OLD-fp", "started_at": 100.0})
    # 新 daemon 启动,带自己的真实指纹
    run = _DaemonRunState(store=store, interval=1.0, pid=22222, start_time="NEW-fp")
    _write_daemon_record(run, "running")
    rec = store.read_daemon()
    assert rec["start_time"] == "NEW-fp"  # 用新进程的指纹,不继承旧的(修复反复误重启)
    assert rec["started_at"] == 100.0  # started_at 仍继承(监控会话连续性,uptime 不清零)


def test_watchdog_serve_respects_stopped(tmp_path, monkeypatch):
    """[R2]daemon 被 stop → 主动看门狗一同退(尊重喊停),不重拉。"""
    store = LogOpsStore(tmp_path, "wd1")
    store.ensure_dirs()
    store.write_daemon({"status": "stopped", "pid": os.getpid()})
    calls = []
    monkeypatch.setattr(mgr, "ensure_daemon_alive", lambda s: calls.append(1))
    assert mgr.watchdog_serve(store, max_cycles=5, interval=0) == 0  # status stopped 立即退
    assert calls == []  # 不重拉


def test_watchdog_serve_revives_dead_daemon(tmp_path, monkeypatch):
    """[R2]daemon 进程死了 → 看门狗主动 ensure_daemon_alive 重拉(无需等 agent 查 status)。"""
    store = LogOpsStore(tmp_path, "wd2")
    store.ensure_dirs()
    store.write_daemon({"status": "running", "pid": 2_000_000_000, "start_time": None, "heartbeat_at": 1})
    calls = []
    monkeypatch.setattr(mgr, "ensure_daemon_alive", lambda s: calls.append(1))
    mgr.watchdog_serve(store, max_cycles=2, interval=0)
    assert len(calls) >= 1  # 检测到死,主动重拉


def test_watchdog_singleton(tmp_path, monkeypatch):
    """[R2]已有活看门狗 → 新看门狗单例退出,不重复守护。"""
    store = LogOpsStore(tmp_path, "wd3")
    store.ensure_dirs()
    from agent.common.json_io import write_json_file_atomic
    write_json_file_atomic(store.watchdog_path, {"pid": 88888, "start_time": "fp", "heartbeat_at": time.time()})
    monkeypatch.setattr(mgr.heartbeat, "process_alive", lambda pid, **k: True)
    assert mgr.watchdog_serve(store, max_cycles=3, interval=0) == 0  # 已有活看门狗,单例退
