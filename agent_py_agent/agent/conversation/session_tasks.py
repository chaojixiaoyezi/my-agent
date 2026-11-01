# LLM: 会话间派活的唯一权威存储。SessionTaskStore 只存派活记录（发送方、接收方、正文引用、dedupe_key、
#   body_dedupe_key、origin_task_id）和指向目标执行的链接；任务正文只存一份（作为 guidance 条目），
#   这里只存它的 id 与队列键。
#   accepted 由目标会话确认消费正文，或宿主沿真实 G1 创建回执把正文交给准确孩子后写入，同时把该回合的
#   request id 绑成 conversation_request_id；done/failed/cancelled 只由目标请求的终态事件写入。
#   禁止出现"这里说 done、目标请求其实 failed"的双账。
# 模块用途: 保存派活状态及宿主typed原输入快照；正文仍仅guidance一份，模型不能改写原输入。
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..common.json_io import locked_json_path
from ..gateway_parts.io import (
    read_json_file_report,
    update_json_file_atomic,
    write_json_file_atomic,
)
from .business_contracts import BusinessContextIntent, BusinessExecutionContext
from .models import new_id
from .store_io import safe_file_stem

# 状态机（唯一权威定义）：queued → accepted → done | failed | cancelled。
SESSION_TASK_QUEUED = "queued"
SESSION_TASK_ACCEPTED = "accepted"
SESSION_TASK_DONE = "done"
SESSION_TASK_FAILED = "failed"
SESSION_TASK_CANCELLED = "cancelled"
SESSION_TASK_STATUSES = frozenset(
    {SESSION_TASK_QUEUED, SESSION_TASK_ACCEPTED, SESSION_TASK_DONE, SESSION_TASK_FAILED, SESSION_TASK_CANCELLED}
)
# 终态：写入后不再被普通生命周期改写（取消只能从非终态发起）。
SESSION_TASK_TERMINAL_STATUSES = frozenset({SESSION_TASK_DONE, SESSION_TASK_FAILED, SESSION_TASK_CANCELLED})


def _completion_execution(host, run_id, attempt_id):
    repo = host.subagents.runtime_db
    if repo is None:
        raise ValueError("COMPLETION_AUTHORITY_REQUIRED")
    owner = str(host.home_paths.owner_id)
    ref = repo.parent_execution_ref(owner_id=owner, run_id=run_id, attempt_id=attempt_id)
    context = repo.business_context_for_execution(owner_id=owner, run_id=run_id, attempt_id=attempt_id)
    if context is None:
        return None
    task = host.conversation_store.session_tasks.require(context.task_id)
    if (task.task_run_id != ref.task_run_id or BusinessExecutionContext.from_dict(task.metadata.get("business_context")) != context
            or any(host.conversation_store.threads.require(t).owner_id != owner for t in (task.sender_thread_id, task.target_thread_id))):
        raise PermissionError("COMPLETION_CONTEXT_MISMATCH")
    return repo, ref, context, task


def _live_completion_claim(host, ref, task):
    from .claim_execution_binding import current_claim_execution

    binding = current_claim_execution()
    if binding is None or binding.thread_id != task.target_thread_id or binding.claims is not host.conversation_store.claims:
        raise PermissionError("COMPLETION_CLAIM_REQUIRED")
    claim, error = binding.claims.load_report(binding.thread_id, claim_scope_id=binding.claim_scope_id)
    authority = claim.get("runtime_authority") or {}
    if (error or claim.get("claim_id") != binding.claim_id or claim.get("status") != "running"
            or claim.get("cancel_requested") is True or float(claim.get("expires_at") or 0) <= time.time()
            or claim.get("thread_id") != task.target_thread_id or authority.get("run_id") != ref.run_id
            or authority.get("agent_run_id") != ref.agent_run_id or authority.get("attempt_id") != ref.observed_attempt_id):
        raise PermissionError("COMPLETION_CLAIM_MISMATCH")
    return {"claim_ref": binding.claim_id, "thread_id": binding.thread_id,
            "run_id": ref.run_id, "attempt_id": ref.observed_attempt_id, "task_run_id": ref.task_run_id}


def _write_completion_metadata(host, task, updater):
    def update(payload):
        current = SessionTask.from_dict(payload)
        if (current.task_id != task.task_id or current.task_run_id != task.task_run_id
                or current.target_thread_id != task.target_thread_id or current.metadata.get("business_context") != task.metadata.get("business_context")):
            raise ValueError("COMPLETION_CONTEXT_MISMATCH")
        return replace(current, metadata=updater(current)).to_dict()
    return SessionTask.from_dict(update_json_file_atomic(
        host.conversation_store.session_tasks._path(task.task_id), update, require_existing=True))


