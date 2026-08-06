from __future__ import annotations

"""从 ConversationStore 与 owner audit 增量构造有界 Curator 输入。"""

# LLM: 本模块只读权威经历并生成预览；不写 cursor、不推断候选、不复制工具大输出。
# 模块用途: 把精确 message/audit 游标后的新事件整理为后台模型的有限 JSON 输入。

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import read_jsonl_objects_report
from .curator_formal import CuratorFormalMemoryInput
from .curator_models import MemoryCuratorConfig, MemoryCuratorState

_MESSAGE_PREVIEW_CHARS = 6_000
_AUDIT_PREVIEW_CHARS = 1_000

ToolReferenceQuery = Callable[[Path, str, int], dict[str, object]]


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

    # LLM: Runtime IDs come only from structured ConversationStore metadata; the Gateway's
    # canonical gateway_request_id is normalized to request_id without inferring from prose.
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

    # LLM: audit 引用不包含正文，只暴露 typed 状态、ID、hash 和 artifact/source refs。
    # 函数用途: 生成一条运行事件的最小证据引用。
    def ref(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "status": self.status,
            "operation_id": self.operation_id,
            "tool_call_id": self.tool_call_id,
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

    # LLM: 模型只能看到有界 preview；完整工具大输出继续由 artifact 权威保存。
    # 函数用途: 把一条 audit 事件转成 Curator 输入对象。
    def to_model(self) -> dict[str, object]:
        return {**self.ref(), "preview": self.preview}


# LLM: batch 保存的是这次 lease 看到的精确输入顺序；模型声明只能引用这里的 ID。
# 类用途: 为 prompt、证据核验和 cursor 计算提供同一输入快照。
@dataclass(frozen=True)
class CuratorInputBatch:
    messages: tuple[CuratorMessageInput, ...]
    audit_events: tuple[CuratorAuditInput, ...]
    formal_memories: tuple[CuratorFormalMemoryInput, ...] = ()
    load_errors: tuple[dict[str, object], ...] = ()
    # 可降级读取错误(如 formal 记忆整体失败):与 load_errors 严格语义不同,只记不阻断。
    formal_memory_errors: tuple[dict[str, object], ...] = ()

    # LLM: 序列化保持消息/audit/formal-memory 的冻结顺序，不能在 prompt 阶段补猜引用。
    # 函数用途: 生成一次后台模型调用的完整结构化输入。
    def to_model_payload(self) -> dict[str, object]:
        return {
            "messages": [item.to_model() for item in self.messages],
            "audit_events": [item.to_model() for item in self.audit_events],
            "formal_memories": [item.to_model() for item in self.formal_memories],
        }

    # LLM: 轮次阈值只统计结构化 role=user 的新消息，不解析正文或 assistant 摘要。
    # 函数用途: 计算本批游标后的真实用户轮次数。
    def user_turns(self) -> int:
        return sum(1 for item in self.messages if item.role == "user")


# LLM: This adapter reads only bounded metadata from the existing runtime-memory control plane;
# it never reads artifact bodies and never treats output-index status as execution authority.
# 类用途: 从当前 owner 的规范 tool-output 索引补全 Curator 的 artifact 引用、哈希和大小。
@dataclass(frozen=True)
class CuratorToolReferenceSource:
    owner_root: Path
    query: ToolReferenceQuery

    # LLM: Exact structured run/call identity is required and every returned path must remain
    # inside this owner root, preventing one owner's index from introducing another owner's ref.
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
        for run_id in run_ids:
            try:
                result = self.query(
                    self.owner_root,
                    run_id,
                    max(1, min(2_048, int(limit_per_run))),
                )
            except (OSError, UnicodeError, ValueError) as exc:
                errors.append(_tool_reference_error(exc, run_id=run_id))
                continue
            if result.get("ok") is not True:
                errors.append(_tool_reference_error(ValueError("control plane failed"), run_id=run_id))
                continue
            rows = result.get("tool_outputs")
            if not isinstance(rows, list):
                errors.append(_tool_reference_error(ValueError("invalid tool_outputs"), run_id=run_id))
                continue
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
                    ref = _bounded_tool_output_ref(row, owner_root=self.owner_root, run_id=run_id)
                except (OSError, ValueError) as exc:
                    errors.append(_tool_reference_error(exc, run_id=run_id))
                    continue
                if ref:
                    refs[(run_id, str(ref["tool_call_id"]))] = ref
        return refs, errors


# LLM: collection 必须用 ConversationStore 的 typed cursor read，不直接解析其文件布局。
# 函数用途: 收集所有线程游标后的有界消息和 audit 事件。
def collect_curator_inputs(
    *,
    conversation_store: object,
    audit_dir: str | Path,
    state: MemoryCuratorState,
    config: MemoryCuratorConfig,
    formal_memories: tuple[CuratorFormalMemoryInput, ...] = (),
    tool_reference_source: CuratorToolReferenceSource | None = None,
) -> CuratorInputBatch:
    messages, message_errors = _collect_messages(
        conversation_store=conversation_store,
        cursors=state.per_thread_cursors,
        limit=config.batch_message_limit,
        max_chars=config.max_input_chars,
    )
    remaining_chars = max(
        1_000,
        config.max_input_chars
        - len(json.dumps([item.to_model() for item in messages], ensure_ascii=False)),
    )
    audit_events, audit_errors = _collect_audit(
        Path(audit_dir),
        after_event_id=state.last_processed_audit_event_id,
        limit=config.batch_message_limit,
        max_chars=remaining_chars,
    )
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
    )


