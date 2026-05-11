"""子代理解析测试 - parsing.py JSON解析、字段提取、错误处理。"""
from __future__ import annotations

import json

from agent_py_agent.agent.subagents.parsing import (
    _dict_list,
    _extract_subagent_result_blocks,
    _int_value,
    _normalize_runner_items,
    _parse_runner_json_payload,
    _split_allowed_items,
    _string_dict,
    _string_list,
    _strip_json_fence,
    parse_parent_planner_output,
    parse_subagent_runner_output,
)


class TestParseSubagentRunnerOutput:
    """parse_subagent_runner_output 函数测试。"""

    def test_parse_valid_result_block(self):
        """解析包含有效结果块的文本。"""
        text = '[SUBAGENT_RESULT]\n{"status": "COMPLETED", "summary": "任务完成"}\n[/SUBAGENT_RESULT]'
        result = parse_subagent_runner_output(text)
        assert result.found is True
        assert result.ok is True
        assert result.status == "COMPLETED"
        assert result.summary == "任务完成"

    def test_parse_complete_json_without_end_marker(self):
        """缺少结束标记但 JSON 完整时仍可恢复解析。"""
        text = '[SUBAGENT_RESULT]\n{"status": "COMPLETED", "summary": "已完成"}\n'
        result = parse_subagent_runner_output(text)
        assert result.found is True
        assert result.ok is True
        assert result.status == "COMPLETED"
        assert result.summary == "已完成"

    def test_parse_incomplete_json_without_end_marker(self):
        """缺少结束标记且 JSON 不完整时返回错误。"""
        text = '[SUBAGENT_RESULT]\n{"status": "COMPLETED"\n'
        result = parse_subagent_runner_output(text)
        assert result.found is True
        assert result.ok is False
        assert "缺少" in result.parse_error and "[/SUBAGENT_RESULT]" in result.parse_error

    def test_parse_partial_success_with_traceable_evidence_packets(self):
        """结果尾部截断但 evidence_packets 已完整时，恢复最小可验收结果。"""
        text = """[SUBAGENT_RESULT]
{
  "status": "AWAITING_ACCEPTANCE",
  "summary": "10个文件已创建",
  "used_tools": ["write_file", "list_files"],
  "evidence_packets": [
    {"id": "evpkt-files", "claim": "文件已创建", "checked_scope": "build", "artifact_refs": ["/tmp/build/index.html"], "evidence_refs": [], "confidence": 1.0}
  ],
  "artifacts": [
    {"path": "/tmp/build/index.html", "kind": "file", "summary": "首页"},
"""
        result = parse_subagent_runner_output(text)
        assert result.found is True
        assert result.ok is True
        assert result.status == "AWAITING_ACCEPTANCE"
        assert result.summary == "10个文件已创建"
        assert result.used_tools == ["write_file", "list_files"]
        assert result.evidence_packets[0]["artifact_refs"] == ["/tmp/build/index.html"]

    def test_parse_partial_success_without_refs_stays_blocked(self):
        """没有可追溯 refs 的截断成功态不能被恢复成完成。"""
        text = """[SUBAGENT_RESULT]
{
  "status": "AWAITING_ACCEPTANCE",
  "summary": "我完成了",
  "evidence_packets": [
    {"id": "evpkt-no-refs", "claim": "完成", "checked_scope": "prose", "confidence": 1.0}
  ],
"""
        result = parse_subagent_runner_output(text)
        assert result.found is True
        assert result.ok is False
        assert "缺少" in result.parse_error and "[/SUBAGENT_RESULT]" in result.parse_error

    def test_parse_partial_success_with_cut_evidence_packet_array(self):
        """evidence_packets 数组尾部截断时，保留已闭合且带 refs 的证据包。"""
        text = """[SUBAGENT_RESULT]
{
  "status": "COMPLETED",
  "summary": "协调节点完成链路和文件检查",
  "evidence_packets": [
    {"id": "evpkt-files", "claim": "文件齐全", "checked_scope": "build", "artifact_refs": ["/tmp/build/index.html"], "evidence_refs": [], "confidence": 1.0},
    {"id": "evpkt-chain", "claim": "链路建立", "checked_scope": "subagents", "artifact_refs": [], "evidence_refs": ["subagent-run-id"], "confidence": 0.95},
    {"id": "evpkt-cut", "claim
"""
        result = parse_subagent_runner_output(text)
        assert result.found is True
        assert result.ok is True
        assert result.status == "COMPLETED"
        assert [item["id"] for item in result.evidence_packets] == ["evpkt-files", "evpkt-chain"]

    def test_parse_not_found(self):
        """完全不包含结果标记时返回未找到。"""
        text = "这只是普通文本"
        result = parse_subagent_runner_output(text)
        assert result.found is False
        assert result.ok is False

    def test_parse_invalid_json(self):
        """JSON格式错误时返回错误信息。"""
        text = '[SUBAGENT_RESULT]\nnot json\n[/SUBAGENT_RESULT]'
        result = parse_subagent_runner_output(text)
        assert result.found is True
        assert result.ok is False
        assert "JSON" in result.parse_error or "解析" in result.parse_error

    def test_parse_multiple_blocks_last_wins(self):
        """多个结果块时取最后一个。"""
        text = '''
        [SUBAGENT_RESULT]
        {"status": "FIRST"}
        [/SUBAGENT_RESULT]
        [SUBAGENT_RESULT]
        {"status": "LAST", "summary": "最后一个"}
        [/SUBAGENT_RESULT]
        '''
        result = parse_subagent_runner_output(text)
        assert result.status == "LAST"
        assert result.summary == "最后一个"

    def test_parse_with_markdown_fence(self):
        """支持 Markdown 代码块包裹的 JSON。"""
        text = '[SUBAGENT_RESULT]\n```json\n{"status": "OK"}\n```\n[/SUBAGENT_RESULT]'
        result = parse_subagent_runner_output(text)
        assert result.ok is True
        assert result.status == "OK"

    def test_parse_all_fields_extracted(self):
        """验证所有字段都被正确提取。"""
        text = '[SUBAGENT_RESULT]\n{"status": "COMPLETED", "summary": "摘要", "blocked_reason": "", "failure_type": "", "used_skills": ["s1"], "used_tools": ["t1"], "evidence": [{"k": "v"}], "evidence_packets": [{"claim": "c", "evidence_refs": ["e"]}], "findings": [{"claim": "f", "evidence_packet_ids": ["p"]}], "capability_requests": [], "artifacts": [], "tests": [], "patches": [], "lessons": ["l1"], "next_actions": ["a1"]}\n[/SUBAGENT_RESULT]'
        result = parse_subagent_runner_output(text)
        assert result.used_skills == ["s1"]
        assert result.used_tools == ["t1"]
        assert result.evidence == [{"k": "v"}]
        assert result.evidence_packets == [{"claim": "c", "evidence_refs": ["e"]}]
        assert result.findings == [{"claim": "f", "evidence_packet_ids": ["p"]}]
        assert result.lessons == ["l1"]
        assert result.next_actions == ["a1"]


