# LLM: 审计只读 Gateway 请求记录里宿主写下的结构化事实：决策观察块（model_selection_observation、capability_presentation_observation）
#   与请求结果（状态、错误码、渠道、私聊/群聊、耗时）。决策观察按 conversation_claim.thread_id 归属 owner；请求结果优先用
#   宿主写进终态响应的 owner_id，旧记录再退回线程归属。字段白名单投影；不读 prompt/goal/正文/附件/用户可见文案，也不读日志。
#   扫描有界：只看修改时间落在窗口内的记录，按新到旧最多读 _SCAN_LIMIT_COUNT 份，超出如实标 truncated。只读，不写任何文件。
#   调用方：GatewayTaskBindingWriter.decision_audit_observations / request_audit_outcomes（audit_records 工具经宿主运行参数调用）。
# 模块用途: 为审计工具提供 Gateway 请求记录里的决策观察和请求结果（谁的请求、成败、错误码与处理建议），不含任何对话内容。
from __future__ import annotations

import os
import sqlite3
from dataclasses import KW_ONLY, dataclass
from pathlib import Path

from .request_binding import CAPABILITY_OBSERVATION_KEY, MODEL_OBSERVATION_KEY

# 一次审计最多读这么多份请求记录（按修改时间新到旧），保证工具调用有界
_SCAN_LIMIT_COUNT = 300
# 选模型观察只投影这些宿主写下的结构化字段
_MODEL_FIELDS = ("status", "reason", "requested_mode", "choice", "adopted", "adoption_eligibility", "frozen_profile_id")
# 能力推荐观察只投影这些字段（不含工具名清单等细节）
_CAPABILITY_FIELDS = ("point", "mode", "status", "reason", "adopted", "retain_reason")
# 选模型采用的两个状态设计上不随请求结束结清（采用事务只记发送意图，HTTP 事实在用量账），给读审计的人一句固定说明；
# 只按结构化 status 取，其它状态不附说明，也不参与任何判定。
_MODEL_STATUS_NOTES = {
    "send_intent_uncertain": ("已采用该模型并记下发送意图；这个状态设计上不随请求结束结清，不代表供应商一定收到。"
                              "实际发往哪个模型、成功几次以 usage（会话用量账）为准。"),
    "commit_unknown": "采用写入线程的结果未能确认；本请求不发业务请求、也不跨模型重发，不要当成已经换了模型。",
}


# LLM: 只取标量字段；非标量（列表、字典）一律丢弃，防止把大块内容带进审计结果。
# 函数用途: 按白名单从一个观察块里取出可展示的字段。
def _pick(block: dict, fields: tuple[str, ...]) -> dict:
    return {key: block[key] for key in fields if isinstance(block.get(key), (str, int, float, bool))}


# LLM: 一份请求记录可同时有选模型观察（至多一条）和能力推荐观察（至多 8 条）；结构不符的块按不存在处理。
#   选模型采用状态（send_intent_uncertain/commit_unknown）附固定 status_note，说明它们设计上不结清、HTTP 事实看用量账。
# 函数用途: 把一份请求记录里的决策观察转成审计条目列表。
def _observations(payload: dict) -> list[dict]:
    result = []
    model = payload.get(MODEL_OBSERVATION_KEY)
    if isinstance(model, dict):
        entry = {"kind": "model_selection", "point": "model_selection", **_pick(model, _MODEL_FIELDS)}
        note = _MODEL_STATUS_NOTES.get(str(entry.get("status") or ""))
        if note:
            entry["status_note"] = note
        result.append(entry)
    capability = payload.get(CAPABILITY_OBSERVATION_KEY)
    entries = capability.get("entries") if isinstance(capability, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict):
            result.append({"kind": "capability_presentation", **_pick(entry, _CAPABILITY_FIELDS)})
    return result


