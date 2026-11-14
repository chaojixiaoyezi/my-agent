"""MP-00：真实文件锁交错、有界 HTTP 出生和原 closeout 的重试契约。"""
from __future__ import annotations

import ast
import gc
import socket
import threading
import time
import weakref
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.concurrency.lock_order import LockLevel
from agent_py_agent.agent.gateway_parts import multiprocess_http_transport as transport
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.agent.subagents import coordination
from agent_py_agent.tests.test_subagent_coordination import _finish, _start
from agent_py_agent.tests.test_v13_w4_capability_closeout_multiprocess import (
    _blocked,
    coordinated,
    coordinator_execution_scope,
    read_json_file,
    request_transaction_ports_scope,
)


class _RuntimeOwner:
    """SimpleNamespace 不支持弱引用/哈希；外壳保留真实控制器的对象身份语义。"""

    def __init__(self):
        self._mutex = threading.RLock()
        self._lease = SimpleNamespace(_lifecycle_gate=threading.RLock())


@pytest.fixture
def runtime_owner(monkeypatch):
    from agent_py_agent.agent.common import runtime_gate_registry as registry

    monkeypatch.setattr(registry, "_LIVE", weakref.WeakSet())
    owner = _RuntimeOwner()
    registry.register_runtime_gate(owner)
    return owner


@contextmanager
def _held_elsewhere(guard):
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with guard:
            acquired.set()
            assert release.wait(5)

    job = _start(hold)
    try:
        assert acquired.wait(2)
        yield release
    finally:
        release.set()
        _finish(job)


@pytest.mark.parametrize("gate_name", ["mutex", "lease"])
def test_creation_guard_busy_when_runtime_gate_held(tmp_path, runtime_owner, gate_name):
    gate = runtime_owner._mutex if gate_name == "mutex" else runtime_owner._lease._lifecycle_gate
    with _held_elsewhere(coordination.subagent_creation_guard(tmp_path)):
        started = time.monotonic()
        with gate, pytest.raises(coordination.SubagentCreationGuardBusy):
            with coordination.subagent_creation_guard(tmp_path):
                pytest.fail("争用 CG 不能进入临界区")
        assert time.monotonic() - started < 0.5

    # 未持 M/L 时仍是原阻塞行为，不把普通竞争改成失败。
    with _held_elsewhere(coordination.subagent_creation_guard(tmp_path)) as release:
        timer = threading.Timer(0.3, release.set)
        timer.start()
        try:
            started = time.monotonic()
            with coordination.subagent_creation_guard(tmp_path):
                assert time.monotonic() - started >= 0.2
        finally:
            timer.join(2)
            assert not timer.is_alive()


# LLM: CG首次取锁必须携带分类；M内同一物理CG的重入不能额外尝试取锁。
# 函数用途: 验证带类别的创建锁只取得一次。
def test_reentrant_guard_inside_runtime_gate_does_not_trylock(tmp_path, runtime_owner, monkeypatch):
    original = coordination.locked_json_path
    calls = []

    def record(path, **kwargs):
        calls.append(kwargs)
        return original(path, **kwargs)

    monkeypatch.setattr(coordination, "locked_json_path", record)
    with coordination.subagent_creation_guard(tmp_path), runtime_owner._mutex:
        with coordination.subagent_creation_guard(tmp_path):
            pass
    assert calls == [{"category": LockLevel.CG}]


def test_creation_guard_does_not_reclassify_body_blocking_io_error(tmp_path, runtime_owner):
    error = BlockingIOError("业务体失败不是 CG 竞争")
    with runtime_owner._mutex, pytest.raises(BlockingIOError) as caught:
        with coordination.subagent_creation_guard(tmp_path):
            raise error
    assert caught.value is error


def test_runtime_gate_registry_has_weak_owners_and_tolerates_missing_attributes(runtime_owner):
    from agent_py_agent.agent.common import runtime_gate_registry as registry

    other = _RuntimeOwner()
    registry.register_runtime_gate(other)
    with other._lease._lifecycle_gate:
        assert registry.current_thread_holds_runtime_gate()
    reference = weakref.ref(other)
    del other
    gc.collect()
    assert reference() is None
    assert not registry.current_thread_holds_runtime_gate()
    runtime_owner._lease = None
    with runtime_owner._mutex:
        assert registry.current_thread_holds_runtime_gate()
    del runtime_owner._mutex
    assert not registry.current_thread_holds_runtime_gate()


