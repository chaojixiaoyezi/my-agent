"""Tests for agent_py_agent.agent.log_analysis.ingest.pipeline module.

Covers: IngestResult, JsonlEventSink, IngestPipeline, normalize_file_format,
file_digest, make_batch_id, _storage_result, _storage_summary, _jsonable_mapping,
and the top-level ingest_file function.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.log_analysis.parsers.base import ParserError

# ---------------------------------------------------------------------------
# normalize_file_format
# ---------------------------------------------------------------------------

class TestIngestPipelineWriteEvents:
    """Test IngestPipeline._write_events dispatch logic."""

    def _make_pipeline(self, tmp_path: Path, store=None):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestPipeline

        with patch.object(IngestPipeline, "_default_store", return_value=store):
            return IngestPipeline(tmp_path, store=store)

    def test_empty_events_returns_fallback_count_zero(self, tmp_path: Path):
        pipeline = self._make_pipeline(tmp_path)
        result = pipeline._write_events([])
        assert result["count"] == 0

    def test_store_none_uses_fallback_sink(self, tmp_path: Path):
        pipeline = self._make_pipeline(tmp_path, store=None)
        # store=None triggers fallback
        pipeline.store = None
        result = pipeline._write_events([{"id": 1}])
        assert result["count"] == 1
        assert "events.jsonl" in result["path"]

    def test_store_with_write_events_method(self, tmp_path: Path):
        store = MagicMock()
        store.write_events.return_value = {"count": 3, "path": "/custom/path"}
        pipeline = self._make_pipeline(tmp_path, store=store)
        events = [{"id": i} for i in range(3)]
        result = pipeline._write_events(events)
        store.write_events.assert_called_once_with(events)
        assert result["count"] == 3

    def test_store_with_append_events_method(self, tmp_path: Path):
        store = MagicMock(spec=["append_events", "events_path"])
        store.append_events.return_value = {"count": 2}
        store.events_path = "/store/events.jsonl"
        pipeline = self._make_pipeline(tmp_path, store=store)
        result = pipeline._write_events([{"id": 1}, {"id": 2}])
        store.append_events.assert_called_once()
        assert result["count"] == 2

    def test_store_with_single_event_method(self, tmp_path: Path):
        store = MagicMock(spec=["write_event"])
        pipeline = self._make_pipeline(tmp_path, store=store)
        events = [{"id": 1}, {"id": 2}]
        result = pipeline._write_events(events)
        assert store.write_event.call_count == 2
        assert result["count"] == 2

class TestIngestPipelineFlushEvents:
    """Test IngestPipeline._flush_events logic."""

    def _make_pipeline(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestPipeline

        with patch.object(IngestPipeline, "_default_store", return_value=None):
            p = IngestPipeline(tmp_path, store=None)
        return p

    def test_empty_events(self, tmp_path: Path):
        pipeline = self._make_pipeline(tmp_path)
        info, ids = pipeline._flush_events([], batch_id="b1")
        assert info["count"] == 0
        assert ids == []

    def test_flush_with_dedup_marks(self, tmp_path: Path):
        pipeline = self._make_pipeline(tmp_path)
        events = [
            {"dedup_key": "dk1", "event_id": "evt-1", "source_id": "src1"},
            {"dedup_key": "dk2", "event_id": "evt-2", "source_id": "src1"},
        ]
        pipeline.dedup.mark_event = MagicMock(return_value=True)
        info, ids = pipeline._flush_events(events, batch_id="b1")
        assert len(ids) == 2
        assert "evt-1" in ids

class TestIngestPipelineIngestFile:
    """Test IngestPipeline.ingest_file with real files but mocked store."""

    def test_file_not_found_raises(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestPipeline

        with patch.object(IngestPipeline, "_default_store", return_value=None):
            p = IngestPipeline(tmp_path, store=None)
        with pytest.raises(FileNotFoundError):
            p.ingest_file(tmp_path / "nonexistent.csv")

    def test_unsupported_format_raises(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestPipeline

        p_file = tmp_path / "data.xml"
        p_file.write_text("<root/>")
        with patch.object(IngestPipeline, "_default_store", return_value=None):
            p = IngestPipeline(tmp_path, store=None)
        with pytest.raises(ParserError, match="unsupported"):
            p.ingest_file(p_file, file_format="xml")

    def test_ingest_jsonl_file_produces_result(self, tmp_path: Path):
        """End-to-end: ingest a simple JSONL file and verify IngestResult fields."""
        from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestPipeline

        # Create a minimal JSONL file with valid alert data
        alert = {
            "alert_id": "a-001",
            "event_time": "2025-01-01T00:00:00Z",
            "source_id": "sensor-1",
            "source_product": "waf",
            "alert_type": "intrusion",
            "threat_name": "test-threat",
        }
        data_file = tmp_path / "alerts.jsonl"
        data_file.write_text(json.dumps(alert) + "\n")

        store = MagicMock()
        store.write_events.return_value = {"count": 1, "path": str(tmp_path / "events.jsonl")}

        with patch.object(IngestPipeline, "_default_store", return_value=store):
            pipeline = IngestPipeline(tmp_path, store=store)

        result = pipeline.ingest_file(data_file)
        assert result.status == "stored"
        assert result.parsed_count >= 1
        assert result.file_format == "jsonl"
        assert result.batch_id.startswith("batch-")
        assert result.manifest_path.endswith(".json")

class TestTopLevelIngestFile:
    """Test the module-level ingest_file convenience function."""

    def test_creates_pipeline_and_delegates(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import ingest_file

        data_file = tmp_path / "test.jsonl"
        data_file.write_text('{"alert_id":"a1","event_time":"2025-01-01T00:00:00Z","source_id":"s1","source_product":"waf"}\n')

        store = MagicMock()
        store.write_events.return_value = {"count": 1, "path": str(tmp_path / "events.jsonl")}

        result = ingest_file(data_file, root=tmp_path, store=store)
        assert result.status == "stored"
        assert result.source_path == str(data_file)

    def test_missing_file_raises(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import ingest_file

        with pytest.raises(FileNotFoundError):
            ingest_file(tmp_path / "missing.csv", root=tmp_path)