# LLM: thread_owners 把线程编号映射到 owner 编号，由调用方按已认证 owner（或管理员许可的全部 owner）解析得到；
#   不在映射里的请求一律不返回。坏文件只计数。结果最新在前，至多 limit 条。
# 函数用途: 读取窗口内属于这些会话的决策观察，并报告扫描范围与是否截断。
def decision_observation_records(paths: object, *, thread_owners: dict[str, str], since: float, limit: int) -> dict:
    from .gateway_history_index import GatewayHistoryIndex
    query = OutcomeQuery(frozenset(thread_owners.values()), thread_owners, "", since, limit)
    page = GatewayHistoryIndex(paths).audit_page(query)
    entries = []
    for row in page["entries"]:
        try:
            payload, stamp, _ = _canonical_payload(paths, row["request_id"])
            claim = payload.get("conversation_claim") or {}
            thread_id = str(claim.get("thread_id") or "")
            if thread_id not in thread_owners:
                continue
            entries.extend({**item, "request_id": row["request_id"], "thread_id": thread_id,
                            "owner_id": thread_owners[thread_id], "recorded_at": stamp} for item in _observations(payload))
        except (OSError, ValueError, sqlite3.DatabaseError):
            page["unreadable"] += 1
    return {**page, "entries": entries[:limit], "matched": len(entries),
            "truncated": page["truncated"] or len(entries) > limit}


UNATTRIBUTED_OWNER = "unattributed"


# LLM: 归属只认两种结构化事实：宿主写进终态响应的 owner_id（权威），或旧记录的 conversation_claim.thread_id 落在调用方
#   给出的线程映射里；都没有就返回空串（未归属），不按 user_id 或渠道猜。
# 函数用途: 判断一份请求记录属于哪个 owner。
def _record_owner(payload: dict, thread_owners: dict[str, str]) -> str:
    terminal = payload.get("terminal_response") if isinstance(payload.get("terminal_response"), dict) else {}
    owner_id = str(terminal.get("owner_id") or payload.get("owner_id") or "").strip()
    if owner_id:
        return owner_id
    claim = payload.get("conversation_claim") if isinstance(payload.get("conversation_claim"), dict) else {}
    return thread_owners.get(str(claim.get("thread_id") or ""), "")


# LLM: 错误码的处理建议取自唯一错误分类表（类别、可否重试、建议动作、恢复提示），不读异常原文或用户可见文案。
# 函数用途: 把一个错误码变成可展示的处理建议。
def _error_summary(code: str) -> dict:
    from ..contracts.error_taxonomy import error_contract

    contract = error_contract(code)
    return {"category": contract.category, "retryable": contract.retryable,
            "recommended_action": contract.recommended_action, "recovery_hint": contract.recovery_hint}


# LLM: 只投影标量白名单字段；终态字段优先取 terminal_response，再退回记录顶层。
# 函数用途: 把一份请求记录变成一条请求结果审计条目。
def _outcome_entry(payload: dict, mtime: float, owner_id: str) -> dict:
    terminal = payload.get("terminal_response") if isinstance(payload.get("terminal_response"), dict) else {}
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    code = str(payload.get("error_code") or terminal.get("error_code") or "").strip()
    entry = {"request_id": str(payload.get("id") or payload.get("request_id") or ""), "recorded_at": round(mtime, 3),
             "owner_id": owner_id or UNATTRIBUTED_OWNER, "channel": str(meta.get("channel") or conversation.get("channel") or ""),
             "chat_type": str(meta.get("channel_chat_type") or ""), "status": str(payload.get("status") or terminal.get("status") or ""),
             "error_code": code}
    from ..adapter.protocol import provider_message_reference
    entry["request_ref"] = provider_message_reference(entry["request_id"])
    entry.update(_pick(terminal, ("duration_seconds", "tool_rounds")))
    if code:
        entry["error"] = _error_summary(code)
    return entry


