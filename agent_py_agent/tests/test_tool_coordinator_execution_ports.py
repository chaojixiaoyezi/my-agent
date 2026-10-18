# LLM: 使用真实LocalStore/ToolExecutor和临时C claim；故障注入只产生实际typed异常，不能替代权限或伪造操作终态。
# 模块用途: 验证工具执行与原短写来源同寿命，未结和缺来源异常不被普通工具回执掩盖。
import json
import threading
import time
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import bounded_call as bounded
from agent_py_agent.agent.common import tool_write_context as transport
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
    current_coordinator_execution,
)
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.runtime_db.execution_mode import ExecutionMode
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeConflictError, RuntimeRepository
from agent_py_agent.agent.tooling import tool_operation_coordinator as coordinator
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome, ToolOperationReconciliation
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_tool_operation_idempotency import (
    _call,
    _CountingTool,
    _execute,
    _registry,
)


# LLM: 原tool request只是执行器本来接受的结构输入；store实际领取/代数/holder由生产API生成。
# 函数用途: 为故障矩阵构造稳定的一次工具操作，不造Gateway claim或权限。
def _request(store, invoke):
    return coordinator.ToolOperationExecutionRequest(
        store=store, store_required=True, owner_id="owner-a", run_id="ports-run", task_id="ports-task",
        operation_id="ports-op", tool_name="counting_write", args_hash=tool_arguments_hash({"value": 7}),
        idempotency_key="ports-key", idempotency_scope="operation", idempotency_namespace="counting_write",
        timeout_seconds=0, invoke=invoke,
    )


# LLM: 测试只读真实操作账本状态，不用异常标签或模型结果宣告未执行/已结算。
# 函数用途: 读取同一个原工具记录，供每个异常出口核对持久责任。
def _record(store):
    return store.get_tool_operation(owner_id="owner-a", run_id="ports-run", operation_id="ports-op")


# LLM: MANAGED续租只有原adapter提供；先于C租约创建真实运行链，不给LocalStore伪造续租方法。
# 函数用途: 为实际续租线程准备临时原repo/store/current attempt。
@pytest.fixture
def managed_case(tmp_path):
    repo = RuntimeRepository(tmp_path / "managed-runtime.db")
    chain = repo.record_run_creation(owner_id="owner-a", goal="合成工具续租", conversation_task_id="ports-task",
        thread_id="ports-thread", run_id="ports-run", role="assistant")
    return ManagedOperationStore(repo), str(chain["attempt_id"])


@pytest.fixture(params=["transport", "exit", "request_context", "tool_context"])
def actual_error(request, managed_case, c_actual, monkeypatch):
    _gate, claim, _agent = c_actual
    if request.param == "transport":
        yield RequestExecutionOutcomeUncertainError(json.loads(claim.path.read_text()), "global_tool_commit")
        return
    if request.param == "request_context":
        yield RequestExecutionContextUnavailable("original_request_transaction_ports_required")
        return
    if request.param == "tool_context":
        yield transport.ToolWriteContextUnavailable("original_tool_write_claim_required")
        return
    # 物理退出异常必须来自原bounded Call及真实Thread尾部，不能伪造call_id或operation_entered=False。
    original_run = threading.Thread.run
    release, reached = threading.Event(), threading.Event()
    workers = []

    def tail(worker):
        result = original_run(worker)
        if worker.name.startswith("bounded-call:"):
            workers.append(worker)
            reached.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(threading.Thread, "run", tail)
    error = None
    try:
        try:
            bounded.call_with_deadline(lambda: "logical-result", deadline=time.monotonic() + 0.03,
                                       resource_key=object(), wait_for_exit=True)
        except bounded.BoundedCallExitUnconfirmedError as caught:
            error = caught
        assert error is not None and reached.wait(1)
        assert len(workers) == 1 and workers[0].is_alive()
        assert any(row.call_id == error.call_id and row.worker is workers[0] and row.retained()
                   for row in bounded.bounded_call_drain_snapshot())
        yield error
        assert workers[0].is_alive() and not hasattr(error, "operation_entered")
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
        assert all(not worker.is_alive() for worker in workers)
        with bounded._CALL_LOCK:
            bounded._reap_calls_locked()


