from __future__ import annotations

import json

from agent_py_agent.agent.capability.grants import CapabilityGrantScope, filter_cards_by_grant_scope
from agent_py_agent.agent.capability.router import CapabilityCard, CapabilityRouter
from agent_py_agent.agent.capability.usage import (
    CapabilityUsageRecord,
    CapabilityUsageStore,
    JsonlCapabilityUsageStore,
)


def test_grant_scope_filters_tools_skills_and_mcp_without_granting_execution():
    cards = [
        CapabilityCard(id="tool:read_file", kind="tool", name="read_file", description="Read files"),
        CapabilityCard(id="tool:write_file", kind="tool", name="write_file", description="Write files"),
        CapabilityCard(id="skill:pytest", kind="skill", name="pytest", description="Debug tests"),
        CapabilityCard(id="mcp:browser:screenshot", kind="mcp_tool", name="screenshot", description="Screenshot"),
    ]
    scope = CapabilityGrantScope(
        tools=["read_file"],
        skills=["pytest"],
        mcp_tools=["browser:screenshot"],
    )

    visible = filter_cards_by_grant_scope(cards, scope)

    assert [card.id for card in visible] == [
        "tool:read_file",
        "skill:pytest",
        "mcp:browser:screenshot",
    ]
    assert scope.allows_tool("write_file") is False
    assert scope.allows_mcp_tool("browser", "screenshot") is True


def test_usage_store_summarizes_success_and_failure_for_router_scoring():
    store = CapabilityUsageStore()
    store.record(
        CapabilityUsageRecord(
            capability_id="tool:read_file",
            task_fingerprint="inspect config",
            selected_reason="needed file contents",
            accepted=True,
        )
    )
    store.record(
        CapabilityUsageRecord(
            capability_id="tool:write_file",
            task_fingerprint="inspect config",
            selected_reason="model guessed edit",
            accepted=False,
            failure_code="WRITE_FORBIDDEN",
        )
    )

    read_stats = store.stats_for("tool:read_file")
    write_stats = store.stats_for("tool:write_file")

    assert read_stats.successes == 1
    assert read_stats.failures == 0
    assert write_stats.successes == 0
    assert write_stats.failures == 1
    assert write_stats.last_failure_code == "WRITE_FORBIDDEN"


def test_jsonl_usage_store_persists_records_and_ignores_invalid_lines(tmp_path):
    path = tmp_path / "capability-usage.jsonl"
    path.write_text('{"not":"a record"}\nnot-json\n', encoding="utf-8")
    store = JsonlCapabilityUsageStore(path)

    stored = store.record(
        CapabilityUsageRecord(
            capability_id="mcp:browser:screenshot",
            task_fingerprint="ui smoke",
            selected_reason="needed browser screenshot",
            accepted=True,
            metadata={"server": "browser"},
        )
    )
    reloaded = JsonlCapabilityUsageStore(path)

    payloads = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("{")]
    assert stored.created_at > 0
    assert reloaded.stats_for("mcp:browser:screenshot").successes == 1
    assert payloads[-1]["schema_version"] == "capability_usage_record.v1"
    assert payloads[-1]["capability_id"] == "mcp:browser:screenshot"


def test_router_uses_usage_history_and_grant_scope_to_rank_visible_cards():
    store = CapabilityUsageStore()
    store.record(
        CapabilityUsageRecord(
            capability_id="tool:read_file",
            task_fingerprint="inspect source",
            selected_reason="worked before",
            accepted=True,
        )
    )
    router = CapabilityRouter(
        extra_cards=[
            CapabilityCard(
                id="tool:read_file",
                kind="tool",
                name="read_file",
                description="Read source files",
                keywords=["source", "inspect"],
            ),
            CapabilityCard(
                id="tool:write_file",
                kind="tool",
                name="write_file",
                description="Edit source files",
                keywords=["source", "inspect"],
            ),
        ],
        usage_store=store,
        grant_scope=CapabilityGrantScope(tools=["read_file"]),
    )

    hits = router.search("inspect source", limit=10)

    assert [hit.card.id for hit in hits] == ["tool:read_file"]
    assert any("历史成功" in reason for reason in hits[0].reasons)


def test_router_expands_common_semantic_aliases_without_vector_dependency():
    router = CapabilityRouter(
        extra_cards=[
            CapabilityCard(
                id="mcp:browser:screenshot",
                kind="mcp_tool",
                name="screenshot",
                description="Capture browser image evidence",
                keywords=["browser", "screenshot"],
            )
        ]
    )

    hits = router.search("网页截图验收", limit=3, kinds={"mcp_tool"})

    assert [hit.card.id for hit in hits] == ["mcp:browser:screenshot"]
    assert any("语义别名" in reason for reason in hits[0].reasons)
