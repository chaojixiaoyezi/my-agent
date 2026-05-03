"""压力测试：log_pipeline 日志管道高并发和大数据量场景"""

from __future__ import annotations

import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.log_analysis.ingest.dead_letter import DeadLetterWriter
from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestPipeline
from agent_py_agent.agent.log_analysis.ingest.pipeline_enrich import flush_events


class TestLargeFileIngest:
    """大文件摄入压力测试"""

    @pytest.fixture
    def large_jsonl_file(self, tmp_path):
        """创建大型 JSONL 文件（用 mock 数据模拟）"""
        file_path = tmp_path / "large_test.jsonl"
        # 使用小体积的 mock 数据模拟大文件场景
        # 实际测试时每个事件约 200 字节
        events = []
        for i in range(1000):
            events.append(json.dumps({
                "event_id": f"e{i}",
                "session_id": f"sess-{i % 100}",
                "request_id": f"req-{i % 50}",
                "run_id": f"run-{i % 20}",
                "speaker": "user" if i % 2 == 0 else "assistant",
                "target": "assistant" if i % 2 == 0 else "user",
                "action": "message",
                "content": f"Test message content for event {i}",
                "status": "ok",
                "dedup_key": f"key-{i % 500}",
            }))
        file_path.write_text("\n".join(events))
        return file_path

    @pytest.mark.slow
    def test_ingest_1000_events(self, tmp_path):
        """验证 1000 条事件的摄入"""
        pipeline = IngestPipeline(root=tmp_path)

        # 创建 1000 条事件的文件
        file_path = tmp_path / "1000_events.jsonl"
        lines = [
            json.dumps({
                "event_id": f"e{i}",
                "session_id": "sess-1",
                "request_id": "req-1",
                "run_id": "run-1",
                "speaker": "user",
                "action": "message",
                "content": f"Content {i}",
                "dedup_key": f"key-{i}",
            })
            for i in range(1000)
        ]
        file_path.write_text("\n".join(lines))

        result = pipeline.ingest_file(file_path)
        assert result.parsed_count >= 990

    @pytest.mark.slow
    def test_ingest_with_many_duplicates(self, tmp_path):
        """验证大量重复事件的去重"""
        pipeline = IngestPipeline(root=tmp_path)

        # 创建有大量重复 dedup_key 的文件
        file_path = tmp_path / "duplicates.jsonl"
        lines = []
        for i in range(500):
            lines.append(json.dumps({
                "event_id": f"e{i}",
                "session_id": "sess-1",
                "request_id": "req-1",
                "run_id": "run-1",
                "speaker": "user",
                "action": "message",
                "content": f"Content {i % 50}",  # 只有 50 种不同内容
                "dedup_key": f"key-{i % 50}",  # 只有 50 种不同 key
            }))
        file_path.write_text("\n".join(lines))

        result = pipeline.ingest_file(file_path)
        # 应该有大量重复被过滤
        assert result.duplicate_count > 400

    @pytest.mark.slow
    def test_ingest_csv_with_many_rows(self, tmp_path):
        """验证多行 CSV 摄入"""
        import csv
        pipeline = IngestPipeline(root=tmp_path)

        csv_path = tmp_path / "many_rows.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["event_id", "dedup_key", "session_id", "content"])
            writer.writeheader()
            for i in range(500):
                writer.writerow({
                    "event_id": f"e{i}",
                    "dedup_key": f"key-{i}",
                    "session_id": "sess-1",
                    "content": f"Row content {i}",
                })

        result = pipeline.ingest_file(csv_path, file_format="csv")
        assert result.parsed_count >= 490


