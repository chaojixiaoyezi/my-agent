from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.common.value_parsing import dedupe_strings
from agent_py_agent.agent.memory_routing.models import (
    MemoryPathResolution,
    MemoryReadReceipt,
    MemoryRoute,
    MemoryRouteMatch,
)
from agent_py_agent.agent.memory_routing.validator import (
    VALID_INJECT_MODES,
    _validate_authority_path,
    validate_routes,
)


class TestValidInjectModes:
    def test_valid_inject_modes_contains_expected_values(self):
        assert {"always", "on_hit", "never"} == VALID_INJECT_MODES

    def test_valid_inject_modes_is_set(self):
        assert isinstance(VALID_INJECT_MODES, set)


class TestMemoryRouteModel:
    def test_memory_route_basic(self):
        route = MemoryRoute(route_id="r1", topic="test topic")
        assert route.route_id == "r1"
        assert route.topic == "test topic"

    def test_memory_route_defaults(self):
        route = MemoryRoute(route_id="r1", topic="t")
        assert route.trigger_keywords == []
        assert route.aliases == []
        assert route.authority_path == ""
        assert route.inject_mode == "on_hit"
        assert route.scope == "global"
        assert route.priority == 0

    def test_memory_route_authority_file_prefers_source_file(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            source_file="new_path.md",
            authority_path="old_path.md",
        )
        assert route.authority_file() == "new_path.md"

    def test_memory_route_authority_file_falls_back_to_authority_path(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            authority_path="authority.md",
        )
        assert route.authority_file() == "authority.md"

    def test_memory_route_authority_file_strips_whitespace(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            source_file="  path.md  ",
            authority_path="  ",
        )
        assert route.authority_file() == "path.md"

    def test_memory_route_authority_file_empty(self):
        route = MemoryRoute(route_id="r1", topic="t")
        assert route.authority_file() == ""

    def test_memory_route_trigger_terms_keywords_only(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            trigger_keywords=["apple", "banana"],
        )
        assert route.trigger_terms() == ["apple", "banana"]

    def test_memory_route_trigger_terms_aliases_only(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            aliases=["x", "y"],
        )
        assert route.trigger_terms() == ["x", "y"]

    def test_memory_route_trigger_terms_combined(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            trigger_keywords=["kw1"],
            aliases=["alias1"],
        )
        result = route.trigger_terms()
        assert result == ["kw1", "alias1"]

    def test_memory_route_trigger_terms_deduplicates(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            trigger_keywords=["word"],
            aliases=["word"],
        )
        result = route.trigger_terms()
        assert result == ["word"]

    def test_memory_route_trigger_terms_strips_whitespace(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            trigger_keywords=["  spaced  "],
            aliases=["  trimmed  "],
        )
        result = route.trigger_terms()
        assert "spaced" in result
        assert "trimmed" in result

    def test_memory_route_trigger_terms_empty(self):
        route = MemoryRoute(route_id="r1", topic="t")
        assert route.trigger_terms() == []

    def test_memory_route_trigger_terms_preserves_order(self):
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            trigger_keywords=["first", "second"],
            aliases=["third", "fourth"],
        )
        result = route.trigger_terms()
        assert result == ["first", "second", "third", "fourth"]


class TestMemoryRouteMatch:
    def test_memory_route_match_basic(self):
        route = MemoryRoute(route_id="r1", topic="t")
        match = MemoryRouteMatch(route=route, score=0.9)
        assert match.route is route
        assert match.score == 0.9
        assert match.reasons == []
        assert match.matched_terms == []

    def test_memory_route_match_with_reasons(self):
        route = MemoryRoute(route_id="r1", topic="t")
        match = MemoryRouteMatch(
            route=route,
            score=0.8,
            reasons=["exact match"],
            matched_terms=["test"],
        )
        assert match.reasons == ["exact match"]
        assert match.matched_terms == ["test"]


class TestMemoryPathResolution:
    def test_memory_path_resolution_soft(self):
        resolution = MemoryPathResolution(mode="soft")
        assert resolution.mode == "soft"
        assert resolution.required_read_paths == []
        assert resolution.candidate_paths == []

    def test_memory_path_resolution_strict(self):
        resolution = MemoryPathResolution(
            mode="strict",
            required_read_paths=["path1.md"],
            candidate_paths=["path2.md"],
        )
        assert resolution.mode == "strict"
        assert resolution.required_read_paths == ["path1.md"]
        assert resolution.candidate_paths == ["path2.md"]


