
from __future__ import annotations

"""验证 6 个 log_ops 工具被 ToolRegistry 注册、出现在 specs 与原生 tools schema 里(纯加法不破坏现有)。"""

from pathlib import Path

from agent_py_agent.agent.backends.tool_schema import tool_specs_to_anthropic_tools
from agent_py_agent.agent.tooling.log_ops.tools import LOG_OPS_TOOL_NAMES
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


def _registry(tmp_path: Path) -> ToolRegistry:
    params = ToolRegistryParams(
        workspace_root=tmp_path,
        max_chars=10_000,
        max_entries=200,
        max_matches=200,
        web_max_chars=10_000,
        http_timeout=10,
        catalog_limit=500,
        retrieval_limit=20,
        vector_search_enabled=False,
    )
    return ToolRegistry(params)


def test_all_log_ops_tools_registered(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for name in LOG_OPS_TOOL_NAMES:
        assert name in registry.tools, f"{name} 未注册"
        assert registry.tools[name].spec.category == "log_ops"


def test_log_ops_tools_in_specs_and_native_schema(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    spec_names = {spec.name for spec in registry.specs()}
    for name in LOG_OPS_TOOL_NAMES:
        assert name in spec_names

    tools = tool_specs_to_anthropic_tools(registry.specs())
    native_names = {t["name"] for t in tools}
    for name in LOG_OPS_TOOL_NAMES:
        assert name in native_names


def test_log_ops_tools_workspace_rooted(tmp_path: Path) -> None:
    # 工具的 store 根应落在 workspace 下的 .log_ops(不污染别处)。
    registry = _registry(tmp_path)
    start_tool = registry.tools["log_monitor_start"]
    store = start_tool.store({})
    assert str(store.root).startswith(str(tmp_path.resolve()))
    assert ".log_ops" in str(store.root)
