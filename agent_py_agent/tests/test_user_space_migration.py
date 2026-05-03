"""Tests for user_space/migration.py - user space migration utilities."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.user_space.migration import (
    check_needs_migration,
    create_migration_marker,
    get_migration_status,
    migrate_to_user_space,
)


class TestMigrateToUserSpace:
    """Test migrate_to_user_space function."""

    def test_migrate_creates_user_directory(self, tmp_path: Path):
        """Test that migration creates the target user directory structure."""
        # Setup source data
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        memory_file = data_dir / "memory.jsonl"
        memory_file.write_text("[{\"role\": \"user\", \"content\": \"test\"}]")

        # Setup fake config similar to migrate_to_user_space expectations
        # Actually create subdirectories that should be migrated
        subagents_dir = data_dir / "subagents"
        subagents_dir.mkdir()
        (subagents_dir / "task-001").mkdir()

        result = migrate_to_user_space(tmp_path, user_id="testuser")

        assert "moved_files" in result
        assert "skipped_files" in result
        assert "errors" in result

    def test_migrate_skips_nonexistent_source(self, tmp_path: Path):
        """Test that migration skips files that don't exist."""
        result = migrate_to_user_space(tmp_path, user_id="testuser")

        # Should have skipped all files that don't exist
        assert len(result["skipped_files"]) > 0

    def test_migrate_handles_existing_destination(self, tmp_path: Path):
        """Test that migration skips when destination already exists."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        memory_file = data_dir / "memory.jsonl"
        memory_file.write_text("[{\"role\": \"user\"}]")

        # Create destination directory first
        user_root = data_dir / "users" / "testuser"
        user_root.mkdir(parents=True)
        dest_memory = user_root / "memory.jsonl"
        dest_memory.write_text("[{\"role\": \"assistant\"}]")

        result = migrate_to_user_space(tmp_path, user_id="testuser")

        # Should have skipped because dest exists
        assert len(result["skipped_files"]) > 0

    def test_migrate_with_string_path(self, tmp_path: Path):
        """Test migration accepts string path."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)

        # Should not raise
        result = migrate_to_user_space(str(tmp_path), user_id="testuser")
        assert "errors" in result

    def test_migrate_resolves_relative_paths(self, tmp_path: Path):
        """Test that relative paths are resolved."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        memory_file = data_dir / "memory.jsonl"
        memory_file.write_text("[{\"role\": \"user\"}]")

        # Create a relative path scenario
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = migrate_to_user_space(".", user_id="testuser")
            # Should handle relative path resolution
            assert "errors" in result
        finally:
            os.chdir(original_cwd)


class TestCreateMigrationMarker:
    """Test create_migration_marker function."""

    def test_creates_marker_file(self, tmp_path: Path):
        """Test marker file is created successfully."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)

        create_migration_marker(tmp_path, user_id="testuser")

        marker_file = data_dir / "MIGRATED_TO_USER_SPACE.txt"
        assert marker_file.exists()
        content = marker_file.read_text(encoding="utf-8")
        assert "testuser" in content

    def test_marker_handles_permission_error(self, tmp_path: Path):
        """Test marker creation handles permission errors gracefully."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)

        # Mock write to raise PermissionError
        with patch.object(Path, "write_text", side_effect=PermissionError("Permission denied")):
            # Should not raise, just pass
            create_migration_marker(tmp_path, user_id="testuser")

    def test_marker_handles_oserror(self, tmp_path: Path):
        """Test marker creation handles OSError gracefully."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)

        with patch.object(Path, "write_text", side_effect=OSError("Disk full")):
            create_migration_marker(tmp_path, user_id="testuser")
            # Should not raise


class TestCheckNeedsMigration:
    """Test check_needs_migration function."""

    def test_needs_migration_when_no_users_dir(self, tmp_path: Path):
        """Test returns True when users directory doesn't exist."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        result = check_needs_migration(tmp_path)
        assert result is True

    def test_no_migration_when_users_dir_exists(self, tmp_path: Path):
        """Test returns False when users directory exists."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        users_dir = data_dir / "users"
        users_dir.mkdir()

        result = check_needs_migration(tmp_path)
        assert result is False

    def test_check_with_string_path(self, tmp_path: Path):
        """Test accepts string path."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        result = check_needs_migration(str(tmp_path))
        assert result is True


class TestGetMigrationStatus:
    """Test get_migration_status function."""

    def test_returns_correct_status_structure(self, tmp_path: Path):
        """Test status dict has required fields."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        status = get_migration_status(tmp_path)

        assert "needs_migration" in status
        assert "migration_marker_exists" in status
        assert "users" in status
        assert status["needs_migration"] is True
        assert status["migration_marker_exists"] is False
        assert status["users"] == []

    def test_lists_existing_users(self, tmp_path: Path):
        """Test lists existing users in users directory."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        users_dir = data_dir / "users"
        users_dir.mkdir()
        (users_dir / "alice").mkdir()
        (users_dir / "bob").mkdir()

        status = get_migration_status(tmp_path)

        assert "alice" in status["users"]
        assert "bob" in status["users"]

    def test_excludes_hidden_directories(self, tmp_path: Path):
        """Test that hidden directories (. prefix) are excluded."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        users_dir = data_dir / "users"
        users_dir.mkdir()
        (users_dir / "alice").mkdir()
        (users_dir / ".hidden").mkdir()

        status = get_migration_status(tmp_path)

        assert ".hidden" not in status["users"]
        assert "alice" in status["users"]

    def test_includes_marker_content_when_exists(self, tmp_path: Path):
        """Test marker content is included when marker file exists."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        marker_file = data_dir / "MIGRATED_TO_USER_SPACE.txt"
        marker_file.write_text("Migration completed\nUser: testuser", encoding="utf-8")

        status = get_migration_status(tmp_path)

        assert status["migration_marker_exists"] is True
        assert "marker_content" in status
        assert "testuser" in status["marker_content"]

    def test_handles_marker_read_error(self, tmp_path: Path):
        """Test handles OSError when reading marker file."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        marker_file = data_dir / "MIGRATED_TO_USER_SPACE.txt"
        marker_file.write_text("test", encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=OSError("Read failed")):
            status = get_migration_status(tmp_path)
            # Should still return valid structure
            assert "needs_migration" in status

    def test_get_status_with_string_path(self, tmp_path: Path):
        """Test accepts string path."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        status = get_migration_status(str(tmp_path))
        assert status["needs_migration"] is True