from __future__ import annotations

"""cancel_subagents control tool with TaskStatus-backed status filters."""

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....common.audit_activation import structured_audit_source_worker_attributes
from ....concurrency.interrupt import interrupt_by_name
from ....runtime_errors import runtime_error_report
from ....subagents.authorization_gate import (
    OperationRequest,
    authorize_direct_child_operation,
    authorize_operation,
)
from ....subagents.model_capabilities import capability_request_requires_parent_resolution
from ....subagents.models import (
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    FailureType,
    normalize_task_status,
    task_status_in,
)
from ....subagents.process_control import terminate_pid_with_escalation
from ....subagents.runner_session_liveness import has_fresh_runner_session, runner_session_of
from ....tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolRuntimePolicy,
)
from ..create_policy import current_orchestration_requester_run_id
from ..tool_specs import build_cancel_subagents_model_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent
    from ....subagents.models import SubAgentTask


@dataclass(frozen=True)
class _CancelPayloadRequest:
    ok: bool
    cancelled: list[dict[str, object]]
    failed: list[dict[str, object]]
    skipped: list[dict[str, object]]
    dry_run: bool


@dataclass(frozen=True)
class _CancelOneRequest:
    task: SubAgentTask
    params: dict[str, object]


@dataclass(frozen=True)
class CancelSubagentTaskRequest:
    task: SubAgentTask
    reason: str
    kill_process: bool = True
    source: str = "runtime"


@dataclass(frozen=True)
class _ResolveRunIdsResult:
    ok: bool
    run_ids: list[str]
    error_payload: dict[str, object]


@dataclass(frozen=True)
class _CancellationContext:
    reason: str
    source: str
    now: float
    attempt_id: str
    pid_report: dict[str, object]
    closed_request_ids: list[str]
    findings_ledger: str
    findings_recorded: int


@dataclass(frozen=True)
class _ListRunsForCancelResult:
    ok: bool
    tasks: list[SubAgentTask]
    error_payload: dict[str, object]


@dataclass(frozen=True)
class _StatusFilterResult:
    ok: bool
    statuses: set[str]
    error_payload: dict[str, object]


# LLM: 模型侧 cancel 只接受 run_id/run_ids，并通过直接父子授权门；子树和状态
# 批量取消仍由下面的宿主级 canonical primitive 保留，不能重新暴露进模型 schema。
# 类用途: 让当前代理按名字停止自己的一个或多个直接子代理。
class CancelSubagentsTool(BaseTool):
    model_spec = build_cancel_subagents_model_spec()
    runtime_policy = ToolRuntimePolicy(
        # LLM: Model calls always perform a real direct-child interruption;
        # host-only dry runs bypass this model ToolRuntime entirely.
        # 配置用途: 模型的取消调用统一按真实副作用审批。
        effect_resolver=EffectResolverPolicy("dangerous"),
        idempotency_policy=IdempotencyPolicy("operation"),
        # seq 253 闭合：run_id/run_ids 是逻辑 ID（任务标识），不是物理
        # 路径——不标 logical 会被 scope 投影 resolve 成 workspace 假写根，与
        # workspace_root 物理重叠 → RuntimeConflictError → store_unavailable。
        resource_scopes=ResourceScopePolicy(
            parameter_names=("run_id", "run_ids"),
            parameter_kinds={"run_id": "logical", "run_ids": "logical"},
            # seq 266 #1：run_id/run_ids 是同一 agent_run 资源的两个参数入口
            # （handler 的 _explicit_run_ids 归一），必须锁同一 scope——
            # 否则 cancel(run_id=r-1) 与 cancel(run_ids=[r-1]) 可同时过锁。
            resource_domains={
                "run_id": "agent_run",
                "run_ids": "agent_run",
            },
        ),
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: 拒绝宿主私有的扫描/进程参数，并在执行任何取消副作用前一次性
    # 校验全部显式目标都是当前请求方的直接下级。
    # 函数用途: 校验模型点名的直接子代理，再调用统一取消实现落状态和审计。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        private_parameters = [
            name
            for name in ("root_id", "status", "kill_process", "dry_run")
            if name in params
        ]
        if private_parameters:
            return _cancel_failure(
                "模型只能用 run_id/run_ids 点名停止自己的直接下级；"
                f"宿主私有参数不可用：{', '.join(private_parameters)}。",
                "TOOL_INVALID_ARGUMENTS",
            )
        for run_id in _explicit_run_ids(params):
            try:
                authorize_direct_child_operation(
                    self.agent.subagents,
                    OperationRequest(
                        operation="cancel",
                        run_id=run_id,
                        requester_owner=_requester_owner(self.agent),
                        requester_run_id=_model_requester_run_id(self.agent),
                    ),
                )
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                return _cancel_failure(str(exc), "TOOL_PERMISSION_DENIED")
        return execute_cancel_subagents(self.agent, params)


