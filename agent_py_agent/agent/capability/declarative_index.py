"""把 role_template / workflow 扫进 capability 索引,让它们像 skill 一样"放进 home/shared/
对应目录就自动被发现"。

与 skill 不同:role_template/workflow 各有专门加载器(load_role_template_store /
load_workflow_templates),能从源码 builtin + 用户 shared 目录加载并懂各自格式(JSON/YAML)。
这里复用加载器提取元数据、不重写解析,把加载到的模板(builtin + 用户自定义)写进
shared/indexes/<kind>.jsonl,使 capability_resolver 能发现两者。因此不必把 builtin 镜像到
home(加载器已直接读源码),只同步索引。每次 ensure 重建索引(模板数量少、加载+写很便宜),
用户新增后下次启动即生效。
"""
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


def sync_workflow_index(shared_workflows_dir: Path, index_jsonl: Path) -> int:
    """加载 builtin + 用户自定义(shared/workflows)的 workflow,写 workflows.jsonl。"""
    from ..subagent_workflows import load_workflow_templates

    templates, _issues = load_workflow_templates(shared_workflows_dir)
    records = [
        {
            "id": tpl.id,
            "name": tpl.name or tpl.id,
            "kind": "workflow",
            "source": tpl.source or "builtin",
            "path": tpl.source_path,
            "description": "；".join(tpl.solves) if tpl.solves else tpl.name,
        }
        for tpl in templates
    ]
    return _write_index(index_jsonl, records)
