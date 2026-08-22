from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from agent_py_agent.agent.action_protocol import RunScope
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.recovery import runtime_run_scope
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolCallExecuteParams,
    ToolCallRecordParams,
)
from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
    persist_tool_runtime_ledger,
    write_boundary_with_runtime_ledger,
)
from agent_py_agent.agent.local_storage import LocalStore, RuntimeGateLedgerRecord
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolOperation,
    ToolProtocolSnapshot,
    ToolResult,
    ToolSuccessFacts,
)


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


def test_runtime_gate_ledger_is_audit_only(tmp_path):
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

    assert not hasattr(store, "runtime_idempotency_ledger")
    assert len(store.list_runtime_gate_ledger(run_id="run-1")) == 1


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

    call = _runtime_gate_call()
    ToolLoopService(agent)._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            call=call,
            result=_runtime_gate_result(call),
        )
    )

    records = store.list_runtime_gate_ledger(run_id="run-1")
    assert len(records) == 1
    assert records[0].operation_id == "op-1"
    assert records[0].runtime_gate["allowed"] is True
    assert records[0].parameters == {"path": "out/report.md"}


def test_execute_traced_tool_call_passes_canonical_call_and_trusted_scope(tmp_path):
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
    call = _runtime_gate_call(run_id="run-child")
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=ToolCallExecuteParams(params, 1, 1, call),
        call=call,
    )

    execute_traced_tool_call(request)

    assert isinstance(tools.captured_call, ToolCall)
    assert tools.captured_call.run_id == "run-child"
    assert tools.captured_call.arguments == {"path": "out/report.md"}
    assert tools.captured_trusted_run_context["task_attributes"] == params.task_attributes
    assert (
        tools.captured_trusted_run_context["run_scope"]
        == runtime_run_scope(agent, params).to_dict()
    )


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
            "operation_id": "op-1",
            "runtime_gate": {"gate": "tool_execution", "allowed": True},
        },
    )

    assert _LockedStore.calls >= 1


class _DiskIOStore:
    calls = 0

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
            "operation_id": "op-1",
            "runtime_gate": {"gate": "tool_execution", "allowed": True},
        },
    )

    assert _DiskIOStore.calls >= 1  # 尝试写了台账、但磁盘 IO 错没把任务崩掉


def test_runtime_ledger_appends_authority_event(tmp_path):
    """A.3：工具完成事件写入权威 runtime_events（追到 attempt/task_run）。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    chain = repo.record_run_creation(owner_id="local/main", run_id="run-1", goal="g")
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    agent = SimpleNamespace(
        local_store=store,
        subagents=SimpleNamespace(runtime_db=repo),
    )

    persist_tool_runtime_ledger(
        agent,
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "tool": "write_file",
            "ok": True,
            "operation_id": "op-1",
            "attempt_id": chain["attempt_id"],
            "runtime_gate": {"gate": "tool_execution", "allowed": True},
        },
    )

    events = [
        e
        for e in repo.events_for_attempt(chain["attempt_id"])
        if e["event_type"] == "tool_completed"
    ]
    assert len(events) == 1
    event = events[0]
    assert event["agent_run_id"] == chain["agent_run_id"]
    assert event["task_run_id"] == chain["task_run_id"]
    assert event["payload"]["operation_id"] == "op-1"
    assert event["payload"]["tool"] == "write_file"
    assert event["payload"]["ok"] is True
    assert event["payload"]["status"] == "done"


def test_runtime_ledger_appends_failed_event_with_status(tmp_path):
    """A.3：失败工具事件同样入权威流，status=failed。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    chain = repo.record_run_creation(owner_id="local/main", run_id="run-1", goal="g")
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    agent = SimpleNamespace(
        local_store=store,
        subagents=SimpleNamespace(runtime_db=repo),
    )

    persist_tool_runtime_ledger(
        agent,
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "tool": "write_file",
            "ok": False,
            "error_code": "TOOL_ERROR",
            "operation_id": "op-2",
            "attempt_id": chain["attempt_id"],
            "runtime_gate": {"gate": "tool_execution", "allowed": True},
        },
    )

    events = [
        e
        for e in repo.events_for_attempt(chain["attempt_id"])
        if e["event_type"] == "tool_completed"
    ]
    assert len(events) == 1
    assert events[0]["payload"]["ok"] is False
    assert events[0]["payload"]["error_code"] == "TOOL_ERROR"
    assert events[0]["payload"]["status"] == "failed"