# LLM: Direct-parent interruption is authoritative once authorization passes.
# Automatic retry eligibility may inform scheduling but must never veto the
# same parent edge's explicit interrupt, matching 会话运行时 active-turn control.
# 函数用途: 中断点名的下级运行并持久化取消事实，不做轮询、推动或质量验收。
def execute_cancel_subagents(
    agent: SimpleAgent,
    params: dict[str, object],
) -> ToolHandlerOutcome:
    """Run canonical cancellation; model calls add Tool Gateway authority around it."""
    run_ids_result = _resolve_run_ids(agent, params)
    if not run_ids_result.ok:
        payload = run_ids_result.error_payload
        return _cancel_failure(
            json.dumps(payload, ensure_ascii=False, indent=2),
            _cancel_error_code(payload),
        )
    run_ids = run_ids_result.run_ids
    if not run_ids:
        return _cancel_failure(
            "缺少 run_id/run_ids/root_id/status，未取消任何子代理。",
            "TOOL_PARAMETER_REQUIRED",
        )
    status_filter_result = _status_filter(params.get("status"))
    if not status_filter_result.ok:
        payload = status_filter_result.error_payload
        return _cancel_failure(
            json.dumps(payload, ensure_ascii=False, indent=2),
            _cancel_error_code(payload),
        )
    targets = _filter_existing_targets(
        agent,
        run_ids,
        status_filter_result.statuses,
    )
    system_managed = _system_managed_source_workers(targets)
    if system_managed:
        payload = {
            "ok": False,
            "error_code": "AUDIT_SOURCE_WORKER_SYSTEM_MANAGED",
            "error": (
                "Audit 来源工作者由命名 Audit 的持久生命周期和租约控制器管理；"
                "cancel_subagents 不能把保证来源改写成永久取消。"
            ),
            "protected_runs": system_managed,
            "next_action": {
                "control": "audit_named_clear",
                "reason": "只有精确命名 Audit 的 clear 或父任务终止才能关闭来源工作者。",
            },
        }
        return _cancel_failure(
            json.dumps(payload, ensure_ascii=False, indent=2),
            "AUDIT_SOURCE_WORKER_SYSTEM_MANAGED",
        )
    if bool(params.get("dry_run")):
        return _cancel_payload_result(
            _CancelPayloadRequest(
                True,
                _dry_run_targets(targets),
                [],
                [],
                True,
            ),
        )
    return _execute_cancel_targets(agent, params, run_ids, targets)


def _execute_cancel_targets(
    agent: SimpleAgent,
    params: dict[str, object],
    run_ids: list[str],
    targets: list[dict[str, object]],
) -> ToolHandlerOutcome:
    cancelled: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    for item in targets:
        task = item.get("task")
        if task is None:
            failed.append(
                {
                    "run_id": item.get("run_id", ""),
                    "error": item.get("error", "load_failed"),
                }
            )
            continue
        try:
            cancelled.append(
                _cancel_one(agent, _CancelOneRequest(task=task, params=params))
            )
        except Exception as exc:  # pragma: no cover - defensive persistence/process edge cases.
            failed.append(
                {
                    "run_id": getattr(task, "id", ""),
                    **runtime_error_report(
                        exc,
                        context="cancel_subagents.cancel_one",
                    ),
                }
            )
    target_ids = {str(item.get("run_id", "")) for item in targets}
    skipped = [
        {"run_id": run_id, "reason": "status_filter_or_missing"}
        for run_id in run_ids
        if run_id not in target_ids
    ]
    return _cancel_payload_result(
        _CancelPayloadRequest(
            not failed,
            cancelled,
            failed,
            skipped,
            False,
        ),
    )


# LLM: Cancel receipts report only named outcomes. Returning the whole tree
# would recreate inspect-by-dry-run through a mutating control tool.
# 函数用途: 生成取消结果，不夹带兄弟、孙代理或整树状态。
def _cancel_payload_result(request: _CancelPayloadRequest) -> ToolHandlerOutcome:
    payload = {
        "ok": request.ok,
        "dry_run": request.dry_run,
        "cancelled": request.cancelled,
        "failed": request.failed,
        "skipped": request.skipped,
    }
    return ToolHandlerOutcome(
        "cancel_subagents",
        request.ok,
        json.dumps(payload, ensure_ascii=False, indent=2),
        effect_outcome=(
            "not_started"
            if not request.ok and not request.cancelled
            else ""
        ),
    )


