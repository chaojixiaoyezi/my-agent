from __future__ import annotations

from agent_py_agent.agent.capability.router import (
    CapabilityCard,
    CapabilityRouter,
    McpToolDescriptor,
    from_mcp_tool,
)
from agent_py_agent.agent.capability_config import CapabilityConfig


def test_router_describes_card_by_id_and_kind_name() -> None:
    router = CapabilityRouter(
        config=CapabilityConfig(capability_candidate_limit=5),
        extra_cards=[
            CapabilityCard(
                id="skill:api-check",
                kind="skill",
                name="api-check",
                description="检查 REST API 返回和错误码",
                capabilities=["api_testing"],
                when_to_use=["需要接口验收时使用"],
                risk_level="low",
            )
        ],
    )

    by_id = router.describe("skill:api-check")
    by_name = router.describe("api-check")

    assert "api-check" in by_id
    assert "api_testing" in by_id
    assert by_id == by_name


def test_router_describe_unknown_card_raises_key_error() -> None:
    router = CapabilityRouter(config=CapabilityConfig())

    try:
        router.describe("../missing")
    except KeyError as exc:
        assert "未知 capability" in str(exc)
    else:
        raise AssertionError("describe should reject unknown capability ids")


def test_router_catalog_can_filter_kinds_and_respect_limit() -> None:
    router = CapabilityRouter(
        config=CapabilityConfig(capability_candidate_limit=5),
        extra_cards=[
            CapabilityCard(id="skill:one", kind="skill", name="one", description="one"),
            CapabilityCard(id="tool:two", kind="tool", name="two", description="two"),
            CapabilityCard(id="skill:three", kind="skill", name="three", description="three"),
        ],
    )

    catalog = router.render_catalog(kinds={"skill"}, limit=1)

    assert "# Capability Catalog" in catalog
    assert "skill:one" in catalog
    assert "tool:two" not in catalog
    assert "skill:three" not in catalog


def test_mcp_descriptor_becomes_searchable_capability_card() -> None:
    descriptor = McpToolDescriptor(
        server="browser",
        name="screenshot",
        description="Capture a browser screenshot for visual verification",
        capabilities=["browser_automation", "visual_verification"],
        keywords=["playwright", "screenshot", "browser", "截图"],
        risk_level="medium",
    )
    router = CapabilityRouter(config=CapabilityConfig(), extra_cards=[from_mcp_tool(descriptor)])

    hits = router.search("需要浏览器截图验证", kinds={"mcp_tool"}, limit=5)

    assert hits
    assert hits[0].card.id == "mcp:browser:screenshot"
    assert hits[0].card.kind == "mcp_tool"
    assert hits[0].card.metadata["server"] == "browser"


def test_from_mcp_tool_rejects_missing_server_or_name() -> None:
    try:
        from_mcp_tool(McpToolDescriptor(server="", name="query", description="query"))
    except ValueError as exc:
        assert "server 和 name" in str(exc)
    else:
        raise AssertionError("missing server should be rejected")
