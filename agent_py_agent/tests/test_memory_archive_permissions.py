from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_archive.models import CompressionSnapshot
from agent_py_agent.agent.memory_archive.storage import (
    tighten_memory_archive_permissions,
    write_compression_snapshot_file,
)
from agent_py_agent.agent.memory_archive.tokens import TurnTokenUsage, append_session_token_usage
from agent_py_agent.agent.user_space import owner_maintenance

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_archive_writes_create_private_json_files_and_directories(tmp_path):
    owner_home = tmp_path / "owner"
    snapshot = CompressionSnapshot(
        snapshot_id="snapshot-1",
        session_id="session-1",
        compression_id="compact-1",
        turn_range=[1, 2],
        content="不应被其它本机用户读取。",
    )
    snapshot_path = write_compression_snapshot_file(owner_home, snapshot)
    token_result = append_session_token_usage(
        owner_home,
        usage=TurnTokenUsage("session-1", "turn-1", 1, 2, 3, "2026-10-03T10:00:00Z"),
    )
    token_path = Path(token_result["path"])

    for path in (snapshot_path, token_path):
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700


def test_existing_archive_permissions_are_tightened_without_following_symlinks(tmp_path):
    archive = tmp_path / "owner" / "memory_archive"
    nested = archive / "task_progress" / "task-1"
    nested.mkdir(parents=True)
    payload = nested / "progress.json"
    payload.write_text('{"content":"正文保持不变"}', encoding="utf-8")
    os.chmod(archive, 0o755)
    os.chmod(archive / "task_progress", 0o755)
    os.chmod(nested, 0o755)
    os.chmod(payload, 0o644)

    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "keep.json"
    external_file.write_text("外部文件", encoding="utf-8")
    os.chmod(outside, 0o755)
    os.chmod(external_file, 0o644)
    file_link = archive / "linked-file.json"
    dir_link = archive / "linked-dir"
    file_link.symlink_to(external_file)
    dir_link.symlink_to(outside, target_is_directory=True)
    original = payload.read_bytes()

    result = tighten_memory_archive_permissions(archive)

    assert result == {
        "tightened_count": 4,
        "tightened_files": 1,
        "tightened_directories": 3,
        "failed_count": 0,
        "failure_codes": {},
        "symlink_skipped_count": 2,
    }
    assert payload.read_bytes() == original
    assert _mode(payload) == 0o600
    assert {_mode(archive), _mode(archive / "task_progress"), _mode(nested)} == {0o700}
    assert _mode(external_file) == 0o644
    assert _mode(outside) == 0o755


def test_archive_permission_failures_have_stable_counts_and_codes(tmp_path, monkeypatch):
    import agent_py_agent.agent.memory_archive.storage as storage

    archive = tmp_path / "owner" / "memory_archive"
    archive.mkdir(parents=True)
    protected = archive / "protected.txt"
    protected.write_text("body", encoding="utf-8")
    os.chmod(protected, 0o644)
    original_chmod = storage.os.chmod

    def fail_one(path, mode, *, follow_symlinks=True):
        if Path(path) == protected:
            raise PermissionError("injected permission denial")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(storage.os, "chmod", fail_one)

    result = tighten_memory_archive_permissions(archive)

    assert result["failed_count"] == 1
    assert result["failure_codes"] == {"permission_denied": 1}
    assert result["tightened_count"] == result["tightened_files"] + result["tightened_directories"]
    assert _mode(protected) == 0o644


def test_owner_maintenance_persists_archive_permission_receipt(tmp_path, monkeypatch):
    archive = tmp_path / "owner" / "memory_archive"
    archive.mkdir(parents=True)
    payload = archive / "old.txt"
    payload.write_text("body", encoding="utf-8")
    os.chmod(payload, 0o644)
    os.chmod(archive, 0o755)
    retention = SimpleNamespace(
        applied=True,
        legal_hold=False,
        load_errors=[],
        actions=[],
        isolated_errors=[],
        to_dict=lambda: {},
    )
    home = SimpleNamespace(owner_home_dir=archive.parent, memory_archive_dir=archive)
    monkeypatch.setattr(owner_maintenance, "owner_maintenance_due", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(owner_maintenance, "apply_owner_retention", lambda *_args, **_kwargs: retention)
    monkeypatch.setattr(owner_maintenance, "_outcome_fields", lambda *_args: {
        "status": "success",
        "apply_outcome": "applied",
        "isolated_error_count": 0,
        "failed_action_count": 0,
        "last_success_at": 1.0,
        "last_applied_at": 1.0,
        "action_count": 0,
    })
    monkeypatch.setattr(owner_maintenance, "_reclaim_text_vector_cache_orphans", lambda *_args: (0, ""))
    monkeypatch.setattr(owner_maintenance, "_compact_global_indexes", lambda *_args, **_kwargs: ([], []))

    owner_maintenance.run_owner_retention_if_due(home, now=1.0)

    state = json.loads((archive.parent / "data" / "maintenance.json").read_text(encoding="utf-8"))
    assert state["memory_archive_permissions"] == {
        "tightened_count": 2,
        "tightened_files": 1,
        "tightened_directories": 1,
        "failed_count": 0,
        "failure_codes": {},
        "symlink_skipped_count": 0,
    }
    assert _mode(payload) == 0o600