def _resolve_run_ids(agent: SimpleAgent, params: dict[str, object]) -> _ResolveRunIdsResult:
    explicit = _explicit_run_ids(params)
    root_id = str(params.get("root_id") or "").strip()
    status_filter_result = _status_filter(params.get("status"))
    if not status_filter_result.ok:
        return _ResolveRunIdsResult(False, [], status_filter_result.error_payload)
    status_filter = status_filter_result.statuses
    ids = list(explicit)
    if root_id:
        tasks_result = _list_runs_for_cancel(agent)
        if not tasks_result.ok:
            return _ResolveRunIdsResult(False, [], tasks_result.error_payload)
        tasks = tasks_result.tasks
        ids.extend(_subtree_ids(tasks, root_id))
    if status_filter and not explicit and not root_id:
        tasks_result = _list_runs_for_cancel(agent)
        if not tasks_result.ok:
            return _ResolveRunIdsResult(False, [], tasks_result.error_payload)
        tasks = tasks_result.tasks
        ids.extend(str(task.id) for task in tasks if task_status_in(task.status, status_filter))
    return _ResolveRunIdsResult(True, _dedupe(ids), {})


def _list_runs_for_cancel(agent: SimpleAgent) -> _ListRunsForCancelResult:
    try:
        tasks = agent.subagents.list_runs()
    except Exception as exc:
        return _ListRunsForCancelResult(
            False,
            [],
            {"ok": False, "error": runtime_error_report(exc, context="cancel_subagents.list_runs")},
        )
    return _ListRunsForCancelResult(True, tasks, {})


def _explicit_run_ids(params: dict[str, object]) -> list[str]:
    ids: list[str] = []
    single = str(params.get("run_id") or "").strip()
    if single:
        ids.append(single)
    raw_many = params.get("run_ids")
    if isinstance(raw_many, str):
        ids.extend(part.strip() for part in raw_many.split(","))
    elif isinstance(raw_many, list):
        ids.extend(str(part).strip() for part in raw_many)
    return [item for item in ids if item]


def _cancel_failure(output: str, error_code: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "cancel_subagents",
        False,
        output,
        error_code=error_code,
        effect_outcome="not_started",
    )


def _cancel_error_code(error_payload: dict[str, object]) -> str:
    """把 resolve/status 错误 payload 映射到准确分类码（避免无码兜底成 UNKNOWN_ERROR）。

    - invalid_status_filter：status 取值非法，改参数可修 → TOOL_INVALID_ARGUMENTS。
    - 其余（list_runs 抛错产出的 runtime_error_report）：运行时查询异常 → TOOL_EXECUTION_FAILED(可重试)。
    """
    if str(error_payload.get("error") or "") == "invalid_status_filter":
        return "TOOL_INVALID_ARGUMENTS"
    return "TOOL_EXECUTION_FAILED"


def _status_filter(value: object) -> _StatusFilterResult:
    if not value:
        return _StatusFilterResult(True, set(), {})
    statuses: set[str] = set()
    invalid: list[str] = []
    for item in _status_filter_values(value):
        try:
            statuses.add(normalize_task_status(item))
        except ValueError:
            invalid.append(str(item))
    if invalid:
        return _StatusFilterResult(
            False,
            set(),
            {
                "ok": False,
                "error": "invalid_status_filter",
                "invalid_statuses": invalid,
                "message": "status 只接受当前 TaskStatus 协议值。",
            },
        )
    return _StatusFilterResult(True, statuses, {})


def _status_filter_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _subtree_ids(tasks: list[SubAgentTask], root_id: str) -> list[str]:
    by_id = {str(task.id): task for task in tasks}
    children: dict[str, list[str]] = {}
    for task in tasks:
        parent_id = str(getattr(task, "parent_id", "") or "")
        if parent_id:
            children.setdefault(parent_id, []).append(str(task.id))
    found: list[str] = []
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        if current in found:
            continue
        if current in by_id:
            found.append(current)
        queue.extend(children.get(current, []))
    return found


def _filter_existing_targets(agent: SimpleAgent, run_ids: list[str], status_filter: set[str]) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for run_id in run_ids:
        item = _load_cancel_target(agent, run_id)
        task = item.get("task")
        if task is None:
            targets.append({"run_id": run_id, "error": item.get("error")})
            continue
        if status_filter and not task_status_in(getattr(task, "status", ""), status_filter):
            continue
        targets.append(item)
    return targets


def _load_cancel_target(agent: SimpleAgent, run_id: str) -> dict[str, object]:
    try:
        # 3.txt B.4：cancel 走统一授权查询门（owner 一致性 + 子树可见性）。
        task = authorize_operation(
            agent.subagents,
            OperationRequest(
                operation="cancel",
                run_id=run_id,
                requester_owner=_requester_owner(agent),
                # 取消工具在 execute() 入口已按稳定父级做过直接下级校验；实际
                # 落状态时必须复用同一身份。普通主代理后续消息会换 gwreq，
                # 但它仍是创建这些 child 的同一个 conversation task。
                requester_run_id=_model_requester_run_id(agent),
            ),
        )
        return {"run_id": run_id, "task": task}
    except Exception as exc:
        return {"run_id": run_id, "task": None, "error": runtime_error_report(exc, context="cancel_subagents.load")}


