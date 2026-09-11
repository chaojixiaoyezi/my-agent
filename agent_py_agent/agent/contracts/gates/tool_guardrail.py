# LLM: 本模块只按结构化调用/结果事实判定既有动作门与独立软观察；重复成功不能扩权、代替执行或升级硬门。
# 模块用途: 统一保存有界工具观测，并分别提供重复失败/只读门和成功重复提醒。
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..recovery import RecoveryAction
from .models import GateDecision, GateFinding


# LLM: 旧重复失败/只读阈值仍只影响原门；成功重复阈值仅由执行后的观察器读取。
# 类用途: 保存工具重复诊断配置，修改默认值时同步运行门 YAML 和读取入口。
@dataclass(frozen=True)
class ToolGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    repeat_fail_threshold:
        N in the N/2N/3N policy. 0 means unlimited: no action block, only
        fixed soft hints at 50 and 100.
    readonly_no_progress_threshold:
        Same N/2N/3N policy for read-only calls returning identical results.
        This defaults lower because repeated successful reads burn context
        without adding evidence.
    terminal_block_enabled:
        False by default. When true, the 3N decision may be treated by callers
        as a terminal block; otherwise it is an action-level block asking the
        model to change strategy.
    repeated_success_hint_threshold:
        Post-execution observation only. 0 disables it; the action gate does
        not read this threshold and cannot deny a successful repeat with it.
    """

    repeat_fail_threshold: int = 10
    readonly_no_progress_threshold: int = 3
    terminal_block_enabled: bool = False
    repeated_success_hint_threshold: int = 5


# LLM: observed_success 只来自已执行且非 UNKNOWN 的规范结果，不能从正文或安全 effect 推测。
# 类用途: 表示一条工具调用的哈希与执行事实，供原 gate 和成功重复提醒共享。
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
    observed_success: bool = False
    run_id: str = ""


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
    if _repeat_threshold(cfg) < 0:
        return None
    count = _count_repeat_failures(records_list, facts.tool_name, facts.args_hash)
    if count <= 0:
        return None
    evidence = _evidence(facts, count, cfg, "failure")
    if _should_action_block(cfg, count):
        code = "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED"
        action_evidence = _action_block_evidence(evidence, cfg)
        return GateDecision(
            "tool_guardrail",
            "DENY",
            False,
            (
                GateFinding(
                    code,
                    "P0",
                    f"{facts.tool_name} failed {count} times with identical args and failure class",
                    action_evidence,
                ),
            ),
            RecoveryAction.CHANGE_STRATEGY.value,
            action_evidence,
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
    if _should_action_block(cfg, count, repeat_kind="no_progress"):
        action_evidence = _action_block_evidence(evidence, cfg)
        return GateDecision(
            "tool_guardrail",
            "DENY",
            False,
            (
                GateFinding(
                    "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED",
                    "P0",
                    f"{facts.tool_name} returned the same result {count} times; change strategy",
                    action_evidence,
                ),
            ),
            RecoveryAction.CHANGE_STRATEGY.value,
            action_evidence,
        )
    if _should_hint(cfg, count, repeat_kind="no_progress"):
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


# LLM: records 是唯一有界观测历史；保留安全 effect 与成功事实，不为提醒另建持久队列。
# 函数用途: 追加一次规范调用观测，既有失败/只读 gate 与成功重复提醒共用该记录。
def record_tool_guardrail_result(
    records: tuple[dict[str, object], ...],
    facts: ToolGuardrailFacts,
    max_records: int = 256,
) -> tuple[dict[str, object], ...]:
    record: dict[str, object] = {
        "tool_name": facts.tool_name,
        "args_hash": facts.args_hash,
        "failed": facts.failed,
        "is_readonly": facts.is_readonly,
        "observed_success": facts.observed_success,
        "run_id": facts.run_id,
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


# LLM: 只观察已执行成功的相同 tool/args/result；返回 Finding 而非 GateDecision，调用方不得据此拒绝或重放工具。
# 函数用途: 在阈值及倍增点提醒模型检查重复动作，利用原记录上的提示标记避免窗口饱和后逐轮刷屏。
def repeated_success_observation(
    facts: ToolGuardrailFacts,
    records: tuple[dict[str, object], ...],
    *,
    threshold: int,
) -> GateFinding | None:
    if threshold <= 0 or not facts.observed_success or not facts.result_hash:
        return None
    matching: list[dict[str, object]] = []
    for record in reversed(records):
        if record.get("run_id") != facts.run_id:
            continue
        if record.get("tool_name") != facts.tool_name or record.get("args_hash") != facts.args_hash:
            continue
        if record.get("observed_success") is not True or record.get("result_hash") != facts.result_hash:
            break
        matching.append(record)
    count = len(matching)
    multiple, remainder = divmod(count, threshold)
    if remainder or multiple <= 0 or multiple & (multiple - 1):
        return None
    if any(record.get("repeated_success_hint_count") == count for record in matching):
        return None
    return GateFinding(
        "TOOL_REPEATED_SUCCESS_OBSERVATION",
        "P1",
        f"{facts.tool_name} 使用相同参数已执行成功 {count} 次，返回内容也相同。"
        "结果相同不代表没有副作用，也不证明任务完成；请利用已有结果判断是否确需再次执行，"
        "或改用能提供新信息的步骤。本提醒不阻止工具、不要求用户审批。",
        {"run_id": facts.run_id, "tool_name": facts.tool_name, "args_hash": facts.args_hash,
         "result_hash": facts.result_hash, "count": count, "threshold": threshold},
    )


def failure_class_of_result(result: object) -> str:
    """Stable failure class of one tool result (code > category > output hash).

    The same class re-occurring across different argument hashes is the
    structural signature of a model repeating the same mistake (real-machine
    evidence: urllib3 replica, 60+ consecutive send_message failures with
    TOOL_PARAMETER_TYPE_INVALID — args changed every attempt, so the
    (tool, args_hash) identity never accumulated).
    """
    code = str(getattr(result, "error_code", "") or "").strip()
    if code:
        return f"code:{code}"
    category = str(getattr(result, "error_category", "") or "").strip()
    if category:
        return f"category:{category}"
    return f"output:{result_hash_for_guardrail(getattr(result, 'output', None))}"


def consecutive_same_failure_count(
    records: tuple[dict[str, object], ...],
    tool_name: str,
    failure_class: str,
) -> int:
    """Consecutive same-tool same-class failures, ignoring other tools' events.

    Same tool + same class accumulates; same tool + a different class or a
    success clears the current run, so only the trailing same-class segment
    counts. Events of other tools do not participate (a model alternating
    between a broken call and a working one must still be caught, which the
    strict per-record reset of _count_repeat_failures misses). Guardrail's own
    block records (TOOL_GUARDRAIL_*_BLOCKED) are not model behavior: they
    neither accumulate nor clear the segment.
    """
    count = 0
    for record in records:
        if str(record.get("tool_name") or "") != tool_name:
            continue
        current_class = str(record.get("failure_class") or "")
        if current_class.startswith("code:TOOL_GUARDRAIL"):
            continue
        if record.get("failed") is not True:
            count = 0
            continue
        count = count + 1 if current_class == failure_class else 0
    return count


def args_hash_for_guardrail(tool_name: str, args: object) -> str:
    try:
        canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = str(args)
    raw = f"{tool_name}:{canonical}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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


# LLM: 真实成功的非只读调用仍退休此前只读无进展段；保留原记录供成功观察，不扩大 gate 覆盖范围。
# 函数用途: 倒查同一只读结果次数，遇到本地执行进展时停止计数。
def _count_no_progress(records: list[dict[str, object]], tool_name: str, args_hash: str, current_hash: str) -> int:
    count = 0
    for r in reversed(records):
        if r.get("failed") is False and r.get("is_readonly") is False:
            break
        if str(r.get("tool_name") or "") != tool_name or str(r.get("args_hash") or "") != args_hash:
            continue
        if r.get("failed") is True:
            break
        if str(r.get("result_hash") or "") != current_hash:
            break
        count += 1
    return count


def _threshold(cfg: ToolGuardrailConfig, repeat_kind: str = "failure") -> int:
    return _no_progress_threshold(cfg) if repeat_kind == "no_progress" else _repeat_threshold(cfg)


def _repeat_threshold(cfg: ToolGuardrailConfig) -> int:
    try:
        return max(0, int(cfg.repeat_fail_threshold))
    except (TypeError, ValueError):
        return 10


def _no_progress_threshold(cfg: ToolGuardrailConfig) -> int:
    try:
        return max(0, int(cfg.readonly_no_progress_threshold))
    except (TypeError, ValueError):
        return 3


def _should_hint(cfg: ToolGuardrailConfig, count: int, repeat_kind: str = "failure") -> bool:
    threshold = _threshold(cfg, repeat_kind)
    if threshold == 0:
        return count in (50, 100)
    return count in (threshold, threshold * 2)


def _should_action_block(cfg: ToolGuardrailConfig, count: int, repeat_kind: str = "failure") -> bool:
    threshold = _threshold(cfg, repeat_kind)
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
        "repeat_fail_threshold": _repeat_threshold(cfg),
        "readonly_no_progress_threshold": _no_progress_threshold(cfg),
        "terminal_block_enabled": cfg.terminal_block_enabled,
    }


def _action_block_evidence(evidence: dict[str, Any], cfg: ToolGuardrailConfig) -> dict[str, Any]:
    """Attach explicit task-level control separately from the recovery action."""
    payload = dict(evidence)
    payload["action_blocked"] = True
    if cfg.terminal_block_enabled:
        payload["block_task"] = True
    return payload


__all__ = [
    "ToolGuardrailConfig",
    "ToolGuardrailFacts",
    "args_hash_for_guardrail",
    "evaluate_tool_guardrail_gate",
    "record_tool_guardrail_result",
    "repeated_success_observation",
    "result_hash_for_guardrail",
]
