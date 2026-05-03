from __future__ import annotations

"""LLM: CLI output formatting adapters for archive query results.

新手说明:
这个文件放的是查询结果的 CLI 渲染逻辑。
它们把查询响应格式化成适合命令行输出或 JSON 显示的格式。
"""

import json
from typing import Any


def format_archive_record(record: dict[str, Any]) -> str:
    """LLM: format a single archive record as a readable CLI line.

    新手说明:
    把单条归档记录格式化成一行可读的 CLI 文本。

    参数说明:
    `record` 是标准化归档记录。

    返回说明:
    返回格式化的单行文本。
    """

    timestamp = record.get("created_at", "")
    kind = record.get("kind", "")
    speaker = record.get("speaker", "")
    action = record.get("action", "")
    status = record.get("status", "")
    preview = record.get("content_preview", "")[:80]

    parts = [timestamp[:19]]
    if kind:
        parts.append(f"[{kind}]")
    if speaker:
        parts.append(f"{speaker}:")
    if action:
        parts.append(action)
    if status:
        parts.append(f"({status})")
    if preview:
        parts.append(f"- {preview}")

    return " ".join(parts)


def format_archive_records_table(records: list[dict[str, Any]]) -> str:
    """LLM: format a list of archive records as a CLI-friendly table.

    新手说明:
    把多条归档记录格式化成表格文本，适合命令行展示。

    参数说明:
    `records` 是标准化归档记录列表。

    返回说明:
    返回格式化的表格文本。
    """

    if not records:
        return "No records found."

    lines: list[str] = []
    lines.append(f"{'Time':<20} {'Kind':<14} {'Speaker':<12} {'Action':<10} {'Status':<8} Preview")
    lines.append("-" * 100)

    for record in records[:50]:
        timestamp = record.get("created_at", "")[:19]
        kind = record.get("kind", "")[:12]
        speaker = record.get("speaker", "")[:10]
        action = record.get("action", "")[:8]
        status = record.get("status", "")[:6]
        preview = record.get("content_preview", "")[:40].replace("\n", " ")

        lines.append(f"{timestamp:<20} {kind:<14} {speaker:<12} {action:<10} {status:<8} {preview}")

    if len(records) > 50:
        lines.append(f"\n... and {len(records) - 50} more records")

    return "\n".join(lines)


def format_query_response_json(response: Any) -> str:
    """LLM: format query response as JSON string for API-like output.

    新手说明:
    把查询响应对象格式化成 JSON 字符串。

    参数说明:
    `response` 是查询响应对象。

    返回说明:
    返回 JSON 字符串。
    """

    return json.dumps(
        {
            "records": response.records,
            "total": response.total,
            "page": response.page,
            "page_size": response.page_size,
            "has_more": response.has_more,
            "pages": response.pages,
        },
        ensure_ascii=False,
        indent=2,
    )


def render_resume_guidance(guidance: dict[str, Any]) -> str:
    """LLM: render resume guidance as a human-readable CLI summary.

    新手说明:
    把恢复指导字典格式化成人类可读的 CLI 摘要。

    参数说明:
    `guidance` 是恢复指导字典。

    返回说明:
    返回格式化的恢复指导文本。
    """

    lines: list[str] = []

    counts = {
        "archive_matches": guidance.get("archive_match_count", 0),
        "local_hits": guidance.get("local_match_count", 0),
        "task_sources": guidance.get("task_fact_source_count", 0),
        "gateway_sources": guidance.get("gateway_fact_source_count", 0),
    }
    lines.append("=== Recovery Summary ===")
    for key, value in counts.items():
        lines.append(f"  {key}: {value}")

    recommended = guidance.get("recommended_read_paths", [])
    if recommended:
        lines.append("\n=== Recommended Read Paths ===")
        for path in recommended[:10]:
            lines.append(f"  - {path}")

    actions = guidance.get("next_actions", [])
    if actions:
        lines.append("\n=== Next Actions ===")
        for action in actions:
            lines.append(f"  - {action}")

    return "\n".join(lines)
