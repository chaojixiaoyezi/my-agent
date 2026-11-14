"""#54 独占 runner 停止合同；PID、信号与时钟全为替身，不操作真实宿主。"""
from __future__ import annotations

import inspect
import signal
import subprocess
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.subagents import cancellation
from agent_py_agent.agent.subagents import cancellation_hosts as hosts


@pytest.fixture
def runner(monkeypatch):
    """冻结假身份，记录核验/信号/等待，禁止探测或触碰真实 PID。"""
    state = SimpleNamespace(pid=99999, birth="birth", alive=True, clock=0.0, events=[], on_sleep=None, on_kill=None)

    def alive(pid):
        assert pid == state.pid
        return state.alive

    def capture(pid):
        assert pid == state.pid
        state.events.append(("birth", state.birth))
        return state.birth

    def matches(pid, expected, current):
        assert pid == state.pid and expected == "birth"
        state.events.append(("verify", current))
        return current == expected

    def kill(pid, signum):
        assert pid == state.pid
        # 最后一个事件必须是本次 birth 核验，不可复用宽限期前的证明。
        assert state.events[-1] == ("verify", "birth")
        state.events.append(("signal", signum, state.clock))
        if state.on_kill:
            state.on_kill(signum)

    def sleep(seconds):
        assert seconds > 0
        state.clock += seconds
        state.events.append(("sleep", seconds))
        if state.on_sleep:
            state.on_sleep()

    state.sleep = sleep
    state.now = lambda: state.clock
    monkeypatch.setattr(hosts, "is_pid_alive", alive)
    monkeypatch.setattr(hosts, "capture_process_birth_token", capture)
    monkeypatch.setattr(hosts, "_birth_token_matches", matches)
    monkeypatch.setattr("os.kill", kill)
    return state


def _stop(*, status="cooperative_requested", pid=99999, birth="birth"):
    return hosts.FrozenRunnerStop({"status": status, "pid": pid}, pid, birth)


def _escalate(runner, stop=None, **kwargs):
    return hosts.escalate_exclusive_runner_stop(stop or _stop(), sleep=runner.sleep, now=runner.now, **kwargs)


def test_running_worker_gets_original_relay_tick_before_host_cleanup(runner):
    runner.on_sleep = lambda: setattr(runner, 'alive', False) if runner.clock >= 5.0 else None
    stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': runner.pid},
                                 runner.pid, 'birth', 5.0, True)
    result = _escalate(runner, stop)
    assert result['host_exited'] is True
    assert result['signals'] == []
    assert 5.0 <= runner.clock <= 5.5


def test_unconfirmed_foreground_cleanup_never_orphans_process_group(runner):
    stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': runner.pid},
                                 runner.pid, 'birth', 1000.0, True)
    result = _escalate(runner, stop)
    assert result['host_exited'] is False
    assert result['cleanup_state'] == 'unconfirmed'
    assert result['reason_code'] == 'RUNNER_TOOL_EXIT_UNCONFIRMED'
    assert result['signals'] == []
    assert runner.clock <= 10.1


def test_freeze_preserves_relay_interval_and_does_not_authorize_orphaning(monkeypatch):
    session = {'in_process': False, 'worker_pid': 1234, 'worker_birth_token': 'original',
               'launch_id': 'launch-1', 'attempt_id': 'attempt-1', 'interval_seconds': 5.0}
    task = SimpleNamespace(id='run-1', attributes={'background_start': {
        'launch_id': 'launch-1', 'attempt_id': 'attempt-1', 'pid': 1234}})
    monkeypatch.setattr(hosts, 'has_fresh_runner_session', lambda _: True)
    monkeypatch.setattr(hosts, 'runner_session_of', lambda _: session)
    stop = hosts.freeze_runner_stop(task, [task], {'run-1'}, kill_process=True)
    assert stop.pid == 1234 and stop.birth_token == 'original'
    assert stop.relay_interval_seconds == 5.0
    assert stop.cleanup_unconfirmed is True


