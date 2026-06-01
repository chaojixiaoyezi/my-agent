from __future__ import annotations

"""Tests for user space module."""

import json
from pathlib import Path

import pytest


class TestLegacyUserPaths:
    """Test LegacyUserPaths and get_legacy_user_paths."""

    def test_get_legacy_user_paths_basic(self, tmp_path: Path):
        """get_legacy_user_paths returns old data/users paths for a user."""
        from agent_py_agent.agent.user_space.legacy_user_paths import get_legacy_user_paths

        paths = get_legacy_user_paths("alice", tmp_path / "data" / "users")

        assert paths.user_id == "alice"
        assert paths.root_dir == tmp_path / "data" / "users" / "alice"
        assert paths.memory_path == tmp_path / "data" / "users" / "alice" / "memory.jsonl"
        assert paths.subagent_workspace == tmp_path / "data" / "users" / "alice" / "subagents"
        assert paths.gateway_workspace == tmp_path / "data" / "users" / "alice" / "gateway"
        assert paths.sessions_dir == tmp_path / "data" / "users" / "alice" / "sessions"
        assert paths.local_store_path == tmp_path / "data" / "users" / "alice" / "local_store" / "local.db"

    def test_get_legacy_user_paths_admin(self, tmp_path: Path):
        """admin user gets expected paths."""
        from agent_py_agent.agent.user_space.legacy_user_paths import get_legacy_user_paths

        paths = get_legacy_user_paths("admin", tmp_path / "data" / "users")

        assert paths.user_id == "admin"
        assert paths.root_dir == tmp_path / "data" / "users" / "admin"

    def test_get_legacy_user_paths_with_string(self, tmp_path: Path):
        """get_legacy_user_paths accepts string path."""
        from agent_py_agent.agent.user_space.legacy_user_paths import get_legacy_user_paths

        paths = get_legacy_user_paths("bob", str(tmp_path / "users"))

        assert paths.user_id == "bob"
        assert paths.root_dir == tmp_path / "users" / "bob"


class TestUserSpaceManager:
    """Test UserSpaceManager."""

    def test_ensure_user_space_creates_directories(self, tmp_path: Path):
        """ensure_user_space creates all required directories."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")
        paths = manager.ensure_user_space("alice")

        assert paths.root_dir.exists()
        assert paths.subagent_workspace.exists()
        assert paths.gateway_workspace.exists()
        assert paths.sessions_dir.exists()
        assert (paths.root_dir / "local_store").exists()

    def test_get_legacy_user_paths_no_create(self, tmp_path: Path):
        """get_legacy_user_paths does not create directories."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")
        paths = manager.get_legacy_user_paths("alice")

        # Should not create directory
        assert not paths.root_dir.exists()

    def test_list_users_empty(self, tmp_path: Path):
        """list_users returns empty list when no users exist."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")
        assert manager.list_users() == []

    def test_list_users_with_data(self, tmp_path: Path):
        """list_users returns list of existing users."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")

        # Create some user directories manually
        (tmp_path / "data" / "users" / "alice").mkdir(parents=True)
        (tmp_path / "data" / "users" / "bob").mkdir(parents=True)
        (tmp_path / "data" / "users" / ".hidden").mkdir(parents=True)  # Hidden dir

        users = manager.list_users()
        assert "alice" in users
        assert "bob" in users
        assert ".hidden" not in users

    def test_user_exists(self, tmp_path: Path):
        """user_exists returns correct status."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")
        (tmp_path / "data" / "users" / "alice").mkdir(parents=True)

        assert manager.user_exists("alice")
        assert not manager.user_exists("bob")

    def test_is_admin(self, tmp_path: Path):
        """is_admin correctly identifies admin user."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")

        assert manager.is_admin("admin")
        assert not manager.is_admin("alice")
        assert not manager.is_admin("bob")

    def test_can_access_user_admin(self, tmp_path: Path):
        """admin can access any user."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")

        assert manager.can_access_user("admin", "alice")
        assert manager.can_access_user("admin", "bob")
        assert manager.can_access_user("admin", "admin")

    def test_can_access_user_self(self, tmp_path: Path):
        """users can access their own data."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")

        assert manager.can_access_user("alice", "alice")
        assert manager.can_access_user("bob", "bob")

    def test_can_access_user_other_denied(self, tmp_path: Path):
        """users cannot access other user's data."""
        from agent_py_agent.agent.user_space.manager import UserSpaceManager

        manager = UserSpaceManager(tmp_path / "data" / "users")

        assert not manager.can_access_user("alice", "bob")


class TestMigration:
    """Test migration functions."""

    def test_check_needs_migration_true(self, tmp_path: Path):
        """check_needs_migration returns True when no user data exists."""
        from agent_py_agent.agent.user_space.migration import check_needs_migration

        # data/users doesn't exist
        assert check_needs_migration(tmp_path)

    def test_check_needs_migration_false(self, tmp_path: Path):
        """check_needs_migration returns False when user data exists."""
        from agent_py_agent.agent.user_space.migration import check_needs_migration

        (tmp_path / "data" / "users" / "admin").mkdir(parents=True)
        assert not check_needs_migration(tmp_path)

    def test_get_migration_status(self, tmp_path: Path):
        """get_migration_status returns correct status."""
        from agent_py_agent.agent.user_space.migration import get_migration_status

        status = get_migration_status(tmp_path)
        assert status["needs_migration"] is True
        assert status["users"] == []

        # Create some users
        (tmp_path / "data" / "users" / "alice").mkdir(parents=True)
        (tmp_path / "data" / "users" / "bob").mkdir(parents=True)

        status = get_migration_status(tmp_path)
        assert status["needs_migration"] is False
        assert "alice" in status["users"]
        assert "bob" in status["users"]


class TestIntegration:
    """Integration tests for user space with config."""

    def test_config_user_id_field(self, tmp_path: Path):
        """AgentConfig accepts user_id field."""
        from agent_py_agent.agent.settings.config import AgentConfig

        config = AgentConfig(user_id="alice", user_data_root="data/users")
        assert config.user_id == "alice"
        assert config.user_data_root == "data/users"

    def test_config_default_values(self, tmp_path: Path):
        """AgentConfig has correct defaults."""
        from agent_py_agent.agent.settings.config import AgentConfig

        config = AgentConfig()
        assert config.user_id == "admin"
        assert config.user_data_root == "data/users"

    def test_load_config_with_user_fields(self, tmp_path: Path):
        """load_config loads user_id and user_data_root."""
        from agent_py_agent.agent.settings.config import load_config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "user_id: alice\nuser_data_root: data/users\n",
            encoding="utf-8",
        )

        config = load_config(config_path)
        assert config.user_id == "alice"
        assert config.user_data_root == "data/users"

    def test_get_legacy_user_paths_integration(self, tmp_path: Path):
        """get_legacy_user_paths works with legacy config values."""
        from agent_py_agent.agent.settings.config import AgentConfig
        from agent_py_agent.agent.user_space.legacy_user_paths import get_legacy_user_paths

        config = AgentConfig()
        user_id = config.user_data_root.split("/")[0]  # "data"
        base = tmp_path

        # This mimics what SimpleAgent does
        user_data_root = base / config.user_data_root
        paths = get_legacy_user_paths(config.user_id, user_data_root)

        assert paths.user_id == "admin"
        assert paths.root_dir == tmp_path / "data" / "users" / "admin"
