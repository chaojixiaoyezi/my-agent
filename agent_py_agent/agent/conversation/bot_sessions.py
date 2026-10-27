# LLM: slots.json是前台Bot槽位的唯一权威；轮换仅封闭前台，不改后台任务、Goal、claim或线程active状态。
# 模块用途: 管理双Bot的有界事务、可恢复轮换、原线程历史和最近消息投影。
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import nullcontext
from dataclasses import replace

from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..common.opaque_id import validate_opaque_id
from ..gateway_parts.io import locked_file_transition
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from .bot_tool_policy import normalize_bot_mode
from .learning_source import unknown_projection, user_dialogue_projection
from .models import ConversationThread
from .store_threads import create_thread_atomic

PROTECTED_BOT_FIELDS = frozenset({"bot_mode", "bot_generation", "bot_slot_id", "learning_excluded",
                                  "foreground_closed", "conversation_archive_state"})
_PREFIX = ("data", "bot_sessions")
_SLOTS = (*_PREFIX, "slots.json")
_MAX_STATE = 2 * 1024 * 1024
TAIL_BYTES = 65536


# LLM: bool/NaN/Infinity不构成前台活动时间；数字仅用于度量，不建立运行身份。
# 函数用途: 归一可用的非负消息时间，防止污染idle基准。
def _foreground_timestamp(value):
    if type(value) not in (int, float):
        return None
    try:
        stamp = float(value)
    except OverflowError:
        return None
    return stamp if math.isfinite(stamp) and stamp >= 0 else None


# LLM: 摘要只用于内部索引和确定性ID，不是授权证明；输入必须来自宿主冻结参数。
# 函数用途: 为槽位、回执和线程计算稳定逻辑编号。
def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


# LLM: 只读取线程宿主管理的顶层metadata；page_context和消息metadata不能成为线程身份权威。
# 函数用途: 投影线程的受保护Bot字段。
def bot_thread_fields(thread):
    return {key: thread.metadata[key] for key in PROTECTED_BOT_FIELDS if key in thread.metadata}


# LLM: 固定块逆读且拼齐完整JSONL行；单行超过原历史上限明确拒绝，不把64KiB首片丢失当空历史。
# 函数用途: 从给定字节边界向前生成完整行及其起点，不扫描无关前缀。
def _reverse_bot_lines(handle, through):
    from ..plugin_host.history import HISTORY_RECORD_MAX_BYTES
    position, pending, end = through, b"", through
    while position:
        start = max(0, position - TAIL_BYTES)
        handle.seek(start)
        chunk = handle.read(position - start)
        if len(chunk) != position - start:
            raise HostAPIError("CURSOR_EXPIRED", 409)
        pending, position = chunk + pending, start
        while (separator := pending.rfind(b"\n", 0, len(pending) - 1)) >= 0:
            raw, pending = pending[separator + 1:], pending[:separator + 1]
            if len(raw) > HISTORY_RECORD_MAX_BYTES:
                raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
            end -= len(raw)
            yield raw, end
        if len(pending) > HISTORY_RECORD_MAX_BYTES:
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    if pending:
        yield pending, 0