class TestParseParentPlannerOutput:
    """parse_parent_planner_output 函数测试。"""

    def test_parse_valid_planner_result(self):
        """解析有效的父代理规划器结果。"""
        text = '[PARENT_PLANNER_RESULT]\n{"decision": "DISPATCH", "summary": "规划摘要"}\n[/PARENT_PLANNER_RESULT]'
        result = parse_parent_planner_output(text)
        assert result.found is True
        assert result.ok is True
        assert result.decision == "DISPATCH"
        assert result.summary == "规划摘要"

    def test_parse_decision_from_should_dispatch(self):
        """没有 decision 字段时根据 should_dispatch 推断。"""
        text = '[PARENT_PLANNER_RESULT]\n{"should_dispatch": true}\n[/PARENT_PLANNER_RESULT]'
        result = parse_parent_planner_output(text)
        assert result.decision == "DISPATCH"

    def test_parse_suggested_max_runners(self):
        """验证 suggested_max_runners 字段。"""
        text = '[PARENT_PLANNER_RESULT]\n{"suggested_max_runners": 5}\n[/PARENT_PLANNER_RESULT]'
        result = parse_parent_planner_output(text)
        assert result.suggested_max_runners == 5

    def test_parse_complete_planner_json_without_end_marker(self):
        """父级规划器缺少结束标记但 JSON 完整时仍可恢复。"""
        text = '[PARENT_PLANNER_RESULT]\n{"decision": "HEARTBEAT_OK", "summary": "已观察"}\n'
        result = parse_parent_planner_output(text)
        assert result.found is True
        assert result.ok is True
        assert result.decision == "HEARTBEAT_OK"
        assert result.summary == "已观察"


