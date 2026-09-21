# LLM: 生命周期修改复用原状态协议；启动标记在本服务按 launch/attempt 条件写入，CLI 不反向导入领域实现。
# 模块用途: 组织子代理授权、状态与执行轮操作，集中保存准确启动记录，保留各写入口的原事务顺序。
from __future__ import annotations

"""Lifecycle mutation service for subagent task records.

Manual status mutations pass through the current TaskStatus protocol instead
of accepting old success/failure aliases.
"""

import time
from dataclasses import dataclass
from typing import Any

from ...memory_routing import load_routes, match_routes, resolve_required_paths
from ...runtime_db.operations import RuntimeConflictError
from ...runtime_errors import runtime_error_report
from ..capability_request_identity import find_equivalent_capability_request
from ..capability_scope import ensure_grant_path_scope_within_owner
from ..models import (
    SUBAGENT_WAKE_STATUSES,
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    FailureType,
    SubAgentTask,
    TaskStatus,
    VerificationEvidence,
    known_failure_type,
    normalize_task_status,
    task_status_in,
)
from ..process_control import BackgroundStartUpdate, build_background_start_record
from ..utils import _merge_list
from .lifecycle_capability_records import (
    BuildCapabilityGapInput,
    build_capability_gap,
    build_capability_grant,
    build_capability_request,
)
from .lifecycle_runner_attempts import (
    abandon_runner_attempt as abandon_runner_attempt_for_manager,
)
from .lifecycle_runner_attempts import (
    prepare_runner_attempt as prepare_runner_attempt_for_manager,
)
from .lifecycle_runner_attempts import (
    reconcile_dead_runner_attempt as reconcile_dead_runner_attempt_for_manager,
)


@dataclass(frozen=True)
class RecordCapabilityGrantParams:
    """Params bundle for record_capability_grant."""

    request_id: str
    grant_type: str = "generic"
    skills: list[str] | None = None
    tools: list[str] | None = None
    mcp_tools: list[str] | None = None
    command_allowlist: list[str] | None = None
    capability_cards: list[dict[str, str]] | None = None
    reason: str = ""
    constraints: dict[str, str] | None = None
    path_scope: list[str] | None = None
    network_scope: list[str] | None = None
    output_budget: dict[str, object] | None = None
    risk_level: str = ""
    request_scope: dict[str, object] | None = None
    expires_after_task: bool = True
    expires_at: float = 0.0


@dataclass(frozen=True)
class RecordCapabilityGapParams:
    """Params bundle for record_capability_gap."""

    missing_capability: str
    why_failed: str
    request_id: str = ""
    gap_type: str = "generic"
    attempted_skills: list[str] | None = None
    attempted_tools: list[str] | None = None
    needed_outputs: list[str] | None = None
    suggested_skill: str = ""
    suggested_tool: str = ""
    requested_scope: dict[str, object] | None = None
    constraints: dict[str, str] | None = None
    escalation_chain: list[str] | None = None
    next_record_refs: list[str] | None = None


@dataclass(frozen=True)
class RecordCapabilityRequestParams:
    """Params bundle for record_capability_request."""

    problem: str
    needed_capability: str
    expected_output: str = ""
    capability_type: str = "generic"
    tried: list[str] | None = None
    evidence: list[str] | None = None
    constraints: dict[str, str] | None = None
    requested_tools: list[str] | None = None
    requested_skills: list[str] | None = None
    requested_mcp_tools: list[str] | None = None
    requested_commands: list[str] | None = None
    cwd_scope: list[str] | None = None
    path_scope: list[str] | None = None
    network_scope: list[str] | None = None
    output_budget: dict[str, object] | None = None
    risk_level: str = ""
    alternatives_attempted: list[str] | None = None
    escalation_target: str = ""


@dataclass(frozen=True)
class RecordEvidenceParams:
    """Params bundle for record_evidence."""

    kind: str
    summary: str
    command: str = ""
    path: str = ""
    url: str = ""
    ok: bool = True


