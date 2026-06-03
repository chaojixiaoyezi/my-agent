
from __future__ import annotations

"""CLI output formatting adapters for archive query results.

新手说明:
这个文件放的是查询结果的 CLI 渲染逻辑。
它们把查询响应格式化成适合命令行输出或 JSON 显示的格式。
"""

import json
from typing import Any


def format_archive_record(record: dict[str, Any]) -> str:

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