class TestStripJsonFence:
    """_strip_json_fence 函数测试。"""

    def test_no_fence(self):
        """无代码块时直接返回。"""
        text = '{"key": "value"}'
        assert _strip_json_fence(text) == text

    def test_json_fence_removed(self):
        """移除 ```json 包裹。"""
        text = '```json\n{"key": "value"}\n```'
        assert _strip_json_fence(text) == '{"key": "value"}'

    def test_triple_backticks_removed(self):
        """移除不带语言的 ``` 包裹。"""
        text = '```\n{"key": "value"}\n```'
        assert _strip_json_fence(text) == '{"key": "value"}'

    def test_whitespace_stripped(self):
        """验证首尾空白被清理。"""
        text = '  \n{"key": "value"}\n  '
        assert _strip_json_fence(text) == '{"key": "value"}'


class TestHelpers:
    """辅助函数测试。"""

    def test_dict_list_from_list(self):
        """_dict_list 处理列表输入。"""
        result = _dict_list([{"a": 1}, {"b": 2}])
        assert len(result) == 2
        assert result[0] == {"a": 1}

    def test_dict_list_from_non_list(self):
        """_dict_list 非列表输入返回空列表。"""
        assert _dict_list("not a list") == []
        assert _dict_list(None) == []

    def test_string_list_from_list(self):
        """_string_list 处理列表输入。"""
        result = _string_list(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_string_list_from_string(self):
        """_string_list 处理字符串输入。"""
        result = _string_list("single")
        assert result == ["single"]

    def test_string_list_filters_empty(self):
        """_string_list 过滤空白字符串。"""
        result = _string_list(["a", "", "  ", "b"])
        assert result == ["a", "b"]

    def test_string_list_non_list_returns_empty(self):
        """_string_list 非列表非字符串返回空。"""
        assert _string_list(123) == []

    def test_int_value_from_int(self):
        """_int_value 处理整数。"""
        assert _int_value(42) == 42

    def test_int_value_from_float(self):
        """_int_value 处理浮点数。"""
        assert _int_value(3.14) == 3

    def test_int_value_from_string(self):
        """_int_value 处理数字字符串。"""
        assert _int_value("42") == 42
        assert _int_value("3.7") == 3

    def test_int_value_from_bool(self):
        """_int_value 处理布尔值。"""
        assert _int_value(True) == 1
        assert _int_value(False) == 0

    def test_int_value_invalid_returns_zero(self):
        """_int_value 无效输入返回0。"""
        assert _int_value("not a number") == 0
        assert _int_value(None) == 0

    def test_split_allowed_items(self):
        """_split_allowed_items 拆分授权项。"""
        items = ["a", "b", "c", "d"]
        allowed = {"a", "c"}
        accepted, ignored = _split_allowed_items(items, allowed)
        assert accepted == ["a", "c"]
        assert ignored == ["b", "d"]

    def test_normalize_runner_items(self):
        """_normalize_runner_items 规范化 runner 项。"""
        items = [
            {"name": "item1", "count": 5, "active": True},
            {"name": "item2", "tags": ["t1", "t2"]},
        ]
        result = _normalize_runner_items(items)
        assert result[0]["name"] == "item1"
        assert result[0]["count"] == 5
        assert result[1]["tags"] == ["t1", "t2"]
