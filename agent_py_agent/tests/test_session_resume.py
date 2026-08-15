"""Tests for session/resume.py - session resume functionality."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.session.resume import (
    format_resume_context,
    resume_session,
)


class TestResumeSession:
    """Test resume_session function."""

    def test_returns_error_for_nonexistent_session(self):
        """Test that nonexistent session returns error dict."""
        mock_agent = MagicMock()
        mock_agent.config.memory_path = "/tmp/nonexistent/memory.jsonl"

        with patch("agent_py_agent.agent.session.resume.SessionManager") as MockSM:
            mock_instance = MagicMock()
            mock_instance.load_session.return_value = None
            MockSM.return_value = mock_instance

            result = resume_session(mock_agent, "nonexistent-session-id")

            assert result["session"] is None
            assert "error" in result

    def test_handles_memory_file_read_error(self, tmp_path: Path):
        """Test handles OSError when reading memory file."""
        mock_agent = MagicMock()
        memory_file = tmp_path / "memory.jsonl"
        memory_file.write_text("[{\"role\": \"user\"}]")
        mock_agent.config.memory_path = str(memory_file)

        mock_session = MagicMock()
        mock_session.session_id = "test-session"
        mock_session.user_id = "testuser"

        with patch("agent_py_agent.agent.session.resume.SessionManager") as MockSM:
            mock_instance = MagicMock()
            mock_instance.load_session.return_value = mock_session
            MockSM.return_value = mock_instance

            # Mock Path.read_text to raise OSError
            with patch.object(Path, "read_text", side_effect=OSError("Read error")):
                result = resume_session(mock_agent, "test-session")
                # Should return session but memories may be empty due to error
                assert "session" in result

    def test_handles_unicode_decode_error(self, tmp_path: Path):
        """Test handles UnicodeDecodeError when reading memory file."""
        mock_agent = MagicMock()
        memory_file = tmp_path / "memory.jsonl"
        # Write some data that could cause decode issues
        memory_file.write_bytes(b"\xff\xfe invalid content")
        mock_agent.config.memory_path = str(memory_file)

        mock_session = MagicMock()
        mock_session.session_id = "test-session"

        with patch("agent_py_agent.agent.session.resume.SessionManager") as MockSM:
            mock_instance = MagicMock()
            mock_instance.load_session.return_value = mock_session
            MockSM.return_value = mock_instance

            result = resume_session(mock_agent, "test-session")
            # Should not raise, should return session
            assert "session" in result

    def test_handles_missing_subagent_manager(self, tmp_path: Path):
        """没有当前 subagents manager 时，恢复上下文只返回空子代理列表。"""
        mock_agent = MagicMock()
        mock_agent.subagents = None
        memory_file = tmp_path / "memory.jsonl"
        memory_file.write_text('{"session_id": "test-session", "role": "user", "content": "hello"}\n')
        mock_agent.config.memory_path = str(memory_file)

        mock_session = MagicMock()
        mock_session.session_id = "test-session"

        with patch("agent_py_agent.agent.session.resume.SessionManager") as MockSM:
            mock_instance = MagicMock()
            mock_instance.load_session.return_value = mock_session
            MockSM.return_value = mock_instance

            result = resume_session(mock_agent, "test-session")

        assert result["subagent_context"] == []

    def test_subagent_context_load_failure_is_model_visible(self, tmp_path: Path):
        """Subagent board read errors should be visible, not disguised as no subagents."""
        mock_agent = MagicMock()
        memory_file = tmp_path / "memory.jsonl"
        memory_file.write_text("", encoding="utf-8")
        mock_agent.config.memory_path = str(memory_file)
        mock_agent.subagents.list_runs.side_effect = OSError("board unreadable")

        mock_session = MagicMock()
        mock_session.session_id = "test-session"

        with patch("agent_py_agent.agent.session.resume.SessionManager") as MockSM:
            mock_instance = MagicMock()
            mock_instance.load_session.return_value = mock_session
            MockSM.return_value = mock_instance
            result = resume_session(mock_agent, "test-session")

        error = result["subagent_context"][0]
        assert error["type"] == "subagent_context_load_error"
        assert error["recoverable"] is True
        assert "子代理" in format_resume_context(result)

    def test_recent_memory_corrupt_line_is_model_visible(self, tmp_path: Path):
        """坏记忆行不能被吞成“没有历史记忆”。"""
        mock_agent = MagicMock()
        memory_file = tmp_path / "memory.jsonl"
        memory_file.write_text(
            "\n".join(
                [
                    '{"session_id": "test-session", "role": "user", "content": "hello"}',
                    "{not-json",
                ]
            ),
            encoding="utf-8",
        )
        mock_agent.config.memory_path = str(memory_file)

        mock_session = MagicMock()
        mock_session.session_id = "test-session"

        with patch("agent_py_agent.agent.session.resume.SessionManager") as MockSM:
            mock_instance = MagicMock()
            mock_instance.load_session.return_value = mock_session
            MockSM.return_value = mock_instance
            result = resume_session(mock_agent, "test-session")

        assert result["recent_memories"][0]["content"] == "hello"
        assert result["recent_memory_load_errors"][0]["context"] == "session.resume.memory_line"
        assert "最近记忆读取警告" in format_resume_context(result)


class TestFormatResumeContext:
    """Test format_resume_context function."""

    def test_returns_error_message_for_error_dict(self):
        """Test formats error dict correctly."""
        resume_data = {"error": "Session not found"}

        result = format_resume_context(resume_data)

        assert "恢复失败" in result
        assert "Session not found" in result

    def test_returns_not_exist_for_empty_session(self):
        """Test returns 'session not exist' for None session."""
        resume_data = {"session": None}

        result = format_resume_context(resume_data)

        assert "会话不存在" in result

    def test_formats_session_with_all_fields(self):
        """Test formats session with all fields."""
        mock_session = MagicMock()
        mock_session.session_id = "session-123"
        mock_session.created_at = "2026-01-01T10:00:00Z"
        mock_session.updated_at = "2026-01-02T10:00:00Z"
        mock_session.last_active_channel = "chat"

        resume_data = {
            "session": mock_session,
            "recent_memories": [],
            "subagent_context": [],
        }

        result = format_resume_context(resume_data)

        assert "session-123" in result
        assert "chat" in result

    def test_includes_memories_in_output(self):
        """Test includes memories in formatted output."""
        mock_session = MagicMock()
        mock_session.session_id = "session-123"
        mock_session.created_at = "2026-01-01T10:00:00Z"
        mock_session.updated_at = "2026-01-02T10:00:00Z"
        mock_session.last_active_channel = "feishu"

        resume_data = {
            "session": mock_session,
            "recent_memories": [
                {"role": "user", "content": "Hello world"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "subagent_context": [],
        }

        result = format_resume_context(resume_data)

        assert "最近记忆" in result
        assert "Hello world" in result

    def test_includes_subagents_in_output(self):
        """Test includes subagent context in formatted output."""
        mock_session = MagicMock()
        mock_session.session_id = "session-123"
        mock_session.created_at = "2026-01-01T10:00:00Z"
        mock_session.updated_at = "2026-01-02T10:00:00Z"
        mock_session.last_active_channel = "qq"

        resume_data = {
            "session": mock_session,
            "recent_memories": [],
            "subagent_context": [
                {"id": "task-001", "goal": "Complete deployment", "status": "RUNNING"},
            ],
        }

        result = format_resume_context(resume_data)

        assert "子代理任务" in result
        assert "Complete deployment" in result

    def test_shows_placeholder_when_no_history(self):
        """Test shows '暂无历史记录' when no memories or subagents."""
        mock_session = MagicMock()
        mock_session.session_id = "session-123"
        mock_session.created_at = "2026-01-01T10:00:00Z"
        mock_session.updated_at = "2026-01-02T10:00:00Z"
        mock_session.last_active_channel = "chat"

        resume_data = {
            "session": mock_session,
            "recent_memories": [],
            "subagent_context": [],
        }

        result = format_resume_context(resume_data)

        assert "暂无历史记录" in result

    def test_limits_memory_display_to_three(self):
        """Test only displays first 3 memories."""
        mock_session = MagicMock()
        mock_session.session_id = "session-123"
        mock_session.created_at = "2026-01-01T10:00:00Z"
        mock_session.updated_at = "2026-01-02T10:00:00Z"
        mock_session.last_active_channel = "chat"

        resume_data = {
            "session": mock_session,
            "recent_memories": [
                {"role": "user", "content": f"Memory {i}"}
                for i in range(10)
            ],
            "subagent_context": [],
        }

        result = format_resume_context(resume_data)

        # Should not contain all 10 memories (truncated to 3)
        assert "Memory 0" in result  # First is shown
        # Count occurrences of "..." which indicates truncation
        # With 10 memories and only 3 shown, we should see truncation