# LLM: thread_ids只能由已核owner/模式的canonical代次构造；before绑定逻辑线程/FD身份/行边界，内部工具行不公开。
# 函数用途: 逆序跨代读取一页至多15条可见user/assistant消息，输出按时间正序及下页边界。
def bot_message_page(store, thread_ids, *, limit=15, byte_budget=16384, before=None):
    from ..plugin_host.history import _entry, _message, _open, _visible_content
    if type(limit) is not int or not 1 <= limit <= 15:
        raise HostAPIError("INVALID_SCHEMA", 400)
    if before and before.get("thread_id") not in thread_ids:
        raise HostAPIError("CURSOR_EXPIRED", 409)
    start = thread_ids.index(before["thread_id"]) if before else 0
    rows, used, next_position = [], 0, None
    for tid in thread_ids[start:]:
        try:
            handle = _open(store, tid)
        except FileNotFoundError:
            if before and tid == before["thread_id"]:
                raise HostAPIError("CURSOR_EXPIRED", 409) from None
            continue
        with handle:
            info = os.fstat(handle.fileno())
            identity = [info.st_dev, info.st_ino]
            through = before["offset"] if before and tid == before["thread_id"] else info.st_size
            if (type(through) is not int or not 0 <= through <= info.st_size
                    or before and tid == before["thread_id"] and before.get("identity") != identity):
                raise HostAPIError("CURSOR_EXPIRED", 409)
            for raw, offset in _reverse_bot_lines(handle, through):
                entry = _entry(raw, tid)
                visible = _visible_content(entry) if entry.role in {"user", "assistant"} else None
                if visible is None:
                    continue
                if len(rows) >= limit or used >= byte_budget:
                    return {"messages": list(reversed(rows)), "before": next_position, "has_more": True}
                row = _message(entry, visible, offset, byte_budget - used)
                rows.append(row)
                used += len(row["content"].encode())
                next_position = {"thread_id": tid, "offset": offset, "identity": identity}
    return {"messages": list(reversed(rows)), "before": None, "has_more": False}


# LLM: 只扩展同owner/home/slot/mode的已归档代次，最多4096条；不从索引摘要获取正文或跨到更晚代次。
# 函数用途: 从canonical归档账找到当前Bot线程之前的同槽位线程。
def _recent_bot_threads(store, thread_id):
    thread = store.threads.require(thread_id)
    fields = bot_thread_fields(thread)
    if not fields or fields.get("bot_generation", 1) <= 1:
        return [thread_id]
    try:
        fd = open_directory_beneath(thread.owner_home, (*_PREFIX, "history"))
    except FileNotFoundError:
        return [thread_id]
    rows = []
    try:
        with os.scandir(fd) as entries:
            for index, item in enumerate(entries):
                if index >= 4096:
                    raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
                if not item.name.endswith(".json"):
                    continue
                validate_opaque_id(item.name[:-5], kind="thread")
                raw = read_bytes_beneath(thread.owner_home, (*_PREFIX, "history", item.name), max_bytes=_MAX_STATE)
                row = json.loads(raw)
                if row["bot_mode"] != fields["bot_mode"] or row["generation"] >= fields["bot_generation"]:
                    continue
                previous = store.threads.require(row["thread_id"])
                if (previous.owner_id != thread.owner_id or previous.owner_home != thread.owner_home
                        or previous.metadata.get("bot_slot_id") != fields["bot_slot_id"]
                        or previous.metadata.get("bot_mode") != fields["bot_mode"]):
                    raise HostAPIError("RESOURCE_NOT_FOUND", 404)
                rows.append(row)
    finally:
        os.close(fd)
    return [thread_id, *(row["thread_id"] for row in sorted(rows, key=lambda row: row["generation"], reverse=True))]


# LLM: 缺消息时从canonical同槽位旧代补齐；before只在已核线程集合中定位，正文仍限可见角色及字节预算。
# 函数用途: 返回最近消息列表，跨代分页元数据由bot_message_page统一生成。
def recent_bot_messages(store, thread_id, *, max_messages=15, byte_budget=16384, thread_ids=None, before_cursor=None):
    return bot_message_page(store, thread_ids if thread_ids is not None else _recent_bot_threads(store, thread_id), limit=min(15, max_messages),
                            byte_budget=byte_budget, before=before_cursor)["messages"]