class TestMemoryReadReceipt:
    def test_memory_read_receipt_basic(self):
        receipt = MemoryReadReceipt(
            route_id="r1",
            authority_path="path.md",
            status="read",
        )
        assert receipt.route_id == "r1"
        assert receipt.authority_path == "path.md"
        assert receipt.status == "read"
        assert receipt.read_at == 0.0
        assert receipt.error == ""

    def test_memory_read_receipt_mark_now_sets_timestamp(self):
        receipt = MemoryReadReceipt(
            route_id="r1",
            authority_path="path.md",
            status="read",
        )
        assert receipt.read_at == 0.0
        result = receipt.mark_now()
        assert result is receipt
        assert receipt.read_at > 0

    def test_memory_read_receipt_mark_now_preserves_existing(self):
        receipt = MemoryReadReceipt(
            route_id="r1",
            authority_path="path.md",
            status="read",
            read_at=12345.0,
        )
        result = receipt.mark_now()
        assert result is receipt
        assert receipt.read_at == 12345.0


class TestDedupe:
    def test_dedupe_empty(self):
        assert dedupe_strings([]) == []

    def test_dedupe_no_duplicates(self):
        assert dedupe_strings(["a", "b", "c"]) == ["a", "b", "c"]

    def test_dedupe_with_duplicates(self):
        assert dedupe_strings(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]

    def test_dedupe_preserves_first_occurrence(self):
        assert dedupe_strings(["first", "second", "first"]) == ["first", "second"]

    def test_dedupe_strips_whitespace(self):
        assert dedupe_strings(["  a  ", "a", " b"]) == ["a", "b"]

    def test_dedupe_skips_empty_after_strip(self):
        assert dedupe_strings(["a", "  ", "", "b"]) == ["a", "b"]


class TestValidateRoutesEmpty:
    def test_validate_routes_empty_list(self):
        findings = validate_routes([], "/tmp")
        assert findings == []

    def test_validate_routes_root_unresolvable(self):
        route = MemoryRoute(route_id="r1", topic="t", source_file="p.md")
        with patch("pathlib.Path.resolve", side_effect=OSError("cannot resolve")):
            findings = validate_routes([route], "/tmp")
        assert len(findings) >= 1
        assert any("root path cannot be resolved" in f for f in findings)


