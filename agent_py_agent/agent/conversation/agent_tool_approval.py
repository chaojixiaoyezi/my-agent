"""Conversation-owned approval bridge for one exact delegated-agent tool call."""

# LLM: This module is the sole durable authority for child-to-owner tool approval
# handoff. One JSON record owns pending/decided state; TUI/Web projections may
# read it, but only an owner-authorized control service may write a decision.
# 模块用途: 让子代理把具体工具审批上送给所属用户界面，等待精确决定后原地续跑同一次调用。

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic,
)
from ..common.opaque_id import validate_opaque_id
from ..contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

SUBAGENT_TOOL_APPROVAL_SCHEMA = "subagent_tool_approval.v1"
SUBAGENT_TOOL_APPROVAL_CONSUMER_SCHEMA = "subagent_tool_approval_consumer.v1"
SUBAGENT_TOOL_APPROVAL_DISCOVERY_SECONDS = 1.5
SUBAGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS = 15.0
SUBAGENT_TOOL_APPROVAL_POLL_SECONDS = 0.05
SUBAGENT_TOOL_APPROVAL_MAX_PENDING = 32


# LLM: The handle freezes canonical root/run/path/request identity before any
# waiting starts; callers cannot swap a later request into an existing record.
# 类用途: 保存一条已发布子代理审批请求的精确等待句柄。
@dataclass(frozen=True)
class SubagentToolApprovalHandle:
    root_task_id: str
    run_id: str
    path: Path
    request: ToolApprovalRequest
    created_at: float


# LLM: The mixin is attached only to child/background model sinks. It publishes
# one exact request through this module and blocks the original ToolCall until a
# typed owner decision, cancellation, or missing interactive consumer resolves it.
# 类用途: 给子代理模型回调补上工具审批入口，不在展示类里复制跨进程控制逻辑。
class SubagentToolApprovalSinkMixin:
    agent: object
    thread_id: str
    task_id: str

    # LLM: The returned mapping is the shared ToolApprovalDecision contract;
    # exceptions fail closed as unavailable and never become an implicit grant.
    # 函数用途: 把子代理当前具体工具调用送到所属用户界面审批并等待结果。
    def request_permission(
        self,
        request_value: Mapping[str, object],
        *,
        cancellation_token: object | None = None,
    ) -> dict[str, object]:
        try:
            request = ToolApprovalRequest.from_mapping(request_value)
            session_key = _approval_session_key(request)
            approved_keys = getattr(self, "_subagent_approved_session_keys", set())
            if session_key and session_key in approved_keys:
                return ToolApprovalDecision(
                    request.permission_id,
                    "approved",
                ).to_dict()
            handle = publish_subagent_tool_approval(
                self.agent,
                run_id=self.task_id,
                thread_id=self.thread_id,
                request_value=request,
            )
            decision = wait_for_subagent_tool_approval(
                handle,
                cancellation_token=cancellation_token,
            )
            if decision.decision == "approved_session" and session_key:
                approved_keys = set(approved_keys)
                approved_keys.add(session_key)
                self._subagent_approved_session_keys = approved_keys
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
            permission_id = str(request_value.get("permission_id") or "").strip()
            if not permission_id:
                raise
            decision = ToolApprovalDecision(permission_id, "unavailable")
        return decision.to_dict()


# LLM: Publication validates the child's canonical task/root and exact request
# binding, then atomically creates or idempotently reuses one pending record.
# 函数用途: 发布一条子代理工具审批请求，供所属用户界面发现。
def publish_subagent_tool_approval(
    agent: object,
    *,
    run_id: str,
    thread_id: str,
    request_value: ToolApprovalRequest | Mapping[str, object],
) -> SubagentToolApprovalHandle:
    task, root_task_id = _task_and_root(agent, run_id)
    request = _approval_request(request_value)
    selected = str(getattr(task, "id", "") or "")
    if request.binding.get("run_id") != selected:
        raise ValueError("subagent approval binding run mismatch")
    path = _approval_path(agent, root_task_id, selected, request.permission_id)
    created_at = request.requested_at or time.time()
    payload = _pending_payload(
        root_task_id,
        selected,
        str(thread_id or "").strip(),
        request,
        created_at,
    )
    with locked_json_path(_transition_path(path)):
        report = read_json_object_report(path, context="subagent.tool_approval.publish")
        if report.load_error is not None:
            raise OSError("subagent approval record is unreadable")
        if report.payload and not _same_pending_record(report.payload, payload):
            raise ValueError("subagent approval identity conflict")
        if not report.payload:
            write_json_file_atomic(path, payload)
    return SubagentToolApprovalHandle(root_task_id, selected, path, request, created_at)