@dataclass(frozen=True)
class SetStatusParams:
    """Params bundle for set_status."""

    run_id: str
    status: str
    result: str = ""
    failure_type: str = ""


# LLM: 公开生命周期服务供宿主调用；启动条件写在原 creation/canonical 锁下完成，不能另选身份或在锁里启动进程。
# 类用途: 管理子代理授权、状态和准确执行轮，让 CLI 与调度器共用正式状态写入口。
class SubAgentLifecycleService:
    """Mutate lifecycle fields on subagent tasks through SubAgentManager."""

    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: Equivalent capability requests are deduplicated against the latest canonical state in
    # one mutate transaction; request prose never acts as lifecycle authority beyond identity data.
    # 函数用途: 原子登记子代理的能力申请，并复用已经存在的等价申请避免重复排队。
    def record_capability_request(
        self,
        run_id: str,
        params: RecordCapabilityRequestParams,
    ) -> CapabilityRequest:
        committed_request: list[CapabilityRequest] = []

        # LLM: Equivalence and append must observe the latest canonical request list under one
        # guard; otherwise two simultaneous tool calls can create duplicate OPEN requests.
        # 函数用途: 在权威锁内复用等价能力申请，确实没有时才新增一条。
        def _request_reducer(task: SubAgentTask) -> None:
            request = find_equivalent_capability_request(task.capability_requests, params)
            if request is None:
                request = build_capability_request(run_id, params)
                task.capability_requests.append(request)
                task.updated_at = time.time()
            committed_request.append(request)

        self.manager.mutate(run_id, _request_reducer)
        return committed_request[0]

    # LLM: A grant and its exact request resolution are one lifecycle mutation. Ordinary and MCP
    # tool names must both enter persisted effective permissions while retaining typed audit fields.
    # 函数用途: 给子代理授权时同步结清申请、合并普通/MCP 工具，并让阻塞轮次重进待执行队列。
    def record_capability_grant(
        self,
        run_id: str,
        params: RecordCapabilityGrantParams,
    ) -> CapabilityGrant:
        committed_grant: list[CapabilityGrant] = []

        # LLM: This reducer executes only inside persistence.mutate and may touch capability fields
        # plus the grant timestamp; it must not perform network calls or nested canonical saves.
        # 函数用途: 在权威锁内幂等写入一条授权、结清对应申请并合并允许的工具和技能。
        def _grant_reducer(task: SubAgentTask) -> None:
            grant = next(
                (
                    item
                    for item in task.capability_grants
                    if str(item.request_id or "").strip()
                    == str(params.request_id or "").strip()
                ),
                None,
            )
            if grant is None:
                grant = build_capability_grant(run_id, params)
            ensure_grant_path_scope_within_owner(
                self.manager,
                task,
                list(getattr(grant, "path_scope", []) or []),
            )
            if grant not in task.capability_grants:
                task.capability_grants.append(grant)
            for request in task.capability_requests:
                if str(request.id or "").strip() == str(params.request_id or "").strip():
                    current_status = str(request.status or "").strip()
                    if current_status not in {"OPEN", "GRANTED"}:
                        raise ValueError(
                            f"capability_request_already_resolved:{current_status}"
                        )
                    request.status = "GRANTED"
            task.allowed_skills = _merge_list(task.allowed_skills, grant.skills)
            # MCP 名称和普通工具名称最终都进入模型的同一工具快照；保留 grant.mcp_tools
            # 独立账本字段的同时，也必须把它并入 task 的持久有效权限，供孙代理继承上限。
            task.allowed_tools = _merge_list(
                task.allowed_tools,
                [*grant.tools, *grant.mcp_tools],
            )
            task.updated_at = time.time()
            committed_grant.append(grant)

        task = self.manager.mutate(run_id, _grant_reducer)
        _reopen_capability_blocked_conversation_link(self.manager, task)
        return committed_grant[0]

    # LLM: An existing grant may resolve another request only after structural coverage. Reapply
    # both ordinary/MCP effective tools under the canonical guard without duplicating the grant.
    # 函数用途: 用已有授权原子结清另一条申请，并恢复其普通/MCP 权限和来源关联。
    def resolve_request_with_existing_grant(
        self,
        run_id: str,
        request_id: str,
        grant_id: str,
    ) -> CapabilityGrant:
        committed_grant: list[CapabilityGrant] = []

        # LLM: This reducer trusts only exact ids supplied by the structured router and confirms
        # both records still exist in the latest canonical state before changing request status.
        # 函数用途: 在权威锁内核对申请和已有授权，写入覆盖关系并合并实际允许能力。
        def _existing_grant_reducer(task: SubAgentTask) -> None:
            grant = next(
                (item for item in task.capability_grants if item.id == grant_id),
                None,
            )
            request = next(
                (item for item in task.capability_requests if item.id == request_id),
                None,
            )
            if grant is None or request is None:
                raise ValueError("capability_existing_grant_resolution_missing_record")
            ensure_grant_path_scope_within_owner(
                self.manager,
                task,
                list(getattr(grant, "path_scope", []) or []),
            )
            current_status = str(request.status or "").strip()
            if current_status not in {"OPEN", "GRANTED"}:
                raise ValueError(
                    f"capability_request_already_resolved:{current_status}"
                )
            request.status = "GRANTED"
            constraints = dict(request.constraints or {})
            constraints["covered_by_grant_id"] = grant.id
            request.constraints = constraints
            task.allowed_skills = _merge_list(task.allowed_skills, grant.skills)
            task.allowed_tools = _merge_list(
                task.allowed_tools,
                [*grant.tools, *grant.mcp_tools],
            )
            task.updated_at = time.time()
            committed_grant.append(grant)

        task = self.manager.mutate(run_id, _existing_grant_reducer)
        _reopen_capability_blocked_conversation_link(self.manager, task)
        return committed_grant[0]

    # LLM: A gap and its exact request terminal status are one mutation. Memory-route discovery is
    # computed before the lock, while only typed records and paths are committed inside it.
    # 函数用途: 原子登记能力缺口并结清对应申请，同时挂入后续需要读取的记忆规则路径。
    def record_capability_gap(
        self,
        run_id: str,
        params: RecordCapabilityGapParams,
    ) -> CapabilityGap:
        task = self.manager.load(run_id)
        injected_rule_paths, memory_routes = self._match_memory_routes(params.missing_capability, params.why_failed, task)
        gap = build_capability_gap(
            BuildCapabilityGapInput(run_id, task.goal, params, memory_routes, injected_rule_paths)
        )
        committed_gap: list[CapabilityGap] = []

        # LLM: Gap append and exact request resolution share one canonical reducer. A request that
        # already reached another terminal decision rejects this late conflicting resolution.
        # 函数用途: 原子记录能力缺口、结清对应申请并合并需要继续读取的规则路径。
        def _gap_reducer(current: SubAgentTask) -> None:
            existing = next(
                (
                    item
                    for item in current.capability_gaps
                    if params.request_id
                    and str(item.request_id or "").strip()
                    == str(params.request_id or "").strip()
                ),
                None,
            )
            selected = existing or gap
            if existing is None:
                current.capability_gaps.append(gap)
            for request in current.capability_requests:
                if str(request.id or "").strip() != str(params.request_id or "").strip():
                    continue
                current_status = str(request.status or "").strip()
                if current_status not in {"OPEN", "GAP"}:
                    raise ValueError(
                        f"capability_request_already_resolved:{current_status}"
                    )
                request.status = "GAP"
            if injected_rule_paths:
                current.context_manifest.required_read_paths = _merge_list(
                    current.context_manifest.required_read_paths,
                    injected_rule_paths,
                )
            current.updated_at = time.time()
            committed_gap.append(selected)

        self.manager.mutate(run_id, _gap_reducer)
        return committed_gap[0]

    def record_evidence(
        self,
        run_id: str,
        params: RecordEvidenceParams,
    ) -> VerificationEvidence:
        task = self.manager.load(run_id)
        evidence = VerificationEvidence(
            kind=params.kind,
            summary=params.summary,
            command=params.command,
            path=params.path,
            url=params.url,
            ok=params.ok,
            created_at=time.time(),
        )
        task.evidence.append(evidence)
        task.verification_status = "VERIFIED" if params.ok else "FAILED"
        task.updated_at = time.time()
        self.manager.save(task)
        return evidence

    def touch_heartbeat(self, run_id: str) -> None:
        task = self.manager.load(run_id)
        task.heartbeat_at = time.time()
        task.updated_at = task.heartbeat_at
        self.manager.save(task)

    def set_status(
        self,
        params: str | SetStatusParams,
        status: str = "",
        *,
        result: str = "",
        failure_type: str = "",
    ) -> SubAgentTask:
        if isinstance(params, SetStatusParams):
            status_params = params
        else:
            status_params = SetStatusParams(
                run_id=params,
                status=status,
                result=result,
                failure_type=failure_type,
            )

        task = self.manager.load(status_params.run_id)
        normalized = normalize_task_status(status_params.status)
        task.status = normalized
        if status_params.result:
            task.result = status_params.result
        known_failure = known_failure_type(status_params.failure_type)
        if known_failure:
            task.failure_type = known_failure
        if task_status_in(normalized, SUBAGENT_WAKE_STATUSES):
            task.ended_at = time.time()
        task.updated_at = time.time()
        self.manager.save(task)
        return task

    # LLM: 条件比较与窄 mutation 同在 creation guard；launch/attempt/status 三者匹配才写，停止后的回执不得重新标 running。
    # 函数用途: 两种后台执行方式共用一个启动记录写入口，保留新任务字段和新进程身份。
    def update_background_start(
        self, run_id: str, update: BackgroundStartUpdate, *, channel_failure: bool = False,
    ) -> None:
        from ..runner_start import assert_expected_runner_attempt

        manager = self.manager
        with manager.creation_guard():
            assert_expected_runner_attempt(manager, run_id, update.attempt_id, pending_only=update.replace_launch)

            # LLM: reducer 只读锁内最新 canonical 任务；抛错时不写文件，不能恢复旧 task 整体快照。
            # 函数用途: 原子校验这份回执的归属，并合并它负责的启动字段。
            def apply(task):
                previous = (task.attributes or {}).get("background_start") or {}
                if task_status_in(task.status, {"CANCELLED", "ABANDONED", "TAKEN_OVER"}):
                    raise RuntimeConflictError("任务已关闭，拒绝旧启动回执")
                if update.replace_launch:
                    if previous.get("status") in {"launching", "running"}:
                        raise RuntimeConflictError("已有启动尚未回收，拒绝重复接纳")
                    if manager.runtime_db is None and previous.get("launch_id") != update.launch_id:
                        raise RuntimeConflictError("文件启动不能更换原预留的宿主")
                elif (
                    previous.get("launch_id") != update.launch_id
                    or previous.get("attempt_id", "") != update.attempt_id
                    or previous.get("status") == "reclaimed"
                    or previous.get("status") in {"finished", "failed"} and update.status == "running"
                ):
                    raise RuntimeConflictError("后台启动回执已失效")
                task.attributes = dict(task.attributes or {})
                task.attributes["background_start"] = build_background_start_record(previous, update)
                if channel_failure:
                    task.status = TaskStatus.CHANNEL_ERROR.value
                    task.channel_status = "BROKEN"
                    task.failure_type = FailureType.BACKGROUND_DISPATCH_STARTUP.value
                    task.result = update.error

            manager.mutate(run_id, apply)

    # LLM: 派工调用方须传排队时的 expected_attempt_id；委托原生命周期实现，不在此重新查询 current。
    # 函数用途: 为子代理取得准确执行轮，直接调用与后台启动共用同一激活规则。
    def prepare_runner_attempt(
        self, run_id: str, *, retry_reason: str = "", expected_attempt_id: str | None = None,
    ) -> SubAgentTask:
        return prepare_runner_attempt_for_manager(
            self.manager, run_id, retry_reason=retry_reason, expected_attempt_id=expected_attempt_id,
        )

    def abandon_runner_attempt(self, run_id: str, attempt_id: str, *, reason: str = "") -> SubAgentTask:
        return abandon_runner_attempt_for_manager(self.manager, run_id, attempt_id, reason=reason)

    # LLM: This is the only task-lifecycle bridge from a dead runner-session
    # projection to runtime.db attempt authority; ready=False forbids requeue.
    # 函数用途: 在孤儿监督重新排队前，确认旧 runner attempt 已经安全结算。
    def reconcile_dead_runner_attempt(
        self,
        run_id: str,
        attempt_id: str,
        *,
        reason: str = "",
    ) -> dict[str, object]:
        return reconcile_dead_runner_attempt_for_manager(
            self.manager,
            run_id,
            attempt_id,
            reason=reason,
        )

    def _match_memory_routes(
        self,
        missing_capability: str,
        why_failed: str,
        task: SubAgentTask,
    ) -> tuple[list[str], list[dict[str, str]]]:
        route_query = " ".join([missing_capability, why_failed, task.goal]).strip()
        if not route_query:
            return [], []
        try:
            index_path = (self.manager.workspace_root / "memory" / "routing" / "INDEX.md").resolve()
            if not index_path.exists():
                return [], []
            routes = load_routes(index_path)
            matches = match_routes(route_query, routes, limit=5)
            resolution = resolve_required_paths(matches, mode="strict", auto_read_limit=3)
            injected_rule_paths = list(dict.fromkeys([*resolution.required_read_paths, *resolution.candidate_paths]))
            memory_routes = [
                {
                    "route_id": match.route.route_id,
                    "source_file": match.route.authority_file(),
                    "inject_mode": match.route.inject_mode,
                }
                for match in matches
            ]
            return injected_rule_paths, memory_routes
        except Exception as exc:
            report = runtime_error_report(exc, context="capability_gap.memory_routes")
            return [], [_memory_route_load_error(report)]