# LLM: list_threads 只列元数据；每个 transcript 从精确 cursor 后读取，坏线程阻断 cursor 推进。
# 函数用途: 收集最早仍未处理的消息直至批量/字符上限。
def _collect_messages(
    *,
    conversation_store: object,
    cursors: dict[str, str],
    limit: int,
    max_chars: int,
) -> tuple[list[CuratorMessageInput], list[dict[str, object]]]:
    list_report = getattr(conversation_store, "list_threads_report", None)
    after_report = getattr(conversation_store, "messages_after_report", None)
    if not callable(list_report) or not callable(after_report):
        raise TypeError("ConversationStore lacks incremental Memory Curator read contract")
    threads, thread_errors = list_report(limit=0)
    errors: list[dict[str, object]] = [dict(item) for item in thread_errors]
    selected: list[CuratorMessageInput] = []
    used_chars = 0
    for thread in sorted(threads, key=lambda item: (float(item.updated_at), str(item.thread_id))):
        if len(selected) >= limit or used_chars >= max_chars:
            break
        entries, load_errors = after_report(
            str(thread.thread_id),
            after_message_id=str(cursors.get(str(thread.thread_id)) or ""),
            limit=limit - len(selected),
        )
        errors.extend(dict(item) for item in load_errors)
        if load_errors:
            continue
        for entry in entries:
            content = str(entry.content or "")
            remaining = max_chars - used_chars
            if remaining < 300:
                break
            preview_limit = min(_MESSAGE_PREVIEW_CHARS, max(200, remaining - 250))
            preview = content[:preview_limit]
            metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
            item = CuratorMessageInput(
                message_id=str(entry.message_id),
                thread_id=str(entry.thread_id),
                role=str(entry.role),
                channel=str(entry.channel),
                created_at=float(entry.created_at),
                full_content=content,
                content_preview=preview,
                content_hash=_content_hash(content),
                metadata=dict(metadata),
            )
            item_chars = len(json.dumps(item.to_model(), ensure_ascii=False))
            if selected and used_chars + item_chars > max_chars:
                break
            selected.append(item)
            used_chars += item_chars
            if len(selected) >= limit:
                break
    return selected, errors


