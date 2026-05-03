"""单元测试：memory_archive resume_brief 恢复摘要生成"""

from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.memory_archive.resume_brief import (
    _append,
    _context_block,
    _dedupe,
    _latest_assistant_actions,
    _latest_user_intents,
    _likely_task_statuses,
    _related_ids,
    _string_list,
    _summary_line,
    build_resume_brief,
)


class TestBuildResumeBrief:
    """测试 build_resume_brief 主函数"""

    def test_empty_inputs(self):
        """验证空输入返回基础结构"""
        result = build_resume_brief(
            archive_matches=[],
            local_hits=[],
            task_payloads=[],
            recommended_read_paths=[],
            next_actions=[],
        )

        assert "latest_user_intent" in result
        assert "latest_assistant_action" in result
        assert "related_ids" in result
        assert "likely_task_statuses" in result
        assert "recommended_read_paths" in result
        assert "next_actions" in result
        assert "summary" in result
        assert "context_block" in result

    def test_with_archive_matches(self):
        """验证有归档匹配时提取信息"""
        archive_matches = [
            {
                "speaker": "user",
                "content_preview": "用户想要完成某事",
                "payload": {
                    "user_intents": ["完成代码任务"],
                },
            },
            {
                "speaker": "assistant",
                "content_preview": "助手开始执行",
                "payload": {
                    "assistant_actions": ["执行代码"],
                },
            },
        ]

        result = build_resume_brief(
            archive_matches=archive_matches,
            local_hits=[],
            task_payloads=[],
            recommended_read_paths=["/path/to/status.md"],
            next_actions=["查看任务状态"],
        )

        assert result["latest_user_intent"] == "完成代码任务"
        assert result["latest_assistant_action"] == "执行代码"

    def test_with_local_hits(self):
        """验证有 LocalStore 命中时提取 ID"""
        local_hits = [
            {
                "source_id": "subagent-123",
                "source_type": "subagent_run",
                "metadata": {
                    "request_id": "gwreq-456",
                    "run_id": "subagent-123",
                },
            },
        ]

        result = build_resume_brief(
            archive_matches=[],
            local_hits=local_hits,
            task_payloads=[],
            recommended_read_paths=[],
            next_actions=[],
        )

        assert "subagent-123" in result["related_ids"]["run_ids"]

    def test_with_task_payloads(self):
        """验证有任务负载时提取状态"""
        task_payloads = [
            {
                "run_id": "subagent-789",
                "exists": True,
                "status": "RUNNING",
                "verification_status": "UNVERIFIED",
                "goal": "测试目标",
                "task_dir": "/tmp/task",
            },
        ]

        result = build_resume_brief(
            archive_matches=[],
            local_hits=[],
            task_payloads=task_payloads,
            recommended_read_paths=[],
            next_actions=[],
        )

        assert len(result["likely_task_statuses"]) == 1
        assert result["likely_task_statuses"][0]["run_id"] == "subagent-789"

    def test_recommended_read_paths_limit(self):
        """验证推荐阅读路径限制"""
        paths = [f"/path/{i}.md" for i in range(30)]

        result = build_resume_brief(
            archive_matches=[],
            local_hits=[],
            task_payloads=[],
            recommended_read_paths=paths,
            next_actions=[],
        )

        assert len(result["recommended_read_paths"]) == 20

    def test_context_block_format(self):
        """验证 context_block 格式"""
        result = build_resume_brief(
            archive_matches=[],
            local_hits=[],
            task_payloads=[],
            recommended_read_paths=["/status.md"],
            next_actions=["查看状态"],
        )

        assert result["context_block"].startswith("# Recovery Brief")
        assert "authority:" in result["context_block"]
        assert "must_read:" in result["context_block"]

    def test_summary_line_generation(self):
        """验证 summary 行生成"""
        result = build_resume_brief(
            archive_matches=[
                {
                    "speaker": "user",
                    "content_preview": "用户意图",
                    "payload": {},
                },
            ],
            local_hits=[],
            task_payloads=[],
            recommended_read_paths=[],
            next_actions=[],
        )

        assert "latest_user_intent:" in result["summary"]
        # 如果没有 assistant 内容，该行不会被添加
        assert isinstance(result["summary"], str)