def test_stale_cancelled_runner_retains_original_identity_for_recheck(monkeypatch):
    session = {'in_process': False, 'worker_pid': 1234, 'worker_birth_token': 'original',
               'launch_id': 'launch-1', 'attempt_id': 'attempt-1', 'interval_seconds': 5.0,
               'run_id': 'run-1', 'session_id': 'session-1', 'status': 'running'}
    task = SimpleNamespace(id='run-1', runner_active_attempt_id='', attributes={
        'background_start': {'launch_id': 'launch-1', 'attempt_id': 'attempt-1', 'pid': 1234},
        'runner_session': session,
        'cancel_subagents': {'stop_receipt': {'schema': 'runner-stop.v1',
                                             'run_id': 'run-1', 'attempt_id': 'attempt-1'}}})
    monkeypatch.setattr(hosts, 'has_fresh_runner_session', lambda _: False)
    stop = hosts.freeze_runner_stop(task, [task], {'run-1'}, kill_process=True)
    assert stop.pid == 1234 and stop.birth_token == 'original'
    assert stop.cleanup_unconfirmed is True
    task.runner_active_attempt_id = 'attempt-next'
    assert hosts.freeze_runner_stop(task, [task], {'run-1'}, kill_process=True).report['status'] == 'no_pid'


def test_worker_exit_without_tool_receipt_is_not_process_cleanup(runner):
    runner.alive = False
    stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': runner.pid},
                                 runner.pid, 'birth', 5.0, True)
    result = cancellation.cleanup_subagent_stops(_batch(stop))
    report = result['cancelled'][0]['pid_report']
    assert report['host_exited'] is True and report['tools_exit_confirmed'] is False
    assert result['ok'] is False
    assert result['failed'][0]['error_code'] == 'RUNNER_TOOL_EXIT_UNCONFIRMED'
    assert not any(event[0] == 'signal' for event in runner.events)


def test_original_worker_final_receipt_confirms_cleanup_but_reused_birth_does_not(tmp_path, runner):
    import json

    runner.alive = False
    path = tmp_path / 'canonical_state.json'
    payload = {'id': 'run-123', 'runner_active_attempt_id': '', 'attributes': {
        'cancel_subagents': {'stop_receipt': {'schema': 'runner-stop.v1', 'run_id': 'run-123', 'attempt_id': 'a1'}},
        'runner_session': {'run_id': 'run-123', 'attempt_id': 'a1', 'session_id': 's1',
                           'worker_pid': runner.pid, 'worker_birth_token': 'birth',
                           'status': 'failed', 'ended_at': 10.0}}}
    path.write_text(json.dumps(payload))
    stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': runner.pid},
                                 runner.pid, 'birth', 5.0, True, str(path), 'run-123', 'a1', 's1')
    assert cancellation.cleanup_subagent_stops(_batch(stop))['ok'] is True
    payload['attributes']['runner_session']['worker_birth_token'] = 'reused'
    path.write_text(json.dumps(payload))
    assert cancellation.cleanup_subagent_stops(_batch(stop))['ok'] is False
    path.write_text('{broken')
    result = cancellation.cleanup_subagent_stops(_batch(stop))
    proof = result['cancelled'][0]['pid_report']
    assert proof['proof_error_type'] == 'JSONDecodeError'
    assert proof['proof_reason'] == 'ORIGINAL_SESSION_EXIT_PROOF_UNREADABLE'
    assert proof['proof_next_action']


def test_abnormal_real_worker_exit_keeps_live_command_unconfirmed():
    import sys
    import time

    code = ("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)'], "
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); "
            "print(p.pid,flush=True); time.sleep(.3)")
    with subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True) as worker:
        command_pid = int(worker.stdout.readline())
        birth = hosts.capture_process_birth_token(worker.pid)
        assert birth
        worker.wait(timeout=3)
        stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': worker.pid},
                                     worker.pid, birth, 5.0, True)
        result = cancellation.cleanup_subagent_stops(_batch(stop))
        assert hosts.is_pid_alive(command_pid)
        assert result['ok'] is False
        assert result['cancelled'][0]['pid_report']['host_exited'] is True
        assert result['cancelled'][0]['pid_report']['tools_exit_confirmed'] is False
        # 不给无退出证明的命令发信号；该测试命令自行结束，不留下活跃孤儿。
        deadline = time.monotonic() + 4
        while hosts.is_pid_alive(command_pid) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not hosts.is_pid_alive(command_pid)


