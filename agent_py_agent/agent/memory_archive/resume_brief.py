# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""builds concise human/LLM-readable recovery briefs for memory-resume.

新手说明:
这个文件只负责把归档线索、LocalStore 线索和任务事实源压成一份'恢复简报'。
它不读取文件、不修改状态，只帮人和后续自动化快速知道下一步该看哪里。
"""

import json
from dataclasses import dataclass
from typing import Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 RecoveryBriefContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 RecoveryBriefContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class RecoveryBriefContext:
    """Bundle for _context_block keyword parameters."""

    latest_user_intents: list[str]
    latest_assistant_actions: list[str]
    related_ids: dict[str, list[str]]
    likely_task_statuses: list[dict[str, str]]
    recommended_read_paths: list[str]
    next_actions: list[str]
    authority_note: str


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 ResumeBriefParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ResumeBriefParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ResumeBriefParams:
    # LLM: resume brief callers pass optional recommendations as one bundle.
    recommended_read_paths: list[str] | None = None
    next_actions: list[str] | None = None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 build_resume_brief 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build resume brief 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def build_resume_brief(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
    *,
    params: ResumeBriefParams | None = None,
    recommended_read_paths: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Synthesize a compact recovery brief from existing resume evidence."""

    values = params or ResumeBriefParams(recommended_read_paths, next_actions)
    recommended_read_paths = list(values.recommended_read_paths or [])
    next_actions = list(values.next_actions or [])
    latest_user_intents = _latest_user_intents(archive_matches)
    latest_assistant_actions = _latest_assistant_actions(archive_matches)
    related_ids = _related_ids(archive_matches, local_hits, task_payloads)
    likely_task_statuses = _likely_task_statuses(task_payloads)
    authority_note = "archive/local matches are recovery clues; task files are the authority for current state."
    ctx = RecoveryBriefContext(
        latest_user_intents=latest_user_intents,
        latest_assistant_actions=latest_assistant_actions,
        related_ids=related_ids,
        likely_task_statuses=likely_task_statuses,
        recommended_read_paths=recommended_read_paths,
        next_actions=next_actions,
        authority_note=authority_note,
    )
    return {
        "latest_user_intent": latest_user_intents[0] if latest_user_intents else "",
        "latest_assistant_action": latest_assistant_actions[0] if latest_assistant_actions else "",
        "latest_user_intents": latest_user_intents[:5],
        "latest_assistant_actions": latest_assistant_actions[:5],
        "related_ids": related_ids,
        "likely_task_statuses": likely_task_statuses,
        "recommended_read_paths": recommended_read_paths[:20],
        "next_actions": next_actions,
        "authority_note": authority_note,
        "summary": _summary(ctx),
        "context_block": _context_block(ctx),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summary 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _summary(ctx: RecoveryBriefContext) -> str:
    summary_lines = [
        _summary_line("latest_user_intent", ctx.latest_user_intents[:1]),
        _summary_line("latest_assistant_action", ctx.latest_assistant_actions[:1]),
        _summary_line("related_run_ids", ctx.related_ids["run_ids"][:3]),
        _summary_line("likely_task_status", _task_status_summaries(ctx.likely_task_statuses[:3])),
    ]
    return "\n".join(line for line in summary_lines if line)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _task_status_summaries 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 task status summaries 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _task_status_summaries(statuses: list[dict[str, str]]) -> list[str]:
    return [f"{item['run_id']} {item['status']}/{item['verification_status']}" for item in statuses]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _latest_user_intents 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 latest user intents 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _latest_user_intents(archive_matches: list[dict[str, Any]]) -> list[str]:

    values: list[str] = []
    for record in archive_matches:
        payload = record.get("payload", {}) if isinstance(record.get("payload"), dict) else {}
        values.extend(_string_list(payload.get("user_intents")))
        if record.get("speaker") == "user":
            values.append(str(record.get("content_preview", "") or ""))
    return _dedupe(values)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _latest_assistant_actions 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 latest assistant actions 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _latest_assistant_actions(archive_matches: list[dict[str, Any]]) -> list[str]:

    values: list[str] = []
    for record in archive_matches:
        payload = record.get("payload", {}) if isinstance(record.get("payload"), dict) else {}
        values.extend(_string_list(payload.get("assistant_actions")))
        if record.get("speaker") == "assistant":
            values.append(str(record.get("content_preview", "") or ""))
    return _dedupe(values)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _related_ids 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 related ids 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _related_ids(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
) -> dict[str, list[str]]:

    ids = {
        "session_ids": [],
        "request_ids": [],
        "run_ids": [],
        "task_ids": [],
    }
    for record in archive_matches:
        _append(ids["session_ids"], record.get("session_id"))
        _append(ids["request_ids"], record.get("request_id"))
        _append(ids["run_ids"], record.get("run_id"))
        _append(ids["task_ids"], record.get("task_id"))
    for hit in local_hits:
        source_id = str(hit.get("source_id", "") or "")
        if source_id.startswith("gwreq-"):
            _append(ids["request_ids"], source_id)
        if source_id.startswith("subagent-"):
            _append(ids["run_ids"], source_id)
            _append(ids["task_ids"], source_id)
        metadata = hit.get("metadata", {}) if isinstance(hit.get("metadata"), dict) else {}
        _append(ids["request_ids"], metadata.get("request_id"))
        _append(ids["run_ids"], metadata.get("run_id"))
        _append(ids["task_ids"], metadata.get("task_id"))
    for task in task_payloads:
        _append(ids["run_ids"], task.get("run_id"))
        _append(ids["task_ids"], task.get("run_id"))
    return ids


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _likely_task_statuses 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 likely task statuses 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _likely_task_statuses(task_payloads: list[dict[str, Any]]) -> list[dict[str, str]]:

    statuses: list[dict[str, str]] = []
    for task in task_payloads:
        statuses.append(
            {
                "run_id": str(task.get("run_id", "") or ""),
                "exists": str(bool(task.get("exists", False))).lower(),
                "status": str(task.get("status", "missing") or "missing"),
                "verification_status": str(task.get("verification_status", "unknown") or "unknown"),
                "goal": str(task.get("goal", "") or ""),
                "task_dir": str(task.get("task_dir", "") or ""),
            }
        )
    return statuses


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _context_block 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 context block 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _context_block(ctx: RecoveryBriefContext) -> str:

    lines = ["# Recovery Brief", "", f"- authority: {ctx.authority_note}"]
    lines.append(f"- latest_user_intent: {ctx.latest_user_intents[0] if ctx.latest_user_intents else 'unknown'}")
    lines.append(
        f"- latest_assistant_action: {ctx.latest_assistant_actions[0] if ctx.latest_assistant_actions else 'unknown'}"
    )
    lines.append("- related_ids: " + json.dumps(ctx.related_ids, ensure_ascii=False, sort_keys=True))
    if ctx.likely_task_statuses:
        lines.append("- likely_task_statuses:")
        for item in ctx.likely_task_statuses[:5]:
            lines.append(
                f"  - {item['run_id']} {item['status']}/{item['verification_status']} :: {item['goal']}"
            )
    else:
        lines.append("- likely_task_statuses: none")
    lines.append("- must_read:")
    for path in ctx.recommended_read_paths[:10] or ["none"]:
        lines.append(f"  - {path}")
    lines.append("- next_actions:")
    for action in ctx.next_actions[:5]:
        lines.append(f"  - {action}")
    return "\n".join(lines)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _summary_line 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summary line 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _summary_line(label: str, values: list[str]) -> str:

    return f"{label}: {values[0]}" if values else ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _string_list 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 string list 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _string_list(value: object) -> list[str]:

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _dedupe 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 dedupe 涉及的字段，让后续匹配和存储使用同一形态。
def _dedupe(values: list[str]) -> list[str]:

    items: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in items:
            items.append(text)
    return items


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 append 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _append(items: list[str], value: object) -> None:

    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)