class TestLatestUserIntents:
    """测试 _latest_user_intents 用户意图提取"""

    def test_extracts_from_hook_payload(self):
        """验证从 hook payload 提取"""
        records = [
            {
                "payload": {
                    "user_intents": ["意图1", "意图2"],
                },
            },
        ]
        result = _latest_user_intents(records)
        assert "意图1" in result
        assert "意图2" in result

    def test_extracts_from_raw_user_messages(self):
        """验证从 raw user 消息提取"""
        records = [
            {
                "speaker": "user",
                "content_preview": "用户消息内容",
            },
        ]
        result = _latest_user_intents(records)
        assert "用户消息内容" in result

    def test_deduplicates(self):
        """验证去重"""
        records = [
            {"speaker": "user", "content_preview": "内容A"},
            {"speaker": "user", "content_preview": "内容A"},  # 重复
            {"speaker": "user", "content_preview": "内容B"},
        ]
        result = _latest_user_intents(records)
        assert result.count("内容A") == 1

    def test_empty_input(self):
        """验证空输入"""
        result = _latest_user_intents([])
        assert result == []


class TestLatestAssistantActions:
    """测试 _latest_assistant_actions 助手动作提取"""

    def test_extracts_from_hook_payload(self):
        """验证从 hook payload 提取"""
        records = [
            {
                "payload": {
                    "assistant_actions": ["动作1", "动作2"],
                },
            },
        ]
        result = _latest_assistant_actions(records)
        assert "动作1" in result
        assert "动作2" in result

    def test_extracts_from_raw_assistant_messages(self):
        """验证从 raw assistant 消息提取"""
        records = [
            {
                "speaker": "assistant",
                "content_preview": "助手回复内容",
            },
        ]
        result = _latest_assistant_actions(records)
        assert "助手回复内容" in result

    def test_empty_input(self):
        """验证空输入"""
        result = _latest_assistant_actions([])
        assert result == []


class TestRelatedIds:
    """测试 _related_ids 关联 ID 收集"""

    def test_extracts_from_archive_matches(self):
        """验证从归档匹配提取"""
        archive_matches = [
            {
                "session_id": "sess-1",
                "request_id": "req-1",
                "run_id": "run-1",
                "task_id": "task-1",
            },
        ]
        result = _related_ids(archive_matches, [], [])
        assert "sess-1" in result["session_ids"]
        assert "req-1" in result["request_ids"]
        assert "run-1" in result["run_ids"]

    def test_extracts_gwreq_from_local_hits(self):
        """验证从 LocalStore 命中提取 gwreq ID"""
        local_hits = [
            {
                "source_id": "gwreq-123",
                "metadata": {},
            },
        ]
        result = _related_ids([], local_hits, [])
        assert "gwreq-123" in result["request_ids"]

    def test_extracts_subagent_from_local_hits(self):
        """验证从 LocalStore 命中提取 subagent ID"""
        local_hits = [
            {
                "source_id": "subagent-456",
                "source_type": "subagent_run",
                "metadata": {},
            },
        ]
        result = _related_ids([], local_hits, [])
        assert "subagent-456" in result["run_ids"]
        assert "subagent-456" in result["task_ids"]

    def test_extracts_from_task_payloads(self):
        """验证从任务负载提取"""
        task_payloads = [
            {
                "run_id": "subagent-789",
                "status": "RUNNING",
            },
        ]
        result = _related_ids([], [], task_payloads)
        assert "subagent-789" in result["run_ids"]
        assert "subagent-789" in result["task_ids"]

    def test_empty_input(self):
        """验证空输入"""
        result = _related_ids([], [], [])
        assert result["session_ids"] == []
        assert result["request_ids"] == []


class TestLikelyTaskStatuses:
    """测试 _likely_task_statuses 任务状态推断"""

    def test_extracts_task_info(self):
        """验证提取任务信息"""
        task_payloads = [
            {
                "run_id": "sub-1",
                "exists": True,
                "status": "COMPLETED",
                "verification_status": "VERIFIED",
                "goal": "完成目标",
                "task_dir": "/path/to/task",
            },
        ]
        result = _likely_task_statuses(task_payloads)
        assert len(result) == 1
        assert result[0]["run_id"] == "sub-1"
        assert result[0]["status"] == "COMPLETED"
        assert result[0]["exists"] == "true"

    def test_missing_task_defaults(self):
        """验证缺失任务的默认值"""
        task_payloads = [
            {
                "run_id": "sub-missing",
                "exists": False,
            },
        ]
        result = _likely_task_statuses(task_payloads)
        assert result[0]["exists"] == "false"
        assert result[0]["status"] == "missing"
        assert result[0]["verification_status"] == "unknown"