# LLM: allowed_owners 为 None 只能由已通过跨用户审计许可的调用方传入（全部 owner，含未归属）；thread_id 非空时只看这个会话
#   （按 conversation_claim.thread_id）；thread_owners 只用于给没有 owner_id 的旧记录归属。since/limit 为时间窗与条数上限。
# 类用途: 一次请求结果审计的过滤条件。
@dataclass(frozen=True)
class OutcomeQuery:
    allowed_owners: frozenset | None
    thread_owners: dict
    thread_id: str
    since: float
    limit: int
    _: KW_ONLY
    request_id: str = ""
    error_code: str = ""
    channel: str = ""
    until: float | None = None
    cursor: str = ""
    message_ref: str = ""
    request_ref: str = ""
    tool_name: str = ""
    status: str = ""
    min_duration_ms: float | None = None


# 函数用途: 判断一份请求记录是否落在本次查询的 owner 与会话范围内。
def _outcome_in_scope(payload: dict, owner_id: str, query: OutcomeQuery) -> bool:
    if query.allowed_owners is not None and owner_id not in query.allowed_owners:
        return False
    if not query.thread_id:
        return True
    claim = payload.get("conversation_claim") if isinstance(payload.get("conversation_claim"), dict) else {}
    return str(claim.get("thread_id") or "") == query.thread_id


# LLM: 同一请求在 done/failed 与 terminal 各有一份，按请求编号去重；坏文件只计数。结果最新在前，至多 limit 条，
#   另附按状态、错误码、渠道的计数。只读，不写任何文件。
# 函数用途: 读取窗口内的请求结果，回答“哪些请求失败了、为什么、属于谁”。
def request_outcome_records(paths: object, query: OutcomeQuery) -> dict:
    import base64
    import hashlib
    import json
    from dataclasses import replace

    from .gateway_history_index import GatewayHistoryIndex

    _validate_query(query)
    binding = hashlib.sha256(json.dumps([str(paths.root), sorted(query.allowed_owners) if query.allowed_owners is not None else None,
        query.thread_id, query.since, query.until, query.limit, query.error_code, query.channel, query.status]).encode()).hexdigest()
    if query.cursor.startswith("h0:"):
        try:
            state = json.loads(base64.urlsafe_b64decode(query.cursor[3:]))
            if state.get("binding") != binding:
                raise ValueError("audit.cursor history transition scope/filter/window mismatch; restart without cursor; not_started")
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("audit.cursor history transition invalid or mismatched; restart without cursor; not_started") from error
    if query.request_id or query.cursor.startswith(("h:", "h0:")):
        page = GatewayHistoryIndex(paths).audit_page(replace(query, cursor=(query.cursor[2:] if query.cursor.startswith("h:") else "")))
        if page.get("next_cursor"):
            page["next_cursor"] = "h:" + page["next_cursor"]
        return page
    history_start = "h0:" + base64.urlsafe_b64encode(json.dumps({"since": query.since, "until": query.until, "binding": binding}).encode()).decode()
    active = replace(query, cursor=query.cursor[2:] if query.cursor.startswith("q:") else query.cursor)
    for _ in range(2):
        page = request_queue_records(paths, active)
        if page["entries"] or page["scanned"]:
            page["next_cursor"] = "q:" + page["next_cursor"] if page["next_cursor"] else history_start
            page["truncated"] = True
            return page
        if not page["next_cursor"]:
            result = GatewayHistoryIndex(paths).audit_page(replace(query, cursor=""))
            if result.get("next_cursor"):
                result["next_cursor"] = "h:" + result["next_cursor"]
            return result
        active = replace(active, cursor=page["next_cursor"])
    return {**page, "next_cursor": "q:" + page["next_cursor"]}



# 函数用途: 按状态、错误码、渠道汇总请求结果条数。
def _outcome_counts(entries: list[dict]) -> dict:
    counts: dict[str, dict[str, int]] = {"status": {}, "error_code": {}, "channel": {}}
    for entry in entries:
        for field in counts:
            key = str(entry.get(field) or "none")
            counts[field][key] = counts[field].get(key, 0) + 1
    return counts


__all__ = ["UNATTRIBUTED_OWNER", "OutcomeQuery", "decision_observation_records", "request_outcome_records",
           "request_queue_records", "message_lifecycle_records"]