# LLM: Waiting accepts only the exact record and typed decision. A structured
# cancellation wins, while an absent interactive consumer fails closed instead
# of leaving an IM/background child permanently blocked.
# 函数用途: 等待用户界面的批准或拒绝；界面不存在/离线时安全返回 unavailable。
def wait_for_subagent_tool_approval(
    handle: SubagentToolApprovalHandle,
    *,
    cancellation_token: object | None = None,
    poll_seconds: float = SUBAGENT_TOOL_APPROVAL_POLL_SECONDS,
    discovery_seconds: float = SUBAGENT_TOOL_APPROVAL_DISCOVERY_SECONDS,
    consumer_lease_seconds: float = SUBAGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS,
) -> ToolApprovalDecision:
    interval = max(0.01, float(poll_seconds or SUBAGENT_TOOL_APPROVAL_POLL_SECONDS))
    while True:
        if _cancelled(cancellation_token):
            _remove_exact_record(handle)
            return ToolApprovalDecision(handle.request.permission_id, "cancelled")
        report = read_json_object_report(handle.path, context="subagent.tool_approval.wait")
        if report.load_error is not None or not report.payload:
            return ToolApprovalDecision(handle.request.permission_id, "unavailable")
        decision = _record_decision(handle, report.payload)
        if decision is not None:
            _remove_exact_record(handle)
            return decision
        now = time.time()
        lease_seen_at = _consumer_seen_at(handle.path.parent, handle.root_task_id)
        lease_fresh = now - lease_seen_at <= max(1.0, float(consumer_lease_seconds))
        if not lease_fresh and now - handle.created_at >= max(0.0, float(discovery_seconds)):
            _remove_exact_record(handle)
            return ToolApprovalDecision(handle.request.permission_id, "unavailable")
        time.sleep(interval)


# LLM: Consumer renewal is an explicit interactive-client capability lease for
# one root task. It is not a user approval and cannot decide any pending call.
# 函数用途: 记录当前会话的 TUI/Web 正在接收子代理审批请求。
def renew_subagent_tool_approval_consumer(
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
            "schema_version": SUBAGENT_TOOL_APPROVAL_CONSUMER_SCHEMA,
            "root_task_id": root,
            "seen_at": max(0.0, float(seen_at if seen_at is not None else time.time())),
        },
    )
    return target


