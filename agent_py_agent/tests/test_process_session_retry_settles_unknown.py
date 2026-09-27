"""重试停止能结清实例已消失的旧 unknown 记录；首次停止对已消失实例仍不凭空确认；host 自行退出的竞态收敛为确认。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.tooling import process_session_cleanup
from agent_py_agent.agent.tooling.process_registry import (
    ProcessTerminationReceipt,
    capture_process_birth_token,
)
from agent_py_agent.agent.tooling.process_session_cleanup import stop_process_session
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore

SCOPE = {"owner_id": "local/main", "owner_home": "/owner", "plugin_id": "sample-peek", "activation_id": "a" * 64}


# 函数用途: 起两个会立刻退出的真实进程，拿到它们的 PID 与出生标识，等它们被回收后返回（确保 PID 已不存在）。
def _vanished_instances() -> tuple[tuple[int, str], tuple[int, str]]:
    instances = []
    for _ in range(2):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        token = capture_process_birth_token(proc.pid) or "birth"
        proc.wait(timeout=10)
        instances.append((proc.pid, token))
    return instances[0], instances[1]


# 函数用途: 构造一条 v3 共享激活进程记录，实例身份指向已经不存在的进程。
def _record(session_id: str, host: tuple[int, str], child: tuple[int, str], *, status: str, prior_cleanup: bool) -> dict:
    now = time.time()
    record = {
        "schema": "managed_process_session.v3", "session_id": session_id,
        "access_scope": {"owner_id": SCOPE["owner_id"], "conversation_id": "", "owner_home": SCOPE["owner_home"]},
        "execution_scope": {"owner_home": SCOPE["owner_home"], "thread_id": "", "root_task_id": "", "run_id": "", "attempt_id": ""},
        "activation_scope": dict(SCOPE), "completion_target": {},
        "launcher_pid": os.getpid(), "launcher_birth_token": capture_process_birth_token(os.getpid()) or "launcher",
        "pid": host[0], "pid_birth_token": host[1], "child_pid": child[0], "child_pid_birth_token": child[1],
        "revision": 0, "stop_requested": True, "handoff_confirmed": True, "child_launch_started": True,
        "reserved_at": now - 60, "started_at": now - 59, "status": status, "exit_code": None, "finished_at": None,
        "command": "python -m sample_peek", "cwd": "/owner", "output_file": "/owner/out.log", "host_state_file": "/owner/host.json",
        "completion_notice_id": "",
    }
    if prior_cleanup:
        record["termination"] = {"cleanup": {"confirmed": False, "instances": []}}
    return record


@pytest.fixture
def store(tmp_path):
    return ProcessSessionStore(tmp_path / "process_sessions")


def test_retried_stop_settles_unknown_record_when_instances_are_gone(store):
    host, child = _vanished_instances()
    written = store.write(_record("bg-stale-unknown", host, child, status="unknown", prior_cleanup=True))
    cleanup = stop_process_session(store, written)
    assert cleanup.confirmed is True
    assert cleanup.record["status"] == "killed" and cleanup.record["finished_at"]
    assert cleanup.record["termination"]["cleanup"]["confirmed"] is True
    assert cleanup.terminations == (), "实例已不存在，没有可终止的对象，不伪造回执"
    stored = store.load("bg-stale-unknown").record
    assert stored["status"] == "killed" and stored["termination"]["cleanup"]["confirmed"] is True


def test_first_stop_of_vanished_running_record_still_needs_terminal_evidence(store):
    host, child = _vanished_instances()
    written = store.write(_record("bg-vanished-running", host, child, status="running", prior_cleanup=False))
    cleanup = stop_process_session(store, written)
    assert cleanup.confirmed is False and cleanup.record["status"] == "unknown"
    second = stop_process_session(store, store.load("bg-vanished-running").record)
    assert second.confirmed is True and second.record["status"] == "killed", "第二次重试按出生身份结清"


# 2026-09-27 Codex 全仓复现：stop 预检时 host 还活着，终止原语采快照前 host 已按停止意图清完 child、写回已确认回执并退出，
# 出生身份读不到，原语只能回 identity_changed（未发信号）。记录已是 host 写的 killed，却因这一张回执被判 unknown。
# 这里用替身回执固定这个时序：只替换锁外终止那一步，Store 事务、实例消失判定都走真实代码。
_RACE_RECEIPT = ProcessTerminationReceipt("identity_changed", False, None, 3, ())


# 函数用途: 构造 host 已自行收尾的记录：status=killed，顶层 termination 是 host 写的已确认 child 回执。
def _host_settled_record(session_id: str, host, child, *, host_confirmed: bool = True) -> dict:
    record = _record(session_id, host, child, status="killed", prior_cleanup=False)
    record["termination"] = {"confirmed": host_confirmed, "method": "SIGTERM", "observed_processes": 1, "unresolved_pids": []}
    record["exit_code"], record["finished_at"] = -15, time.time()
    return record


# 函数用途: 让锁外终止那一步返回给定回执，模拟 host 恰好在快照前退出的竞态。
def _race_with(monkeypatch, *receipts):
    monkeypatch.setattr(process_session_cleanup, "_terminate_frozen_instances", lambda record, host_process: receipts)


def test_stop_converges_when_host_exits_on_its_own_during_the_snapshot(store, monkeypatch):
    host, child = _vanished_instances()
    written = store.write(_host_settled_record("bg-host-self-exit", host, child))
    _race_with(monkeypatch, replace(_RACE_RECEIPT, unresolved_pids=(host[0],)))
    cleanup = stop_process_session(store, written)
    assert cleanup.confirmed is True
    assert cleanup.record["status"] == "killed" and cleanup.record["exit_code"] == -15, "host 写的终态与退出码不重写"
    assert cleanup.record["termination"]["confirmed"] is True, "host 的 child 回执原样保留"
    assert cleanup.record["termination"]["cleanup"]["confirmed"] is True
    receipt, = cleanup.record["termination"]["cleanup"]["instances"]
    assert receipt["method"] == "host_exited_during_stop" and receipt["confirmed"] is True, "改写的来历写在 method 里"
    assert list(receipt["unresolved_pids"]) == [] and receipt["observed_processes"] == 3
    assert [item.method for item in cleanup.terminations] == ["host_exited_during_stop"]
    stored = store.load("bg-host-self-exit").record
    assert stored["termination"]["cleanup"]["confirmed"] is True, "改写后的回执通过存储校验并落盘"


def test_host_race_receipt_does_not_confirm_without_the_hosts_own_confirmed_receipt(store, monkeypatch):
    host, child = _vanished_instances()
    written = store.write(_host_settled_record("bg-host-unconfirmed", host, child, host_confirmed=False))
    _race_with(monkeypatch, replace(_RACE_RECEIPT, unresolved_pids=(host[0],)))
    cleanup = stop_process_session(store, written)
    assert cleanup.confirmed is False and cleanup.record["termination"]["cleanup"]["confirmed"] is False
    assert [item.method for item in cleanup.terminations] == ["identity_changed"], "缺 host 确认时原回执不改写"


def test_host_race_receipt_does_not_confirm_while_an_instance_is_still_alive(store, monkeypatch):
    host, _gone = _vanished_instances()
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        token = capture_process_birth_token(alive.pid)
        assert token, "需要读到存活进程的出生标识"
        written = store.write(_host_settled_record("bg-child-alive", host, (alive.pid, token)))
        _race_with(monkeypatch, replace(_RACE_RECEIPT, unresolved_pids=(host[0],)))
        cleanup = stop_process_session(store, written)
        assert cleanup.confirmed is False, "child 仍是原实例在跑，不能凭 host 回执确认"
        assert [item.method for item in cleanup.terminations] == ["identity_changed"], "实例未消失时原回执不改写"
    finally:
        alive.kill()
        alive.wait(timeout=10)


def test_other_unconfirmed_receipts_still_block_a_host_settled_stop(store, monkeypatch):
    host, child = _vanished_instances()
    written = store.write(_host_settled_record("bg-host-residue", host, child))
    _race_with(monkeypatch, ProcessTerminationReceipt("SIGTERM->SIGKILL", False, None, 2, (host[0],)))
    cleanup = stop_process_session(store, written)
    assert cleanup.confirmed is False, "信号后仍有残留的回执不属于竞态例外"
    assert [item.method for item in cleanup.terminations] == ["SIGTERM->SIGKILL"]


def test_host_race_receipt_needs_the_host_written_terminal_status(store, monkeypatch):
    host, child = _vanished_instances()
    record = _host_settled_record("bg-not-terminal", host, child)
    record["status"], record["exit_code"], record["finished_at"] = "unknown", None, None
    written = store.write(record)
    _race_with(monkeypatch, replace(_RACE_RECEIPT, unresolved_pids=(host[0],)))
    cleanup = stop_process_session(store, written)
    assert cleanup.confirmed is False and cleanup.record["status"] == "unknown", "不是 host 写的终态，不按竞态收敛"
    assert [item.method for item in cleanup.terminations] == ["identity_changed"]
