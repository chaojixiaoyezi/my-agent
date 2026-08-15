"""把共享 role template 扫进管理员可查看的 capability 索引。"""
from __future__ import annotations

import json
from pathlib import Path


def _write_index(index_jsonl: Path, records: list[dict[str, object]]) -> int:
    index_jsonl.parent.mkdir(parents=True, exist_ok=True)
    index_jsonl.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return len(records)


def sync_role_template_index(shared_role_templates_dir: Path, index_jsonl: Path) -> int:
    """加载 builtin + 用户自定义(shared/role_templates)的 role_template,写 role_templates.jsonl。"""
    from ..subagents.role_templates import load_role_template_store

    store = load_role_template_store(shared_role_templates_dir)
    records = [
        {
            "id": tpl.id,
            "name": tpl.name or tpl.id,
            "kind": "role_template",
            "source": tpl.source,
            "path": tpl.source_path,
            "description": tpl.summary_zh or tpl.summary or tpl.name_zh or tpl.name,
        }
        for tpl in store.templates.values()
    ]
    return _write_index(index_jsonl, records)
