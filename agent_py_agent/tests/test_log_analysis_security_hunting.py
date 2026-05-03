from __future__ import annotations

"""Tests for agent_py_agent.agent.log_analysis.security.hunting."""

import hashlib
from typing import Any

import pytest

from agent_py_agent.agent.log_analysis.models import CaseRecord, EvidenceRef
from agent_py_agent.agent.log_analysis.security.correlation import RouteDraft
from agent_py_agent.agent.log_analysis.security.hunting import (
    HuntQuery,
    build_case_hunt_plan,
    build_seed_hunt_queries,
    next_query_plan,
    retrohunt_query_plan,
)


# ---------------------------------------------------------------------------
# HuntQuery dataclass
# ---------------------------------------------------------------------------


class TestHuntQuery:
    """Tests for the HuntQuery dataclass."""

    def test_basic_creation(self) -> None:
        q = HuntQuery(
            query_id="q-1",
            purpose="test",
            seed_type="ip",
            seed_value="1.2.3.4",
        )
        assert q.query_id == "q-1"
        assert q.purpose == "test"
        assert q.seed_type == "ip"
        assert q.seed_value == "1.2.3.4"
        assert q.source_products == []
        assert q.time_window == ("", "")
        assert q.filters == {}
        assert q.limit == 500

    def test_to_dict_roundtrip(self) -> None:
        q = HuntQuery(
            query_id="q-2",
            purpose="roundtrip",
            seed_type="user",
            seed_value="alice",
            source_products=["vpn", "sso"],
            time_window=("2024-01-01", "2024-01-02"),
            filters={"user": "alice"},
            limit=100,
        )
        d = q.to_dict()
        assert d["query_id"] == "q-2"
        assert d["seed_type"] == "user"
        assert d["source_products"] == ["vpn", "sso"]
        assert d["time_window"] == ("2024-01-01", "2024-01-02")
        assert d["filters"] == {"user": "alice"}
        assert d["limit"] == 100

    def test_custom_fields(self) -> None:
        q = HuntQuery(
            query_id="q-3",
            purpose="custom",
            seed_type="domain",
            seed_value="evil.com",
            source_products=["dns", "proxy"],
            time_window=("2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z"),
            filters={"domain": "evil.com", "limit": 50},
            limit=200,
        )
        assert q.source_products == ["dns", "proxy"]
        assert q.filters["domain"] == "evil.com"
        assert q.limit == 200


# ---------------------------------------------------------------------------
# build_seed_hunt_queries
# ---------------------------------------------------------------------------


