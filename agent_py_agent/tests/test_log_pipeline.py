"""单元测试：日志分析管道 - 处理管道、阶段流转、错误处理"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.log_analysis.ingest.pipeline import (
    DeadLetterWriter,
    IngestPipeline,
    IngestResult,
    default_log_analysis_root,
    file_digest,
    make_batch_id,
    normalize_file_format,
)
from agent_py_agent.agent.log_analysis.parsers.base import ParserError


class TestNormalizeFileFormat:
    """测试文件格式标准化"""

    def test_normalize_jsonl_lowercase(self):
        """验证 'jsonl' 小写标准化"""
        assert normalize_file_format("jsonl") == "jsonl"

    def test_normalize_json_uppercase(self):
        """验证 'json' 转为 'jsonl'"""
        assert normalize_file_format("JSON") == "jsonl"

    def test_normalize_csv_extension(self):
        """验证 '.csv' 标准化为 'csv'"""
        assert normalize_file_format(".csv") == "csv"

    def test_normalize_log_format(self):
        """验证 'log' 格式"""
        assert normalize_file_format("log") == "log"

    def test_normalize_empty_string_defaults_to_jsonl(self):
        """验证空字符串默认返回 jsonl"""
        assert normalize_file_format("") == "jsonl"

    def test_normalize_unsupported_format_raises(self):
        """验证不支持的格式抛出 ParserError"""
        with pytest.raises(ParserError):
            normalize_file_format("txt")


class TestMakeBatchId:
    """测试批次 ID 生成"""

    def test_batch_id_format(self):
        """验证批次 ID 以 'batch-' 前缀开头"""
        batch_id = make_batch_id(
            source_id="test_source",
            source_path="/path/to/file",
            content_hash="abc123",
        )
        assert batch_id.startswith("batch-")

    def test_batch_id_deterministic(self):
        """验证相同输入产生相同批次 ID（幂等性）"""
        params = dict(source_id="s1", source_path="/p", content_hash="hash1")
        id1 = make_batch_id(**params)
        id2 = make_batch_id(**params)
        assert id1 == id2

    def test_batch_id_different_inputs(self):
        """验证不同输入产生不同批次 ID"""
        id1 = make_batch_id(source_id="a", source_path="/p", content_hash="h")
        id2 = make_batch_id(source_id="b", source_path="/p", content_hash="h")
        assert id1 != id2


class TestFileDigest:
    """测试文件摘要计算"""

    def test_file_digest_returns_size_and_hash(self):
        """验证返回 (大小, 哈希) 元组"""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello world")
            f.flush()
            path = Path(f.name)

        size, hash_str = file_digest(path)
        assert size == 11
        assert hash_str.startswith("sha256:")

    def test_file_digest_empty_file(self):
        """验证空文件返回大小 0"""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = Path(f.name)

        size, _ = file_digest(path)
        assert size == 0


class TestIngestPipelineInit:
    """测试 IngestPipeline 初始化"""

    def test_default_initialization(self, tmp_path):
        """验证默认初始化创建必要组件"""
        pipeline = IngestPipeline(root=tmp_path)
        assert pipeline.root == tmp_path
        assert pipeline.write_batch_size == 1000
        assert pipeline.registry is not None

    def test_custom_write_batch_size(self, tmp_path):
        """验证自定义批次大小"""
        pipeline = IngestPipeline(root=tmp_path, write_batch_size=500)
        assert pipeline.write_batch_size == 500

    def test_write_batch_size_minimum_enforced(self, tmp_path):
        """验证批次大小最小值为 1"""
        pipeline = IngestPipeline(root=tmp_path, write_batch_size=0)
        assert pipeline.write_batch_size == 1


class TestIngestPipelineIngestFile:
    """测试 IngestPipeline.ingest_file 方法"""

    def test_file_not_found_raises(self, tmp_path):
        """验证文件不存在时抛出 FileNotFoundError"""
        pipeline = IngestPipeline(root=tmp_path)
        with pytest.raises(FileNotFoundError):
            pipeline.ingest_file("/nonexistent/file.jsonl")

    def test_ingest_jsonl_file_returns_result(self, tmp_path):
        """验证成功摄入 JSONL 文件返回 IngestResult"""
        test_file = tmp_path / "test.jsonl"
        test_file.write_text('{"event_id":"e1","dedup_key":"k1"}\n')

        pipeline = IngestPipeline(root=tmp_path)
        result = pipeline.ingest_file(test_file)

        assert isinstance(result, IngestResult)
        assert result.status == "stored"
        assert result.parsed_count >= 1

    def test_ingest_csv_file_returns_result(self, tmp_path):
        """验证成功摄入 CSV 文件返回 IngestResult"""
        test_file = tmp_path / "test.csv"
        test_file.write_text("event_id,dedup_key\n1,key1\n")

        pipeline = IngestPipeline(root=tmp_path)
        result = pipeline.ingest_file(test_file, file_format="csv")

        assert isinstance(result, IngestResult)
        assert result.file_format == "csv"

    def test_ingest_creates_manifest(self, tmp_path):
        """验证摄入后创建 manifest 文件"""
        test_file = tmp_path / "test.jsonl"
        test_file.write_text('{"event_id":"e1","dedup_key":"k1"}\n')

        pipeline = IngestPipeline(root=tmp_path)
        result = pipeline.ingest_file(test_file)

        assert Path(result.manifest_path).exists()

    def test_ingest_creates_checkpoint(self, tmp_path):
        """验证摄入后创建 checkpoint 文件"""
        test_file = tmp_path / "test.jsonl"
        test_file.write_text('{"event_id":"e1","dedup_key":"k1"}\n')

        pipeline = IngestPipeline(root=tmp_path)
        result = pipeline.ingest_file(test_file)

        assert Path(result.checkpoint_path).exists()

    def test_ingest_with_custom_source_id(self, tmp_path):
        """验证自定义 source_id"""
        test_file = tmp_path / "test.jsonl"
        test_file.write_text('{"event_id":"e1","dedup_key":"k1"}\n')

        pipeline = IngestPipeline(root=tmp_path)
        result = pipeline.ingest_file(test_file, source_id="my_source")

        assert result.source_id == "my_source"


class TestIngestPipelineDeadLetter:
    """测试 IngestPipeline 死信处理"""

    def test_malformed_line_goes_to_dead_letter(self, tmp_path):
        """验证格式错误的行写入死信队列"""
        test_file = tmp_path / "test.jsonl"
        test_file.write_text('{"event_id":"e1"}\ninvalid json line\n')

        pipeline = IngestPipeline(root=tmp_path)
        result = pipeline.ingest_file(test_file)

        assert result.dead_letter_count >= 1
        assert len(result.dead_letter_refs) >= 1


class TestIngestPipelineDuplicateHandling:
    """测试 IngestPipeline 去重逻辑"""

    def test_duplicate_events_filtered(self, tmp_path):
        """验证重复事件被过滤"""
        test_file = tmp_path / "test.jsonl"
        test_file.write_text(
            '{"event_id":"e1","dedup_key":"k1"}\n'
            '{"event_id":"e2","dedup_key":"k1"}\n'
        )

        pipeline = IngestPipeline(root=tmp_path)
        result = pipeline.ingest_file(test_file)

        assert result.stored_count == 1
        assert result.duplicate_count == 1