def _validate_query(query):
    """直接调用者同样先校验，任意编号不能转换成目录遍历。"""
    from ..tooling.audit_lifecycle_topic import finite_number
    from .recovery_pending_index import projection_key

    if type(query.limit) is not int or not 1 <= query.limit <= 100:
        raise ValueError("audit.limit requires integer 1..100; not_started")
    for field in ("request_id", "request_ref", "message_ref"):
        value = getattr(query, field)
        if value:
            if len(value) > 256:
                raise ValueError(f"audit.{field} exceeds 256; not_started")
            projection_key(value)
    for field in ("error_code", "channel", "tool_name", "status"):
        value = getattr(query, field)
        if not isinstance(value, str) or len(value) > 128:
            raise ValueError(f"audit.{field} requires bounded string; not_started")
    for field in ("since", "until", "min_duration_ms"):
        value = getattr(query, field)
        if value is not None and (not finite_number(value) or value < 0):
            raise ValueError(f"audit.{field} requires finite nonnegative number; not_started")
    if query.until is not None and query.until < query.since:
        raise ValueError("audit.until precedes since; not_started")
    if not isinstance(query.cursor, str) or len(query.cursor) > 65536:
        raise ValueError("audit.cursor requires bounded string; not_started")


def _canonical_payload(paths, request_id, budget=None):
    """终态优先，固定规范地址；热文件最多1MiB，不创建读锁或修复。"""
    import json

    from ..common.nofollow_fs import read_bytes_beneath
    from .recovery_pending_index import projection_key
    projection_key(request_id)
    for folder in (paths.terminal, paths.processing, paths.inbox, paths.done, paths.failed):
        path = folder / (request_id + ".json")
        raw = read_bytes_beneath(paths.root, path.relative_to(paths.root).parts, max_bytes=min(1024 * 1024, budget["bytes"]) if budget is not None else 1024 * 1024)
        if raw is None and folder == paths.terminal:
            raw = _archived_request_bytes(paths, path)
        if raw is not None:
            if budget is not None:
                budget["bytes"] -= len(raw)
            payload = json.loads(raw)
            if not isinstance(payload, dict) or str(payload.get("id") or payload.get("request_id") or "") != request_id:
                raise ValueError("request_identity_mismatch")
            try:
                stamp = path.stat().st_mtime
            except FileNotFoundError:
                stamp = payload.get("ended_at", 0)
            return payload, stamp, "terminal" if folder == paths.terminal else folder.name
    return None, 0, "missing"


def _audit_result(entries=(), *, scanned=0, unreadable=0, next_cursor="", reason="", complete=False):
    entries = list(entries)
    return {"available": True, "entries": entries, "counts": _outcome_counts(entries), "counts_scope": "page",
            "matched": len(entries), "scanned": scanned, "unreadable": unreadable, "next_cursor": next_cursor,
            "truncated": bool(next_cursor) or not complete,
            "coverage": {"status": "complete" if complete else "partial", "reason": reason}}


def _filtered_entry(paths, request_id, query, budget=None):
    payload, stamp, source = _canonical_payload(paths, request_id, budget)
    if payload is None:
        return None
    owner = _record_owner(payload, query.thread_owners)
    if not _outcome_in_scope(payload, owner, query):
        return None
    entry = _outcome_entry(payload, stamp, owner)
    claim = payload.get("conversation_claim") if isinstance(payload.get("conversation_claim"), dict) else {}
    entry.update(thread_id=str(claim.get("thread_id") or ""), source=source)
    for field in ("error_code", "channel", "status"):
        if getattr(query, field) and entry[field] != getattr(query, field):
            return None
    if stamp < query.since or (query.until is not None and stamp > query.until):
        return None
    terminal = payload.get("terminal_response") if isinstance(payload.get("terminal_response"), dict) else {}
    entry["stages_ms"] = _numeric_map(terminal.get("stages_ms"))
    entry["request_stage_timing"] = _numeric_map(payload.get("request_stage_timing"))
    return entry


def _numeric_map(value):
    from ..tooling.audit_lifecycle_topic import _TIMING_FIELDS, finite_number
    return {key: item for key, item in value.items() if key in _TIMING_FIELDS
            and finite_number(item)} if isinstance(value, dict) else {}


