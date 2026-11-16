"""计划重启 fast 策略：有界等待不扩大重放与强制接班权限。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.gateway_parts import restart_service as service
from agent_py_agent.cli.gateway_process import _join_gateway_loops


@pytest.fixture(autouse=True)
def reset_gate():
    service.reset_restart_drain_state()
    yield
    service.reset_restart_drain_state()


def drain(**kwargs):
    args = dict(active_turns=lambda: 1, turn_wait_seconds=30,
                drain_timeout_seconds=0, mode="fast", fast_timeout_seconds=.03)
    args.update(kwargs)
    return service.run_restart_drain({"request_id": "original", "operation": "restart"}, **args)


def test_fast_parked_model_or_readonly_turn_does_not_wait_for_whole_turn():
    started = time.monotonic()
    result = drain()
    assert time.monotonic() - started < 1
    assert result == {"ok": True, "request_id": "original", "active_turns_at_exit": 1,
                      "restart_mode": "fast"}
    assert restart_gate.tool_gate_closed()


def test_fast_unknown_write_never_inherits_legacy_force():
    entered, release = threading.Event(), threading.Event()
    def write():
        with restart_gate.tool_execution_admission({"request_id": "original", "tool": "write"}):
            entered.set()
            release.wait(3)
    worker = threading.Thread(target=write)
    worker.start()
    try:
        assert entered.wait(1)
        result = drain(force_on_drain_timeout=True)
        assert not result["ok"]
        assert result["side_effect_state"] == "unknown"
        assert result["error_code"] == "GATEWAY_RESTART_WRITES_UNSETTLED"
        assert result["executing_tools"][0]["request_id"] == "original"
        assert not restart_gate.tool_gate_closed()
    finally:
        release.set()
        worker.join(3)


@pytest.mark.parametrize("operation", ["restart", "stop_for_update"])
def test_graceful_zero_remains_unlimited(monkeypatch, operation):
    observed = []
    monkeypatch.setattr(service, "wait_until_no_executing_tools", lambda timeout: observed.append(timeout) or True)
    result = service.run_restart_drain({"request_id": "original", "operation": operation},
        active_turns=lambda: 0, turn_wait_seconds=0, drain_timeout_seconds=0,
        mode="fast" if operation == "stop_for_update" else "graceful", fast_timeout_seconds=.03)
    assert result["ok"] and "restart_mode" not in result
    assert observed == [None]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_fast_policy_does_not_touch_gate(timeout):
    result = drain(fast_timeout_seconds=timeout)
    assert result["side_effect_state"] == "not_started"
    assert result["error_code"] == "GATEWAY_RESTART_POLICY_INVALID"
    assert service.restart_phase()["phase"] == "idle"
    assert not restart_gate.tool_gate_closed()


def test_user_stop_wins_fast():
    result = drain(stop_requested=lambda: True)
    assert result["reason"] == "stop_requested"
    assert not restart_gate.tool_gate_closed()


def test_cleanup_loops_share_one_deadline(monkeypatch):
    clock, waits = [10.0], []
    class Thread:
        ident = 1
        def join(self, timeout):
            waits.append(timeout)
            clock[0] += timeout
        def is_alive(self):
            return True
    monkeypatch.setattr("agent_py_agent.cli.gateway_process.time.monotonic", lambda: clock[0])
    request = SimpleNamespace(heartbeat_thread=Thread(), request_thread=Thread(), background_thread=Thread())
    assert _join_gateway_loops(request) == ["heartbeat", "request", "background"]
    assert waits == [2.0, 0.0, 0.0]


def test_fast_child_process_exits_with_parked_readonly_turn(tmp_path):
    # 使用真实进程验证原 daemon 请求池不把只读停顿变成 Python 退出 join；不启动正式 Gateway。
    script = '''
import json, threading, time
from agent_py_agent.agent.concurrency import DurableDaemonThreadPoolExecutor
from agent_py_agent.agent.gateway_parts.restart_service import run_restart_drain
pool = DurableDaemonThreadPoolExecutor(max_workers=1)
started = threading.Event()
def readonly():
    started.set()
    threading.Event().wait(60)
pool.submit(readonly)
assert started.wait(1)
t0=time.monotonic()
r=run_restart_drain({'request_id':'original'}, active_turns=lambda:1,
 turn_wait_seconds=30, drain_timeout_seconds=0, mode='fast', fast_timeout_seconds=.03)
pool.shutdown(wait=False)
print(json.dumps({'result':r,'drain_seconds':time.monotonic()-t0}),flush=True)
'''
    env = {**os.environ, "HOME": str(tmp_path), "MY_AGENT_HOME": str(tmp_path / "home")}
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", script], env=env, text=True,
                          capture_output=True, timeout=8, check=True)
    payload = json.loads(proc.stdout)
    assert payload["result"]["restart_mode"] == "fast"
    assert payload["drain_seconds"] < 1
    assert time.monotonic() - started < 8


@pytest.mark.parametrize("blocker,allowed", [
    ({"reason": "model_call_retained", "ref": "original-model"}, True),
    ({"reason": "bounded_call_retained", "ref": "original-model", "purpose": "model"}, True),
    ({"reason": "bounded_call_retained", "ref": "original-write", "purpose": "unknown"}, False),
    ({"reason": "background_operation_unsettled", "ref": "original-claim"}, False),
])
def test_handoff_preserves_model_refs_and_rejects_unknown_operations(tmp_path, monkeypatch, blocker, allowed):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import gateway_restart_handover as handover

    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir()
    identity = {"pid": os.getpid(), "instance_id": "original"}
    context = SimpleNamespace(paths=paths, process_identity=identity, agent=SimpleNamespace())
    write_json_file_atomic(paths.state, {"pid": os.getpid(), "process_identity": identity, "started_at": 1.0})
    marker = service.mark_restart_drained(paths, {"request_id": "original", "restart_mode": "fast"},
        old_pid=os.getpid(), old_process_started_at=1.0, active_turns_at_exit=1,
        old_process_identity=identity, cleanup_result={"status": "pending"})
    monkeypatch.setattr(handover, "log_gateway_event", lambda *args: None)
    monkeypatch.setattr(handover, "append_restart_cancellation", lambda *args, **kwargs: {"notification_results": []})
    foreground = {"reason": "foreground_turn_unsettled", "ref": "gateway_admission", "count": 1}
    result = handover.finalize_restart_cleanup(context, marker, {
        "drain_complete": False, "alive_threads": [], "background_blockers": [blocker],
        "unresolved_operation_refs": [foreground],
    })
    assert result["handover_allowed"] is allowed
    assert service.consume_restart_marker(paths, old_pid_alive=lambda pid: True) is None
    if allowed:
        assert result["status"] == "safe_model_handoff"
        assert blocker in result["continuation_refs"]
        assert result["predecessor_exit_required"]
        successor = service.consume_restart_marker(paths, old_pid_alive=lambda pid: False)
        assert successor["cleanup_result"]["continuation_refs"] == result["continuation_refs"]
    else:
        assert blocker in result["unresolved_operation_refs"]
        assert service.consume_restart_marker(paths, old_pid_alive=lambda pid: False) is None


def test_bounded_call_purpose_defaults_unknown_and_cannot_be_reassigned():
    from agent_py_agent.agent.backends.bounded_call import _Call
    from agent_py_agent.agent.concurrency.interrupt import InterruptHandle

    original = _Call("original", InterruptHandle())
    assert original.purpose == "unknown"
    with pytest.raises(AttributeError):
        original.purpose = "model"


def test_real_model_worker_marker_requires_predecessor_death(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive

    script = '''
import json, os, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from agent_py_agent.agent.backends.bounded_call import call_with_deadline, BoundedCallTimeoutError
from agent_py_agent.agent.conversation.background_shutdown import model_shutdown_blockers
from agent_py_agent.agent.gateway_parts import restart_service as service
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_restart_handover as handover
paths=gateway_paths_from_root(Path(sys.argv[1]));paths.root.mkdir()
try:
 call_with_deadline(lambda:threading.Event().wait(60),deadline=time.monotonic()+.03,
                    resource_key='original-model',purpose='model')
except BoundedCallTimeoutError:
 pass
identity={'pid':os.getpid(),'instance_id':'original'}
write_json_file_atomic(paths.state,{'pid':os.getpid(),'process_identity':identity,'started_at':1.0})
context=SimpleNamespace(paths=paths,process_identity=identity,agent=SimpleNamespace())
t0=time.monotonic()
r=service.run_restart_drain({'request_id':'original'},active_turns=lambda:1,
 turn_wait_seconds=30,drain_timeout_seconds=0,mode='fast',fast_timeout_seconds=.03)
marker=service.mark_restart_drained(paths,{'request_id':'original','restart_mode':r['restart_mode']},
 old_pid=os.getpid(),old_process_started_at=1.0,active_turns_at_exit=1,
 old_process_identity=identity,cleanup_result={'status':'pending'})
handover.log_gateway_event=lambda *a:None
result=handover.finalize_restart_cleanup(context,marker,{'drain_complete':False,'alive_threads':[],
 'background_blockers':model_shutdown_blockers(),
 'unresolved_operation_refs':[{'reason':'foreground_turn_unsettled','ref':'gateway_admission','count':1}]})
Path(sys.argv[2]).write_text(json.dumps({'handover_allowed':result['handover_allowed'],'seconds':time.monotonic()-t0}))
sys.stdin.readline()
'''
    env = {**os.environ, "HOME": str(tmp_path), "MY_AGENT_HOME": str(tmp_path / "home")}
    ready = tmp_path / "ready.json"
    proc = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "gateway"), str(ready)], env=env,
                            text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 45
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        if not ready.exists() and proc.poll() is not None:
            pytest.fail(proc.communicate(timeout=3)[1])
        assert ready.exists()
        payload = json.loads(ready.read_text())
        assert payload["handover_allowed"] and payload["seconds"] < 1
        paths = gateway_paths_from_root(tmp_path / "gateway")
        assert service.consume_restart_marker(paths, old_pid_alive=is_pid_alive) is None
        proc.communicate("exit\n", timeout=8)
        assert proc.returncode == 0
        marker = service.consume_restart_marker(paths, old_pid_alive=is_pid_alive)
        assert marker["request_id"] == "original"
        assert marker["cleanup_result"]["status"] == "safe_model_handoff"
        assert any(row.get("purpose") == "model" for row in marker["cleanup_result"]["continuation_refs"])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=3)
