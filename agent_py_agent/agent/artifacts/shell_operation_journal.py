"""LLM: Persist shell artifact recovery phases below the owner-private backup store.

模块用途: 用稳定 operation key 保存命令前像与执行阶段；Gateway 崩溃后 ShellTool 可按同一
ToolCall.operation_id 核对，而不是依赖进程内快照或猜测命令是否已经执行。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..common.nofollow_fs import (
    read_text_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)

JOURNAL_SCHEMA = "shell_artifact_operation.v1"
JOURNAL_FILE = "operation.json"
JOURNAL_REF_PREFIX = "owner-artifact-operation:"
_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


# LLM: The operation key is host-derived and must remain one plain component. This validation is
# repeated at every storage boundary so a manifest cannot turn a logical id into path traversal.
# 函数用途: 校验 shell 产物恢复操作键是否是固定长度的小写哈希。
def validate_operation_key(operation_key: str) -> str:
    key = str(operation_key or "").strip()
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("invalid shell artifact operation key")
    return key


# LLM: Atomic replacement and fsync make each phase transition all-or-nothing across process loss.
# 函数用途: 原子持久化一次 shell 产物保护操作的完整状态。
def write_operation_manifest(
    backup_store_root: str | Path,
    operation_key: str,
    payload: dict[str, Any],
) -> None:
    key = validate_operation_key(operation_key)
    body = dict(payload)
    body["schema_version"] = JOURNAL_SCHEMA
    body["operation_key"] = key
    write_text_atomic_beneath(
        backup_store_root,
        ("v1", key, JOURNAL_FILE),
        json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n",
    )


# LLM: Reads use the same no-follow chain as writes. A malformed manifest is a hard structural
# failure; callers must keep the operation unknown rather than executing it again.
# 函数用途: 读取并校验指定 operation 的私有恢复清单，不存在时返回 None。
def read_operation_manifest(
    backup_store_root: str | Path,
    operation_key: str,
) -> dict[str, Any] | None:
    key = validate_operation_key(operation_key)
    text = read_text_beneath(backup_store_root, ("v1", key, JOURNAL_FILE))
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OSError("shell artifact operation manifest is malformed") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != JOURNAL_SCHEMA
        or payload.get("operation_key") != key
    ):
        raise OSError("shell artifact operation manifest contract is invalid")
    return payload


# LLM: Manifest cleanup removes only the exact regular leaf under the validated operation key.
# 函数用途: 删除已不再需要的 shell 产物操作清单；blob 是否保留由产物恢复合同单独决定。
def remove_operation_manifest(
    backup_store_root: str | Path,
    operation_key: str,
) -> None:
    key = validate_operation_key(operation_key)
    unlink_file_beneath(backup_store_root, ("v1", key, JOURNAL_FILE))


# LLM: Source refs are logical audit identities and intentionally expose neither owner-home nor
# task-workspace absolute paths.
# 函数用途: 生成工具操作核对使用的稳定私有清单引用。
def operation_manifest_ref(operation_key: str) -> str:
    key = validate_operation_key(operation_key)
    return f"{JOURNAL_REF_PREFIX}v1/{key}/{JOURNAL_FILE}"


__all__ = [
    "JOURNAL_SCHEMA",
    "operation_manifest_ref",
    "read_operation_manifest",
    "remove_operation_manifest",
    "validate_operation_key",
    "write_operation_manifest",
]
