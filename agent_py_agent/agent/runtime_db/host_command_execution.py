# LLM: 显式宿主命令复用原运行、执行器及操作账；资源释放属于原执行区间，联测插件收尾和重复请求，不重开 UNKNOWN。
# 模块用途: 精确启动原 pending 命令，在本次资源释放后收口原运行，从唯一原操作或未启动事件查询结果。

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace

from ..common.strict_json import load_strict_json
from ..local_storage.tool_operations import TOOL_OPERATION_TERMINAL_STATUSES
from ..tooling.executor import ToolExecutor, ToolExecutorRequest
from ..tooling.models import ToolHandlerOutcome
from ..tooling.runtime_contracts import tool_arguments_hash
from ..tooling.tool_operation_coordinator import replay_completed_tool_operation
from .executor_liveness import attempt_executor, exited_attempt_facts, mark_exited_attempt_unknown
from .host_command_approval import resolve_host_command_approval
from .host_commands import HostCommandBinding, HostCommandIdentity, HostCommandRequest
from .managed_operation_store import ManagedOperationStore
from .operations import RuntimeConflictError
from .repository import RuntimeRepository

_UNSTARTED_SCHEMA = "host_command_not_started.v1"
_LOGGER = logging.getLogger(__name__)


# LLM: 只有原 pending 领取者拥有执行及释放回调；审批与资源收尾都在同一执行区间，重送只读原结果。
#   执行器内产生的未启动拒绝（审批取消/拒绝、前置门失配）必须在 attempt_executor 退出事实落库之前写回执：
#   否则并发的 query_host_command 会在“执行器已退出、回执未到”的窗口里把它投影成 outcome_unknown（2026-09-25 CI 复现）。
# 函数用途: 经原审批/工具链执行一次，先释放调用方本次资源再关闭原运行，不换代或重跑 handler。
def execute_host_command(
    repo: RuntimeRepository, request: HostCommandRequest,
    prepare: Callable[[HostCommandBinding], ToolExecutorRequest],
    *, request_permission: Callable | None = None,
    release_execution: Callable[[], None] | None = None,
) -> dict:
    binding = repo.register_host_command(request)
    try:
        repo.create_attempt(binding.agent_run_id, reuse_pending=True, reject_running=True,
                            expected_pending_attempt_id=binding.attempt_id)
    except RuntimeConflictError:
        attempt = repo.get_attempt(binding.attempt_id)
        if exited_attempt_facts(repo, binding.run_id, binding.attempt_id) or (
            attempt is not None and attempt["status"] in {"done", "failed", "cancelled"}
        ):
            _finalize_execution(repo, binding)
        return query_host_command(repo, request)
    entered_executor = False
    unstarted = None
    try:
        with attempt_executor(repo, binding.run_id, binding.attempt_id), ExitStack() as resources:
            if release_execution is not None:
                resources.callback(release_execution)
            prepared = prepare(binding)
            _validate_execution(binding, prepared)
            original_gate = prepared.pre_handler_gate

            # LLM: gate 接收原执行器补全后的规范输入；失配在操作领取前拒绝，不改原请求摘要。
            # 函数用途: 检查实际即将执行的参数，随后保留调用方原先的前置校验。
            def gate(call):
                if tool_arguments_hash(call.arguments) != "sha256:" + request.input_digest:
                    return ToolHandlerOutcome(call.tool_name, False, "宿主命令参数补全改变了原输入。",
                                              error_code="TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
                return original_gate(call) if original_gate else None

            trusted = dict(prepared.trusted_run_context or {})
            trusted["run_scope"] = {**trusted.get("run_scope", {}), "task_id": binding.task_id}
            prepared = replace(prepared, operation_store=ManagedOperationStore(repo), operation_store_required=True,
                               trusted_run_context=trusted, pre_handler_gate=gate, require_model_visibility=False)
            entered_executor = True
            execution = ToolExecutor().execute(prepared)
            execution = resolve_host_command_approval(prepared, execution, request_id=request.request_id,
                                                       consumer=request_permission)
            if _operation(repo, binding) is None and not execution.result.handler_executed:
                # 执行器登记仍是 running 时就写未启动回执，不给并发查询留下 outcome_unknown 窗口。
                _settle_unstarted(repo, binding, execution.result.reported_error_code,
                                  state="approval_required" if execution.decision.status == "ask" else "rejected")
    except Exception as exc:  # noqa: BLE001 不输出参数或私有路径，进入执行链之后只能核对原账
        _LOGGER.warning("宿主命令执行中断: operation=%s error=%s", request.operation_id, type(exc).__name__)
        if not entered_executor:
            unstarted = ("HOST_COMMAND_PREPARATION_FAILED", "rejected")
    if unstarted is not None:
        _settle_unstarted(repo, binding, unstarted[0], state=unstarted[1])
    _finalize_execution(repo, binding)
    return query_host_command(repo, request)


# LLM: 这里只补原运行收口，绝不重进 handler；纯查询不调用，UNKNOWN 与未决操作沿原退出事实封存。
# 函数用途: 执行结束或显式重送后核对确定结果，处理原操作已结束而运行尚未关闭的中间状态。
def _finalize_execution(repo: RuntimeRepository, binding: HostCommandBinding) -> None:
    record = _operation(repo, binding)
    if record is not None:
        _settle_known_operation(repo, binding, record)
    else:
        attempt = repo.get_attempt(binding.attempt_id)
        if (attempt is not None and attempt["status"] in {"done", "failed", "cancelled"}
                and _unstarted_receipt(repo, binding) is not None):
            _close_task_run(repo, binding)
    facts = exited_attempt_facts(repo, binding.run_id, binding.attempt_id)
    if facts and facts["uncertain_effects"]:
        mark_exited_attempt_unknown(repo, facts)


# LLM: prepare 不能借别的 run/attempt、工具或参数执行；原请求摘要就是本次完整规范工具参数摘要，正文不参与校验。
# 函数用途: 在进入唯一执行器前核对组装结果，失配时保留原 pending 激活后的可诊断记录。
def _validate_execution(binding: HostCommandBinding, prepared: ToolExecutorRequest) -> None:
    call, request = prepared.call, binding.request
    if (call.run_id != binding.run_id or call.attempt_id != binding.attempt_id
            or call.operation_id != request.operation_id or call.tool_name != request.command_name
            or prepared.operation_owner_id != request.owner_id
            or tool_arguments_hash(call.arguments).removeprefix("sha256:") != request.input_digest):
        raise RuntimeConflictError("宿主命令执行身份或输入不符")


# LLM: 查询与终态收口都须沿同一精确原身份读取；记录缺失与坏账不同，不从 current 指针补新 attempt。
# 函数用途: 取得本请求唯一工具行并核对输入指纹。
def _operation(repo: RuntimeRepository, binding: HostCommandBinding):
    return ManagedOperationStore(repo).get_tool_operation(
        owner_id=binding.request.owner_id, run_id=binding.run_id, task_id=binding.task_id,
        attempt_id=binding.attempt_id, operation_id=binding.request.operation_id,
        tool_name=binding.request.command_name, args_hash="sha256:" + binding.request.input_digest,
    )


# LLM: 原操作持久状态与可解码结果共同证明终态；领域回执或 ToolResult.ok 不能越过原操作 UNKNOWN。
# 函数用途: 已知工具结果才关闭原 AgentRun 与 TaskRun，失败关闭不转成另一次执行。
def _settle_known_operation(repo: RuntimeRepository, binding: HostCommandBinding, record) -> None:
    if record.status not in TOOL_OPERATION_TERMINAL_STATUSES:
        return
    if replay_completed_tool_operation(record).error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN":
        return
    row = repo.get_agent_run(binding.agent_run_id)
    attempt = repo.get_attempt(binding.attempt_id)
    if (row is not None and row["current_attempt_id"] == binding.attempt_id
            and row["status"] in {"done", "failed", "cancelled"}
            and attempt is not None and attempt["status"] in {"done", "failed", "cancelled"}):
        _close_task_run(repo, binding)
        return
    result = repo.settle_agent_run(
        agent_run_id=binding.agent_run_id, attempt_id=binding.attempt_id,
        expected_attempt_status="running",
        status={"succeeded": "done", "failed": "failed", "cancelled": "cancelled"}[record.status],
    )
    if result.get("settled") or result.get("reason") == "already_terminal":
        _close_task_run(repo, binding)


# LLM: 无 operation 的前置拒绝只记录到原完成事件；同事务要求本 attempt 仍无工具行，不能伪造 CANCELLED 操作或覆盖实际副作用。
# 函数用途: 保存明确未启动的策略拒绝或审批要求，供后续只读查询。
def _settle_unstarted(
    repo: RuntimeRepository, binding: HostCommandBinding, error_code: str, *, state: str = "rejected",
) -> None:
    result = repo.settle_agent_run(
        agent_run_id=binding.agent_run_id, attempt_id=binding.attempt_id,
        expected_attempt_status="running", require_no_tool_operations=True, status="failed",
        payload={"host_command": {
            "schema_version": _UNSTARTED_SCHEMA, "operation_id": binding.request.operation_id,
            "state": state, "error_code": error_code,
            "handler_executed": False,
        }},
    )
    if result.get("settled"):
        _close_task_run(repo, binding)


# LLM: Task 是长期身份，不写业务完成；原 TaskRun 只在整棵独立宿主命令运行树已终态时关闭。
# 函数用途: 复用原运行树收口，不触发会话唤醒或模型续跑。
def _close_task_run(repo: RuntimeRepository, binding: HostCommandBinding) -> None:
    repo.settle_task_run_if_agent_tree_terminal(
        task_run_id=binding.task_run_id, task_id=binding.task_id,
        operator=binding.request.actor_id, reason="host_command_terminal",
    )


# LLM: 查询完全只读；原正文和工具名只投影原账，UNKNOWN 不暴露未经确认的结果，也不领取执行或对账权。
# 函数用途: 重送、断连后或显式状态命令查回原执行事实及可重放的工具输出。
def query_host_command(repo: RuntimeRepository, identity: HostCommandIdentity) -> dict:
    binding = repo.find_host_command(identity)
    if binding is None:
        return {"state": "not_found", "ok": False}
    record = _operation(repo, binding)
    attempt = repo.get_attempt(binding.attempt_id)
    common = {"request_id": identity.request_id, "operation_id": identity.operation_id,
              "tool_name": binding.request.command_name,
              "attempt_status": attempt["status"] if attempt is not None else "unknown"}
    if record is not None:
        if (record.status == "running" and attempt is not None and attempt["status"] == "running"
                and not exited_attempt_facts(repo, binding.run_id, binding.attempt_id)):
            return {**common, "state": "running", "ok": False}
        result = replay_completed_tool_operation(record)
        if result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN":
            return {**common, "state": "outcome_unknown", "ok": False}
        return {**common, "state": record.status, "ok": result.ok, "error_code": result.error_code,
                "result": result.result_envelope, "output": result.output,
                "finalization_pending": (attempt is None or attempt["status"] not in {"done", "failed", "cancelled"}
                                         or not repo.get_task_run(binding.task_run_id)["closed_at"])}
    rejection = _unstarted_receipt(repo, binding)
    if rejection is not None:
        return {**common, **rejection, "ok": False,
                "finalization_pending": not repo.get_task_run(binding.task_run_id)["closed_at"]}
    if attempt is not None and attempt["status"] == "pending":
        state = "pending"
    elif attempt is not None and attempt["status"] == "running" and not exited_attempt_facts(
        repo, binding.run_id, binding.attempt_id,
    ):
        state = "running"
    else:
        state = "outcome_unknown"
    return {**common, "state": state, "ok": False}


# LLM: 原完成事件仅能证明未启动；遇实际工具行调用方已优先读原 operation，损坏事件不能补成拒绝或成功。
# 函数用途: 从原运行事件读取本请求唯一的前置拒绝事实。
def _unstarted_receipt(repo: RuntimeRepository, binding: HostCommandBinding) -> dict | None:
    with repo._runtime_connection() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runtime_events WHERE event_type='agent_run.completed' "
            "AND agent_run_id=? AND attempt_id=?",
            (binding.agent_run_id, binding.attempt_id),
        ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeConflictError("宿主命令前置回执不唯一")
    try:
        envelope = load_strict_json(rows[0]["payload_json"])
        if not isinstance(envelope, dict):
            raise ValueError("完成事件不是对象")
        payload = envelope.get("host_command")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ValueError("未启动回执不是对象")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeConflictError("宿主命令前置回执损坏") from exc
    if (payload.get("schema_version") != _UNSTARTED_SCHEMA
            or payload.get("operation_id") != binding.request.operation_id
            or payload.get("handler_executed") is not False
            or payload.get("state") not in {"rejected", "approval_required"}
            or not isinstance(payload.get("error_code"), str)):
        raise RuntimeConflictError("宿主命令前置回执不完整")
    return {"state": payload["state"], "error_code": payload["error_code"]}
