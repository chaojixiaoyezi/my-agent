"""role_template 自动索引测试:放进 home/shared/ 后被 capability 发现。"""
from __future__ import annotations

import json

from agent_py_agent.agent.capability.declarative_index import (
    sync_role_template_index,
)


def test_builtin_role_templates_indexed(tmp_path):
    index = tmp_path / "role_templates.jsonl"
    count = sync_role_template_index(tmp_path / "none", index)
    assert count >= 6
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(r["kind"] == "role_template" for r in rows)
    assert all(r["source"] == "builtin" for r in rows)
    assert all(r["path"].endswith((".json", ".yaml", ".yml")) for r in rows)


def test_nonexistent_dir_tolerated(tmp_path):
    """用户没自定义(目录不存在)时只出 builtin,不报错。"""
    assert sync_role_template_index(tmp_path / "ghost", tmp_path / "r.jsonl") >= 6


def test_ensure_home_writes_role_template_index(tmp_path):
    """端到端:ensure_my_agent_home 后 role_templates.jsonl 非空。"""
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path / "home")
    role_lines = [
        line
        for line in paths.shared_indexes_role_templates_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(role_lines) >= 6
