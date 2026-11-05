"""W4 Track 2：沿真实构建工具收文件，执行位只认声明。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.capability.learnpack_store import LearnpackStore
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.package_build_tool import PackageBuildTool
from agent_py_agent.tests.test_cpack_w4_import_synthesis import put
from agent_py_agent.tests.test_cpack_w4_pack_grammar_rollback import make_pack
from agent_py_agent.tests.test_package_build_tool import _agent


@pytest.mark.parametrize("executable", [False, True])
def test_auto_collection_marks_only_declared_executable_entry(tmp_path, monkeypatch, executable):
    agent = _agent(tmp_path, monkeypatch)
    _, declaration, contents = make_pack(executable=executable)
    declaration.pop("files")
    root = agent.home_paths.owner_home_dir / "fixture-source"
    for path, data in contents.items():
        put(root, path, data).chmod(0o755)  # 模式位不能额外授予执行权。
    put(root, "mcp/demo/data", "data").chmod(0o755)
    outcome = agent.tools.tools["package_build"].execute({"kind": "capability_pack", "source_dir": str(root),
        "declaration": declaration, "origin": "自己写的", "license": "MIT"})
    assert outcome.ok, outcome.output
    report = json.loads(outcome.output)
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    record = store.build(report["build"]["sha256"])
    manifest = inspect_plugin_package(store.build_path(record.sha256).read_bytes()).manifest
    assert manifest.has_mcp_runtime
    assert {f.path for f in manifest.files if f.executable} == ({"mcp/demo/server.py"} if executable else set())


def test_model_description_explains_single_mcp_runtime_and_preserves_language_tokens():
    text = PackageBuildTool.model_spec.description
    for token in ("mcp_servers", "root", "pinned_on_enable", "permissions.network", "npx", "uvx",
                  "summary", "capability.description", "capability.keywords", "和用户对话时用的语言",
                  "另一种语言", "中英文", "目录展示", "关键词推荐"):
        assert token in text