class TestConcurrentIngest:
    """并发写入压力测试"""

    @pytest.mark.slow
    def test_concurrent_flush_events(self, tmp_path):
        """验证并发 flush_events 写入"""
        pipeline = IngestPipeline(root=tmp_path)

        results = []
        errors = []

        def flush_batch(batch_id):
            try:
                events = [
                    {
                        "event_id": f"batch-{batch_id}-e{i}",
                        "session_id": f"sess-{batch_id}",
                        "request_id": f"req-{batch_id}",
                        "run_id": f"run-{batch_id}",
                        "speaker": "user",
                        "action": "message",
                        "content": f"Content from batch {batch_id}",
                        "dedup_key": f"batch-{batch_id}-key-{i}",
                        "source_id": f"src-{batch_id}",
                    }
                    for i in range(10)
                ]
                info, ids = flush_events(pipeline, events, batch_id=f"batch-{batch_id}")
                results.append((info, ids))
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(flush_batch, i) for i in range(20)]
            for f in as_completed(futures):
                pass

        assert len(errors) == 0
        assert len(results) == 20

    @pytest.mark.slow
    def test_concurrent_dead_letter_writes(self, tmp_path):
        """验证并发死信写入"""
        writers = []
        errors = []

        def write_dead_letters(writer_id):
            try:
                writer = DeadLetterWriter(
                    root=tmp_path,
                    source_id=f"source-{writer_id}",
                    batch_id=f"batch-{writer_id}",
                )
                for i in range(50):
                    writer.write(
                        reason=f"error-{i}",
                        raw_ref=f"ref-{writer_id}-{i}",
                        line_no=i,
                    )
                writers.append(writer)
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(write_dead_letters, i) for i in range(10)]
            for f in as_completed(futures):
                pass

        assert len(errors) == 0
        assert len(writers) == 10

    @pytest.mark.slow
    def test_multiple_pipelines_same_root(self, tmp_path):
        """验证多个 pipeline 共享同一 root 的并发写入"""
        results = []

        def run_pipeline(pipeline_id):
            pipeline = IngestPipeline(root=tmp_path)
            file_path = tmp_path / f"concurrent_{pipeline_id}.jsonl"
            lines = [
                json.dumps({
                    "event_id": f"p{pipeline_id}-e{i}",
                    "session_id": f"sess-{pipeline_id}",
                    "request_id": f"req-{pipeline_id}",
                    "run_id": f"run-{pipeline_id}",
                    "speaker": "user",
                    "action": "message",
                    "content": f"Content {i}",
                    "dedup_key": f"key-{pipeline_id}-{i}",
                })
                for i in range(50)
            ]
            file_path.write_text("\n".join(lines))

            result = pipeline.ingest_file(file_path)
            results.append(result)

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(run_pipeline, i) for i in range(5)]
            for f in as_completed(futures):
                pass

        assert len(results) == 5
        assert all(r.parsed_count >= 45 for r in results)


class TestMalformedDataHandling:
    """畸形数据处理压力测试"""

    @pytest.mark.slow
    def test_many_malformed_lines(self, tmp_path):
        """验证大量格式错误行的处理"""
        pipeline = IngestPipeline(root=tmp_path)

        file_path = tmp_path / "malformed_many.jsonl"
        lines = []
        # 90% 是畸形数据，10% 是有效数据
        for i in range(1000):
            if i % 10 == 0:
                lines.append(json.dumps({"event_id": f"e{i}", "dedup_key": f"key-{i}"}))
            else:
                lines.append(f"not json line {i}")
        file_path.write_text("\n".join(lines))

        result = pipeline.ingest_file(file_path)
        # 应该处理完所有行，死信数量应该约 900
        assert result.parsed_count >= 90
        assert result.dead_letter_count >= 800

    @pytest.mark.slow
    def test_giant_json_line(self, tmp_path):
        """验证超长 JSON 行的处理"""
        pipeline = IngestPipeline(root=tmp_path)

        file_path = tmp_path / "giant_line.jsonl"
        # 创建一个超长的 JSON 行（但不至于真的太大）
        long_content = "x" * 10000
        file_path.write_text(json.dumps({
            "event_id": "giant",
            "session_id": "sess-1",
            "request_id": "req-1",
            "run_id": "run-1",
            "speaker": "user",
            "action": "message",
            "content": long_content,
            "dedup_key": "giant-key",
        }))

        result = pipeline.ingest_file(file_path)
        # 应该正常处理
        assert result.parsed_count >= 1

    @pytest.mark.slow
    def test_empty_file_handling(self, tmp_path):
        """验证空文件的处理"""
        pipeline = IngestPipeline(root=tmp_path)

        file_path = tmp_path / "empty.jsonl"
        file_path.write_text("")

        result = pipeline.ingest_file(file_path)
        assert result.parsed_count == 0
        assert result.stored_count == 0


class TestParserStress:
    """Parser 压力测试"""

    @pytest.mark.slow
    def test_many_sessions_events(self, tmp_path):
        """验证多会话事件的处理"""
        pipeline = IngestPipeline(root=tmp_path)

        file_path = tmp_path / "many_sessions.jsonl"
        lines = []
        # 100 个会话，每会话 50 条事件
        for session_idx in range(100):
            for event_idx in range(50):
                lines.append(json.dumps({
                    "event_id": f"s{session_idx}-e{event_idx}",
                    "session_id": f"session-{session_idx}",
                    "request_id": f"req-{session_idx}-{event_idx // 10}",
                    "run_id": f"run-{session_idx}",
                    "speaker": "user" if event_idx % 2 == 0 else "assistant",
                    "action": "message",
                    "content": f"Session {session_idx} event {event_idx}",
                    "dedup_key": f"key-{session_idx}-{event_idx}",
                }))
        file_path.write_text("\n".join(lines))

        result = pipeline.ingest_file(file_path)
        assert result.parsed_count >= 4900

    @pytest.mark.slow
    def test_deeply_nested_json(self, tmp_path):
        """验证深层嵌套 JSON 的解析"""
        pipeline = IngestPipeline(root=tmp_path)

        file_path = tmp_path / "nested.jsonl"
        nested = {"level0": {"level1": {"level2": {"level3": {"level4": "deep-value"}}}}}
        file_path.write_text(json.dumps(nested))

        result = pipeline.ingest_file(file_path)
        # 应该正常处理或进入死信
        assert result.dead_letter_count + result.parsed_count >= 1