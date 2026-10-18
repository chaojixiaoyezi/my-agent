# LLM: 共享role-template索引提交守原globalguard；读取模板不新增写门，原记录格式和默认模式保持。
# 模块用途: 从原模板Store生成管理员共享发现索引，受限actor不得发布。
"""把共享 role template 扫进管理员可查看的 capability 索引。"""
from __future__ import annotations

import json
from pathlib import Path


# LLM: 此叶子只发布共享role-template派生索引，原load_role_template_store等读取不加门；不据路径猜权限。
# 函数用途: 建目录写共享索引之前拒绝受限写者，默认写法与返回数量不变。
def _write_index(index_jsonl: Path, records: list[dict[str, object]]) -> int:
    from ..common.global_write_guard import require_global_writer

    require_global_writer("shared_declarative_index")
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
