"""派工巡查内核文件锁测试。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.lock import (
    _DispatchWatchLock,
)


def test_lock_acquire_publishes_diagnostic_metadata(tmp_path: Path) -> None:
    lock_path = tmp_path / "dispatch.lock"

    with _DispatchWatchLock(lock_path) as lock:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

        assert lock.acquired is True
        assert payload["schema_version"] == "dispatch-watch-lock.v2"
        assert payload["token"] == lock.token
        assert payload["pid"] == os.getpid()


def test_release_keeps_stable_inode_and_allows_next_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "dispatch.lock"

    with _DispatchWatchLock(lock_path) as first:
        first_token = first.token

    assert lock_path.exists()
    with _DispatchWatchLock(lock_path) as second:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert second.token != first_token
    assert payload["token"] == second.token


def test_competing_owner_is_rejected_without_deleting_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "dispatch.lock"

    with _DispatchWatchLock(lock_path) as owner:
        with pytest.raises(RuntimeError, match="dispatch watch lock already held"):
            _DispatchWatchLock(lock_path).__enter__()
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["token"] == owner.token


def test_force_does_not_steal_live_kernel_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "dispatch.lock"

    with _DispatchWatchLock(lock_path):
        with pytest.raises(RuntimeError, match="不能抢占"):
            _DispatchWatchLock(lock_path, force=True).__enter__()


@pytest.mark.parametrize("contents", ["", "not valid json{{{"])
def test_empty_or_malformed_metadata_never_creates_permanent_deadlock(
    tmp_path: Path,
    contents: str,
) -> None:
    lock_path = tmp_path / "dispatch.lock"
    lock_path.write_text(contents, encoding="utf-8")

    with _DispatchWatchLock(lock_path) as lock:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert payload["token"] == lock.token


def test_metadata_corruption_does_not_control_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "dispatch.lock"

    with _DispatchWatchLock(lock_path):
        lock_path.write_text("broken", encoding="utf-8")

    with _DispatchWatchLock(lock_path) as replacement:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))

    assert payload["token"] == replacement.token


def test_exit_without_acquire_is_noop(tmp_path: Path) -> None:
    lock = _DispatchWatchLock(tmp_path / "dispatch.lock")

    lock.__exit__(None, None, None)

    assert lock.acquired is False
