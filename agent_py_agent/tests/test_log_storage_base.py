"""测试 log_analysis storage/base.py

测试存储层基础功能：
- utc_now: 当前 UTC 时间字符串
- model_to_dict/dict_to_model: 模型转换
- record_identity: 记录标识生成
- stable_digest/canonical_json: 哈希和规范化
- parse_event_time/event_time_value: 时间解析
- evidence_path_from_ref: 证据路径提取
- normalize_limit: 限制值规范化
- nested_get: 嵌套取值
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_py_agent.agent.log_analysis.storage.base import (
    DEFAULT_PREVIEW_LIMIT,
    DEFAULT_QUERY_LIMIT,
    MAX_QUERY_LIMIT,
    SUPPORTED_QUERY_FIELDS,
    JsonlReadAudit,
    QueryCriteria,
    QueryRecord,
    canonical_json,
    dict_to_model,
    event_time_value,
    evidence_path_from_ref,
    model_to_dict,
    nested_get,
    normalize_limit,
    parse_event_time,
    record_identity,
    stable_digest,
    utc_now,
)

# ============================================================
# 测试用例：时间函数
# ============================================================

class TestUtcNow:
    """测试 utc_now 函数"""

    def test_returns_string(self):
        """测试返回字符串"""
        result = utc_now()
        assert isinstance(result, str)

    def test_contains_z_suffix(self):
        """测试包含 Z 后缀"""
        result = utc_now()
        assert "Z" in result or "+00:00" in result

    def test_valid_iso_format(self):
        """测试有效的 ISO 格式"""
        result = utc_now()
        # 应该能被解析
        dt = datetime.fromisoformat(result.replace("Z", "+00:00"))
        assert dt.tzinfo is not None


# ============================================================
# 测试用例：model_to_dict
# ====================================

class TestModelToDict:
    """测试 model_to_dict 函数"""

    def test_none_input(self):
        """测试 None 输入"""
        assert model_to_dict(None) == {}

    def test_dict_input(self):
        """测试字典输入"""
        result = model_to_dict({"key": "value"})
        assert result == {"key": "value"}

    def test_dataclass_input(self):
        """测试 dataclass 输入"""
        @dataclass
        class TestData:
            name: str = "test"
            value: int = 42
        result = model_to_dict(TestData())
        assert result["name"] == "test"
        assert result["value"] == 42

    def test_object_with_to_dict(self):
        """测试有 to_dict 方法的对象"""
        class WithDict:
            def to_dict(self):
                return {"custom": "dict"}
        result = model_to_dict(WithDict())
        assert result == {"custom": "dict"}

    def test_object_with_model_dump(self):
        """测试有 model_dump 方法的对象（Pydantic 风格）"""
        class WithModelDump:
            def model_dump(self):
                return {"pydantic": "style"}
        result = model_to_dict(WithModelDump())
        assert result == {"pydantic": "style"}

    def test_nested_dataclass(self):
        """测试嵌套 dataclass"""
        @dataclass
        class Inner:
            x: int = 1
        @dataclass
        class Outer:
            inner: Inner
            name: str = "outer"
        result = model_to_dict(Outer(inner=Inner()))
        assert result["name"] == "outer"
        assert result["inner"]["x"] == 1

    def test_path_converted_to_string(self):
        """测试 Path 对象转为字符串（通过 __dict__ 处理）"""
        class PathHolder:
            def __init__(self):
                self.path = Path("/some/path")
        result = model_to_dict(PathHolder())
        assert result["path"] == "/some/path"

    def test_datetime_converted_to_iso(self):
        """测试 datetime 转为 ISO 格式（通过 __dict__ 处理）"""
        class DatetimeHolder:
            def __init__(self):
                self.dt = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = model_to_dict(DatetimeHolder())
        assert "2026-05-01" in result["dt"]

    def test_falls_back_to___dict__(self):
        """测试回退到 __dict__"""
        class Custom:
            def __init__(self):
                self.name = "test"
                self.value = 42
        result = model_to_dict(Custom())
        assert result["name"] == "test"
        assert result["value"] == 42


# ============================================================
# 测试用例：dict_to_model
# ====================================

class TestDictToModel:
    """测试 dict_to_model 函数"""

    def test_dataclass_with_from_dict(self):
        """测试有 from_dict 的 dataclass"""
        @dataclass
        class HasFromDict:
            name: str = ""
            value: int = 0
            @classmethod
            def from_dict(cls, data):
                return cls(**data)
        result = dict_to_model(HasFromDict, {"name": "test", "value": 10})
        assert result.name == "test"
        assert result.value == 10

    def test_dataclass_without_from_dict(self):
        """测试没有 from_dict 的 dataclass"""
        @dataclass
        class SimpleDataclass:
            name: str = ""
            value: int = 0
        result = dict_to_model(SimpleDataclass, {"name": "simple", "value": 5})
        assert result.name == "simple"
        assert result.value == 5

    def test_extra_fields_filtered(self):
        """测试额外字段被过滤"""
        @dataclass
        class OnlyTwo:
            name: str = ""
            value: int = 0
        result = dict_to_model(OnlyTwo, {"name": "extra", "value": 1, "extra": "ignored"})
        assert not hasattr(result, "extra")

    def test_dict_input_passthrough(self):
        """测试字典输入直通"""
        data = {"key": "value"}
        result = dict_to_model(dict, data)
        assert result == data


# ============================================================
# 测试用例：record_identity
# ====================================

class TestRecordIdentity:
    """测试 record_identity 函数"""

    def test_uses_first_available_id_field(self):
        """测试使用第一个可用的 ID 字段"""
        payload = {"case_id": "case-001", "other": "data"}
        result = record_identity(payload, ["case_id", "dedup_key"])
        assert result == "case-001"

    def test_fallback_to_digest(self):
        """测试降级到 digest"""
        payload = {"no": "id_fields", "data": "value"}
        result = record_identity(payload, ["case_id", "dedup_key"])
        assert result.startswith("sha256:")

    def test_nested_field_access(self):
        """测试嵌套字段访问"""
        payload = {"attributes": {"case_id": "nested-case"}}
        result = record_identity(payload, ["case_id"])
        assert result == "nested-case"

    def test_empty_id_fields(self):
        """测试空 ID 字段列表"""
        payload = {"key": "value"}
        result = record_identity(payload, [])
        assert result.startswith("sha256:")


# ============================================================
# 测试用例：stable_digest
# ====================================

class TestStableDigest:
    """测试 stable_digest 函数"""

    def test_same_input_same_output(self):
        """测试相同输入产生相同输出"""
        data = {"key": "value", "number": 42}
        result1 = stable_digest(data)
        result2 = stable_digest(data)
        assert result1 == result2

    def test_different_input_different_output(self):
        """测试不同输入产生不同输出"""
        result1 = stable_digest({"a": 1})
        result2 = stable_digest({"b": 2})
        assert result1 != result2

    def test_order_independent(self):
        """测试顺序无关"""
        result1 = stable_digest({"a": 1, "b": 2})
        result2 = stable_digest({"b": 2, "a": 1})
        assert result1 == result2

    def test_returns_hex_string(self):
        """测试返回十六进制字符串"""
        result = stable_digest({"test": "data"})
        assert all(c in "0123456789abcdef" for c in result)


# ============================================================
# 测试用例：canonical_json
# ====================================

class TestCanonicalJson:
    """测试 canonical_json 函数"""

    def test_returns_json_string(self):
        """测试返回 JSON 字符串"""
        result = canonical_json({"key": "value"})
        assert isinstance(result, str)
        # 应该能被解析
        assert json.loads(result) == {"key": "value"}

    def test_sorted_keys(self):
        """测试键排序"""
        result = canonical_json({"b": 1, "a": 2})
        # 键应该按字母顺序
        assert '"a":' in result
        assert '"b":' in result
        a_pos = result.find('"a":')
        b_pos = result.find('"b":')
        assert a_pos < b_pos


# ============================================================
# 测试用例：nested_get
# ====================================

class TestNestedGet:
    """测试 nested_get 函数"""

    def test_direct_key(self):
        """测试直接键访问"""
        assert nested_get({"key": "value"}, "key") == "value"

    def test_missing_key(self):
        """测试缺失键返回 None"""
        assert nested_get({"key": "value"}, "missing") is None

    def test_nested_in_attributes(self):
        """测试嵌套在 attributes 中"""
        payload = {"attributes": {"inner": "nested-value"}}
        assert nested_get(payload, "inner") == "nested-value"

    def test_nested_in_raw_fields(self):
        """测试嵌套在 raw_fields 中"""
        payload = {"raw_fields": {"field": "raw-value"}}
        assert nested_get(payload, "field") == "raw-value"

    def test_nested_in_metadata(self):
        """测试嵌套在 metadata 中"""
        payload = {"metadata": {"meta": "data-value"}}
        assert nested_get(payload, "meta") == "data-value"

    def test_priority_direct_key(self):
        """测试直接键优先"""
        payload = {"key": "direct", "attributes": {"key": "nested"}}
        assert nested_get(payload, "key") == "direct"

    def test_with_default(self):
        """测试带默认值"""
        assert nested_get({}, "missing") is None


# ============================================================
# 测试用例：parse_event_time
# ====================================

class TestParseEventTime:
    """测试 parse_event_time 函数"""

    def test_datetime_input(self):
        """测试 datetime 输入"""
        dt = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = parse_event_time(dt)
        assert result is not None

    def test_none_input(self):
        """测试 None 输入"""
        assert parse_event_time(None) is None

    def test_empty_string(self):
        """测试空字符串"""
        assert parse_event_time("") is None

    def test_iso_format_with_z(self):
        """测试带 Z 的 ISO 格式"""
        result = parse_event_time("2026-05-01T12:00:00Z")
        assert result is not None
        assert result.tzinfo is not None

    def test_iso_format_with_offset(self):
        """测试带时区偏移的 ISO 格式"""
        result = parse_event_time("2026-05-01T12:00:00+00:00")
        assert result is not None

    def test_invalid_format(self):
        """测试无效格式"""
        assert parse_event_time("not-a-date") is None

    def test_timestamp_float(self):
        """测试时间戳浮点数"""
        result = parse_event_time(1714564800.0)
        assert result is not None
        assert result.year == 2024

    def test_naive_datetime_gets_utc(self):
        """测试无时区 datetime 被当作 UTC"""
        result = parse_event_time("2026-05-01T12:00:00")
        assert result is not None


# ============================================================
# 测试用例：event_time_value
# ====================================

class TestEventTimeValue:
    """测试 event_time_value 函数"""

    def test_event_time_field(self):
        """测试 event_time 字段"""
        payload = {"event_time": "2026-05-01T12:00:00Z"}
        assert event_time_value(payload) == "2026-05-01T12:00:00Z"

    def test_timestamp_field(self):
        """测试 timestamp 字段"""
        payload = {"timestamp": "2026-05-01T12:00:00Z"}
        assert event_time_value(payload) == "2026-05-01T12:00:00Z"

    def test_time_field(self):
        """测试 time 字段"""
        payload = {"time": "2026-05-01T12:00:00Z"}
        assert event_time_value(payload) == "2026-05-01T12:00:00Z"

    def test_ingest_time_field(self):
        """测试 ingest_time 字段"""
        payload = {"ingest_time": "2026-05-01T12:00:00Z"}
        assert event_time_value(payload) == "2026-05-01T12:00:00Z"

    def test_priority_event_time_first(self):
        """测试 event_time 优先"""
        payload = {
            "event_time": "event-time",
            "timestamp": "timestamp-value",
            "time": "time-value",
        }
        assert event_time_value(payload) == "event-time"

    def test_all_missing_returns_none(self):
        """测试全部缺失返回 None"""
        assert event_time_value({}) is None


# ============================================================
# 测试用例：evidence_path_from_ref
# ====================================

class TestEvidencePathFromRef:
    """测试 evidence_path_from_ref 函数"""

    def test_path_field(self):
        """测试 path 字段"""
        ref = {"path": "/evidence/data.json"}
        assert evidence_path_from_ref(ref) == "/evidence/data.json"

    def test_uri_field(self):
        """测试 uri 字段"""
        ref = {"uri": "file:///evidence/data.json"}
        assert evidence_path_from_ref(ref) == "file:///evidence/data.json"

    def test_metadata_evidence_path(self):
        """测试 metadata.evidence_path"""
        ref = {"metadata": {"evidence_path": "/meta/path.json"}}
        assert evidence_path_from_ref(ref) == "/meta/path.json"

    def test_empty_ref(self):
        """测试空引用"""
        assert evidence_path_from_ref({}) == ""

    def test_metadata_evidence_path_checked_first(self):
        """测试 metadata.evidence_path 在直接字段之前被检查"""
        ref = {"path": "/direct/path", "metadata": {"evidence_path": "/meta/path"}}
        # metadata.evidence_path 被先检查，所以返回 metadata 中的值
        assert evidence_path_from_ref(ref) == "/meta/path"


# ============================================================
# 测试用例：normalize_limit
# ====================================

class TestNormalizeLimit:
    """测试 normalize_limit 函数"""

    def test_none_returns_default(self):
        """测试 None 返回默认限制"""
        assert normalize_limit(None) == DEFAULT_QUERY_LIMIT

    def test_zero_returns_default(self):
        """测试 0 返回默认限制"""
        assert normalize_limit(0) == DEFAULT_QUERY_LIMIT

    def test_negative_returns_default(self):
        """测试负数返回默认限制"""
        assert normalize_limit(-5) == DEFAULT_QUERY_LIMIT

    def test_positive_capped_at_max(self):
        """测试正值被上限截断"""
        result = normalize_limit(1000)
        assert result <= MAX_QUERY_LIMIT

    def test_positive_within_limit(self):
        """测试有效正值"""
        result = normalize_limit(50)
        assert result == 50

    def test_custom_max_limit(self):
        """测试自定义最大限制"""
        result = normalize_limit(100, max_limit=80)
        assert result == 80

    def test_max_limit_zero_uses_default(self):
        """测试 max_limit=0 时使用全局 MAX_QUERY_LIMIT 作为上限"""
        result = normalize_limit(100, max_limit=0)
        # effective_max = MAX_QUERY_LIMIT (500) when max_limit=0, so min(100, 500) = 100
        assert result == 100


# ============================================================
# 测试用例：查询相关常量
# ====================================

class TestConstants:
    """测试常量定义"""

    def test_default_query_limit(self):
        """测试默认查询限制"""
        assert DEFAULT_QUERY_LIMIT == 100

    def test_max_query_limit(self):
        """测试最大查询限制"""
        assert MAX_QUERY_LIMIT == 500

    def test_default_preview_limit(self):
        """测试默认预览限制"""
        assert DEFAULT_PREVIEW_LIMIT == 10

    def test_supported_query_fields(self):
        """测试支持的查询字段"""
        assert "attacker_ip" in SUPPORTED_QUERY_FIELDS
        assert "victim_ip" in SUPPORTED_QUERY_FIELDS
        assert "start_time" in SUPPORTED_QUERY_FIELDS
        assert "end_time" in SUPPORTED_QUERY_FIELDS


# ============================================================
# 测试用例：QueryCriteria
# ====================================

class TestQueryCriteria:
    """测试 QueryCriteria 数据类"""

    def test_default_values(self):
        """测试默认值"""
        criteria = QueryCriteria()
        assert criteria.attacker_ip is None
        assert criteria.victim_ip is None
        assert criteria.limit == DEFAULT_QUERY_LIMIT

    def test_limit_defaults_to_100(self):
        """测试 limit 默认为 100"""
        criteria = QueryCriteria()
        assert criteria.limit == 100


# ============================================================
# 测试用例：JsonlReadAudit
# ====================================



# ============================================================
# 测试用例：边界场景
# ====================================
