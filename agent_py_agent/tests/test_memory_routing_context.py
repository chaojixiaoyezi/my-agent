"""运行时 memory routing context 服务测试。"""

from agent_py_agent.agent.memory_routing import build_routed_memory_context


def _write_index(root, routes):
    index_path = root / "memory" / "routing" / "INDEX.md"
    index_path.parent.mkdir(parents=True)
    blocks = ["# Memory Routes"]
    for route in routes:
        blocks.extend(
            [
                "",
                f"## {route['route_id']}",
                f"topic: {route['topic']}",
                f"trigger_keywords: {', '.join(route['trigger_keywords'])}",
                f"authority_path: {route['authority_path']}",
                f"priority: {route['priority']}",
            ]
        )
    index_path.write_text("\n".join(blocks) + "\n", encoding="utf-8")
    return index_path


def _route(route_id, keyword, authority_path, *, priority=10):
    return {
        "route_id": route_id,
        "topic": route_id,
        "trigger_keywords": [keyword],
        "authority_path": authority_path,
        "priority": priority,
    }


def test_routed_memory_context_missing_index_returns_finding(tmp_path):
    ctx = build_routed_memory_context(tmp_path, "memory rules")

    assert ctx.enabled is True
    assert ctx.index_path == "memory/routing/INDEX.md"
    assert ctx.routes_count == 0
    assert ctx.matches == []
    assert ctx.receipts == []
    assert any("memory route index does not exist" in finding for finding in ctx.findings)


def test_routed_memory_context_blocks_authority_path_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "secret.md").write_text("do not read this", encoding="utf-8")
    _write_index(root, [_route("escape", "escape", "../secret.md")])

    ctx = build_routed_memory_context(root, "escape")

    assert [match["route_id"] for match in ctx.matches] == ["escape"]
    assert ctx.candidate_paths == []
    assert ctx.required_read_paths == []
    assert ctx.receipts == []
    assert ctx.injected_sections == []
    assert any("authority_path escapes root" in finding for finding in ctx.findings)


def test_routed_memory_context_blocks_index_path_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "INDEX.md").write_text("# Outside index\n", encoding="utf-8")

    ctx = build_routed_memory_context(root, "outside", index_path="../INDEX.md")

    assert ctx.routes_count == 0
    assert ctx.matches == []
    assert ctx.receipts == []
    assert any("index_path escapes root" in finding for finding in ctx.findings)


def test_routed_memory_context_soft_reads_limited_candidates(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "high.md").write_text("high rule body", encoding="utf-8")
    (rules / "low.md").write_text("low rule body", encoding="utf-8")
    _write_index(
        tmp_path,
        [
            _route("low", "rule", "rules/low.md", priority=1),
            _route("high", "rule", "rules/high.md", priority=50),
        ],
    )

    ctx = build_routed_memory_context(
        tmp_path,
        "rule",
        mode="soft",
        auto_read_limit=1,
        limit=0,
    )

    assert ctx.required_read_paths == []
    assert ctx.candidate_paths == ["rules/high.md", "rules/low.md"]
    assert [receipt["path"] for receipt in ctx.receipts] == ["rules/high.md"]
    assert ctx.receipts[0]["status"] == "read"
    assert ctx.receipts[0]["content_hash"]
    assert "rules/high.md" in ctx.injected_sections[0]
    assert "high rule body" in ctx.injected_sections[0]
    assert "low rule body" not in "\n".join(ctx.injected_sections)


def test_routed_memory_context_strict_reads_required_paths(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "first.md").write_text("first strict rule", encoding="utf-8")
    (rules / "second.md").write_text("second strict rule", encoding="utf-8")
    _write_index(
        tmp_path,
        [
            _route("second", "strict", "rules/second.md", priority=10),
            _route("first", "strict", "rules/first.md", priority=20),
        ],
    )

    ctx = build_routed_memory_context(
        tmp_path,
        "strict",
        mode="strict",
        auto_read_limit=1,
        limit=0,
    )

    assert ctx.candidate_paths == ["rules/first.md", "rules/second.md"]
    assert ctx.required_read_paths == ["rules/first.md"]
    assert [receipt["path"] for receipt in ctx.receipts] == ctx.required_read_paths
    assert ctx.receipts[0]["status"] == "read"


def test_routed_memory_context_zero_auto_read_does_not_read_body(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "zero.md").write_text("body should stay out", encoding="utf-8")
    _write_index(tmp_path, [_route("zero", "zero", "rules/zero.md")])

    ctx = build_routed_memory_context(
        tmp_path,
        "zero",
        mode="soft",
        auto_read_limit=0,
    )

    assert ctx.candidate_paths == ["rules/zero.md"]
    assert ctx.required_read_paths == []
    assert ctx.receipts == []
    assert ctx.injected_sections == []


def test_routed_memory_context_truncates_injected_body(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "long.md").write_text("0123456789ABCDEFGHIJ", encoding="utf-8")
    _write_index(tmp_path, [_route("long", "long", "rules/long.md")])

    ctx = build_routed_memory_context(
        tmp_path,
        "long",
        max_chars_per_file=8,
    )

    assert len(ctx.receipts[0]["content_hash"]) == 64
    section = ctx.injected_sections[0]
    assert "rules/long.md" in section
    assert "01234567" in section
    assert "89ABCDEFGHIJ" not in section
    assert "[truncated:" in section