def _requester_owner(agent: SimpleAgent) -> str:
    return str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")


# LLM: 子代理线程身份优先于外层 gateway request；主代理没有 runner 身份时才
# 使用当前请求 run/task/request id，避免孙代理把自己误认成根主控。
# 函数用途: 返回模型侧递归控制动作的当前代理身份。
def _model_requester_run_id(agent: SimpleAgent) -> str:
    return current_orchestration_requester_run_id(agent)


# LLM: A model-facing generic cancellation tool cannot terminate the
# system-managed worker that carries an Audit source guarantee. Exact named
# Audit control and lifecycle reconcilers call ``cancel_subagent_task`` directly.
# 中文说明：模型可调用的通用取消工具不能关闭承担 Audit 来源保证的系统工作者；
# 精确命名 clear 和父任务生命周期控制器仍复用底层统一取消原语。
def _system_managed_source_workers(
    targets: list[dict[str, object]],
) -> list[dict[str, object]]:
    protected: list[dict[str, object]] = []
    for item in targets:
        task = item.get("task")
        attrs = getattr(task, "attributes", {}) if task is not None else {}
        if not structured_audit_source_worker_attributes(attrs):
            continue
        protected.append(
            {
                "run_id": str(getattr(task, "id", "") or ""),
                "status": str(getattr(task, "status", "") or ""),
                "worker_key": str(attrs.get("audit_source_worker_key") or ""),
                "watch_id": str(attrs.get("audit_source_watch_id") or ""),
            }
        )
    return protected


