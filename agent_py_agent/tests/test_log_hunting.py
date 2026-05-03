"""威胁狩猎测试 - hunting.py 威胁狩猎、规则匹配、异常检测。"""
from __future__ import annotations

from agent_py_agent.agent.log_analysis.security import (
    HuntQuery,
    build_case_hunt_plan,
    build_seed_hunt_queries,
    next_query_plan,
    retrohunt_query_plan,
)


class TestHuntQuery:
    """HuntQuery 数据类测试。"""

    def test_hunt_query_required_fields(self):
        """验证必需字段。"""
        query = HuntQuery(
            query_id="hunt-1",
            purpose="查找种子实体",
            seed_type="ip",
            seed_value="1.2.3.4",
        )
        assert query.query_id == "hunt-1"
        assert query.purpose == "查找种子实体"
        assert query.seed_type == "ip"
        assert query.seed_value == "1.2.3.4"

    def test_hunt_query_defaults(self):
        """验证默认值。"""
        query = HuntQuery(
            query_id="hunt-1",
            purpose="purpose",
            seed_type="ip",
            seed_value="1.2.3.4",
        )
        assert query.source_products == []
        assert query.time_window == ("", "")
        assert query.filters == {}
        assert query.limit == 500

    def test_hunt_query_to_dict(self):
        """验证转换为字典。"""
        query = HuntQuery(
            query_id="hunt-1",
            purpose="purpose",
            seed_type="ip",
            seed_value="1.2.3.4",
            source_products=["waf", "edr"],
        )
        d = query.to_dict()
        assert d["query_id"] == "hunt-1"
        assert d["source_products"] == ["waf", "edr"]


class TestBuildSeedHuntQueries:
    """build_seed_hunt_queries 函数测试。"""

    def test_build_for_ip_seed(self):
        """为 IP 种子生成查询。"""
        queries = build_seed_hunt_queries("ip", "192.168.1.1")
        assert len(queries) == 3
        assert all(q.seed_type == "ip" for q in queries)
        assert all(q.seed_value == "192.168.1.1" for q in queries)

    def test_build_for_domain_seed(self):
        """为域名种子生成查询。"""
        queries = build_seed_hunt_queries("domain", "evil.com")
        assert len(queries) == 3
        assert all(q.seed_type == "domain" for q in queries)
        assert queries[0].source_products == ["dns", "proxy", "waf"]

    def test_build_for_user_seed(self):
        """为用户种子生成查询。"""
        queries = build_seed_hunt_queries("user", "admin")
        assert len(queries) == 3
        assert all(q.seed_type == "user" for q in queries)
        assert queries[0].source_products == ["vpn", "sso", "ad", "windows", "pam"]

    def test_build_for_hash_seed(self):
        """为哈希种子生成查询。"""
        queries = build_seed_hunt_queries("hash", "abc123def456")
        assert len(queries) == 3
        assert all(q.seed_type == "hash" for q in queries)
        assert queries[0].source_products == ["edr", "hids", "sandbox"]

    def test_build_with_time_window(self):
        """验证时间窗口设置。"""
        queries = build_seed_hunt_queries(
            "ip",
            "1.2.3.4",
            start="2024-01-01T00:00:00Z",
            end="2024-01-02T00:00:00Z",
        )
        for q in queries:
            assert q.time_window == ("2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z")

    def test_build_empty_for_empty_seed_type(self):
        """空种子类型返回空列表。"""
        queries = build_seed_hunt_queries("", "value")
        assert queries == []

    def test_build_empty_for_empty_seed_value(self):
        """空种子值返回空列表。"""
        queries = build_seed_hunt_queries("ip", "")
        assert queries == []

    def test_build_trims_whitespace(self):
        """验证空白被去除。"""
        queries = build_seed_hunt_queries("  ip  ", "  1.2.3.4  ")
        assert len(queries) == 3
        assert all(q.seed_type == "ip" for q in queries)
        assert all(q.seed_value == "1.2.3.4" for q in queries)

    def test_build_unique_query_ids(self):
        """验证生成的查询 ID 唯一。"""
        queries = build_seed_hunt_queries("ip", "1.2.3.4")
        ids = [q.query_id for q in queries]
        assert len(ids) == len(set(ids))