def test_abba_pair_completes(tmp_path, runtime_owner):
    b_has_cg = threading.Event()
    attempts = []

    def a():
        assert b_has_cg.wait(2)

        def attempt():
            with runtime_owner._mutex, runtime_owner._lease._lifecycle_gate:
                attempts.append(True)
                with coordination.subagent_creation_guard(tmp_path):
                    return "a completed"

        return request_worker._retry_on_creation_guard_busy(attempt)

    def b():
        with coordination.subagent_creation_guard(tmp_path):
            b_has_cg.set()
            time.sleep(0.2)
            with runtime_owner._mutex:
                return "b completed"

    b_job = _start(b)
    a_job = _start(a)
    assert _finish(b_job) == "b completed"
    assert _finish(a_job) == "a completed"
    assert len(attempts) > 1


def test_retry_creation_guard_busy_has_bounded_backoff_and_reraises(monkeypatch):
    from agent_py_agent.agent.subagents.coordination import SubagentCreationGuardBusy

    clock, delays = [0.0], []
    error = SubagentCreationGuardBusy("busy")

    def sleep(delay):
        assert 0 < delay <= 1.0
        delays.append(delay)
        clock[0] += delay

    def busy():
        raise error

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(SubagentCreationGuardBusy) as caught:
        request_worker._retry_on_creation_guard_busy(busy)
    assert caught.value is error
    assert delays[:6] == [0.05, 0.1, 0.2, 0.4, 0.8, 1.0]
    assert clock[0] == pytest.approx(30.0)


def test_close_claimed_parent_capabilities_retries_on_busy(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents import manager as manager_module

    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, request_id = _blocked(case.agent, claim.request_id)
            original_guard = manager_module.subagent_creation_guard
            original_write = request_worker.write_json_file_atomic
            attempts, first_proofs = [], []

            @contextmanager
            def busy_twice(workspace):
                attempts.append(True)
                if len(attempts) <= 2:
                    assert "capability_closeout" not in read_json_file(claim.path)
                    raise coordination.SubagentCreationGuardBusy(str(workspace))
                with original_guard(workspace):
                    yield

            def write(path, payload):
                if path == claim.path and "capability_closeout" not in read_json_file(path):
                    first_proofs.append(payload["capability_closeout"]["terminal_response"].copy())
                return original_write(path, payload)

            monkeypatch.setattr(manager_module, "subagent_creation_guard", busy_twice)
            monkeypatch.setattr(request_worker, "write_json_file_atomic", write)
            response = {"id": claim.request_id, "status": "done", "ok": True, "response": "原终答"}
            request_worker._close_claimed_parent_capabilities(case.paths, claim.path, claim.request_id,
                response, case.agent, claim.execution_attempt_id, claim.lease_epoch)
            request_worker._close_claimed_parent_capabilities(case.paths, claim.path, claim.request_id,
                response, case.agent, claim.execution_attempt_id, claim.lease_epoch)
        proof = read_json_file(claim.path)["capability_closeout"]
        assert first_proofs == [response]
        assert len(attempts) >= 4
        assert proof["acknowledged"] is True and proof["requests"] == {child: [request_id]}
        current = case.agent.subagents.load(child)
        assert current.status == "CANCELLED" and current.capability_requests[0].status == "CLOSED"


def _fake_runtime():
    return SimpleNamespace(controller=SimpleNamespace(_mutex=threading.RLock()),
        _lease_origin=SimpleNamespace(_lifecycle_gate=threading.RLock()), _gate=threading.RLock())


@pytest.mark.parametrize("gate_name", ["mutex", "lease", "runtime"])
def test_http_birth_timeout_returns_503_without_fault(monkeypatch, gate_name):
    monkeypatch.setattr(transport, "require_http_transport", lambda *args, **kwargs: None)
    runtime = _fake_runtime()
    gates = {"mutex": runtime.controller._mutex, "lease": runtime._lease_origin._lifecycle_gate,
             "runtime": runtime._gate}
    server = GatewayBoundedHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler, bind_and_activate=False)
    server._request_drain_runtime = server._request_drain_runtime_origin = runtime
    server.http_birth_gate_timeout_seconds = 0.05
    reject = Mock(wraps=server._reject_overloaded_request)
    failure = Mock()
    submit = Mock(side_effect=AssertionError("超时前不应 submit"))
    monkeypatch.setattr(server, "_reject_overloaded_request", reject)
    monkeypatch.setattr(server, "_request_worker_failure", failure)
    monkeypatch.setattr(server._request_executor, "submit", submit)
    request, peer = socket.socketpair()
    try:
        with _held_elsewhere(gates[gate_name]):
            started = time.monotonic()
            server.process_request(request, ("127.0.0.1", 0))
            assert time.monotonic() - started < 0.5
        peer.settimeout(1)
        response = peer.recv(4096)
        assert response.startswith(b"HTTP/1.1 503 ") and b"GATEWAY_HTTP_BUSY" in response
        reject.assert_called_once_with(request)
        failure.assert_not_called()
        submit.assert_not_called()
        assert not server._request_executor._threads
        assert server._request_slots._value == server.max_outstanding_requests
    finally:
        request.close()
        peer.close()
        server.server_close()


