"""冻结业务历史只认原来源；索引用于预筛，canonical正文/范围才决定可见性。"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..common.opaque_id import validate_opaque_id
from ..conversation.business_contracts import BusinessExecutionContext
from ..conversation.host_actor import model_long_term_scope
from ..local_storage.models import LocalSearchResult
from ..local_storage.search import search_records_with_authority
from ..memory_store.recall import long_term_record_matches_scope
from .session_history_read import (
    SessionHistoryRefusal,
    message_text,
    read_thread_scope,
    trusted_thread,
    visible_conversation_row,
)

# 单次冻结历史检索最多扫描的来源条数上限。
_MAX_SOURCES = 256
# 单条冻结历史来源允许读取的最大字节数（2 MiB）。
_MAX_SOURCE_BYTES = 2 * 1024 * 1024


def frozen_history_scope(agent):
    """无冻结的本人主动查史和原批准子工具不变；不按自然语言或task_local标签添禁令。"""
    current = getattr(agent, "_current_run_params", None)
    business = getattr(current, "business_context", None)
    if (
        not isinstance(business, BusinessExecutionContext)
        and getattr(current, "memory_recall_scope", None) is None
    ):
        return None
    return model_long_term_scope(agent, getattr(agent, "memory", None), frozen_only=True)


class HistoryReadBudget:
    """一次冻结历史查询共用硬字节上限，不领取超大原子行例外。"""
    max_bytes = 4 * 1024 * 1024
    max_entries = 10000

    def __init__(self):
        self.size = self.entries = 0

    def charge(self, *, size=0, entries=0):
        self.size += size
        self.entries += entries

    def exhausted(self):
        return self.size >= self.max_bytes or self.entries >= self.max_entries


class ScopedHistory:
    """单查询有界缓存和一个runtime连接；绝不以当前线程展示归属认领旧记录。"""

    def __init__(self, agent, scope):
        self.agent, self.scope = agent, scope
        self.owner = agent.home_paths.owner_id
        self.cache, self.requests, self.unknown = {}, {}, False
        self.read_budget = HistoryReadBudget()
        self.source_errors = set()
        self.stack = ExitStack()
        repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
        self.db = self.stack.enter_context(repo._runtime_connection()) if repo is not None else None

    @property
    def remaining_bytes(self):
        return self.read_budget.max_bytes - self.read_budget.size

    @remaining_bytes.setter
    def remaining_bytes(self, value):
        self.read_budget.size = self.read_budget.max_bytes - value

    def close(self):
        self.stack.close()

    def _unknown(self):
        self.unknown = True
        return None

    def _request(self, request_id):
        if request_id in self.requests:
            return self.requests[request_id]
        validate_opaque_id(request_id, kind="history request")
        root = Path(self.agent.config.gateway_workspace).absolute()
        value = None
        self.requests[request_id] = None
        for state in ("done", "processing", "failed", "inbox"):
            if self.remaining_bytes <= 1:
                raise ValueError("HISTORY_SOURCE_READ_BUDGET")
            budget = min(_MAX_SOURCE_BYTES, self.remaining_bytes - 1)
            try:
                raw = read_bytes_beneath(
                    root, ("requests", state, request_id + ".json"), max_bytes=budget
                )
            except ValueError:
                # 底层拒绝超限前仍读一个哨兵；失败也必须计入累计预算。
                self.remaining_bytes -= budget + 1
                raise
            self.remaining_bytes -= len(raw or b"")
            if raw is not None:
                value = json.loads(raw)
                break
        if value is not None:
            from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

            identity = value.get("host_request_owner.v1") or {}
            owner = resolve_owner_home(
                self.agent.home_paths.root,
                OwnerIdentity(identity["provider"], identity["owner_kind"], identity["owner_id"]),
            ).owner_id
            if owner != self.owner or value.get("id", value.get("request_id")) != request_id:
                value = None
        self.requests[request_id] = value
        return value

    def _context_allowed(self, raw):
        if not isinstance(raw, dict):
            return self._unknown()
        context = BusinessExecutionContext.from_dict(raw)
        if context.owner_id != self.owner:
            return False
        current = getattr(self.agent, "_current_run_params", None)
        selected = getattr(current, "business_context", None)
        sid = (
            selected.space_id
            if isinstance(selected, BusinessExecutionContext)
            else (current.host_session_space or {}).get("space_id")
        )
        from ..memory_store.recall import MemoryRecallScope

        project = MemoryRecallScope.from_runtime(
            task_attributes={"project_id": context.project_ref}
        )
        return context.space_id == sid and all(self.scope.allows(*key) for key in project.keys)

    def _original_scope(self, request_id, thread_id):
        if not request_id:
            return self._unknown()
        if self.db is not None:
            rows = self.db.execute(
                "SELECT t.owner_id,t.thread_id,t.conversation_task_id,tr.metadata_json FROM agent_runs ar "
                "JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id "
                "WHERE ar.run_id=? AND t.owner_id=? LIMIT 2",
                (request_id, self.owner),
            ).fetchall()
            if len(rows) == 1:
                meta = json.loads(rows[0]["metadata_json"])
                if meta.get("business_context"):
                    if rows[0]["thread_id"] != thread_id:
                        return False
                    if meta.get("session_task_ref") != meta["business_context"].get(
                        "task_id"
                    ) or rows[0]["conversation_task_id"] != meta["business_context"].get("task_id"):
                        return self._unknown()
                    return self._context_allowed(meta["business_context"])
        request = self._request(request_id)
        snapshot = (request or {}).get("host_session_space.v1")
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("owner_id") != self.owner
            or snapshot.get("thread_id") != thread_id
        ):
            return self._unknown()
        current = getattr(self.agent, "_current_run_params", None)
        business = getattr(current, "business_context", None)
        sid = (
            business.space_id
            if isinstance(business, BusinessExecutionContext)
            else (current.host_session_space or {}).get("space_id")
        )
        if snapshot.get("space_id") != sid:
            return False
        # 原前台六字段只证明空间选择，没有历史project证明；不能借当前default反签旧正文。
        return self._unknown()

    def verify_message(self, row):
        if row is None or not visible_conversation_row(row):
            return None
        thread = self.agent.conversation_store.threads.require(row.thread_id)
        if thread.owner_id != self.owner:
            return None
        request_id = str(
            row.metadata.get("conversation_request_id")
            or row.metadata.get("gateway_request_id")
            or ""
        )
        return row if self._original_scope(request_id, row.thread_id) is True else None

    def message(self, thread_id, message_id):
        row, errors = self.agent.conversation_store.messages.by_id_report(
            thread_id, message_id, read_budget=self.read_budget
        )
        if errors or row is None or row.thread_id != thread_id:
            return self._unknown()
        return self.verify_message(row)

    def _memory(self, source_id):
        if not hasattr(self, "memories"):
            # 共用正式库的有界权威回读，不逐命中all()或另建访问账。
            from ..memory_store._jsonl_indexing import bounded_authority_records

            records, report = bounded_authority_records(
                self.agent.memory, max_records=_MAX_SOURCES,
                max_bytes=max(0, min(_MAX_SOURCE_BYTES, self.remaining_bytes))
            )
            self.read_budget.charge(size=report.get("scanned_bytes", 0))
            self.memories = {r.entry_id: r for r in records}
            if report.get("partial") is not False:
                self.unknown = True
        record = self.memories.get(source_id)
        if record is None:
            return self._unknown()
        if not long_term_record_matches_scope(record, self.scope):
            return None
        return record.content, {"attributes": record.attributes}, "Memory"

    def resolve(self, source_type, source_id):
        key = (source_type, source_id)
        if key in self.cache:
            return self.cache[key]
        if len(self.cache) >= _MAX_SOURCES:
            raise RuntimeError("HISTORY_SCOPE_QUERY_BUDGET")
        self.cache[key] = None
        try:
            value = None
            if source_type == "conversation_message":
                thread_id, separator, message_id = source_id.partition(":")
                if separator:
                    row = self.message(thread_id, message_id)
                    if row is not None:
                        value = (
                            message_text(row),
                            {
                                "thread_id": row.thread_id,
                                "message_id": row.message_id,
                                "role": row.role,
                                "channel": row.channel,
                            },
                            "Conversation " + row.role,
                        )
            elif source_type == "gateway_request":
                request = self._request(source_id)
                snapshot = (request or {}).get("host_session_space.v1") or {}
                tid = snapshot.get("thread_id") or (
                    (request or {}).get("conversation_runtime") or {}
                ).get("thread_id")
                if request is not None and tid and self._original_scope(source_id, tid) is True:
                    value = (
                        "\n".join(str(request.get(key) or "") for key in ("goal", "response")),
                        {
                            "status": request.get("status", ""),
                            "conversation_runtime": {
                                **{
                                    k: v
                                    for k, v in (request.get("conversation_runtime") or {}).items()
                                    if k in {"thread_id", "task_id", "task_path"}
                                    and isinstance(v, str)
                                },
                                "thread_id": tid,
                                "request_id": source_id,
                            },
                        },
                        "Gateway request",
                    )
            elif source_type == "memory":
                value = self._memory(source_id)
            elif source_type == "skill":
                snapshot = self.agent.current_skill_snapshot()
                entry = snapshot.resolve(source_id) if snapshot is not None else None
                if entry is not None and snapshot.owner_id == self.owner:
                    from ..contracts.gates.skill_guard import (
                        SkillGuardRequest,
                        evaluate_skill_guard_gate,
                    )
                    from .skills import skill_enabled

                    gate = evaluate_skill_guard_gate(
                        Path(entry.path).parent,
                        SkillGuardRequest(source=entry.source, skill_name=entry.name),
                    )
                    if skill_enabled(Path(entry.path)) and gate.allowed:
                        # 当前授权快照摘要才是可公开事实；正文仍走原skill_search版本/guard读口。
                        value = (entry.description, {"skill_id": entry.stable_id}, entry.name)
                else:
                    self.unknown = True
            else:
                self.unknown = True
            self.cache[key] = value
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.source_errors.add(type(exc).__name__)
            self.unknown = True
        return self.cache[key]

    def allowed(self, source_type, source_id):
        return int(self.resolve(source_type, source_id) is not None)

    def result(self, row):
        value = self.resolve(row["source_type"], row["source_id"])
        return LocalSearchResult(
            id=row["id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            title=value[2],
            content=value[0],
            metadata=value[1],
            visibility="private",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _refuse_scope():
    raise SessionHistoryRefusal(
        "HISTORY_SCOPE_NOT_VISIBLE",
        "当前冻结范围不能确认该历史引用可见。",
        "使用当前范围返回的引用；旧来源未证时由本人在未选空间的明确查史入口核对，勿换ID扩大范围。",
    )


def scoped_dispatch(agent, store, params, scope):
    """原DTO和分页不变；呈现同一已核canonical记录，所有mode共用来源裁决。"""
    from ..local_storage.search import record_thread_id
    from .session_history_read import (
        _browse_item,
        render_conversation_message,
        require_current_thread,
    )
    from .session_search_tool import _prioritize_historical_task_refs, _shape_hit, _shape_record

    guard = ScopedHistory(agent, scope)
    try:
        mode = params["mode"]
        if mode == "message":
            tid, resolution = read_thread_scope(agent, params)
            row = guard.message(tid, params["message_id"])
            if row is None:
                _refuse_scope()
            return render_conversation_message(row, tid, resolution, params)
        if mode == "thread_browse":
            tid, source = trusted_thread(agent)
            require_current_thread(tid, source)
            cursor, visible = params.get("cursor"), []
            for _ in range(4):
                page = agent.conversation_store.messages.history_page_report(
                    tid, before=cursor, limit=params.get("limit", 10), read_budget=guard.read_budget
                )
                if page.errors and any(e.get("error_type") == "MessageReadDeferred" for e in page.errors):
                    guard.unknown = True
                    break
                if page.errors:
                    raise SessionHistoryRefusal(
                        "TOOL_INVALID_ARGUMENTS",
                        "原会话分页游标不可读。",
                        "从上一页older_cursor继续，或不带cursor重新读取。",
                    )
                visible = [
                    verified
                    for row in page.rows
                    if (verified := guard.verify_message(row)) is not None
                ]
                cursor = page.before or None
                if visible or cursor is None:
                    break
            return {
                "mode": "thread_browse",
                "thread_id": tid,
                "messages": [_browse_item(row) for row in visible],
                "count": len(visible),
                "older_cursor": cursor,
                "coverage": "partial" if guard.unknown or cursor is not None else "complete",
                "scope_resolution": {"thread_source": source, "current_thread_id": tid},
                "hint": "仅呈现同次核验的可见原消息；有older_cursor继续原分页。缺历史project证明保持partial，不从当前选择补权。",
            }
        if store is None:
            raise SessionHistoryRefusal(
                "TOOL_UNAVAILABLE",
                "本地历史索引不可用。",
                "从当前范围摘要的message_id读取已授权原文。",
            )
        thread_id, thread_source = trusted_thread(agent) if mode == "thread_search" else ("", "")
        if mode == "thread_search":
            require_current_thread(thread_id, thread_source)

        def allowed(kind, ref):
            value = guard.resolve(kind, ref)
            return int(
                value is not None and (not thread_id or record_thread_id(value[1]) == thread_id)
            )

        partial = False
        if mode == "around":
            with store._connection() as db:
                raw = db.execute(
                    "SELECT id,source_type,source_id,created_at,updated_at FROM records WHERE id=?",
                    (params["around_id"],),
                ).fetchone()
            if raw is None or not guard.allowed(raw["source_type"], raw["source_id"]):
                _refuse_scope()
            original = guard.result(dict(raw))
            if params.get("source_type") and original.source_type != params["source_type"]:
                return {
                    "mode": "scroll",
                    "around_id": params["around_id"],
                    "messages": [],
                    "count": 0,
                    "hint": "该锚点不存在于指定source_type；使用同一来源的检索引用。",
                }
            thread_id = record_thread_id(original.metadata)
            key = (original.updated_at, original.updated_at, original.id)
            window = params.get("window", 5)
            before, p1 = search_records_with_authority(
                store,
                query="",
                source_type=params.get("source_type"),
                limit=window,
                authorize=allowed,
                anchor=key,
            )
            after, p2 = search_records_with_authority(
                store,
                query="",
                source_type=params.get("source_type"),
                limit=window,
                authorize=allowed,
                anchor=key,
                after=True,
            )
            records = [
                *[guard.result(row) for row in reversed(before)],
                original,
                *[guard.result(row) for row in after],
            ]
            partial = p1 or p2
            result = {
                "mode": "scroll",
                "around_id": params["around_id"],
                "context_kind": "same_thread_records" if thread_id else "record_time_neighbors",
                "thread_id": thread_id,
                "window": window,
                "messages": [_shape_record(row, anchor_id=original.id) for row in records],
                "count": len(records),
                "messages_before": len(before),
                "messages_after": len(after),
            }
        else:
            limit = params.get("limit", 5 if mode != "recent" else 10)
            source_type = params.get("source_type") or (
                "conversation_message" if mode == "thread_search" else None
            )
            fetch_limit = (
                20 if mode == "search" and source_type in {None, "gateway_request"} else limit
            )
            rows, partial = search_records_with_authority(
                store,
                query=str(params.get("query") or ""),
                source_type=source_type,
                limit=fetch_limit + 1,
                authorize=allowed,
            )
            limited = len(rows) > limit
            partial = partial or limited
            records = [guard.result(row) for row in rows[:fetch_limit]]
            if mode in {"search", "thread_search"}:
                hits = _prioritize_historical_task_refs(
                    [_shape_hit(row, snippet_for=params.get("query", "")) for row in records]
                )[:limit]
                result = {
                    "mode": "thread_discover" if mode == "thread_search" else "discover",
                    "query": params.get("query", ""),
                    "results": hits,
                    "count": len(hits),
                }
                if mode == "thread_search":
                    result.update(
                        thread_id=thread_id,
                        scope_resolution={
                            "thread_source": thread_source,
                            "current_thread_id": thread_id,
                        },
                    )
            else:
                result = {
                    "mode": "browse",
                    "results": [_shape_record(row, preview=True) for row in records],
                    "count": len(records),
                }
        result.update(
            coverage="partial" if partial or guard.unknown else "complete",
            scope_diagnostics=(["HISTORY_QUERY_OR_RESULT_BUDGET"] if partial else []) + sorted(guard.source_errors)[:8]
            + (["HISTORY_SOURCE_UNPROVEN"] if guard.unknown else []),
            hint="仅返回原来源证明可见的内容；partial不能认定没有结果。缩小query/source_type，按原around/message游标续读；旧来源需本人在未选空间的主动查史入口核对。",
        )
        return result
    finally:
        guard.close()
