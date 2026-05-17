# LLM: Compact apply rendering keeps human/model markdown output separate from apply orchestration.
# 模块用途: 渲染 memory-compact --apply 生成的恢复上下文 Markdown，不读取或修改归档文件。

from __future__ import annotations

import json
from typing import Any


# LLM: render_compact_context_markdown produces the compact entrypoint shown to people and future resumes.
# 函数用途: 把 apply metadata 和 dry-run plan 渲染成短 Markdown，提示先读自检再核对原始事实源。
def render_compact_context_markdown(payload: dict[str, Any], plan: dict[str, Any]) -> str:
    source = payload["source_plan"]
    return (
        "# Memory Compact Context\n\n"
        f"- event_id: {payload['event_id']}\n"
        f"- compact_status: {payload['compact_status']}\n"
        f"- workspace_root: {payload['workspace_root']}\n"
        f"- archive_records: {source['archive_record_count']}\n"
        f"- snapshots: {source['snapshot_file_count']}\n"
        f"- token_ledgers: {source['token_ledger_count']}\n"
        f"- main_context_bundle: {payload.get('refs', {}).get('main_context_bundle', '') or '-'}\n"
        f"- estimated_compactable_bytes: {source['estimated_compactable_bytes']}\n"
        "- content_preserved: true\n\n"
        "## Scope\n\n"
        f"{json.dumps(plan['scope'], ensure_ascii=False, sort_keys=True)}\n\n"
        "## Risks\n\n"
        f"{_bullet_lines(source['risks'])}\n\n"
        "## Recovery Rule\n\n"
        "Use this context as the compact entrypoint, then verify against raw/hook archives, "
        "compression snapshots, token ledgers, and task/run workspaces before trusting a resumed answer.\n"
    )


# LLM: _bullet_lines keeps empty Markdown lists deterministic for tests and docs.
# 函数用途: 将字符串列表渲染成 Markdown bullet；空列表固定输出 none。
def _bullet_lines(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- none"


__all__ = ["render_compact_context_markdown"]
