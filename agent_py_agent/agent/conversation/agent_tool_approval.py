# LLM: 主/子后台调用共用原耐久审批账本；main 额外绑定精确 claim，旧路径/schema 保持，展示文案不能授权。
# 模块用途: 把具体工具审批交给所属用户界面；批准、拒绝、取消和失联都回到原调用，不另建审批状态源。

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic,
)
from ..common.opaque_id import validate_opaque_id
from ..contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from .tool_approval_scope import (
    ToolApprovalScope,
    tool_approval_scope,
    tool_approval_scope_is_current,
)

AGENT_TOOL_APPROVAL_SCHEMA = "subagent_tool_approval.v1"
AGENT_TOOL_APPROVAL_CONSUMER_SCHEMA = "subagent_tool_approval_consumer.v1"
AGENT_TOOL_APPROVAL_DISCOVERY_SECONDS = 1.5
AGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS = 15.0
AGENT_TOOL_APPROVAL_POLL_SECONDS = 0.05
AGENT_TOOL_APPROVAL_MAX_PENDING = 32


# LLM: 句柄冻结 canonical 归属、完整请求与 main claim；等待及决定必须核对同一记录，不能换成后续执行。
# 类用途: 保存一条主/子后台审批请求的等待句柄。
@dataclass(frozen=True)
class AgentToolApprovalHandle:
    scope: ToolApprovalScope
    path: Path
    request: ToolApprovalRequest
    created_at: float
    scope_valid: Callable[[], bool] = field(repr=False, compare=False)


# LLM: 主/子后台模型 sink 共用精确审批入口；会话缓存也先校验 canonical 归属，不允许缓存跨 run 或 claim 扩权。
# 类用途: 把审批请求发布到原会话账本，并等待决定、取消或接收方失联。
class AgentToolApprovalSinkMixin:
    agent: object
    thread_id: str
    task_id: str

    # LLM: 先冻结归属再查同作用域缓存；挂起期间租约变化、取消和权限拒绝不能由自主模式覆盖。
    # 函数用途: 上送精确主/子审批，等待原调用的用户决定或同 owner 显式权限模式变化。
    def request_permission(
        self,
        request_value: Mapping[str, object],
        *,
        cancellation_token: object | None = None,
    ) -> dict[str, object]:
        try:
            from ..user_space.approval_mode import autonomous_tool_decision

            request = ToolApprovalRequest.from_mapping(request_value)
            if _cancelled(cancellation_token):
                return ToolApprovalDecision(request.permission_id, "cancelled").to_dict()
            scope = _request_scope(self.agent, self.task_id, self.thread_id, request)
            session_key = f"{scope.run_id}:{scope.claim_id}:{_approval_session_key(request)}"
            approved_keys = getattr(self, "_approved_session_keys", set())
            if session_key and session_key in approved_keys:
                return ToolApprovalDecision(
                    request.permission_id,
                    "approved",
                ).to_dict()
            from ..user_space.operation_grants import (
                owner_operation_granted,
                record_owner_operation_grant,
            )

            grant_key = str(request.binding.get("grant_key") or "").strip()
            if grant_key and owner_operation_granted(getattr(self.agent, "home_paths", None), grant_key):
                # 用户已对这类操作长期允许：不再发布审批，直接以 approved 放行本次精确调用。
                return ToolApprovalDecision(request.permission_id, "approved").to_dict()
            handle = publish_agent_tool_approval(
                self.agent,
                run_id=self.task_id,
                thread_id=self.thread_id,
                request_value=request,
            )
            decision = wait_for_agent_tool_approval(
                handle,
                cancellation_token=cancellation_token,
                mode_decision_provider=partial(autonomous_tool_decision, self.agent),
            )
            if decision.decision == "approved_session" and session_key:
                approved_keys = set(approved_keys)
                approved_keys.add(session_key)
                self._approved_session_keys = approved_keys
            if decision.decision == "approved_owner" and grant_key:
                record_owner_operation_grant(self.agent.home_paths, grant_key, source="agent_tool_approval")
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
            permission_id = str(request_value.get("permission_id") or "").strip()
            if not permission_id:
                raise
            decision = ToolApprovalDecision(permission_id, "unavailable")
        return decision.to_dict()


