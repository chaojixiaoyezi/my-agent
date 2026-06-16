
from __future__ import annotations

"""log_ops daemon 进程级启停/续接 + serve 主循环 + liveness 探测(真子进程,CI 友好)。

用 manager.start_daemon 真起独立 daemon 进程,验证:
  - daemon 真采到数据(存档/候选/对账)、心跳更新、PID 存活探测;
  - log_monitor_stop 真停掉进程;
  - 停后重新 start 从断点续采不重(状态文件续接)。
所有进程 teardown 兜底 kill,短轮询等待,不留孤儿。
"""

import json
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling.log_ops import manager
from agent_py_agent.agent.tooling.log_ops.daemon import serve
from agent_py_agent.agent.tooling.log_ops.store import LogOpsStore, build_source_specs


def _alert_line(idx: int) -> str:
    return f"2026-06-16T00:00:{idx:02d} ALERT ALERT-{idx:06d} reverse shell /dev/tcp/1.1.1.1/4444\n"


def _wait_until(predicate, timeout: float = 12.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def store(tmp_path: Path):
    st = LogOpsStore(tmp_path / ".log_ops", "default")
    yield st
    # teardown:兜底停掉可能残留的 daemon。
    try:
        manager.stop_daemon(st)
    except Exception:
        pass


# ----------------------- serve 主循环(同进程,max_cycles 限定) -----------------------


def test_serve_runs_bounded_cycles_and_writes_heartbeat(tmp_path: Path) -> None:
    src = tmp_path / "a.log"
    src.write_text(_alert_line(1) + "noise\n", encoding="utf-8")
    store = LogOpsStore(tmp_path / ".log_ops", "default")
    store.write_config(build_source_specs([str(src)]), poll_interval_seconds=0.05)

    cycles = serve(store, poll_interval_seconds=0.05, max_cycles=2)
    assert cycles == 2
    sid = build_source_specs([str(src)])[0].source_id
    assert store.count_archive_lines(sid) == 2
    assert store.count_candidates() == 1
    daemon_record = store.read_daemon()
    assert daemon_record["status"] == "stopped"
    assert daemon_record["cycles"] == 2
    assert "heartbeat_at" in daemon_record


# ----------------------- 真子进程 daemon 启停 -----------------------


def test_start_daemon_collects_then_stop(store) -> None:
    workspace_dir = store.root.parent.parent  # tmp_path
    src = workspace_dir / "live.log"
    src.write_text(_alert_line(1), encoding="utf-8")

    started = manager.start_daemon(store, [str(src)], poll_interval_seconds=0.1)
    assert started["ok"] is True
    assert started["status"] == "started"
    pid = started["pid"]

    sid = build_source_specs([str(src)])[0].source_id
    # daemon 进程应采到第一行并落档。
    assert _wait_until(lambda: store.count_archive_lines(sid) >= 1), "daemon 未采到首行"
    assert _wait_until(lambda: store.count_candidates() >= 1), "daemon 未产候选"

    # 心跳在更新(liveness 存活)。
    live = manager.daemon_liveness(store)
    assert live["alive"] is True
    assert live["pid"] == pid

    # 运行中 append 新行 → daemon 增量采到。
    with src.open("a", encoding="utf-8") as handle:
        handle.write(_alert_line(2))
    assert _wait_until(lambda: store.count_archive_lines(sid) >= 2), "daemon 未采到增量行"

    # 停掉。
    stopped = manager.stop_daemon(store)
    assert stopped["status"] == "stopped"
    assert _wait_until(lambda: not manager.daemon_liveness(store)["alive"]), "daemon 未真正退出"


def test_start_is_idempotent_when_running(store) -> None:
    workspace_dir = store.root.parent.parent
    src = workspace_dir / "live.log"
    src.write_text(_alert_line(1), encoding="utf-8")
    first = manager.start_daemon(store, [str(src)], poll_interval_seconds=0.1)
    assert first["ok"]
    assert _wait_until(lambda: manager.daemon_liveness(store)["alive"])
    # 再 start 同一个 → already_running,不起第二个进程。
    second = manager.start_daemon(store, [str(src)], poll_interval_seconds=0.1)
    assert second["status"] == "already_running"
    assert second["pid"] == first["pid"]


def test_restart_resumes_from_checkpoint_no_dup(store) -> None:
    workspace_dir = store.root.parent.parent
    src = workspace_dir / "live.log"
    src.write_text(_alert_line(1) + _alert_line(2), encoding="utf-8")
    sid = build_source_specs([str(src)])[0].source_id

    manager.start_daemon(store, [str(src)], poll_interval_seconds=0.1)
    assert _wait_until(lambda: store.count_archive_lines(sid) >= 2)
    manager.stop_daemon(store)
    assert _wait_until(lambda: not manager.daemon_liveness(store)["alive"])
    archive_after_first = store.count_archive_lines(sid)
    assert archive_after_first == 2

    # 停机期间写入新行。
    with src.open("a", encoding="utf-8") as handle:
        handle.write(_alert_line(3))

    # 重启:从断点续采,只采到新行(不重采 1/2)。
    manager.start_daemon(store, [str(src)], poll_interval_seconds=0.1)
    assert _wait_until(lambda: store.count_archive_lines(sid) >= 3)
    # 给它多跑几拍,确认没有重采导致超过 3。
    time.sleep(0.6)
    assert store.count_archive_lines(sid) == 3
    manager.stop_daemon(store)


def test_liveness_detects_dead_pid(store) -> None:
    # 写一个不存在的高 PID 的 daemon 记录 → liveness 判 dead。
    store.write_daemon({"status": "running", "pid": 999_999_999, "heartbeat_at": time.time()})
    live = manager.daemon_liveness(store)
    assert live["alive"] is False
