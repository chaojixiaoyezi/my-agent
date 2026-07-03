from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.agent_core.runner.stage_trace import RunnerToolStageTraceRequest
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
    persist_tool_runtime_ledger,
    write_boundary_with_runtime_ledger,
)
from agent_py_agent.agent.local_storage import LocalStore, RuntimeGateLedgerRecord
from agent_py_agent.agent.tooling.models import ToolExecutionResult


def test_local_store_persists_runtime_gate_records_for_replay(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)

    store.record_runtime_gate_ledger(
        RuntimeGateLedgerRecord(
            run_id="run-1",
            task_id="task-1",
            operation_id="op-1",
            tool="write_file",
            parameters={"path": "out/report.md"},
            runtime_gate={"gate": "runtime_tool_gateway", "allowed": True},
            idempotency_key="idem-1",
            args_hash="sha256:args",
            approval_id="",
            result_ref="artifact://run-1/op-1",
            status="done",
        )
    )

    reopened = LocalStore(tmp_path / "local.db", enable_fts=False)
    records = reopened.list_runtime_gate_ledger(run_id="run-1")

    assert len(records) == 1
    assert records[0].operation_id == "op-1"
    assert records[0].runtime_gate["allowed"] is True
    assert records[0].parameters == {"path": "out/report.md"}


def test_local_store_projects_runtime_gate_records_to_idempotency_ledger(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    store.record_runtime_gate_ledger(
        RuntimeGateLedgerRecord(
            run_id="run-1",
            task_id="task-1",
            operation_id="op-1",
            tool="write_file",
            parameters={"path": "out/report.md"},
            runtime_gate={"gate": "runtime_tool_gateway", "allowed": True},
            idempotency_key="idem-1",
            args_hash="sha256:args",
            result_ref="artifact://run-1/op-1",
            status="done",
        )
    )

    ledger = store.runtime_idempotency_ledger(run_id="run-1")

    assert ledger == (
        {
            "idempotency_key": "idem-1",
            "args_hash": "sha256:args",
            "status": "done",
            "result_ref": "artifact://run-1/op-1",
        },
    )


def test_local_store_runtime_gate_operation_id_is_idempotent(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    base = RuntimeGateLedgerRecord(
        run_id="run-1",
        task_id="task-1",
        operation_id="op-1",
        tool="write_file",
        parameters={"path": "out/report.md"},
        runtime_gate={"gate": "runtime_tool_gateway", "allowed": False},
        idempotency_key="idem-1",
        args_hash="sha256:args",
        status="blocked",
    )
    store.record_runtime_gate_ledger(base)
    store.record_runtime_gate_ledger(
        RuntimeGateLedgerRecord(
            **{
                **base.__dict__,
                "runtime_gate": {"gate": "runtime_tool_gateway", "allowed": True},
                "status": "done",
                "result_ref": "artifact://run-1/op-1",
            }
        )
    )

    records = store.list_runtime_gate_ledger(run_id="run-1")

    assert len(records) == 1
    assert records[0].status == "done"
    assert records[0].result_ref == "artifact://run-1/op-1"


def test_tool_loop_record_persists_runtime_gate_ledger(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    agent = SimpleNamespace(root=tmp_path, local_store=store)
    params = _loop_params(write_boundary={})

    ToolLoopService(agent)._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "write_file", "path": "out/report.md"},
            result=_runtime_gate_result(),
        )
    )

    records = store.list_runtime_gate_ledger(run_id="run-1")
    assert len(records) == 1
    assert records[0].operation_id == "op-1"
    assert records[0].runtime_gate["allowed"] is True
    assert records[0].parameters == {"tool": "write_file", "path": "out/report.md"}


