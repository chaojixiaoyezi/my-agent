"""后台服务监听范围：默认只允许本机回环，开放局域网要结构化声明并由用户长期授权；host 按真实 socket 表回收越界服务。"""
from __future__ import annotations

import io
import json
import stat
import subprocess
import time
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
    operation_grant_key,
)
from agent_py_agent.agent.tooling import _filesystem_find as find_module
from agent_py_agent.agent.tooling import _filesystem_search as search_module
from agent_py_agent.agent.tooling import background_process_host as host
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling import listen_scope as listeners
from agent_py_agent.agent.tooling import process_registry as registry_module
from agent_py_agent.agent.tooling.background_process_host import _read_launch_spec
from agent_py_agent.agent.tooling.listen_scope import normalize_listen_scope, scope_violations
from agent_py_agent.agent.tooling.models import ApprovalPolicy, _validate_runtime_policy
from agent_py_agent.agent.tooling.process_registry import BackgroundProcess
from agent_py_agent.agent.tooling.process_session_cleanup import stop_process_session
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.agent.tooling.shell import ShellTool, _background_listen_scope
from agent_py_agent.agent.user_space.approval_mode import (
    autonomous_tool_decision,
    execute_approval_mode_operation,
)
from agent_py_agent.agent.user_space.operation_grants import (
    owner_operation_granted,
    record_owner_operation_grant,
)
from agent_py_agent.tests._managed_process_harness import managed_request
from agent_py_agent.tests._tool_runtime_harness import runtime_snapshot_for_tools

BIND_ANY = "import socket,time; s=socket.socket(); s.bind(('0.0.0.0',0)); s.listen(); time.sleep(60)"
BIND_LOOP = "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); time.sleep(60)"
GRANT_PARAMS = (("background_listen_scope", ("lan",)),)
LAN_KEY = "run_command:background_listen_scope=lan"


def _call(arguments: dict | None = None):
    return SimpleNamespace(call_id="call-1", tool_name="run_command", run_id="run-1", operation_id="op-1",
                           idempotency_key="idem-1", args_hash="sha256:args", turn_id="turn-1", arguments=arguments or {})


def _request(grant_key: str = ""):
    return build_tool_approval_request(_call(), request_id="req-1", round_number=1, call_index=0,
                                       description="run_command", grant_key=grant_key)


def _home(tmp_path):
    return SimpleNamespace(owner_tool_policy_json=tmp_path / "policy" / "tool_policy.json")