def _index_audit_page(index, query):
    import base64
    import hashlib
    import json
    import sqlite3
    import time

    from .recovery_pending_index import private_projection_connection

    _validate_query(query)
    entries, scanned, unreadable = [], 0, 0
    if query.request_id:
        try:
            entry = _filtered_entry(index.paths, query.request_id, query)
            return _audit_result([entry] if entry else [], scanned=1, complete=bool(entry),
                                 reason="" if entry else "no_authorized_observation")
        except (OSError, ValueError, sqlite3.DatabaseError) as error:
            return {**_audit_result(scanned=1, unreadable=1, reason=type(error).__name__),
                    "read_error": _read_failure(error)}
    fingerprint = hashlib.sha256(json.dumps([sorted(query.allowed_owners) if query.allowed_owners is not None else None,
        query.thread_id, query.since, query.until, query.error_code, query.channel, query.status,
        query.limit], sort_keys=True).encode()).hexdigest()
    state = {}
    if query.cursor:
        try:
            state = json.loads(base64.urlsafe_b64decode(query.cursor))
        except (ValueError, TypeError) as error:
            raise ValueError("audit.cursor invalid; restart without cursor; not_started") from error
        if not isinstance(state, dict) or state.get("filter") != fingerprint:
            raise ValueError("audit.cursor filter/scope/window mismatch; restart without cursor; not_started")
    deadline = time.monotonic() + .05
    budget = {"bytes": 8 * 1024 * 1024}
    try:
        info = index.path.stat()
        with private_projection_connection(index.path, read_only=True, blocking=False, deadline=deadline) as conn:
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
            generation = f"{info.st_dev}:{info.st_ino}:audit-v2:" + index._meta(conn, "audit_revision", "0")
            if state.get("generation") and state.get("generation") != generation:
                raise ValueError("audit.cursor source generation changed; restart without cursor; not_started")
            after = state.get("after", "")
            after_stamp = state.get("after_stamp")
            clauses, args = ["recorded_at>=?"], [query.since]
            if after:
                if not isinstance(after, str) or type(after_stamp) not in (int, float):
                    raise ValueError("audit.cursor invalid seek key; restart without cursor; not_started")
                clauses.append("(recorded_at,request_id)<(?,?)")
                args.extend((after_stamp, after))
            if query.until is not None:
                clauses.append("recorded_at<=?")
                args.append(query.until)
            # 选一个等值前缀的有序索引；其它过滤在固定候选页内核canonical，避免稀疏过滤拖出全表扫描。
            source_field = "error_code" if query.error_code else "channel" if query.channel else ""
            if source_field:
                clauses.append(source_field + "=?")
                args.append(getattr(query, source_field))
            rows = conn.execute("SELECT request_id,recorded_at FROM audit_keys WHERE " + " AND ".join(clauses)
                                + " ORDER BY recorded_at DESC,request_id DESC LIMIT ?",
                                (*args, min(_SCAN_LIMIT_COUNT, query.limit))).fetchall()
            invalid_projection = index._meta(conn, "audit_invalid") == "1"
            complete = index._meta(conn, "audit_complete") == "1" and not invalid_projection
            if query.since > 0 and conn.execute("SELECT 1 FROM audit_keys WHERE recorded_at=0 LIMIT 1").fetchone():
                complete = False
            for row in rows:
                if time.monotonic() >= deadline or budget["bytes"] <= 0:
                    break
                after = row["request_id"]
                after_stamp = row["recorded_at"]
                scanned += 1
                try:
                    entry = _filtered_entry(index.paths, after, query, budget)
                except (OSError, ValueError, sqlite3.DatabaseError):
                    unreadable += 1
                    continue
                if entry:
                    entries.append(entry)
            more = len(rows) == min(_SCAN_LIMIT_COUNT, query.limit) or scanned < len(rows)
            next_cursor = base64.urlsafe_b64encode(json.dumps({"filter": fingerprint, "generation": generation,
                "after": after, "after_stamp": after_stamp, "since": query.since, "until": query.until}).encode()).decode() if more else ""
            stopped = scanned < len(rows)
            return _audit_result(entries, scanned=scanned, unreadable=unreadable, next_cursor=next_cursor,
                complete=complete and not unreadable and not stopped,
                reason="source_budget_exhausted" if stopped else "projection_backfill_invalid_or_oversized" if invalid_projection else "" if complete else "projection_backfill_incomplete")
    except (OSError, sqlite3.DatabaseError) as error:
        return {**_audit_result(entries, scanned=scanned, unreadable=unreadable, next_cursor=query.cursor,
                             reason="projection_unavailable:" + type(error).__name__), "read_error": _read_failure(error)}


