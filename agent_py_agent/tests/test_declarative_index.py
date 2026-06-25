"""role_template / workflow 自动索引测试:放进 home/shared/ 对应目录就被 capability 发现。

复用现有加载器(load_role_template_store / load_workflow_templates),把 builtin + 用户自定义
模板写进 shared/indexes/<kind>.jsonl。
"""
from __future__ import annotations

import json

from agent_py_agent.agent.capability.declarative_index import (
    sync_role_template_index,
    sync_workflow_index,
)


def test_builtin_role_templates_indexed(tmp_path):
    index = tmp_path / "role_templates.jsonl"
    count = sync_role_template_index(tmp_path / "none", index)
    assert count >= 6
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(r["kind"] == "role_template" for r in rows)
    assert all(r["source"] == "builtin" for r in rows)
    assert all(r["path"].endswith((".json", ".yaml", ".yml")) for r in rows)


def test_builtin_workflows_indexed(tmp_path):
    index = tmp_path / "workflows.jsonl"
    count = sync_workflow_index(tmp_path / "none", index)
    assert count >= 3
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(r["kind"] == "workflow" for r in rows)
    assert "code_feature_split" in {r["id"] for r in rows}


def test_custom_workflow_auto_indexed(tmp_path):
    """用户放进 shared/workflows 的自定义 workflow 自动进索引,builtin 仍在。"""
    user_dir = tmp_path / "workflows"
    user_dir.mkdir()
    (user_dir / "my_flow.json").write_text(
        json.dumps(
            {
                "id": "my_flow",
                "name": "我的流程",
                "solves": ["我的问题"],
                "fit_for": ["我的场景"],
                "phases": [{"id": "p1", "kind": "worker", "task": "干活", "acceptance": []}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    index = tmp_path / "workflows.jsonl"
    sync_workflow_index(user_dir, index)
    ids = {json.loads(line)["id"] for line in index.read_text(encoding="utf-8").splitlines() if line.strip()}
    assert "my_flow" in ids  # 用户自定义纳入
    assert "code_feature_split" in ids  # builtin 仍在


def test_nonexistent_dir_tolerated(tmp_path):
    """用户没自定义(目录不存在)时只出 builtin,不报错。"""
    assert sync_role_template_index(tmp_path / "ghost", tmp_path / "r.jsonl") >= 6
    assert sync_workflow_index(tmp_path / "ghost", tmp_path / "w.jsonl") >= 3


def test_ensure_home_writes_role_and_workflow_indexes(tmp_path):
    """端到端:ensure_my_agent_home 后 role_templates.jsonl + workflows.jsonl 非空。"""
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path / "home")
    role_lines = [
        line
        for line in paths.shared_indexes_role_templates_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    wf_lines = [
        line for line in paths.shared_indexes_workflows_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(role_lines) >= 6
    assert len(wf_lines) >= 3
