# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""CLI output formatting adapters for archive query results.

新手说明:
这个文件放的是查询结果的 CLI 渲染逻辑。
它们把查询响应格式化成适合命令行输出或 JSON 显示的格式。
"""

import json
from typing import Any


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 format_archive_record 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 format archive record 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 format_archive_records_table 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 format archive records table 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 format_query_response_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 format query response json 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 render_resume_guidance 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 render resume guidance 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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
