# LLM: Tool guardrail detects repeated failed or non-progressing tool calls within a turn.
# 模块用途: 参考 长期助手 tool_guardrails.py，在每个工具调用前后检查 exact 重复失败、同工具累积失败、只读工具无进展三种循环模式。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .models import GateDecision, GateFinding


# LLM: ToolGuardrailConfig holds thresholds for loop-detection severity escalation.
# 类用途: 承载 exact 重复失败、同工具累积失败、无进展检测三种循环检测的 warn/block 阈值。
@dataclass(frozen=True)
class ToolGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection."""

    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_block_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5


# LLM: ToolGuardrailFacts bundles per-call structured inputs for loop detection.
# 类用途: 封装工具名、参数 hash、失败标记、结果 hash、只读标记和时间戳等 guardrail 判据。
@dataclass(frozen=True)
class ToolGuardrailFacts:
    """Structured facts for one tool guardrail check.

    Before call: pass tool_name + args_hash; leave failed/result_hash empty.
    After call:  pass tool_name + args_hash + failed=True/False + result_hash.
    """

    tool_name: str
    args_hash: str
    failed: bool = False
    result_hash: str = ""
    is_readonly: bool = True
    now: float = 0.0


# LLM: evaluate_tool_guardrail_gate checks accumulated per-turn records against loop thresholds.
# 函数用途: 在工具执行前调用；读取持久化的 guardrail_records，判断是否应 warn/block 本次调用。
def evaluate_tool_guardrail_gate(
    facts: ToolGuardrailFacts,
    config: ToolGuardrailConfig | None = None,
    records: tuple[dict[str, object], ...] = (),
) -> GateDecision:
    cfg = config or ToolGuardrailConfig()
    records_list = [dict(r) for r in records if isinstance(r, dict)]
    # Exact failure repeat
    decision = _check_threshold(records_list, facts, cfg, "exact")
    if decision:
        return decision
    # Same-tool cumulative failure
    decision = _check_threshold(records_list, facts, cfg, "same_tool")
    if decision:
        return decision
    # No-progress (readonly tools only)
    decision = _check_threshold(records_list, facts, cfg, "no_progress")
    if decision:
        return decision
    return GateDecision.allow("tool_guardrail", evidence={"tool_name": facts.tool_name})


# LLM: _check_threshold evaluates one guardrail dimension against its warn/block thresholds.
# 函数用途: 根据 check_kind 选择计数器和阈值，返回 DENY/ALLOW_WARN 或 None 表示继续。
def _check_threshold(
    records_list: list[dict[str, object]],
    facts: ToolGuardrailFacts,
    cfg: ToolGuardrailConfig,
    kind: str,
) -> GateDecision | None:
    if kind not in ("exact", "same_tool") and not facts.is_readonly:
        return None
    if kind == "exact":
        count = _count_exact_failures(records_list, facts.tool_name, facts.args_hash)
        block_after, warn_after = cfg.exact_failure_block_after, cfg.exact_failure_warn_after
        block_code, warn_code = "TOOL_GUARDRAIL_EXACT_FAILURE_BLOCKED", "TOOL_GUARDRAIL_EXACT_FAILURE_WARNING"
        msg = f"{facts.tool_name} failed {count} times with identical args"
    elif kind == "same_tool":
        count = _count_same_tool_failures(records_list, facts.tool_name)
        block_after, warn_after = cfg.same_tool_failure_block_after, cfg.same_tool_failure_warn_after
        block_code, warn_code = "TOOL_GUARDRAIL_SAME_TOOL_FAILURE_BLOCKED", "TOOL_GUARDRAIL_SAME_TOOL_FAILURE_WARNING"
        msg = f"{facts.tool_name} failed {count} times this turn"
    else:
        count = _count_no_progress(records_list, facts.tool_name, facts.args_hash, facts.result_hash)
        block_after, warn_after = cfg.no_progress_block_after, cfg.no_progress_warn_after
        block_code, warn_code = "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED", "TOOL_GUARDRAIL_NO_PROGRESS_WARNING"
        msg = f"{facts.tool_name} returned same result {count} times"
    evidence = {"tool_name": facts.tool_name, "count": count}
    if block_after > 0 and count >= block_after:
        return GateDecision("tool_guardrail", "DENY", False, (GateFinding(block_code, "P0", msg, evidence),), "repair_tool_call")
    if warn_after > 0 and count >= warn_after:
        return GateDecision("tool_guardrail", "ALLOW", True, (GateFinding(warn_code, "P1", msg, evidence),))
    return None


# LLM: record_tool_guardrail_result appends a structured record after tool execution completes.
# 函数用途: 在工具执行后调用，把失败状态和结果 hash 写入 records 供下一轮 before_call 检查。
def record_tool_guardrail_result(
    records: tuple[dict[str, object], ...],
    facts: ToolGuardrailFacts,
    max_records: int = 256,
) -> tuple[dict[str, object], ...]:
    record: dict[str, object] = {
        "tool_name": facts.tool_name,
        "args_hash": facts.args_hash,
        "failed": facts.failed,
    }
    if facts.result_hash:
        record["result_hash"] = facts.result_hash
    if facts.now:
        record["ts"] = facts.now
    new_records = list(records)
    new_records.append(record)
    if len(new_records) > max_records:
        new_records = new_records[-max_records:]
    return tuple(new_records)


# LLM: args_hash_for_guardrail produces a stable hash for tool call identity comparison.
# 函数用途: 给 tool guardrail 计算 tool+args 的 sha256，供 exact-failure 和 no-progress 去重。
def args_hash_for_guardrail(tool_name: str, args: object) -> str:
    try:
        canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = str(args)
    raw = f"{tool_name}:{canonical}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# LLM: result_hash_for_guardrail produces a stable hash of tool result content.
# 函数用途: 给 no-progress 检测比对两次调用结果是否相同。
def result_hash_for_guardrail(result: str | None) -> str:
    if not result:
        return ""
    try:
        parsed = json.loads(result)
        canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (json.JSONDecodeError, TypeError):
        canonical = result
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# LLM: _count_exact_failures counts consecutive identical-args failures, resetting on success.
# 函数用途: 统计同一 tool+args_hash 的连续失败次数，一旦成功就归零。
def _count_exact_failures(records: list[dict[str, object]], tool_name: str, args_hash: str) -> int:
    count = 0
    for r in records:
        if str(r.get("tool_name") or "") != tool_name or str(r.get("args_hash") or "") != args_hash:
            continue
        if r.get("failed") is True:
            count += 1
        else:
            count = 0
    return count


# LLM: _count_same_tool_failures counts consecutive same-tool failures regardless of args.
# 函数用途: 统计同一工具名的连续失败次数，参数可能不同但工具相同。
def _count_same_tool_failures(records: list[dict[str, object]], tool_name: str) -> int:
    count = 0
    for r in records:
        if str(r.get("tool_name") or "") != tool_name:
            continue
        if r.get("failed") is True:
            count += 1
        else:
            count = 0
    return count


# LLM: _count_no_progress counts recent identical calls with the same result for read-only tools.
# 函数用途: 从后往前扫描相同 tool+args 的记录，统计连续返回相同结果的次数。
def _count_no_progress(records: list[dict[str, object]], tool_name: str, args_hash: str, _current_hash: str) -> int:
    count = 0
    for r in reversed(records):
        if str(r.get("tool_name") or "") != tool_name or str(r.get("args_hash") or "") != args_hash:
            continue
        if r.get("failed") is True:
            break
        count += 1
    return count


__all__ = [
    "ToolGuardrailConfig",
    "ToolGuardrailFacts",
    "args_hash_for_guardrail",
    "evaluate_tool_guardrail_gate",
    "record_tool_guardrail_result",
    "result_hash_for_guardrail",
]
