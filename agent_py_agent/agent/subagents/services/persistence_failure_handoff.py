# LLM: Persistence helpers for failure handoff records keep recovery artifacts focused.
# 模块用途: 归一化和写入失败交接 JSON，避免 persistence 主流程继续膨胀。

from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from ..models import FailureHandoff, SubAgentTask


# LLM: normalize_failure_handoff keeps task JSON compatible across handoff schema changes.
# 函数用途: 归一化 failure handoff 输入，空值保持为空记录。
def normalize_failure_handoff(value: object) -> FailureHandoff:
    if isinstance(value, FailureHandoff):
        return value
    if not isinstance(value, dict):
        return FailureHandoff()
    payload = {key: value[key] for key in _field_names(FailureHandoff) if key in value}
    for key in ["artifact_refs", "evidence_refs", "avoid_next_time"]:
        payload[key] = _string_list(payload.get(key))
    payload["reserved"] = _dict_value(payload.get("reserved"))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return FailureHandoff(**payload)


# LLM: write_failure_handoff persists only meaningful failure records.
# 函数用途: 将失败交接记录写成机器可读 JSON；空记录不写入。
def write_failure_handoff(task: SubAgentTask) -> None:
    if not task.failure_handoff_json or not task.failure_handoff.run_id:
        return
    Path(task.failure_handoff_json).write_text(
        json.dumps(asdict(task.failure_handoff), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# LLM: _field_names keeps dict normalization aligned with the dataclass definition.
# 函数用途: 获取 FailureHandoff 的合法字段，过滤旧 JSON 中的未知键。
def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


# LLM: _string_list normalizes persisted refs and advice into clean string lists.
# 函数用途: 将 JSON 中可能缺失或非列表的字段转成字符串列表。
def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [] if value in (None, "") else [str(value)]


# LLM: _dict_value preserves reserved payloads only when they are dictionaries.
# 函数用途: 安全读取 reserved 字段，避免坏数据进入 dataclass。
def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


# LLM: _float_value tolerates historical non-numeric timestamps in handoff files.
# 函数用途: 将 created_at 归一化为 float，失败时返回 0。
def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