# LLM: 文件路径仅由可信home和固定组件构造；回执独立小文件避免槽位JSON随调用历史无限增长。
# 类用途: 提供Bot槽位事务需要的有界JSON读取和配额内原子发布。
class BotSessionStorage:
    # LLM: access由宿主注入鲜活账号检查；构造本身不创建目录，不接受页面中的owner路径。
    # 函数用途: 固定本owner、线程库和实时资格检查。
    def __init__(self, owner_home, store, check_access, *, clock=time.time, installation_ref="host"):
        self.home, self.store, self.check_access = owner_home, store, check_access
        self.clock, self.installation_ref = clock, installation_ref

    # LLM: 缺失和坏JSON严格区分，不能把损坏回执当首次提交。
    # 函数用途: 读取同一owner家目录内的Bot状态文件。
    def read(self, parts):
        raw = read_bytes_beneath(self.home.home_dir, parts, max_bytes=_MAX_STATE)
        if raw is None:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        return value

    # LLM: 调用方须持owner配额及槽位锁；不再获取反向配额锁或等待模型。
    # 函数用途: 有界写入一份Bot事务状态。
    def write(self, parts, value, admission):
        raw = canonical_json(value).encode()
        if len(raw) > _MAX_STATE:
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
        admission.check((OwnerQuotaChange(self.home.home_dir.joinpath(*parts), len(raw)),))
        write_bytes_atomic_beneath(self.home.home_dir, parts, raw)

    # LLM: journal先于线程写入发表；恢复重用相同线程ID，不创建第二代或触碰任何运行责任。
    # 函数用途: 应用并结清一次可恢复的前台槽位发布。
    def recover(self, state, admission):
        pending = state.get("pending")
        if pending is None:
            return
        self.check_access()
        if pending.get("old_thread_id"):
            self.archive(pending, admission)
        thread = ConversationThread.from_dict(pending["thread"])
        existing = self.store.threads.load(thread.thread_id)
        if existing is None:
            raw = canonical_json(thread.to_dict()).encode()
            admission.check((OwnerQuotaChange(self.store.storage.thread_path(thread.thread_id), len(raw)),))
            create_thread_atomic(self.store.threads, thread)
        elif bot_thread_fields(existing) != bot_thread_fields(thread):
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        state["slots"][pending["mode"]] = pending["slot"]
        state.pop("pending")
        self.write(_SLOTS, state, admission)

    # LLM: 只标记前台封闭，原status保持active以允许旧任务落账；后台责任字段完全原样保留。
    # 函数用途: 在原线程锁中封存旧代前台，并保存确定性历史摘要。
    def archive(self, pending, admission):
        tid = pending["old_thread_id"]

        # LLM: 原线程锁内核配额并保留并发后台更新；不得替换task/claim/Goal或依赖THREAD_BUSY归档口。
        # 函数用途: 只修改旧线程的前台封闭元数据，增长仍受owner配额约束。
        def close(thread):
            if thread.owner_id != self.home.owner_id or thread.owner_home != str(self.home.home_dir):
                raise HostAPIError("RESOURCE_NOT_FOUND", 404)
            updated = replace(thread, metadata={**thread.metadata, "foreground_closed": True,
                              "conversation_archive_state": "archived"})
            admission.check((OwnerQuotaChange(self.store.storage.thread_path(tid),
                                             len(canonical_json(updated.to_dict()).encode())),))
            return updated

        self.store.threads.update_atomic(tid, close)
        self.write((*_PREFIX, "history", tid + ".json"), pending["archive"], admission)

    # LLM: 仅枚举本owner固定history目录，nofollow且最多4096条；超预算明确拒绝，不把部分扫描称为全量。
    # 函数用途: 取得本人的已归档Bot代次摘要，不读取任何聊天正文。
    def archived_generations(self):
        self.check_access()
        try:
            descriptor = open_directory_beneath(self.home.home_dir, (*_PREFIX, "history"))
        except FileNotFoundError:
            return []
        rows = []
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json"):
                        continue
                    validate_opaque_id(entry.name[:-5], kind="thread")
                    if len(rows) >= 4096:
                        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
                    row = self.read((*_PREFIX, "history", entry.name))
                    if row is None:
                        raise HostAPIError("STORE_UNAVAILABLE", 503)
                    thread = self.store.threads.require(row["thread_id"])
                    if thread.owner_id != self.home.owner_id or thread.owner_home != str(self.home.home_dir):
                        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
                    rows.append(row)
        finally:
            os.close(descriptor)
        return sorted(rows, key=lambda row: (row["archived_at"], row["thread_id"]), reverse=True)

    # LLM: 摘要与消息各自使用原Host MAC游标；只同owner/模式跨代，before不会更换摘要筛选或授予线程权限。
    # 函数用途: 保留归档摘要合同，同时返回至多15条跨代可见消息与独立向前游标。
    def history(self, payload, authorized, context, codec):
        from ..plugin_host.conversations import BoundCursor

        self.check_access()
        if context.owner_home.home_dir != self.home.home_dir or context.identity != self.home.identity:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        filters = {key: value for key, value in payload.items() if key not in {"cursor", "before_cursor"}}
        mode = payload.get("mode") or payload.get("bot_mode") or payload.get("bot_kind")
        mode = normalize_bot_mode(mode) if mode else None
        cursor = BoundCursor(codec, authorized, context, filters)
        saved = cursor.decode(payload.get("cursor"))
        rows = self.archived_generations()
        tid = payload.get("thread_id")
        state = self.read(_SLOTS) or {"slots": {}}
        active = [slot["active_thread_id"] for slot in state["slots"].values()]
        if tid and tid not in active and not any(row["thread_id"] == tid for row in rows):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        query = payload.get("query", "").casefold()
        rows = [row for row in rows if (not tid or row["thread_id"] == tid)
                and (not mode or row["bot_mode"] == mode)
                and (payload.get("since") is None or row["archived_at"] >= payload["since"])
                and (payload.get("until") is None or row["archived_at"] <= payload["until"])
                and (not payload.get("alert_id") or row.get("alert_id") == payload["alert_id"])
                and (not query or query in (row["title"] + row["summary"]).casefold())]
        anchor = {"sha256": _digest(rows), "record_count": len(rows)}
        if saved and (saved["anchor"] != anchor or saved["offset"] > len(rows)):
            raise HostAPIError("CURSOR_EXPIRED", 409)
        offset = saved["offset"] if saved else 0
        selected = rows[offset:offset + payload.get("limit", 15)]
        end = offset + len(selected)
        token = cursor.encode(anchor, end, saved["expires"] if saved else time.time() + 900) if end < len(rows) else None
        messages = self.message_history(payload, rows, mode or "query_readonly", authorized, context, codec)
        self.check_access()
        return {"items": selected, "archived_generations": selected, "next_cursor": token,
                "complete": token is None, "snapshot_id": _digest(anchor), **messages}

    # LLM: 历史集合来自slots及原归档摘要；每代冷核owner/home/模式，MAC绑定完整筛选，不能用另一模式游标读消息。
    # 函数用途: 读取当前及归档Bot代次的可见消息并签发向前分页游标。
    def message_history(self, payload, archives, mode, authorized, context, codec):
        from ..plugin_host.conversations import BoundCursor
        filters = {key: value for key, value in payload.items() if key not in {"cursor", "before_cursor"}}
        cursor = BoundCursor(codec, authorized, context, {**filters, "projection": "bot_messages"})
        saved = cursor.decode(payload.get("before_cursor"))
        state = self.read(_SLOTS) or {"slots": {}}
        slot = state["slots"].get(mode)
        tids = ([slot["active_thread_id"]] if slot else []) + [row["thread_id"] for row in
            sorted(archives, key=lambda row: row["generation"], reverse=True) if row["bot_mode"] == mode]
        tids = list(dict.fromkeys(tids))
        if payload.get("thread_id"):
            tids = [tid for tid in tids if tid == payload["thread_id"]]
        for tid in tids:
            thread = self.store.threads.require(tid)
            if (thread.owner_id != self.home.owner_id or thread.owner_home != str(self.home.home_dir)
                    or thread.metadata.get("bot_mode") != mode):
                raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        page = bot_message_page(self.store, tids, limit=payload.get("limit", 15), before=saved["anchor"] if saved else None)
        token = cursor.encode(page["before"], 0, saved["expires"] if saved else time.time() + 900) if page["has_more"] else None
        return {"messages": page["messages"], "next_before_cursor": token, "has_more": page["has_more"]}