# LLM: Listing is a bounded owner projection over one exact root directory. It
# validates every record and returns public request fields plus agent labels only.
# 函数用途: 列出当前主任务树里等待用户处理的子代理工具审批。
def list_pending_subagent_tool_approvals(
    agent: object,
    *,
    root_task_id: str,
    limit: int = SUBAGENT_TOOL_APPROVAL_MAX_PENDING,
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


# LLM: Resolution reloads the canonical record under its transition lock and
# compares the full ToolApprovalRequest before writing a typed decision. Labels,
# row positions, and client-supplied binding fragments never authorize a call.
# 函数用途: 将用户对指定子代理工具调用的决定原子写回等待记录。
def resolve_subagent_tool_approval(
    agent: object,
    *,
    run_id: str,
    request_value: ToolApprovalRequest | Mapping[str, object],
    decision_value: ToolApprovalDecision | Mapping[str, object],
) -> dict[str, object]:
    _task, root_task_id = _task_and_root(agent, run_id)
    request = _approval_request(request_value)
    decision = _approval_decision(decision_value)
    selected = validate_opaque_id(run_id, kind="run_id")
    if request.binding.get("run_id") != selected or decision.permission_id != request.permission_id:
        raise ValueError("subagent approval decision binding mismatch")
    path = _approval_path(agent, root_task_id, selected, request.permission_id)
    with locked_json_path(_transition_path(path)):
        report = read_json_object_report(path, context="subagent.tool_approval.resolve")
        if report.load_error is not None or not _record_matches(
            report.payload,
            root_task_id,
            selected,
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


# LLM: Paths derive only from validated root/run ids and a permission hash; the
# permission id itself never becomes a filesystem component.
# 函数用途: 生成一条审批记录的唯一安全路径。
def _approval_path(agent: object, root_task_id: str, run_id: str, permission_id: str) -> Path:
    root = validate_opaque_id(root_task_id, kind="root_task_id")
    selected = validate_opaque_id(run_id, kind="run_id")
    digest = hashlib.sha256(str(permission_id or "").encode("utf-8")).hexdigest()[:24]
    return _approval_root(agent, root) / f"{selected}.{digest}.json"


# LLM: ConversationStore is the one owner-scoped durable root shared by Gateway
# and child runners; no task workspace or display transcript may replace it.
# 函数用途: 返回某棵主任务树审批账本的目录。
def _approval_root(agent: object, root_task_id: str) -> Path:
    store = getattr(agent, "conversation_store", None)
    root = getattr(store, "root", None)
    if root is None:
        raise RuntimeError("subagent approval requires ConversationStore")
    return Path(root) / "subagent_tool_approvals" / root_task_id


# LLM: Task loading provides canonical root membership; request text and caller
# path guesses are never used to place or authorize a record.
# 函数用途: 加载指定子代理并返回它真实所属的主任务 ID。
def _task_and_root(agent: object, run_id: str) -> tuple[object, str]:
    selected = validate_opaque_id(run_id, kind="run_id")
    manager = getattr(agent, "subagents", None)
    if manager is None:
        raise RuntimeError("subagent manager unavailable")
    task = manager.load(selected)
    root = validate_opaque_id(str(getattr(task, "root_id", "") or ""), kind="root_task_id")
    return task, root


# LLM: Pending payload copies the already-sanitized approval contract and exact
# structural ids; it contains no raw tool arguments or credentials.
# 函数用途: 构造一条尚未决定的审批记录。
def _pending_payload(
    root_task_id: str,
    run_id: str,
    thread_id: str,
    request: ToolApprovalRequest,
    created_at: float,
) -> dict[str, object]:
    return {
        "schema_version": SUBAGENT_TOOL_APPROVAL_SCHEMA,
        "status": "pending",
        "root_task_id": root_task_id,
        "run_id": run_id,
        "thread_id": thread_id,
        "request": request.to_dict(),
        "created_at": created_at,
    }


# LLM: Public projection revalidates task ancestry and hides decided/corrupt
# records. Agent name/role are bounded display fields, not decision authority.
# 函数用途: 将一条合法等待记录转换成 TUI/Web 可展示对象。
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
        task, actual_root = _task_and_root(agent, run_id)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if actual_root != root_task_id or not _record_matches(payload, root_task_id, run_id, request):
        return None
    if task_status_in(
        str(getattr(task, "status", "") or ""),
        SUBAGENT_ENDED_STATUSES,
    ):
        return None
    return {
        "run_id": run_id,
        "agent_name": str(getattr(task, "name", "") or getattr(task, "role", "") or run_id)[:96],
        "request": request.to_dict(),
        "requested_at": request.requested_at,
    }


# LLM: Record comparison requires exact schema/root/run/request equality; a
# matching permission id alone is insufficient authorization.
# 函数用途: 核验账本记录是否精确对应当前审批请求。
def _record_matches(
    payload: object,
    root_task_id: str,
    run_id: str,
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
        payload.get("schema_version") == SUBAGENT_TOOL_APPROVAL_SCHEMA
        and str(payload.get("root_task_id") or "") == root_task_id
        and str(payload.get("run_id") or "") == run_id
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


# LLM: A decided record must still match the handle's exact request before its
# typed decision can wake the child runner.
# 函数用途: 从当前账本行提取已核验决定，尚未决定时返回 None。
def _record_decision(
    handle: SubagentToolApprovalHandle,
    payload: object,
) -> ToolApprovalDecision | None:
    if not _record_matches(
        payload,
        handle.root_task_id,
        handle.run_id,
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
        or payload.get("schema_version") != SUBAGENT_TOOL_APPROVAL_CONSUMER_SCHEMA
        or str(payload.get("root_task_id") or "") != root_task_id
    ):
        return 0.0
    try:
        return max(0.0, float(payload.get("seen_at") or 0.0))
    except (TypeError, ValueError):
        return 0.0


# LLM: Cleanup holds the same record transition lock and deletes only when the
# stored full identity still matches this waiter.
# 函数用途: 消费、取消或无人接收后移除这一条精确临时审批记录。
def _remove_exact_record(handle: SubagentToolApprovalHandle) -> None:
    with locked_json_path(_transition_path(handle.path)):
        report = read_json_object_report(handle.path, context="subagent.tool_approval.remove")
        if _record_matches(report.payload, handle.root_task_id, handle.run_id, handle.request):
            try:
                handle.path.unlink()
            except OSError:
                return


# LLM: One sibling lock serializes publish, decision, and cleanup for the exact
# hashed permission path without blocking unrelated child requests.
# 函数用途: 返回审批记录对应的跨进程事务锁文件。
def _transition_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.transition")


# LLM: Cancellation reads only a structured token field/method; exception text
# and user prose never stop or approve a tool.
# 函数用途: 判断子代理等待审批期间是否收到真实取消信号。
def _cancelled(token: object | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    return bool(checker()) if callable(checker) else bool(getattr(token, "cancelled", False))


# LLM: Session approval scope mirrors the foreground Gateway key: exact tool
# name plus normalized argument hash. It remains sink/attempt-local and cannot
# approve another child or differently parameterized call.
# 函数用途: 生成子代理本次执行尝试内“始终允许同一调用”的缓存键。
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
    "SUBAGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS",
    "SubagentToolApprovalHandle",
    "SubagentToolApprovalSinkMixin",
    "list_pending_subagent_tool_approvals",
    "publish_subagent_tool_approval",
    "renew_subagent_tool_approval_consumer",
    "resolve_subagent_tool_approval",
    "wait_for_subagent_tool_approval",
]