class TestContextBlock:
    """测试 _context_block 上下文块生成"""

    def test_basic_format(self):
        """验证基本格式"""
        result = _context_block(
            latest_user_intents=["用户意图"],
            latest_assistant_actions=["助手动作"],
            related_ids={"run_ids": ["run-1"]},
            likely_task_statuses=[],
            recommended_read_paths=["/path/file.md"],
            next_actions=["查看状态"],
            authority_note="测试权威说明",
        )

        assert "# Recovery Brief" in result
        assert "authority: 测试权威说明" in result
        assert "latest_user_intent: 用户意图" in result

    def test_empty_intents(self):
        """验证空意图时的处理"""
        result = _context_block(
            latest_user_intents=[],
            latest_assistant_actions=[],
            related_ids={"run_ids": []},
            likely_task_statuses=[],
            recommended_read_paths=[],
            next_actions=[],
            authority_note="note",
        )

        assert "latest_user_intent: unknown" in result
        assert "latest_assistant_action: unknown" in result

    def test_task_statuses_listed(self):
        """验证任务状态列表"""
        result = _context_block(
            latest_user_intents=[],
            latest_assistant_actions=[],
            related_ids={"run_ids": []},
            likely_task_statuses=[
                {"run_id": "sub-1", "status": "RUNNING", "verification_status": "UNVERIFIED", "goal": "目标"},
            ],
            recommended_read_paths=[],
            next_actions=[],
            authority_note="note",
        )

        assert "sub-1 RUNNING/UNVERIFIED" in result

    def test_must_read_paths(self):
        """验证必须阅读路径"""
        result = _context_block(
            latest_user_intents=[],
            latest_assistant_actions=[],
            related_ids={"run_ids": []},
            likely_task_statuses=[],
            recommended_read_paths=["/status.md", "/log.txt"],
            next_actions=[],
            authority_note="note",
        )

        assert "- /status.md" in result
        assert "- /log.txt" in result


class TestSummaryLine:
    """测试 _summary_line 单行摘要生成"""

    def test_with_values(self):
        """验证有值时输出"""
        result = _summary_line("label", ["value1"])
        assert result == "label: value1"

    def test_without_values(self):
        """验证无值时返回空字符串"""
        result = _summary_line("label", [])
        assert result == ""


class TestStringList:
    """测试 _string_list 字符串列表标准化"""

    def test_list_input(self):
        """验证列表输入"""
        result = _string_list(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_string_input(self):
        """验证字符串输入"""
        result = _string_list("single")
        assert result == ["single"]

    def test_empty_string(self):
        """验证空字符串"""
        result = _string_list("")
        assert result == []

    def test_strips_whitespace(self):
        """验证去除空白"""
        result = _string_list(["  a  ", "  b"])
        assert "a" in result
        assert "b" in result


class TestDedupe:
    """测试 _dedupe 字符串去重"""

    def test_removes_duplicates(self):
        """验证移除重复"""
        result = _dedupe(["a", "b", "a", "c", "b"])
        assert result == ["a", "b", "c"]

    def test_preserves_order(self):
        """验证保留顺序"""
        result = _dedupe(["first", "second", "third"])
        assert result == ["first", "second", "third"]

    def test_ignores_empty_strings(self):
        """验证忽略空字符串"""
        result = _dedupe(["a", "", "b", ""])
        assert result == ["a", "b"]


class TestAppend:
    """测试 _append 单项追加"""

    def test_appends_non_empty(self):
        """验证追加非空值"""
        items = []
        _append(items, "value")
        assert items == ["value"]

    def test_skips_empty(self):
        """验证跳过空值"""
        items = ["existing"]
        _append(items, "")
        _append(items, None)
        assert items == ["existing"]

    def test_skips_duplicates(self):
        """验证跳过重复"""
        items = ["value"]
        _append(items, "value")
        assert items == ["value"]