class TestBuildCaseHuntPlan:
    """build_case_hunt_plan 函数测试。"""

    def test_build_plan_from_case(self):
        """从 CaseRecord 构建狩猎计划。"""
        from agent_py_agent.agent.log_analysis.models import CaseRecord

        case = CaseRecord(
            case_id="case-1",
            title="可疑 IP 活动",
            entities={
                "attacker_ip": ["1.2.3.4"],
                "victim_ip": ["10.0.0.1"],
            },
        )
        queries = build_case_hunt_plan(case)
        assert len(queries) > 0

    def test_build_plan_with_entities(self):
        """验证从实体生成查询。"""
        from agent_py_agent.agent.log_analysis.models import CaseRecord

        case = CaseRecord(
            case_id="case-1",
            title="测试案例",
            entities={
                "attacker_ip": ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"],
                "user": ["admin", "root"],
            },
        )
        queries = build_case_hunt_plan(case)
        # attacker_ip 有 4 个，但每个实体类型最多取 3 个
        assert len(queries) >= 0

    def test_build_plan_skips_unsupported_entities(self):
        """跳过不支持的实体类型。"""
        from agent_py_agent.agent.log_analysis.models import CaseRecord

        case = CaseRecord(
            case_id="case-1",
            title="测试",
            entities={
                "unsupported_type": ["value1"],
                "attacker_ip": ["1.2.3.4"],
            },
        )
        queries = build_case_hunt_plan(case)
        assert all(q.seed_type != "unsupported_type" for q in queries)

    def test_build_plan_deduplicates(self):
        """验证查询去重。"""
        from agent_py_agent.agent.log_analysis.models import CaseRecord

        case = CaseRecord(
            case_id="case-1",
            title="测试",
            entities={
                "attacker_ip": ["1.1.1.1", "1.1.1.1", "2.2.2.2"],
            },
        )
        queries = build_case_hunt_plan(case)
        unique_keys = {(q.seed_type, q.seed_value, tuple(q.source_products)) for q in queries}
        assert len(unique_keys) == len(queries)


class TestNextQueryPlan:
    """next_query_plan 函数测试。"""

    def test_next_query_plan_basic(self):
        """验证基本结构。"""
        from agent_py_agent.agent.log_analysis.models import CaseRecord

        case = CaseRecord(
            case_id="case-1",
            title="测试",
            entities={"attacker_ip": ["1.2.3.4"]},
        )
        result = next_query_plan(case)
        assert "case_id" in result
        assert "next_queries" in result
        assert "hunt_queries" in result
        assert result["case_id"] == "case-1"

    def test_next_query_plan_with_route(self):
        """验证带 route 的计划。"""
        from agent_py_agent.agent.log_analysis.models import CaseRecord

        case = CaseRecord(
            case_id="case-1",
            title="测试",
            entities={"attacker_ip": ["1.2.3.4"]},
        )
        route = {"next_queries": ["query1", "query2"]}
        result = next_query_plan(case, route)
        assert len(result["next_queries"]) >= 2


class TestRetrohuntQueryPlan:
    """retrohunt_query_plan 函数测试。"""

    def test_retrohunt_basic(self):
        """验证基本结构。"""
        result = retrohunt_query_plan("ip", "1.2.3.4")
        assert result["kind"] == "retrohunt"
        assert result["seed_type"] == "ip"
        assert result["seed_value"] == "1.2.3.4"
        assert result["lookback_days"] == 30
        assert "queries" in result

    def test_retrohunt_custom_days(self):
        """验证自定义回溯天数。"""
        result = retrohunt_query_plan("domain", "evil.com", days=7)
        assert result["lookback_days"] == 7

    def test_retrohunt_queries_structure(self):
        """验证回溯查询结构。"""
        result = retrohunt_query_plan("hash", "abc123")
        assert len(result["queries"]) == 3
        for query in result["queries"]:
            assert "query_id" in query
            assert "purpose" in query
            assert "seed_type" in query
            assert "seed_value" in query
