# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

"""Readback verification helpers for memory archive storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 MemoryArchiveError 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 MemoryArchiveError 的状态和协作方法，作为当前模块对外复用的领域对象。
class MemoryArchiveError(RuntimeError):
    """Raised when archive writes cannot be verified after append."""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _verify_record_exists 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 verify record exists 的输入、状态或路径，提前暴露无效数据和越界条件。
def _verify_record_exists(path: Path, *, key: str, value: str, expected: dict[str, Any]) -> None:
    """Read a JSONL file backwards and confirm the just-written record is present."""
    normalized_expected = _normalized_json(expected)
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get(key) != value:
            continue
        if _normalized_json(payload) != normalized_expected:
            raise MemoryArchiveError(f"readback payload mismatch for {key}={value}")
        return
    raise MemoryArchiveError(f"readback failed for {key}={value} in {path}")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _verify_json_file_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 verify json file payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _verify_json_file_payload(path: Path, *, expected: dict[str, Any]) -> None:
    """Ensure an authoritative JSON snapshot file can be read back exactly."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryArchiveError(f"readback failed for snapshot file {path}: {exc}") from exc
    if _normalized_json(payload) != _normalized_json(expected):
        raise MemoryArchiveError(f"readback payload mismatch for snapshot file {path}")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _normalized_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalized json 涉及的字段，让后续匹配和存储使用同一形态。
def _normalized_json(payload: dict[str, Any]) -> str:
    """Serialize JSON payloads into a canonical string for equality checks."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