# LLM: audit cursor 按 daily 文件和行的追加顺序扫描；找不到旧 cursor 必须报告损坏而非从头重放。
# 函数用途: 收集 last_processed_audit_event_id 后的有界 audit 预览。
def _collect_audit(
    audit_dir: Path,
    *,
    after_event_id: str,
    limit: int,
    max_chars: int,
) -> tuple[list[CuratorAuditInput], list[dict[str, object]]]:
    if not audit_dir.exists():
        return [], []
    target = str(after_event_id or "").strip()
    cursor_found = not target
    selected: list[CuratorAuditInput] = []
    errors: list[dict[str, object]] = []
    used_chars = 0
    for path in sorted(audit_dir.glob("*.jsonl")):
        rows, path_errors = _read_audit_rows(path)
        errors.extend(path_errors)
        for payload in rows:
            event_id = str(payload.get("event_id") or "").strip()
            if not cursor_found:
                cursor_found = event_id == target
                continue
            item = _audit_input(payload)
            item_chars = len(json.dumps(item.to_model(), ensure_ascii=False))
            if selected and (len(selected) >= limit or used_chars + item_chars > max_chars):
                return selected, errors
            selected.append(item)
            used_chars += item_chars
            if len(selected) >= limit:
                return selected, errors
    if target and not cursor_found:
        errors.append(
            {
                "context": "memory_curator.audit_cursor",
                "error_code": "AUDIT_CURSOR_MISSING",
                "event_id": target,
            }
        )
    return selected, errors


# LLM: Any malformed row marks the whole input batch unsafe; valid rows are returned only so
# diagnostics preserve cursor discovery without nested file parsing logic.
# 函数用途: 严格读取一个 audit 日分片并转换通用错误为 Curator 稳定错误。
def _read_audit_rows(path: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not path.is_file():
        return [], []
    report = read_jsonl_objects_report(path, context="memory_curator.audit_read")
    errors = [
        {
            "context": "memory_curator.audit_read",
            "error_code": "AUDIT_READ_FAILED",
            "error_type": str(item.get("error_type") or item.get("error_code") or "InvalidRow"),
            "path": str(path),
            "line": int(item.get("line") or 0),
        }
        for item in report.load_errors
    ]
    rows: list[dict[str, object]] = []
    for payload in report.records:
        if not str(payload.get("event_id") or "").strip():
            errors.append(_load_error(path, ValueError("audit row lacks event_id"), line=0))
            continue
        rows.append(dict(payload))
    return rows, errors


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
    return CuratorAuditInput(
        event_id=str(payload.get("event_id") or ""),
        event_type=str(
            payload.get("event_type") or payload.get("event") or payload.get("action") or ""
        ),
        created_at=_audit_time(payload),
        status=str(payload.get("status") or payload.get("effect_outcome") or ""),
        operation_id=str(payload.get("operation_id") or ""),
        tool_call_id=str(payload.get("tool_call_id") or payload.get("call_id") or ""),
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


# LLM: Enrichment is keyed only by exact run_id/tool_call_id and copies no status, operation, or
# output text, so it cannot upgrade an audit event into successful tool evidence.
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


# LLM: Artifact enrichment must not silently exceed the same model-input budget used before the
# lookup; trimming preserves the audit append-order prefix required by cursor advancement.
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


# LLM: Only exact scalar artifact metadata leaves the control plane; parameters, source input,
# summaries, and any embedded output fields are intentionally discarded.
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


# LLM: Hash normalization validates the full digest instead of accepting a model-like label or
# truncated value as durable artifact identity.
# 函数用途: 规范化 tool-output SHA-256 为统一的 sha256: 前缀格式。
def _normalized_sha256(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        return ""
    return f"sha256:{text}"


# LLM: Tool-reference diagnostics contain only type/run/line metadata and never leak the bad
# ledger row or artifact content into Curator state and run logs.
# 函数用途: 生成稳定的 tool-output 引用读取错误。
def _tool_reference_error(exc: BaseException, *, run_id: str) -> dict[str, object]:
    return {
        "context": "memory_curator.tool_reference_read",
        "error_code": "TOOL_REFERENCE_READ_FAILED",
        "error_type": type(exc).__name__,
        "run_id": run_id,
    }


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


# LLM: 输入损坏只暴露类型、路径和行号，不把坏行正文复制进 state/run audit。
# 函数用途: 构造稳定读错误。
def _load_error(path: Path, exc: BaseException, *, line: int) -> dict[str, object]:
    return {
        "context": "memory_curator.audit_read",
        "error_code": "AUDIT_READ_FAILED",
        "error_type": type(exc).__name__,
        "path": str(path),
        "line": line,
    }


__all__ = [
    "CuratorAuditInput",
    "CuratorFormalMemoryInput",
    "CuratorInputBatch",
    "CuratorMessageInput",
    "CuratorToolReferenceSource",
    "collect_curator_inputs",
]
