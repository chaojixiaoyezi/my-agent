from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_subagent_create_run_records_inheritance_manifest(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(
        goal="上级代理任务",
        thought="提供能力、上下文和验收约束。",
        plan=["准备上下文"],
        role="coordinator",
        allowed_skills=["review", "debug"],
        allowed_tools=["read_file", "shell"],
        acceptance_checks=["必须经过上级验收"],
        context_packs=[{"id": "parent-pack", "summary": "父级上下文包"}],
    )

    child = manager.create_run(
        goal="子代理任务",
        thought="继承一部分能力，裁剪高风险工具。",
        plan=["执行子步骤"],
        role="worker",
        parent_id=parent.id,
        root_id=parent.root_id,
        depth=1,
        allowed_skills=["review", "child-only"],
        allowed_tools=["read_file"],
        acceptance_checks=["子任务需要附 evidence"],
        context_packs=[],
    )

    loaded = manager.load(child.id)
    manifest = loaded.inheritance_manifest
    manifest_json = json.loads(Path(loaded.inheritance_manifest_json).read_text(encoding="utf-8"))

    assert manifest.source_run_id == parent.id
    assert manifest.target_run_id == child.id
    assert manifest.root_task_id == parent.root_id
    assert manifest.inherited["allowed_skills"] == ["review"]
    assert manifest.inherited["allowed_tools"] == ["read_file"]
    assert manifest.dropped["allowed_skills"] == ["debug"]
    assert manifest.dropped["allowed_tools"] == ["shell"]
    assert manifest.dropped["context_packs"][0]["id"] == "parent-pack"
    assert manifest.overridden["allowed_skills"]["added"] == ["child-only"]
    assert manifest.overridden["acceptance_checks"]["parent"][0] == "必须经过上级验收"
    assert any("协调子代理" in item for item in manifest.overridden["acceptance_checks"]["parent"])
    assert manifest.overridden["acceptance_checks"]["child"][0] == "子任务需要附 evidence"
    assert any("执行子代理" in item for item in manifest.overridden["acceptance_checks"]["child"])
    assert manifest_json["target_run_id"] == child.id
    assert manifest_json["policy"]["auto_expand_parent_context"] is False
