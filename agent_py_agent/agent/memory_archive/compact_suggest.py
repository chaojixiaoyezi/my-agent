# LLM: Compact suggestion is a read-only semi-automatic prompt, never an apply trigger.
# 模块用途: 根据 token 预算和 compact plan 生成半自动 compact 建议，提示用户确认后再手动 apply/resume。

from __future__ import annotations

"""semi-automatic compact suggestion helpers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact import MemoryCompactPlanOptions, build_memory_compact_plan
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_SUGGESTION_SCHEMA = RuntimeMemorySchemaOptions("compact_suggestion")


# LLM: MemoryCompactSuggestOptions bundles context-budget inputs for main-agent and future subagent sessions.
# 类用途: 描述 compact 建议所需的 token、scope 和 owner 字段；后续子代理自动会话压缩可复用 owner 口子。
@dataclass(frozen=True)
class MemoryCompactSuggestOptions:
    current_tokens: int
    max_context_tokens: int
    plan_options: MemoryCompactPlanOptions
    trigger_percent: int = 90
    owner_type: str = "main_agent"
    owner_id: str = ""
    # 参数说明: trigger 字段只记录触发来源；正常阈值和兜底救场仍走同一个 compact suggestion。
    trigger_reason: str = "normal_threshold"
    trigger_source: str = "token_budget"
    force_trigger: bool = False


# LLM: build_memory_compact_suggestion only suggests next commands and never writes compact artifacts.
# 函数用途: 生成半自动 compact 提示，包含风险状态、原因、命令建议和需要人工确认的边界。
def build_memory_compact_suggestion(root: str | Path, options: MemoryCompactSuggestOptions) -> dict[str, Any]:
    workspace = Path(root)
    plan = build_memory_compact_plan(workspace, options.plan_options)
    ratio = _ratio(options.current_tokens, options.max_context_tokens)
    trigger_ratio = _trigger_ratio(options.trigger_percent)
    status = _suggestion_status(ratio, trigger_ratio=trigger_ratio, force_trigger=options.force_trigger)
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
        "reserved": runtime_memory_reserved_fields(COMPACT_SUGGESTION_SCHEMA),
    }


# LLM: _suggestion_status maps one configured compact trigger into deterministic statuses.
# 函数用途: 根据用户配置的单一百分比判断是否进入 compact；provider 兜底触发走 forced_compact。
def _suggestion_status(ratio: float, *, trigger_ratio: float, force_trigger: bool = False) -> str:
    if force_trigger:
        return "forced_compact"
    if ratio >= trigger_ratio:
        return "ready_to_compact"
    return "ok"


# LLM: _recommended_commands keeps semi-auto compact explicit and user-confirmed.
# 函数用途: 返回用户可以复制执行的 dry-run/apply/resume 命令；不会在服务内自动执行。
def _recommended_commands(plan: dict[str, Any], should_prompt: bool) -> list[str]:
    if not should_prompt:
        return []
    scope = _scope_flags(plan["scope"])
    return [
        f"my-agent memory-compact{scope} --dry-run",
        f"my-agent memory-compact{scope} --apply",
        "my-agent memory-resume --from-compact <apply_id> --context-only",
    ]


# LLM: _scope_flags preserves the current compact scope in suggested commands.
# 函数用途: 把 session/request/run/task/date 范围转成 CLI flag 字符串。
def _scope_flags(scope: dict[str, Any]) -> str:
    flags: list[str] = []
    for key in ("session_id", "request_id", "run_id", "task_id", "date", "since", "until"):
        if value := scope.get(key):
            flags.append(f"--{key.replace('_', '-')} {value}")
    return " " + " ".join(flags) if flags else ""


# LLM: _message explains compact status in a short user-facing sentence.
# 函数用途: 根据状态生成半自动提示文案，明确不会自动 apply。
def _message(status: str, ratio: float, trigger_ratio: float) -> str:
    percent = f"{ratio:.0%}"
    trigger_percent = f"{trigger_ratio:.0%}"
    messages = {
        "ok": f"context usage is {percent}; auto compact trigger is {trigger_percent}.",
        "ready_to_compact": f"context usage is {percent}; reached auto compact trigger {trigger_percent}.",
        "forced_compact": f"context usage is {percent}; provider reported context pressure, compact/resume now.",
    }
    return messages[status]


# LLM: _token_budget_payload records enough budget evidence for CLI and future automation guards.
# 函数用途: 结构化记录当前 token、窗口、比例和阈值，方便审计 compact 提示原因。
def _token_budget_payload(options: MemoryCompactSuggestOptions, ratio: float) -> dict[str, Any]:
    return {
        "current_tokens": max(0, int(options.current_tokens)),
        "max_context_tokens": max(0, int(options.max_context_tokens)),
        "ratio": ratio,
        "auto_trigger_percent": _trigger_percent(options.trigger_percent),
        "auto_trigger_ratio": _trigger_ratio(options.trigger_percent),
    }


# LLM: _candidate_counts keeps the prompt compact and avoids copying raw compact plan lists.
# 函数用途: 汇总 compact dry-run 候选数，供提示解释“为什么值得 compact”。
def _candidate_counts(plan: dict[str, Any]) -> dict[str, int]:
    return {
        "archive_records": int(plan["archive"]["record_count"]),
        "snapshot_files": int(plan["snapshots"]["file_count"]),
        "token_ledgers": int(plan["tokens"]["ledger_count"]),
    }


# LLM: _owner_payload reserves ownership metadata for future subagent session compaction.
# 函数用途: 标记 compact 建议属于主代理还是未来某个子代理 run/session。
def _owner_payload(options: MemoryCompactSuggestOptions) -> dict[str, str]:
    return {"owner_type": options.owner_type, "owner_id": options.owner_id}


# LLM: _trigger_payload records why compact was suggested without changing the compact execution path.
# 函数用途: 输出 normal/provider-overflow 等触发来源，方便恢复和排查同链路兜底。
def _trigger_payload(options: MemoryCompactSuggestOptions) -> dict[str, Any]:
    return {
        "reason": _clean_token(options.trigger_reason, fallback="normal_threshold"),
        "source": _clean_token(options.trigger_source, fallback="token_budget"),
        "forced": bool(options.force_trigger),
    }


# LLM: _clean_token bounds trigger metadata before it is written to compact reports.
# 函数用途: 清理 compact trigger 的 reason/source 字段，空值使用安全兜底。
def _clean_token(value: object, *, fallback: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned[:120] if cleaned else fallback


# LLM: _ratio handles unset context windows conservatively without raising.
# 函数用途: 计算当前 token 占窗口比例；窗口未设置时返回 0 代表不提示。
def _ratio(current_tokens: int, max_context_tokens: int) -> float:
    if max_context_tokens <= 0:
        return 0.0
    return max(0.0, current_tokens / max_context_tokens)


# LLM: _trigger_percent normalizes user compact thresholds to the supported 50-100 range.
# 函数用途: 解析自动 compact 触发百分比；0 表示只在满窗/兜底时压缩，低于 50 抬到 50。
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


# LLM: _trigger_ratio turns the normalized percent into a ratio for compact budget comparisons.
# 函数用途: 给 compact_suggest 使用 0.5-1.0 的比例值，保持百分比解析只有一个入口。
def _trigger_ratio(value: object) -> float:
    return _trigger_percent(value) / 100.0


__all__ = ["MemoryCompactSuggestOptions", "build_memory_compact_suggestion"]