def test_execute_traced_tool_call_passes_explicit_run_scope_envelope(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tools = _CapturingTools()
    agent = SimpleNamespace(tools=tools, local_store=store)
    params = _loop_params(
        run_id="run-child",
        task_id="task-child",
        task_attributes={
            "parent_run_id": "run-parent",
            "root_run_id": "run-root",
            "root_task_id": "task-root",
            "depth": 1,
            "agent_kind": "child_agent",
        },
        write_boundary={},
    )
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=SimpleNamespace(params=params),
        payload={"tool": "write_file", "path": "out/report.md"},
        trace_request=RunnerToolStageTraceRequest(agent=agent, params=params, tool_rounds=1, idx=1, payload={}),
    )

    execute_traced_tool_call(request)

    assert isinstance(tools.captured_payload, ToolCallEnvelope)
    assert tools.captured_payload.scope.run_id == "run-child"
    assert tools.captured_payload.scope.parent_run_id == "run-parent"
    assert tools.captured_payload.scope.root_run_id == "run-root"
    assert tools.captured_payload.scope.root_task_id == "task-root"
    assert tools.captured_payload.scope.depth == 1
    assert tools.captured_payload.scope.agent_kind == "child_agent"


def test_tool_loop_record_appends_agent_event_with_explicit_scope(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    agent = SimpleNamespace(root=tmp_path, local_store=store)
    params = _loop_params(
        run_id="run-child",
        task_id="task-child",
        task_attributes={
            "parent_run_id": "run-parent",
            "root_run_id": "run-root",
            "root_task_id": "task-root",
            "depth": 1,
            "agent_kind": "child_agent",
        },
        write_boundary={},
    )

    ToolLoopService(agent)._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "write_file", "path": "out/report.md"},
            result=_runtime_gate_result(),
        )
    )

    events = store.list_agent_events(root_task_id="task-root", run_id="run-child")
    tool_events = [event for event in events if event.event_type == "tool_call_finished"]
    assert len(tool_events) == 1
    event = tool_events[0]
    assert event.parent_run_id == "run-parent"
    assert event.payload["tool"] == "write_file"
    assert event.payload["ok"] is True
    assert event.payload["scope"]["run_id"] == "run-child"
    assert event.payload["scope"]["parent_run_id"] == "run-parent"
    assert event.payload["scope"]["root_run_id"] == "run-root"
    assert event.payload["operation_id"] == "op-1"


def test_runtime_ledger_locked_control_plane_does_not_crash_tool_loop():
    """控制面 SQLite 忙时不能让真实工具轮直接崩掉。"""

    _LockedStore.calls = 0
    agent = SimpleNamespace(local_store=_LockedStore())

    persist_tool_runtime_ledger(
        agent,
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "tool": "write_file",
            "ok": True,
            "result_ref": "artifact://run-1/op-1",
            "runtime_gate": {"gate": "tool_execution", "allowed": True},
            "tool_protocol_v2": {"operation_id": "op-1"},
        },
    )

    assert _LockedStore.calls >= 1


class _DiskIOStore:
    calls = 0

    def record_agent_event(self, event):
        type(self).calls += 1
        raise sqlite3.OperationalError("disk I/O error")

    def record_runtime_gate_ledger(self, record):
        type(self).calls += 1
        raise sqlite3.OperationalError("disk I/O error")


def test_runtime_ledger_disk_io_error_does_not_crash_tool_loop():
    """控制面 SQLite 磁盘满/IO 错(不止 'locked')也绝不能崩真实工具轮:尽力而为台账丢一条可以、崩任务
    不行(回归:真机大数据任务塞满磁盘 47G,'disk I/O error' 原被 re-raise 把整个 72 轮任务崩在台账写入上)。"""
    _DiskIOStore.calls = 0
    agent = SimpleNamespace(local_store=_DiskIOStore())

    persist_tool_runtime_ledger(
        agent,
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "tool": "write_file",
            "ok": True,
            "result_ref": "artifact://run-1/op-1",
            "runtime_gate": {"gate": "tool_execution", "allowed": True},
            "tool_protocol_v2": {"operation_id": "op-1"},
        },
    )

    assert _DiskIOStore.calls >= 1  # 尝试写了台账、但磁盘 IO 错没把任务崩掉


