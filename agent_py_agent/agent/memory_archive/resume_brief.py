from __future__ import annotations

"""LLM: builds concise human/LLM-readable recovery briefs for memory-resume.

新手说明:
这个文件只负责把归档线索、LocalStore 线索和任务事实源压成一份'恢复简报'。
它不读取文件、不修改状态，只帮人和后续自动化快速知道下一步该看哪里。
"""

import json
from dataclasses import dataclass
from typing import Any


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


def build_resume_brief(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
    *,
    recommended_read_paths: list[str],
    next_actions: list[str],
) -> dict[str, Any]:
    """Synthesize a compact recovery brief from existing resume evidence."""

    latest_user_intents = _latest_user_intents(archive_matches)
    latest_assistant_actions = _latest_assistant_actions(archive_matches)
    related_ids = _related_ids(archive_matches, local_hits, task_payloads)
    likely_task_statuses = _likely_task_statuses(task_payloads)
    authority_note = "archive/local matches are recovery clues; task files are the authority for current state."
    summary_lines = [
        _summary_line("latest_user_intent", latest_user_intents[:1]),
        _summary_line("latest_assistant_action", latest_assistant_actions[:1]),
        _summary_line("related_run_ids", related_ids["run_ids"][:3]),
        _summary_line(
            "likely_task_status",
            [
                f"{item['run_id']} {item['status']}/{item['verification_status']}"
                for item in likely_task_statuses[:3]
            ],
        ),
    ]
    context_block = _context_block(
        RecoveryBriefContext(
            latest_user_intents=latest_user_intents,
            latest_assistant_actions=latest_assistant_actions,
            related_ids=related_ids,
            likely_task_statuses=likely_task_statuses,
            recommended_read_paths=recommended_read_paths,
            next_actions=next_actions,
            authority_note=authority_note,
        )
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
        "summary": "\n".join(line for line in summary_lines if line),
        "context_block": context_block,
    }


def _latest_user_intents(archive_matches: list[dict[str, Any]]) -> list[str]:
    """LLM: extract recent user-facing intents from hook snapshots and raw user messages.

    新手说明:
    优先读 hook 里的 `user_intents`，没有就退回 raw 里 user 的正文预览。

    参数说明:
    `archive_matches` 是标准化归档记录。

    返回说明:
    返回去重后的最近用户意图列表。
    """

    values: list[str] = []
    for record in archive_matches:
        payload = record.get("payload", {}) if isinstance(record.get("payload"), dict) else {}
        values.extend(_string_list(payload.get("user_intents")))
        if record.get("speaker") == "user":
            values.append(str(record.get("content_preview", "") or ""))
    return _dedupe(values)


def _latest_assistant_actions(archive_matches: list[dict[str, Any]]) -> list[str]:
    """LLM: extract recent assistant actions from hook snapshots and raw assistant responses.

    新手说明:
    优先读 hook 里的 `assistant_actions`，没有就退回 raw 里 assistant 的正文预览。

    参数说明:
    `archive_matches` 是标准化归档记录。

    返回说明:
    返回去重后的最近助手动作列表。
    """

    values: list[str] = []
    for record in archive_matches:
        payload = record.get("payload", {}) if isinstance(record.get("payload"), dict) else {}
        values.extend(_string_list(payload.get("assistant_actions")))
        if record.get("speaker") == "assistant":
            values.append(str(record.get("content_preview", "") or ""))
    return _dedupe(values)


def _related_ids(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """LLM: gather stable IDs that can be used for precise follow-up recovery.

    新手说明:
    恢复时最有用的是 session/request/run/task 这些 ID。
    这里把它们集中起来，用户下一轮可以直接 `memory-resume --run-id xxx`。

    参数说明:
    `archive_matches`、`local_hits`、`task_payloads` 是三类恢复线索。

    返回说明:
    返回 session/request/run/task ID 的分组字典。
    """

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


def _likely_task_statuses(task_payloads: list[dict[str, Any]]) -> list[dict[str, str]]:
    """LLM: summarize task fact-source states without claiming archive truth as final.

    新手说明:
    只有任务目录读出来的状态才放这里。
    如果任务不存在，就明确标记 missing，不用历史摘要猜。

    参数说明:
    `task_payloads` 是任务事实源摘要。

    返回说明:
    返回轻量任务状态列表。
    """

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


def _context_block(ctx: RecoveryBriefContext) -> str:
    """LLM: render a short text block suitable for future prompt injection or handoff.

    新手说明:
    这段还不会自动进 prompt，但格式先做稳定。
    后续如果要 `--apply-context`，就可以直接复用它。

    参数说明:
    所有参数都是已经抽取好的简报字段，包括意图、动作、ID、任务状态、推荐读物和下一步。

    返回说明:
    返回 Markdown 风格的恢复上下文块。
    """

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


def _summary_line(label: str, values: list[str]) -> str:
    """LLM: render one optional single-line summary item.

    新手说明:
    有值就输出 `名字: 内容`，没值就返回空字符串，避免简报里出现一堆 unknown 噪声。

    参数说明:
    `label` 是字段名；`values` 是候选值列表。

    返回说明:
    返回一行 summary 文本或空字符串。
    """

    return f"{label}: {values[0]}" if values else ""


def _string_list(value: object) -> list[str]:
    """LLM: coerce a payload field into a clean list of strings.

    新手说明:
    hook 里可能已经是列表，也可能是单个字符串。
    这里统一清洗掉空白，后面提取意图和动作时更稳。

    参数说明:
    `value` 是任意 payload 字段。

    返回说明:
    返回字符串列表。
    """

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _dedupe(values: list[str]) -> list[str]:
    """LLM: remove duplicate strings without changing evidence order.

    新手说明:
    同一个意图可能同时出现在 raw 和 hook 里。
    我们只保留第一次出现的版本，让简报短一点，也不打乱时间线。

    参数说明:
    `values` 是待去重字符串列表。

    返回说明:
    返回去重后的字符串列表。
    """

    items: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in items:
            items.append(text)
    return items


def _append(items: list[str], value: object) -> None:
    """LLM: append one non-empty string if it is not already present.

    新手说明:
    集中收集 session/request/run/task ID 时用它去重。
    这样下一步恢复可以拿到干净的 ID 列表。

    参数说明:
    `items` 是目标列表；`value` 是候选值。

    返回说明:
    不返回值；可能原地追加一项。
    """

    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)
