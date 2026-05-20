from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


# LLM: list_tools gives the model a machine-readable ToolManifest at runtime.
# 函数用途: 验证模型主动查询工具时能看到 run_command 和 data_to_workbook，不再靠猜。
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

    result = registry.execute_call({"tool": "list_tools"})

    assert result.ok, result.output
    payload = json.loads(result.output)
    names = {item["name"] for item in payload["tools"]}
    assert "TOOL_UNAVAILABLE" in payload["tool_failure_taxonomy"]
    assert "run_command" in names
    assert "data_to_workbook" in names
    assert "list_tools" in names
    list_tools = next(item for item in payload["tools"] if item["name"] == "list_tools")
    assert list_tools["visible_in_context"] is True
    assert list_tools["executable_in_context"] is True
    failure_contracts = {item["code"]: item for item in payload["failure_contracts"]}
    assert failure_contracts["TOOL_TIMEOUT"]["recommended_action"] == "retry_with_smaller_scope_or_longer_timeout"