# LLM: 发布先验证 canonical run/thread/root 与有效执行，再原子创建完整请求；main claim 随记录冻结，不更换旧审批身份。
# 函数用途: 发布一条主/子后台工具审批，供所属用户界面发现。
def publish_agent_tool_approval(
    agent: object,
    *,
    run_id: str,
    thread_id: str,
    request_value: ToolApprovalRequest | Mapping[str, object],
) -> AgentToolApprovalHandle:
    request = _approval_request(request_value)
    scope = _request_scope(agent, run_id, thread_id, request)
    path = _approval_path(agent, scope, request.permission_id)
    created_at = request.requested_at or time.time()
    payload = _pending_payload(scope, request, created_at)
    with locked_json_path(_transition_path(path)):
        report = read_json_object_report(path, context="subagent.tool_approval.publish")
        if report.load_error is not None:
            raise OSError("subagent approval record is unreadable")
        if report.payload and not _same_pending_record(report.payload, payload):
            raise ValueError("subagent approval identity conflict")
        if not report.payload:
            write_json_file_atomic(path, payload)
    return AgentToolApprovalHandle(
        scope, path, request, created_at,
        partial(tool_approval_scope_is_current, agent, scope),
    )


# LLM: 取消优先，执行归属失效先于任何批准；无交互 consumer 关闭式失败，不让后台永久等待或自行放行。
# 函数用途: 等待精确决定或同 owner 模式变化，同时收口取消、换轮与接收方离线。
def wait_for_agent_tool_approval(
    handle: AgentToolApprovalHandle,
    *,
    cancellation_token: object | None = None,
    poll_seconds: float = AGENT_TOOL_APPROVAL_POLL_SECONDS,
    discovery_seconds: float = AGENT_TOOL_APPROVAL_DISCOVERY_SECONDS,
    consumer_lease_seconds: float = AGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS,
    mode_decision_provider: Callable[[ToolApprovalRequest], ToolApprovalDecision | None] | None = None,
) -> ToolApprovalDecision:
    interval = max(0.01, float(poll_seconds or AGENT_TOOL_APPROVAL_POLL_SECONDS))
    while True:
        if _cancelled(cancellation_token):
            _remove_exact_record(handle)
            return ToolApprovalDecision(handle.request.permission_id, "cancelled")
        if not handle.scope_valid():
            _remove_exact_record(handle)
            return ToolApprovalDecision(handle.request.permission_id, "unavailable")
        report = read_json_object_report(handle.path, context="subagent.tool_approval.wait")
        if report.load_error is not None or not report.payload:
            return ToolApprovalDecision(handle.request.permission_id, "unavailable")
        decision = _record_decision(handle, report.payload)
        if decision is not None:
            _remove_exact_record(handle)
            return decision
        if mode_decision_provider is not None:
            decision = mode_decision_provider(handle.request)
            if decision is not None:
                _remove_exact_record(handle)
                return decision
        now = time.time()
        lease_seen_at = _consumer_seen_at(handle.path.parent, handle.scope.root_task_id)
        lease_fresh = now - lease_seen_at <= max(1.0, float(consumer_lease_seconds))
        if not lease_fresh and now - handle.created_at >= max(0.0, float(discovery_seconds)):
            _remove_exact_record(handle)
            return ToolApprovalDecision(handle.request.permission_id, "unavailable")
        time.sleep(interval)


# LLM: 接收方续租只声明一个 root 的交互能力，不是用户批准，不能决定任何挂起调用。
# 函数用途: 记录当前会话的 TUI/Web 正在接收主/子后台审批请求。
def renew_agent_tool_approval_consumer(
    agent: object,
    *,
    root_task_id: str,
    seen_at: float | None = None,
) -> Path:
    root = validate_opaque_id(root_task_id, kind="root_task_id")
    directory = _approval_root(agent, root)
    target = directory / ".consumer.json"
    write_json_file_atomic(
        target,
        {
            "schema_version": AGENT_TOOL_APPROVAL_CONSUMER_SCHEMA,
            "root_task_id": root,
            "seen_at": max(0.0, float(seen_at if seen_at is not None else time.time())),
        },
    )
    return target


