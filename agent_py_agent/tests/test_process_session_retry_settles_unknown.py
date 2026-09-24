"""重试停止能结清实例已消失的旧 unknown 记录；首次停止对已消失实例仍不凭空确认。"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from agent_py_agent.agent.tooling.process_registry import capture_process_birth_token
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
