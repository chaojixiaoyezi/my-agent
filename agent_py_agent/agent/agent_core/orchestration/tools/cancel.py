# LLM: 模型取消只负责参数、直接父子授权与结果投影；生命周期和固定资源清理由 subagents/cancellation.py 承担。
# 模块用途: 把模型点名取消交给共用子代理控制域，不实现另一套停止协议。
from __future__ import annotations

"""cancel_subagents control tool with TaskStatus-backed status filters."""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....common.audit_activation import structured_audit_source_worker_attributes
from ....runtime_errors import runtime_error_report
from ....subagents.authorization_gate import (
    OperationRequest,
    authorize_direct_child_operation,
    authorize_operation,
)
from ....subagents.cancellation import (
    CancelSubagentTaskRequest,
    cancel_subagent_task,
    cancel_subagent_tree,
    subtree_run_ids,
)
from ....subagents.models import (
    normalize_task_status,
    task_status_in,
)
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
class _ResolveRunIdsResult:
    ok: bool
    run_ids: list[str]
    error_payload: dict[str, object]


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


# LLM: 只遍历已经过授权的目标；开始取消后异常保留 attempted 事实，工具回执不能谎称无副作用。
# 函数用途: 逐个执行取消并保留成功与失败结果，避免部分已停止时被整体写成未开始。
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
            result = _cancel_one(agent, _CancelOneRequest(task=task, params=params))
            if result.get("ok", True):
                cancelled.append(result)
            else:
                failed.append({**result, "cancellation_attempted": True})
        except Exception as exc:  # pragma: no cover - defensive persistence/process edge cases.
            failed.append(
                {
                    "run_id": getattr(task, "id", ""),
                    "cancellation_attempted": True,
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
# 已进入取消的异常可能已经发过信号，不能降为 not_started。
# 函数用途: 生成取消结果；部分失败保留未知，不夹带兄弟、孙代理或整树状态。
def _cancel_payload_result(request: _CancelPayloadRequest) -> ToolHandlerOutcome:
    payload = {
        "ok": request.ok,
        "dry_run": request.dry_run,
        "cancelled": request.cancelled,
        "failed": request.failed,
        "skipped": request.skipped,
    }
    effect_outcome = ""
    if not request.ok:
        attempted = request.cancelled or any(row.get("cancellation_attempted") for row in request.failed)
        effect_outcome = "unknown" if attempted else "not_started"
    return ToolHandlerOutcome(
        "cancel_subagents",
        request.ok,
        json.dumps(payload, ensure_ascii=False, indent=2),
        effect_outcome=effect_outcome,
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
        ids.extend(subtree_run_ids(tasks, root_id))
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
    # 宿主显式单点取消保留原语义；真正的根停止在控制锁内准备整批，再到锁外清理。
    if params.get("cascade_descendants") is False:
        return cancel_subagent_task(agent, cancel_request)
    return cancel_subagent_tree(agent, cancel_request)



def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "CancelSubagentsTool",
    "execute_cancel_subagents",
]
