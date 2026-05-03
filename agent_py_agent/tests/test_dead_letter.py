"""单元测试：死信队列模块 - 失败记录、重试、告警升级"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.log_analysis.ingest.dead_letter import (
    DeadLetterRef,
    DeadLetterWriter,
    _preview,
)
from agent_py_agent.agent.log_analysis.parsers.common import sha256_text


class TestDeadLetterPreview:
    """测试死信预览函数"""

    def test_preview_none_returns_none(self):
        """验证 None 输入返回 None"""
        assert _preview(None) is None

    def test_preview_short_string(self):
        """验证短字符串完整返回"""
        text = "hello world"
        assert _preview(text) == text

    def test_preview_truncates_long_string(self):
        """验证长字符串被截断到最大字符数"""
        long_text = "a" * 3000
        result = _preview(long_text, max_chars=2048)
        assert len(result) == 2048

    def test_preview_default_max_chars(self):
        """验证默认最大字符数为 2048"""
        long_text = "x" * 5000
        result = _preview(long_text)
        assert len(result) == 2048


class TestDeadLetterWriterInit:
    """测试 DeadLetterWriter 初始化"""

    def test_initialization(self, tmp_path):
        """验证 DeadLetterWriter 正确初始化"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test_source",
            batch_id="batch-123",
        )
        assert writer.source_id == "test_source"
        assert writer.batch_id == "batch-123"
        assert writer.count == 0

    def test_path_construction(self, tmp_path):
        """验证死信文件路径构造"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="my-source",
            batch_id="batch-456",
        )
        assert "dead_letter" in str(writer.path)
        assert "my-source" in str(writer.path)
        assert "batch-456" in str(writer.path)

    def test_diagnostic_path_exists(self, tmp_path):
        """验证诊断路径存在"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="src",
            batch_id="batch-789",
        )
        assert "events" in str(writer.diagnostic_path)


class TestDeadLetterWrite:
    """测试 DeadLetterWriter.write 方法"""

    def test_write_increments_count(self, tmp_path):
        """验证写入增加计数器"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-001",
        )
        writer.write(reason="test_error", raw_ref="ref-1")
        assert writer.count == 1

    def test_write_creates_dead_letter_file(self, tmp_path):
        """验证写入创建死信文件"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-002",
        )
        writer.write(reason="parse_error", raw_ref="ref-2")

        assert writer.path.exists()
        # 验证文件内容是 JSONL 格式
        lines = writer.path.read_text().strip().split("\n")
        assert len(lines) >= 1

    def test_write_records_all_fields(self, tmp_path):
        """验证写入记录所有必要字段"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-003",
        )
        writer.write(
            reason="validation_failed",
            raw_ref="ref-3",
            line_no=42,
            raw_line='{"bad": "data"}',
            raw_fields={"field1": "value1"},
            parser_id="security_alert_v1",
        )

        # 读取并验证内容
        content = json.loads(writer.path.read_text())
        assert content["reason"] == "validation_failed"
        assert content["line_no"] == 42
        assert content["parser_id"] == "security_alert_v1"
        assert "dead_letter_id" in content

    def test_write_multiple_increments_count(self, tmp_path):
        """验证多次写入正确增加计数"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-004",
        )
        for i in range(5):
            writer.write(reason=f"error_{i}", raw_ref=f"ref-{i}")

        assert writer.count == 5

    def test_write_to_diagnostic_path(self, tmp_path):
        """验证诊断事件写入诊断文件"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-005",
        )
        writer.write(reason="diag_test", raw_ref="ref-d")

        assert writer.diagnostic_path.exists()
        content = json.loads(writer.diagnostic_path.read_text())
        assert content["event_type"] == "log_parse_failure"


class TestDeadLetterRefs:
    """测试 DeadLetterWriter.refs 方法"""

    def test_refs_empty_when_no_writes(self, tmp_path):
        """验证未写入时 refs 返回空列表"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-006",
        )
        assert writer.refs() == []

    def test_refs_returns_path_and_count(self, tmp_path):
        """验证 refs 返回路径和计数"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-007",
        )
        writer.write(reason="err", raw_ref="r1")
        writer.write(reason="err", raw_ref="r2")

        refs = writer.refs()
        assert len(refs) == 1
        assert refs[0]["path"] == str(writer.path)
        assert refs[0]["count"] == 2

    def test_refs_after_reset_count(self, tmp_path):
        """验证计数重置后 refs 返回空"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-008",
        )
        writer.write(reason="err", raw_ref="r1")
        # 手动重置计数模拟不同场景
        writer.count = 0
        assert writer.refs() == []


class TestDeadLetterIntegration:
    """测试死信模块集成场景"""

    def test_batch_processing_with_failures(self, tmp_path):
        """验证批量处理中部分失败的场景"""
        source_id = "batch_failures"
        batch_id = "batch-009"

        writer = DeadLetterWriter(
            root=tmp_path,
            source_id=source_id,
            batch_id=batch_id,
        )

        # 模拟多次失败
        for i in range(3):
            writer.write(
                reason=f"failure_{i}",
                raw_ref=f"ref-{i}",
                line_no=i + 1,
            )

        # 验证计数
        assert writer.count == 3

        # 验证 refs 返回正确数据
        refs = writer.refs()
        assert len(refs) == 1
        assert refs[0]["count"] == 3

    def test_different_sources_isolated(self, tmp_path):
        """验证不同 source 的死信文件隔离"""
        writer1 = DeadLetterWriter(
            root=tmp_path,
            source_id="source_a",
            batch_id="batch-a",
        )
        writer2 = DeadLetterWriter(
            root=tmp_path,
            source_id="source_b",
            batch_id="batch-b",
        )

        writer1.write(reason="err_a", raw_ref="ref-a")
        writer2.write(reason="err_b", raw_ref="ref-b")

        # 验证路径不同
        assert writer1.path != writer2.path

        # 验证两个文件都存在
        assert writer1.path.exists()
        assert writer2.path.exists()

    def test_dedup_key_generation(self, tmp_path):
        """验证死信 ID 的确定性生成"""
        writer = DeadLetterWriter(
            root=tmp_path,
            source_id="test",
            batch_id="batch-dedup",
        )

        # 相同输入应产生相同死信 ID
        writer.write(reason="same_reason", raw_ref="same_ref")
        content1 = json.loads(writer.path.read_text())
        first_id = content1["dead_letter_id"]

        # 重置并再次写入相同内容
        writer.count = 0
        writer.path.unlink()
        writer.write(reason="same_reason", raw_ref="same_ref")
        content2 = json.loads(writer.path.read_text())

        assert content2["dead_letter_id"] == first_id