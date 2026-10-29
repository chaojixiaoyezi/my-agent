# LLM: 完整manifest只读现有快照，规范catalog不更新发现账或加载状态。
# 模块用途: 检查运行时工具目录的完整机器可读输出。
from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


# LLM: 完整manifest沿原注册快照生成，规范catalog不得丢失状态和来源。
# 函数用途: 验证目录合并后仍返回真实运行时工具清单。
def test_list_tools_returns_runtime_tool_manifest(tmp_path: Path) -> None:
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=50,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )

    result = execute_registry_test_call(registry, "tool_search", {"action": "catalog"})

    assert result.ok, result.output
    payload = json.loads(
        result.metadata["handler_details"]["tool_output_policy"]["live_prompt_output"]
    )
    names = {item["name"] for item in payload["tools"]}
    assert "run_command" in names
    assert "write_file" in names
    assert "apply_patch" in names
    assert "tool_search" in names
