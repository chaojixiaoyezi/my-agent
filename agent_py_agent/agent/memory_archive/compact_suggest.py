# LLM: compact 建议的状态只由结构化 token 事实决定：trigger_tokens>0 时按“当前 token ≥ 触发线”，否则按占窗口的百分比；
#   runtime 的 finalization 自动压缩在触发线被绝对上限封顶时才传 trigger_tokens，其余调用方不传，行为不变。
# 模块用途: 生成半自动 compact 建议（该不该压、范围与风险），供 finalization 自动压缩周期和手动建议复用。
from __future__ import annotations

"""semi-automatic compact suggestion helpers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact import MemoryCompactPlanOptions, build_memory_compact_plan
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

COMPACT_SUGGESTION_SCHEMA = RuntimeMemorySchemaOptions("compact_suggestion")


# LLM: trigger_tokens 为 0 时按 trigger_percent（规范到 50-100）判断；大于 0 时直接按 token 触发线判断，承接
#   runtime_compact_policy 的绝对上限（百分比最低 50%，表达不了 1M 窗口下的 30 万）。
# 类用途: 一次 compact 建议的输入。
@dataclass(frozen=True)
class MemoryCompactSuggestOptions:
    current_tokens: int
    max_context_tokens: int
    plan_options: MemoryCompactPlanOptions
    # 独立 suggestion 调用也必须复用正式 90% 默认，避免旁路重新漂回旧值。
    trigger_percent: int = 90
    # runtime_compact_policy 封顶后的 token 触发线；0 表示按 trigger_percent 判断。
    trigger_tokens: int = 0
    owner_type: str = "main_agent"
    owner_id: str = ""
    #  trigger 字段只记录触发来源；正常阈值和强制触发仍走同一个 compact suggestion。
    trigger_reason: str = "normal_threshold"
    trigger_source: str = "token_budget"
    force_trigger: bool = False


# LLM: 只读计划与 token 事实，不写任何文件；状态判定见 _suggestion_status。
# 函数用途: 生成一次 compact 建议载荷。
def build_memory_compact_suggestion(
    root: str | Path, options: MemoryCompactSuggestOptions
) -> dict[str, Any]:
    workspace = Path(root)
    plan = build_memory_compact_plan(workspace, options.plan_options)
    ratio = _ratio(options.current_tokens, options.max_context_tokens)
    trigger_ratio = _effective_trigger_ratio(options)
    status = _suggestion_status(options, ratio)
    should_prompt = options.force_trigger or status == "ready_to_compact"
    trigger = _trigger_payload(options)
    return {
        "version": COMPACT_SUGGESTION_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_SUGGESTION_SCHEMA),
        "ok": True,
        "event_type": "compact_suggestion",
        "status": status,
        "should_prompt": should_prompt,
        "requires_confirmation": should_prompt,
        "automatic_action": "none",
        "owner": _owner_payload(options),
        "trigger": trigger,
        "token_budget": _token_budget_payload(options, ratio),
        "scope": plan["scope"],
        "candidate_counts": _candidate_counts(plan),
        "risks": list(plan["risks"]),
        "recommended_commands": _recommended_commands(plan, should_prompt),
        "message": _message(status, ratio, trigger_ratio),
    }


# LLM: 强制触发优先；否则 trigger_tokens>0 时按 current_tokens ≥ trigger_tokens，不然按占窗口比例 ≥ 规范后的触发比例。
# 函数用途: 判断这次 compact 建议的状态（ok / ready_to_compact / forced_compact）。
def _suggestion_status(options: MemoryCompactSuggestOptions, ratio: float) -> str:
    if options.force_trigger:
        return "forced_compact"
    trigger_tokens = max(0, int(options.trigger_tokens or 0))
    if trigger_tokens:
        reached = int(options.current_tokens) >= trigger_tokens
    else:
        reached = ratio >= _trigger_ratio(options.trigger_percent)
    return "ready_to_compact" if reached else "ok"


def _recommended_commands(plan: dict[str, Any], should_prompt: bool) -> list[str]:
    if not should_prompt:
        return []
    scope = _scope_flags(plan["scope"])
    return [
        f"my-agent memory-compact{scope} --dry-run",
        f"my-agent memory-compact{scope} --apply",
        "my-agent memory-resume --from-compact <apply_id> --context-only",
    ]


def _scope_flags(scope: dict[str, Any]) -> str:
    flags: list[str] = []
    for key in ("session_id", "request_id", "run_id", "task_id", "date", "since", "until"):
        if value := scope.get(key):
            flags.append(f"--{key.replace('_', '-')} {value}")
    return " " + " ".join(flags) if flags else ""


# LLM: 只用于建议消息的文字：有 token 触发线时按它占窗口的比例描述（封顶后的真实触发点），否则按规范后的百分比；
#   是否触发由 _suggestion_status 直接比 token，不经过这里的浮点比例。
# 函数用途: 算出这次建议实际使用的触发比例，供消息展示。
def _effective_trigger_ratio(options: MemoryCompactSuggestOptions) -> float:
    trigger_tokens = max(0, int(options.trigger_tokens or 0))
    if trigger_tokens and options.max_context_tokens > 0:
        return trigger_tokens / options.max_context_tokens
    return _trigger_ratio(options.trigger_percent)


def _message(status: str, ratio: float, trigger_ratio: float) -> str:
    percent = f"{ratio:.0%}"
    trigger_percent = f"{trigger_ratio:.0%}"
    messages = {
        "ok": f"context usage is {percent}; auto compact trigger is {trigger_percent}.",
        "ready_to_compact": f"context usage is {percent}; reached auto compact trigger {trigger_percent}.",
        "forced_compact": f"context usage is {percent}; provider reported context pressure, compact/resume now.",
    }
    return messages[status]


def _token_budget_payload(options: MemoryCompactSuggestOptions, ratio: float) -> dict[str, Any]:
    return {
        "current_tokens": max(0, int(options.current_tokens)),
        "max_context_tokens": max(0, int(options.max_context_tokens)),
        "ratio": ratio,
        "auto_trigger_percent": _trigger_percent(options.trigger_percent),
        "auto_trigger_ratio": _trigger_ratio(options.trigger_percent),
    }


def _candidate_counts(plan: dict[str, Any]) -> dict[str, int]:
    return {
        "archive_records": int(plan["archive"]["record_count"]),
        "snapshot_files": int(plan["snapshots"]["file_count"]),
        "token_ledgers": int(plan["tokens"]["ledger_count"]),
    }


def _owner_payload(options: MemoryCompactSuggestOptions) -> dict[str, str]:
    return {"owner_type": options.owner_type, "owner_id": options.owner_id}


def _trigger_payload(options: MemoryCompactSuggestOptions) -> dict[str, Any]:
    return {
        "reason": _clean_token(options.trigger_reason, default="normal_threshold"),
        "source": _clean_token(options.trigger_source, default="token_budget"),
        "forced": bool(options.force_trigger),
    }


def _clean_token(value: object, *, default: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned[:120] if cleaned else default


def _ratio(current_tokens: int, max_context_tokens: int) -> float:
    if max_context_tokens <= 0:
        return 0.0
    return max(0.0, current_tokens / max_context_tokens)


# LLM: 无效值必须回到产品默认 90，合法 50-100 配置原样生效；不要在下游另设隐藏天花板。
# 函数用途: 把 compact 百分比配置规范到合法范围，缺失或格式错误时使用 90%。
def _trigger_percent(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 90
    if parsed <= 0:
        return 100
    if parsed < 50:
        return 50
    if parsed > 100:
        return 100
    return parsed


def _trigger_ratio(value: object) -> float:
    return _trigger_percent(value) / 100.0


__all__ = ["MemoryCompactSuggestOptions", "build_memory_compact_suggestion"]
