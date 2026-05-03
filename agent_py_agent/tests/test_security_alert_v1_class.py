"""安全告警解析器测试 - security_alert_v1.py 安全告警模型、严重程度判断。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.log_analysis.parsers.base import ParserError
from agent_py_agent.agent.log_analysis.parsers.security_alert_v1 import SecurityAlertV1Parser


class TestSecurityAlertV1ParserInit:
    """SecurityAlertV1Parser 初始化测试。"""

    def test_parser_has_default_values(self):
        """验证解析器默认属性。"""
        parser = SecurityAlertV1Parser()
        assert parser.parser_id == "security_alert_v1"
        assert parser.schema == "SecurityAlertV1"
        assert parser.supported_formats == ("jsonl", "csv", "log")
        assert parser.payload_max_chars == 512

    def test_parser_with_custom_payload_max(self):
        """验证自定义 payload 最大字符数。"""
        parser = SecurityAlertV1Parser(payload_max_chars=1024)
        assert parser.payload_max_chars == 1024


class TestParseJsonLine:
    """parse_json_line 方法测试。"""

    def test_parse_valid_json_line(self):
        """解析有效的 JSON 行。"""
        parser = SecurityAlertV1Parser()
        line = json.dumps({"alert_id": "alert-1", "event_time": "2024-01-01T00:00:00Z", "source_id": "waf-1"})
        result = parser.parse_json_line(
            line,
            raw_ref="ref-1",
            source_id="waf",
            line_no=1,
        )
        assert result.parser_id == "security_alert_v1"
        assert result.event["alert_id"] == "alert-1"
        assert result.parser_confidence > 0

    def test_parse_invalid_json_raises_error(self):
        """无效 JSON 抛出 ParserError。"""
        parser = SecurityAlertV1Parser()
        with pytest.raises(ParserError, match="invalid JSON"):
            parser.parse_json_line("not json", raw_ref="ref-1")

    def test_parse_json_non_object_raises_error(self):
        """JSON 非对象时抛出 ParserError。"""
        parser = SecurityAlertV1Parser()
        with pytest.raises(ParserError, match="must contain a JSON object"):
            parser.parse_json_line('"just a string"', raw_ref="ref-1")


class TestParseRecord:
    """parse_record 方法测试。"""

    def test_parse_valid_record(self):
        """解析有效的记录。"""
        parser = SecurityAlertV1Parser()
        record = {
            "alert_id": "alert-123",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "waf-01",
            "alert_type": "SQL注入",
            "attacker_ip": "192.168.1.100",
            "victim_ip": "10.0.0.1",
        }
        result = parser.parse_record(
            record,
            raw_ref="log-001",
            source_product="waf",
            line_no=42,
        )
        assert result.event["alert_id"] == "alert-123"
        assert result.event["alert_type"] == "SQL注入"
        assert result.raw_ref == "log-001"
        assert result.line_no == 42

    def test_parse_non_mapping_raises_error(self):
        """非映射类型抛出 ParserError。"""
        parser = SecurityAlertV1Parser()
        with pytest.raises(ParserError, match="must be an object"):
            parser.parse_record(["list", "items"], raw_ref="ref-1")

    def test_parse_empty_record_raises_error(self):
        """空记录抛出 ParserError。"""
        parser = SecurityAlertV1Parser()
        with pytest.raises(ParserError, match="is empty"):
            parser.parse_record({}, raw_ref="ref-1")

    def test_parse_all_null_record_raises_error(self):
        """全是空值的记录抛出 ParserError。"""
        parser = SecurityAlertV1Parser()
        with pytest.raises(ParserError, match="is empty"):
            parser.parse_record({"a": None, "b": ""}, raw_ref="ref-1")


class TestParseCsvRow:
    """parse_csv_row 方法测试。"""

    def test_parse_csv_row_basic(self):
        """解析基本 CSV 行。"""
        parser = SecurityAlertV1Parser()
        row = {
            "alert_id": "csv-alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "ids-01",
        }
        result = parser.parse_csv_row(row, raw_ref="csv-ref")
        assert result.event["alert_id"] == "csv-alert-1"
        assert result.parser_id == "security_alert_v1"

    def test_parse_csv_row_with_extra_columns(self):
        """解析带额外列的 CSV 行。"""
        parser = SecurityAlertV1Parser()
        row = {
            "alert_id": "alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "waf-01",
            "alert_type": "XSS",
            None: "extra column marker",
        }
        with pytest.raises(ParserError, match="more columns than the header"):
            parser.parse_csv_row(row, raw_ref="ref-1")


class TestSecurityAlertNormalization:
    """安全告警规范化测试。"""

    def test_chinese_field_mapping(self):
        """验证中文字段映射。"""
        parser = SecurityAlertV1Parser()
        record = {
            "告警ID": "chn-alert-1",
            "事件时间": "2024-01-01T00:00:00Z",
            "告警类型": "SQL注入",
            "受害IP": "10.0.0.1",
            "攻击IP": "192.168.1.100",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        assert result.event["alert_id"] == "chn-alert-1"
        assert result.event["alert_type"] == "SQL注入"
        assert result.event["victim_ip"] == "10.0.0.1"
        assert result.event["attacker_ip"] == "192.168.1.100"

    def test_ip_semantic_copy(self):
        """验证 IP 语义复制（src_ip -> attacker_ip）。"""
        parser = SecurityAlertV1Parser()
        record = {
            "alert_id": "alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "ids-01",
            "src_ip": "1.2.3.4",
            "dst_ip": "5.6.7.8",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        # 验证 IP 语义双向复制
        assert result.event["attacker_ip"] == "1.2.3.4"
        assert result.event["victim_ip"] == "5.6.7.8"

    def test_port_coercion(self):
        """验证端口号类型强制转换。"""
        parser = SecurityAlertV1Parser()
        record = {
            "alert_id": "alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "fw-01",
            "dst_port": "8080",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        assert result.event["dst_port"] == 8080
        assert isinstance(result.event["dst_port"], int)

    def test_payload_truncation(self):
        """验证 payload 截断。"""
        parser = SecurityAlertV1Parser(payload_max_chars=10)
        record = {
            "alert_id": "alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "waf-01",
            "payload": "this is a very long payload",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        assert result.event["payload"] == "this is a "  # 保留截断位置的字符
        assert result.event["payload_truncated"] is True
        assert result.event["payload_original_size"] == 27

    def test_dedup_key_generation(self):
        """验证去重键生成。"""
        parser = SecurityAlertV1Parser()
        record = {
            "alert_id": "dedup-test",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "waf-01",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        assert result.event["dedup_key"].startswith("sha256:")
        assert result.event["event_id"].startswith("evt-")


class TestParserConfidence:
    """解析器置信度测试。"""

    def test_confidence_high_with_many_fields(self):
        """多字段时高置信度。"""
        parser = SecurityAlertV1Parser()
        record = {
            "alert_id": "alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "waf-01",
            "alert_type": "SQL注入",
            "attacker_ip": "1.2.3.4",
            "victim_ip": "5.6.7.8",
            "uri": "/api/login",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        assert result.parser_confidence >= 0.85

    def test_confidence_low_with_few_fields(self):
        """少字段时低置信度。"""
        from agent_py_agent.agent.log_analysis.parsers.common import parser_confidence

        raw_fields = {
            "field1": "v1",
            "field2": "v2",
            "field3": "v3",
            "field4": "v4",
            "field5": "v5",
        }
        mapping_source = {"field1": "alias"}
        conf = parser_confidence(raw_fields, mapping_source)
        # ratio = 1/5 = 0.2, mapped_count < 5, >= 1
        # confidence = min(0.86, 0.65 + 0.2*0.2) = min(0.86, 0.69) = 0.69
        assert conf < 0.7


class TestEdgeCases:
    """边界场景测试。"""

    def test_empty_string_values_are_filtered(self):
        """空字符串值被过滤。"""
        parser = SecurityAlertV1Parser()
        record = {
            "alert_id": "alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "",
            "alert_type": "SQL注入",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        assert result.event["source_id"] == "unknown"

    def test_whitespace_only_values_are_filtered(self):
        """纯空白值被过滤。"""
        parser = SecurityAlertV1Parser()
        record = {
            "alert_id": "alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "   ",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        assert result.event["source_id"] == "unknown"

    def test_extra_attributes_preserved(self):
        """额外属性被保留。"""
        parser = SecurityAlertV1Parser()
        record = {
            "alert_id": "alert-1",
            "event_time": "2024-01-01T00:00:00Z",
            "source_id": "waf-01",
            "custom_field": "custom_value",
        }
        result = parser.parse_record(record, raw_ref="ref-1")
        assert "custom_field" in result.event["attributes"]
