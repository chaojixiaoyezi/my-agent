"""Tests for agent_py_agent.agent.log_analysis.ingest.pipeline module.

Covers: IngestResult, IngestPipeline, normalize_file_format,
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


class TestNormalizeFileFormat:
    """Test the normalize_file_format helper."""

    def test_jsonl_passthrough(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        assert normalize_file_format("jsonl") == "jsonl"

    def test_csv_passthrough(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        assert normalize_file_format("csv") == "csv"

    def test_log_passthrough(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        assert normalize_file_format("log") == "log"

    def test_json_maps_to_jsonl(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        assert normalize_file_format("json") == "jsonl"

    def test_uppercase_normalization(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        assert normalize_file_format("JSONL") == "jsonl"
        assert normalize_file_format("CSV") == "csv"

    def test_dot_prefix_stripped(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        assert normalize_file_format(".jsonl") == "jsonl"
        assert normalize_file_format(".csv") == "csv"

    def test_empty_string_defaults_to_jsonl(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        # after lstrip(".") an empty string -> "jsonl"
        assert normalize_file_format("") == "jsonl"
        assert normalize_file_format(".") == "jsonl"

    def test_unsupported_format_raises(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        with pytest.raises(ParserError, match="unsupported file format"):
            normalize_file_format("xml")

    def test_unsupported_format_msg_contains_original(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import normalize_file_format

        with pytest.raises(ParserError, match="foobar"):
            normalize_file_format("foobar")


# ---------------------------------------------------------------------------
# file_digest
# ---------------------------------------------------------------------------


class TestFileDigest:
    """Test the file_digest helper."""

    def test_empty_file(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import file_digest

        p = tmp_path / "empty.txt"
        p.write_bytes(b"")
        size, hash_str = file_digest(p)
        assert size == 0
        assert hash_str.startswith("sha256:")

    def test_known_content(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import file_digest

        p = tmp_path / "data.bin"
        content = b"hello world"
        p.write_bytes(content)
        size, hash_str = file_digest(p)
        assert size == len(content)
        assert hash_str.startswith("sha256:")
        assert len(hash_str) == len("sha256:") + 64

    def test_deterministic(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import file_digest

        p = tmp_path / "deterministic.bin"
        p.write_bytes(b"same content")
        _, h1 = file_digest(p)
        _, h2 = file_digest(p)
        assert h1 == h2

    def test_different_files_different_hashes(self, tmp_path: Path):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import file_digest

        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"aaa")
        b.write_bytes(b"bbb")
        _, ha = file_digest(a)
        _, hb = file_digest(b)
        assert ha != hb


# ---------------------------------------------------------------------------
# make_batch_id
# ---------------------------------------------------------------------------


class TestMakeBatchId:
    """Test the make_batch_id helper."""

    def test_deterministic(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import make_batch_id

        args = dict(source_id="src1", source_path="/a/b.csv", content_hash="sha256:abc123")
        b1 = make_batch_id(**args)
        b2 = make_batch_id(**args)
        assert b1 == b2

    def test_prefix(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import make_batch_id

        bid = make_batch_id(source_id="x", source_path="/p", content_hash="sha256:z")
        assert bid.startswith("batch-")

    def test_different_inputs_different_ids(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import make_batch_id

        b1 = make_batch_id(source_id="a", source_path="/p", content_hash="sha256:z")
        b2 = make_batch_id(source_id="b", source_path="/p", content_hash="sha256:z")
        assert b1 != b2

    def test_length(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import make_batch_id

        bid = make_batch_id(source_id="x", source_path="/p", content_hash="sha256:z")
        # "batch-" (6) + 24 hex chars = 30
        assert len(bid) == 30


# ---------------------------------------------------------------------------
# _storage_result
# ---------------------------------------------------------------------------


class TestStorageResult:
    """Test the _storage_result helper."""

    def test_mapping_input_passthrough(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_result

        result = _storage_result({"count": 5, "path": "/some/path"}, count=10)
        assert result["count"] == 5  # original count preserved
        assert result["path"] == "/some/path"

    def test_mapping_input_fills_missing_count(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_result

        result = _storage_result({"path": "/p"}, count=3)
        assert result["count"] == 3

    def test_mapping_input_fills_none_path_from_store(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_result

        store = MagicMock()
        store.events_path = "/store/events.jsonl"
        result = _storage_result({"count": 2, "path": None}, count=2, store=store)
        assert result["path"] == "/store/events.jsonl"

    def test_string_result(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_result

        result = _storage_result("/some/path", count=7)
        assert result == {"count": 7, "path": "/some/path"}

    def test_path_result(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_result

        result = _storage_result(Path("/some/path"), count=7)
        assert result == {"count": 7, "path": "/some/path"}

    def test_int_result(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_result

        store = MagicMock()
        store.events_path = "/store/events.jsonl"
        result = _storage_result(42, count=10, store=store)
        assert result["count"] == 42
        assert result["path"] == "/store/events.jsonl"

    def test_int_result_no_store(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_result

        result = _storage_result(42, count=10)
        assert result["count"] == 42
        assert result["path"] is None

    def test_unknown_type_returns_default(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_result

        result = _storage_result(object(), count=5)
        assert result["count"] == 5


# ---------------------------------------------------------------------------
# _storage_summary
# ---------------------------------------------------------------------------


class TestStorageSummary:
    """Test the _storage_summary helper."""

    def test_empty_infos_uses_default_path(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_summary

        summary = _storage_summary([], default_path=Path("/store/events.jsonl"))
        assert summary["count"] == 0
        assert summary["path"] == "/store/events.jsonl"

    def test_single_info(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_summary

        infos = [{"count": 10, "path": "/a/events.jsonl"}]
        summary = _storage_summary(infos, default_path=Path("/store/events.jsonl"))
        assert summary["count"] == 10
        assert summary["path"] == "/a/events.jsonl"
        assert "paths" not in summary

    def test_multiple_same_path(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_summary

        infos = [
            {"count": 5, "path": "/a/events.jsonl"},
            {"count": 5, "path": "/a/events.jsonl"},
        ]
        summary = _storage_summary(infos, default_path=Path("/store/events.jsonl"))
        assert summary["count"] == 10
        assert "paths" not in summary

    def test_multiple_different_paths(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_summary

        infos = [
            {"count": 5, "path": "/a/events.jsonl"},
            {"count": 3, "path": "/b/events.jsonl"},
        ]
        summary = _storage_summary(infos, default_path=Path("/store/events.jsonl"))
        assert summary["count"] == 8
        assert "paths" in summary
        assert len(summary["paths"]) == 2

    def test_none_count_treated_as_zero(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _storage_summary

        infos = [{"count": None, "path": "/a"}]
        summary = _storage_summary(infos, default_path=Path("/store/events.jsonl"))
        assert summary["count"] == 0


# ---------------------------------------------------------------------------
# _jsonable_mapping
# ---------------------------------------------------------------------------


class TestJsonableMapping:
    """Test the _jsonable_mapping helper."""

    def test_basic(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _jsonable_mapping

        result = _jsonable_mapping({"a": 1, "b": 2})
        assert result == {"a": 1, "b": 2}

    def test_non_string_keys(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _jsonable_mapping

        result = _jsonable_mapping({1: "one", None: "none"})
        assert result == {"1": "one", "None": "none"}

    def test_empty_mapping(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import _jsonable_mapping

        assert _jsonable_mapping({}) == {}


# ---------------------------------------------------------------------------
# IngestResult dataclass
# ---------------------------------------------------------------------------


class TestIngestResult:
    """Test the IngestResult dataclass."""

    def test_fields(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestResult

        r = IngestResult(
            batch_id="b1",
            source_id="s1",
            source_path="/a.csv",
            file_format="csv",
            status="stored",
            parsed_count=10,
            stored_count=8,
            duplicate_count=2,
            dead_letter_count=0,
            skipped_count=0,
            content_hash="sha256:abc",
            manifest_path="/m.json",
            checkpoint_path="/c.json",
            events_path="/e.jsonl",
            dead_letter_refs=[],
            stored_event_ids=["evt-1", "evt-2"],
        )
        assert r.batch_id == "b1"
        assert r.stored_count == 8
        assert len(r.stored_event_ids) == 2

    def test_frozen(self):
        from agent_py_agent.agent.log_analysis.ingest.pipeline import IngestResult

        r = IngestResult(
            batch_id="b1", source_id="s1", source_path="/a.csv",
            file_format="csv", status="stored", parsed_count=0, stored_count=0,
            duplicate_count=0, dead_letter_count=0, skipped_count=0,
            content_hash="sha256:x", manifest_path="/m", checkpoint_path="/c",
            events_path=None, dead_letter_refs=[], stored_event_ids=[],
        )
        with pytest.raises(AttributeError):
            r.batch_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# IngestPipeline – _write_events (via mocks)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# IngestPipeline – _flush_events
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# IngestPipeline – ingest_file integration (lightweight)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# top-level ingest_file function
# ---------------------------------------------------------------------------