# 函数用途: 轮询会话记录直到条件成立或超时，避免固定 sleep。
def _wait_record(store: ProcessSessionStore, session_id: str, predicate, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    record = store.load(session_id).record
    while not predicate(record) and time.monotonic() < deadline:
        time.sleep(0.2)
        record = store.load(session_id).record
    return record


def _cleanup(hosted) -> None:
    store = ProcessSessionStore(hosted.store_root)
    current = store.load(hosted.record["session_id"]).record
    if current and current.get("status") in {"running", "starting", "unknown"}:
        stop_process_session(store, current, host_process=hosted.process)


def test_operation_grant_key_only_matches_declared_values():
    assert operation_grant_key("run_command", {"background_listen_scope": "lan"}, GRANT_PARAMS) == LAN_KEY
    assert operation_grant_key("run_command", {"background_listen_scope": " LAN "}, GRANT_PARAMS) == LAN_KEY
    assert operation_grant_key("run_command", {"background_listen_scope": "loopback"}, GRANT_PARAMS) == ""
    assert operation_grant_key("run_command", {"command": "ls"}, GRANT_PARAMS) == ""
    assert operation_grant_key("run_command", {"background_listen_scope": "lan"}, ()) == ""
    assert operation_grant_key("run_command", None, GRANT_PARAMS) == ""


def test_approval_request_adds_owner_option_only_with_a_grant_key():
    plain = _request()
    assert [option["decision"] for option in plain.options] == ["approved", "approved_session", "denied"]
    assert "grant_key" not in plain.binding
    granted = _request(LAN_KEY)
    assert granted.binding["grant_key"] == LAN_KEY
    assert [option["decision"] for option in granted.options] == ["approved", "approved_session", "approved_owner", "denied"]
    owner = next(option for option in granted.options if option["decision"] == "approved_owner")
    assert owner["id"] == "allow_owner" and "长期" in owner["label"]
    decision = ToolApprovalDecision(granted.permission_id, "approved_owner")
    assert decision.approved is True
    assert ToolApprovalDecision.from_mapping(decision.to_dict()).decision == "approved_owner"


def test_owner_grant_store_is_owner_policy_file_and_fail_closed(tmp_path):
    home = _home(tmp_path)
    assert owner_operation_granted(home, LAN_KEY) is False
    assert owner_operation_granted(SimpleNamespace(), LAN_KEY) is False, "没有策略路径的替身永远未授权"
    first = record_owner_operation_grant(home, LAN_KEY, source="test")
    assert owner_operation_granted(home, LAN_KEY) is True
    assert owner_operation_granted(home, "run_command:background_listen_scope=loopback") is False
    path = home.owner_tool_policy_json
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "tool-policy.v1" and payload.get("permission_mode", "ask") == "ask"
    assert payload["operation_grants"][LAN_KEY]["source"] == "test"
    again = record_owner_operation_grant(home, LAN_KEY, source="other")
    assert again == first, "重复记录不刷新原授权"
    with pytest.raises(ValueError):
        record_owner_operation_grant(home, "", source="test")


def test_autonomous_mode_does_not_release_an_ungranted_operation(tmp_path):
    home = _home(tmp_path)
    home.owner_tool_policy_json.parent.mkdir(parents=True)
    execute_approval_mode_operation(home, "set", "auto")
    tool = SimpleNamespace(runtime_policy=SimpleNamespace(approval_policy=ApprovalPolicy(owner_grant_parameters=GRANT_PARAMS)))
    agent = SimpleNamespace(tools=SimpleNamespace(tools={"run_command": tool}), home_paths=home)
    assert autonomous_tool_decision(agent, _request()).decision == "approved", "普通危险调用在自主模式照常放行"
    assert autonomous_tool_decision(agent, _request(LAN_KEY)) is None, "开放局域网没授权过：自主模式也要等用户"
    record_owner_operation_grant(home, LAN_KEY, source="test")
    assert autonomous_tool_decision(agent, _request(LAN_KEY)).decision == "approved"
    execute_approval_mode_operation(home, "set", "ask")
    assert autonomous_tool_decision(agent, _request(LAN_KEY)).decision == "approved", "长期授权过的类别在 ask 模式也不再询问"
    assert autonomous_tool_decision(agent, _request()) is None


def test_shell_declares_the_listen_scope_parameter_and_grant(tmp_path):
    tool = ShellTool(tmp_path)
    schema = tool.model_spec.input_schema["properties"]["background_listen_scope"]
    assert schema["enum"] == ["loopback", "lan"]
    assert tool.runtime_policy.approval_policy.owner_grant_parameters == GRANT_PARAMS
    runtime_snapshot_for_tools({tool.model_spec.name: tool}, run_id="run-scope")
    assert _background_listen_scope({}) == "loopback"
    assert _background_listen_scope({"background_listen_scope": "lan"}) == "lan"
    with pytest.raises(ValueError):
        _background_listen_scope({"background_listen_scope": "public"})
    bogus = replace(tool.runtime_policy, approval_policy=ApprovalPolicy(owner_grant_parameters=(("no_such_param", ("x",)),)))
    with pytest.raises(ValueError):
        _validate_runtime_policy(tool.model_spec, bogus)


def test_listen_scope_normalization_and_violation_rules():
    assert normalize_listen_scope(None) == "loopback" and normalize_listen_scope(" LAN ") == "lan"
    with pytest.raises(ValueError):
        normalize_listen_scope("public")
    rows = [{"host": "127.0.0.1", "port": 1, "scope": "loopback"}, {"host": "*", "port": 2, "scope": "non_loopback"}]
    assert scope_violations("loopback", rows) == [rows[1]]
    assert scope_violations("lan", rows) == []


def test_launch_request_and_spec_carry_the_listen_scope(tmp_path):
    request = managed_request(tmp_path, listen_scope=" LAN ", listen_scope_enforce=False)
    assert request.listen_scope == "lan" and request.listen_scope_enforce is False
    with pytest.raises(ValueError):
        managed_request(tmp_path / "bad", listen_scope="public")
    store = ProcessSessionStore(tmp_path / "authority")
    spec_dir = store.root / ".launches"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "bg-test.json"
    base = {"schema": launch.LAUNCH_SPEC_SCHEMA, "session_id": "bg-test", "command_argv": ["python"], "max_log_bytes": 10,
            "deadline_monotonic": 0, "stop_on_launcher_exit": False, "io_mode": "log", "activation": None}
    spec_path.write_text(json.dumps(base))
    with pytest.raises(ValueError):
        _read_launch_spec(store, "bg-test", spec_path)
    spec_path.write_text(json.dumps({**base, "listen_scope": "loopback", "listen_scope_enforce": True}))
    assert _read_launch_spec(store, "bg-test", spec_path)["listen_scope"] == "loopback"
    spec_path.write_text(json.dumps({**base, "listen_scope": "public", "listen_scope_enforce": True}))
    with pytest.raises(ValueError):
        _read_launch_spec(store, "bg-test", spec_path)


def test_host_recycles_a_loopback_declared_service_that_binds_all_interfaces(tmp_path):
    hosted = launch.start_background_process(managed_request(tmp_path, BIND_ANY, listen_scope="loopback"))
    try:
        store = ProcessSessionStore(hosted.store_root)
        record = _wait_record(store, hosted.record["session_id"], lambda r: r and r.get("status") not in {"running", "starting"})
        assert record["status"] == "killed" and record["reason"] == "listen_scope_violation"
        evidence = record["listener_violation"]
        assert evidence["enforced"] is True and evidence["listen_scope"] == "loopback"
        assert evidence["bindings"] and all(row["scope"] == "non_loopback" for row in evidence["bindings"])
        summary = BackgroundProcess.from_record(record, store_root=hosted.store_root).to_summary()
        assert summary["listen_scope"] == "loopback" and summary["reason"] == "listen_scope_violation"
        assert summary["listener_violation"]["bindings"][0]["port"] == evidence["bindings"][0]["port"]
    finally:
        _cleanup(hosted)


@pytest.mark.parametrize("code,scope", [(BIND_LOOP, "loopback"), (BIND_ANY, "lan")])
def test_allowed_listeners_keep_running(tmp_path, code, scope):
    hosted = launch.start_background_process(managed_request(tmp_path, code, listen_scope=scope))
    try:
        store = ProcessSessionStore(hosted.store_root)
        time.sleep(4.5)  # 覆盖两次监听核对
        record = store.load(hosted.record["session_id"]).record
        assert record["status"] == "running" and record["listen_scope"] == scope
        assert "listener_violation" not in record and "listener_warning" not in record
    finally:
        _cleanup(hosted)


def test_enforcement_off_only_records_a_warning(tmp_path):
    hosted = launch.start_background_process(managed_request(tmp_path, BIND_ANY, listen_scope="loopback", listen_scope_enforce=False))
    try:
        store = ProcessSessionStore(hosted.store_root)
        record = _wait_record(store, hosted.record["session_id"], lambda r: r and r.get("listener_warning"))
        assert record["status"] == "running"
        assert record["listener_warning"]["enforced"] is False and record["listener_warning"]["bindings"]
        assert "listener_violation" not in record
    finally:
        _cleanup(hosted)


@pytest.mark.parametrize("elapsed,expected", [(0, 2), (9.99, 2), (10, 10), (59.99, 10), (60, 30), (600, 30)])
@pytest.mark.parametrize("enforce", [True, False])
def test_listen_check_interval_keeps_enforcement_fast(elapsed, expected, enforce):
    assert hasattr(host, "_listen_check_interval"), "监听检查缺少独立的分级退避策略"
    assert host._listen_check_interval(elapsed, enforce=enforce) == (2 if enforce else expected)


# LLM: 运行原monitor和监听告警事务，仅替换时钟、内核观察和最终整树清理；直接child禁止poll/wait。
# 函数用途: 用虚拟时间观察长寿命监听策略，不启动服务或等待一分钟。
@pytest.fixture
def monitor_probe(monkeypatch):
    state = SimpleNamespace(now=0.0, duration=100.0, violation_at=float("inf"), checks=[], writes=[], record={"stop_requested": False})
    transaction = SimpleNamespace(load=lambda _sid: dict(state.record))

    def write(payload):
        state.writes.append(dict(payload))
        state.record.update(payload)
        return payload

    def observe(_pid):
        state.checks.append(state.now)
        rows = [{"host": "*", "port": 1234, "scope": "non_loopback", "listener_pids": [42]}]
        return (rows if state.now >= state.violation_at else []), "observed"

    transaction.write = write
    state.store = SimpleNamespace(transaction=lambda: nullcontext(transaction))
    state.child = SimpleNamespace(pid=42, poll=lambda: pytest.fail("不得提前reap业务组长"), wait=lambda: pytest.fail("不得提前wait业务组长"))
    monkeypatch.setattr(host, "time", SimpleNamespace(monotonic=lambda: state.now, time=lambda: state.now + 1000,
                                                    sleep=lambda seconds: setattr(state, "now", state.now + seconds)))
    monkeypatch.setattr(host, "_HostRecordProjection", lambda: SimpleNamespace(
        load=lambda *_: SimpleNamespace(load_error=None, record=dict(state.record))))
    monkeypatch.setattr(host, "_stop_reason", lambda *_args, **_kwargs: "test_done" if state.now >= state.duration else "")
    monkeypatch.setattr(host, "_direct_child_exited_unreaped", lambda _child: False)
    monkeypatch.setattr(host, "observe_tree_listeners", observe)
    monkeypatch.setattr(host, "terminate_process_tree", lambda *_args, **_kwargs:
                        registry_module.ProcessTerminationReceipt("fixture", True, 0, 1))
    return state


@pytest.mark.parametrize("preexisting", [True, False])
def test_monitor_warning_stops_all_further_listener_checks(monitor_probe, preexisting):
    state = monitor_probe
    state.violation_at = 0
    if preexisting:
        state.record["listener_warning"] = {"enforced": False}
    assert host._monitor_child(state.store, "bg-probe", state.child, "birth", 0,
                               host._MonitorPolicy(listen_scope_enforce=False)) == 0
    assert len(state.checks) == (0 if preexisting else 1)
    assert len(state.writes) == (1 if preexisting else 2), "只写一次告警和最终终态"


def test_monitor_warning_only_checks_back_off_without_a_violation(monitor_probe):
    state = monitor_probe
    host._monitor_child(state.store, "bg-probe", state.child, "birth", 0,
                        host._MonitorPolicy(listen_scope_enforce=False))
    assert state.checks[0] == 0 and state.checks[-1] >= 90
    for previous, current in zip(state.checks, state.checks[1:]):
        expected = 2 if previous < 10 else 10 if previous < 60 else 30
        assert expected <= current - previous <= expected + 0.251


def test_monitor_enforces_a_late_listener_even_after_an_old_warning(monitor_probe):
    state = monitor_probe
    state.violation_at = 61
    state.record["listener_warning"] = {"enforced": False}
    host._monitor_child(state.store, "bg-probe", state.child, "birth", 0, host._MonitorPolicy())
    assert 61 <= state.now <= 63.25
    assert state.record["reason"] == "listen_scope_violation"
    assert state.record["listener_violation"]["enforced"] is True
    assert all(current - previous <= 2.251 for previous, current in zip(state.checks, state.checks[1:]))


# LLM: 同时替换run身份哨兵才模拟原生缓存分支；普通monkeypatch测试另核旧kwargs，无真实ps/lsof。
# 函数用途: 给父子表TTL、跨root复用及观测参数提供可变的进程快照。
@pytest.fixture
def listener_probe(monkeypatch):
    state = SimpleNamespace(now=100.0, rows="1 0\n2 1\n3 2\n4 0\n", calls=[], failure=False)

    def run(argv, **kwargs):
        state.calls.append((argv, kwargs))
        if state.failure:
            raise OSError("probe unavailable")
        return SimpleNamespace(stdout=state.rows if argv[0] == "ps" else "p1\nn*:1234\n", returncode=0)

    monkeypatch.setattr(listeners.subprocess, "run", run)
    monkeypatch.setattr(listeners, "_STDLIB_SUBPROCESS_RUN", run, raising=False)
    monkeypatch.setattr(listeners, "_PS_LAST_RUN_OBJ", run, raising=False)
    monkeypatch.setattr(listeners, "_PS_CACHE", (0.0, {}, set()), raising=False)
    monkeypatch.setattr(listeners, "time", SimpleNamespace(monotonic=lambda: state.now), raising=False)
    monkeypatch.setattr(listeners.shutil, "which", lambda _name: "/usr/bin/lsof")
    return state


def test_listener_native_ps_cache_is_shared_for_250ms_and_invalidates_on_clock_rollback(listener_probe):
    state = listener_probe
    assert listeners._descendant_pids_posix(1) == {1, 2, 3}
    assert listeners._descendant_pids_posix(4) == {4}
    state.rows += "5 1\n"
    state.now = 100.249
    assert listeners._descendant_pids_posix(1) == {1, 2, 3}
    assert len(state.calls) == 1
    state.now = 100.25
    assert listeners._descendant_pids_posix(1) == {1, 2, 3, 5}
    assert len(state.calls) == 2
    state.now = 99
    assert listeners._descendant_pids_posix(1) == {1, 2, 3, 5}
    assert len(state.calls) == 3
    for _argv, kwargs in state.calls:
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert kwargs.get("env", {}).get("LC_ALL") == "C"


def test_listener_monkeypatched_run_ignores_cache_and_keeps_legacy_signature(listener_probe, monkeypatch):
    state = listener_probe
    monkeypatch.setattr(listeners, "_STDLIB_SUBPROCESS_RUN", object())
    assert listeners._descendant_pids_posix(1) == {1, 2, 3}
    state.rows += "5 1\n"
    assert listeners._descendant_pids_posix(1) == {1, 2, 3, 5}
    rows, observation = listeners._observe_via_lsof(1)
    assert rows[0]["scope"] == "non_loopback" and observation == "observed"
    assert len(state.calls) == 4
    assert all(set(kwargs) == {"capture_output", "text", "timeout", "check"} for _argv, kwargs in state.calls)


@pytest.mark.parametrize("probe_fails", [False, True])
def test_listener_restored_run_invalidates_native_snapshot_within_ttl(listener_probe, monkeypatch, probe_fails):
    state = listener_probe
    native_run = listeners.subprocess.run
    assert listeners._descendant_pids_posix(1) == {1, 2, 3}
    patched_calls = []

    def patched_run(argv, **kwargs):
        patched_calls.append((argv, kwargs))
        if probe_fails:
            raise OSError("patched probe unavailable")
        return SimpleNamespace(stdout="1 0\n9 1\n", returncode=0)

    state.now = 100.05
    monkeypatch.setattr(listeners.subprocess, "run", patched_run)
    assert listeners._descendant_pids_posix(1) == ({1} if probe_fails else {1, 9})
    state.rows += "5 1\n"
    state.now = 100.1
    monkeypatch.setattr(listeners.subprocess, "run", native_run)
    assert listeners._descendant_pids_posix(1) == {1, 2, 3, 5}
    assert len(state.calls) == 2 and len(patched_calls) == 1
    assert set(patched_calls[0][1]) == {"capture_output", "text", "timeout", "check"}
    assert listeners._descendant_pids_posix(1) == {1, 2, 3, 5}
    assert len(state.calls) == 2


def test_listener_native_lsof_closes_stdin_and_uses_c_locale(listener_probe):
    rows, observation = listeners._observe_via_lsof(1)
    assert rows[0]["listener_pids"] == [1] and observation == "observed"
    for _argv, kwargs in listener_probe.calls:
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert kwargs.get("env", {}).get("LC_ALL") == "C"


def test_listener_concurrent_native_cache_misses_share_one_ps(listener_probe, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(8)
    original_run = listeners.subprocess.run

    def slow_run(argv, **kwargs):
        time.sleep(0.03)
        return original_run(argv, **kwargs)

    def sample(_index):
        barrier.wait(timeout=3)
        return listeners._descendant_pids_posix(1)

    monkeypatch.setattr(listeners.subprocess, "run", slow_run)
    monkeypatch.setattr(listeners, "_STDLIB_SUBPROCESS_RUN", slow_run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(sample, range(8))) == [{1, 2, 3}] * 8
    assert len(listener_probe.calls) == 1


def test_listener_ps_failures_are_not_cached(listener_probe):
    state = listener_probe
    state.failure = True
    assert listeners._descendant_pids_posix(1) == {1}
    state.failure = False
    assert listeners._descendant_pids_posix(1) == {1, 2, 3}
    assert len(state.calls) == 2


@pytest.mark.parametrize("module,stop_name", [(search_module, "_stop_process"), (find_module, "_stop_find_process")])
@pytest.mark.parametrize("after_kill", [0, subprocess.TimeoutExpired("rg", 1), OSError("wait failed")])
def test_filesystem_stop_waits_after_kill_without_closing_reader_stdout(monkeypatch, module, stop_name, after_kill):
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("rg", 1), after_kill]
    monkeypatch.setattr(module, "signal", SimpleNamespace(SIGKILL=9))
    getattr(module, stop_name)(process)
    process.kill.assert_called_once_with()
    assert process.wait.call_args_list == [call(timeout=1), call(timeout=1)]
    process.stdout.close.assert_not_called()


@pytest.mark.parametrize("reader", ["hits", "counts", "find"])
@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_filesystem_reader_finally_reaps_and_closes_stdout(tmp_path, monkeypatch, reader, failure):
    module = find_module if reader == "find" else search_module
    process = Mock(returncode=None, stdout=io.StringIO("entry\n" if failure else ""))
    process.poll.side_effect = lambda: process.returncode
    process.terminate.side_effect = lambda: setattr(process, "returncode", -15)

    def wait(timeout):
        if process.returncode is None:
            process.returncode = 0
        return process.returncode

    def cancel():
        if failure:
            raise failure("reader interrupted")

    process.wait.side_effect = wait
    monkeypatch.setattr(module, "raise_if_cancelled", cancel)
    request = SimpleNamespace(offset=0, limit=1)
    tool = SimpleNamespace(resolve_path=lambda path: path, display_path=lambda path: path.name)
    function = {"hits": lambda: search_module._rg_hits_from_process(tool, process, request),
                "counts": lambda: search_module._rg_counts_from_process(tool, process, request),
                "find": lambda: find_module._read_rg_find_process(tool, tmp_path, request, process)}[reader]
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt, match="reader interrupted"):
            function()
    else:
        result = function()
        if failure:
            assert result is None
        elif reader == "find":
            assert result.results == [] and result.scan_complete
        else:
            assert result == ([] if reader == "hits" else {})
    assert process.stdout.closed
    assert process.returncode is not None
    assert process.terminate.call_count == (1 if failure else 0)