# LLM: 有界列表只投影 owner 的一个 root 目录；每行重验原始请求及当前归属，返回公开字段与展示标签。
# 函数用途: 列出当前主任务树里等待用户处理的主/子后台工具审批。
def list_pending_agent_tool_approvals(
    agent: object,
    *,
    root_task_id: str,
    limit: int = AGENT_TOOL_APPROVAL_MAX_PENDING,
) -> list[dict[str, object]]:
    root = validate_opaque_id(root_task_id, kind="root_task_id")
    directory = _approval_root(agent, root)
    rows: list[dict[str, object]] = []
    paths = [
        path
        for path in sorted(directory.glob("*.json"))
        if not path.name.startswith(".")
    ]
    for path in paths[: max(1, int(limit or 1))]:
        report = read_json_object_report(path, context="subagent.tool_approval.list")
        public = _public_pending_record(agent, root, report.payload)
        if public is not None:
            rows.append(public)
    rows.sort(key=lambda item: (float(item.get("requested_at") or 0.0), str(item["run_id"])))
    return rows[: max(1, int(limit or 1))]


# LLM: 决定写回在原记录锁内重读归属与完整请求；main 的 claim 换轮使旧决定失效，文案和行号不授予权限。
# 函数用途: 将用户对主/子后台原调用的决定原子写回同一等待记录。
def resolve_agent_tool_approval(
    agent: object,
    *,
    run_id: str,
    request_value: ToolApprovalRequest | Mapping[str, object],
    decision_value: ToolApprovalDecision | Mapping[str, object],
) -> dict[str, object]:
    scope = tool_approval_scope(agent, run_id)
    request = _approval_request(request_value)
    decision = _approval_decision(decision_value)
    selected = validate_opaque_id(run_id, kind="run_id")
    if request.binding.get("run_id") != selected or decision.permission_id != request.permission_id:
        raise ValueError("subagent approval decision binding mismatch")
    path = _approval_path(agent, scope, request.permission_id)
    with locked_json_path(_transition_path(path)):
        if not tool_approval_scope_is_current(agent, scope):
            raise FileNotFoundError("tool approval execution is no longer active")
        report = read_json_object_report(path, context="subagent.tool_approval.resolve")
        if report.load_error is not None or not _record_matches(
            report.payload,
            scope,
            request,
        ):
            raise FileNotFoundError("pending subagent approval not found")
        prior = _decision_from_payload(report.payload)
        if prior is not None and (
            prior.decision != decision.decision or prior.feedback != decision.feedback
        ):
            raise ValueError("subagent approval already resolved differently")
        payload = dict(report.payload)
        payload["status"] = "decided"
        payload["decision"] = decision.to_dict()
        write_json_file_atomic(path, payload)
    return {
        "ok": True,
        "run_id": selected,
        "permission_id": request.permission_id,
        "decision": decision.decision,
    }


# LLM: 路径只来自已校验归属与 permission 哈希；main/child 沿同一历史目录，公开 permission ID 不成为路径分量。
# 函数用途: 生成一条审批记录的唯一安全路径。
def _approval_path(agent: object, scope: ToolApprovalScope, permission_id: str) -> Path:
    digest = hashlib.sha256(str(permission_id or "").encode("utf-8")).hexdigest()[:24]
    return _approval_root(agent, scope.root_task_id) / f"{scope.run_id}.{digest}.json"


# LLM: ConversationStore 是 Gateway 与主/子 runner 共用的唯一 owner 审批根；原持久路径保留，工作区和正文不能替代它。
# 函数用途: 返回某棵主任务树审批账本的目录。
def _approval_root(agent: object, root_task_id: str) -> Path:
    store = getattr(agent, "conversation_store", None)
    root = getattr(getattr(store, "storage", None), "root", None)
    if root is None:
        raise RuntimeError("subagent approval requires ConversationStore")
    return Path(root) / "subagent_tool_approvals" / root_task_id


