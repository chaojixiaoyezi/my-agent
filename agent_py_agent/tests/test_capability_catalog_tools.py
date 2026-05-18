from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.capability.grants import CapabilityGrantScope
from agent_py_agent.agent.capability.mcp import McpToolDescriptor, from_mcp_tool
from agent_py_agent.agent.capability.skills import SkillCard, SkillRegistry
from agent_py_agent.agent.tooling.capability_catalog import (
    CapabilityDescribeTool,
    CapabilitySearchTool,
)
from agent_py_agent.agent.tooling.models import ToolSpec


def _tool_spec(
    name: str,
    *,
    category: str,
    description: str,
    keywords: list[str],
) -> ToolSpec:
    return ToolSpec(
        name=name,
        category=category,
        description=description,
        use_cases=[description],
        avoid_when=[],
        keywords=keywords,
        parameters={"path": "目标路径"} if category == "filesystem" else {"url": "目标 URL"},
    )


def _payload(result):
    return json.loads(result.output)


def test_search_tool_returns_json_results_for_short_medium_and_complex_queries():
    tool = CapabilitySearchTool(
        tool_specs=[
            _tool_spec(
                "read_file",
                category="filesystem",
                description="读取文件内容以检查配置或源码",
                keywords=["read", "file", "config", "source", "读取", "文件"],
            ),
            _tool_spec(
                "http_request",
                category="network",
                description="发送 HTTP 请求并检查 REST API JSON 返回",
                keywords=["http", "rest", "api", "json", "request"],
            ),
            _tool_spec(
                "write_file",
                category="filesystem",
                description="写入文件内容",
                keywords=["write", "patch", "edit", "文件"],
            ),
        ]
    )

    short = _payload(tool.execute({"query": "api", "limit": 3}))
    medium = _payload(tool.execute({"request": {"query": "read config file", "kind": "tool", "limit": 3}}))
    complex_payload = _payload(
        tool.execute(
            {
                "query": "Need to send HTTP POST to a REST API and inspect the JSON response without editing files",
                "limit": 2,
            }
        )
    )

    assert short["schema_version"] == "capability_catalog.v1"
    assert short["results"][0]["name"] == "http_request"
    assert short["results"][0]["kind"] == "tool"
    assert short["results"][0]["risk"] == "medium"
    assert short["results"][0]["source"] == "builtin_tool_registry"
    assert short["results"][0]["reasons"]
    assert medium["results"][0]["name"] == "read_file"
    assert [item["name"] for item in complex_payload["results"]][:1] == ["http_request"]
    assert all("side_effects" in item for item in complex_payload["results"])


def test_search_tool_respects_limit():
    tool = CapabilitySearchTool(
        tool_specs=[
            _tool_spec("read_file", category="filesystem", description="读取文件", keywords=["file"]),
            _tool_spec("write_file", category="filesystem", description="写入文件", keywords=["file"]),
            _tool_spec("fetch_url", category="network", description="读取 URL", keywords=["file", "url"]),
        ]
    )

    payload = _payload(tool.execute({"query": "file", "limit": 1}))

    assert len(payload["results"]) == 1


def test_describe_tool_returns_tool_detail_without_executing_or_authorizing():
    tool = CapabilityDescribeTool(
        tool_specs=[
            _tool_spec(
                "write_file",
                category="filesystem",
                description="写入文件内容",
                keywords=["write", "file"],
            )
        ]
    )

    result = tool.execute({"id": "tool:write_file"})
    payload = _payload(result)

    assert result.ok is True
    assert payload["schema_version"] == "capability_catalog.v1"
    assert payload["detail"]["id"] == "tool:write_file"
    assert payload["detail"]["kind"] == "tool"
    assert payload["detail"]["name"] == "write_file"
    assert payload["detail"]["risk"] == "high"
    assert payload["detail"]["side_effects"] == ["filesystem_write"]
    assert payload["detail"]["authorization"] == "not_granted"
    assert payload["detail"]["execution"] == "not_executed"


def test_describe_tool_returns_visible_skill_detail():
    registry = SkillRegistry([])
    registry._cards = {
        "python-debug": SkillCard(
            name="python-debug",
            description="排查 Python 测试失败和异常",
            path=Path("/tmp/python-debug/SKILL.md"),
            when_to_use="遇到 pytest 失败时使用",
            tags=["python", "pytest"],
            capabilities=["python_debugging"],
            tools_required=["read_file"],
            risk_level="low",
            source="test",
        )
    }
    tool = CapabilityDescribeTool(tool_specs=[], skill_registry=registry)

    payload = _payload(tool.execute({"request": {"id": "skill:python-debug"}}))

    assert payload["detail"]["id"] == "skill:python-debug"
    assert payload["detail"]["kind"] == "skill"
    assert payload["detail"]["name"] == "python-debug"
    assert payload["detail"]["source"] == "test"
    assert payload["detail"]["metadata"]["tools_required"] == ["read_file"]


def test_security_spec_not_passed_to_catalog_is_not_visible():
    tool = CapabilityDescribeTool(
        tool_specs=[
            _tool_spec("read_file", category="filesystem", description="读取文件", keywords=["read", "file"])
        ]
    )

    result = tool.execute({"id": "tool:security_query"})
    payload = _payload(result)

    assert result.ok is False
    assert payload["error"]["code"] == "CAPABILITY_NOT_FOUND"
    assert "tool:security_query" in payload["error"]["message"]


def test_search_security_spec_not_passed_to_catalog_is_not_visible():
    tool = CapabilitySearchTool(
        tool_specs=[
            _tool_spec("read_file", category="filesystem", description="读取文件", keywords=["read", "file"])
        ]
    )

    payload = _payload(tool.execute({"query": "security hunt ip threat", "limit": 5}))

    assert all(item["name"] != "security_query" for item in payload["results"])


def test_catalog_search_can_include_mcp_cards_and_filter_by_grant_scope():
    mcp_card = from_mcp_tool(
        McpToolDescriptor(
            server="browser",
            name="screenshot",
            description="Capture browser screenshots",
            capabilities=["browser", "screenshot"],
            keywords=["ui"],
        )
    )
    tool = CapabilitySearchTool(
        tool_specs=[
            _tool_spec("read_file", category="filesystem", description="读取文件", keywords=["read", "file"])
        ],
        extra_cards=[mcp_card],
        grant_scope=CapabilityGrantScope(mcp_tools=["browser:screenshot"]),
    )

    payload = _payload(tool.execute({"query": "browser screenshot", "kind": "mcp_tool", "limit": 5}))

    assert [item["id"] for item in payload["results"]] == ["mcp:browser:screenshot"]


def test_catalog_describe_hides_cards_outside_grant_scope():
    mcp_card = from_mcp_tool(
        McpToolDescriptor(server="browser", name="screenshot", description="Capture browser screenshots")
    )
    tool = CapabilityDescribeTool(
        tool_specs=[
            _tool_spec("read_file", category="filesystem", description="读取文件", keywords=["read", "file"])
        ],
        extra_cards=[mcp_card],
        grant_scope=CapabilityGrantScope(tools=["read_file"]),
    )

    result = tool.execute({"id": "mcp:browser:screenshot"})
    payload = _payload(result)

    assert result.ok is False
    assert payload["error"]["code"] == "CAPABILITY_NOT_FOUND"
