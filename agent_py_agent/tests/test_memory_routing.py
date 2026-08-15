"""长期规则 memory routing 测试。"""

import json

from agent_py_agent.agent.memory_routing import (
    MemoryRoute,
    load_memory_routes,
    match_routes,
    resolve_required_paths,
    validate_routes,
)


def test_load_json_routes_and_match_chinese_trigger(tmp_path):
    rules_dir = tmp_path / "references" / "memory"
    rules_dir.mkdir(parents=True)
    (rules_dir / "compression.md").write_text("压缩前必须先写 hook。", encoding="utf-8")
    index_path = tmp_path / "memory_routes.json"
    index_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "route_id": "memory.compression",
                        "topic": "上下文压缩",
                        "trigger_keywords": ["压缩前", "上下文压缩", "hook 记忆"],
                        "related_terms": ["compression memory"],
                        "when_to_read": "讨论上下文压缩、压缩前落盘、恢复快照时读取",
                        "authority_path": "references/memory/compression.md",
                        "scope": "global",
                        "priority": 20,
                        "stale_check": "monthly",
                        "last_verified_at": "2026-04-30",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    routes = load_memory_routes(index_path)
    hits = match_routes("我们要处理上下文压缩，压缩前先保存 hook", routes)

    assert len(routes) == 1
    assert hits
    assert hits[0].route.route_id == "memory.compression"
    assert "压缩前" in hits[0].matched_terms
    assert validate_routes(routes, tmp_path) == []


def test_markdown_routes_match_related_term(tmp_path):
    rules_dir = tmp_path / "references" / "memory"
    rules_dir.mkdir(parents=True)
    (rules_dir / "routing.md").write_text("长期规则走 index。", encoding="utf-8")
    index_path = tmp_path / "routes.md"
    index_path.write_text(
        """# Memory Routes

## memory.routing
topic: 长期规则索引
trigger_keywords: 长期规则, 规则索引
related_terms: memory index, 规则导航
when_to_read: 用户讨论长期规则、memory 导航、index 到 authority file 时读取
authority_path: references/memory/routing.md
scope: global
priority: 30
stale_check: monthly
last_verified_at: 2026-04-30
""",
        encoding="utf-8",
    )

    routes = load_memory_routes(index_path)
    hits = match_routes("以后 memory index 需要由代码强制路由", routes)

    assert hits[0].route.route_id == "memory.routing"
    assert "memory index" in hits[0].matched_terms
    assert any("相关词" in reason for reason in hits[0].reasons)


def test_markdown_routes_split_common_human_list_separators(tmp_path):
    index_path = tmp_path / "routes.md"
    index_path.write_text(
        """## memory.human-list
topic: 人工索引
trigger_keywords: 英文逗号, 中文逗号，英文分号; 中文分号；竖线|最后一个
related_terms: alpha|beta，gamma
authority_path: references/memory/human-list.md
""",
        encoding="utf-8",
    )

    routes = load_memory_routes(index_path)

    assert routes[0].trigger_keywords == [
        "英文逗号",
        "中文逗号",
        "英文分号",
        "中文分号",
        "竖线",
        "最后一个",
    ]
    assert routes[0].related_terms == ["alpha", "beta", "gamma"]


def test_match_routes_honors_limit_and_priority():
    routes = [
        MemoryRoute(
            route_id="low",
            topic="记忆",
            trigger_keywords=["记忆"],
            authority_path="rules/low.md",
            priority=1,
        ),
        MemoryRoute(
            route_id="high",
            topic="记忆",
            trigger_keywords=["记忆"],
            authority_path="rules/high.md",
            priority=90,
        ),
    ]

    hits = match_routes("记忆怎么设计", routes, limit=1)

    assert [hit.route.route_id for hit in hits] == ["high"]


def test_resolve_required_paths_soft_and_strict():
    routes = [
        MemoryRoute(
            route_id="memory.routing",
            topic="长期规则",
            trigger_keywords=["长期规则"],
            authority_path="references/memory/routing.md",
            priority=30,
        ),
        MemoryRoute(
            route_id="memory.compression",
            topic="压缩",
            trigger_keywords=["压缩"],
            authority_path="references/memory/compression.md",
            priority=20,
        ),
    ]
    hits = match_routes("长期规则和压缩都要处理", routes, limit=0)

    soft = resolve_required_paths(hits, mode="soft", auto_read_limit=1)
    strict = resolve_required_paths(hits, mode="strict", auto_read_limit=1)

    assert soft.required_read_paths == []
    assert soft.candidate_paths == ["references/memory/routing.md"]
    assert strict.required_read_paths == ["references/memory/routing.md"]
    assert strict.candidate_paths == ["references/memory/compression.md"]


def test_resolve_required_paths_zero_auto_read_selects_no_paths():
    routes = [
        MemoryRoute(
            route_id="memory.routing",
            topic="长期规则",
            trigger_keywords=["长期规则"],
            authority_path="references/memory/routing.md",
            priority=30,
        ),
        MemoryRoute(
            route_id="memory.compression",
            topic="压缩",
            trigger_keywords=["压缩"],
            authority_path="references/memory/compression.md",
            priority=20,
        ),
    ]
    hits = match_routes("长期规则和压缩都要处理", routes, limit=0)

    soft = resolve_required_paths(hits, mode="soft", auto_read_limit=0)
    strict = resolve_required_paths(hits, mode="strict", auto_read_limit=0)

    assert soft.required_read_paths == []
    assert soft.candidate_paths == []
    assert strict.required_read_paths == []
    assert strict.candidate_paths == [
        "references/memory/routing.md",
        "references/memory/compression.md",
    ]
    assert strict.matches == hits


def test_validate_routes_reports_missing_file_and_empty_triggers(tmp_path):
    routes = [
        MemoryRoute(
            route_id="memory.missing",
            topic="缺文件",
            trigger_keywords=[],
            related_terms=[],
            authority_path="references/memory/missing.md",
        )
    ]

    findings = validate_routes(routes, tmp_path)

    assert any("trigger_keywords and related_terms are both empty" in finding for finding in findings)
    assert any("authority_path does not exist" in finding for finding in findings)


def test_validate_routes_reports_duplicate_route_id(tmp_path):
    rules_dir = tmp_path / "references"
    rules_dir.mkdir()
    (rules_dir / "rule.md").write_text("规则正文", encoding="utf-8")
    routes = [
        MemoryRoute(
            route_id="memory.duplicate",
            topic="规则 A",
            trigger_keywords=["规则"],
            authority_path="references/rule.md",
        ),
        MemoryRoute(
            route_id="memory.duplicate",
            topic="规则 B",
            related_terms=["rule"],
            authority_path="references/rule.md",
        ),
    ]

    findings = validate_routes(routes, tmp_path)

    assert any("duplicate route_id" in finding for finding in findings)