def test_original_http_birth_releases_partial_locks_on_timeout(monkeypatch):
    monkeypatch.setattr(transport, "require_http_transport", lambda *args, **kwargs: None)
    runtime = _fake_runtime()
    with _held_elsewhere(runtime._gate):
        with pytest.raises(transport.HTTPBirthGateBusy):
            with transport.original_http_birth(runtime, timeout_seconds=0.05):
                pytest.fail("G 被占用时不能出生")
        assert not runtime.controller._mutex._is_owned()
        assert not runtime._lease_origin._lifecycle_gate._is_owned()


def test_http_birth_runtime_gate_bound_includes_original_validation(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_for_root
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    with curator_case(tmp_path, monkeypatch) as (_agent, _service, _backend, gate, _claim, _close):
        control = coordinator_for_root(gate.paths.root, context=gate.paths).control_plane
        runtime = GatewayDrainRuntime(control)
        with _held_elsewhere(runtime._gate):
            started = time.monotonic()
            with pytest.raises(transport.HTTPBirthGateBusy):
                with transport.original_http_birth(runtime, timeout_seconds=0.05):
                    pytest.fail("原 require_original 的 G 不能先无界等待")
            assert time.monotonic() - started < 0.5
        assert not runtime.controller._mutex._is_owned()
        assert not runtime._lease_origin._lifecycle_gate._is_owned()


def test_http_birth_timed_path_revalidates_and_releases_on_body_error(monkeypatch):
    runtime = _fake_runtime()
    checks = []

    def validate(selected, **kwargs):
        assert selected is runtime
        assert all(gate._is_owned() for gate in (runtime.controller._mutex,
                   runtime._lease_origin._lifecycle_gate, runtime._gate))
        checks.append(kwargs)

    monkeypatch.setattr(transport, "require_http_transport", validate)
    error = ValueError("出生体失败")
    with pytest.raises(ValueError) as caught:
        with transport.original_http_birth(runtime, control="control", paths="paths", timeout_seconds=0.05):
            raise error
    assert caught.value is error
    assert checks == [{"control": "control", "paths": "paths"}]
    assert not any(gate._is_owned() for gate in (runtime.controller._mutex,
                   runtime._lease_origin._lifecycle_gate, runtime._gate))


def _assert_creation_guard_first(relative_path, function_name, protected):
    source = (Path(__file__).resolve().parents[1] / "agent" / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name == function_name]
    assert len(matches) == 1
    function = matches[0]
    # session_task_stop 保留原可选 guard()/nullcontext()；解析真实别名，不靠函数名白名单豁免。
    aliases = {target.id for node in ast.walk(function) if isinstance(node, ast.Assign)
               and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "getattr"
               and len(node.value.args) > 1 and isinstance(node.value.args[1], ast.Constant)
               and node.value.args[1].value == "creation_guard"
               for target in node.targets if isinstance(target, ast.Name)}

    def creation(expression):
        return any(isinstance(node, ast.Call) and ("creation_guard" in ast.unparse(node.func)
                   or isinstance(node.func, ast.Name) and node.func.id in aliases)
                   for node in ast.walk(expression))

    positions = []
    for node in sorted((node for node in ast.walk(function) if isinstance(node, ast.With)),
                       key=lambda node: node.lineno):
        for index, item in enumerate(node.items):
            text = ast.unparse(item.context_expr)
            if creation(item.context_expr):
                positions.append((node.lineno, index, "creation"))
            elif any(name in text for name in protected):
                positions.append((node.lineno, index, "protected"))
    assert positions and positions[0][2] == "creation", (function_name, positions)
    assert any(row[2] == "protected" for row in positions)

    # 不仅检查文本先后：每个受保护的 with 都必须实际处于外层 CG 的作用域。
    def check_scope(node, depth):
        if isinstance(node, ast.With):
            for item in node.items:
                if creation(item.context_expr):
                    depth += 1
                elif any(name in ast.unparse(item.context_expr) for name in protected):
                    assert depth, (function_name, node.lineno, ast.unparse(item.context_expr))
        for child in ast.iter_child_nodes(node):
            check_scope(child, depth)

    check_scope(function, 0)


@pytest.mark.parametrize("relative_path,function_name", [
    ("gateway_parts/control_service.py", "_stop_unpromoted_main"),
    ("gateway_parts/control_service.py", "_prepare_main_task_stop"),
    ("gateway_parts/session_task_stop.py", "stop_business_session_task"),
    ("scheduler/control.py", "cancel_scheduled_task"),
])
def test_stop_paths_take_creation_guard_first(relative_path, function_name):
    _assert_creation_guard_first(relative_path, function_name,
        ("transition_guard", "background_stop_guard", "locked_file_transition"))


@pytest.mark.parametrize("relative_path,function_name", [
    ("subagents/services/base.py", "create_run"),
    ("subagents/services/lifecycle_runner_attempts.py", "prepare_runner_attempt"),
    ("subagents/services/lifecycle_runner_attempts.py", "abandon_runner_attempt"),
])
def test_transaction_owners_take_creation_guard_first(relative_path, function_name):
    _assert_creation_guard_first(relative_path, function_name, ("gateway_short_transaction",))


@pytest.mark.parametrize("guard_available", [False, True])
def test_business_stop_optional_creation_guard_precedes_task_guard(monkeypatch, guard_available):
    from agent_py_agent.agent.gateway_parts import session_task_stop

    events = []
    stop = ValueError("已进入原 task guard，结束探针")

    @contextmanager
    def task_guard(_task_id):
        events.append("task")
        raise stop
        yield

    @contextmanager
    def creation_guard():
        events.append("creation")
        yield

    task = SimpleNamespace(target_thread_id="thread")
    subagents = SimpleNamespace()
    if guard_available:
        subagents.creation_guard = creation_guard
    agent = SimpleNamespace(subagents=subagents,
        conversation_store=SimpleNamespace(tasks=SimpleNamespace(transition_guard=task_guard)))
    monkeypatch.setattr(session_task_stop, "_business_stop_target",
        lambda *_args: (None, task, None, "root", None, None, False))
    with pytest.raises(ValueError) as caught:
        session_task_stop.stop_business_session_task(agent, task)
    assert caught.value is stop
    assert events == (["creation", "task"] if guard_available else ["task"])


def test_f1_stop_reads_binding_before_entering_goal_guard(monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service

    held, events = [], []
    stop = ValueError("已进入原 GG，结束锁序探针")
    linked, scope = object(), object()

    @contextmanager
    def creation_guard():
        held.append("CG")
        events.append("CG")
        try:
            yield
        finally:
            held.pop()

    def fresh_binding(record, selected_scope):
        assert record is linked and selected_scope is scope
        assert held == ["CG"]
        events.append("binding")
        return {"task_id": "task"}

    fresh = Mock(side_effect=fresh_binding)

    @contextmanager
    def goal_guard(thread_id):
        assert thread_id == "thread"
        fresh.assert_called_once_with(linked, scope)
        events.append("GG")
        raise stop
        yield

    agent = SimpleNamespace(subagents=SimpleNamespace(creation_guard=creation_guard),
        conversation_store=SimpleNamespace(goals=SimpleNamespace(transition_guard=goal_guard)))
    active = SimpleNamespace(payload={"conversation_thread_id": "thread"}, linked_request=linked)
    monkeypatch.setattr(control_service, "_fresh_stop_runtime_binding", fresh)
    with pytest.raises(ValueError) as caught:
        control_service._prepare_main_task_stop(agent, active, "task", scope)
    assert caught.value is stop
    assert events == ["CG", "binding", "GG"] and held == []


@pytest.mark.parametrize("closeout,expected", [
    ({"status": "blocked", "reason": "closeout_busy", "error_code": "OWNER_SCOPE_UNAVAILABLE"}, "blocked"),
    ({"status": "blocked"}, "blocked"),
    ({"status": "expired"}, "expired"),
    ({"status": "admitted"}, "raced"),
    ({"status": None}, "raced"),
    ({}, "raced"),
])
def test_f4_pending_closeout_short_circuits_before_owner_route(tmp_path, monkeypatch, closeout, expected):
    request = SimpleNamespace(path=tmp_path / "pending.json", payload={"id": "pending", "kind": "ask"})
    paths, limits = object(), request_worker.AdmissionLimits(2, 2)
    hook = Mock(return_value=closeout)
    gate = SimpleNamespace(try_close_unroutable_pending=hook)
    owner = Mock(side_effect=AssertionError("closeout 必须先于 owner 路由"))
    decision, claim, submit, wait = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(request_worker, "_durable_request_owner", owner)
    monkeypatch.setattr(request_worker, "_durable_admission_decision", decision)
    monkeypatch.setattr(request_worker, "claim_request", claim)
    monkeypatch.setattr(request_worker, "_record_admission_wait", wait)
    assert request_worker._admit_and_submit_durable(paths, request, (limits, submit, gate)) == expected
    hook.assert_called_once_with(request.path, snapshot=request.payload)
    assert hook.call_args.kwargs["snapshot"] is request.payload
    for downstream in (owner, decision, claim, submit):
        downstream.assert_not_called()
    if expected == "blocked":
        wait.assert_called_once_with(request,
            reason=str(closeout.get("reason") or "admission_blocked"),
            error_code=str(closeout.get("error_code") or ""), paths=paths, admission_gate=gate)
    else:
        wait.assert_not_called()


@pytest.mark.parametrize("hook_mode", ["absent", "none", "returns_none"])
def test_f4_absent_or_empty_closeout_preserves_original_admission(tmp_path, monkeypatch, hook_mode):
    request = SimpleNamespace(path=tmp_path / "pending.json", payload={"id": "pending", "kind": "ask"})
    paths, limits, owner_identity = object(), request_worker.AdmissionLimits(2, 2), object()
    gate, hook = SimpleNamespace(), Mock(return_value=None)
    if hook_mode != "absent":
        gate.try_close_unroutable_pending = None if hook_mode == "none" else hook
    owner = Mock(return_value=owner_identity)
    decision = Mock(return_value={"status": "expired"})
    wait, submit = Mock(), Mock()
    monkeypatch.setattr(request_worker, "_durable_request_owner", owner)
    monkeypatch.setattr(request_worker, "_durable_admission_decision", decision)
    monkeypatch.setattr(request_worker, "_record_admission_wait", wait)
    assert request_worker._admit_and_submit_durable(paths, request, (limits, submit, gate)) == "expired"
    owner.assert_called_once_with(gate, request.payload)
    decision.assert_called_once()
    assert decision.call_args.args[:3] == (paths, request, gate)
    assert decision.call_args.args[3][0] is owner_identity
    if hook_mode == "returns_none":
        hook.assert_called_once_with(request.path, snapshot=request.payload)
    else:
        hook.assert_not_called()
    wait.assert_not_called()
    submit.assert_not_called()


def test_f4_closeout_error_is_not_silently_admitted(tmp_path, monkeypatch):
    request = SimpleNamespace(path=tmp_path / "pending.json", payload={"id": "pending", "kind": "ask"})
    error = OSError("收口来源读取失败")
    hook = Mock(side_effect=error)
    gate = SimpleNamespace(try_close_unroutable_pending=hook)
    owner = Mock(side_effect=AssertionError("收口失败不能继续路由"))
    submit = Mock()
    monkeypatch.setattr(request_worker, "_durable_request_owner", owner)
    with pytest.raises(OSError) as caught:
        request_worker._admit_and_submit_durable(object(), request, (request_worker.AdmissionLimits(2, 2), submit, gate))
    assert caught.value is error
    hook.assert_called_once_with(request.path, snapshot=request.payload)
    owner.assert_not_called()
    submit.assert_not_called()