def request_queue_records(paths, query):
    """仅活动目录的原生游标页；不按年龄推断孤儿或补全历史。"""
    import base64
    import hashlib
    import json
    import time

    from .recovery_pending_index import directory_page

    _validate_query(query)
    if query.request_id:
        report = request_outcome_records(paths, query)
        report["entries"] = [entry for entry in report["entries"] if entry.get("source") in ("pending", "processing")]
        return report
    fingerprint = hashlib.sha256(json.dumps([str(paths.root), sorted(query.allowed_owners) if query.allowed_owners is not None else None,
        query.thread_id, query.since, query.until, query.error_code, query.channel, query.status, query.limit]).encode()).hexdigest()
    try:
        state = json.loads(base64.urlsafe_b64decode(query.cursor)) if query.cursor else {}
    except (ValueError, TypeError) as error:
        raise ValueError("audit.cursor invalid; restart without cursor; not_started") from error
    if not isinstance(state, dict):
        raise ValueError("audit.cursor requires object state; restart without cursor; not_started")
    if state and state.get("filter") != fingerprint:
        raise ValueError("audit.cursor queue scope/filter mismatch; restart without cursor; not_started")
    phase, cookie = state.get("phase", 0), state.get("cookie", "")
    if type(phase) is not int or phase not in (0, 1) or not isinstance(cookie, str):
        raise ValueError("audit.cursor invalid active page; restart without cursor; not_started")
    if (not isinstance(state.get("tail", []), list) or len(state.get("tail", [])) > 128
            or any(not isinstance(name, str) or len(name) > 256 for name in state.get("tail", []))):
        raise ValueError("audit.cursor invalid active tail; restart without cursor; not_started")
    entries, scanned, unreadable = [], 0, 0
    folder = (paths.inbox, paths.processing)[phase]
    deadline = time.monotonic() + .05
    budget = {"bytes": 8 * 1024 * 1024}
    source_entries_scanned = 0
    try:
        info = folder.stat()
        generation = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}"
        if state.get("generation") and state.get("generation") != generation:
            raise ValueError("audit.cursor active directory changed; restart without cursor; not_started")
        tail = state.get("tail", [])
        if tail:
            names, complete = tail, state.get("end", False)
        else:
            names, cookie, complete = directory_page(folder, cookie)
            source_entries_scanned = len(names)
        tail = list(names[query.limit:])
        end = complete
        if tail:
            complete = False
        for offset, name in enumerate(names[:query.limit]):
            if time.monotonic() >= deadline or budget["bytes"] <= 0:
                tail = list(names[offset:query.limit]) + tail
                complete = False
                break
            scanned += 1
            if not name.endswith(".json"):
                continue
            try:
                # 活动队列不继承默认时间窗；since=0由调用者明确传入。
                entry = _filtered_entry(paths, name[:-5], __import__("dataclasses").replace(query, since=0, until=None), budget)
                if entry and entry["source"] == folder.name:
                    queue_entry = {**entry, "state": entry["status"], "wait_reason": "unknown",
                                   "orphan_reason": "unknown", "age_seconds": max(0, time.time()-entry["recorded_at"])}
                    from . import queue_service
                    observe = getattr(queue_service, "observe_pending_request", None)
                    if callable(observe) and folder == paths.inbox:
                        try:
                            observation = observe(paths, name[:-5], blocking=False, deadline=deadline)
                            for field in ("state", "wait_reason", "orphan_reason", "coverage"):
                                if field in observation:
                                    queue_entry[field] = observation[field]
                        except (OSError, ValueError, sqlite3.DatabaseError) as error:
                            queue_entry["coverage"] = {"status": "partial", "reason": "pending_observation_unavailable"}
                            queue_entry["read_error"] = _read_failure(error)
                    entries.append(queue_entry)
            except (OSError, ValueError, sqlite3.DatabaseError):
                unreadable += 1
    except FileNotFoundError:
        complete, generation, tail, end = True, "missing", [], True
    except OSError as error:
        return {**_audit_result(entries, scanned=scanned, unreadable=unreadable + 1, next_cursor=query.cursor,
                               reason="active_source_unavailable"), "read_error": _read_failure(error)}
    if complete and phase == 0:
        phase, cookie, generation, complete = 1, "", "", False
    next_state = {"filter": fingerprint, "phase": phase, "cookie": cookie, "tail": tail, "end": end, "since": query.since, "until": query.until}
    if generation:
        next_state["generation"] = generation
    cursor = "" if complete else base64.urlsafe_b64encode(json.dumps(next_state).encode()).decode()
    return {**_audit_result(entries, scanned=scanned, unreadable=unreadable, next_cursor=cursor,
                         complete=False, reason="active_page_only"), "source_entries_scanned": source_entries_scanned,
            "bytes_read": 8 * 1024 * 1024 - budget["bytes"]}