def test_idle_handler_proof_uses_exact_attempt_without_requiring_logs(tmp_path):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / 'runtime.db')
    first = repo.record_run_creation(owner_id='local/main', run_id='first', goal='执行')
    other = repo.record_run_creation(owner_id='local/main', run_id='other', goal='独立空闲轮')
    manager = SimpleNamespace(runtime_db=repo)
    assert cancellation._runner_never_started_handler(manager, first)
    op = repo.create_tool_operation(agent_run_id=first['agent_run_id'], attempt_id=first['attempt_id'],
                                    operation_type='run_command')
    assert cancellation._runner_never_started_handler(manager, first)
    repo.mark_operation_executing(op['operation_id'])
    assert not cancellation._runner_never_started_handler(manager, first)
    assert cancellation._runner_never_started_handler(manager, other)
    assert not cancellation._runner_never_started_handler(manager, {'attempt_id': 'unknown'})


def _batch(stop):
    return cancellation.FrozenSubagentStops((cancellation.FrozenSubagentStop(
        {"run_id": "run-123", "thread_interrupt": "attempt_signaled"}, stop,
    ),))


def _fast_cleanup(monkeypatch, runner, batch):
    """走真实 cleanup→升级主链路，只替换升级器的时间依赖。"""
    def escalate(stop):
        return _escalate(runner, stop, grace_seconds=0.2, kill_grace_seconds=0.3)

    monkeypatch.setattr(cancellation, "escalate_exclusive_runner_stop", escalate, raising=False)
    return cancellation.cleanup_subagent_stops(batch)


def test_issue_repro_live_exclusive_worker_cannot_report_ok(monkeypatch, runner):
    batch = _batch(_stop())
    assert batch.unconfirmed is False
    result = _fast_cleanup(monkeypatch, runner, batch)
    assert result["ok"] is False
    assert result["failed"] == [{
        "run_id": "run-123", "cancellation_attempted": True,
        "error_code": "RUNNER_WORKER_STILL_ALIVE",
    }]
    assert result["cancelled"][0]["pid_report"]["host_exited"] is False
    assert batch.unconfirmed is False


@pytest.mark.parametrize("exit_signal", [signal.SIGTERM, signal.SIGKILL])
def test_escalation_signals_waits_and_confirms_exit(runner, exit_signal):
    def on_kill(signum):
        if signum == exit_signal:
            # 信号返回不代表退出，后续等待中的存活观察才是证明。
            runner.on_sleep = lambda: setattr(runner, "alive", False)

    runner.on_kill = on_kill
    stop = _stop()
    result = _escalate(runner, stop, grace_seconds=0.2, kill_grace_seconds=0.3)
    expected = [signal.SIGTERM] if exit_signal == signal.SIGTERM else [signal.SIGTERM, signal.SIGKILL]
    assert result["signals"] == expected
    assert result["escalated"] is True and result["host_exited"] is True
    sends = [event for event in runner.events if event[0] == "signal"]
    assert sends[0][2] >= 0.2
    if exit_signal == signal.SIGKILL:
        assert sends[1][2] >= sends[0][2] + 0.2
    assert stop.report == {"status": "cooperative_requested", "pid": runner.pid}


def test_cooperative_exit_during_initial_grace_sends_nothing(runner):
    runner.on_sleep = lambda: setattr(runner, "alive", False)
    result = _escalate(runner)
    assert result["signals"] == [] and result["escalated"] is False
    assert result["host_exited"] is True