def test_execute_traced_tool_call_injects_persisted_idempotency_ledger(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    store.record_runtime_gate_ledger(
        RuntimeGateLedgerRecord(
            run_id="run-1",
            task_id="task-1",
            operation_id="op-old",
            tool="write_file",
            parameters={"path": "out/report.md"},
            runtime_gate={"gate": "tool_execution", "allowed": True},
            idempotency_key="idem-old",
            args_hash="sha256:old",
            result_ref="artifact://run-1/op-old",
            status="done",
        )
    )
    tools = _CapturingTools()
    agent = SimpleNamespace(tools=tools, local_store=store)
    params = _loop_params(run_id="run-1", task_id="task-1", write_boundary={})
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=SimpleNamespace(params=params),
        payload={"tool": "write_file", "path": "out/report.md"},
        trace_request=RunnerToolStageTraceRequest(agent=agent, params=params, tool_rounds=1, idx=1, payload={}),
    )

    execute_traced_tool_call(request)

    assert tools.captured_write_boundary["idempotency_ledger"] == (
        {
            "idempotency_key": "idem-old",
            "args_hash": "sha256:old",
            "status": "done",
            "result_ref": "artifact://run-1/op-old",
        },
    )


def test_write_boundary_injects_tool_rate_limit_records(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    store.record_runtime_gate_ledger(
        RuntimeGateLedgerRecord(
            run_id="run-1",
            task_id="task-1",
            operation_id="op-old",
            tool="web_fetch",
            parameters={"url": "https://example.test/a"},
            runtime_gate={"gate": "tool_execution", "allowed": False},
            args_hash="sha256:fetch-a",
            status="failed",
            created_at=10.0,
        )
    )
    agent = SimpleNamespace(local_store=store)

    boundary = write_boundary_with_runtime_ledger(agent, _loop_params(run_id="run-1", write_boundary={}))

    assert boundary["tool_rate_limit_records"] == (
        {
            "tool_name": "web_fetch",
            "args_hash": "sha256:fetch-a",
            "attempt_timestamps": [10.0],
            "consecutive_failures": 1,
            "last_failure_at": 10.0,
            "last_success_at": 0.0,
            "total_failures": 1,
        },
    )


def test_write_boundary_carries_current_task_workspace_roots(tmp_path):
    task_root = tmp_path / "home" / "tasks" / "today" / "task"
    params = _loop_params(
        write_boundary={},
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            }
        },
    )

    boundary = write_boundary_with_runtime_ledger(SimpleNamespace(local_store=None), params)

    assert boundary["task_root"] == str(task_root)
    assert boundary["task_output_dir"] == str(task_root / "output")
    assert boundary["task_work_dir"] == str(task_root / "work")
    assert "allowed_write_roots" not in boundary


def test_write_boundary_locks_active_child_declared_outputs() -> None:
    child = SimpleNamespace(
        parent_id="run-1",
        root_id="run-1",
        status="RUNNING",
        attributes={"output_files": ["output/child-report.md"]},
    )
    agent = SimpleNamespace(local_store=None, subagents=SimpleNamespace(list_runs=lambda: [child]))

    boundary = write_boundary_with_runtime_ledger(agent, _loop_params(run_id="run-1", write_boundary={}))

    assert boundary["locked_files"] == ["output/child-report.md"]


def test_write_boundary_does_not_lock_finished_child_declared_outputs() -> None:
    child = SimpleNamespace(
        parent_id="run-1",
        root_id="run-1",
        status="DONE",
        attributes={"output_files": ["output/child-report.md"]},
    )
    agent = SimpleNamespace(local_store=None, subagents=SimpleNamespace(list_runs=lambda: [child]))

    boundary = write_boundary_with_runtime_ledger(agent, _loop_params(run_id="run-1", write_boundary={}))

    assert "locked_files" not in boundary


