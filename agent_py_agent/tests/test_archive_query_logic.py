"""单元测试：memory_archive query 模块 - query_logic 查询逻辑"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.memory_archive.query import query_logic


class TestCollectArchiveRecords:
    """测试 collect_archive_records 收集归档记录"""

    def test_empty_result_when_no_files(self, tmp_path):
        """验证无文件时返回空列表"""
        result = query_logic.collect_archive_records(tmp_path, layer="raw", date_key=None, limit=10)
        assert result == []

    def test_limit_applied(self, tmp_path):
        """验证 limit 参数生效"""
        # 创建测试文件
        raw_dir = tmp_path / "audit"
        raw_dir.mkdir(parents=True)
        for i in range(20):
            (raw_dir / f"2026-05-{i+1:02d}.jsonl").write_text('{"event_id": "e"}\n')

        result = query_logic.collect_archive_records(tmp_path, layer="raw", date_key=None, limit=5)
        assert len(result) <= 5

    def test_level_filter_applied(self, tmp_path):
        """验证 level 过滤生效"""
        raw_dir = tmp_path / "audit"
        raw_dir.mkdir(parents=True)
        (raw_dir / "2026-05-01.jsonl").write_text(
            '{"event_id": "e1", "archive_level": 1}\n'
            '{"event_id": "e2", "archive_level": 3}\n'
        )

        result = query_logic.collect_archive_records(tmp_path, layer="raw", date_key=None, limit=10, level=1)
        assert all(r.get("archive_level") == 1 for r in result)


class TestArchiveFiltersFromArgs:
    """测试 archive_filters_from_args 参数过滤器构造"""

    def test_empty_args_returns_empty_filters(self):
        """验证空参数返回空过滤器"""
        args = MagicMock()
        args.session_id = ""
        args.request_id = ""
        args.run_id = ""
        args.task_id = ""
        args.speaker = ""
        args.target = ""
        args.action = ""
        args.status = ""
        args.tool_name = ""
        args.source = ""

        result = query_logic.archive_filters_from_args(args)
        assert result == {}

    def test_populated_args_returns_filters(self):
        """验证有值参数被正确提取"""
        args = MagicMock()
        args.session_id = "s1"
        args.request_id = "r1"
        args.run_id = ""
        args.task_id = ""
        args.speaker = ""
        args.target = ""
        args.action = ""
        args.status = ""
        args.tool_name = ""
        args.source = ""

        result = query_logic.archive_filters_from_args(args)
        assert result == {"session_id": "s1", "request_id": "r1"}


class TestFilterArchiveRecords:
    """测试 filter_archive_records 记录过滤"""

    def test_empty_query_returns_all(self):
        """验证空关键词返回全部记录"""
        records = [
            {"session_id": "s1", "created_at_sort": 100.0, "archive_level": 3},
            {"session_id": "s2", "created_at_sort": 200.0, "archive_level": 3},
        ]
        result = query_logic.filter_archive_records(records, query="", filters={}, since=None, until=None, level=None)
        assert len(result) == 2

    def test_field_filter_exact_match(self):
        """验证字段精确过滤"""
        records = [
            {"session_id": "s1", "created_at_sort": 100.0, "archive_level": 3},
            {"session_id": "s2", "created_at_sort": 200.0, "archive_level": 3},
        ]
        result = query_logic.filter_archive_records(records, query="", filters={"session_id": "s1"}, since=None, until=None, level=None)
        assert len(result) == 1
        assert result[0]["session_id"] == "s1"

    def test_time_window_filter(self):
        """验证时间窗口过滤"""
        records = [
            {"session_id": "s1", "created_at_sort": 100.0, "archive_level": 3},
            {"session_id": "s2", "created_at_sort": 200.0, "archive_level": 3},
            {"session_id": "s3", "created_at_sort": 300.0, "archive_level": 3},
        ]
        result = query_logic.filter_archive_records(records, query="", filters={}, since="1970-01-01", until="1970-01-03", level=None)
        # 所有记录都在 1970-01-01 到 1970-01-03 范围内
        assert len(result) == 3

    def test_query_keyword_match(self):
        """验证关键词匹配"""
        records = [
            {"session_id": "s1", "created_at_sort": 100.0, "content_preview": "hello world", "archive_level": 3},
            {"session_id": "s2", "created_at_sort": 200.0, "content_preview": "foo bar", "archive_level": 3},
        ]
        result = query_logic.filter_archive_records(records, query="hello", filters={}, since=None, until=None, level=None)
        assert len(result) == 1


class TestResumeLocalQuery:
    """测试 resume_local_query 本地查询构造"""

    def test_query_takes_priority(self):
        """验证 query 参数优先"""
        args = MagicMock()
        args.query = "my query"
        args.run_id = "run1"
        args.request_id = ""
        args.session_id = ""
        args.task_id = ""

        result = query_logic.resume_local_query(args, [])
        assert result == "my query"

    def test_run_id_used_when_no_query(self):
        """验证无 query 时使用 run_id"""
        args = MagicMock()
        args.query = ""
        args.run_id = "run1"
        args.request_id = ""
        args.session_id = ""
        args.task_id = ""

        result = query_logic.resume_local_query(args, [])
        assert result == "run1"

    def test_archive_match_fallback(self):
        """验证归档命中兜底"""
        args = MagicMock()
        args.query = ""
        args.run_id = ""
        args.request_id = ""
        args.session_id = ""
        args.task_id = ""

        archive_matches = [{"run_id": "archived_run"}]

        result = query_logic.resume_local_query(args, archive_matches)
        assert result == "archived_run"


class TestLocalHitPayload:
    """测试 local_hit_payload 本地命中序列化"""

    def test_serializes_all_fields(self):
        """验证所有字段被正确序列化"""
        hit = MagicMock()
        hit.id = "h1"
        hit.source_type = "subagent_run"
        hit.source_id = "sub-123"
        hit.title = "Test Task"
        hit.content = "Long content" + "x" * 600
        hit.metadata = {"key": "value"}
        hit.visibility = "private"
        hit.updated_at = "2026-05-01"
        hit.content_path = "/path/to/content"

        result = query_logic.local_hit_payload(hit)

        assert result["id"] == "h1"
        assert result["source_type"] == "subagent_run"
        assert result["title"] == "Test Task"
        assert len(result["content_preview"]) == 500  # 截断到 500


class TestCollectResumeTaskIds:
    """测试 collect_resume_task_ids 恢复任务 ID 收集"""

    def test_extracts_from_args(self):
        """验证从参数提取任务 ID"""
        args = MagicMock()
        args.run_id = "subagent-123"
        args.task_id = "subagent-456"

        result = query_logic.collect_resume_task_ids(args, [], [])
        assert "subagent-123" in result
        assert "subagent-456" in result

    def test_extracts_from_archive_matches(self):
        """验证从归档命中提取任务 ID"""
        args = MagicMock()
        args.run_id = ""
        args.task_id = ""

        archive_matches = [
            {"run_id": "subagent-789", "task_refs": ["subagent-000"]},
        ]

        result = query_logic.collect_resume_task_ids(args, archive_matches, [])
        assert "subagent-789" in result
        assert "subagent-000" in result

    def test_deduplication(self):
        """验证去重"""
        args = MagicMock()
        args.run_id = "subagent-1"
        args.task_id = ""

        archive_matches = [{"run_id": "subagent-1"}]

        result = query_logic.collect_resume_task_ids(args, archive_matches, [])
        # 去重后只有一个
        assert result.count("subagent-1") == 1


class TestBuildResumeGuidance:
    """测试 build_resume_guidance 恢复指导构建"""

    def test_empty_payloads(self):
        """验证空负载返回基础指导"""
        result = query_logic.build_resume_guidance(query_logic.ResumeGuidanceRequest([], [], [], None))

        assert "archive_match_count" in result
        assert "recommended_read_paths" in result
        assert "next_actions" in result
        assert len(result["next_actions"]) >= 2

    def test_task_payloads_included(self):
        """验证任务负载被包含"""
        task_payloads = [
            {
                "run_id": "sub-1",
                "recommended_read_paths": ["/path/status.md", "/path/work_log.md"],
            }
        ]

        result = query_logic.build_resume_guidance(query_logic.ResumeGuidanceRequest([], [], task_payloads, None))

        assert result["task_fact_source_count"] == 1
        assert "/path/status.md" in result["recommended_read_paths"]

    def test_gateway_payloads_included(self):
        """验证 gateway 负载被包含"""
        gateway_payloads = [
            {
                "request_id": "gwreq-1",
                "recommended_read_paths": ["/path/request.json"],
            }
        ]

        result = query_logic.build_resume_guidance(query_logic.ResumeGuidanceRequest([], [], [], gateway_payloads))

        assert result["gateway_fact_source_count"] == 1

    def test_invalid_authority_flagged(self):
        """验证无效权威文件被标记"""
        task_payloads = [
            {
                "run_id": "sub-bad",
                "authority_validation": {"ok": False, "missing_paths": ["/missing/file"]},
            }
        ]

        result = query_logic.build_resume_guidance(query_logic.ResumeGuidanceRequest([], [], task_payloads, None))

        assert any("sub-bad" in action for action in result["next_actions"])


class TestStripSortKeys:
    """测试 strip_sort_keys 排序键剥离"""

    def test_removes_from_dict(self):
        """验证从字典移除排序键"""
        payload = {"name": "test", "created_at_sort": 123.0, "value": 1}
        result = query_logic.strip_sort_keys(payload)

        assert "created_at_sort" not in result
        assert result["name"] == "test"

    def test_removes_from_list(self):
        """验证从列表移除排序键"""
        payload = [
            {"name": "a", "created_at_sort": 1.0},
            {"name": "b", "created_at_sort": 2.0},
        ]
        result = query_logic.strip_sort_keys(payload)

        assert all("created_at_sort" not in item for item in result)

    def test_preserves_other_values(self):
        """验证其他值被保留"""
        payload = {"name": "test", "count": 5, "active": True}
        result = query_logic.strip_sort_keys(payload)

        assert result["name"] == "test"
        assert result["count"] == 5
        assert result["active"] is True

    def test_nested_structures(self):
        """验证嵌套结构处理"""
        payload = {
            "outer": {"inner": "value", "created_at_sort": 1.0},
            "list": [{"item": 1, "created_at_sort": 2.0}],
        }
        result = query_logic.strip_sort_keys(payload)

        assert "created_at_sort" not in result["outer"]
        assert "created_at_sort" not in result["list"][0]
