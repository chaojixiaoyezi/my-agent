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