# LLM: 故障只包住指定真实store方法，所有原领取/UNKNOWN/lease仍由原数据库实现产生。
# 函数用途: 在指定阶段抛同一个对象，同时暴露真实记录供断言核实。
def _stage_case(tmp_path, monkeypatch, stage, error):
    store = LocalStore(tmp_path / "operations.db", enable_fts=False)
    calls = []

    def invoke():
        calls.append("handler")
        return ToolHandlerOutcome("counting_write", True, "completed")

    request = _request(store, invoke)
    if stage in {"claim", "finish", "settled"}:
        if stage == "settled":
            def settled(_context):
                raise error
            return store, replace(request, on_settled=settled), calls
        name = "claim_tool_operation" if stage == "claim" else "finish_tool_operation"
        original = getattr(store, name)

        def fail(value):
            if stage == "claim":
                original(value)  # 部分commit后丢回执仍保原running，不能宣称not_started。
            raise error

        monkeypatch.setattr(store, name, fail)
        return store, request, calls
    seed = replace(request, invoke=lambda: ToolHandlerOutcome(
        "counting_write", False, "provider unknown", error_code="TOOL_TIMEOUT",
        handler_executed=True, effect_outcome="unknown"))
    assert coordinator.execute_tool_operation(seed).error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert _record(store).status == "unknown"
    reconciliation = ToolOperationReconciliation(outcome="unknown", source_ref="controlled:unknown")
    if stage == "reconciler":
        def reconcile(_record):
            raise error
    else:
        if stage == "reopen":
            reconciliation = ToolOperationReconciliation(outcome="safe_to_retry", source_ref="controlled:retry")
        elif stage == "reconcile_finish":
            reconciliation = ToolOperationReconciliation(outcome="succeeded", source_ref="controlled:committed",
                result=ToolHandlerOutcome("counting_write", True, "reconciled"))
        def reconcile(_record):
            return reconciliation
        names = {"reconcile_claim": "claim_tool_operation_reconciliation", "release": "release_tool_operation_reconciliation",
                 "reopen": "reopen_tool_operation_after_reconciliation", "reconcile_finish": "finish_tool_operation"}
        name = names[stage]
        original = getattr(store, name)

        def fail(value):
            if stage == "reconcile_claim":
                original(value)
            raise error

        monkeypatch.setattr(store, name, fail)
    return store, replace(request, reconcile=reconcile), calls


STAGES = ["claim", "finish", "settled", "reconciler", "reconcile_claim", "release", "reopen", "reconcile_finish"]


@pytest.mark.parametrize("stage", STAGES)
def test_actual_typed_failure_survives_original_store_stage_without_retiring_responsibility(
    tmp_path, monkeypatch, actual_error, stage,
):
    store, request, calls = _stage_case(tmp_path, monkeypatch, stage, actual_error)
    before = dict(vars(actual_error))
    with pytest.raises(type(actual_error)) as raised:
        coordinator.execute_tool_operation(request)
    assert raised.value is actual_error and vars(actual_error) == before
    record = _record(store)
    assert record is not None and record.generation == 1
    assert record.status == ("running" if stage in {"claim", "finish"} else "succeeded" if stage == "settled" else "unknown")
    assert calls == (["handler"] if stage in {"finish", "settled"} else [])
    if stage not in {"claim", "finish", "settled"}:
        assert record.result.get("_reconciliation_claim")


@pytest.mark.parametrize("stage", STAGES)
def test_same_error_code_ordinary_runtime_error_preserves_original_failure_receipt(tmp_path, monkeypatch, stage):
    error = RuntimeError("ordinary business failure")
    error.error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"
    store, request, calls = _stage_case(tmp_path, monkeypatch, stage, error)
    result = coordinator.execute_tool_operation(request)
    record = _record(store)
    assert record is not None and record.generation == 1
    assert result.ok is (stage == "settled")
    assert record.status == ("running" if stage in {"claim", "finish"} else "succeeded" if stage == "settled" else "unknown")
    assert calls == (["handler"] if stage in {"finish", "settled"} else [])