# LLM: 发布和缓存使用同一归属校验；明确 thread/run 与 canonical 状态冲突时拒绝，不能猜测或代换身份。
# 函数用途: 核对当前 sink 是否有资格为这一次调用发起审批。
def _request_scope(agent: object, run_id: str, thread_id: str, request: ToolApprovalRequest) -> ToolApprovalScope:
    scope = tool_approval_scope(agent, run_id)
    if not scope.active or scope.thread_id != thread_id or request.binding.get("run_id") != scope.run_id:
        raise ValueError("tool approval execution scope mismatch")
    return scope


# LLM: 记录只复制已脱敏合同与 canonical 身份；main 额外冻结 claim，原 child schema/路径及字段含义保持。
# 函数用途: 构造一条尚未决定的审批记录。
def _pending_payload(
    scope: ToolApprovalScope,
    request: ToolApprovalRequest,
    created_at: float,
) -> dict[str, object]:
    return {
        "schema_version": AGENT_TOOL_APPROVAL_SCHEMA,
        "status": "pending",
        "root_task_id": scope.root_task_id,
        "run_id": scope.run_id,
        "thread_id": scope.thread_id,
        "request": request.to_dict(),
        "created_at": created_at,
        **({"claim_id": scope.claim_id} if scope.claim_id else {}),
    }


# LLM: 投影重新验证归属和 main claim，隐藏终态/旧轮/坏账；代理类型与名称只用于展示，不能授权。
# 函数用途: 将仍属于当前执行的等待记录转换成主/子代理共用的 TUI/Web 行。
def _public_pending_record(
    agent: object,
    root_task_id: str,
    payload: object,
) -> dict[str, object] | None:
    if not isinstance(payload, Mapping) or str(payload.get("status") or "") != "pending":
        return None
    try:
        run_id = validate_opaque_id(str(payload.get("run_id") or ""), kind="run_id")
        request = _approval_request(payload.get("request") if isinstance(payload.get("request"), Mapping) else {})
        scope = tool_approval_scope(agent, run_id)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if not scope.active or scope.root_task_id != root_task_id or not _record_matches(
        payload, scope, request,
    ):
        return None
    return {
        "run_id": run_id,
        "agent_name": scope.agent_name,
        "agent_kind": scope.agent_kind,
        "request": request.to_dict(),
        "requested_at": request.requested_at,
    }