def message_lifecycle_records(paths, query):
    """精确消息引用连接原入站、输入和规范请求；缺证保持 unknown。"""
    from dataclasses import replace

    from ..tooling.audit_lifecycle_topic import metadata_only

    _validate_query(query)
    if sum(bool(getattr(query, key)) for key in ("request_id", "request_ref", "message_ref")) != 1:
        raise ValueError("lifecycle requires exactly one request_id/request_ref/message_ref; not_started")
    sources = {}
    try:
        from ..adapter.lifecycle_records import adapter_lifecycle_records
        adapter = adapter_lifecycle_records(paths.root / "channel-delivery", allowed_owners=query.allowed_owners,
            message_ref=query.message_ref, request_id=query.request_id, request_ref=query.request_ref,
            limit=query.limit, cursor="")
        sources["adapter"] = adapter.get("coverage", {"status": "unknown"})
        if sources["adapter"].get("status", sources["adapter"].get("state")) == "ambiguous":
            return {"entries": [], "coverage": {"status": "ambiguous", "reason": "message_ref_collision"}, "sources": sources}
        matches = adapter.get("matches", [])
    except (ImportError, OSError, ValueError) as error:
        sources["adapter"] = {"status": "unavailable", "reason": type(error).__name__}
        matches = []
    if query.request_id and not matches:
        matches = [{"request_id": query.request_id}]
    entries = []
    for match in matches[:query.limit]:
        request_id = str(match.get("request_id") or "")
        if not request_id:
            continue
        _validate_query(replace(query, request_id=request_id))
        try:
            payload, stamp, source = _canonical_payload(paths, request_id)
            receipt = None
            import json
            from types import SimpleNamespace

            from ..common.nofollow_fs import read_bytes_beneath
            input_error = ""
            try:
                raw = read_bytes_beneath(paths.root, ("input_receipts", request_id + ".json"), max_bytes=1024 * 1024)
                if raw is not None:
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("request_id") != request_id:
                        raise ValueError("input_receipt_identity_mismatch")
                    receipt = SimpleNamespace(prepared_request=data.get("prepared_request") or {},
                                              target_turn_id=data.get("target_turn_id") or "", to_dict=lambda: data)
            except (OSError, ValueError, sqlite3.DatabaseError) as error:
                input_error = type(error).__name__
            if payload is None and receipt is not None:
                prepared = receipt.prepared_request
                owner = _record_owner(prepared, query.thread_owners)
            else:
                owner = _record_owner(payload or {}, query.thread_owners)
            owner = owner or str(match.get("owner_id") or "")
            if query.allowed_owners is not None and owner not in query.allowed_owners:
                continue
            if query.thread_id and not _outcome_in_scope(payload or {}, owner, query):
                continue
            entry = metadata_only(match)
            entry["request_id"] = request_id
            entry["owner_id"] = owner or UNATTRIBUTED_OWNER
            stage_coverage = {"request": {"status": "observed" if payload else "missing", "reason": source},
                              "input": {"status": "observed" if receipt else "unavailable" if input_error else "missing", "reason": input_error}}
            if payload:
                request_entry = _filtered_entry(paths, request_id, replace(query, since=0, until=None))
                entry["request"] = metadata_only(request_entry or {})
                entry["runtime_authority"] = metadata_only(payload.get("runtime_authority") or {})
            if receipt:
                entry["input"] = metadata_only(receipt.to_dict())
                target_id = receipt.target_turn_id
                if target_id and target_id != request_id:
                    target, _, _ = _canonical_payload(paths, target_id)
                    if target is not None and _record_owner(target, query.thread_owners) == owner:
                        entry["target_request"] = metadata_only(_outcome_entry(target, 0, owner))
                        entry["runtime_authority"] = metadata_only(target.get("runtime_authority") or {})
                    else:
                        stage_coverage["target_request"] = {"status": "missing", "reason": "target_not_proven"}
            entry["coverage"] = stage_coverage
            entries.append(entry)
        except (OSError, ValueError, sqlite3.DatabaseError) as error:
            sources["request"] = {"status": "unavailable", "reason": type(error).__name__}
    return {"entries": entries, "coverage": {"status": "partial", "reason": "per_source_evidence_only"}, "sources": sources}