# LLM: This projection update follows only the exact grant path. Canonical persistence may already
# have reduced BLOCKED to PENDING; both states are accepted here, while expected_status=blocked
# prevents a concurrent cancel/stop from being resurrected.
# 函数用途: 能力授权落盘后，仅把对应子代理被阻塞的会话链接恢复为可继续调度状态。
def _reopen_capability_blocked_conversation_link(manager: Any, task: SubAgentTask) -> None:
    """Reactivate only the exact blocked child link unlocked by a grant.

    Runner completion mirrors ``BLOCKED`` into the conversation task link.  A
    capability grant makes that same run eligible for its follow-up attempt,
    so the link must move back to ``active`` before the conversation lifecycle
    gate evaluates it.  The expected-status compare prevents a concurrent
    ``/stop`` or terminal transition from being resurrected.
    """

    task_status = str(getattr(task, "status", "") or "").strip().upper()
    if task_status not in {"BLOCKED", "PENDING"}:
        return
    if task_status == "BLOCKED" and (
        str(getattr(task, "failure_type", "") or "").strip().lower()
        != "capability_request"
    ):
        return
    store = getattr(manager, "conversation_store", None)
    update = getattr(getattr(store, 'tasks', None), 'update_status', None)
    if not callable(update):
        return
    try:
        update(
            {
                "task_id": str(getattr(task, "id", "") or ""),
                "status": "active",
                "expected_status": "blocked",
            }
        )
    except Exception as exc:
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["capability_grant_conversation_reopen_error"] = runtime_error_report(
            exc,
            context="record_capability_grant.conversation.update_task_status",
        )
        task.attributes = attrs
        manager.save(task)


def _memory_route_load_error(report: dict[str, object]) -> dict[str, str]:
    return {
        "route_id": "_memory_route_load_error",
        "source_file": "",
        "inject_mode": "diagnostic",
        "context": str(report.get("context") or "capability_gap.memory_routes"),
        "category": str(report.get("category") or ""),
        "error_type": str(report.get("error_type") or ""),
        "message": str(report.get("message") or ""),
    }