def stage_background_learning_candidate(host, *, params, candidate):
    """W5真实finalize返回后保存候选摘要；不宣告completed、不入队、不调用学习模型。"""
    if not isinstance(getattr(params, "business_context", None), BusinessExecutionContext) or params.source != "background_main_agent":
        return
    execution = _completion_execution(host, params.run_id, params.attempt_id)
    if execution is None:
        raise ValueError("COMPLETION_CONTEXT_REQUIRED")
    _repo, ref, context, task = execution
    if context != params.business_context or candidate.source != "background_main_agent":
        raise PermissionError("COMPLETION_CONTEXT_MISMATCH")
    proof = _live_completion_claim(host, ref, task)
    if type(candidate.tool_rounds) is not int or candidate.tool_rounds < 0:
        raise ValueError("COMPLETION_TOOL_ROUNDS_INVALID")
    trajectory = json.dumps(candidate.archive_tool_calls, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value = {**proof, "tool_rounds": candidate.tool_rounds, "source_kind": "background_main_agent",
             "trajectory_digest": hashlib.sha256(trajectory.encode()).hexdigest()}

    def update(current):
        if current.status in {SESSION_TASK_CANCELLED, SESSION_TASK_FAILED}:
            return current.metadata
        old = current.metadata.get("task_learning_candidate")
        if old and old.get("attempt_id") == value["attempt_id"] and old != value:
            raise ValueError("COMPLETION_CANDIDATE_CONFLICT")
        return {**current.metadata, "task_learning_candidate": value}
    _write_completion_metadata(host, task, update)


def confirm_background_task_completion(host, *, run_id: str, attempt_id: str):
    """只在原SessionTask终态+准确执行终态已落盘后铸造运输DTO；同claim原文件持久确认。"""
    from .business_contracts import TrustedTaskCompletion

    execution = _completion_execution(host, run_id, attempt_id)
    if execution is None:
        return None
    repo, ref, context, task = execution
    proof = _live_completion_claim(host, ref, task)
    if task.status != SESSION_TASK_DONE:
        return None
    agent_run, attempt = repo.get_agent_run(ref.agent_run_id), repo.get_attempt(attempt_id)
    if agent_run["status"] != "done" or attempt["status"] != "done" or not attempt["ended_at"]:
        return None
    candidate = task.metadata.get("task_learning_candidate")
    if not isinstance(candidate, dict) or any(candidate.get(key) != value for key, value in proof.items()):
        return None
    digest = hashlib.sha256(json.dumps([context.owner_id, task.task_id, run_id, attempt_id,
        proof["claim_ref"], candidate["trajectory_digest"]], separators=(",", ":")).encode()).hexdigest()
    completion = TrustedTaskCompletion(context.owner_id, task.target_thread_id, task.task_id, run_id, attempt_id,
        "completed", proof["claim_ref"], "completion-" + digest, context.space_id, context.project_ref,
        candidate["source_kind"], candidate["tool_rounds"], task.result_refs, candidate["trajectory_digest"])

    def update(current):
        if current.status != SESSION_TASK_DONE or current.metadata.get("task_learning_candidate") != candidate:
            raise ValueError("COMPLETION_STATE_CONFLICT")
        previous = current.metadata.get("trusted_task_completion")
        if previous is not None and previous != completion.to_dict():
            raise ValueError("COMPLETION_IDENTITY_CONFLICT")
        return {**current.metadata, "trusted_task_completion": completion.to_dict(), "task_completion_proof": proof,
                "task_learning_enqueue": current.metadata.get("task_learning_enqueue", {"status": "pending", "reason": "awaiting_queue"})}
    _write_completion_metadata(host, task, update)
    repair_completed_task_learning_wake(host, task.task_id)
    return completion


def repair_completed_task_learning_wake(host, task_id):
    """proof已落原task，只有原wake发布失败时补同一completion键；不重新铸证、不进入模型。"""
    from .business_contracts import TrustedTaskCompletion

    task = host.conversation_store.session_tasks.require(task_id)
    raw = task.metadata.get("trusted_task_completion")
    if raw is None or task.metadata.get("task_learning_enqueue", {}).get("status") != "pending":
        return None
    completion = TrustedTaskCompletion.from_dict(raw)
    if not verify_trusted_task_completion(host, completion):
        raise PermissionError("COMPLETION_PROOF_INVALID")
    wake = host.conversation_store.wakes.raise_signal({
        "thread_id": task.target_thread_id, "reason": "task_learning_completion", "urgency": "normal",
        "summary": "原已完成任务的方法学习入队待确认", "dedupe_key": completion.completion_ref, "retain_handled": True,
        "metadata": {"session_task_id": task.task_id, "completion_ref": completion.completion_ref}})
    _write_completion_metadata(host, task, lambda current: {**current.metadata, "task_learning_wake_id": wake.wake_signal_id})
    return wake


def repair_pending_completed_task_learning_wakes(host):
    """原调度维护每次只读一页定位引用；游标不是权威，失效重建，不扫描全部任务正文。"""
    store = getattr(host, "conversation_store", None)
    repo = getattr(getattr(host, "subagents", None), "runtime_db", None)
    if repo is None or not callable(getattr(getattr(store, "session_tasks", None), "list_page", None)):
        return
    page = store.session_tasks.list_page(limit=16, cursor=getattr(host, "_completion_repair_cursor", None))
    host._completion_repair_cursor = page["next_cursor"] if page["coverage"] != "expired" else None
    for task in page["items"]:
        if task.status == SESSION_TASK_DONE and task.metadata.get("task_learning_enqueue", {}).get("status") == "pending":
            repair_completed_task_learning_wake(host, task.task_id)


def verify_trusted_task_completion(host, completion) -> bool:
    """W6消费时重核原证明及准确DB链；claim已换到别的任务也不能借最新claim替换原完成出处。"""
    from .business_contracts import TrustedTaskCompletion

    if not isinstance(completion, TrustedTaskCompletion) or completion.owner_id != str(host.home_paths.owner_id):
        return False
    try:
        execution = _completion_execution(host, completion.run_id, completion.attempt_id)
        if execution is None:
            return False
        repo, ref, context, task = execution
        proof = task.metadata.get("task_completion_proof") or {}
        return (task.status == SESSION_TASK_DONE and task.task_id == completion.task_id
            and task.target_thread_id == completion.thread_id and context.space_id == completion.space_id
            and context.project_ref == completion.project_ref and task.metadata.get("trusted_task_completion") == completion.to_dict()
            and proof == {"claim_ref": completion.claim_ref, "thread_id": completion.thread_id, "run_id": ref.run_id,
                          "attempt_id": completion.attempt_id, "task_run_id": ref.task_run_id}
            and repo.get_agent_run(ref.agent_run_id)["status"] == "done" and repo.get_attempt(completion.attempt_id)["status"] == "done")
    except (ValueError, TypeError, KeyError, PermissionError, FileNotFoundError):
        return False


def retry_completed_task_learning(host, task_id):
    """只补原queue；原文件记本地重试责任，队列身份去重仍由W6原队列决定。"""
    from .business_contracts import TrustedTaskCompletion

    store = host.conversation_store.session_tasks
    with locked_json_path(store._path(task_id).with_suffix(".learning")):
        task = store.require(task_id)
        raw = task.metadata.get("trusted_task_completion")
        if raw is None:
            return {"status": "ineligible", "reason": "canonical_completion_missing"}
        completion = TrustedTaskCompletion.from_dict(raw)
        if not verify_trusted_task_completion(host, completion):
            raise PermissionError("COMPLETION_PROOF_INVALID")
        previous = task.metadata.get("task_learning_enqueue") or {}
        if previous.get("status") in {"enqueued", "duplicate", "ineligible", "conflict"}:
            if task.metadata.get("task_learning_wake_id"):
                host.conversation_store.wakes.mark_handled(task.metadata["task_learning_wake_id"])
            return dict(previous)
        try:
            from ..capability.task_learning_bridge import enqueue_completed_task_learning
        except ModuleNotFoundError as exc:
            if exc.name != "agent_py_agent.agent.capability.task_learning_bridge":
                raise
            return {"status": "pending", "reason": "TASK_LEARNING_BRIDGE_UNAVAILABLE"}
        result = enqueue_completed_task_learning(completion=completion, host=host)
        if not isinstance(result, dict) or result.get("status") not in {"enqueued", "duplicate", "ineligible", "conflict"}:
            raise ValueError("TASK_LEARNING_BRIDGE_INVALID_RESULT")
        _write_completion_metadata(host, task, lambda current: {**current.metadata, "task_learning_enqueue": dict(result)})
        if task.metadata.get("task_learning_wake_id"):
            host.conversation_store.wakes.mark_handled(task.metadata["task_learning_wake_id"])
        return result


# LLM: 冻结值对象；正文只以 body_guidance_id 引用既有的 guidance 条目，不在这里复制第二份正文。
# 类用途: 保存一条会话间派活任务的结构化记录。
@dataclass(frozen=True)
class SessionTask:
    task_id: str
    sender_thread_id: str
    target_thread_id: str
    goal: str
    # 任务正文只存一份：作为目标 thread 的 guidance 条目，这里只存它的 id。
    body_guidance_id: str = ""
    # 任务正文在目标 guidance 队列里的幂等键：撤队列（取消未开始的任务）按它定位那一条正文。
    body_dedupe_key: str = ""
    status: str = SESSION_TASK_QUEUED
    dedupe_key: str = ""
    # 防循环：由对端 task 触发的 task 记下上级 task；链深按这个字段结构化计算。
    origin_task_id: str = ""
    # 指向目标执行的链接（结构化，不猜）。
    conversation_request_id: str = ""
    task_run_id: str = ""
    result_refs: tuple[str, ...] = ()
    summary: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    # 宿主判定任务失败时的结构化原因码（登记在 ERROR_CONTRACTS），例如唤醒被毒丸结案；模型自己报的失败不填。
    #   读旧记录缺这个字段按空串处理；旧版读新记录会忽略这个多出的键（from_dict 只取已知字段）。
    failure_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "session_task.v1",
            "task_id": self.task_id,
            "sender_thread_id": self.sender_thread_id,
            "target_thread_id": self.target_thread_id,
            "goal": self.goal,
            "body_guidance_id": self.body_guidance_id,
            "body_dedupe_key": self.body_dedupe_key,
            "status": self.status,
            "dedupe_key": self.dedupe_key,
            "origin_task_id": self.origin_task_id,
            "conversation_request_id": self.conversation_request_id,
            "task_run_id": self.task_run_id,
            "result_refs": list(self.result_refs),
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata or {}),
            "failure_code": self.failure_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionTask:
        refs = data.get("result_refs")
        metadata = data.get("metadata")
        return cls(
            task_id=str(data.get("task_id") or ""),
            sender_thread_id=str(data.get("sender_thread_id") or ""),
            target_thread_id=str(data.get("target_thread_id") or ""),
            goal=str(data.get("goal") or ""),
            body_guidance_id=str(data.get("body_guidance_id") or ""),
            body_dedupe_key=str(data.get("body_dedupe_key") or ""),
            status=str(data.get("status") or SESSION_TASK_QUEUED),
            dedupe_key=str(data.get("dedupe_key") or ""),
            origin_task_id=str(data.get("origin_task_id") or ""),
            conversation_request_id=str(data.get("conversation_request_id") or ""),
            task_run_id=str(data.get("task_run_id") or ""),
            result_refs=tuple(str(item) for item in (refs if isinstance(refs, list) else [])),
            summary=str(data.get("summary") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            metadata=metadata if isinstance(metadata, dict) else {},
            failure_code=str(data.get("failure_code") or ""),
        )


# LLM: 冻结值对象，承载新建一条会话任务所需的全部结构化输入；字段与 SessionTask 一一对应。
#   用具名对象替代 8 个关键字参数，避免参数表随字段增长。
# 类用途: 保存一次会话任务创建请求的输入。
@dataclass(frozen=True)
class SessionTaskDraft:
    sender_thread_id: str
    target_thread_id: str
    goal: str
    body_guidance_id: str = ""
    body_dedupe_key: str = ""
    dedupe_key: str = ""
    origin_task_id: str = ""


# LLM: 冻结值对象，承载一次状态推进的全部结构化输入；用具名对象替代 6 个关键字参数。
#   字段语义与 SessionTask 一一对应，空值表示"不改这个字段"（沿用旧值）。
# 类用途: 保存一次会话任务状态推进请求。
@dataclass(frozen=True)
class SessionTaskUpdate:
    status: str
    summary: str = ""
    result_refs: tuple[str, ...] = ()
    conversation_request_id: str = ""
    task_run_id: str = ""
    failure_code: str = ""


# LLM: 幂等判定只比结构化身份字段（发送方/目标/正文），不比对时间戳等派生值。
# 函数用途: 判断同键重放是否是同一份输入。
def _draft_matches(existing: SessionTask, draft: SessionTaskDraft) -> bool:
    pairs = (
        (existing.sender_thread_id, draft.sender_thread_id),
        (existing.target_thread_id, draft.target_thread_id),
        (existing.goal, draft.goal),
    )
    return all(left == right for left, right in pairs)


# LLM: 索引文件只是查找用，权威仍是每任务文件；调用方保证键非空，本函数不做语义判断。
# 函数用途: 写一个任务索引文件。
def _write_index(path: Path, schema_version: str, task_id: str) -> None:
    write_json_file_atomic(path, {"schema_version": schema_version, "task_id": task_id})


# LLM: 存储只落 canonical 目录下的每任务一个 JSON 文件；状态变更走 update_json_file_atomic（锁内重读），
#   避免两个写入方互相覆盖。读取坏文件必须显式报错，不能当成"不存在"。
# 类用途: 保存、读取与推进会话间派活任务的状态。
class SessionTaskStore:
    # LLM: 只接收 storage；不注册调度、不发模型请求、不持有目标执行对象。
    # 函数用途: 绑定 canonical 存储目录。
    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def _list_index(self):
        from .session_list_index import SessionListIndex

        def read(path):
            report = read_json_file_report(path, context="conversation.session_task.read")
            if report.load_error is not None:
                return {}, {"code": "SESSION_TASK_READ_FAILED", "path": str(path)}
            try:
                task = SessionTask.from_dict(report.payload or {})
                if not task.task_id or self._path(task.task_id) != path:
                    raise ValueError("task identity mismatch")
                return task.to_dict(), None
            except (ValueError, TypeError, KeyError):
                return {}, {"code": "SESSION_TASK_INVALID", "path": str(path)}
        return SessionListIndex(self._dir(), kind="session_task", reader=read)

    def list_page(self, *, limit: int = 50, cursor: str | None = None, target_thread_id: str | None = None) -> dict:
        page = self._list_index().page(limit=limit, cursor=cursor, filter_key=target_thread_id)
        return {**page, "items": [SessionTask.from_dict(item) for item in page["items"]]}

    def find_by_dedupe_key(self, key: str) -> SessionTask | None:
        return self._find_by_dedupe_key(key)

    def require(self, task_id: str) -> SessionTask:
        task = self.load(task_id)
        if task is None:
            raise FileNotFoundError("SessionTask not found")
        return task

    def record_execution_cancellation_control(self, task_id: str, *, execution_ref, outstanding_refs: list[str]) -> SessionTask:
        from .business_contracts import HostParentExecutionRef

        if (not isinstance(execution_ref, HostParentExecutionRef) or not execution_ref.observed_attempt_id
                or len(outstanding_refs) > 100 or any(type(ref) is not str or not ref for ref in outstanding_refs)):
            raise ValueError("invalid execution cancellation references")

        def update(payload):
            task = SessionTask.from_dict(payload)
            context = BusinessExecutionContext.from_dict(task.metadata.get("business_context"))
            if task.task_id != task_id or task.task_run_id != execution_ref.task_run_id or context.owner_id != execution_ref.owner_id:
                raise ValueError("SESSION_TASK_EXECUTION_CONFLICT")
            previous = task.metadata.get("cancellation_control", {})
            refs = list(dict.fromkeys([*previous.get("outstanding_refs", []), *outstanding_refs]))
            if len(refs) > 100:
                raise ValueError("cancellation reference limit")
            return replace(task, metadata={**task.metadata, "cancellation_control": {
                **previous, "outstanding_refs": refs, "execution_ref": execution_ref.to_dict()}}).to_dict()

        return SessionTask.from_dict(update_json_file_atomic(self._path(task_id), update, require_existing=True))

    def record_cancellation_control(self, task_id: str, *, outstanding_refs: list[str]) -> SessionTask:
        if len(outstanding_refs) > 100 or any(type(ref) is not str or not ref for ref in outstanding_refs):
            raise ValueError("invalid cancellation references")

        def update(payload):
            task = SessionTask.from_dict(payload)
            if task.task_id != task_id or task.status != SESSION_TASK_CANCELLED:
                raise ValueError("SESSION_TASK_CANCEL_CONFLICT")
            metadata = {**task.metadata, "cancellation_control": {
                "conversation_request_id": task.conversation_request_id, "outstanding_refs": list(outstanding_refs)}}
            return replace(task, metadata=metadata).to_dict()

        return SessionTask.from_dict(update_json_file_atomic(self._path(task_id), update, require_existing=True))

    # LLM: 目录不存在时按"没有任务"返回；读坏文件进 load_errors，不静默跳过。
    # 函数用途: 列出本 owner 的会话任务记录。
    def list_report(self, *, limit: int = 100) -> tuple[list[SessionTask], list[dict[str, Any]]]:
        directory = self._dir()
        tasks: list[SessionTask] = []
        load_errors: list[dict[str, Any]] = []
        if not directory.exists():
            return tasks, load_errors
        for path in sorted(directory.glob("*.json")):
            report = read_json_file_report(path, context="conversation.session_task.read")
            if report.load_error is not None:
                load_errors.append({"code": "SESSION_TASK_READ_FAILED", "path": str(path)})
                continue
            try:
                tasks.append(SessionTask.from_dict(report.payload or {}))
            except (TypeError, ValueError, KeyError):
                load_errors.append({"code": "SESSION_TASK_INVALID", "path": str(path)})
        tasks.sort(key=lambda item: item.created_at)
        limited = tasks if limit <= 0 else tasks[-limit:]
        return limited, load_errors

    # LLM: 精确 ID 读取；不存在返回 None，坏文件抛结构化错误由调用方处理。
    # 函数用途: 按 task_id 读取一条会话任务。
    def load(self, task_id: str) -> SessionTask | None:
        selected = str(task_id or "").strip()
        if not selected:
            return None
        path = self._path(selected)
        if not path.exists():
            return None
        report = read_json_file_report(path, context="conversation.session_task.read")
        if report.load_error is not None or not report.payload:
            raise ValueError(f"conversation session task is unreadable: {selected}")
        return SessionTask.from_dict(report.payload)

    # LLM: 幂等创建：同 dedupe_key 且同输入返回原记录（不新建、不改状态）；同键异文报错。
    #   链深在调用方按 origin_task_id 结构化计算后传入校验结果，本方法不读模型给的深度。
    # LLM: 原输入上下文仅typed宿主参数可写；同dedupe必须同快照，模型Draft无此字段，重放不能借旧身份。
    # 函数用途: 新建原派活记录及可选不可变输入快照；同键重放严格核对输入，不新增账本。
    def create(self, draft: SessionTaskDraft, *, now: float | None = None, host_message_context=None,
               business_context_intent: BusinessContextIntent | None = None,
               business_context_factory: Callable[[str], BusinessExecutionContext] | None = None) -> SessionTask:
        from .host_message_context import CONTEXT_KEY, HostMessageContext

        if (business_context_intent is None) != (business_context_factory is None):
            raise ValueError("business intent/factory must be provided together")
        if business_context_intent is not None:
            if not isinstance(business_context_intent, BusinessContextIntent) or not callable(business_context_factory):
                raise ValueError("invalid host business context")
            self._validate_business_owner(draft, business_context_intent.owner_id)
        if host_message_context is not None and (not isinstance(host_message_context, HostMessageContext)
                or host_message_context.actor.source_thread_id != draft.sender_thread_id):
            raise ValueError('原派活引用上下文身份无效')
        snapshot = host_message_context.to_record() if host_message_context is not None else None
        current = now if now is not None else time.time()
        key = str(draft.dedupe_key or "").strip()
        intent = business_context_intent.to_dict() if business_context_intent else None
        fingerprint = hashlib.sha256(json.dumps(
            [draft.sender_thread_id, draft.target_thread_id, draft.goal, draft.origin_task_id,
             draft.body_dedupe_key, snapshot, intent], sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        # 独立的创建门覆盖预留、factory、canonical提交及原索引修复；不持有模型或网络调用。
        gate = locked_json_path(self._dedupe_path(key).with_suffix(".create")) if key else nullcontext()
        with gate:
            replayed = self._replay_existing(key, draft)
            if replayed is not None:
                if (replayed.metadata.get(CONTEXT_KEY) != snapshot
                        or replayed.metadata.get("business_context_intent") != intent
                        or replayed.origin_task_id != draft.origin_task_id):
                    raise ValueError("IDEMPOTENCY_CONFLICT: task input or business intent changed")
                self._write_lookup_indexes(key, draft.body_dedupe_key, replayed.task_id)
                return replayed
            task_id = self._reserve_creation_id(key, fingerprint)
            metadata = {CONTEXT_KEY: snapshot} if snapshot is not None else {}
            if business_context_intent is not None:
                context = business_context_factory(task_id)
                if not isinstance(context, BusinessExecutionContext) or context.task_id != task_id:
                    raise ValueError("business context task_id does not match reserved task_id")
                if context.owner_id != business_context_intent.owner_id or not context.space_id:
                    raise ValueError("business context owner/space mismatch")
                if (business_context_intent.requested_space_id is not None
                        and business_context_intent.requested_space_id != context.space_id
                        or business_context_intent.project_ref != context.project_ref
                        or business_context_intent.binding_id != context.binding_id):
                    raise ValueError("business context selection conflicts with intent")
                metadata.update(business_context_intent=intent, business_context=context.to_dict())
            task = SessionTask(
                task_id=task_id, sender_thread_id=str(draft.sender_thread_id or ""),
                target_thread_id=str(draft.target_thread_id or ""), goal=str(draft.goal or ""),
                body_guidance_id=str(draft.body_guidance_id or ""), body_dedupe_key=str(draft.body_dedupe_key or ""),
                dedupe_key=key, origin_task_id=str(draft.origin_task_id or ""),
                created_at=current, updated_at=current, metadata=metadata,
            )
            write_json_file_atomic(self._path(task.task_id), task.to_dict())
            self._write_lookup_indexes(key, str(draft.body_dedupe_key or "").strip(), task.task_id)
            self._list_index().publish(self._path(task.task_id))
            return task

    def _validate_business_owner(self, draft: SessionTaskDraft, owner_id: str) -> None:
        from .store_threads import ThreadStore

        threads = ThreadStore(self.storage)
        for thread_id in (draft.sender_thread_id, draft.target_thread_id):
            thread = threads.require(thread_id)
            if thread.owner_id != owner_id:
                raise ValueError("business task owner mismatch")

    def _reserve_creation_id(self, key: str, fingerprint: str) -> str:
        if not key:
            return new_id("stask")
        report = read_json_file_report(self._dedupe_path(key), context="session_task.reserve")
        if report.load_error is not None:
            raise ValueError("session task dedupe index is unreadable")
        reservation = report.payload
        if reservation:
            if reservation.get("intent_fingerprint") != fingerprint:
                raise ValueError("IDEMPOTENCY_CONFLICT: reserved task input changed")
            task_id = reservation.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("session task reservation is invalid")
            return task_id
        task_id = new_id("stask")
        write_json_file_atomic(self._dedupe_path(key), {
            "schema_version": "session_task_dedupe.v1", "task_id": task_id,
            "intent_fingerprint": fingerprint,
        })
        return task_id

    # LLM: 幂等重放：同 dedupe_key 且同输入返回原记录（不新建、不改状态）；同键异文报错。
    # 函数用途: 按键重放一条已存在的会话任务；没有可重放的记录时返回 None。
    def _replay_existing(self, dedupe_key: str, draft: SessionTaskDraft) -> SessionTask | None:
        if not dedupe_key:
            return None
        existing = self._find_by_dedupe_key(dedupe_key)
        if existing is None:
            return None
        if not _draft_matches(existing, draft):
            raise ValueError("session task dedupe key reused with different input")
        return existing

    # LLM: 索引只是查找用，权威仍是每任务文件；键为空表示不需要该索引，不写文件。
    # 函数用途: 写去重索引与正文反向索引。
    def _write_lookup_indexes(self, dedupe_key: str, body_dedupe_key: str, task_id: str) -> None:
        pairs = (
            (self._dedupe_path(dedupe_key), "session_task_dedupe.v1", dedupe_key),
            (self._body_index_path(body_dedupe_key), "session_task_body_index.v1", body_dedupe_key),
        )
        for path, schema_version, key in pairs:
            if key:
                _write_index(path, schema_version, task_id)

    # LLM: 正文反向索引只做查找，权威仍是每任务文件；坏索引返回 None，不把读坏当"没有任务"处理。
    # 函数用途: 按目标 guidance 队列键找回它对应的会话任务。
    def load_by_body_dedupe_key(self, body_dedupe_key: str) -> SessionTask | None:
        key = str(body_dedupe_key or "").strip()
        if not key:
            return None
        path = self._body_index_path(key)
        if not path.exists():
            return None
        report = read_json_file_report(path, context="conversation.session_task.body_index")
        if report.load_error is not None or not report.payload:
            return None
        task_id = str((report.payload or {}).get("task_id") or "")
        return self.load(task_id) if task_id else None

    # LLM: 正文要在记录之后写入才能带上任务编号，所以记录先以空正文 id 落盘，随后由这里补上。
    #   只补这一次、只补空值；已绑定或终态记录不改写，重放返回当前记录不报错。
    # 函数用途: 把刚写入的正文 id 绑到它所属的会话任务记录上。
    def bind_body(self, task_id: str, body_guidance_id: str) -> SessionTask | None:
        guidance_id = str(body_guidance_id or "").strip()
        current = self.load(task_id)
        if current is None:
            return None
        if not guidance_id or current.body_guidance_id:
            return current
        updated = replace(
            current,
            body_guidance_id=guidance_id,
            updated_at=time.time(),
        )
        write_json_file_atomic(self._path(task_id), updated.to_dict())
        self._list_index().publish(self._path(task_id))
        return updated

    # LLM: 绑定只在非终态且尚未绑定其它回合时生效；同回合重复确认幂等，不覆盖已有绑定。
    #   目标回合结束（任务转终态）后不再绑定新回合，避免把取消打到后来的一轮上。
    # 函数用途: 把一条任务绑定到目标会话正在执行它的那个回合。
    def bind_turn(self, task_id: str, *, turn_id: str, now: float | None = None) -> SessionTask | None:
        bound_turn = str(turn_id or "").strip()
        current = self.load(task_id)
        if current is None:
            return None
        if current.status in SESSION_TASK_TERMINAL_STATUSES or current.conversation_request_id:
            return current
        if not bound_turn:
            return current
        if current.status == SESSION_TASK_QUEUED:
            return self.advance(
                task_id,
                SessionTaskUpdate(status=SESSION_TASK_ACCEPTED, conversation_request_id=bound_turn),
                now=now,
            )
        if current.status == SESSION_TASK_ACCEPTED:
            return self.advance(
                task_id,
                SessionTaskUpdate(status=SESSION_TASK_ACCEPTED, conversation_request_id=bound_turn),
                now=now,
            )
        return current

    # LLM: 状态推进只允许既定迁移；非法迁移不改文件，抛 ValueError 让调用方按结构化错误处理。
    #   终态不可被普通生命周期改写；取消只能从非终态发起。
    # 函数用途: 原子推进一条会话任务的状态与结果字段。
    def advance(self, task_id: str, update: SessionTaskUpdate, *, now: float | None = None) -> SessionTask:
        target_status = str(update.status or "").strip()
        if target_status not in SESSION_TASK_STATUSES:
            raise ValueError(f"unknown session task status: {target_status}")
        current = now if now is not None else time.time()

        # LLM: 锁内重读最新记录再校验迁移；并发下只有一个写入方成功，避免双账。
        # 函数用途: 在原子更新内校验并应用状态迁移。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            latest = SessionTask.from_dict(data)
            if not _transition_allowed(latest.status, target_status):
                raise ValueError(
                    f"illegal session task transition: {latest.status} -> {target_status}"
                )
            updated = replace(
                latest,
                status=target_status,
                summary=str(update.summary or latest.summary) if update.summary else latest.summary,
                result_refs=tuple(update.result_refs) if update.result_refs else latest.result_refs,
                conversation_request_id=update.conversation_request_id or latest.conversation_request_id,
                task_run_id=update.task_run_id or latest.task_run_id,
                failure_code=update.failure_code or latest.failure_code,
                updated_at=current,
            )
            return updated.to_dict()

        update_json_file_atomic(self._path(task_id), updater, require_existing=True)
        self._list_index().publish(self._path(task_id))
        loaded = self.load(task_id)
        assert loaded is not None
        return loaded

    def bind_execution(self, task_id: str, *, task_run_id: str, target_thread_id: str) -> SessionTask:
        """反向locator可幂等修复；JSON与SQLite不是跨存储事务，失败必须阻止模型启动。"""
        def bind(data):
            task = SessionTask.from_dict(data)
            if (task.target_thread_id != target_thread_id or not task_run_id
                    or task.task_run_id and task.task_run_id != task_run_id):
                raise ValueError("session task execution binding conflict")
            return replace(task, task_run_id=task_run_id).to_dict()

        payload = update_json_file_atomic(self._path(task_id), bind, require_existing=True)
        return SessionTask.from_dict(payload)

    # LLM: 链深按 origin_task_id 结构化遍历，不信任模型传的深度；环或坏链返回一个足够大的值让调用方拒绝。
    # 函数用途: 计算一条任务在派活链上的深度（根为 0）。
    def chain_depth(self, task: SessionTask, *, limit: int = 64) -> int:
        depth = 0
        current = str(task.origin_task_id or "").strip()
        seen: set[str] = {task.task_id}
        while current and depth < limit:
            if current in seen:
                return limit
            seen.add(current)
            parent = self.load(current)
            if parent is None:
                break
            depth += 1
            current = str(parent.origin_task_id or "").strip()
        return depth

    # LLM: 去重索引只做查找，权威仍是每任务的 canonical 文件；坏索引返回 None 让调用方新建。
    # 函数用途: 按 dedupe_key 查找既有任务。
    def _find_by_dedupe_key(self, key: str) -> SessionTask | None:
        path = self._dedupe_path(key)
        if not path.exists():
            return None
        report = read_json_file_report(path, context="conversation.session_task.dedupe")
        if report.load_error is not None or not report.payload:
            return None
        task_id = str((report.payload or {}).get("task_id") or "")
        return self.load(task_id) if task_id else None

    # LLM: 目录布局由 storage 提供；缺失时按无任务处理，不在这里创建目录。
    # 函数用途: 返回会话任务目录。
    def _dir(self) -> Path:
        return self.storage.session_tasks_dir

    # LLM: 文件名用 safe_file_stem 拒绝式校验，ID 直接拼路径是注入面。
    # 函数用途: 返回一条会话任务的 canonical 文件路径。
    def _path(self, task_id: str) -> Path:
        return self._dir() / f"{safe_file_stem(str(task_id or ''))}.json"

    # LLM: 去重索引按 key 派生文件名；key 可能含冒号等字符，经 safe_file_stem 归一。
    # 函数用途: 返回去重索引文件路径。
    def _dedupe_path(self, key: str) -> Path:
        return self._dir() / "dedupe" / f"{safe_file_stem(str(key or ''))}.json"

    # LLM: 正文索引与 dedupe 索引分开，避免两套键互相覆盖；文件名同样经 safe_file_stem 归一。
    # 函数用途: 返回任务正文队列键的反向索引文件路径。
    def _body_index_path(self, key: str) -> Path:
        return self._dir() / "body" / f"{safe_file_stem(str(key or ''))}.json"


# LLM: 目标会话确认消费任务正文（guidance ack）后调用；只按结构化字段（origin_kind + 正文队列键）认任务，
#   不解析正文。绑定失败不抛给调用方（ack 主流程不能被任务账本影响），返回真正绑定到该回合的条数。
# 函数用途: 把已被目标回合消费的会话任务正文绑到该回合，完成 accepted 与 request id 的写入。
def bind_session_task_turns(store: object, entries: object, turn_id: str) -> int:
    from .session_messaging import SESSION_TASK_ORIGIN_KIND

    tasks = getattr(store, "session_tasks", None)
    bound_turn = str(turn_id or "").strip()
    if tasks is None or not bound_turn:
        return 0
    bound = 0
    for entry in list(entries or ()):
        key = _session_task_body_key(entry, SESSION_TASK_ORIGIN_KIND)
        if key and _bind_one_task_body(tasks, key, bound_turn):
            bound += 1
    return bound


# LLM: 单条绑定的失败只影响这一条（读坏索引、非法迁移都不抛给 ack 主流程），返回是否真的绑到了该回合。
# 函数用途: 把一条任务正文对应的任务绑到目标回合，返回是否绑定成功。
def _bind_one_task_body(tasks: object, body_key: str, bound_turn: str) -> bool:
    try:
        task = tasks.load_by_body_dedupe_key(body_key)
        if task is None:
            return False
        if str(task.conversation_request_id or "") == bound_turn:
            # 已经绑到同一个回合：这是幂等命中，不是"没绑上"。调用方按返回值判断是否落账，
            # 报 False 会让认领时的绑定看起来失败（真实链路上就表现为"任务一直没绑定"）。
            return True
        updated = tasks.bind_turn(task.task_id, turn_id=bound_turn)
    except (OSError, ValueError, TypeError):
        return False
    return str(getattr(updated, "conversation_request_id", "") or "") == bound_turn


# LLM: 只按结构化 metadata 判定"这条 guidance 是不是某条会话任务的正文"；不解析正文、不看自然语言。
# 函数用途: 从一条 guidance 条目里取出它引用的会话任务正文队列键；不是任务正文时返回空串。
def _session_task_body_key(entry: object, origin_kind: str) -> str:
    metadata = getattr(entry, "metadata", None)
    if not isinstance(metadata, dict):
        return ""
    if str(metadata.get("origin_kind") or "") != origin_kind:
        return ""
    return str(metadata.get("dedupe_key") or "").strip()


# LLM: 状态机唯一权威：只允许 queued→accepted、accepted→done/failed、queued/accepted→cancelled；
#   终态不再改；同状态重复写入视为幂等（幂等重放返回原记录，不算非法迁移）。
# 函数用途: 判断一次状态迁移是否合法。
def _transition_allowed(current: str, target: str) -> bool:
    if current == target:
        return True
    if current in SESSION_TASK_TERMINAL_STATUSES:
        return False
    if current == SESSION_TASK_QUEUED:
        return target in {SESSION_TASK_ACCEPTED, SESSION_TASK_CANCELLED, SESSION_TASK_FAILED}
    if current == SESSION_TASK_ACCEPTED:
        return target in {SESSION_TASK_DONE, SESSION_TASK_FAILED, SESSION_TASK_CANCELLED}
    return False


__all__ = [
    "SESSION_TASK_ACCEPTED",
    "SESSION_TASK_CANCELLED",
    "SESSION_TASK_DONE",
    "SESSION_TASK_FAILED",
    "SESSION_TASK_QUEUED",
    "SESSION_TASK_STATUSES",
    "SESSION_TASK_TERMINAL_STATUSES",
    "SessionTask",
    "SessionTaskStore",
    "bind_session_task_turns",
]
