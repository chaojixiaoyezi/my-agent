"""Tests for memory_archive/storage.py - memory archive storage primitives."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.memory_archive.models import CompressionSnapshot, RawMemoryEvent


class TestFilterSnapshotForLevel:
    """Test filter_snapshot_for_level function."""

    def test_level_0_preserves_all_fields(self):
        """Test level 0 preserves all fields."""
        from agent_py_agent.agent.memory_archive.storage import filter_snapshot_for_level

        payload = {
            "session_id": "test-123",
            "content": "Hello world",
            "user_intents": ["intent1", "intent2"],
            "assistant_actions": ["action1", "action2"],
            "decisions": ["decision1"],
            "open_questions": ["question1"],
            "tool_calls": [
                {"name": "read_file", "parameters_preview": "file.txt"}
            ],
        }

        result = filter_snapshot_for_level(payload, 0)

        assert result["content"] == "Hello world"
        assert len(result["user_intents"]) == 2
        assert len(result["assistant_actions"]) == 2
        assert len(result["decisions"]) == 1
        assert result["tool_calls"][0]["parameters_preview"] == "file.txt"

    def test_level_1_clears_tool_params_preview(self):
        """Test level 1 clears tool call parameters preview."""
        from agent_py_agent.agent.memory_archive.storage import filter_snapshot_for_level

        payload = {
            "session_id": "test-123",
            "content": "Hello",
            "tool_calls": [
                {"name": "read_file", "parameters_preview": "file.txt"},
                {"name": "write_file", "parameters_preview": "new content"},
            ],
        }

        result = filter_snapshot_for_level(payload, 1)

        assert result["tool_calls"][0]["parameters_preview"] == ""
        assert result["tool_calls"][1]["parameters_preview"] == ""

    def test_level_2_limits_intents_and_actions(self):
        """Test level 2 limits intents and actions to 1 each, clears decisions."""
        from agent_py_agent.agent.memory_archive.storage import filter_snapshot_for_level

        payload = {
            "session_id": "test-123",
            "content": "Hello",
            "user_intents": ["intent1", "intent2", "intent3"],
            "assistant_actions": ["action1", "action2"],
            "decisions": ["decision1", "decision2"],
            "open_questions": ["question1", "question2"],
        }

        result = filter_snapshot_for_level(payload, 2)

        assert len(result["user_intents"]) == 1
        assert len(result["assistant_actions"]) == 1
        assert len(result["decisions"]) == 0
        assert len(result["open_questions"]) == 0

    def test_level_3_minimal_output(self):
        """Test level 3 produces minimal output."""
        from agent_py_agent.agent.memory_archive.storage import filter_snapshot_for_level

        payload = {
            "session_id": "test-123",
            "content": "Hello world",
            "user_intents": ["intent1"],
            "assistant_actions": ["action1"],
            "tool_calls": [
                {"name": "read_file", "parameters_preview": "file.txt"}
            ],
        }

        result = filter_snapshot_for_level(payload, 3)

        assert result["user_intents"] == []
        assert result["assistant_actions"] == []
        assert result["tool_calls"] == []
        assert result["content"] == ""

    def test_level_clamped_to_0_3(self):
        """Test that level is clamped to 0-3 range."""
        from agent_py_agent.agent.memory_archive.storage import filter_snapshot_for_level

        payload = {"session_id": "test-123"}

        # Test negative level becomes 0
        result = filter_snapshot_for_level(payload, -1)
        assert "session_id" in result  # Full data preserved

        # Test level > 3 becomes 3
        result = filter_snapshot_for_level(payload, 10)
        assert result["session_id"] == "test-123"

    def test_missing_fields_handled(self):
        """Test that missing fields in payload are handled gracefully."""
        from agent_py_agent.agent.memory_archive.storage import filter_snapshot_for_level

        payload = {"session_id": "test-123"}  # Missing most fields

        result = filter_snapshot_for_level(payload, 2)

        # Should not raise, should have empty lists for missing fields
        assert result.get("user_intents") == []
        assert result.get("assistant_actions") == []


class TestFilterRawEventForLevel:
    """Test filter_raw_event_for_level function."""

    def test_level_0_preserves_all(self):
        """Test level 0 preserves all fields."""
        from agent_py_agent.agent.memory_archive.storage import filter_raw_event_for_level

        payload = {
            "event_type": "user_message",
            "content": "Hello",
            "content_path": "/path/to/content",
        }

        result = filter_raw_event_for_level(payload, 0)

        assert result["content"] == "Hello"
        assert result["content_path"] == "/path/to/content"

    def test_level_2_clears_content_path(self):
        """Test level 2 and above clears content_path."""
        from agent_py_agent.agent.memory_archive.storage import filter_raw_event_for_level

        payload = {
            "event_type": "user_message",
            "content": "Hello",
            "content_path": "/path/to/content",
        }

        result = filter_raw_event_for_level(payload, 2)

        assert result["content_path"] == ""

    def test_level_3_also_clears_content_path(self):
        """Test level 3 also clears content_path."""
        from agent_py_agent.agent.memory_archive.storage import filter_raw_event_for_level

        payload = {
            "event_type": "user_message",
            "content": "Hello",
            "content_path": "/path/to/content",
        }

        result = filter_raw_event_for_level(payload, 3)

        assert result["content_path"] == ""


class TestSnapshotPathFor:
    """Test snapshot_path_for function."""

    def test_returns_hook_file_path(self):
        """Test returns path to memory/hooks/YYYY-MM-DD.jsonl."""
        from agent_py_agent.agent.memory_archive.storage import snapshot_path_for

        path = snapshot_path_for("/tmp/project")

        assert "memory" in str(path)
        assert "hooks" in str(path)
        assert str(path).endswith(".jsonl")

    def test_accepts_none_for_created_at(self):
        """Test accepts None for created_at (uses today)."""
        from agent_py_agent.agent.memory_archive.storage import snapshot_path_for

        path = snapshot_path_for("/tmp/project", created_at=None)

        assert path.name.endswith(".jsonl")

    def test_accepts_iso_string(self):
        """Test accepts ISO date string."""
        from agent_py_agent.agent.memory_archive.storage import snapshot_path_for

        path = snapshot_path_for("/tmp/project", created_at="2026-05-01T10:00:00Z")

        assert "2026-05-01" in str(path)

    def test_accepts_unix_timestamp(self):
        """Test accepts Unix timestamp."""
        from agent_py_agent.agent.memory_archive.storage import snapshot_path_for

        path = snapshot_path_for("/tmp/project", created_at=1746300000.0)

        # Should convert to date string
        assert path.name.endswith(".jsonl")


class TestCompressionSnapshotFileFor:
    """Test compression_snapshot_file_for function."""

    def test_creates_safe_filename(self):
        """Test creates safe filename from snapshot_id."""
        from agent_py_agent.agent.memory_archive.storage import compression_snapshot_file_for

        snapshot = MagicMock(spec=CompressionSnapshot)
        snapshot.snapshot_id = "snap-123"
        snapshot.created_at = "2026-05-01T10:00:00Z"

        path = compression_snapshot_file_for("/tmp/project", snapshot)

        assert "2026-05-01" in str(path)
        assert "snap-123" in str(path)
        assert path.suffix == ".json"

    def test_sanitizes_special_chars_in_id(self):
        """Test sanitizes special characters in snapshot_id."""
        from agent_py_agent.agent.memory_archive.storage import compression_snapshot_file_for

        snapshot = MagicMock(spec=CompressionSnapshot)
        snapshot.snapshot_id = "snap-123!@#$%^&*()"
        snapshot.created_at = "2026-05-01"

        path = compression_snapshot_file_for("/tmp/project", snapshot)

        # Should not have special chars in filename
        assert "!" not in path.name
        assert "@" not in path.name


class TestRawEventPathFor:
    """Test raw_event_path_for function."""

    def test_returns_raw_file_path(self):
        """Test returns path to memory/raw/YYYY-MM-DD.jsonl."""
        from agent_py_agent.agent.memory_archive.storage import raw_event_path_for

        path = raw_event_path_for("/tmp/project")

        assert "memory" in str(path)
        assert "raw" in str(path)
        assert str(path).endswith(".jsonl")

    def test_accepts_iso_string(self):
        """Test accepts ISO date string."""
        from agent_py_agent.agent.memory_archive.storage import raw_event_path_for

        path = raw_event_path_for("/tmp/project", created_at="2026-05-01T10:00:00Z")

        assert "2026-05-01" in str(path)


class TestAppendSnapshot:
    """Test append_snapshot function - basic verification tests."""

    def test_snapshot_path_for_with_timestamp(self, tmp_path: Path):
        """Test snapshot path generation with timestamp."""
        from agent_py_agent.agent.memory_archive.storage import (
            snapshot_path_for,
            compression_snapshot_file_for,
        )

        snapshot = MagicMock(spec=CompressionSnapshot)
        snapshot.snapshot_id = "snap-001"
        snapshot.created_at = "2026-05-01"

        hook_path = snapshot_path_for(tmp_path)
        snapshot_file = compression_snapshot_file_for(tmp_path, snapshot)

        assert "hooks" in str(hook_path)
        assert "2026-05-01" in str(snapshot_file)
        assert snapshot_file.suffix == ".json"

    def test_raw_event_path_for(self, tmp_path: Path):
        """Test raw event path generation."""
        from agent_py_agent.agent.memory_archive.storage import raw_event_path_for

        path = raw_event_path_for(tmp_path, "2026-05-01")

        assert "raw" in str(path)
        assert "2026-05-01" in str(path)
        assert path.suffix == ".jsonl"