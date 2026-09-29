# LLM: 会话间派活的唯一权威存储。SessionTaskStore 只存派活记录（发送方、接收方、正文引用、dedupe_key、
#   body_dedupe_key、origin_task_id）和指向目标执行的链接；任务正文只存一份（作为 guidance 条目），
#   这里只存它的 id 与队列键。
#   状态机只有一个写入方：accepted 由目标会话确认消费任务正文（guidance ack）时写入，同时把该回合的
#   request id 绑成 conversation_request_id；done/failed/cancelled 只由目标请求的终态事件写入。
#   禁止出现"这里说 done、目标请求其实 failed"的双账。
# 模块用途: 保存会话间派活的任务记录与状态，供派活工具、状态查询、取消和结果回报共用。
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import (
    read_json_file_report,
    update_json_file_atomic,
    write_json_file_atomic,
)
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
    # 函数用途: 新建一条会话派活记录；同键重放返回原记录。
    def create(self, draft: SessionTaskDraft, *, now: float | None = None) -> SessionTask:
        current = now if now is not None else time.time()
        key = str(draft.dedupe_key or "").strip()
        replayed = self._replay_existing(key, draft)
        if replayed is not None:
            return replayed
        task = SessionTask(
            task_id=new_id("stask"),
            sender_thread_id=str(draft.sender_thread_id or ""),
            target_thread_id=str(draft.target_thread_id or ""),
            goal=str(draft.goal or ""),
            body_guidance_id=str(draft.body_guidance_id or ""),
            body_dedupe_key=str(draft.body_dedupe_key or ""),
            dedupe_key=key,
            origin_task_id=str(draft.origin_task_id or ""),
            created_at=current,
            updated_at=current,
        )
        write_json_file_atomic(self._path(task.task_id), task.to_dict())
        self._write_lookup_indexes(key, str(draft.body_dedupe_key or "").strip(), task.task_id)
        return task

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
                updated_at=current,
            )
            return updated.to_dict()

        update_json_file_atomic(self._path(task_id), updater, require_existing=True)
        loaded = self.load(task_id)
        assert loaded is not None
        return loaded

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