# LLM: 该工具走原模型规格/运行策略/ToolExecutor，真实文件写仅由原commit门消费；不是权限替身。
# 类用途: 在已授权工具handler观察原claim与晚回调限制，所有产物仅在pytest临时目录。
class _PortTool(_CountingTool):
    # LLM: 注册同一标准mutating测试工具，不改变执行器授权规则。
    # 函数用途: 保存受控故障或真实写叶子，供原工具执行链调用。
    def __init__(self, path, *, error=None, use_commit=False):
        super().__init__()
        self.path, self.error, self.use_commit = path, error, use_commit
        self.commit = self.context = None

    # LLM: 只有原handler进入时计数；typed故障前后都不清数据库权威，写回调不得逃出原claim寿命。
    # 函数用途: 调用同一次原短写门或抛出测试故障，返回真实工具结果。
    def execute(self, params):
        self.calls += 1
        if self.error is not None:
            raise self.error
        self.context = copy_context()
        self.commit = transport.original_tool_write_commit()
        if self.use_commit:
            assert callable(self.commit)
            self.commit(lambda: self.path.write_text("original-commit"))
        return ToolHandlerOutcome("counting_write", True, "completed")


def test_registry_keeps_typed_failure_and_original_claim_running(tmp_path, actual_error):
    store = LocalStore(tmp_path / "registry.db", enable_fts=False)
    tool = _PortTool(tmp_path / "unused", error=actual_error)
    registry = _registry(tmp_path, store, tool)
    before = dict(vars(actual_error))
    with pytest.raises(type(actual_error)) as raised:
        _execute(registry, _call("registry-run", "call-one", 7))
    assert raised.value is actual_error and vars(actual_error) == before and tool.calls == 1
    record = store.get_tool_operation(owner_id="owner-a", run_id="registry-run", operation_id="tool_call:call-one")
    assert record is not None and record.status == "running" and record.generation == 1


def test_real_C_original_store_and_registry_commit_scope_expires_without_second_write(tmp_path, c_actual):
    gate, claim, agent = c_actual
    # 原C fixture只需base HomePaths；真实工具源还须同main owner的完整typed投影，不伪填owner_id绕门。
    agent.home_paths = home_paths_with_owner(agent.home_paths, resolve_owner_home(agent.home_paths.root, OwnerIdentity.local_main()))
    store = LocalStore(tmp_path / "C-local.db", enable_fts=False)
    tool = _PortTool(tmp_path / "committed.txt", use_commit=True)
    registry = _registry(tmp_path, store, tool, owner_id=agent.home_paths.owner_id)
    agent.tools = registry
    agent.local_store = agent._operation_store = store
    agent.subagents = SimpleNamespace(execution_mode=ExecutionMode.LOCAL_UNMANAGED)
    with coordinator_execution_scope(gate, claim, agent):
        result = _execute(registry, _call("C-tool-run", "call-one", 7))
        assert result.ok is True and tool.path.read_text() == "original-commit" and tool.calls == 1, {"code": result.error_code, "output": result.output}
        record = store.get_tool_operation(owner_id=agent.home_paths.owner_id, run_id="C-tool-run", operation_id="tool_call:call-one")
        assert record.status == "succeeded" and record.generation == 1
        with pytest.raises(transport.ToolWriteContextUnavailable):
            tool.commit(lambda: tool.path.write_text("late"))
        with pytest.raises(transport.ToolWriteContextUnavailable):
            tool.context.run(transport.original_tool_write_commit)
        assert tool.path.read_text() == "original-commit"
    with pytest.raises(transport.ToolWriteContextUnavailable):
        tool.context.run(transport.original_tool_write_commit)


def test_real_C_without_tool_claim_cannot_create_write_callback(c_actual):
    gate, claim, agent = c_actual
    with coordinator_execution_scope(gate, claim, agent):
        with pytest.raises(transport.ToolWriteContextUnavailable, match="original_tool_write_claim_required"):
            transport.original_tool_write_commit()


