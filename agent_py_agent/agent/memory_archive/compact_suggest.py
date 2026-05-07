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
    owner_type: str = "main_agent"
    owner_id: str = ""


# LLM: build_memory_compact_suggestion only suggests next commands and never writes compact artifacts.
# 函数用途: 生成半自动 compact 提示，包含风险状态、原因、命令建议和需要人工确认的边界。
def build_memory_compact_suggestion(root: str | Path, options: MemoryCompactSuggestOptions) -> dict[str, Any]:
    workspace = Path(root)
    plan = build_memory_compact_plan(workspace, options.plan_options)
    ratio = _ratio(options.current_tokens, options.max_context_tokens)
    status = _suggestion_status(ratio)
    should_prompt = status in {"suggest_compact", "artifact_guard", "stop_required"}
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
        "token_budget": _token_budget_payload(options, ratio),
        "scope": plan["scope"],
        "candidate_counts": _candidate_counts(plan),
        "risks": list(plan["risks"]),
        "recommended_commands": _recommended_commands(plan, should_prompt),
        "message": _message(status, ratio),
        "reserved": runtime_memory_reserved_fields(COMPACT_SUGGESTION_SCHEMA),
    }


# LLM: _suggestion_status maps thresholds from the compact plan into deterministic statuses.
# 函数用途: 根据上下文使用率给出 ok/checkpoint/suggest/artifact guard/stop 五档状态。
def _suggestion_status(ratio: float) -> str:
    if ratio >= 0.95:
        return "stop_required"
    if ratio >= 0.85:
        return "artifact_guard"
    if ratio >= 0.70:
        return "suggest_compact"
    if ratio >= 0.50:
        return "checkpoint_recommended"
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
def _message(status: str, ratio: float) -> str:
    percent = f"{ratio:.0%}"
    messages = {
        "ok": f"context usage is {percent}; no compact prompt needed.",
        "checkpoint_recommended": f"context usage is {percent}; checkpoint is recommended, compact is not required yet.",
        "suggest_compact": f"context usage is {percent}; suggest running memory-compact --dry-run before continuing long work.",
        "artifact_guard": f"context usage is {percent}; avoid inline large outputs and ask before compact apply.",
        "stop_required": f"context usage is {percent}; stop growing context and compact/resume before more work.",
    }
    return messages[status]


# LLM: _token_budget_payload records enough budget evidence for CLI and future automation guards.
# 函数用途: 结构化记录当前 token、窗口、比例和阈值，方便审计 compact 提示原因。
def _token_budget_payload(options: MemoryCompactSuggestOptions, ratio: float) -> dict[str, Any]:
    return {
        "current_tokens": max(0, int(options.current_tokens)),
        "max_context_tokens": max(0, int(options.max_context_tokens)),
        "ratio": ratio,
        "thresholds": {
            "checkpoint": 0.50,
            "suggest_compact": 0.70,
            "artifact_guard": 0.85,
            "stop_required": 0.95,
        },
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


# LLM: _ratio handles unset context windows conservatively without raising.
# 函数用途: 计算当前 token 占窗口比例；窗口未设置时返回 0 代表不提示。
def _ratio(current_tokens: int, max_context_tokens: int) -> float:
    if max_context_tokens <= 0:
        return 0.0
    return max(0.0, current_tokens / max_context_tokens)


__all__ = ["MemoryCompactSuggestOptions", "build_memory_compact_suggestion"]
