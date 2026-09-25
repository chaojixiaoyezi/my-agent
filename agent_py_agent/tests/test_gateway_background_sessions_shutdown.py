# LLM: 验证 Gateway 停机时把仍存活的受管后台会话记成结构化事实（事件 + state 计数 + status 投影），不停止任何进程；
#   改 _cmd_gateway_run_cleanup 顺序、事件名或字段时同步这里与 test_gateway_model_call_shutdown_settlement.py。
# 模块用途: 后台进程按设计跨 Gateway 存活，用户"关掉程序"后要能看见还有哪些在跑。
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import decision_policy
from agent_py_agent.agent.gateway_parts import background_sessions
from agent_py_agent.agent.gateway_parts.background_sessions import surviving_background_sessions
from agent_py_agent.agent.tooling.process_registry import capture_process_birth_token
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.cli import gateway_process


# 函数用途: 构造一条 v3 受管会话记录；实例身份指向给定 PID（活着或已消失）。
def _record(session_id: str, pid: int, token: str, *, status: str, owner_home: str) -> dict:
    now = time.time()
    scope = {"owner_id": "local/main", "owner_home": owner_home, "plugin_id": "sample-peek", "activation_id": "a" * 64}
    return {
        "schema": "managed_process_session.v3", "session_id": session_id,
        "access_scope": {"owner_id": "local/main", "conversation_id": "", "owner_home": owner_home},
        "execution_scope": {"owner_home": owner_home, "thread_id": "", "root_task_id": "", "run_id": "", "attempt_id": ""},
        "activation_scope": scope, "completion_target": {},
        "launcher_pid": os.getpid(), "launcher_birth_token": capture_process_birth_token(os.getpid()) or "launcher",
        "pid": pid, "pid_birth_token": token, "child_pid": pid, "child_pid_birth_token": token,
        "revision": 0, "stop_requested": False, "handoff_confirmed": True, "child_launch_started": True,
        "reserved_at": now - 60, "started_at": now - 59, "status": status,
        "exit_code": 0 if status == "exited" else None, "finished_at": now - 1 if status == "exited" else None,
        "command": "python3 -m http.server 8080", "cwd": owner_home, "output_file": f"{owner_home}/out.log",
        "host_state_file": f"{owner_home}/host.json", "completion_notice_id": "",
    }


@pytest.fixture
def live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_facade_lists_only_non_terminal_sessions_with_listener_facts(tmp_path: Path, live_process):
    owner = tmp_path / "owners" / "local" / "main"
    owner.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store_root = process_session_store_root(workspace, str(owner))
    store = ProcessSessionStore(store_root)
    live_token = capture_process_birth_token(live_process.pid) or "birth"
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone_token = capture_process_birth_token(gone.pid) or "birth"
    gone.wait(timeout=10)
    store.write(_record("bg-live-server", live_process.pid, live_token, status="running", owner_home=str(owner)))
    store.write(_record("bg-finished", gone.pid, gone_token, status="exited", owner_home=str(owner)))
    agent = SimpleNamespace(tools=SimpleNamespace(workspace_root=workspace, owner_scope_root=str(owner)))

    rows = surviving_background_sessions(agent)

    assert [row["session_id"] for row in rows] == ["bg-live-server"]
    row = rows[0]
    assert row["status"] == "running" and row["command"].startswith("python3 -m http.server")
    assert set(row) == {"session_id", "status", "started_at", "uptime_seconds", "command", "lan_reachability", "listener_observation"}
    assert row["lan_reachability"] and row["listener_observation"], "监听事实只观测不伪造；不支持的平台也要给出观测码"
    assert store.load("bg-live-server").record["status"] == "running", "只读投影，不改记录"


def test_facade_without_registry_or_store_returns_nothing(tmp_path: Path):
    assert surviving_background_sessions(SimpleNamespace()) == []
    agent = SimpleNamespace(tools=SimpleNamespace(workspace_root=tmp_path / "ws", owner_scope_root=str(tmp_path / "owner")))
    assert surviving_background_sessions(agent) == []
    assert not (tmp_path / "owner").exists() and not (tmp_path / ".my-agent-runtime").exists(), "不创建权威目录"


# 函数用途: 构造一次 Gateway 收尾请求：替换文件/心跳/事件副作用与决策取消，只收集事件。
def _cleanup_request(monkeypatch, agent):
    events = []
    monkeypatch.setattr(gateway_process, "remove_pid_file_if_owned", lambda *_args: None)
    monkeypatch.setattr(gateway_process, "remove_gateway_stop_request_if_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_process, "_gateway_context_process_identity", lambda _context: "identity")
    monkeypatch.setattr(gateway_process, "_write_gateway_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_process, "log_gateway_event", lambda _agent, name, payload: events.append((name, payload)))
    monkeypatch.setattr(decision_policy, "cancel_active_decisions_for_shutdown", lambda: 0)
    context = SimpleNamespace(paths=SimpleNamespace(pid="pid", stop_request="stop"), agent=agent, process_started_at=0.0)
    request = SimpleNamespace(
        context=context, pid=7, stop_event=threading.Event(), heartbeat_thread=threading.Thread(target=None),
        request_thread=threading.Thread(target=None), background_thread=threading.Thread(target=None),
        http_server=None, termination_status="stopped", termination_reason="test",
    )
    return request, events


def test_cleanup_records_surviving_sessions_as_a_structured_event(monkeypatch):
    rows = [{"session_id": "bg-1", "status": "running", "lan_reachability": "unverified_external_probe_required"},
            {"session_id": "bg-2", "status": "unknown", "lan_reachability": "unknown"}]
    monkeypatch.setattr(background_sessions, "surviving_background_sessions", lambda _agent: list(rows))
    request, events = _cleanup_request(monkeypatch, SimpleNamespace())
    report = gateway_process._cmd_gateway_run_cleanup(request)
    assert report["surviving_background_sessions"] == 2
    assert [name for name, _payload in events] == ["gateway_background_sessions_surviving", "gateway_run_cleanup"]
    payload = events[0][1]
    assert (payload["count"], payload["pid"], payload["status"], payload["termination_status"]) == (2, 7, "cleanup", "stopped")
    assert payload["sessions"] == rows
    assert events[1][1]["surviving_background_sessions"] == 2


def test_cleanup_without_sessions_or_with_a_failing_scan_never_blocks(monkeypatch):
    request, events = _cleanup_request(monkeypatch, SimpleNamespace())
    assert gateway_process._cmd_gateway_run_cleanup(request)["surviving_background_sessions"] == 0
    assert [name for name, _payload in events] == ["gateway_run_cleanup"], "没有工具注册表的 agent 不产生事件"

    def broken(_agent):
        raise RuntimeError("secret path that must not be logged")

    monkeypatch.setattr(background_sessions, "surviving_background_sessions", broken)
    request, events = _cleanup_request(monkeypatch, SimpleNamespace())
    assert gateway_process._cmd_gateway_run_cleanup(request)["surviving_background_sessions"] == 0
    assert events[0] == ("gateway_background_sessions_scan_failed", {"error_type": "RuntimeError"})
    assert events[-1][0] == "gateway_run_cleanup"


def test_state_merge_keeps_termination_fields_and_adds_the_count(tmp_path: Path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"status": "stopped", "pid": 7, "termination_kind": "signal"}), encoding="utf-8")
    gateway_process._record_surviving_background_sessions_state(SimpleNamespace(state=state), {"surviving_background_sessions": 2})
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert (payload["status"], payload["pid"], payload["termination_kind"], payload["surviving_background_sessions"]) == ("stopped", 7, "signal", 2)