# LLM: 保持Popen的poll/wait边界可观察；Store只有只读枚举与裁剪替身，不发送信号或创建真实资源。
# 函数用途: 为host终态先于物理退出、缓存移除和句柄替换构造可控制的回收竞态。
@pytest.fixture
def reap_probe(tmp_path, monkeypatch):
    registry = registry_module.ProcessRegistry()
    process = Mock(returncode=None)
    process.poll.side_effect = lambda: process.returncode
    process.wait.side_effect = subprocess.TimeoutExpired("host", 0.05)
    payload = {"session_id": "bg-reap", "pid": 42, "started_at": 1.0,
               "status": "exited", "pid_birth_token": "host:old"}
    record = registry_module.BackgroundProcess.from_record(payload, store_root=tmp_path)
    record.process = process
    registry._processes[(str(tmp_path), record.session_id)] = record
    store = SimpleNamespace(list_records=lambda: ([], []), prune_finished=Mock())
    monkeypatch.setattr(registry_module, "ProcessSessionStore", lambda _root: store)
    return SimpleNamespace(registry=registry, process=process, record=record, store=store, root=tmp_path, payload=payload)


def test_registry_terminal_refresh_waits_for_owned_host(reap_probe):
    state = reap_probe

    def wait(timeout):
        state.process.returncode = 0
        return 0

    state.process.wait.side_effect = wait
    state.registry._refresh_locked(state.record)
    state.process.wait.assert_called_once_with(timeout=0.05)
    assert state.process.returncode == 0
    assert not state.registry._pending_reap
    state.process.terminate.assert_not_called()
    state.process.kill.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "unreadable", "refresh_error"])