def _dry_run_targets(targets: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in targets:
        task = item.get("task")
        if task is None:
            result.append({"run_id": item.get("run_id", ""), "error": item.get("error", "load_failed")})
            continue
        result.append({"run_id": getattr(task, "id", ""), "status": getattr(task, "status", "")})
    return result


def _cancel_one(agent: SimpleAgent, request: _CancelOneRequest) -> dict[str, object]:
    params = request.params
    reason = str(params.get("reason") or "cancel_subagents").strip()
    cancel_request = CancelSubagentTaskRequest(
        request.task,
        reason,
        bool(params.get("kill_process", True)),
        "cancel_subagents",
    )
    # 根 /stop 已在 owner-local creation guard 内取得完整 request lineage；它会把
    # 每个 exact run 传进来，不能在非可重入 guard 里再次展开同一子树。
    if params.get("cascade_descendants") is False:
        return cancel_subagent_task(agent, cancel_request)
    return cancel_subagent_tree(agent, cancel_request)


# LLM: Closing one authorized agent means closing its live spawn branch, matching 会话运行时
# shutdown_agent_tree.  Broadcast exact runner-attempt interrupts across the known branch before
# durable closeout so slow model calls do not serialize cancellation latency; then fence creation,
# rescan canonical parent_id lineage, and close late descendants without touching ancestors or
# siblings.
# 函数用途: 终态关闭一个子代理及其仍可能运行或恢复的后代；先广播精确中断，再等待派工落盘并逐项写取消状态。
def cancel_subagent_tree(
    agent: SimpleAgent,
    request: CancelSubagentTaskRequest,
) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    guard = getattr(manager, "creation_guard", None)
    target_id = str(getattr(request.task, "id", "") or "").strip()

    # 会话运行时 sends a native Shutdown to each live thread before forgetting it.  Our runner closeout
    # also writes several durable projections, so signalling only inside that sequential write loop
    # makes N slow model calls stop one after another.  Snapshotting here is only an interrupt
    # fan-out: the guarded canonical rescan below remains the authority for which nodes are closed.
    pre_signalled: dict[str, str] = {
        target_id: _signal_subagent_attempt(agent, request.task),
    }
    try:
        initial_tasks = list(manager.list_runs())
    except Exception:
        initial_tasks = []
    initial_by_id = {
        str(getattr(task, "id", "") or "").strip(): task for task in initial_tasks
    }
    for run_id in _subtree_ids(initial_tasks, target_id)[1:]:
        task = initial_by_id.get(run_id)
        if task is None or task_status_in(
            getattr(task, "status", ""),
            SUBAGENT_RECOVERY_CLOSED_STATUSES,
        ):
            continue
        pre_signalled[run_id] = _signal_subagent_attempt(agent, task)

    root_result = cancel_subagent_task(
        agent,
        request,
        pre_signalled_thread_interrupt=pre_signalled.get(target_id),
    )

    # LLM: This inner pass runs only after the target token has been signalled.  It reads the
    # canonical ledger while creation is fenced and closes leaves before their parents; completed
    # or already-handled descendants remain untouched and continue to carry their real result.
    # 函数用途: 在创建事务稳定后找出目标分支的后代，并把仍可运行或恢复的节点逐个收口。
    def close_descendants() -> dict[str, object]:
        try:
            tasks = list(manager.list_runs())
        except Exception as exc:
            return {
                "cancelled": [],
                "skipped": [],
                "failed": [runtime_error_report(exc, context="cancel_subagents.descendants")],
            }
        descendant_ids = list(reversed(_subtree_ids(tasks, target_id)[1:]))
        by_id = {str(getattr(task, "id", "") or "").strip(): task for task in tasks}
        cancelled: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        failed: list[dict[str, object]] = []
        for run_id in descendant_ids:
            task = by_id.get(run_id)
            if task is None:
                continue
            if task_status_in(
                getattr(task, "status", ""),
                SUBAGENT_RECOVERY_CLOSED_STATUSES,
            ):
                skipped.append(
                    {"run_id": run_id, "status": str(getattr(task, "status", "") or "")}
                )
                continue
            try:
                thread_interrupt = pre_signalled.get(run_id)
                if thread_interrupt is None:
                    # This node landed after the first snapshot while a nested create transaction
                    # was already in flight.  Signal it before its durable terminal writes too.
                    thread_interrupt = _signal_subagent_attempt(agent, task)
                cancelled.append(
                    cancel_subagent_task(
                        agent,
                        CancelSubagentTaskRequest(
                            task=task,
                            reason=request.reason,
                            kill_process=request.kill_process,
                            source=request.source,
                        ),
                        pre_signalled_thread_interrupt=thread_interrupt,
                    )
                )
            except Exception as exc:
                failed.append(
                    {
                        "run_id": run_id,
                        **runtime_error_report(
                            exc,
                            context="cancel_subagents.cancel_descendant",
                        ),
                    }
                )
        return {"cancelled": cancelled, "skipped": skipped, "failed": failed}

    if callable(guard):
        with guard():
            descendant_result = close_descendants()
    else:
        descendant_result = close_descendants()
    return {**root_result, "descendants": descendant_result}


# LLM: This exact-run primitive signals one execution token or process before persisting its
# attempt/terminal state. Branch-aware user/model close calls must use cancel_subagent_tree;
# root-request stop may call this primitive for its already-resolved full lineage while holding
# the non-reentrant creation guard.
# 函数用途: 精确打断子代理执行体、暂停自身 Goal，再收口执行轮和会话链接；不自行遍历后代。
def cancel_subagent_task(
    agent: SimpleAgent,
    request: CancelSubagentTaskRequest,
    *,
    pre_signalled_thread_interrupt: str | None = None,
) -> dict[str, object]:
    """Cancel one canonical run through the same lifecycle path as /stop tooling."""
    task = request.task
    reason = request.reason
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    # 会话运行时/终端交互 都先触发当前执行体的 cancellation token/AbortController，再做
    # durable 收尾。这里同样先打断慢模型或工具，让它尽快释放 canonical state 锁；
    # 否则 TUI 的停止请求会先堵在 attempt 落盘上，用户看到“结果无法确认”。
    pid_report = _terminate_task_pid(agent, task, request.kill_process)
    if pid_report.get("status") == "no_pid":
        # 线程形态没有 pid 可杀:走协作中断,工具循环在下个安全点体面收工。
        pid_report["thread_interrupt"] = (
            pre_signalled_thread_interrupt
            if pre_signalled_thread_interrupt is not None
            else _interrupt_dispatch_thread(agent, task.id, attempt_id)
        )
    elif pre_signalled_thread_interrupt not in {None, "", "not_found"}:
        # 共享/独占进程形态也可能同时注册精确 attempt token。保留这一事实，既避免
        # 误杀同宿主兄弟，也让审计能证明模型调用已先收到协作中断。
        pid_report["thread_interrupt"] = pre_signalled_thread_interrupt
    if attempt_id:
        task = agent.subagents.lifecycle.abandon_runner_attempt(task.id, attempt_id, reason=reason)
    now = time.time()
    closed_request_ids = _close_pending_capability_requests(task, reason)
    findings_ledger, findings_recorded = _findings_ledger_snapshot(task)
    context = _CancellationContext(
        reason,
        request.source,
        now,
        attempt_id,
        pid_report,
        closed_request_ids,
        findings_ledger,
        findings_recorded,
    )
    task.attributes = _cancelled_attributes(task, context)
    from ....conversation.goal_delegation import transition_delegated_goal

    transition_delegated_goal(agent.subagents, task, expected_status="active", status="paused")
    return _persist_cancelled_task(agent, task, context)


# LLM: This helper only signals the exact active attempt and never writes lifecycle state. Tree
# cancellation may call it for several related runs before any slow durable closeout; callers must
# still use cancel_subagent_task to settle attempts, capability requests, projections and links.
# 函数用途: 快速向一个子代理当前执行轮发送中断，用于树停止的第一阶段广播，不单独改变任务终态。
def _signal_subagent_attempt(agent: SimpleAgent, task: SubAgentTask) -> str:
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    return _interrupt_dispatch_thread(
        agent,
        str(getattr(task, "id", "") or ""),
        attempt_id,
    )


# LLM: Cancellation writes both the control receipt and the terminal runner-session projection in
# one canonical save, so liveness readers cannot observe CANCELLED alongside a live lease.
# 函数用途: 生成取消审计属性，并把当前 runner 会话同步标成已取消结束。
def _cancelled_attributes(task: SubAgentTask, context: _CancellationContext) -> dict[str, object]:
    attrs = dict(getattr(task, "attributes", {}) or {})
    attrs["cancel_subagents"] = {
        "cancel_status": "CANCELLED",
        "source": context.source,
        "reason": context.reason,
        "cancelled_at": context.now,
        "previous_status": str(getattr(task, "status", "") or ""),
        "previous_failure_type": str(getattr(task, "failure_type", "") or ""),
        "abandoned_attempt_id": context.attempt_id,
        "pid_report": context.pid_report,
        "closed_capability_request_ids": context.closed_request_ids,
        # 增量结论账指针:取消了结 run,不了结它已确认的结论——账在哪、几条,随回执带给主代理。
        "findings_ledger": context.findings_ledger,
        "findings_recorded": context.findings_recorded,
    }
    session = attrs.get("runner_session")
    if isinstance(session, dict):
        terminal_session = dict(session)
        terminal_session["status"] = "cancelled"
        terminal_session["heartbeat_at"] = context.now
        terminal_session["ended_at"] = context.now
        attrs["runner_session"] = terminal_session
    return attrs


def _persist_cancelled_task(
    agent: SimpleAgent,
    task: SubAgentTask,
    context: _CancellationContext,
) -> dict[str, object]:
    # 结构化取消 = CANCELLED(中性"了结"),不是 ABANDONED(烂尾)。CANCELLED 已补进
    # SUBAGENT_HANDLED_TERMINAL_STATUSES,继承终态语义(recovery 不再捡、compaction 不续传),
    # 行为等价旧 ABANDONED;但状态名不再把"完成产物后收尾取消"误显成失败。ABANDONED 只留给
    # startup_recovery 崩溃调和(进程已死)那种名副其实的烂尾。
    task.status = "CANCELLED"
    task.failure_type = FailureType.CANCELLED.value
    task.ended_at = context.now
    task.updated_at = context.now
    task.runner_active_attempt_id = ""
    runtime_authority = _settle_cancelled_runtime_authority(agent, task, context)
    agent.subagents.save(task)
    agent.subagents.actions._append_task_work_log(
        task,
        f"cancel_subagents: status=CANCELLED reason={context.reason}",
    )
    conversation_link = _sync_cancelled_conversation_link(agent, task.id)
    return {
        "run_id": task.id,
        "status": task.status,
        "cancel_status": "CANCELLED",
        "abandoned_attempt_id": context.attempt_id,
        "pid_report": context.pid_report,
        "conversation_link": conversation_link,
        "runtime_authority": runtime_authority,
        "findings_ledger": context.findings_ledger,
        "findings_recorded": context.findings_recorded,
    }


# LLM: RuntimeDB is the managed run authority. A cancellation may arrive after a coordinator has
# already yielded its current attempt while waiting for descendants, so the runner exception path
# is not guaranteed to settle the run. Fence the exact current attempt here before writing the task
# projection; terminal conflicts must surface instead of leaving RuntimeDB at ``created``.
# 函数用途: 在统一取消入口把受管子代理的权威 AgentRun 收口为 cancelled；无权威库的本地兼容模式保持原样。
def _settle_cancelled_runtime_authority(
    agent: SimpleAgent,
    task: SubAgentTask,
    context: _CancellationContext,
) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    repo = getattr(manager, "runtime_db", None)
    run_id = str(getattr(task, "id", "") or "").strip()
    if repo is None:
        return {"status": "not_managed", "run_id": run_id}
    row = repo.agent_run_for_run_id(run_id)
    if row is None:
        return {"status": "not_registered", "run_id": run_id}
    agent_run_id = str(row["agent_run_id"] or "").strip()
    recorded_attempt_id = str(row["current_attempt_id"] or "").strip()
    requested_attempt_id = str(context.attempt_id or "").strip()
    authority_attempt_id = requested_attempt_id or recorded_attempt_id
    if requested_attempt_id and requested_attempt_id != recorded_attempt_id:
        current_attempt = repo.get_attempt(recorded_attempt_id)
        current_status = (
            str(current_attempt["status"] or "").strip().lower()
            if current_attempt is not None
            else ""
        )
        current_ended_at = (
            float(current_attempt["ended_at"] or 0.0)
            if current_attempt is not None
            else 0.0
        )
        # 创建时登记的 pending attempt 尚未交给 runner，没有模型、工具或副作用可被
        # 旧投影遗漏；可以直接取消该唯一未启动轮。若 current 已运行或已换成其它终态，
        # 必须保留 stale-attempt 闸，不能拿旧 task 快照停止一个未被信号触达的新执行者。
        if current_status == "pending" and current_ended_at == 0:
            authority_attempt_id = recorded_attempt_id
    result = repo.settle_agent_run(
        agent_run_id=agent_run_id,
        status="cancelled",
        attempt_id=authority_attempt_id,
        now=context.now,
        payload={
            "status": "cancelled",
            "runtime_status": "cancelled",
            "runtime_reason": context.reason,
            "runtime_source": context.source,
            "run_id": run_id,
        },
    )
    if bool(result.get("settled")):
        return {
            "status": "cancelled",
            "run_id": run_id,
            "agent_run_id": agent_run_id,
            "attempt_id": authority_attempt_id,
        }
    refreshed = repo.agent_run_for_run_id(run_id)
    refreshed_status = str(refreshed["status"] or "") if refreshed is not None else ""
    if result.get("reason") == "already_terminal" and refreshed_status == "cancelled":
        return {
            "status": "cancelled",
            "run_id": run_id,
            "agent_run_id": agent_run_id,
            "attempt_id": authority_attempt_id,
            "replayed": True,
        }
    raise RuntimeError(
        "子代理取消未能收口权威 AgentRun："
        f"run_id={run_id} reason={result.get('reason') or 'unknown'} "
        f"current_status={refreshed_status or 'missing'}"
    )


def _sync_cancelled_conversation_link(agent: SimpleAgent, task_id: str) -> dict[str, object]:
    """Retire an existing conversation task link without inventing a binding."""
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return {"status": "unavailable"}
    try:
        link = store.update_task_status({"task_id": task_id, "status": "cancelled"})
    except Exception as exc:
        return {
            "status": "error",
            "error": runtime_error_report(
                exc,
                context="cancel_subagents.conversation_link",
            ),
        }
    return {"status": "updated" if link is not None else "not_linked"}


# 函数用途: 取消时刻给出该 run 增量结论账的指针与行数(结构化事实,内容不判定)——
#   主代理据此在整合报告里合并被取消路的已确认结论,取消不再等于结论丢失。
def _findings_ledger_snapshot(task: SubAgentTask) -> tuple[str, int]:
    path_text = str(getattr(task, "agent_run_findings_jsonl", "") or "").strip()
    if not path_text:
        return "", 0
    try:
        with open(path_text, encoding="utf-8") as handle:
            return path_text, sum(1 for line in handle if line.strip())
    except OSError:
        return path_text, 0


# LLM: 取消=对该子代理一切未决事项的"了结":它挂着的 OPEN 能力申请永远不会再被执行,
#   留着会让父代理持续把一个已了结的子代理视为待裁决。CLOSED 是协议现有终态，
#   原因落 constraints 审计。
# 函数用途: 取消时把该子代理仍需父级裁决的申请逐条置 CLOSED,返回被关闭的申请 id。
def _close_pending_capability_requests(task: SubAgentTask, reason: str) -> list[str]:
    closed: list[str] = []
    for request in getattr(task, "capability_requests", None) or []:
        if not capability_request_requires_parent_resolution(getattr(request, "status", "OPEN")):
            continue
        request.status = "CLOSED"
        constraints = dict(getattr(request, "constraints", {}) or {})
        constraints["denial_reason"] = f"subagent_cancelled: {reason}"
        request.constraints = constraints
        closed.append(str(getattr(request, "id", "") or ""))
    return [item for item in closed if item]


# LLM: 进程终止统一走 subagents/process_control 的两阶段原语(SIGTERM 组→宽限→
#   SIGKILL 升级),与出口孤儿回收同一手法;本函数只负责"要不要杀"的参数裁决。
# 函数用途: 取消任务时按 kill_process 参数决定是否连后台进程一起收掉。
# LLM: Prefer the exact runner-attempt cancellation token registered by the worker. The batch
# dispatch thread is only a legacy fallback and must never be interrupted while it hosts siblings.
# 函数用途: 先精确停止目标子代理这一执行轮；旧运行记录才回退到共享派工线程的安全判定。
def _interrupt_dispatch_thread(
    agent: SimpleAgent,
    run_id: str,
    attempt_id: str,
) -> str:
    normalized_attempt_id = str(attempt_id or "").strip()
    if normalized_attempt_id and interrupt_by_name(
        f"subagent-runner-attempt:{run_id}:{normalized_attempt_id}"
    ):
        return "attempt_signaled"
    registry = getattr(agent, "_background_subagent_dispatches", None)
    if not isinstance(registry, dict):
        return "not_found"
    for entry in registry.values():
        data = entry if isinstance(entry, dict) else {}
        run_ids = [str(item or "") for item in (data.get("run_ids") or [])]
        if run_id not in run_ids:
            continue
        siblings = _active_shared_host_siblings(agent, run_id, run_ids)
        if siblings is None:
            return "shared_host_unknown"
        if siblings:
            return "shared_host_cooperative"
        if interrupt_by_name(str(data.get("thread_name") or "")):
            return "signaled"
    return "not_found"


def _terminate_task_pid(
    agent: SimpleAgent,
    task: SubAgentTask,
    kill_process: bool,
) -> dict[str, object]:
    pid = _task_pid(task)
    if not pid:
        return {"status": "no_pid"}
    if not kill_process:
        return {"status": "skipped", "pid": pid}
    siblings = _active_subprocess_host_siblings(agent, task, pid)
    if siblings is None:
        return {
            "status": "shared_host_unknown",
            "pid": pid,
            "escalated": False,
        }
    if siblings:
        # A subagents-dispatch process may host several runner attempts. The
        # task attempt is fenced immediately after this process decision, so
        # this runner stops at its next model/tool/commit boundary. Killing the
        # shared PID here would also abort unrelated live tasks and force
        # avoidable orphan recovery.
        return {
            "status": "shared_host_cooperative",
            "pid": pid,
            "escalated": False,
            "shared_run_ids": siblings,
        }
    return terminate_pid_with_escalation(pid)


def _active_subprocess_host_siblings(
    agent: SimpleAgent,
    task: SubAgentTask,
    pid: int,
) -> list[str] | None:
    manager = getattr(agent, "subagents", None)
    if not callable(getattr(manager, "list_runs", None)):
        return None
    try:
        tasks = manager.list_runs()
    except Exception:
        # Process exclusivity is a destructive-action prerequisite. If the
        # canonical task ledger cannot be read, fail closed to attempt fencing.
        return None
    current_id = str(getattr(task, "id", "") or "")
    siblings: list[str] = []
    for candidate in tasks:
        candidate_id = str(getattr(candidate, "id", "") or "")
        if not candidate_id or candidate_id == current_id:
            continue
        if not task_status_in(getattr(candidate, "status", ""), {"RUNNING"}):
            continue
        if not has_fresh_runner_session(candidate):
            continue
        session = runner_session_of(candidate)
        if session.get("in_process") is not False:
            continue
        try:
            candidate_pid = int(session.get("worker_pid") or 0)
        except (TypeError, ValueError):
            continue
        if candidate_pid == pid:
            siblings.append(candidate_id)
    return sorted(set(siblings))


def _active_shared_host_siblings(
    agent: SimpleAgent,
    run_id: str,
    shared_run_ids: list[str],
) -> list[str] | None:
    candidate_ids = [
        candidate_id
        for candidate_id in shared_run_ids
        if candidate_id and candidate_id != run_id
    ]
    # The dispatch registry is the authority for which run ids share this
    # in-process host.  A single-run host is therefore proven exclusive without
    # consulting the task ledger; requiring an unrelated manager here would
    # turn a safe immediate interrupt into a false ``shared_host_unknown``.
    if not candidate_ids:
        return []
    manager = getattr(agent, "subagents", None)
    if not callable(getattr(manager, "load", None)):
        return None
    siblings: list[str] = []
    for candidate_id in candidate_ids:
        try:
            candidate = manager.load(candidate_id)
        except Exception:
            return None
        if task_status_in(getattr(candidate, "status", ""), {"RUNNING"}):
            siblings.append(candidate_id)
    return sorted(set(siblings))


def _task_pid(task: SubAgentTask) -> int:
    runner_session = runner_session_of(task) if has_fresh_runner_session(task) else {}
    # An in-process runner is a thread inside the Gateway process.  Its
    # worker_pid identifies the host process for liveness only; signalling it
    # would terminate every user and task sharing that Gateway.  会话运行时 and
    # 长期助手 interrupt in-process agents cooperatively and reserve OS signals
    # for separately spawned workers, so make the persisted topology fact the
    # authority before considering any legacy PID mirrors.
    # Only the exact persisted boolean ``False`` authorizes OS signalling.
    # Missing/corrupt topology fails closed to cooperative cancellation.
    if not runner_session or runner_session.get("in_process") is not False:
        return 0
    # The fresh subprocess session is the sole authority for its OS PID.
    # Legacy mirrors can be stale or can name the shared Gateway host, so they
    # must never recover a destructive signal path when the session is absent.
    try:
        pid = int(runner_session.get("worker_pid") or 0)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "CancelSubagentTaskRequest",
    "CancelSubagentsTool",
    "cancel_subagent_task",
    "cancel_subagent_tree",
    "execute_cancel_subagents",
]
