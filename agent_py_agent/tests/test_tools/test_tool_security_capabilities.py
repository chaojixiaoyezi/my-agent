"""LLM: tests for security tool catalog visibility and authorization grants.

给人看的解释：
这个文件只验证安全类工具默认隐藏、未授权阻断，以及授权后可见可用。
"""

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.log_analysis.storage import LocalLogStore
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


def _make_tool_registry(workspace: Path) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace,
            max_chars=12000,
            max_entries=100,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
        )
    )


def test_security_tools_are_hidden_by_default_and_require_authorization():
    """LLM: verify security tools are hidden from catalog and blocked without authorization.

    新手说明:
    安全工具默认不出现在工具目录和推荐列表中，调用时会被拒绝。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        registry = _make_tool_registry(workspace)

        catalog = registry.render_catalog_section()
        recommended = registry.render_recommended_tools_section("investigate security logs for attacker ip")
        blocked = registry.execute_call(
            {
                "tool": "security_query",
                "start_time": "2026-04-30T09:00:00Z",
                "end_time": "2026-04-30T11:00:00Z",
                "limit": 10,
            }
        )

        assert "security_query [log_analysis]" not in catalog
        assert "security_query" not in recommended
        assert not blocked.ok
        assert "not authorized" in blocked.output


def test_security_tools_are_exposed_for_security_capability_or_tool_grant():
    """LLM: verify security tools appear and work when capability or tool grant is provided.

    新手说明:
    授予 logs/security 能力或显式允许 security_query 工具后，安全工具可正常使用。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        registry = _make_tool_registry(workspace)
        store = LocalLogStore(workspace)
        store.upsert_event(
            {
                "event_id": "evt-1",
                "event_time": "2026-04-30T10:00:00Z",
                "source_id": "waf-prod",
                "alert_type": "web_attack",
                "attacker_ip": "198.51.100.10",
                "payload": "A" * 500,
            }
        )

        capability_catalog = registry.render_catalog_section(granted_capabilities=["logs/security"])
        allowed_catalog = registry.render_catalog_section(allowed_tools=["security_query"])
        result = registry.execute_call(
            {
                "tool": "security_query",
                "attacker_ip": "198.51.100.10",
                "start_time": "2026-04-30T09:00:00Z",
                "end_time": "2026-04-30T11:00:00Z",
                "limit": 10,
            },
            granted_capabilities=["logs/security"],
        )
        payload = json.loads(result.output)

        assert "security_query [log_analysis" in capability_catalog
        assert "security_query [log_analysis" in allowed_catalog
        assert result.ok
        assert payload["tool"] == "security_query"
        assert payload["row_count"] == 1
        assert payload["evidence_refs"]
        assert "rows" not in payload
        assert "preview_rows" in payload