def test_registry_authority_failures_keep_owned_host_for_reaping(reap_probe, monkeypatch, failure):
    state = reap_probe
    state.record.status = "running"
    state.store.load = lambda _sid: SimpleNamespace(record=None, load_error={"error_type": "unreadable"} if failure == "unreadable" else None)
    if failure == "refresh_error":
        state.record.persisted_snapshot = {"schema": next(iter(registry_module.MANAGED_PROCESS_SESSION_SCHEMAS))}
        state.store.transaction = Mock(side_effect=OSError("store unavailable"))
        monkeypatch.setattr(registry_module, "_process_instance_terminated", lambda *_: True)
    with pytest.raises(registry_module.ProcessSessionAuthorityError):
        if failure == "refresh_error":
            state.registry._refresh_locked(state.record)
        else:
            state.registry.get(state.record.session_id, store_root=state.root)
    assert not state.registry._processes
    assert getattr(state.registry, "_pending_reap", []) == [state.process]
    state.process.wait.assert_not_called()
    state.process.terminate.assert_not_called()
    state.process.kill.assert_not_called()


@pytest.mark.parametrize("removal", ["clear", "clear_live", "prune", "hydrate", "hydrate_live", "replace", "replace_live"])
def test_registry_retains_hosts_across_record_removal_until_next_operation(reap_probe, monkeypatch, removal):
    state = reap_probe
    if removal.endswith("live"):
        state.record.status = "running"
    if removal.startswith("clear"):
        state.registry.clear()
    elif removal == "prune":
        monkeypatch.setattr(registry_module, "_MAX_FINISHED_COUNT", 0)
        state.registry._prune_finished_locked(state.root)
    elif removal.startswith("hydrate"):
        assert state.registry._hydrate_store_locked(state.root) == []
    else:
        replacement = {**state.payload, "pid": 43, "pid_birth_token": "host:new"}
        assert state.registry._cache_payload(replacement, state.root).process is None
    assert getattr(state.registry, "_pending_reap", []) == [state.process]
    state.process.wait.assert_not_called()
    state.process.returncode = 0
    before = state.process.poll.call_count
    state.registry.list_report()
    assert state.process.poll.call_count > before
    assert not state.registry._pending_reap
    state.process.terminate.assert_not_called()
    state.process.kill.assert_not_called()