def test_write_boundary_never_locks_run_out_of_its_own_declared_outputs() -> None:
    """正主不锁自己(真机实锤:子代理被自己申报的 output/inventory.py 锁死,capability
    已 GRANTED 也无济于事,3/4 子代理被迫由主代理接管代写)。锁只拦【别人】乱写。"""
    me = SimpleNamespace(
        id="subagent-1",
        parent_id="req-root",
        root_id="req-root",
        status="RUNNING",
        attributes={"output_files": ["output/inventory.py"]},
    )
    sibling = SimpleNamespace(
        id="subagent-2",
        parent_id="req-root",
        root_id="req-root",
        status="RUNNING",
        attributes={"output_files": ["output/kitchen.py"]},
    )
    agent = SimpleNamespace(local_store=None, subagents=SimpleNamespace(list_runs=lambda: [me, sibling]))

    # 子代理 runner 轮:run_id=自己,task_id=根任务 → 自己的申报不锁,兄弟的仍锁
    boundary = write_boundary_with_runtime_ledger(
        agent, _loop_params(run_id="subagent-1", task_id="req-root", write_boundary={})
    )

    assert boundary["locked_files"] == ["output/kitchen.py"]


def test_write_boundary_master_still_locked_from_active_child_outputs() -> None:
    """主代理在孩子还活跃时写孩子的在建产物仍被拦(单向外溢保护语义不回退)。"""
    child = SimpleNamespace(
        id="subagent-1",
        parent_id="req-root",
        root_id="req-root",
        status="RUNNING",
        attributes={"output_files": ["output/inventory.py"]},
    )
    agent = SimpleNamespace(local_store=None, subagents=SimpleNamespace(list_runs=lambda: [child]))

    boundary = write_boundary_with_runtime_ledger(
        agent, _loop_params(run_id="req-root", task_id="req-root", write_boundary={})
    )

    assert boundary["locked_files"] == ["output/inventory.py"]


def test_tool_rate_limit_records_reset_failures_on_done_status(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    for status, timestamp in (("failed", 10.0), ("done", 20.0)):
        store.record_runtime_gate_ledger(
            RuntimeGateLedgerRecord(
                run_id="run-1",
                task_id="task-1",
                operation_id=f"op-{status}",
                tool="web_fetch",
                parameters={"url": "https://example.test/a"},
                runtime_gate={"gate": "tool_execution", "allowed": status == "done"},
                args_hash="sha256:fetch-a",
                status=status,
                created_at=timestamp,
            )
        )
    agent = SimpleNamespace(local_store=store)

    boundary = write_boundary_with_runtime_ledger(agent, _loop_params(run_id="run-1", write_boundary={}))

    assert boundary["tool_rate_limit_records"][0]["consecutive_failures"] == 0
    assert boundary["tool_rate_limit_records"][0]["last_success_at"] == 20.0
    assert boundary["tool_rate_limit_records"][0]["total_failures"] == 1


class _CapturingTools:
    def __init__(self):
        self.captured_write_boundary = None
        self.captured_payload = None

    def execute_call(self, payload, *, allowed_tools, granted_capabilities, write_boundary):
        self.captured_payload = payload
        self.captured_write_boundary = write_boundary
        return ToolExecutionResult("write_file", True, "ok")


class _LockedStore:
    calls = 0

    def record_agent_event(self, event):
        type(self).calls += 1
        raise sqlite3.OperationalError("database is locked")

    def record_runtime_gate_ledger(self, record):
        type(self).calls += 1
        raise sqlite3.OperationalError("database is locked")


def _loop_params(
    *,
    run_id: str = "run-1",
    task_id: str = "task-1",
    write_boundary: dict | None = None,
    task_attributes: dict | None = None,
    run_scope: RunScope | None = None,
):
    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=write_boundary,
        task_attributes=task_attributes,
        request_id="request-1",
        run_id=run_id,
        task_id=task_id,
        run_scope=run_scope,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )


def _runtime_gate_result() -> ToolExecutionResult:
    return ToolExecutionResult(
        "write_file",
        True,
        "ok",
        result_envelope={
            "operation_id": "op-1",
            "runtime_gate": {
                "gate": "tool_execution",
                "allowed": True,
                "evidence": {"idempotency_key": "idem-1"},
            },
            "tool_protocol_v2": {
                "operation_id": "op-1",
                "idempotency_key": "idem-1",
            },
        },
    )
