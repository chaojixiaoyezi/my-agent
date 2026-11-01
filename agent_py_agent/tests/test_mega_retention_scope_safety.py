"""维护仅触碰该owner规范会话树；坏文件和其它owner均不得被顺手清掉。"""
from __future__ import annotations

import os
import time

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent


def test_session_pair_retention_is_owner_isolated(tmp_path):
    now = time.time()
    homes = [ensure_my_agent_home(tmp_path / name) for name in ("host-a", "host-b")]
    paths = []
    for home in homes:
        workspace = runtime_paths_for_agent(None, home.owner_home_dir, home)["conversation_workspace"]
        limiter = ConversationStore(workspace).session_pair_rate
        limiter.record("old", "target", now=now - 14400)
        paths.append(limiter._path("old", "target"))
    run_owner_retention_if_due(homes[0], now=now, quota_max_entries=128, quota_max_ms=100)
    assert not paths[0].exists()
    assert paths[1].exists()


def test_bad_bucket_and_link_are_preserved_without_touching_link_target(tmp_path):
    home = ensure_my_agent_home(tmp_path / "host")
    workspace = runtime_paths_for_agent(None, home.owner_home_dir, home)["conversation_workspace"]
    limiter = ConversationStore(workspace).session_pair_rate
    directory = limiter._path("a", "b").parent
    bad = directory / "bad.bucket.json"
    bad.write_text("{broken")
    outside = tmp_path / "other-owner.json"
    outside.write_text('{"schema_version":"session_pair_rate.v1","window_start":0,"count":1}')
    link = directory / "link.bucket.json"
    link.symlink_to(outside)
    before = outside.read_bytes()
    os.utime(bad, (1, 1))
    run_owner_retention_if_due(home, now=time.time(), quota_max_entries=128, quota_max_ms=100)
    assert bad.read_text() == "{broken" and link.is_symlink()
    assert outside.read_bytes() == before


def test_cleanup_pins_original_parent_fd_across_ancestor_symlink_replacement(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.conversation.session_pair_rate import (
        SessionPairRetentionRequest,
        cleanup_session_pair_rates,
    )
    from agent_py_agent.agent.gateway_parts import io

    home = ensure_my_agent_home(tmp_path / "host")
    workspace = runtime_paths_for_agent(None, home.owner_home_dir, home)["conversation_workspace"]
    limiter = ConversationStore(workspace).session_pair_rate
    now = time.time()
    limiter.record("old", "target", now=now - 14400)
    path = limiter._path("old", "target")
    outside = tmp_path / "other-owner"
    directory = outside / "session_pair_rate"
    directory.mkdir(parents=True)
    sentinel = directory / path.name
    sentinel.write_bytes(path.read_bytes())
    sentinel.with_suffix(".json.lock").write_text("")
    original = io.try_existing_file_transition_at
    changed = []

    @contextmanager
    def swap(descriptor, target):
        if not changed:
            workspace.rename(workspace.with_name("moved-conversations"))
            workspace.symlink_to(outside, target_is_directory=True)
            changed.append(True)
        with original(descriptor, target) as acquired:
            yield acquired

    monkeypatch.setattr(io, "try_existing_file_transition_at", swap)
    cleanup_session_pair_rates(SessionPairRetentionRequest(home, now))
    assert changed and sentinel.exists() and sentinel.with_suffix(".json.lock").exists()


def test_active_orphan_lock_inode_is_not_unlinked_then_can_be_reclaimed(tmp_path):
    from agent_py_agent.agent.conversation.session_pair_rate import (
        SessionPairRetentionRequest,
        cleanup_session_pair_rates,
    )
    from agent_py_agent.agent.gateway_parts.io import locked_file_transition

    home = ensure_my_agent_home(tmp_path / "host")
    workspace = runtime_paths_for_agent(None, home.owner_home_dir, home)["conversation_workspace"]
    limiter = ConversationStore(workspace).session_pair_rate
    now = time.time()
    limiter.record("old", "target", now=now - 14400)
    path = limiter._path("old", "target")
    lock = path.with_suffix(".json.lock")
    os.utime(lock, (1, 1))
    inode = lock.stat().st_ino
    with locked_file_transition(path):
        path.unlink()
        cleanup_session_pair_rates(SessionPairRetentionRequest(home, now))
        assert lock.exists() and lock.stat().st_ino == inode
    cleanup_session_pair_rates(SessionPairRetentionRequest(home, now))
    assert not lock.exists()