# LLM: 记录比较要求 schema/root/run/request 及 main claim 全部一致；单独相同 permission ID 不构成授权。
# 函数用途: 核验等待记录是否精确对应原调用及原执行租约。
def _record_matches(
    payload: object,
    scope: ToolApprovalScope,
    request: ToolApprovalRequest,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    raw_request = payload.get("request")
    try:
        stored = _approval_request(raw_request if isinstance(raw_request, Mapping) else {})
    except (TypeError, ValueError):
        return False
    return bool(
        payload.get("schema_version") == AGENT_TOOL_APPROVAL_SCHEMA
        and str(payload.get("root_task_id") or "") == scope.root_task_id
        and str(payload.get("run_id") or "") == scope.run_id
        and str(payload.get("thread_id") or "") == scope.thread_id
        and str(payload.get("claim_id") or "") == scope.claim_id
        and stored == request
    )


# LLM: Idempotent publish compares the full pending identity while ignoring only
# the atomically-added decision field, which cannot exist on a pending record.
# 函数用途: 判断磁盘上的等待记录是否就是本次重放请求。
def _same_pending_record(current: object, expected: Mapping[str, object]) -> bool:
    return bool(
        isinstance(current, Mapping)
        and str(current.get("status") or "") == "pending"
        and all(current.get(key) == value for key, value in expected.items())
    )


# LLM: 决定必须匹配句柄冻结的完整归属和请求，才能唤醒原主/子调用；不是当前作用域的记录保持不可用。
# 函数用途: 从当前账本行提取已核验决定，尚未决定时返回 None。
def _record_decision(
    handle: AgentToolApprovalHandle,
    payload: object,
) -> ToolApprovalDecision | None:
    if not _record_matches(
        payload,
        handle.scope,
        handle.request,
    ):
        return ToolApprovalDecision(handle.request.permission_id, "unavailable")
    decision = _decision_from_payload(payload)
    if decision is not None and decision.permission_id != handle.request.permission_id:
        return ToolApprovalDecision(handle.request.permission_id, "unavailable")
    return decision


# LLM: Decision recovery accepts only the closed contract enum and matching
# permission id; malformed data fails closed as unavailable.
# 函数用途: 从账本记录恢复一条已决定结果。
def _decision_from_payload(payload: object) -> ToolApprovalDecision | None:
    if not isinstance(payload, Mapping) or str(payload.get("status") or "") != "decided":
        return None
    raw = payload.get("decision")
    if not isinstance(raw, Mapping):
        return None
    try:
        return ToolApprovalDecision.from_mapping(raw)
    except (TypeError, ValueError):
        return None


# LLM: Lease read validates exact schema/root and returns zero for missing,
# corrupt, or cross-root files; zero can only shorten waiting, never approve.
# 函数用途: 读取最近一次交互界面领取审批请求的时间。
def _consumer_seen_at(directory: Path, root_task_id: str) -> float:
    report = read_json_object_report(directory / ".consumer.json", context="subagent.tool_approval.consumer")
    payload = report.payload
    if (
        report.load_error is not None
        or payload.get("schema_version") != AGENT_TOOL_APPROVAL_CONSUMER_SCHEMA
        or str(payload.get("root_task_id") or "") != root_task_id
    ):
        return 0.0
    try:
        return max(0.0, float(payload.get("seen_at") or 0.0))
    except (TypeError, ValueError):
        return 0.0


# LLM: 清理持有原记录锁，只删除与本等待者完整归属、claim 和请求相同的记录，不回收后续轮审批。
# 函数用途: 消费、取消或无人接收后移除这一条精确临时审批记录。
def _remove_exact_record(handle: AgentToolApprovalHandle) -> None:
    with locked_json_path(_transition_path(handle.path)):
        report = read_json_object_report(handle.path, context="subagent.tool_approval.remove")
        if _record_matches(report.payload, handle.scope, handle.request):
            try:
                handle.path.unlink()
            except OSError:
                return


# LLM: One sibling lock serializes publish, decision, and cleanup for the exact
# hashed permission path without blocking unrelated child requests.
# 函数用途: 返回审批记录对应的跨进程事务锁文件。
def _transition_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.transition")


# LLM: 取消只读结构化令牌字段/方法；Goal paused、异常正文和用户普通文字不能取消或批准工具。
# 函数用途: 判断主/子后台等待审批期间是否收到当前回合的真实取消信号。
def _cancelled(token: object | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    return bool(checker()) if callable(checker) else bool(getattr(token, "cancelled", False))


# LLM: 缓存沿前台的精确工具名与参数哈希，并由调用方加 run/claim 隔离；不跨 sink、执行或参数扩权。
# 函数用途: 生成本次执行内“始终允许同一调用”的参数缓存键。
def _approval_session_key(request: ToolApprovalRequest) -> str:
    tool_name = str(request.binding.get("tool_name") or "").strip()
    args_hash = str(request.binding.get("args_hash") or "").strip()
    return f"{tool_name}:{args_hash}" if tool_name and args_hash else ""


# LLM: Request/decision conversion always re-enters shared contract validation.
# 函数用途: 规范跨进程审批请求对象。
def _approval_request(value: ToolApprovalRequest | Mapping[str, object]) -> ToolApprovalRequest:
    return value if isinstance(value, ToolApprovalRequest) else ToolApprovalRequest.from_mapping(value)


# LLM: Request/decision conversion always re-enters shared contract validation.
# 函数用途: 规范跨进程审批决定对象。
def _approval_decision(value: ToolApprovalDecision | Mapping[str, object]) -> ToolApprovalDecision:
    return value if isinstance(value, ToolApprovalDecision) else ToolApprovalDecision.from_mapping(value)


__all__ = [
    "AGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS",
    "AgentToolApprovalHandle",
    "AgentToolApprovalSinkMixin",
    "list_pending_agent_tool_approvals",
    "publish_agent_tool_approval",
    "renew_agent_tool_approval_consumer",
    "resolve_agent_tool_approval",
    "wait_for_agent_tool_approval",
]