def _archived_request_bytes(paths, path):
    """原封存定位器的只读点查；不调用会重建缺失索引的便利读口。"""
    import base64
    import hashlib
    import json
    import re

    from ..common.nofollow_fs import open_readonly_file_beneath
    from ..contracts.optimization_contracts import require_schema_generation
    from .recovery_pending_index import private_projection_connection

    if not (paths.root / "archive" / "receipts-manifest.json").exists():
        return None
    relative = str(path.relative_to(paths.root))
    with private_projection_connection(paths.root / "archive" / "receipts-index.sqlite3", read_only=True,
                                       blocking=False) as conn:
        row = conn.execute("SELECT segment,offset,length,digest FROM receipts WHERE path=?", (relative,)).fetchone()
    if row is None:
        return None
    segment, offset, length, digest = row
    if (not isinstance(segment, str) or not re.fullmatch(r"receipts-seg-[0-9]+\.jsonl", segment)
            or type(offset) is not int or offset < 0 or type(length) is not int or not 0 < length <= 2 * 1024 * 1024):
        raise ValueError("archive_locator_invalid")
    descriptor = open_readonly_file_beneath(paths.root, ("archive", segment))
    with os.fdopen(descriptor, "rb") as handle:
        handle.seek(offset)
        record = json.loads(handle.read(length))
    try:
        require_schema_generation(record)
    except RuntimeError as error:
        raise ValueError("archive_schema_generation_unsupported") from error
    body = base64.b64decode(record["body"], validate=True)
    if record.get("path") != relative or record.get("digest") != digest or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("archive_digest_mismatch")
    return body


def _read_failure(error):
    """异常只公开类型、系统/SQLite编号和JSON位置，不回传文件名或坏正文。"""
    cause = error.__cause__ or error
    facts = {"error_type": type(error).__name__, "cause_type": type(cause).__name__,
             "error_code": getattr(cause, "sqlite_errorname", None) or "AUDIT_SOURCE_READ_UNAVAILABLE",
             "effect_outcome": "not_started", "next_action": "保留 unknown；核实原索引存在、权限和维护覆盖，再原样重试此只读查询。"}
    for key in ("errno", "sqlite_errorcode", "lineno", "colno"):
        if type(getattr(cause, key, None)) is int:
            facts[key] = getattr(cause, key)
    return facts
