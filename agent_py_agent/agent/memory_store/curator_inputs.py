# LLM: 本模块只读权威经历并生成预览；临时关系注释绑定来源和正式版本，锚点复用权威范围键，不写游标、不推断候选、不复制工具大输出。
# 模块用途: 把精确消息与审计游标后的新事件及同范围只读锚点整理为后台模型的有界 JSON 输入。
from __future__ import annotations

"""从 ConversationStore 与 owner audit 增量构造有界 Curator 输入。"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.cache_freshness import cache_entry_trustworthy
from ..io.cursor_cache import CursorCache, file_stamp
from .curator_audit_cursor import AuditRead, collect_audit_window
from .curator_formal import CuratorFormalMemoryInput
from .curator_models import MemoryCuratorConfig, MemoryCuratorState
from .learning_exclusions import thread_is_excluded
from .scope_contract import canonical_scope_key

# 消息预览最多 6000 字符：多轮消息给足上下文又不过载。
_MESSAGE_PREVIEW_CHARS = 6_000
# 审计事件预览最多 1000 字符：事件详情截断，保留判定要点。
_AUDIT_PREVIEW_CHARS = 1_000
_SUCCESS_TOOL_STATUSES = frozenset(
    {"success", "succeeded", "ok", "completed", "committed", "applied"}
)
_INCOMPLETE_TOOL_EFFECTS = frozenset({"unknown", "not_started", "failed"})
# 可覆盖生产万级冷线程；仅保存文件指纹与游标，淘汰只会恢复原只读扫描。
_COLD_THREAD_EOF = CursorCache(16_384)

ToolReferenceQuery = Callable[[Path, tuple[str, ...], int], dict[str, object]]


# LLM: full_content 只在宿主内用于 quote/hash 核验，to_model 永远只发有界预览。
# 类用途: 保存一条待策展 ConversationStore 消息和可验证引用。
@dataclass(frozen=True)
class CuratorMessageInput:
    message_id: str
    thread_id: str
    role: str
    channel: str
    created_at: float
    full_content: str
    content_preview: str
    content_hash: str
    metadata: dict[str, object]
    # LLM: 消息所属线程的宿主来源投影（收集时读取，供候选来源判定用）；缺字段的旧线程为空（未知）。
    thread_source: dict[str, object] = field(default_factory=dict)

    # LLM: 运行 ID 只取 ConversationStore 结构化元数据；Gateway 的 gateway_request_id 归一为 request_id，不从正文推断。
    # 函数用途: 生成可由宿主核验的消息证据引用并规范化 Gateway 请求编号。
    def ref(self) -> dict[str, object]:
        result: dict[str, object] = {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "role": self.role,
            "channel": self.channel,
            "created_at": _iso_from_epoch(self.created_at),
            "content_hash": self.content_hash,
        }
        aliases = {
            "session_id": ("session_id",),
            "request_id": ("request_id", "gateway_request_id"),
            "task_id": ("task_id",),
            "run_id": ("run_id",),
        }
        for key, source_keys in aliases.items():
            value = next(
                (
                    str(self.metadata.get(source_key) or "").strip()
                    for source_key in source_keys
                    if str(self.metadata.get(source_key) or "").strip()
                ),
                "",
            )
            if value:
                result[key] = value
        return result

    # LLM: 模型投影始终使用有界 preview，并显式标明原文长度和是否截断。
    # 函数用途: 把一条消息转成 Curator 严格输入对象。
    def to_model(self) -> dict[str, object]:
        return {
            **self.ref(),
            "content_preview": self.content_preview,
            "content_chars": len(self.full_content),
            "content_truncated": len(self.content_preview) < len(self.full_content),
            "metadata": _bounded_metadata(self.metadata),
        }


# LLM: audit payload 只保留工具/任务状态、hash、预览和引用，不携带 raw/output/content 正文。
# 类用途: 保存一条待策展 owner audit 事件。
@dataclass(frozen=True)
class CuratorAuditInput:
    event_id: str
    event_type: str
    created_at: str
    status: str
    operation_id: str
    tool_call_id: str
    tool_name: str
    tool_success: bool | None
    error_code: str
    effect_outcome: str
    session_id: str
    thread_id: str
    request_id: str
    task_id: str
    run_id: str
    content_hash: str
    source_ref: str
    artifact_ref: str
    artifact_hash: str
    artifact_size_bytes: int
    preview: str
    # 宿主在收集时冻结，不进入ref/to_model，不允许提取模型提交该字段。
    learning_source: dict[str, object] = field(default_factory=dict)

    # LLM: 事件类型与两个工具身份字段均取自 owner 审计行；status=ok 的助手轮次不能仅凭 ID 查找就成为工具引用。
    # 函数用途: 判断这是否是一条具有完整宿主身份的真实工具调用事件。
    def is_tool_call(self) -> bool:
        tool_name = self.tool_name.strip().lower()
        return (
            self.event_type.strip().lower() == "tool_call"
            and tool_name not in {"", "unknown"}
            and bool(self.tool_call_id.strip())
        )

    # LLM: tool_success 是宿主归档投影的规范 ToolResult.ok；副作用为 unknown/not_started 时保守拒绝，正式晋升写入长期记忆前仍须复核操作权威账。
    # 函数用途: 判断工具事件能否作为 Curator 的成功工具证据。
    def is_verified_success(self) -> bool:
        return (
            self.is_tool_call()
            and self.tool_success is True
            and self.status.strip().lower() in _SUCCESS_TOOL_STATUSES
            and self.effect_outcome.strip().lower() not in _INCOMPLETE_TOOL_EFFECTS
        )

    # LLM: 一条 audit 事件的最小证据引用；空字段不发送——56 行里 11 个恒空字段的键名与空值
    #   要占掉约 240 字符/行，属于同一批材料里的零信息重复。缺失即表示该字段为空，与显式空串语义一致。
    #   正文仍未进入引用；preview 由 to_model 单独投影。
    # 函数用途: 生成一条运行事件的最小证据引用。
    def ref(self) -> dict[str, object]:
        values: dict[str, object] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "status": self.status,
            "operation_id": self.operation_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_success": self.tool_success,
            "error_code": self.error_code,
            "effect_outcome": self.effect_outcome,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "content_hash": self.content_hash,
            "source_ref": self.source_ref,
            "artifact_ref": self.artifact_ref,
            "artifact_hash": self.artifact_hash,
            "artifact_size_bytes": self.artifact_size_bytes,
        }
        return {key: value for key, value in values.items() if value not in ("", None, 0)}

    # LLM: 模型只能看到有界 preview；完整工具大输出继续由 artifact 权威保存。
    # 函数用途: 把一条 audit 事件转成 Curator 输入对象。
    def to_model(self) -> dict[str, object]:
        return {**self.ref(), "preview": self.preview}


# LLM: 标注只属于当前批次的模型输入，既非证据也非持久状态；来源消失或正文摘要变化时不得继续发送。
# 类用途: 保存原生决策给提取模型的临时标签和优先级，不具备修改记忆或游标的权限。
@dataclass(frozen=True)
class CuratorDecisionAnnotation:
    source_kind: str
    source_id: str
    content_hash: str
    tag: str = ""
    priority: str = ""
    model: str = ""
    input_digest: str = ""
    tag_outcome: str = ""
    priority_outcome: str = ""
    required_refs: tuple[tuple[str, str], ...] = ()


# LLM: 关系只覆盖精确来源与正式条目的一对，不是候选、证据、动作或全库覆盖证明；版本归原仓库。
# 类用途: 保存提取前可选关系建议，既不持久化也不能触发记忆修改。
@dataclass(frozen=True)
class CuratorRelationAnnotation:
    source_kind: str
    source_id: str
    content_hash: str
    authority_type: str
    authority_id: str
    authority_ref: str
    authority_version: int
    formal_content_hash: str
    relation: str
    model: str
    input_digest: str
    coverage: str = "presented_pair_only"


# LLM: batch 保存的是这次 lease 看到的精确输入顺序；关系注释只附当前双边引用，不成为候选或正式记忆的第二状态。
# 类用途: 为 prompt、证据核验和 cursor 计算提供同一输入快照。
@dataclass(frozen=True)
class CuratorInputBatch:
    messages: tuple[CuratorMessageInput, ...]
    audit_events: tuple[CuratorAuditInput, ...]
    formal_memories: tuple[CuratorFormalMemoryInput, ...] = ()
    load_errors: tuple[dict[str, object], ...] = ()
    # 可降级读取错误(如 formal 记忆整体失败):与 load_errors 严格语义不同,只记不阻断。
    formal_memory_errors: tuple[dict[str, object], ...] = ()
    decision_annotations: tuple[CuratorDecisionAnnotation, ...] = ()
    relation_annotations: tuple[CuratorRelationAnnotation, ...] = ()
    # 排除名单过滤后整个 audit 窗口被跳过的游标（原始窗口最后一条 event_id）；空串表示无跳过。
    skipped_audit_cursor: str = ""
    # 消息级 learning_excluded 连续前缀被宿主核验跳过的线程游标；坏线程不写入此表。
    skipped_message_cursors: dict[str, str] = field(default_factory=dict)
    candidate_anchors: tuple[dict[str, object], ...] = ()

    # LLM: 保持原材料顺序；标签校验来源 hash，关系同时校验正式 ref/hash/版本；缩批不能遗留尾部建议。
    # 函数用途: 生成一次后台模型调用的完整结构化输入。
    def to_model_payload(self) -> dict[str, object]:
        payload = {
            "messages": [item.to_model() for item in self.messages],
            "audit_events": [item.to_model() for item in self.audit_events],
            "formal_memories": [item.to_model() for item in self.formal_memories],
        }
        if self.candidate_anchors:
            payload["candidate_anchors"] = list(self.candidate_anchors)
        refs = {("message", item.message_id, item.content_hash) for item in self.messages}
        refs.update(("audit", item.event_id, item.content_hash) for item in self.audit_events)
        annotations = [{**vars(item), "required_refs": [{"kind": kind, "ref": ref} for kind, ref in item.required_refs]}
                       for item in self.decision_annotations if (item.source_kind, item.source_id, item.content_hash) in refs]
        if annotations:
            payload["decision_annotations"] = annotations
        formal_refs = {(item.authority_type, item.authority_id, item.authority_ref, item.authority_version, item.content_hash)
                       for item in self.formal_memories}
        relations = [dict(vars(item)) for item in self.relation_annotations
                     if (item.source_kind, item.source_id, item.content_hash) in refs
                     and (item.authority_type, item.authority_id, item.authority_ref, item.authority_version, item.formal_content_hash) in formal_refs]
        if relations:
            payload["relation_annotations"] = relations
        return payload

    # LLM: 轮次阈值只统计结构化 role=user 的新消息，不解析正文或 assistant 摘要。
    # 函数用途: 计算本批游标后的真实用户轮次数。
    def user_turns(self) -> int:
        return sum(1 for item in self.messages if item.role == "user")


# LLM: 复用规范scope合同；非法宿主ID只跳过该范围，不扩大权限或阻断其它合法范围，每类每范围三条且总计十二条。
# 函数用途: 按本批宿主结构化范围提供可精确链接的lesson/HOT锚点，避免同一经验因范围键漂移而反复新建。
def attach_candidate_anchors(batch, candidates):
    scopes = {("personal", "personal"), ("global", "global")}
    for message in batch.messages:
        if message.thread_id:
            _add_anchor_scope(scopes, "session", message.thread_id)
        task_id = str(message.metadata.get("task_id") or "")
        if task_id:
            _add_anchor_scope(scopes, "project", task_id)
    for event in batch.audit_events:
        if event.task_id:
            _add_anchor_scope(scopes, "project", event.task_id)
    anchors, counts = [], {}
    rows = candidates.readonly_rows(statuses={"pending_review", "approved", "observed", "blocked_missing_evidence"})
    for row in sorted(rows, key=lambda item: (item.last_observed_at, item.candidate_id), reverse=True):
        scope = (row.scope["scope_type"], row.scope["scope_key"])
        key = (*scope, row.candidate_type)
        if (scope not in scopes or row.candidate_type not in {"lesson", "hot_rule"}
                or row.proposed_action != "add" or counts.get(key, 0) >= 3):
            continue
        counts[key] = counts.get(key, 0) + 1
        anchors.append({"candidate_id": row.candidate_id, "candidate_type": row.candidate_type,
            "content": row.content[:300], "subject_key": row.subject_key, "scope": dict(row.scope),
            "promotion_target": row.promotion_target, "target_entry_id": row.target_entry_id,
            "occurrence_count": row.occurrence_count, "last_observed_at": row.last_observed_at})
        if len(anchors) >= 12:
            break
    return replace(batch, candidate_anchors=tuple(anchors))


# LLM: 只捕获规范scope校验的值错误；单条脏ID不能变为个人/全局范围，也不吞其它仓库或执行异常。
# 函数用途: 安全收集一个合法范围，非法或空前缀仅跳过当前条目。
def _add_anchor_scope(scopes, scope_type, value):
    try:
        key = canonical_scope_key(scope_type, value)
    except ValueError:
        return
    scopes.add((scope_type, key))


# LLM: 此适配器只从既有运行记忆控制面读取有界元数据；不读产物正文，也不把输出索引状态视为执行权威。
# 类用途: 从当前 owner 的规范 tool-output 索引补全 Curator 的 artifact 引用、哈希和大小。
@dataclass(frozen=True)
class CuratorToolReferenceSource:
    owner_root: Path
    query: ToolReferenceQuery

    # LLM: 必须提供精确结构化运行与调用身份，返回路径必须位于当前 owner 根内，禁止索引引入其他 owner 的引用。
    #   批量契约：一次调用对全部 run 只查一遍（读取器每批只 glob、只顺序扫描一次索引）；
    #   整批失败时给每个 run 复制同类型错误——旧路径里每个 run 的独立查询都会在同一个
    #   确定性原因上失败，逐条等价。
    #   错误合同（mc4-1302，3a 已接受的有意变化）：本读取只依赖工具输出索引；daily/*/events.jsonl
    #   与 memory_archive/compact_applies/ledger.jsonl 损坏（非法 UTF-8）不再连带失败（旧路径会因
    #   整读它们而给每个 run 报 UnicodeDecodeError）。这是行为改进，不是等价修改。
    # 函数用途: 为一批 audit 工具事件读取有界、owner 隔离的大输出引用元数据。
    def read(
        self,
        audit_events: tuple[CuratorAuditInput, ...],
        *,
        limit_per_run: int,
    ) -> tuple[dict[tuple[str, str], dict[str, object]], list[dict[str, object]]]:
        refs: dict[tuple[str, str], dict[str, object]] = {}
        errors: list[dict[str, object]] = []
        run_ids = sorted(
            {
                event.run_id
                for event in audit_events
                if event.run_id and event.tool_call_id
            }
        )
        if not run_ids:
            return refs, errors
        try:
            limit = max(1, min(2_048, int(limit_per_run)))
            result = self.query(self.owner_root, tuple(run_ids), limit)
        except (OSError, UnicodeError, ValueError) as exc:
            return refs, _all_run_errors(run_ids, exc)
        if result.get("ok") is not True:
            return refs, _all_run_errors(run_ids, ValueError("control plane failed"))
        rows_by_run = result.get("tool_outputs_by_run")
        if not isinstance(rows_by_run, dict):
            return refs, _all_run_errors(run_ids, ValueError("invalid tool_outputs"))
        for run_id in run_ids:
            rows = rows_by_run.get(run_id)
            if not isinstance(rows, list):
                errors.append(_tool_reference_error(ValueError("invalid tool_outputs"), run_id=run_id))
                continue
            run_refs, run_errors = _run_refs_and_errors(run_id, rows, self.owner_root)
            refs.update(run_refs)
            errors.extend(run_errors)
        return refs, errors


# LLM: 消息来源包把 ConversationStore 与 owner 学习排除名单绑成一个只读输入；排除判定只影响
#   消息收集，审计与正式记忆投影不受影响。
# 类用途: 绑定一次消息收集的会话存储与 owner 学习排除名单。
@dataclass(frozen=True)
class CuratorMessageSource:
    conversation_store: object
    # 学习排除名单（channel_conversation_id 集合）：命中的会话在读取消息前跳过、游标不动；
    # None 表示名单不可读的 fail-closed——本轮不收集任何会话消息，修复后从原游标重放。
    excluded_conversation_ids: frozenset[str] | None = frozenset()


# LLM: collection 必须用 ConversationStore 的 typed cursor read，不直接解析其文件布局。
# 函数用途: 收集所有线程游标后的有界消息和 audit 事件。
def collect_curator_inputs(
    *,
    message_source: CuratorMessageSource,
    audit_dir: str | Path,
    state: MemoryCuratorState,
    config: MemoryCuratorConfig,
    formal_memories: tuple[CuratorFormalMemoryInput, ...] = (),
    tool_reference_source: CuratorToolReferenceSource | None = None,
) -> CuratorInputBatch:
    skipped_message_cursors: dict[str, str] = {}
    messages, message_errors = _collect_messages(
        source=message_source,
        cursors=state.per_thread_cursors,
        limit=config.batch_message_limit,
        max_chars=config.max_input_chars,
        skipped_cursors=skipped_message_cursors,
    )
    remaining_chars = max(
        1_000,
        config.max_input_chars
        - len(json.dumps([item.to_model() for item in messages], ensure_ascii=False)),
    )
    audit_thread_ids = _excluded_audit_thread_ids(message_source)
    if audit_thread_ids is None:
        # fail-closed：名单不可读时本轮不消费任何 audit，也不推进游标，修复后整段重放。
        audit_events, audit_errors, skipped_audit_cursor = [], [], ""
    else:
        audit_events, audit_errors = _collect_audit(
            Path(audit_dir),
            after_event_id=state.last_processed_audit_event_id,
            limit=config.batch_message_limit,
            max_chars=remaining_chars,
        )
        audit_events, skipped_audit_cursor = _filter_excluded_audit(audit_events, audit_thread_ids)
    tool_reference_errors: list[dict[str, object]] = []
    if tool_reference_source is not None and audit_events:
        refs, tool_reference_errors = tool_reference_source.read(
            tuple(audit_events),
            limit_per_run=max(64, config.batch_message_limit * 8),
        )
        audit_events = _enrich_audit_artifact_refs(audit_events, refs)
        audit_events = _fit_audit_budget(audit_events, max_chars=remaining_chars)
    return CuratorInputBatch(
        messages=tuple(messages),
        audit_events=tuple(audit_events),
        formal_memories=tuple(formal_memories),
        load_errors=(*message_errors, *audit_errors, *tool_reference_errors),
        skipped_audit_cursor=skipped_audit_cursor,
        skipped_message_cursors=skipped_message_cursors,
    )


# LLM: threads.list_report 只列元数据，messages.after_report 从精确 cursor 后读取；坏线程阻断游标推进。
# 两个读取能力来自同一 ConversationStore 的显式领域，不探测旧方法或建立第二份消息来源。
# 学习排除名单与线程只读标记在读取任何消息前生效；消息级严格True标记有界跨页跳过并记录跳过游标前缀。
# 函数用途: 收集预算内消息；排除来源先跳过，已确认EOF且文件未变的冷线程不再调用消息读取器。
def _collect_messages(
    *,
    source: CuratorMessageSource,
    cursors: dict[str, str],
    limit: int,
    max_chars: int,
    skipped_cursors: dict[str, str] | None = None,
) -> tuple[list[CuratorMessageInput], list[dict[str, object]]]:
    conversation_store = source.conversation_store
    list_report = getattr(getattr(conversation_store, 'threads', None), 'list_report', None)
    after_report = getattr(getattr(conversation_store, 'messages', None), 'after_report', None)
    if not callable(list_report) or not callable(after_report):
        raise TypeError("ConversationStore lacks incremental Memory Curator read contract")
    threads, thread_errors = list_report(limit=0)
    errors: list[dict[str, object]] = [dict(item) for item in thread_errors]
    if source.excluded_conversation_ids is None:
        # fail-closed：名单不可读时本轮不学任何会话；消息留在原游标后，名单修复后重放。
        return [], errors
    selected: list[CuratorMessageInput] = []
    state = {
        "store": conversation_store,
        "after_report": after_report,
        "limit": limit,
        "max_chars": max_chars,
        "used_chars": 0,
        "selected": selected,
        "errors": errors,
        "skipped_cursors": skipped_cursors,
    }
    for thread in sorted(threads, key=lambda item: (float(item.updated_at), str(item.thread_id))):
        if len(selected) >= limit or int(state["used_chars"]) >= max_chars:
            break
        if _thread_learning_excluded(thread) or thread_is_excluded(thread, source.excluded_conversation_ids):
            continue
        _collect_thread_messages(state, thread, str(cursors.get(str(thread.thread_id)) or ""))
    return selected, errors


# LLM: 单线程有界多页读取：跨过 learning_excluded=True 的消息并记录连续前缀跳过游标，遇读错即清空跳过记录。
# 函数用途: 从单个会话增量收集预算内待提取消息。
def _collect_thread_messages(state: dict[str, object], thread: object, cursor: str) -> None:
    thread_id = str(thread.thread_id)
    eof_key = _cold_thread_key(state["store"], thread_id, cursor)
    if eof_key is not None and _COLD_THREAD_EOF.get(eof_key):
        return
    scan_cursor = cursor
    seen_cursors = {scan_cursor}
    thread_selected = False
    selected: list[CuratorMessageInput] = state["selected"]  # type: ignore[assignment]
    errors: list[dict[str, object]] = state["errors"]  # type: ignore[assignment]
    skipped_cursors: dict[str, str] | None = state["skipped_cursors"]  # type: ignore[assignment]
    after_report = state["after_report"]
    limit = int(state["limit"])
    for _page in range(16):
        page_limit = max(1, limit - len(selected))
        entries, load_errors = after_report(thread_id, after_message_id=scan_cursor, limit=page_limit)  # type: ignore[operator]
        errors.extend(dict(item) for item in load_errors)
        if load_errors:
            if skipped_cursors is not None:
                skipped_cursors.pop(thread_id, None)
            break
        if not entries:
            if not thread_selected and scan_cursor == cursor and eof_key is not None and eof_key == _cold_thread_key(state["store"], thread_id, cursor):
                _COLD_THREAD_EOF.put(eof_key, True)
            break
        scan_cursor, thread_selected, stopped = _consume_thread_page(
            state, entries, thread, (scan_cursor, thread_selected)
        )
        if stopped or len(entries) < page_limit or not scan_cursor or scan_cursor in seen_cursors:
            break
        seen_cursors.add(scan_cursor)


# LLM: 处理单页返回的消息：严格跳过 learning_excluded=True 并按字符/条数预算收口。
# 函数用途: 遍历一页增量消息并追加到批列表。
def _consume_thread_page(
    state: dict[str, object],
    entries: list[object],
    thread: object,
    page_state: tuple[str, bool],
) -> tuple[str, bool, bool]:
    scan_cursor, thread_selected = page_state
    thread_id = str(thread.thread_id)
    thread_source = getattr(thread, "learning_source", None)
    source_dict = dict(thread_source) if isinstance(thread_source, dict) else {}
    selected: list[CuratorMessageInput] = state["selected"]  # type: ignore[assignment]
    skipped_cursors: dict[str, str] | None = state["skipped_cursors"]  # type: ignore[assignment]
    limit = int(state["limit"])
    max_chars = int(state["max_chars"])
    for entry in entries:
        entry_id = str(entry.message_id)
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        if metadata.get("learning_excluded") is True:
            scan_cursor = entry_id
            if skipped_cursors is not None and not thread_selected:
                skipped_cursors[thread_id] = entry_id
            continue
        used_chars = int(state["used_chars"])
        remaining = max_chars - used_chars
        if remaining < 300:
            return scan_cursor, thread_selected, True
        content = str(entry.content or "")
        preview = content[: min(_MESSAGE_PREVIEW_CHARS, max(200, remaining - 250))]
        item = CuratorMessageInput(
            message_id=entry_id,
            thread_id=str(entry.thread_id),
            role=str(entry.role),
            channel=str(entry.channel),
            created_at=float(entry.created_at),
            full_content=content,
            content_preview=preview,
            content_hash=_content_hash(content),
            metadata=dict(metadata),
            thread_source=dict(source_dict),
        )
        item_chars = len(json.dumps(item.to_model(), ensure_ascii=False))
        if selected and used_chars + item_chars > max_chars:
            return scan_cursor, thread_selected, True
        selected.append(item)
        state["used_chars"] = used_chars + item_chars
        thread_selected = True
        scan_cursor = entry_id
        if len(selected) >= limit:
            return scan_cursor, thread_selected, True
    return scan_cursor, thread_selected, False


# LLM: 只从原Store的canonical路径读五字段stat；fresh、缺失或替身无路径时不缓存，任何变更回到原报告。
# 函数用途: 为已确认追平的线程建立有界进程内EOF键，不保存消息正文或改变持久游标。
def _cold_thread_key(store, thread_id: str, cursor: str):
    storage = getattr(store, "storage", None)
    path_for = getattr(storage, "message_path", None)
    if not callable(path_for):
        return None
    try:
        path = path_for(thread_id)
        stamp = file_stamp(path.stat())
        if cache_entry_trustworthy(stamp.mtime_ns):
            return id(store), str(path.absolute()), cursor, stamp
    except (OSError, ValueError, TypeError):
        pass
    return None


# LLM: 仅宿主线程metadata的严格True或query_readonly能排除；缺省/旧记录不凭正文猜来源。
# 函数用途: 在消息和审计收集前识别不可学习线程。
def _thread_learning_excluded(thread: object) -> bool:
    metadata = getattr(thread, "metadata", None)
    return isinstance(metadata, dict) and (
        metadata.get("learning_excluded") is True or metadata.get("bot_mode") == "query_readonly"
    )


# LLM: audit与消息共用名单及线程metadata门；线程或名单不可读时不消费audit游标。
# 函数用途: 即使owner名单为空，也排除只读/不可学习线程的审计事件。
def _excluded_audit_thread_ids(source: CuratorMessageSource) -> frozenset[str] | None:
    if source.excluded_conversation_ids is None:
        return None
    threads, errors = source.conversation_store.threads.list_report(limit=0)
    if errors:
        return None
    return frozenset(str(thread.thread_id) for thread in threads if _thread_learning_excluded(thread)
                     or thread_is_excluded(thread, source.excluded_conversation_ids))


# LLM: 排除会话的 audit 事件不进入模型输入；窗口整段被排除时给出可安全跳过的游标，
#   否则同一窗口每轮都被重新扫描（游标不推进就永远停在原地）。
# 函数用途: 按排除 thread 集合过滤 audit 事件并返回可跳过的游标。
def _filter_excluded_audit(
    events: list[CuratorAuditInput],
    excluded_thread_ids: frozenset[str],
) -> tuple[list[CuratorAuditInput], str]:
    if not excluded_thread_ids or not events:
        return events, ""
    kept = [event for event in events if event.thread_id not in excluded_thread_ids]
    if kept:
        return kept, ""
    return kept, str(events[-1].event_id or "")


# LLM: audit经有界只读定位缓存按原日分片/行顺序扫描；失效同算法重定位，保留全部原错误，不推进持久cursor。
# 函数用途: 收集 last_processed_audit_event_id 后的有界 audit 预览。
def _collect_audit(
    audit_dir: Path,
    *,
    after_event_id: str,
    limit: int,
    max_chars: int,
) -> tuple[list[CuratorAuditInput], list[dict[str, object]]]:
    return collect_audit_window(AuditRead(audit_dir, after_event_id, limit, max_chars), _audit_input)


# LLM: 字段选择只认 audit 结构键，preview 也有固定上限且不读取 content_path 指向的大正文。
# 函数用途: 把 audit row 转成安全 Curator 输入。
def _audit_input(payload: dict[str, object]) -> CuratorAuditInput:
    preview = str(
        payload.get("preview")
        or payload.get("content_preview")
        or payload.get("summary")
        or payload.get("message_preview")
        or ""
    )[:_AUDIT_PREVIEW_CHARS]
    artifact_ref = str(
        payload.get("artifact_ref")
        or payload.get("content_path")
        or payload.get("output_ref")
        or ""
    )
    tool_success = payload.get("tool_success")
    if not isinstance(tool_success, bool):
        tool_success = None
    return CuratorAuditInput(
        event_id=str(payload.get("event_id") or ""),
        event_type=str(
            payload.get("event_type") or payload.get("event") or payload.get("action") or ""
        ),
        created_at=_audit_time(payload),
        status=str(payload.get("status") or payload.get("effect_outcome") or ""),
        operation_id=str(payload.get("operation_id") or ""),
        tool_call_id=str(payload.get("tool_call_id") or payload.get("call_id") or ""),
        tool_name=str(payload.get("tool_name") or payload.get("tool") or ""),
        tool_success=tool_success,
        error_code=str(payload.get("error_code") or ""),
        effect_outcome=str(payload.get("effect_outcome") or ""),
        session_id=str(payload.get("session_id") or ""),
        thread_id=str(payload.get("thread_id") or ""),
        request_id=str(payload.get("request_id") or payload.get("gateway_request_id") or ""),
        task_id=str(payload.get("task_id") or ""),
        run_id=str(payload.get("run_id") or ""),
        content_hash=str(payload.get("content_hash") or payload.get("sha256") or ""),
        source_ref=str(payload.get("source_ref") or ""),
        artifact_ref=artifact_ref,
        artifact_hash="",
        artifact_size_bytes=0,
        preview=preview,
    )


# LLM: 补全仅按精确 run_id/tool_call_id 匹配，不复制状态、操作或输出正文，不能把普通审计事件升级为成功工具证据。
# 函数用途: 把规范 tool-output artifact 元数据合并到对应 Curator audit 事件。
def _enrich_audit_artifact_refs(
    events: list[CuratorAuditInput],
    refs: dict[tuple[str, str], dict[str, object]],
) -> list[CuratorAuditInput]:
    enriched: list[CuratorAuditInput] = []
    for event in events:
        ref = refs.get((event.run_id, event.tool_call_id))
        if not ref:
            enriched.append(event)
            continue
        enriched.append(
            replace(
                event,
                artifact_ref=str(ref.get("artifact_ref") or ""),
                artifact_hash=str(ref.get("artifact_hash") or ""),
                artifact_size_bytes=int(ref.get("artifact_size_bytes") or 0),
            )
        )
    return enriched


# LLM: 产物补全后仍须遵守查找前同一模型输入预算；截取必须保留审计追加顺序的前缀以维持游标推进规则。
# 函数用途: 在补全 artifact 元数据后重新施加 audit 字符预算。
def _fit_audit_budget(
    events: list[CuratorAuditInput],
    *,
    max_chars: int,
) -> list[CuratorAuditInput]:
    selected: list[CuratorAuditInput] = []
    used_chars = 0
    for event in events:
        item_chars = len(json.dumps(event.to_model(), ensure_ascii=False))
        if selected and used_chars + item_chars > max_chars:
            break
        selected.append(event)
        used_chars += item_chars
    return selected


# LLM: 只允许精确的产物标量元数据离开控制面；参数、来源输入、摘要和内嵌输出字段均丢弃。
# 函数用途: 校验并压缩一条 tool-output 索引记录。
def _bounded_tool_output_ref(
    row: dict[str, object],
    *,
    owner_root: Path,
    run_id: str,
) -> dict[str, object]:
    if str(row.get("kind") or "") != "tool_output":
        return {}
    row_run_id = str(row.get("run_id") or "").strip()
    call_id = str(row.get("call_id") or "").strip()
    path_text = str(row.get("path") or "").strip()
    if row_run_id != run_id or not call_id or not path_text:
        return {}
    try:
        root = owner_root.expanduser().resolve(strict=False)
        path = Path(path_text).expanduser().resolve(strict=False)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("tool artifact escaped owner root") from exc
    digest = _normalized_sha256(row.get("sha256"))
    try:
        size_bytes = max(0, int(row.get("size_bytes") or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid tool artifact size") from exc
    return {
        "tool_call_id": call_id,
        "artifact_ref": str(path),
        "artifact_hash": digest,
        "artifact_size_bytes": size_bytes,
    }


# LLM: 哈希归一必须校验完整摘要，不能把模型式标签或截断值当成持久产物身份。
# 函数用途: 规范化 tool-output SHA-256 为统一的 sha256: 前缀格式。
def _normalized_sha256(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        return ""
    return f"sha256:{text}"


# LLM: 工具引用诊断仅保留类型与运行等元数据，不把坏账本行或产物正文泄露到 Curator 状态和运行日志。
# 函数用途: 生成稳定的 tool-output 引用读取错误。
def _tool_reference_error(exc: BaseException, *, run_id: str) -> dict[str, object]:
    return {
        "context": "memory_curator.tool_reference_read",
        "error_code": "TOOL_REFERENCE_READ_FAILED",
        "error_type": type(exc).__name__,
        "run_id": run_id,
    }


# LLM: 批量读取器整批失败时（读文件出错、查询失败、返回结构不对），旧路径里每个 run 的独立查询都会
#   在同一个确定性原因上失败；这里给每个请求的 run 复制同一条错误，保持错误集合逐条等价。
# 函数用途: 为一次批量查询里的每个 run 生成同类型的读取错误。
def _all_run_errors(run_ids: list[str], exc: BaseException) -> list[dict[str, object]]:
    return [_tool_reference_error(exc, run_id=run_id) for run_id in run_ids]


# LLM: 单个 run 的行处理与旧路径逐条相同（非 dict 行、decode_error 行、越界/坏 size、合法行覆盖写入）；
#   行序由读取器保证与旧全量查询一致，因此同一 (run_id, call_id) 的覆盖结果也一致。
# 函数用途: 把一个 run 的索引行转成有界引用，并收集逐行错误。
def _run_refs_and_errors(
    run_id: str,
    rows: list[object],
    owner_root: Path,
) -> tuple[dict[tuple[str, str], dict[str, object]], list[dict[str, object]]]:
    refs: dict[tuple[str, str], dict[str, object]] = {}
    errors: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            errors.append(_tool_reference_error(ValueError("invalid tool output row"), run_id=run_id))
            continue
        if str(row.get("kind") or "") == "control_plane_decode_error":
            errors.append(
                {
                    "context": "memory_curator.tool_reference_read",
                    "error_code": "TOOL_REFERENCE_READ_FAILED",
                    "error_type": "InvalidJsonlRow",
                    "run_id": run_id,
                    "line": int(row.get("line_number") or 0),
                }
            )
            continue
        try:
            ref = _bounded_tool_output_ref(row, owner_root=owner_root, run_id=run_id)
        except (OSError, ValueError) as exc:
            errors.append(_tool_reference_error(exc, run_id=run_id))
            continue
        if ref:
            refs[(run_id, str(ref["tool_call_id"]))] = ref
    return refs, errors


# LLM: metadata 仅白名单短标量，避免把隐藏工具结果或 provider payload 带进后台模型。
# 函数用途: 提取 request/task/run/channel 等消息元数据。
def _bounded_metadata(payload: dict[str, object]) -> dict[str, object]:
    allowed = {
        "reason",
        "dedupe_key",
    }
    result: dict[str, object] = {}
    for key in sorted(allowed):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 500:
            result[key] = value
    return result


# LLM: content hash 是用户原话证据校验用的精确 UTF-8 hash，不用规范化内容 hash 替代。
# 函数用途: 计算 ConversationStore 正文 SHA-256。
def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


# LLM: audit 时间可能是 ISO 或 epoch；只做格式归一，不用时间决定事实真伪。
# 函数用途: 返回模型可读的 audit 创建时间。
def _audit_time(payload: dict[str, object]) -> str:
    raw = payload.get("created_at") or payload.get("observed_at") or payload.get("timestamp")
    if isinstance(raw, (int, float)):
        return _iso_from_epoch(float(raw))
    return str(raw or "")


# LLM: epoch 转换固定 UTC，顺序仍以文件行和 ConversationStore append 为权威。
# 函数用途: 格式化消息时间。
def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(max(0.0, value), tz=timezone.utc).isoformat()


__all__ = [
    "CuratorAuditInput",
    "CuratorFormalMemoryInput",
    "CuratorInputBatch",
    "CuratorMessageInput",
    "CuratorMessageSource",
    "CuratorToolReferenceSource",
    "collect_curator_inputs",
]