class TestValidateRoutesSingleValid:
    def test_validate_routes_single_valid_route(self, tmp_path):
        authority_file = tmp_path / "authority.md"
        authority_file.write_text("content", encoding="utf-8")
        route = MemoryRoute(
            route_id="r1",
            topic="test topic",
            source_file="authority.md",
            trigger_keywords=["test"],
        )
        findings = validate_routes([route], tmp_path)
        assert findings == []

    def test_validate_routes_valid_with_aliases(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(
            route_id="r1",
            topic="topic",
            source_file="af.md",
            aliases=["alias1"],
        )
        findings = validate_routes([route], tmp_path)
        assert findings == []

    def test_validate_routes_valid_with_both_trigger_and_aliases(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(
            route_id="r1",
            topic="topic",
            source_file="af.md",
            trigger_keywords=["kw"],
            aliases=["als"],
        )
        findings = validate_routes([route], tmp_path)
        assert findings == []


class TestValidateRoutesErrors:
    def test_validate_routes_empty_route_id(self, tmp_path):
        route = MemoryRoute(route_id="", topic="topic", source_file="p.md")
        findings = validate_routes([route], tmp_path)
        assert any("route_id is empty" in f for f in findings)

    def test_validate_routes_route_id_whitespace_only(self, tmp_path):
        route = MemoryRoute(route_id="  ", topic="topic", source_file="p.md")
        findings = validate_routes([route], tmp_path)
        assert any("route_id is empty" in f for f in findings)

    def test_validate_routes_duplicate_route_id(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        routes = [
            MemoryRoute(route_id="dup", topic="t1", source_file="af.md"),
            MemoryRoute(route_id="dup", topic="t2", source_file="af.md"),
        ]
        findings = validate_routes(routes, tmp_path)
        assert any("duplicate route_id" in f for f in findings)

    def test_validate_routes_empty_topic(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(route_id="r1", topic="", source_file="af.md")
        findings = validate_routes([route], tmp_path)
        assert any("topic is empty" in f for f in findings)

    def test_validate_routes_topic_whitespace_only(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(route_id="r1", topic="  ", source_file="af.md")
        findings = validate_routes([route], tmp_path)
        assert any("topic is empty" in f for f in findings)

    def test_validate_routes_both_keywords_and_aliases_empty(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(route_id="r1", topic="t", source_file="af.md")
        findings = validate_routes([route], tmp_path)
        assert any("trigger_keywords and aliases are both empty" in f for f in findings)

    def test_validate_routes_invalid_inject_mode(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            source_file="af.md",
            inject_mode="invalid_mode",
        )
        findings = validate_routes([route], tmp_path)
        assert any("inject_mode must be one of" in f for f in findings)

    def test_validate_routes_duplicate_keyword_in_trigger(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            source_file="af.md",
            trigger_keywords=["word", "word"],
        )
        findings = validate_routes([route], tmp_path)
        assert any("duplicate keyword" in f for f in findings)

    def test_validate_routes_duplicate_keyword_in_aliases(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(
            route_id="r1",
            topic="t",
            source_file="af.md",
            aliases=["term", "term"],
        )
        findings = validate_routes([route], tmp_path)
        assert any("duplicate keyword" in f for f in findings)

    def test_validate_routes_keyword_conflict_across_routes(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        routes = [
            MemoryRoute(route_id="r1", topic="t1", source_file="af.md", aliases=["conflict"]),
            MemoryRoute(route_id="r2", topic="t2", source_file="af.md", aliases=["conflict"]),
        ]
        findings = validate_routes(routes, tmp_path)
        assert any("keyword conflict" in f for f in findings)

    def test_validate_routes_missing_authority_file(self, tmp_path):
        route = MemoryRoute(route_id="r1", topic="t")
        findings = validate_routes([route], tmp_path)
        assert any("authority_path/source_file is empty" in f for f in findings)


class TestValidateAuthorityPath:
    def test_absolute_path_flagged(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(route_id="r1", topic="t", source_file="/absolute/path.md")
        findings: list[str] = []
        _validate_authority_path(route, tmp_path, findings)
        assert any("must be relative" in f for f in findings)

    def test_nonexistent_authority_path(self, tmp_path):
        route = MemoryRoute(route_id="r1", topic="t", source_file="doesnotexist.md")
        findings: list[str] = []
        _validate_authority_path(route, tmp_path, findings)
        assert any("does not exist" in f for f in findings)

    def test_authority_path_is_directory(self, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        route = MemoryRoute(route_id="r1", topic="t", source_file="subdir")
        findings: list[str] = []
        _validate_authority_path(route, tmp_path, findings)
        assert any("is not a file" in f for f in findings)

    def test_authority_path_escapes_root(self, tmp_path):
        route = MemoryRoute(route_id="r1", topic="t", source_file="../escape.md")
        findings: list[str] = []
        _validate_authority_path(route, tmp_path, findings)
        assert any("escapes root" in f for f in findings)

    def test_authority_path_valid_relative(self, tmp_path):
        af = tmp_path / "valid.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(route_id="r1", topic="t", source_file="valid.md")
        findings: list[str] = []
        _validate_authority_path(route, tmp_path, findings)
        assert findings == []

    def test_authority_path_resolve_error(self, tmp_path):
        route = MemoryRoute(route_id="r1", topic="t", source_file="somefile.md")
        findings: list[str] = []
        with patch("pathlib.Path.resolve", side_effect=OSError("resolve failed")):
            _validate_authority_path(route, tmp_path, findings)
        assert any("cannot be resolved" in f for f in findings)

    def test_authority_path_empty_label_uses_placeholder(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        route = MemoryRoute(route_id="", topic="t", source_file="/absolute.md")
        findings: list[str] = []
        _validate_authority_path(route, tmp_path, findings)
        assert any("<empty route_id>" in f for f in findings)


class TestValidateRoutesMixed:
    def test_validate_routes_mixed_valid_and_invalid(self, tmp_path):
        af = tmp_path / "af.md"
        af.write_text("c", encoding="utf-8")
        routes = [
            MemoryRoute(route_id="valid1", topic="t1", source_file="af.md", aliases=["als"]),
            MemoryRoute(route_id="", topic="t2", source_file="af.md"),
            MemoryRoute(route_id="valid2", topic="t3", source_file="af.md", trigger_keywords=["kw"]),
        ]
        findings = validate_routes(routes, tmp_path)
        assert len(findings) >= 1

    def test_validate_routes_multiple_errors_on_same_route(self, tmp_path):
        route = MemoryRoute(route_id="", topic="", source_file="")
        findings = validate_routes([route], tmp_path)
        assert len(findings) >= 3
