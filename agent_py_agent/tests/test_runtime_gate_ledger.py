from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.agent_core.runner_stage_trace import RunnerToolStageTraceRequest
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_round_execution import ToolCallRecordParams
from agent_py_agent.agent.local_storage import RuntimeGateLedgerRecord
from agent_py_agent.agent.local_store import LocalStore
from agent_py_agent.agent.tooling.models import ToolExecutionResult


# LLM: Runtime gate records must survive process restarts for replay and resume.
# 函数用途: 验证工具入口 gate、审批绑定和幂等事实会落入 LocalStore，而不是只存在当前内存。
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
            status="completed",
        )
    )

    reopened = LocalStore(tmp_path / "local.db", enable_fts=False)
    records = reopened.list_runtime_gate_ledger(run_id="run-1")

    assert len(records) == 1
    assert records[0].operation_id == "op-1"
    assert records[0].runtime_gate["allowed"] is True
    assert records[0].parameters == {"path": "out/report.md"}


# LLM: Idempotency replay should read the same ledger facts used by the runtime gate.
# 函数用途: 验证 LocalStore 能输出 IdempotencyLedgerGate 可直接消费的结构字段。
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
            status="completed",
        )
    )

    ledger = store.runtime_idempotency_ledger(run_id="run-1")

    assert ledger == (
        {
            "idempotency_key": "idem-1",
            "args_hash": "sha256:args",
            "status": "completed",
            "result_ref": "artifact://run-1/op-1",
        },
    )


# LLM: Replaying the same operation should update the row instead of duplicating side effects.
# 函数用途: 验证 operation_id 是同一 run 内的幂等写入键，重复记录不会膨胀账本。
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
                "status": "completed",
                "result_ref": "artifact://run-1/op-1",
            }
        )
    )

    records = store.list_runtime_gate_ledger(run_id="run-1")

    assert len(records) == 1
    assert records[0].status == "completed"
    assert records[0].result_ref == "artifact://run-1/op-1"


# LLM: Tool loop recording should persist runtime gate facts without relying on final prose.
# 函数用途: 验证真实工具循环记录时，会把 archive_tool_call 中的 gate 和参数写入 LocalStore 账本。
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


# LLM: Tool execution should feed persisted idempotency rows back into the runtime gate.
# 函数用途: 验证同一 run 的历史副作用账本会注入 write_boundary.idempotency_ledger。
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
            status="completed",
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
            "status": "completed",
            "result_ref": "artifact://run-1/op-old",
        },
    )


class _CapturingTools:
    def __init__(self):
        self.captured_write_boundary = None

    def execute_call(self, payload, *, allowed_tools, granted_capabilities, write_boundary):
        self.captured_write_boundary = write_boundary
        return ToolExecutionResult("write_file", True, "ok")


def _loop_params(*, run_id: str = "run-1", task_id: str = "task-1", write_boundary: dict | None = None):
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
        task_attributes=None,
        request_id="request-1",
        run_id=run_id,
        task_id=task_id,
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
