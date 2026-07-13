
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


@dataclass(frozen=True)
class MemoryCompactSuggestOptions:
    current_tokens: int
    max_context_tokens: int
    plan_options: MemoryCompactPlanOptions
    # 独立 suggestion 调用也必须复用正式 90% 默认，避免旁路重新漂回旧值。
    trigger_percent: int = 90
    owner_type: str = "main_agent"
    owner_id: str = ""
    #  trigger 字段只记录触发来源；正常阈值和强制触发仍走同一个 compact suggestion。
    trigger_reason: str = "normal_threshold"
    trigger_source: str = "token_budget"
    force_trigger: bool = False


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
    }


def _suggestion_status(ratio: float, *, trigger_ratio: float, force_trigger: bool = False) -> str:
    if force_trigger:
        return "forced_compact"
    if ratio >= trigger_ratio:
        return "ready_to_compact"
    return "ok"


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


def _trigger_percent(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 50
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
