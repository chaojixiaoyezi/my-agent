# LLM: C可选严格IO只沿原ops事件格式/4096上限，Root外持锁准备，默认False原调用不添global guard。
# 模块用途: 保原无正文操作审计，并把C固定raw末写交当次原scope。
from __future__ import annotations

"""Owner-local, content-free memory operation audit helpers."""

# LLM: ops.jsonl only records content-free formal mutations; candidates belong exclusively to CandidateService.
#   落盘走 append_private_jsonl_capped（pw2）：目录 0700、文件 0600、存量宽权限下次写入收紧。
# 模块用途: 记录长期记忆新增、替换、删除的编号、哈希、范围、来源和结果，不保存正文。

import hashlib
import json
import time
from pathlib import Path

from ..common.global_write_commit import execute_global_write
from ..common.json_io import (
    append_private_jsonl_capped,
    locked_json_path,
    read_jsonl_objects_report,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
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
# LLM: C只透传IO策略，原每record事件/时间/身份/4096裁剪不合并；锁/读/编码门外，末replace_once入原scope。
# 函数用途: 逐原记录追加无正文审计，C失败保实际已完成事件前缀。
def append_memory_operation_events(
    path: str | Path | None,
    records: list[object],
    *,
    coordinator_io: bool = False,
) -> None:
    """Record content-free active-memory mutations for diagnostics."""

    if path is None:
        return
    target = Path(path)
    for record in records:
        attributes = getattr(record, "attributes", None)
        attributes = attributes if isinstance(attributes, dict) else {}
        content = str(getattr(record, "content", "") or "")
        _append_operation_record(
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
            **({} if not coordinator_io else {"coordinator_io": True}),
        )


# LLM: hard delete audit is append-only and content-free; candidate plaintext removal is owned by CandidateService.
# LLM: C仅绑定原ops path的固定capped写叶，无新增quota/embedding或第二审计；默认参数/字节不变。
# 函数用途: 沿原删除顺序保存同一无正文审计事件，失效不无权补账。
def append_memory_purge_event(
    path: str | Path | None,
    *,
    entry_ids: set[str],
    content_hashes: set[str],
    coordinator_io: bool = False,
) -> None:
    if path is None:
        return
    _append_operation_record(
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
        **({} if not coordinator_io else {"coordinator_io": True}),
    )


__all__ = [
    "append_memory_purge_event",
    "append_memory_operation_events",
    "memory_content_hash",
    "normalized_memory_content",
]


# LLM: 默认严格委托原capped append；C保原读取/坏行跳过/裁剪/编码，在持原锁的Root外准备，仅raw写入门。
# 函数用途: 将同一个ops事件按原上限写回原文件，不合并业务事件或新建状态。
def _append_operation_record(path, record, *, max_records, coordinator_io=False):
    if not coordinator_io:
        return append_private_jsonl_capped(path, record, max_records=max_records)
    _coordinator_operation_write(lambda: ensure_private_dir(path.parent))
    with locked_json_path(path, birth_commit=_coordinator_operation_write):
        records = read_jsonl_objects_report(path).records
        records.append(record)
        if max_records > 0:
            records = records[-max_records:]
        content = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
        _coordinator_operation_write(lambda: write_private_text_file_atomic_unlocked(
            path, content, replace_once=True))


# LLM: 只消费当前原global scope，目录/FD出生与已准备raw叶逐次核，不持scan/锁等待/付费业务。
# 函数用途: 为C原ops文件的固定本地写调用真实末门。
def _coordinator_operation_write(operation):
    return execute_global_write(operation, require_scope=True)
