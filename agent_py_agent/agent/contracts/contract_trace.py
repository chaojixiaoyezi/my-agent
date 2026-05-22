# LLM: Contract trace helpers attach bounded machine call-chain refs to findings.
# 模块用途: 给合同 finding 增加最多三层结构化 trace，方便调试时不用手动追多级 JSON 引用。

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_TRACE_DEPTH = 3


# LLM: trace_entry creates one machine-readable contract call-chain item.
# 函数用途: 生成 trace 节点，只保存阶段、引用和路径等结构化字段，不保存自由文本事实。
def trace_entry(
    stage: str,
    *,
    ref: str = "",
    path: str | Path = "",
    code: str = "",
) -> dict[str, str]:
    entry = {"stage": str(stage or "").strip()}
    if ref:
        entry["ref"] = str(ref)
    if path:
        entry["path"] = str(path)
    if code:
        entry["code"] = str(code)
    return {key: value for key, value in entry.items() if value}


# LLM: with_contract_trace merges existing and new trace entries without unbounded growth.
# 函数用途: 给 finding 附加 bounded trace；旧 trace 保留前缀，新 trace 追加到最多三项。
def with_contract_trace(
    finding: dict[str, Any],
    *entries: dict[str, object],
    max_depth: int = MAX_TRACE_DEPTH,
) -> dict[str, object]:
    merged = dict(finding)
    trace = _trace_items(merged.get("trace"))
    trace.extend(_trace_items(list(entries)))
    if trace:
        merged["trace"] = trace[:max(1, int(max_depth))]
    return merged


def _trace_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and str(item.get("stage") or "").strip()]


__all__ = ["MAX_TRACE_DEPTH", "trace_entry", "with_contract_trace"]