# LLM: 公共入口先重核owner；未结事务只准原意图恢复，保存before_slots以确定性重建同一代，绝不重复生成线程。
# 类用途: 在一个owner中为两种Bot管理可恢复的活跃线程及非打断分卷。
class BotSessionManager(BotSessionStorage):
    # LLM: 回执按安装/动作/键隔离；可选重放标记只影响返回投影，不覆盖原回执或改变已绑定代次。
    # 函数用途: 串行执行槽位操作，并可显式标注是否使用了原幂等结果。
    def transact(self, action, payload, key, effect, *, report_replay=False):
        if not isinstance(key, str) or not 16 <= len(key) <= 128:
            raise HostAPIError("INVALID_SCHEMA")
        self.check_access()
        receipt_path = (*_PREFIX, "receipts", _digest([self.installation_ref, action, key]) + ".json")
        enforcer = owner_quota_enforcer_from_policy(self.home.home_dir)
        with enforcer.admission() as admission, locked_file_transition(self.home.home_dir.joinpath(*_SLOTS)):
            self.check_access()
            state = self.read(_SLOTS) or {"schema": "bot_sessions.v1", "owner_id": self.home.owner_id, "slots": {}}
            if state.get("owner_id") != self.home.owner_id or state.get("schema") != "bot_sessions.v1":
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            self.recover(state, admission)
            prior = self.read(receipt_path)
            if prior:
                if prior["fingerprint"] != _digest(payload):
                    raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409)
                self.check_access()
                return {**prior["result"], "idempotent_replay": True} if report_replay else prior["result"]
            operation = state.get("operation")
            if operation and operation["receipt_path"] != list(receipt_path):
                completed = self.read(tuple(operation["receipt_path"]))
                if completed is None:
                    raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
                state.pop("operation")
                operation = None
            if operation and operation["fingerprint"] != _digest(payload):
                raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409)
            if operation is None:
                operation = {"receipt_path": list(receipt_path), "fingerprint": _digest(payload),
                             "before_slots": json.loads(canonical_json(state["slots"])), "at": self.clock()}
                state["operation"] = operation
                self.write(_SLOTS, state, admission)
            else:
                state["slots"] = json.loads(canonical_json(operation["before_slots"]))
            try:
                result = effect(state, admission)
            except HostAPIError as error:
                if error.outcome == "not_started":
                    state.pop("operation", None)
                    self.write(_SLOTS, state, admission)
                raise
            self.check_access()
            self.write(_SLOTS, state, admission)
            self.write(receipt_path, {"fingerprint": _digest(payload), "result": result}, admission)
            state.pop("operation", None)
            self.write(_SLOTS, state, admission)
            return {**result, "idempotent_replay": False} if report_replay else result

    # LLM: 标记与禁学来源由宿主生成，不合并page_context；只读来源保留在原learning_source供现有读者消费。
    # 函数用途: 为新代构造受保护的线程与槽位。
    def new_generation(self, mode, generation, revision):
        slot_id = "bot-" + _digest([self.home.owner_id, mode])[:32]
        tid = "thread-" + _digest([slot_id, generation])[:32]
        stamp = self.clock()
        fields = {"bot_mode": mode, "bot_generation": generation, "bot_slot_id": slot_id,
                  "learning_excluded": mode == "query_readonly", "foreground_closed": False,
                  "conversation_archive_state": "active"}
        source = unknown_projection() if fields["learning_excluded"] else user_dialogue_projection()
        source.update(bot_mode=mode, learning_excluded=fields["learning_excluded"])
        thread = ConversationThread(thread_id=tid, canonical_user_id=self.home.owner_id,
            owner_id=self.home.owner_id, owner_home=str(self.home.home_dir), cwd=str(self.home.home_dir),
            title=f"[Bot:{mode} #{generation}]", created_at=stamp, updated_at=stamp, learning_source=source,
            metadata={**fields, "plugin_host_v2": {"revision": 1, "installation_ref": self.installation_ref,
                                                  "workspace_ref": "default"}})
        slot = {"bot_mode": mode, "bot_kind": mode, "active_thread_id": tid, "generation": generation,
                "slot_revision": revision, "slot_id": slot_id, "created_at": stamp,
                "last_foreground_activity_at": stamp, "completed_foreground_turns": 0,
                "visible_text_bytes": 0, "metrics_offset": 0, "completed_turn_ids": [], "first_question": "",
                "foreground_turn_in_flight": False}
        return thread, slot

    # LLM: CAS在journal发表前验证；旧任务不迁移，新线程确定性生成；摘要只用首问和告警，不持锁调用模型。
    # 函数用途: 准备并恢复执行一次新建或轮换事务。
    def rotate(self, state, mode, admission, reason):
        old = state["slots"].get(mode)
        thread, slot = self.new_generation(mode, old["generation"] + 1 if old else 1,
                                            old["slot_revision"] + 1 if old else 1)
        archive = {"thread_id": old["active_thread_id"], "bot_mode": mode, "generation": old["generation"],
                   "created_at": old["created_at"], "archived_at": self.clock(),
                   "title": old.get("first_question", "")[:120] or f"[Bot:{mode} #{old['generation']}]",
                   "summary": old.get("alert_summary", "")[:512], "alert_id": old.get("alert_id"),
                   "rollover_reason": reason} if old else {}
        state["pending"] = {"mode": mode, "thread": thread.to_dict(), "slot": slot,
                            "old_thread_id": old["active_thread_id"] if old else None, "archive": archive}
        self.write(_SLOTS, state, admission)
        self.recover(state, admission)
        return old

    # LLM: 原前台执行/排队事实优先于阈值；有界消息扫描未追平时不归档，后台updated_at和Compact仍不延长idle。
    # 函数用途: 只在没有运行中前台回合时判定自动轮换。
    def rollover_reason(self, slot):
        self.refresh_metrics(slot)
        if not slot.get("metrics_caught_up", True) or self._foreground_turn_active(slot):
            return None
        if self.clock() - slot["last_foreground_activity_at"] > 1800:
            return "idle_timeout"
        if slot["completed_foreground_turns"] >= 30:
            return "max_turns"
        if slot["visible_text_bytes"] >= 32768:
            return "max_text_bytes"
        return None

    # LLM: 自动检查/归档共持原ClaimStore领取/续约/终态JSON锁，不创建第二把运行锁；只包文件事务，绝不跑模型。
    # 函数用途: 阻止前台claim在“检查结束、原代归档之前”的间隙取得运行权。
    def _foreground_transition(self, slot):
        return (locked_file_transition(self.store.storage.background_claim_path(slot["active_thread_id"]))
                if slot else nullcontext())

    # LLM: 原车道登记和同线程Gateway claim是运行事实，不释放/续约；坏claim不当空。无ID历史线索仅限既有idle窗口。
    # 函数用途: 核验活跃前台或尚未完成的消息，精确终态可清除同请求的在途投影。
    def _foreground_turn_active(self, slot):
        from .run_claim import user_input_turn_on_lane

        tid = slot["active_thread_id"]
        if user_input_turn_on_lane(self.store, tid):
            return True
        claim, error = self.store.claims.load_report(tid)
        if error or claim and claim.get("thread_id") != tid:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        if claim.get("reason") == "gateway_foreground_turn":
            if claim.get("status") == "running" and (claim.get("recover_same_task_only") is True
                    or (_foreground_timestamp(claim.get("expires_at")) or 0) > self.clock()):
                return True
            request_id = slot.get("foreground_request_id")
            if (request_id and claim.get("task_id") == "gateway:" + request_id
                    and claim.get("status") in {"finished", "failed", "cancelled"}):
                slot["foreground_turn_in_flight"] = False
        if not slot.get("foreground_turn_in_flight"):
            return False
        started = _foreground_timestamp(slot.get("foreground_turn_started_at"))
        active = started is not None and 0 <= self.clock() - started <= 1800
        if not active:
            slot["foreground_turn_in_flight"] = False
        return active

    # LLM: 用户与final投影前台状态，final时间单调推进；既有轮数去重和正文度量不变，后台先由调用者剔除。
    # 函数用途: 从一个可见前台消息更新在途标记、完成时间和轮数。
    def _foreground_message_metrics(self, slot, entry, visible):
        if visible[0] == "user":
            slot["foreground_turn_in_flight"] = True
            slot["foreground_turn_started_at"] = _foreground_timestamp(entry.created_at)
            slot["foreground_request_id"] = str(entry.metadata.get("conversation_request_id")
                or entry.metadata.get("request_id") or entry.metadata.get("turn_id") or "")
        if visible[:2] != ("assistant", "final"):
            return
        slot["foreground_turn_in_flight"] = False
        stamp = _foreground_timestamp(entry.created_at)
        if stamp is not None:
            slot["last_foreground_activity_at"] = max(float(slot.get("last_foreground_activity_at") or 0), stamp)
        turn = entry.metadata.get("turn_id") or entry.metadata.get("conversation_request_id") or entry.message_id
        if turn not in slot["completed_turn_ids"]:
            slot["completed_turn_ids"].append(turn)
            slot["completed_foreground_turns"] += 1

    # LLM: 增量游标及256行预算保持有界；过阈值后仍读完本批，以免错过后面的user/final；不把后台当前台。
    # 函数用途: 从原日志追加区更新计数及在途状态，报告本次是否追平。
    def refresh_metrics(self, slot):
        from ..plugin_host.history import HISTORY_RECORD_MAX_BYTES, _entry, _open, _visible_content

        slot.setdefault("foreground_turn_in_flight", False)
        slot["metrics_caught_up"] = True
        try:
            handle = _open(self.store, slot["active_thread_id"])
        except FileNotFoundError:
            return
        with handle:
            handle.seek(slot["metrics_offset"])
            for _ in range(256):
                raw = handle.readline(HISTORY_RECORD_MAX_BYTES + 1)
                if not raw:
                    break
                if len(raw) > HISTORY_RECORD_MAX_BYTES:
                    raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
                entry = _entry(raw, slot["active_thread_id"])
                slot["metrics_offset"] = handle.tell()
                visible = _visible_content(entry)
                if not visible or visible[0] not in {"user", "assistant"}:
                    continue
                if any(entry.metadata.get(key) for key in ("background_delivery_reason", "background_transcript_request_id",
                        "scheduler_run_id", "session_task_id", "background_wake_signal_id")):
                    continue
                slot["visible_text_bytes"] += len(visible[2].encode())
                if visible[0] == "user" and not slot["first_question"]:
                    slot["first_question"] = visible[2][:120]
                self._foreground_message_metrics(slot, entry, visible)
            slot["metrics_caught_up"] = handle.tell() >= os.fstat(handle.fileno()).st_size

    # LLM: 回执只给槽位和公开消息；不公开home、原工具正文或owner私有元数据。
    # 函数用途: 生成页面当前代描述和非打断轮换引用。
    def descriptor(self, slot, old=None, reason=None):
        keys = ("bot_mode", "bot_kind", "active_thread_id", "generation", "slot_revision")
        return {**{key: slot[key] for key in keys}, "rolled_over": old is not None,
                "previous_thread_id": old["active_thread_id"] if old else None, "rollover_reason": reason,
                "recent_messages": recent_bot_messages(self.store, slot["active_thread_id"])}

    # LLM: 每次新意图才刷新前台活动；原key重放只返回原结果，不创造下一代。
    # 函数用途: 确保指定模式有当前线程并按阈值无感分卷。
    def ensure(self, payload, key):
        mode = normalize_bot_mode(payload.get("bot_mode"), payload.get("bot_kind"))

        # LLM: 原claim短门覆盖阈值检查和切代，不能在两者之间领取前台运行权；不读thread.updated_at。
        # 函数用途: 检查当前代、必要时分卷，并更新前台访问时间。
        def effect(state, admission):
            slot = state["slots"].get(mode)
            with self._foreground_transition(slot):
                reason = self.rollover_reason(slot) if slot else None
                old = self.rotate(state, mode, admission, reason) if not slot or reason else None
            slot = state["slots"][mode]
            slot["last_foreground_activity_at"] = self.clock()
            return self.descriptor(slot, old, reason)

        return self.transact("ensure", payload, key, effect)

    # LLM: 手动轮换必须精确CAS；重放先读回执，因此不会在同一个意图上递增两次。
    # 函数用途: 强制生成下一代并返回旧线程引用。
    def rollover(self, payload, key):
        mode = normalize_bot_mode(payload.get("bot_mode"), payload.get("bot_kind"))

        # LLM: 预期代次和槽位修订均须匹配，不能自动修正调用者陈旧预期。
        # 函数用途: 核验手动轮换版本并封存当前前台。
        def effect(state, admission):
            slot = state["slots"].get(mode)
            if not slot or any(payload.get(key) != slot[field] for key, field in
                    (("expected_generation", "generation"), ("expected_revision", "slot_revision"))):
                raise HostAPIError("REVISION_CONFLICT", 409)
            old = self.rotate(state, mode, admission, "manual")
            return self.descriptor(state["slots"][mode], old, "manual")

        return self.transact("rollover", payload, key, effect)

    # LLM: submitter仅可做有界准备/原输入提交，不在锁内跑模型；旧代pin仍核验，重放绑定不得迁移到新代。
    # 函数用途: 将消息意图原子绑定到当前Bot代次，保留旧领域调用合同。
    def submit(self, payload, key, submitter, *, report_replay=False):
        mode = normalize_bot_mode(payload.get("bot_mode"), payload.get("bot_kind"))

        # LLM: 原claim短门覆盖阈值/CAS及切代，入队在门外；消息数据不能覆盖thread宿主管理字段。
        # 函数用途: 核当前代、记录首问和告警引用，并提交绑定线程的输入。
        def effect(state, admission):
            slot = state["slots"].get(mode)
            if slot is None:
                self.rotate(state, mode, admission, None)
                slot = state["slots"][mode]
            if payload.get("thread_id") and payload["thread_id"] != slot["active_thread_id"]:
                supplied = self.store.threads.load(payload["thread_id"])
                if supplied is None or supplied.metadata.get("bot_mode") != mode:
                    raise HostAPIError("BOT_MODE_MISMATCH", 409)
                if not payload.get("auto_rollover"):
                    raise HostAPIError("BOT_SESSION_ROLLED_OVER", 409)
            expected = payload.get("expected_generation")
            with self._foreground_transition(slot):
                reason = self.rollover_reason(slot)
                if expected is not None and expected > slot["generation"]:
                    raise HostAPIError("BOT_SESSION_ROLLED_OVER", 409)
                if expected is not None and expected != slot["generation"] and not payload.get("auto_rollover"):
                    raise HostAPIError("BOT_SESSION_ROLLED_OVER", 409)
                if reason and not payload.get("auto_rollover"):
                    raise HostAPIError("BOT_SESSION_ROLLED_OVER", 409)
                old = self.rotate(state, mode, admission, reason) if reason else None
            slot = state["slots"][mode]
            slot["last_foreground_activity_at"] = self.clock()
            slot["first_question"] = slot["first_question"] or payload["text"][:120]
            page = payload.get("page_context", {})
            slot["alert_id"] = page.get("alert_id")
            slot["alert_summary"] = str(page.get("alert_summary", ""))[:512]
            result = submitter(slot, payload, key)
            return {**self.descriptor(slot, old, reason), **result}

        return self.transact("submit", payload, key, effect, report_replay=report_replay)

    # LLM: 先持久绑定代次，后由Gateway原input receipt入队；重放在轮换前返回旧绑定，不能同key入两条线程。
    # 函数用途: 为生产Bot提交完成阈值检查、必要分卷和幂等代次预留。
    def prepare_submit(self, payload, key):
        # LLM: 准备回调不运行工具或队列，仅冻结用于原输入接缝的宿主线程ID。
        # 函数用途: 将当前活跃线程投影成可复用的提交绑定。
        def prepare(slot, _payload, _key):
            return {"thread_id": slot["active_thread_id"]}

        return self.submit(payload, key, prepare, report_replay=True)