class TestBuildSeedHuntQueries:
    """Tests for build_seed_hunt_queries."""

    def test_ip_seed_produces_queries(self) -> None:
        queries = build_seed_hunt_queries("ip", "10.0.0.1")
        assert len(queries) == 3
        for q in queries:
            assert q.seed_type == "ip"
            assert q.seed_value == "10.0.0.1"
            assert q.source_products  # non-empty
            assert q.filters == {"ip": "10.0.0.1"}

    def test_ip_seed_query_ids_are_unique(self) -> None:
        queries = build_seed_hunt_queries("ip", "10.0.0.1")
        ids = [q.query_id for q in queries]
        assert len(ids) == len(set(ids))

    def test_ip_seed_query_ids_contain_seed_digest(self) -> None:
        seed_digest = hashlib.sha256("ip:10.0.0.1".encode("utf-8")).hexdigest()[:8]
        queries = build_seed_hunt_queries("ip", "10.0.0.1")
        for q in queries:
            assert seed_digest in q.query_id

    def test_user_seed_uses_correct_products(self) -> None:
        queries = build_seed_hunt_queries("user", "bob")
        assert len(queries) == 3
        for q in queries:
            assert q.seed_type == "user"
            expected_products = {"vpn", "sso", "ad", "windows", "pam"}
            assert set(q.source_products) == expected_products

    def test_host_seed_uses_correct_products(self) -> None:
        queries = build_seed_hunt_queries("host", "server-01")
        assert len(queries) == 3
        expected_products = {"edr", "hids", "windows", "linux", "netflow"}
        for q in queries:
            assert set(q.source_products) == expected_products

    def test_domain_seed_uses_correct_products(self) -> None:
        queries = build_seed_hunt_queries("domain", "evil.com")
        assert len(queries) == 3
        expected_products = {"dns", "proxy", "waf"}
        for q in queries:
            assert set(q.source_products) == expected_products

    def test_hash_seed_uses_correct_products(self) -> None:
        queries = build_seed_hunt_queries("hash", "abc123")
        assert len(queries) == 3
        expected_products = {"edr", "hids", "sandbox"}
        for q in queries:
            assert set(q.source_products) == expected_products

    def test_unknown_seed_type_uses_default_products(self) -> None:
        queries = build_seed_hunt_queries("custom_type", "value")
        assert len(queries) == 3
        default_products = {"waf", "vpn", "edr", "dns", "proxy"}
        for q in queries:
            assert set(q.source_products) == default_products

    def test_empty_seed_type_returns_empty(self) -> None:
        assert build_seed_hunt_queries("", "value") == []

    def test_empty_seed_value_returns_empty(self) -> None:
        assert build_seed_hunt_queries("ip", "") == []

    def test_none_seed_type_returns_empty(self) -> None:
        assert build_seed_hunt_queries(None, "value") == []  # type: ignore[arg-type]

    def test_none_seed_value_returns_empty(self) -> None:
        assert build_seed_hunt_queries("ip", None) == []  # type: ignore[arg-type]

    def test_whitespace_only_seed_returns_empty(self) -> None:
        assert build_seed_hunt_queries("  ", "value") == []
        assert build_seed_hunt_queries("ip", "  ") == []

    def test_time_window_propagated(self) -> None:
        queries = build_seed_hunt_queries("ip", "1.1.1.1", start="2024-01-01", end="2024-01-31")
        for q in queries:
            assert q.time_window == ("2024-01-01", "2024-01-31")

    def test_three_purposes_are_distinct(self) -> None:
        queries = build_seed_hunt_queries("ip", "1.1.1.1")
        purposes = [q.purpose for q in queries]
        assert len(purposes) == 3
        assert len(set(purposes)) == 3

    def test_attacker_ip_seed(self) -> None:
        queries = build_seed_hunt_queries("attacker_ip", "192.168.1.100")
        assert len(queries) == 3
        expected_products = {"waf", "vpn", "firewall", "proxy"}
        for q in queries:
            assert set(q.source_products) == expected_products
            assert q.seed_type == "attacker_ip"

    def test_victim_ip_seed(self) -> None:
        queries = build_seed_hunt_queries("victim_ip", "10.0.0.5")
        assert len(queries) == 3
        expected_products = {"edr", "hids", "netflow", "dns", "proxy"}
        for q in queries:
            assert set(q.source_products) == expected_products

    def test_process_seed(self) -> None:
        queries = build_seed_hunt_queries("process", "powershell.exe")
        assert len(queries) == 3
        expected_products = {"edr", "sysmon", "linux_audit"}
        for q in queries:
            assert set(q.source_products) == expected_products


# ---------------------------------------------------------------------------
# build_case_hunt_plan
# ---------------------------------------------------------------------------


