from __future__ import annotations

"""Owner-local, content-free memory operation audit helpers."""

# LLM: ops.jsonl only records content-free formal mutations; candidates belong exclusively to CandidateService.
# 模块用途: 记录长期记忆新增、替换、删除的编号、哈希、范围、来源和结果，不保存正文。

import hashlib
import time
from pathlib import Path

from ..common.json_io import append_jsonl_capped
from ..common.text_norm import fold_key

_MAX_OPERATION_EVENTS = 4096


# LLM: 规范化只服务精确去重/hash，不承担语义相似判断或控制流。
# 函数用途: 把同一记忆的 Unicode、大小写和空白差异归一，供幂等键使用。
def normalized_memory_content(value: object) -> str:
    return " ".join(fold_key(str(value or "")).split())


# LLM: hash 只标识规范化正文，不能反推正文或替代 owner/entry 权限边界。
# 函数用途: 生成不含正文的候选关联和删除审计指纹。
def memory_content_hash(value: object) -> str:
    return hashlib.sha256(
        normalized_memory_content(value).encode("utf-8", "replace")
    ).hexdigest()


# LLM: active mutation 审计只允许 ID/hash/来源，不得复制记忆正文。
# 函数用途: 在权威记忆提交后记录可排查但不泄露正文的操作事实。
def append_memory_operation_events(
    path: str | Path | None,
    records: list[object],
) -> None:
    """Record content-free active-memory mutations for diagnostics."""

    if path is None:
        return
    target = Path(path)
    for record in records:
        attributes = getattr(record, "attributes", None)
        attributes = attributes if isinstance(attributes, dict) else {}
        content = str(getattr(record, "content", "") or "")
        append_jsonl_capped(
            target,
            {
                "schema": "my-agent.memory-operation.v1",
                "event": "active_memory_mutation",
                "action": str(getattr(record, "action", "") or ""),
                "entry_id": str(getattr(record, "entry_id", "") or ""),
                "version": int(getattr(record, "version", 0) or 0),
                "origin": str(attributes.get("origin") or "legacy"),
                "subject_key": str(attributes.get("subject_key") or ""),
                "scope_type": str(attributes.get("scope_type") or "legacy"),
                "scope_key": str(attributes.get("scope_key") or "legacy"),
                "content_hash": memory_content_hash(content) if content else "",
                "source": str(getattr(record, "source", "") or ""),
                "result": "committed",
                "observed_at": time.time(),
            },
            max_records=_MAX_OPERATION_EVENTS,
        )


# LLM: hard delete audit is append-only and content-free; candidate plaintext removal is owned by CandidateService.
# 函数用途: 长期记忆硬删除后追加无正文 tombstone，保留编号和哈希供审计。
def append_memory_purge_event(
    path: str | Path | None,
    *,
    entry_ids: set[str],
    content_hashes: set[str],
) -> None:
    if path is None:
        return
    append_jsonl_capped(
        Path(path),
        {
            "schema": "my-agent.memory-operation.v1",
            "event": "active_memory_purged",
            "entry_ids": sorted(entry_ids),
            "content_hashes": sorted(content_hashes),
            "result": "committed",
            "observed_at": time.time(),
        },
        max_records=_MAX_OPERATION_EVENTS,
    )


__all__ = [
    "append_memory_purge_event",
    "append_memory_operation_events",
    "memory_content_hash",
    "normalized_memory_content",
]
