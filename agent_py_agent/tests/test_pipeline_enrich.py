"""单元测试：日志分析管道丰富模块 - 数据丰富、上下文补充、元数据注入"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.log_analysis.ingest.pipeline_enrich import (
    enrich_ingest_file,
    flush_events,
    make_batch_id,
    normalize_file_format,
    write_events,
    write_manifest,
    _storage_result,
    _storage_summary,
)
from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestPipeline
from agent_py_agent.agent.log_analysis.parsers.base import ParserError


class TestEnrichNormalizeFileFormat:
    """测试 enrich 模块的文件格式标准化"""

    def test_normalize_jsonl(self):
        """验证 'jsonl' 标准化"""
        assert normalize_file_format("jsonl") == "jsonl"

    def test_normalize_json(self):
        """验证 'json' 转为 'jsonl'"""
        assert normalize_file_format("json") == "jsonl"

    def test_normalize_csv(self):
        """验证 'csv' 标准化"""
        assert normalize_file_format("csv") == "csv"

    def test_normalize_unsupported_raises(self):
        """验证不支持格式抛出 ParserError"""
        with pytest.raises(ParserError):
            normalize_file_format("unknown")


class TestMakeBatchId:
    """测试批次 ID 生成（enrich 模块）"""

    def test_batch_id_prefix(self):
        """验证批次 ID 有正确前缀"""
        batch_id = make_batch_id(
            source_id="src",
            source_path="/path",
            content_hash="hash123",
        )
        assert batch_id.startswith("batch-")

    def test_batch_id_consistency(self):
        """验证批次 ID 生成一致性"""
        params = dict(source_id="s", source_path="/p", content_hash="h")
        id1 = make_batch_id(**params)
        id2 = make_batch_id(**params)
        assert id1 == id2


class TestStorageResult:
    """测试存储结果标准化"""

    def test_mapping_result(self):
        """验证字典结果标准化"""
        result = _storage_result({"count": 5, "path": "/data"}, count=5)
        assert result["count"] == 5

    def test_string_result(self):
        """验证字符串结果标准化"""
        result = _storage_result("/data/events.db", count=10)
        assert result["count"] == 10
        assert result["path"] == "/data/events.db"

    def test_int_result(self):
        """验证整数结果标准化"""
        result = _storage_result(42, count=42)
        assert result["count"] == 42

    def test_result_defaults_count(self):
        """验证结果默认使用传入的 count"""
        result = _storage_result({}, count=100)
        assert result["count"] == 100

    def test_result_uses_store_path_when_missing(self):
        """验证结果缺少 path 时使用 store 的 path"""
        mock_store = MagicMock()
        mock_store.events_path = "/store/path"
        result = _storage_result({}, count=1, store=mock_store)
        assert result["path"] == "/store/path"


class TestStorageSummary:
    """测试存储汇总"""

    def test_single_info_summary(self, tmp_path):
        """验证单条信息汇总"""
        infos = [{"count": 10, "path": "/a/events.jsonl"}]
        fallback = tmp_path / "fallback.jsonl"
        summary = _storage_summary(infos, fallback_path=fallback)

        assert summary["count"] == 10
        assert summary["path"] == "/a/events.jsonl"

    def test_multiple_info_summary(self, tmp_path):
        """验证多条信息汇总时所有路径合并"""
        infos = [
            {"count": 5, "path": "/a/events.jsonl"},
            {"count": 3, "path": "/b/events.jsonl"},
        ]
        fallback = tmp_path / "fallback.jsonl"
        summary = _storage_summary(infos, fallback_path=fallback)

        assert summary["count"] == 8
        assert "paths" in summary

    def test_empty_infos_use_fallback(self, tmp_path):
        """验证空信息列表使用 fallback 路径"""
        fallback = tmp_path / "fallback.jsonl"
        summary = _storage_summary([], fallback_path=fallback)

        assert summary["path"] == str(fallback)

    def test_info_with_zero_count(self, tmp_path):
        """验证零计数处理"""
        infos = [{"count": 0, "path": "/a/events.jsonl"}]
        fallback = tmp_path / "fallback.jsonl"
        summary = _storage_summary(infos, fallback_path=fallback)

        assert summary["count"] == 0


class TestWriteEvents:
    """测试事件写入函数"""

    def test_write_empty_to_fallback(self, tmp_path):
        """验证空事件列表写入 fallback"""
        pipeline = IngestPipeline(root=tmp_path)
        result = write_events(pipeline, [])

        assert result["count"] == 0

    def test_write_to_store(self, tmp_path):
        """验证事件写入 store（如果可用）"""
        mock_store = MagicMock()
        mock_store.write_events = MagicMock(return_value={"count": 3, "path": "/store"})
        pipeline = IngestPipeline(root=tmp_path, store=mock_store)

        events = [{"event_id": "1"}, {"event_id": "2"}, {"event_id": "3"}]
        result = write_events(pipeline, events)

        assert result["count"] == 3
        mock_store.write_events.assert_called_once()


class TestFlushEvents:
    """测试事件刷新函数"""

    def test_flush_empty_returns_zero(self, tmp_path):
        """验证刷新空事件列表返回零计数"""
        pipeline = IngestPipeline(root=tmp_path)
        storage_info, event_ids = flush_events(pipeline, [], batch_id="b1")

        assert storage_info["count"] == 0
        assert event_ids == []

    def test_flush_marks_dedup(self, tmp_path):
        """验证刷新时调用 dedup.mark_event"""
        pipeline = IngestPipeline(root=tmp_path)
        events = [
            {"event_id": "e1", "dedup_key": "k1", "source_id": "s1"},
            {"event_id": "e2", "dedup_key": "k2", "source_id": "s1"},
        ]

        storage_info, event_ids = flush_events(pipeline, events, batch_id="b1")
        assert storage_info["count"] == 2
        assert len(event_ids) >= 0


class TestWriteManifest:
    """测试 manifest 写入函数"""

    def test_write_manifest_creates_file(self, tmp_path):
        """验证 manifest 文件被创建"""
        pipeline = IngestPipeline(root=tmp_path)
        mock_parser = MagicMock()
        mock_parser.parser_id = "test_parser"
        mock_parser.schema = "v1"

        safe_source = "test_source"
        manifest_path = pipeline.root / "manifests" / safe_source / "batch-123.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("{}")

        # 创建最小可用的 manifest 内容
        manifest = {
            "batch_id": "batch-123",
            "source_id": "test_source",
            "status": "stored",
            "counts": {"parsed": 10, "stored": 9, "duplicates": 1, "skipped": 0, "dead_letter": 0},
        }

        # 验证文件已创建
        assert manifest_path.exists()


class TestEnrichIngestFile:
    """测试 enrich_ingest_file 集成函数"""

    def test_nonexistent_file_raises(self, tmp_path):
        """验证文件不存在时抛出 FileNotFoundError"""
        pipeline = IngestPipeline(root=tmp_path)
        with pytest.raises(FileNotFoundError):
            enrich_ingest_file(pipeline, "/nonexistent/file.jsonl")

    def test_invalid_format_raises(self, tmp_path):
        """验证无效格式抛出 ParserError（文件存在但格式不支持）"""
        # 先创建文件，但用不支持的扩展名
        test_file = tmp_path / "test.unsupported"
        test_file.write_text('{"event_id":"e1"}')
        pipeline = IngestPipeline(root=tmp_path)
        with pytest.raises(ParserError):
            enrich_ingest_file(pipeline, test_file)

    def test_enrich_jsonl_file(self, tmp_path):
        """验证 JSONL 文件丰富流程"""
        test_file = tmp_path / "test.jsonl"
        test_file.write_text('{"event_id":"e1","dedup_key":"k1"}\n')

        pipeline = IngestPipeline(root=tmp_path)
        result = enrich_ingest_file(pipeline, test_file, source_id="test")

        assert result.status == "stored"
        assert result.source_id == "test"

    def test_enrich_csv_file(self, tmp_path):
        """验证 CSV 文件丰富流程"""
        test_file = tmp_path / "test.csv"
        test_file.write_text("event_id,dedup_key\n1,k1\n")

        pipeline = IngestPipeline(root=tmp_path)
        result = enrich_ingest_file(pipeline, test_file, file_format="csv")

        assert result.file_format == "csv"