class TestCaseHuntPlan:
    """Tests for build_case_hunt_plan."""

    @staticmethod
    def _make_case(**overrides: Any) -> CaseRecord:
        defaults: dict[str, Any] = {
            "case_id": "C-001",
            "title": "Test case",
            "entities": {"attacker_ip": ["1.2.3.4"], "victim_ip": ["10.0.0.1"]},
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }
        defaults.update(overrides)
        return CaseRecord(**defaults)

    def test_basic_case_produces_queries(self) -> None:
        case = self._make_case()
        queries = build_case_hunt_plan(case)
        assert len(queries) > 0

    def test_case_with_no_entities_returns_empty(self) -> None:
        case = self._make_case(entities={})
        queries = build_case_hunt_plan(case)
        assert queries == []

    def test_case_with_unsupported_entity_type_ignored(self) -> None:
        case = self._make_case(entities={"unsupported_field": ["value"]})
        queries = build_case_hunt_plan(case)
        assert queries == []

    def test_src_ip_normalized_to_ip(self) -> None:
        case = self._make_case(entities={"src_ip": ["192.168.1.1"]})
        queries = build_case_hunt_plan(case)
        assert len(queries) > 0
        for q in queries:
            assert q.seed_type == "ip"
            assert q.seed_value == "192.168.1.1"

    def test_dst_ip_normalized_to_ip(self) -> None:
        case = self._make_case(entities={"dst_ip": ["10.0.0.5"]})
        queries = build_case_hunt_plan(case)
        assert len(queries) > 0
        for q in queries:
            assert q.seed_type == "ip"

    def test_max_three_values_per_entity_type(self) -> None:
        case = self._make_case(
            entities={"attacker_ip": ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"]}
        )
        queries = build_case_hunt_plan(case)
        seed_values = {q.seed_value for q in queries}
        assert "4.4.4.4" not in seed_values

    def test_deduplication_removes_duplicate_queries(self) -> None:
        case = self._make_case(
            entities={"attacker_ip": ["1.1.1.1"], "src_ip": ["1.1.1.1"]}
        )
        queries = build_case_hunt_plan(case)
        keys = [(q.seed_type, q.seed_value, tuple(q.source_products)) for q in queries]
        assert len(keys) == len(set(keys))

    def test_accepts_mapping_input(self) -> None:
        case_dict: dict[str, Any] = {
            "case_id": "C-002",
            "title": "Dict case",
            "entities": {"user": ["alice"]},
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }
        queries = build_case_hunt_plan(case_dict)
        assert len(queries) > 0

    def test_route_draft_time_window_used(self) -> None:
        case = self._make_case()
        route = RouteDraft(
            case_id="C-001",
            timeline=[
                {"time": "2024-06-01T10:00:00Z"},
                {"time": "2024-06-01T12:00:00Z"},
            ],
        )
        queries = build_case_hunt_plan(case, route)
        for q in queries:
            assert q.time_window[0] == "2024-06-01T10:00:00Z"
            assert q.time_window[1] == "2024-06-01T12:00:00Z"

    def test_route_as_mapping(self) -> None:
        case = self._make_case()
        route_dict = {
            "timeline": [
                {"time": "2024-06-01T10:00:00Z"},
                {"time": "2024-06-01T12:00:00Z"},
            ]
        }
        queries = build_case_hunt_plan(case, route_dict)
        for q in queries:
            assert q.time_window[0] == "2024-06-01T10:00:00Z"

    def test_multiple_entity_types_produce_multiple_query_sets(self) -> None:
        case = self._make_case(
            entities={
                "attacker_ip": ["1.1.1.1"],
                "user": ["bob"],
                "host": ["srv-01"],
            }
        )
        queries = build_case_hunt_plan(case)
        seed_types = {q.seed_type for q in queries}
        assert "attacker_ip" in seed_types
        assert "user" in seed_types
        assert "host" in seed_types


# ---------------------------------------------------------------------------
# next_query_plan
# ---------------------------------------------------------------------------


class TestNextQueryPlan:
    """Tests for next_query_plan."""

    @staticmethod
    def _make_case(**overrides: Any) -> CaseRecord:
        defaults: dict[str, Any] = {
            "case_id": "C-100",
            "title": "Next query test",
            "entities": {"attacker_ip": ["5.5.5.5"]},
            "created_at": "2024-03-01T00:00:00Z",
            "updated_at": "2024-03-02T00:00:00Z",
        }
        defaults.update(overrides)
        return CaseRecord(**defaults)

    def test_returns_expected_keys(self) -> None:
        case = self._make_case()
        result = next_query_plan(case)
        assert "case_id" in result
        assert "next_queries" in result
        assert "hunt_queries" in result

    def test_case_id_propagated(self) -> None:
        case = self._make_case()
        result = next_query_plan(case)
        assert result["case_id"] == "C-100"

    def test_hunt_queries_are_dicts(self) -> None:
        case = self._make_case()
        result = next_query_plan(case)
        for hq in result["hunt_queries"]:
            assert isinstance(hq, dict)
            assert "query_id" in hq
            assert "seed_type" in hq

    def test_case_next_queries_merged(self) -> None:
        case = self._make_case(next_queries=["queryA", "queryB"])
        result = next_query_plan(case)
        assert "queryA" in result["next_queries"]
        assert "queryB" in result["next_queries"]

    def test_route_next_queries_merged(self) -> None:
        case = self._make_case()
        route = RouteDraft(next_queries=["routeQuery1"])
        result = next_query_plan(case, route)
        assert "routeQuery1" in result["next_queries"]

    def test_duplicate_next_queries_deduped(self) -> None:
        case = self._make_case(next_queries=["dup", "dup"])
        result = next_query_plan(case)
        assert result["next_queries"].count("dup") == 1

    def test_empty_case_entities_produces_empty_hunt_queries(self) -> None:
        case = self._make_case(entities={})
        result = next_query_plan(case)
        assert result["hunt_queries"] == []

    def test_accepts_mapping_case(self) -> None:
        case_dict: dict[str, Any] = {
            "case_id": "C-MAP",
            "title": "Dict",
            "entities": {"host": ["box-1"]},
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
        }
        result = next_query_plan(case_dict)
        assert result["case_id"] == "C-MAP"
        assert len(result["hunt_queries"]) > 0

    def test_none_route_handled(self) -> None:
        case = self._make_case()
        result = next_query_plan(case, None)
        assert isinstance(result, dict)

    def test_empty_next_queries_list(self) -> None:
        case = self._make_case(next_queries=[])
        result = next_query_plan(case)
        assert isinstance(result["next_queries"], list)


# ---------------------------------------------------------------------------
# retrohunt_query_plan
# ---------------------------------------------------------------------------


class TestRetrohuntQueryPlan:
    """Tests for retrohunt_query_plan."""

    def test_basic_structure(self) -> None:
        result = retrohunt_query_plan("ip", "1.2.3.4")
        assert result["kind"] == "retrohunt"
        assert result["seed_type"] == "ip"
        assert result["seed_value"] == "1.2.3.4"
        assert result["lookback_days"] == 30
        assert isinstance(result["queries"], list)

    def test_custom_lookback_days(self) -> None:
        result = retrohunt_query_plan("user", "alice", days=90)
        assert result["lookback_days"] == 90

    def test_queries_are_dicts(self) -> None:
        result = retrohunt_query_plan("domain", "evil.com")
        for q in result["queries"]:
            assert isinstance(q, dict)
            assert "query_id" in q
            assert "seed_type" in q

    def test_empty_seed_type_produces_empty_queries(self) -> None:
        result = retrohunt_query_plan("", "value")
        assert result["queries"] == []

    def test_empty_seed_value_produces_empty_queries(self) -> None:
        result = retrohunt_query_plan("ip", "")
        assert result["queries"] == []

    def test_seed_type_and_value_preserved(self) -> None:
        result = retrohunt_query_plan("hash", "deadbeef")
        assert result["seed_type"] == "hash"
        assert result["seed_value"] == "deadbeef"

    def test_default_days_is_thirty(self) -> None:
        result = retrohunt_query_plan("ip", "1.1.1.1")
        assert result["lookback_days"] == 30

    def test_zero_days(self) -> None:
        result = retrohunt_query_plan("ip", "1.1.1.1", days=0)
        assert result["lookback_days"] == 0

    def test_queries_count_is_three(self) -> None:
        result = retrohunt_query_plan("ip", "1.1.1.1")
        assert len(result["queries"]) == 3


# ---------------------------------------------------------------------------
# Edge cases and integration
# ---------------------------------------------------------------------------


class TestEdgeCasesAndIntegration:
    """Cross-cutting edge-case tests."""

    def test_build_seed_hunt_queries_all_seed_types_produce_three(self) -> None:
        seed_types = ["ip", "attacker_ip", "victim_ip", "user", "host", "domain", "hash", "process"]
        for st in seed_types:
            queries = build_seed_hunt_queries(st, "test_value")
            assert len(queries) == 3, f"Seed type {st} should produce 3 queries"

    def test_build_case_hunt_plan_preserves_order_by_entity(self) -> None:
        case = CaseRecord(
            case_id="C-ORD",
            title="Order test",
            entities={"attacker_ip": ["1.1.1.1"], "user": ["bob"]},
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-02T00:00:00Z",
        )
        queries = build_case_hunt_plan(case)
        # All attacker_ip queries should come before user queries
        first_user_idx = next(i for i, q in enumerate(queries) if q.seed_type == "user")
        last_ip_idx = max(i for i, q in enumerate(queries) if q.seed_type == "attacker_ip")
        assert last_ip_idx < first_user_idx

    def test_build_case_hunt_plan_route_with_empty_timeline(self) -> None:
        case = CaseRecord(
            case_id="C-ET",
            title="Empty timeline",
            entities={"host": ["box"]},
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-02T00:00:00Z",
        )
        route = RouteDraft(timeline=[])
        queries = build_case_hunt_plan(case, route)
        assert len(queries) > 0

    def test_hunt_query_limit_defaults_to_500(self) -> None:
        queries = build_seed_hunt_queries("ip", "1.1.1.1")
        for q in queries:
            assert q.limit == 500

    def test_retrohunt_with_none_seed_returns_empty_queries(self) -> None:
        result = retrohunt_query_plan(None, None)  # type: ignore[arg-type]
        assert result["queries"] == []