@pytest.mark.parametrize("operation", ["refresh", "prune", "list", "clear"])
def test_registry_operations_drain_completed_pending_hosts(reap_probe, operation):
    state = reap_probe
    done = Mock()
    done.poll.return_value = 0
    state.registry._pending_reap = [done, state.process]
    state.registry._processes.clear()
    if operation == "refresh":
        record = registry_module.BackgroundProcess("bg-empty", "", 0, 1, status="exited")
        state.registry._refresh_locked(record)
    elif operation == "prune":
        state.registry._prune_finished_locked(state.root)
    elif operation == "list":
        state.registry.list_report()
    else:
        state.registry.clear()
    assert state.registry._pending_reap == [state.process]
    done.poll.assert_called()
    state.process.wait.assert_not_called()


def test_registry_pending_reap_is_deduplicated_and_bounded(reap_probe):
    registry = reap_probe.registry
    assert hasattr(registry, "_reap_popen_or_retain_locked")
    registry._reap_popen_or_retain_locked(None)
    processes = [Mock() for _ in range(65)]
    for process in processes[:64]:
        process.poll.return_value = None
        registry._reap_popen_or_retain_locked(process, wait_seconds=0)
        registry._reap_popen_or_retain_locked(process, wait_seconds=0)
        process.wait.assert_not_called()
    processes[-1].poll.return_value = None
    with pytest.raises(RuntimeError, match="host reap capacity"):
        registry._reap_popen_or_retain_locked(processes[-1], wait_seconds=0)
    assert len(registry._pending_reap) == 64
    assert registry._pending_reap == processes[:64], "达到上限也不得逐出先前持有的句柄"