def test_still_alive_after_kill_is_not_exit(runner):
    result = _escalate(runner, grace_seconds=0.2, kill_grace_seconds=0.3)
    assert result["signals"] == [signal.SIGTERM, signal.SIGKILL]
    assert result["host_exited"] is False and result["identity_confirmed"] is True
    sends = [event for event in runner.events if event[0] == "signal"]
    # 和生产等待相同地比较绝对 deadline，避免 0.7-0.4 的浮点相消误差。
    assert runner.clock >= sends[-1][2] + 0.3


@pytest.mark.parametrize("status,pid,birth", [
    ("shared_host_cooperative", 99999, "birth"),
    ("identity_unavailable", 99999, "birth"),
    ("skipped", 99999, "birth"), ("no_pid", 0, ""),
    ("cooperative_requested", 0, "birth"), ("cooperative_requested", -1, "birth"),
    ("cooperative_requested", 99999, ""),
])
def test_shared_unrequested_or_unidentified_runner_never_signaled(runner, status, pid, birth):
    result = _escalate(runner, _stop(status=status, pid=pid, birth=birth))
    assert result["signals"] == [] and result["escalated"] is False
    assert not any(event[0] == "signal" for event in runner.events)


@pytest.mark.parametrize("birth,exited", [("different-birth", True), ("", False)])
def test_initial_birth_mismatch_or_unknown_never_signals(runner, birth, exited):
    runner.birth = birth
    result = _escalate(runner)
    assert result["signals"] == [] and result["host_exited"] is exited


@pytest.mark.parametrize("phase", ["before_term", "before_kill"])
@pytest.mark.parametrize("birth,exited", [("different-birth", True), ("", False)])
def test_birth_changes_during_wait_never_signals_reused_or_unknown_pid(runner, phase, birth, exited):
    def change_birth():
        if phase == "before_term" or any(event[0] == "signal" for event in runner.events):
            runner.birth = birth

    runner.on_sleep = change_birth
    result = _escalate(runner, grace_seconds=0.2, kill_grace_seconds=0.3)
    assert result["signals"] == ([] if phase == "before_term" else [signal.SIGTERM])
    assert result["host_exited"] is exited


@pytest.mark.parametrize("exception", [PermissionError("denied"), subprocess.TimeoutExpired("ps", 2), ValueError("bad birth")])
def test_unverifiable_birth_never_signals(monkeypatch, runner, exception):
    def unavailable(*args):
        raise exception

    monkeypatch.setattr(hosts, "_birth_token_matches", unavailable)
    result = _escalate(runner)
    assert result["signals"] == [] and result["host_exited"] is False
    assert result["identity_confirmed"] is False


@pytest.mark.parametrize("unknown", ["birth", "freeze"])
def test_cleanup_unknown_identity_reports_failure(monkeypatch, runner, unknown):
    stop = _stop()
    if unknown == "birth":
        runner.birth = ""
    else:
        stop = hosts.FrozenRunnerStop({"status": "identity_unavailable", "pid": runner.pid})
    batch = _batch(stop)
    result = _fast_cleanup(monkeypatch, runner, batch)
    assert result["ok"] is False
    assert result["failed"][0]["error_code"] == "RUNNER_WORKER_EXIT_UNCONFIRMED"
    assert batch.unconfirmed is False
    assert not any(event[0] == "signal" for event in runner.events)


def test_cleanup_loses_identity_after_term_is_unconfirmed_not_still_alive(monkeypatch, runner):
    runner.on_kill = lambda _: setattr(runner, "birth", "")
    result = _fast_cleanup(monkeypatch, runner, _batch(_stop()))
    assert result["ok"] is False
    assert result["failed"][0]["error_code"] == "RUNNER_WORKER_EXIT_UNCONFIRMED"
    assert result["cancelled"][0]["pid_report"]["identity_confirmed"] is False
    assert [event[1] for event in runner.events if event[0] == "signal"] == [signal.SIGTERM]