def test_runtime_ledger_without_authority_skips_silently(tmp_path):
    """无权威库（无 home 上下文）→ 不写事件不崩，legacy 台账照写。"""
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    agent = SimpleNamespace(local_store=store)  # 没有 subagents

    persist_tool_runtime_ledger(
        agent,
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "tool": "write_file",
            "ok": True,
            "operation_id": "op-1",
            "runtime_gate": {"gate": "tool_execution", "allowed": True},
        },
    )

    assert len(store.list_runtime_gate_ledger(run_id="run-1")) == 1


class _BrokenAuthorityRepo:
    def agent_run_for_run_id(self, run_id):
        raise sqlite3.OperationalError("disk I/O error")


def test_runtime_ledger_authority_write_error_does_not_crash_tool_loop(tmp_path):
    """A.3 写入失败是尽力而为：权威事件丢一条可以、崩任务不行。"""
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    agent = SimpleNamespace(
        local_store=store,
        subagents=SimpleNamespace(runtime_db=_BrokenAuthorityRepo()),
    )

    persist_tool_runtime_ledger(
        agent,
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "tool": "write_file",
            "ok": True,
            "operation_id": "op-1",
            "runtime_gate": {"gate": "tool_execution", "allowed": True},
        },
    )

    assert len(store.list_runtime_gate_ledger(run_id="run-1")) == 1


def test_execute_traced_tool_call_does_not_inject_audit_as_authority(tmp_path):
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
    call = _runtime_gate_call()
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=ToolCallExecuteParams(params, 1, 1, call),
        call=call,
    )

    execute_traced_tool_call(request)

    assert "idempotency_ledger" not in tools.captured_write_boundary


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

    boundary = write_boundary_with_runtime_ledger(
        agent, _loop_params(run_id="run-1", write_boundary={})
    )

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
    # WRITE-02(2026-08-15): 主链任务无既有写根时注入任务 work/output 为沙箱写根,
    # 旧语义(不注入→write_roots=None→owner home 全可写)改为收紧到任务目录。
    assert boundary["allowed_write_roots"] == [str(task_root / "work"), str(task_root / "output")]


# LLM: A promoted local Gateway task must retain the same project cwd used by
# its foreground turn; task work/output directories cannot replace that grant.
# 函数用途: 复现主代理后台整合时写项目文件被拒绝的问题，验证 cwd 和写权限保持一致。
def test_local_main_conversation_keeps_project_cwd_writable_after_promotion(tmp_path):
    project_cwd = tmp_path / "project"
    task_root = tmp_path / "home" / "tasks" / "today" / "task"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="local"),
        tools=SimpleNamespace(workspace_root=project_cwd, owner_scope_root=""),
        local_store=None,
    )
    params = _loop_params(
        source="gateway",
        write_boundary={},
        task_attributes={
            "conversation_thread_id": "thread-local",
            "conversation_task_id": "task-local",
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            },
        },
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["execution_cwd"] == str(project_cwd.resolve())
    assert boundary["allowed_write_roots"] == [
        str(task_root / "work"),
        str(task_root / "output"),
        str(project_cwd.resolve()),
    ]


def test_transient_audit_prepare_write_boundary_is_exact_work_and_output(tmp_path):
    owner_home = tmp_path / "home" / "owners" / "local" / "main"
    task_root = owner_home / "audits" / "audit-123"
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={"allowed_write_roots": [str(tmp_path)]},
        task_attributes={
            "conversation_transient_workspace": True,
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            },
        },
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == [
        str((task_root / "work").resolve()),
        str((task_root / "output").resolve()),
    ]


