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

class TestJsonlReadAudit:
    """测试 JsonlReadAudit 数据类"""

    def test_default_values(self):
        """测试默认值"""
        audit = JsonlReadAudit(path="/test/path", read_at="2026-05-01T12:00:00Z")
        assert audit.total_lines == 0
        assert audit.blank_lines == 0
        assert audit.valid_records == 0

    def test_with_samples(self):
        """测试带样本"""
        audit = JsonlReadAudit(
            path="/test",
            read_at="2026-05-01T12:00:00Z",
            total_lines=100,
            valid_records=95,
            corrupt_lines=3,
            samples=[{"line_no": 10, "reason": "invalid_json"}],
        )
        assert len(audit.samples) == 1

class TestBoundaryCases:
    """测试边界场景"""

    def test_canonical_json_with_nested_structures(self):
        """测试嵌套结构的规范 JSON"""
        data = {
            "level1": {
                "a": 1,
                "b": 2,
            },
            "level2": [1, 2, 3],
        }
        result = canonical_json(data)
        parsed = json.loads(result)
        assert parsed["level1"]["a"] == 1

    def test_stable_digest_with_special_chars(self):
        """测试特殊字符的摘要"""
        data = {"emoji": "🎉", "chinese": "中文"}
        result = stable_digest(data)
        assert len(result) == 64  # SHA256 hex

    def test_nested_get_deeply_nested(self):
        """测试深层嵌套"""
        payload = {
            "level1": {
                "level2": {
                    "level3": {"value": "deep"},
                }
            }
        }
        assert nested_get(payload, "value") is None  # 不直接访问深层
        assert nested_get(payload["level1"]["level2"]["level3"], "value") == "deep"

    def test_evidence_path_from_none(self):
        """测试从 None 获取证据路径"""
        assert evidence_path_from_ref(None) == ""

    def test_normalize_limit_extreme_values(self):
        """测试极端限制值"""
        assert normalize_limit(1) == 1
        assert normalize_limit(MAX_QUERY_LIMIT + 1) == MAX_QUERY_LIMIT
