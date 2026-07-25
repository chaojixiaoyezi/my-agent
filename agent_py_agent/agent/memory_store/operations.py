from __future__ import annotations

"""Owner-local memory candidate and operation ledger helpers.

``memory.jsonl`` remains the only active recall authority. ``ops.jsonl`` stores
review/audit facts and model-inferred candidates; candidates are never read by
the recall path.
"""

# LLM: ops.jsonl 只能承载候选和无正文审计，绝不能成为 search/prompt/compact 的第二事实源。
# 模块用途: 管理当前 owner 的记忆候选和操作审计，同时保证删除时不残留候选明文。

import hashlib
import json
import time
from pathlib import Path

from ..common.json_io import (
    append_jsonl_capped,
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic_unlocked,
)
from ..common.text_norm import fold_key, nfc

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


# LLM: candidate id 必须由结构化字段确定性生成，重复观察不能制造不同候选身份。
# 函数用途: 为模型推测的同一条候选生成稳定编号。
def memory_candidate_id(
    *,
    role: object,
    kind: object,
    content: object,
    subject_key: object = "",
) -> str:
    material = "\0".join(
        (
            fold_key(str(role or "user")),
            fold_key(str(kind or "fact")),
            normalized_memory_content(content),
            fold_key(str(subject_key or "")),
        )
    )
    return "memory-candidate-" + hashlib.sha256(material.encode()).hexdigest()[:20]


# LLM: 此入口只能写 ops 候选，禁止调用 active memory add；写入有界账本且不进入召回。
# 函数用途: 记录一条模型推测，等待后续明确确认，不直接改变长期记忆。
def record_memory_candidate(
    path: str | Path | None,
    *,
    role: object,
    kind: object,
    content: object,
    tags: list[str] | None,
    source: object,
    subject_key: object = "",
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    """Append a non-active inferred candidate to the existing owner ops ledger."""

    candidate_id = memory_candidate_id(
        role=role,
        kind=kind,
        content=content,
        subject_key=subject_key,
    )
    payload: dict[str, object] = {
        "schema": "my-agent.memory-operation.v1",
        "event": "candidate_observed",
        "candidate_id": candidate_id,
        "origin": "model_inferred",
        "role": str(role or "user"),
        "kind": str(kind or "fact"),
        "content": nfc(str(content or "")),
        "content_hash": memory_content_hash(content),
        "tags": list(tags or []),
        "subject_key": str(subject_key or ""),
        "evidence_refs": list(evidence_refs or []),
        "source": str(source or ""),
        "observed_at": time.time(),
    }
    if path is not None:
        _append_candidate_event(Path(path), payload)
    return payload


# LLM: 同 candidate 的首次事件保存正文，后续观察只追加 hash/count，避免候选噪音和明文复制。
# 函数用途: 在同一文件锁内去重候选观察，并维持有界 ops 账本。
def _append_candidate_event(path: Path, payload: dict[str, object]) -> None:
    with locked_json_path(path):
        rows = read_jsonl_objects_report(
            path,
            context="memory_store.record_memory_candidate",
        ).records
        candidate_id = str(payload.get("candidate_id") or "")
        prior_count = sum(
            1 for row in rows if str(row.get("candidate_id") or "") == candidate_id
        )
        event = dict(payload)
        if prior_count:
            event["event"] = "candidate_reobserved"
            event["observation_count"] = prior_count + 1
            event.pop("content", None)
            event.pop("tags", None)
            event.pop("evidence_refs", None)
        rows.append(event)
        rows = rows[-_MAX_OPERATION_EVENTS:]
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        write_text_file_atomic_unlocked(path, text)


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
                "content_hash": memory_content_hash(content) if content else "",
                "source": str(getattr(record, "source", "") or ""),
                "observed_at": time.time(),
            },
            max_records=_MAX_OPERATION_EVENTS,
        )


# LLM: hard delete 必须同时按 entry/hash 清除 ops 候选正文，只留下无正文 purge 事实。
# 函数用途: 用户删除长期记忆时一并消除候选账本中的相同私人内容。
def purge_memory_operation_content(
    path: str | Path | None,
    *,
    entry_ids: set[str],
    content_hashes: set[str],
) -> None:
    """Remove candidate plaintext linked to a hard-deleted active memory."""

    if path is None:
        return
    target = Path(path)
    with locked_json_path(target):
        rows = read_jsonl_objects_report(
            target,
            context="memory_store.purge_memory_operation_content",
        ).records
        retained = [
            row
            for row in rows
            if str(row.get("entry_id") or "") not in entry_ids
            and str(row.get("content_hash") or "") not in content_hashes
        ]
        retained.append(
            {
                "schema": "my-agent.memory-operation.v1",
                "event": "active_memory_purged",
                "entry_ids": sorted(entry_ids),
                "content_hashes": sorted(content_hashes),
                "observed_at": time.time(),
            }
        )
        retained = retained[-_MAX_OPERATION_EVENTS:]
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in retained)
        write_text_file_atomic_unlocked(target, text)


__all__ = [
    "append_memory_operation_events",
    "memory_candidate_id",
    "memory_content_hash",
    "normalized_memory_content",
    "purge_memory_operation_content",
    "record_memory_candidate",
]