def test_transient_audit_prepare_write_boundary_fails_closed_on_wrong_root(tmp_path):
    owner_home = tmp_path / "home" / "owners" / "local" / "main"
    wrong_root = owner_home / "tasks" / "audit-123"
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={"allowed_write_roots": [str(tmp_path)]},
        task_attributes={
            "conversation_transient_workspace": True,
            "run_workspace": {
                "task_root": str(wrong_root),
                "output_dir": str(wrong_root / "output"),
                "work_dir": str(wrong_root / "work"),
            },
        },
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == []


def test_live_task_workspace_replaces_stale_bootstrap_workspace_roots(tmp_path):
    bootstrap_root = tmp_path / "service-cwd"
    task_root = tmp_path / "home" / "tasks" / "today" / "selected-task"
    params = _loop_params(
        write_boundary={
            "task_root": str(bootstrap_root),
            "task_output_dir": str(bootstrap_root / "output"),
            "task_work_dir": str(bootstrap_root / "work"),
        },
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


def test_remote_owner_write_boundary_is_scoped_to_current_task(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-16" / "current-task"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={},
        task_attributes={"run_workspace": {"task_root": str(task_root)}},
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == [str(task_root.resolve())]


def test_remote_main_conversation_rebases_stale_bootstrap_write_scope(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-18" / "selected-task"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={
            "task_root": str(tmp_path / "service-cwd"),
            "allowed_write_roots": [str(tmp_path / "service-cwd")],
        },
        task_attributes={
            "conversation_thread_id": "thread-alice",
            "conversation_task_id": "task-selected",
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            },
        },
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["task_root"] == str(task_root)
    assert boundary["allowed_write_roots"] == [
        str((task_root / "output").resolve()),
        str((task_root / "work").resolve()),
    ]


def test_remote_main_conversation_keeps_structured_owner_output_override(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-18" / "selected-task"
    requested = owner_home / "workspace" / "named-delivery"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={"allowed_write_roots": [str(tmp_path / "service-cwd")]},
        task_attributes={
            "conversation_thread_id": "thread-alice",
            "conversation_task_id": "task-selected",
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
                "user_requested_output_dir": str(requested),
            },
        },
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == [
        str((task_root / "output").resolve()),
        str((task_root / "work").resolve()),
        str(requested.resolve()),
    ]


def test_remote_main_conversation_rejects_output_override_outside_owner(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-18" / "selected-task"
    outside = tmp_path / "another-owner" / "delivery"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={},
        task_attributes={
            "conversation_thread_id": "thread-alice",
            "conversation_task_id": "task-selected",
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
                "user_requested_output_dir": str(outside),
            },
        },
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == [
        str((task_root / "output").resolve()),
        str((task_root / "work").resolve()),
    ]


def test_remote_main_conversation_does_not_reopen_owner_or_sibling_task(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-18" / "selected-task"
    sibling_output = owner_home / "tasks" / "2026-07-18" / "sibling-task" / "output"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )

    for requested in (owner_home, sibling_output):
        params = _loop_params(
            write_boundary={},
            task_attributes={
                "conversation_thread_id": "thread-alice",
                "conversation_task_id": "task-selected",
                "run_workspace": {
                    "task_root": str(task_root),
                    "output_dir": str(task_root / "output"),
                    "work_dir": str(task_root / "work"),
                    "user_requested_output_dir": str(requested),
                },
            },
        )

        boundary = write_boundary_with_runtime_ledger(agent, params)

        assert boundary["allowed_write_roots"] == [
            str((task_root / "output").resolve()),
            str((task_root / "work").resolve()),
        ]


def test_remote_owner_empty_legacy_scope_is_replaced_by_current_task(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-16" / "current-task"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={"allowed_write_roots": []},
        task_attributes={"run_workspace": {"task_root": str(task_root)}},
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == [str(task_root.resolve())]


def test_remote_owner_keeps_narrow_child_grant_and_drops_sibling_task(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-16" / "current-task"
    child_root = task_root / "work" / "agents" / "child-1"
    sibling_root = owner_home / "tasks" / "2026-07-16" / "sibling-task"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={"allowed_write_roots": [str(child_root), str(sibling_root)]},
        task_attributes={"run_workspace": {"task_root": str(task_root)}},
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == [str(child_root.resolve())]


def test_remote_task_local_run_keeps_narrow_child_grant(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-18" / "current-task"
    child_root = task_root / "work" / "agents" / "child-1"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        context_scope="task_local",
        write_boundary={"allowed_write_roots": [str(child_root)]},
        task_attributes={
            "conversation_thread_id": "thread-alice",
            "conversation_task_id": "task-current",
            "run_workspace": {"task_root": str(task_root)},
        },
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == [str(child_root.resolve())]


def test_remote_task_local_exact_rebase_allows_only_selected_task_root(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    selected_root = owner_home / "tasks" / "2026-07-19" / "selected-task"
    prior_child_root = (
        owner_home / "tasks" / "2026-07-19" / "new-parent" / "work" / "agents" / "child-1"
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        context_scope="task_local",
        write_boundary={"allowed_write_roots": [str(prior_child_root)]},
        task_attributes={
            "conversation_thread_id": "thread-alice",
            "conversation_task_id": "task-parent",
            "conversation_subagent_workspace_rebase": {
                "task_id": "task-selected",
                "task_root": str(selected_root),
            },
            "run_workspace": {"task_root": str(selected_root)},
        },
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == [str(selected_root.resolve())]


def test_remote_owner_invalid_task_root_fails_closed(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=str(owner_home)),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={},
        task_attributes={"run_workspace": {"task_root": str(owner_home / "workspace")}},
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary["allowed_write_roots"] == []


def test_remote_owner_admin_bypass_does_not_add_task_scope(tmp_path):
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "alice"
    task_root = owner_home / "tasks" / "2026-07-16" / "current-task"
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_owner_provider="feishu"),
        tools=SimpleNamespace(owner_scope_root=""),
        local_store=None,
    )
    params = _loop_params(
        write_boundary={},
        task_attributes={"run_workspace": {"task_root": str(task_root)}},
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert "allowed_write_roots" not in boundary


def test_write_boundary_locks_active_child_declared_outputs() -> None:
    child = SimpleNamespace(
        parent_id="run-1",
        root_id="run-1",
        status="RUNNING",
        attributes={"output_files": ["output/child-report.md"]},
    )
    agent = SimpleNamespace(local_store=None, subagents=SimpleNamespace(list_runs=lambda: [child]))

    boundary = write_boundary_with_runtime_ledger(
        agent, _loop_params(run_id="run-1", write_boundary={})
    )

    assert boundary["locked_files"] == ["output/child-report.md"]


def test_write_boundary_does_not_lock_finished_child_declared_outputs() -> None:
    child = SimpleNamespace(
        parent_id="run-1",
        root_id="run-1",
        status="DONE",
        attributes={"output_files": ["output/child-report.md"]},
    )
    agent = SimpleNamespace(local_store=None, subagents=SimpleNamespace(list_runs=lambda: [child]))

    boundary = write_boundary_with_runtime_ledger(
        agent, _loop_params(run_id="run-1", write_boundary={})
    )

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
    agent = SimpleNamespace(
        local_store=None, subagents=SimpleNamespace(list_runs=lambda: [me, sibling])
    )

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


def test_write_boundary_descendant_does_not_lock_ancestor_delegated_output() -> None:
    """A leaf may write a parent-delegated output while sibling outputs stay locked."""

    parent = SimpleNamespace(
        id="coordinator-1",
        parent_id="req-root",
        root_id="req-root",
        status="RUNNING",
        attributes={"output_files": ["output/site.html"]},
    )
    leaf = SimpleNamespace(
        id="leaf-1",
        parent_id="coordinator-1",
        root_id="req-root",
        status="RUNNING",
        attributes={},
    )
    sibling = SimpleNamespace(
        id="sibling-1",
        parent_id="req-root",
        root_id="req-root",
        status="RUNNING",
        attributes={"output_files": ["output/sibling.html"]},
    )
    agent = SimpleNamespace(
        local_store=None,
        subagents=SimpleNamespace(list_runs=lambda: [parent, leaf, sibling]),
    )

    boundary = write_boundary_with_runtime_ledger(
        agent,
        _loop_params(run_id="leaf-1", task_id="req-root", write_boundary={}),
    )

    assert boundary["locked_files"] == ["output/sibling.html"]


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

    boundary = write_boundary_with_runtime_ledger(
        agent, _loop_params(run_id="run-1", write_boundary={})
    )

    assert boundary["tool_rate_limit_records"][0]["consecutive_failures"] == 0
    assert boundary["tool_rate_limit_records"][0]["last_success_at"] == 20.0
    assert boundary["tool_rate_limit_records"][0]["total_failures"] == 1


class _CapturingTools:
    def __init__(self):
        self.captured_write_boundary = None
        self.captured_call = None
        self.captured_trusted_run_context = None

    def execute_tool(
        self,
        call,
        *,
        write_boundary,
        runtime_snapshot,
        trusted_run_context=None,
        cancellation_token=None,
        required_action=None,
        pre_handler_gate=None,
        output_archiver=None,
    ):
        _ = (
            runtime_snapshot,
            cancellation_token,
            required_action,
            pre_handler_gate,
            output_archiver,
        )
        self.captured_call = call
        self.captured_write_boundary = write_boundary
        self.captured_trusted_run_context = trusted_run_context
        return ToolExecution(
            call,
            ActionDecision("allow"),
            ToolResult.succeeded(call, "ok"),
            ("received", "running", "succeeded", "persisted", "projected"),
        )


class _LockedStore:
    calls = 0

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
    context_scope: str = "default",
    source: str = "run",
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
        write_boundary=write_boundary,
        task_attributes=task_attributes,
        context_scope=context_scope,
        source=source,
        request_id="request-1",
        run_id=run_id,
        task_id=task_id,
        run_scope=run_scope,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=ToolProtocolSnapshot(
            run_id,
            "text",
            ProviderToolCapability(
                provider="test",
                endpoint="local://test",
                model="fake",
                stream=False,
                native_supported=False,
                evidence="runtime_gate_ledger_fixture",
            ),
        ),
    )


def _runtime_gate_call(*, run_id: str = "run-1") -> ToolCall:
    return ToolCall(
        call_id="call-1",
        tool_name="write_file",
        arguments={"path": "out/report.md"},
        source_protocol="native",
        schema_hash="sha256:" + "0" * 64,
        run_id=run_id,
        turn_id="turn-1",
        attempt_id="attempt-1",
        operation_id="op-1",
        idempotency_key="idem-1",
    )


def test_runtime_approval_binding_is_merged_once_into_write_boundary() -> None:
    binding = {
        "approval_id": "approval-1",
        "tool_name": "run_command",
        "run_id": "run-1",
        "operation_id": "operation-1",
        "idempotency_key": "idempotency-1",
        "args_hash": "sha256:args",
        "status": "APPROVED",
    }
    params = SimpleNamespace(
        write_boundary={"approved_actions": [dict(binding)]},
        runtime_approved_actions=[dict(binding), {"tool_name": "incomplete"}],
        run_id="",
    )

    merged = write_boundary_with_runtime_ledger(SimpleNamespace(), params)

    assert merged is not None
    assert merged["approved_actions"] == [binding]


def _runtime_gate_result(call: ToolCall) -> ToolResult:
    return ToolResult.succeeded(
        call,
        "ok",
        facts=ToolSuccessFacts(
            operation=ToolOperation(
                operation_id="op-1",
                idempotency_key="idem-1",
                args_hash=call.args_hash,
                status="succeeded",
                handler_executed=True,
                effect_outcome="confirmed",
                effect_source_ref="tool-operation://run-1/op-1",
            ),
            effect_outcome="confirmed",
            effect_source_ref="tool-operation://run-1/op-1",
            metadata={
                "handler_details": {
                    "runtime_gate": {
                        "gate": "tool_execution",
                        "allowed": True,
                        "evidence": {"idempotency_key": "idem-1"},
                    },
                }
            },
        ),
    )
