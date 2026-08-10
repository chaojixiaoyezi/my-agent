"""F7：权威 fence 主链接线（3.txt F.6：handler 前五处 fence 之一 + settle）。

R2 的 fence 机制（create_tool_operation / verify_fence /
mark_operation_executing / settle_operation）原本只有测试调用（旁路原型），
本文件验证把它们接进主链工具执行路径（execute_traced_tool_call）：

- authority_context：无权威库 → None（静默跳过，legacy 兼容）；run 已有链 → 复用
  （A.4：一次 TaskRun 一棵 tree，不重复建）；缺链 → 就地登记。
- open_authority_operation：建操作行 + handler 前 verify_fence + 置 EXECUTING；
  attempt 已不是 current pointer → block_reason（fail-closed，不执行 handler）。
- close_authority_operation：handler 后 settle（SUCCEEDED/FAILED 与结果对应），
  缺失操作行/已 settle 一律吞掉（权威写入尽力而为，崩任务不行）。
- 主链接线：真实仓库下工具执行完整走 建行→fence→EXECUTING→handler→settle；
  attempt 过期时 handler 被拦截、无操作行残留。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.authority_fence import (
    AUTHORITY_FENCE_FAULT,
    authority_context,
    close_authority_operation,
    open_authority_operation,
)
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolCallExecuteParams,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolProtocolSnapshot,
    ToolResult,
)


# ------------------------------------------------------------------- helpers


def _repo(tmp_path) -> RuntimeRepository:
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _agent(repo: RuntimeRepository) -> SimpleNamespace:
    return SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))


def _params(run_id: str = "run-fence", task_id: str = "task-fence") -> ToolLoopExecuteParams:
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
        write_boundary={},
        task_attributes={},
        context_scope="default",
        request_id="request-1",
        run_id=run_id,
        task_id=task_id,
        run_scope=None,
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
                evidence="authority_fence_fixture",
            ),
        ),
    )


def _fence_call(*, run_id: str = "run-fence") -> ToolCall:
    return ToolCall(
        call_id="call-f1",
        tool_name="write_file",
        arguments={"path": "out/report.md"},
        source_protocol="native",
        schema_hash="sha256:" + "0" * 64,
        run_id=run_id,
        turn_id="turn-1",
        attempt_id="attempt-1",
        operation_id="op-f1",
        idempotency_key="idem-f1",
    )


class _FenceTools:
    """按真实 ToolExecutor 顺序的桩：gate 先于 handler（executor.py 同序）。"""

    def __init__(self, repo=None, agent_run_id="", pre_advance=False):
        self.repo = repo
        self.agent_run_id = agent_run_id
        self.pre_advance = pre_advance
        self.gate_outcome = None
        self.handler_called = False
        self.captured_call = None

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
        _ = (write_boundary, runtime_snapshot, trusted_run_context, cancellation_token,
             required_action, output_archiver)
        self.captured_call = call
        if self.pre_advance and self.repo is not None:
            # 模拟并发轮替：gate 前 attempt pointer 已被其它进程推进。
            self.repo.create_attempt(self.agent_run_id)
        if pre_handler_gate is not None:
            outcome = pre_handler_gate(call)
            if outcome is not None:
                self.gate_outcome = outcome
                return ToolExecution(
                    call,
                    ActionDecision("deny"),
                    _gate_result(call, outcome),
                    ("received", "approved", "failed", "reconciled", "persisted", "projected"),
                )
        self.handler_called = True
        return ToolExecution(
            call,
            ActionDecision("allow"),
            ToolResult.succeeded(call, "ok"),
            ("received", "approved", "running", "succeeded", "reconciled", "persisted", "projected"),
        )


def _gate_result(call: ToolCall, outcome: ToolHandlerOutcome) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status="failed",
        error_code=outcome.error_code or "TOOL_BLOCKED",
        error_category=str(outcome.error_category or ""),
        failure_stage=str(outcome.failure_stage or ""),
        handler_executed=bool(outcome.handler_executed),
    )


# ------------------------------------------------------ authority_context


def test_authority_context_skips_without_repo():
    assert authority_context(SimpleNamespace(), _params()) is None


def test_authority_context_creates_chain_and_reuses(tmp_path):
    """A.4：一次 TaskRun 一棵 tree —— run_id 复用即复用链，不重复登记。"""
    repo = _repo(tmp_path)
    agent = _agent(repo)
    params = _params(run_id="run-1", task_id="task-1")

    first = authority_context(agent, params)
    second = authority_context(agent, params)

    assert first is not None and second is not None
    assert first[0] is repo and second[0] is repo
    assert first[1] == second[1]  # 同一 AgentRun
    assert first[2] == second[2]  # 同一 current attempt
    row = repo.agent_run_for_run_id("run-1")
    assert row is not None
    task_run_id = row["task_run_id"]
    assert len(repo.agent_runs_for_task_run(task_run_id)) == 1


def test_authority_context_chain_matches_current_pointer(tmp_path):
    repo = _repo(tmp_path)
    ctx = authority_context(_agent(repo), _params(run_id="run-9", task_id="task-9"))
    assert ctx is not None
    _ctx_repo, agent_run_id, attempt_id = ctx
    row = repo.agent_run_for_run_id("run-9")
    assert str(row["agent_run_id"]) == agent_run_id
    assert str(row["current_attempt_id"]) == attempt_id


def test_authority_context_broken_repo_returns_fail_closed_fault(tmp_path):
    """G4（探针复现）：权威库故障 → AUTHORITY_FENCE_FAULT（fail-closed），
    不是 None —— None 会让调用方跳过 fence 继续执行 handler（fail-open）。

    修复前：except 把库异常静默降级成 None（且 RuntimeError 不在捕获
    元组里会直接冒泡）；现在任何库故障都显式返回哨兵，调用方看到即拒。
    """
    class _Broken:
        def record_run_creation(self, **kwargs):
            raise RuntimeError("boom")

        def agent_run_for_run_id(self, run_id):
            raise RuntimeError("boom")

    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=_Broken()))
    assert authority_context(agent, _params(run_id="run-x")) is AUTHORITY_FENCE_FAULT


def test_execute_traced_tool_call_fails_closed_when_authority_db_broken():
    """G4：权威库故障时 handler 被 TOOL_AUTHORITY_FENCE 拦截，零执行。"""
    class _BrokenRepo:
        def record_run_creation(self, **kwargs):
            raise RuntimeError("db broken")

        def agent_run_for_run_id(self, run_id):
            raise RuntimeError("db broken")

    tools = _FenceTools()
    agent = SimpleNamespace(
        tools=tools,
        subagents=SimpleNamespace(runtime_db=_BrokenRepo()),
    )
    params = _params(run_id="run-fault", task_id="task-fault")
    call = _fence_call(run_id="run-fault")
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=ToolCallExecuteParams(params, 1, 1, call),
        call=call,
    )

    execution = execute_traced_tool_call(request)

    assert not execution.result.ok
    assert execution.result.error_code == "TOOL_AUTHORITY_FENCE"
    assert not execution.result.handler_executed
    assert not tools.handler_called


# -------------------------------------------------- open_authority_operation


def test_open_authority_operation_creates_and_marks_executing(tmp_path):
    repo = _repo(tmp_path)
    ctx = authority_context(_agent(repo), _params(run_id="run-1"))
    assert ctx is not None
    _ctx_repo, agent_run_id, attempt_id = ctx

    operation_id, block = open_authority_operation(
        repo, agent_run_id=agent_run_id, attempt_id=attempt_id, tool_name="write_file"
    )

    assert operation_id and block == ""
    op = repo.get_operation(operation_id)
    assert op["status"] == "EXECUTING"
    assert str(op["attempt_id"]) == attempt_id
    assert str(op["agent_run_id"]) == agent_run_id
    assert int(op["tool_operation_generation"]) >= 1


def test_open_authority_operation_blocks_stale_attempt(tmp_path):
    """F.7：attempt 已不是 current pointer → handler 前拒绝（fail-closed）。"""
    repo = _repo(tmp_path)
    ctx = authority_context(_agent(repo), _params(run_id="run-1"))
    assert ctx is not None
    _ctx_repo, agent_run_id, attempt_id = ctx
    repo.create_attempt(agent_run_id)  # 推进 pointer，旧 attempt 失去执行权

    operation_id, block = open_authority_operation(
        repo, agent_run_id=agent_run_id, attempt_id=attempt_id, tool_name="write_file"
    )

    assert operation_id == ""
    assert "authority fence 拒绝执行" in block
    assert repo.operations_for_attempt(attempt_id) == []  # 无操作行残留


# ------------------------------------------------- close_authority_operation


def test_close_authority_operation_settles_succeeded(tmp_path):
    repo = _repo(tmp_path)
    ctx = authority_context(_agent(repo), _params(run_id="run-1"))
    assert ctx is not None
    _ctx_repo, agent_run_id, attempt_id = ctx
    operation_id, _block = open_authority_operation(
        repo, agent_run_id=agent_run_id, attempt_id=attempt_id, tool_name="write_file"
    )

    close_authority_operation(repo, operation_id, ok=True)

    assert repo.get_operation(operation_id)["status"] == "SUCCEEDED"


def test_close_authority_operation_settles_failed(tmp_path):
    repo = _repo(tmp_path)
    ctx = authority_context(_agent(repo), _params(run_id="run-1"))
    assert ctx is not None
    _ctx_repo, agent_run_id, attempt_id = ctx
    operation_id, _block = open_authority_operation(
        repo, agent_run_id=agent_run_id, attempt_id=attempt_id, tool_name="write_file"
    )

    close_authority_operation(repo, operation_id, ok=False)

    assert repo.get_operation(operation_id)["status"] == "FAILED"


def test_close_authority_operation_missing_operation_swallows(tmp_path):
    """缺失操作行/已 settle：尽力而为，吞掉不崩任务。"""
    repo = _repo(tmp_path)
    close_authority_operation(repo, "no-such-op", ok=True)
    close_authority_operation(repo, "", ok=True)  # 空 id 也直接返回


# ------------------------------------------------------------ 主链接线


def test_execute_traced_tool_call_wires_authority_fence(tmp_path):
    """主链工具执行：建权威链 → 每次调用一条操作行 → handler → settle。"""
    repo = _repo(tmp_path)
    tools = _FenceTools()
    agent = SimpleNamespace(tools=tools, subagents=SimpleNamespace(runtime_db=repo))
    params = _params(run_id="run-wire", task_id="task-wire")
    call = _fence_call(run_id="run-wire")
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=ToolCallExecuteParams(params, 1, 1, call),
        call=call,
    )

    execution = execute_traced_tool_call(request)

    assert execution.result.ok
    assert tools.handler_called
    row = repo.agent_run_for_run_id("run-wire")
    assert row is not None
    attempt_id = row["current_attempt_id"]
    ops = repo.operations_for_attempt(attempt_id)
    assert len(ops) == 1
    assert ops[0]["status"] == "SUCCEEDED"  # 结果 ok → settle SUCCEEDED
    assert ops[0]["operation_type"] == "write_file"


def test_execute_traced_tool_call_fence_blocks_stale_attempt(tmp_path):
    """attempt 过期 → TOOL_AUTHORITY_FENCE 拦截，handler 不执行、无残留。"""
    repo = _repo(tmp_path)
    ctx = authority_context(_agent(repo), _params(run_id="run-stale"))
    assert ctx is not None
    _ctx_repo, agent_run_id, _attempt_id = ctx
    tools = _FenceTools(repo=repo, agent_run_id=agent_run_id, pre_advance=True)
    agent = SimpleNamespace(tools=tools, subagents=SimpleNamespace(runtime_db=repo))
    params = _params(run_id="run-stale", task_id="task-stale")
    call = _fence_call(run_id="run-stale")
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=ToolCallExecuteParams(params, 1, 1, call),
        call=call,
    )

    execution = execute_traced_tool_call(request)

    assert not execution.result.ok
    assert execution.result.error_code == "TOOL_AUTHORITY_FENCE"
    assert not execution.result.handler_executed
    assert not tools.handler_called
    row = repo.agent_run_for_run_id("run-stale")
    # 旧/新 attempt 均无操作行残留（fence 拦在 create 第一步）
    for attempt in (row["current_attempt_id"],):
        assert repo.operations_for_attempt(attempt) == []


def test_execute_traced_tool_call_without_authority_keeps_legacy_behavior(tmp_path):
    """无权威库（无 home 上下文）→ 不建链不 fence，原行为不变。"""
    tools = _FenceTools()
    agent = SimpleNamespace(tools=tools)  # 没有 subagents.runtime_db
    params = _params(run_id="run-legacy", task_id="task-legacy")
    call = _fence_call(run_id="run-legacy")
    request = ToolCallRuntimeRequest(
        agent=agent,
        request=ToolCallExecuteParams(params, 1, 1, call),
        call=call,
    )

    execution = execute_traced_tool_call(request)

    assert execution.result.ok
    assert tools.handler_called
    assert tools.gate_outcome is None