@pytest.mark.parametrize("mode", ["term_exit", "replaced", "shared", "no_pid", "skipped"])
def test_cleanup_confirmed_exit_and_protected_hosts_keep_success(monkeypatch, runner, mode):
    stop = _stop()
    if mode == "term_exit":
        runner.on_kill = lambda _: setattr(runner, "alive", False)
    elif mode == "replaced":
        runner.birth = "different-birth"
    else:
        status = "shared_host_cooperative" if mode == "shared" else mode
        stop = hosts.FrozenRunnerStop({"status": status, "pid": runner.pid})
    result = _fast_cleanup(monkeypatch, runner, _batch(stop))
    assert result["ok"] is True and result["failed"] == []
    if mode != "term_exit":
        assert not any(event[0] == "signal" for event in runner.events)


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("freeze_error,pty_error", [(False, False), (True, False), (False, True)])
def test_unconfirmed_preserves_frozen_resource_truth_table(failed, freeze_error, pty_error):
    resources = SimpleNamespace(background_freeze_error=freeze_error, pty_request_error=pty_error)
    stop = cancellation.FrozenSubagentStop({}, _stop(), resources)
    batch = cancellation.FrozenSubagentStops((stop,), ({"run_id": "bad"},) if failed else ())
    assert batch.unconfirmed is (failed or freeze_error or pty_error)


def test_cleanup_runner_stop_return_contract_is_unchanged(runner):
    stop = _stop()
    assert hosts.cleanup_runner_stop(stop) == {**stop.report, "host_exited": False, "identity_confirmed": True}
    assert runner.clock == 0 and not any(event[0] == "signal" for event in runner.events)


def test_escalation_signature_has_contract_defaults_and_keyword_only_options():
    params = inspect.signature(hosts.escalate_exclusive_runner_stop).parameters
    assert params["grace_seconds"].default == 1.5
    assert params["kill_grace_seconds"].default == 1.5
    assert all(params[name].kind == inspect.Parameter.KEYWORD_ONLY for name in (
        "grace_seconds", "kill_grace_seconds", "sleep", "now",
    ))


@pytest.mark.parametrize("capture_number,expected", [(2, []), (4, [signal.SIGTERM])])
def test_birth_rechecked_immediately_before_each_signal(monkeypatch, runner, capture_number, expected):
    calls = []

    def capture(pid):
        assert pid == runner.pid
        calls.append(pid)
        return "" if len(calls) == capture_number else "birth"

    monkeypatch.setattr(hosts, "capture_process_birth_token", capture)
    result = _escalate(runner, grace_seconds=0, kill_grace_seconds=0)
    assert result["signals"] == expected
    assert result["host_exited"] is False and result["identity_confirmed"] is False


@pytest.mark.parametrize("failure", [PermissionError("denied"), ProcessLookupError("gone")])
def test_signal_failure_must_reobserve_not_claim_exit(monkeypatch, runner, failure):
    def kill(pid, signum):
        assert pid == runner.pid and signum == signal.SIGTERM
        runner.events.append(("failed_signal", signum))
        raise failure

    monkeypatch.setattr("os.kill", kill)
    result = _escalate(runner, grace_seconds=0, kill_grace_seconds=0)
    assert result["signals"] == [] and result["host_exited"] is False
    assert result["signal_error_type"] == type(failure).__name__
    assert runner.events[-1] == ("verify", "birth")


def test_cleanup_probe_exception_is_structured_exit_unconfirmed(monkeypatch, runner):
    def capture(pid):
        assert pid == runner.pid
        raise PermissionError("birth observation denied")

    monkeypatch.setattr(hosts, "capture_process_birth_token", capture)
    result = _fast_cleanup(monkeypatch, runner, _batch(_stop()))
    assert result["ok"] is False
    assert result["failed"][0]["error_code"] == "RUNNER_WORKER_EXIT_UNCONFIRMED"
    assert result["cancelled"][0]["pid_report"]["identity_confirmed"] is False
    assert not any(event[0] == "signal" for event in runner.events)