def test_count_zero_original_store_never_constructs_transport_cell(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Count0 added a tool transport cell")
    monkeypatch.setattr(transport._OriginalCell, "__init__", forbidden)
    store = LocalStore(tmp_path / "count0.db", enable_fts=False)
    tool = _PortTool(tmp_path / "unused")
    registry = _registry(tmp_path, store, tool)
    assert _execute(registry, _call("count0-run", "call-one", 7)).ok
    assert tool.calls == 1 and tool.commit is None and transport._CLAIM.get() is None


# LLM: 多继承仍是common实际未结类型，不能先按RuntimeConflictError重试并宣告未执行。
# 类用途: 验证原忙冲突catch的类型优先级，不改错误码或权限来源。
class _ConflictShapedUncertain(RequestExecutionOutcomeUncertainError, RuntimeConflictError):
    pass


def test_canonical_transport_subclass_cannot_enter_busy_retry(tmp_path, monkeypatch, c_actual):
    _gate, claim, _agent = c_actual
    error = _ConflictShapedUncertain(json.loads(claim.path.read_text()), "claim_tool_operation")
    store = LocalStore(tmp_path / "conflict.db", enable_fts=False)
    original = store.claim_tool_operation
    calls, delays = [], []

    def fail(request):
        calls.append(original(request))
        raise error

    monkeypatch.setattr(store, "claim_tool_operation", fail)
    monkeypatch.setattr(coordinator.time, "sleep", lambda seconds: delays.append(seconds))
    with pytest.raises(_ConflictShapedUncertain) as raised:
        coordinator.execute_tool_operation(_request(store, lambda: pytest.fail("claim failed before handler")))
    assert raised.value is error and len(calls) == 1 and delays == []
    assert _record(store).status == "running" and _record(store).generation == 1


def test_actual_renewal_thread_returns_typed_error_and_preserves_same_context(
    tmp_path, monkeypatch, managed_case, actual_error, c_actual,
):
    gate, claim, agent = c_actual
    store, attempt_id = managed_case
    original = store.renew_tool_operation_lease
    seen, entered = [], threading.Event()
    selected = None

    def renew(**values):
        cell = transport._CLAIM.get()
        seen.append((threading.current_thread(), cell.value[:2] if cell else None, current_coordinator_execution()))
        try:
            original(**values)
        finally:
            entered.set()
        raise actual_error

    def invoke():
        assert entered.wait(3)
        return ToolHandlerOutcome("counting_write", True, "handler-complete")

    monkeypatch.setattr(store, "renew_tool_operation_lease", renew)
    monkeypatch.setattr(coordinator, "_MINIMUM_LEASE_SECONDS", 1)
    monkeypatch.setattr(coordinator, "_LEASE_GRACE_SECONDS", 0)
    request = replace(_request(store, invoke), attempt_id=attempt_id)
    before = dict(vars(actual_error))
    with coordinator_execution_scope(gate, claim, agent):
        selected = current_coordinator_execution()
        with pytest.raises(type(actual_error)) as raised:
            coordinator.execute_tool_operation(request)
        assert raised.value is actual_error and vars(actual_error) == before
    assert len(seen) == 1 and not seen[0][0].is_alive()
    assert seen[0][1][0] is request and seen[0][1][1].action == "execute"
    assert seen[0][2] is selected
    assert _record(store).status == "running" and _record(store).generation == 1


def test_ordinary_renewal_same_code_failure_keeps_original_handler_result(tmp_path, monkeypatch, managed_case):
    store, attempt_id = managed_case
    original = store.renew_tool_operation_lease
    entered = threading.Event()
    error = RuntimeError("ordinary renewal error")
    error.error_code = "EXECUTION_CONTEXT_REQUIRED"

    def renew(**values):
        original(**values)
        entered.set()
        raise error

    def invoke():
        assert entered.wait(3)
        return ToolHandlerOutcome("counting_write", True, "handler-complete")

    monkeypatch.setattr(store, "renew_tool_operation_lease", renew)
    monkeypatch.setattr(coordinator, "_MINIMUM_LEASE_SECONDS", 1)
    monkeypatch.setattr(coordinator, "_LEASE_GRACE_SECONDS", 0)
    result = coordinator.execute_tool_operation(replace(_request(store, invoke), attempt_id=attempt_id))
    assert result.ok and _record(store).status == "succeeded"


def test_renewal_secondary_typed_error_cannot_replace_primary_handler_unknown(
    tmp_path, monkeypatch, managed_case, actual_error,
):
    store, attempt_id = managed_case
    original = store.renew_tool_operation_lease
    entered = threading.Event()
    secondary = RequestExecutionContextUnavailable("secondary_renewal_source_missing")

    def renew(**values):
        original(**values)
        entered.set()
        raise secondary

    def invoke():
        assert entered.wait(3)
        raise actual_error

    monkeypatch.setattr(store, "renew_tool_operation_lease", renew)
    monkeypatch.setattr(coordinator, "_MINIMUM_LEASE_SECONDS", 1)
    monkeypatch.setattr(coordinator, "_LEASE_GRACE_SECONDS", 0)
    with pytest.raises(type(actual_error)) as raised:
        coordinator.execute_tool_operation(replace(_request(store, invoke), attempt_id=attempt_id))
    assert raised.value is actual_error and raised.value.__context__ is secondary
    assert _record(store).status == "running" and _record(store).generation == 1
