# LLM: Tool guardrail detects repeated failed or non-progressing tool calls without killing ordinary tasks.
# 模块用途: 用同一套 N/2N/3N 语义检查“同工具+同参数+同类失败/无进展”，默认只要求换策略，不终止任务。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .models import GateDecision, GateFinding


# LLM: ToolGuardrailConfig mirrors the main-agent runtime repeat gate.
# 类用途: 承载一个通用重复失败/无进展阈值；0 表示无限制，默认不把整个任务置为 blocked。
@dataclass(frozen=True)
class ToolGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    repeat_fail_threshold:
        N in the N/2N/3N policy. 0 means unlimited: no action block, only
        fixed soft hints at 50 and 100.
    terminal_block_enabled:
        False by default. When true, the 3N decision may be treated by callers
        as a terminal block; otherwise it is an action-level block asking the
        model to change strategy.
    """

    repeat_fail_threshold: int = 10
    terminal_block_enabled: bool = False


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
    failure_class: str = ""
    now: float = 0.0


# LLM: evaluate_tool_guardrail_gate checks accumulated per-turn records against the unified repeat policy.
# 函数用途: 在工具执行前调用；读取持久化 records，返回提醒或“本次动作换路”决策。
def evaluate_tool_guardrail_gate(
    facts: ToolGuardrailFacts,
    config: ToolGuardrailConfig | None = None,
    records: tuple[dict[str, object], ...] = (),
) -> GateDecision:
    cfg = config or ToolGuardrailConfig()
    records_list = [dict(r) for r in records if isinstance(r, dict)]
    decision = _repeat_failure_decision(records_list, facts, cfg)
    if decision:
        return decision
    decision = _no_progress_decision(records_list, facts, cfg)
    if decision:
        return decision
    return GateDecision.allow("tool_guardrail", evidence={"tool_name": facts.tool_name})


def _repeat_failure_decision(
    records_list: list[dict[str, object]],
    facts: ToolGuardrailFacts,
    cfg: ToolGuardrailConfig,
) -> GateDecision | None:
    if _threshold(cfg) < 0:
        return None
    count = _count_repeat_failures(records_list, facts.tool_name, facts.args_hash)
    if count <= 0:
        return None
    evidence = _evidence(facts, count, cfg, "failure")
    if _should_action_block(cfg, count):
        code = "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED"
        return GateDecision(
            "tool_guardrail",
            "DENY",
            False,
            (
                GateFinding(
                    code,
                    "P0",
                    f"{facts.tool_name} failed {count} times with identical args and failure class",
                    evidence,
                ),
            ),
            "terminal_block" if cfg.terminal_block_enabled else "change_strategy",
            evidence,
        )
    if _should_hint(cfg, count):
        return GateDecision(
            "tool_guardrail",
            "ALLOW",
            True,
            (
                GateFinding(
                    "TOOL_GUARDRAIL_REPEAT_FAILURE_HINT",
                    "P1",
                    f"{facts.tool_name} repeated the same failing call {count} times; change strategy",
                    evidence,
                ),
            ),
            "change_strategy",
            evidence,
        )
    return None


def _no_progress_decision(
    records_list: list[dict[str, object]],
    facts: ToolGuardrailFacts,
    cfg: ToolGuardrailConfig,
) -> GateDecision | None:
    if not facts.is_readonly or not facts.result_hash:
        return None
    count = _count_no_progress(records_list, facts.tool_name, facts.args_hash, facts.result_hash)
    if count <= 0:
        return None
    evidence = _evidence(facts, count, cfg, "no_progress")
    if _should_action_block(cfg, count):
        return GateDecision(
            "tool_guardrail",
            "DENY",
            False,
            (
                GateFinding(
                    "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED",
                    "P0",
                    f"{facts.tool_name} returned the same result {count} times; change strategy",
                    evidence,
                ),
            ),
            "terminal_block" if cfg.terminal_block_enabled else "change_strategy",
            evidence,
        )
    if _should_hint(cfg, count):
        return GateDecision(
            "tool_guardrail",
            "ALLOW",
            True,
            (
                GateFinding(
                    "TOOL_GUARDRAIL_NO_PROGRESS_WARNING",
                    "P1",
                    f"{facts.tool_name} returned the same result {count} times; use existing result or change strategy",
                    evidence,
                ),
            ),
            "change_strategy",
            evidence,
        )
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
    if facts.failed:
        record["failure_class"] = facts.failure_class or "unknown"
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


def _count_repeat_failures(records: list[dict[str, object]], tool_name: str, args_hash: str) -> int:
    count = 0
    failure_class = ""
    for r in records:
        if str(r.get("tool_name") or "") != tool_name or str(r.get("args_hash") or "") != args_hash:
            continue
        count, failure_class = _repeat_failure_step(r, count, failure_class)
    return count


def _repeat_failure_step(record: dict[str, object], count: int, failure_class: str) -> tuple[int, str]:
    if record.get("failed") is not True:
        return 0, ""
    current_class = str(record.get("failure_class") or "unknown")
    if failure_class and current_class != failure_class:
        return 1, current_class
    return count + 1, current_class


# LLM: _count_no_progress counts recent identical calls with the same result for read-only tools.
# 函数用途: 从后往前扫描相同 tool+args 的记录，只统计连续返回同一 result_hash 的成功只读结果。
def _count_no_progress(records: list[dict[str, object]], tool_name: str, args_hash: str, current_hash: str) -> int:
    count = 0
    for r in reversed(records):
        if str(r.get("tool_name") or "") != tool_name or str(r.get("args_hash") or "") != args_hash:
            continue
        if r.get("failed") is True:
            break
        if str(r.get("result_hash") or "") != current_hash:
            break
        count += 1
    return count


def _threshold(cfg: ToolGuardrailConfig) -> int:
    try:
        return max(0, int(cfg.repeat_fail_threshold))
    except (TypeError, ValueError):
        return 10


def _should_hint(cfg: ToolGuardrailConfig, count: int) -> bool:
    threshold = _threshold(cfg)
    if threshold == 0:
        return count in (50, 100)
    return count in (threshold, threshold * 2)


def _should_action_block(cfg: ToolGuardrailConfig, count: int) -> bool:
    threshold = _threshold(cfg)
    return threshold > 0 and count >= threshold * 3


def _evidence(
    facts: ToolGuardrailFacts,
    count: int,
    cfg: ToolGuardrailConfig,
    repeat_kind: str,
) -> dict[str, Any]:
    return {
        "tool_name": facts.tool_name,
        "args_hash": facts.args_hash,
        "result_hash": facts.result_hash,
        "failure_class": facts.failure_class,
        "repeat_kind": repeat_kind,
        "count": count,
        "repeat_fail_threshold": _threshold(cfg),
        "terminal_block_enabled": cfg.terminal_block_enabled,
    }


__all__ = [
    "ToolGuardrailConfig",
    "ToolGuardrailFacts",
    "args_hash_for_guardrail",
    "evaluate_tool_guardrail_gate",
    "record_tool_guardrail_result",
    "result_hash_for_guardrail",
]