@pytest.mark.parametrize("operation", ["clear", "prune", "hydrate", "replace"])
def test_registry_full_reap_queue_keeps_record_until_capacity_returns(reap_probe, monkeypatch, operation):
    state = reap_probe
    pending = [Mock() for _ in range(64)]
    for process in pending:
        process.poll.return_value = None
    state.registry._pending_reap = pending.copy()
    key = (str(state.root), state.record.session_id)
    with pytest.raises(RuntimeError, match="host reap capacity"):
        if operation == "clear":
            state.registry.clear()
        elif operation == "prune":
            monkeypatch.setattr(registry_module, "_MAX_FINISHED_COUNT", 0)
            state.registry._prune_finished_locked(state.root)
        elif operation == "hydrate":
            state.registry._hydrate_store_locked(state.root)
        else:
            state.registry._cache_payload({**state.payload, "pid": 43}, state.root)
    assert state.registry._processes[key] is state.record
    assert state.record.process is state.process and state.record.pid == 42
    assert state.registry._pending_reap == pending
    state.process.wait.assert_not_called()
    state.process.kill.assert_not_called()
    state.process.terminate.assert_not_called()
    pending[0].poll.return_value = 0
    state.registry.clear()
    assert not state.registry._processes
    assert state.registry._pending_reap == [*pending[1:], state.process]


def test_registry_reaps_a_real_host_after_its_terminal_record_was_cleared(tmp_path):
    import os
    import sys

    registry = registry_module.ProcessRegistry()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.15)"],
                               cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record = registry_module.BackgroundProcess("bg-real-reap", "", process.pid, time.time(), process=process, status="exited")
    registry._processes[(str(tmp_path), record.session_id)] = record
    try:
        registry._refresh_locked(record)
        registry.clear()
        deadline = time.monotonic() + 3
        while process.returncode is None and time.monotonic() < deadline:
            registry.list_report()
            time.sleep(0.01)
        assert process.returncode == 0
        assert not registry._pending_reap
        if os.name == "posix":
            with pytest.raises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
    finally:
        process.kill()
        process.wait(timeout=2)
