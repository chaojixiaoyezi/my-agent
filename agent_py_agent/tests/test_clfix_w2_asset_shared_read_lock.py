"""W2: read-only asset paths share the lock file; writers still exclude readers; lock identity is still checked."""
from __future__ import annotations

import threading

import pytest

from agent_py_agent.agent.assets.workspace_assets import _read_lock
from agent_py_agent.agent.common.json_io import locked_json_path


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".asset-batch.lock").write_text("")
    return tmp_path


def test_readers_hold_the_read_lock_together(root):
    barrier = threading.Barrier(2, timeout=5)
    errors: list[BaseException] = []

    def reader():
        try:
            with _read_lock(root, (".asset-batch.lock",)):
                barrier.wait()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []


def test_writer_excludes_reader_until_release(root):
    entered = threading.Event()
    with locked_json_path(root / ".asset-batch"):
        def reader():
            with _read_lock(root, (".asset-batch.lock",)):
                entered.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        assert not entered.wait(0.3)
    assert entered.wait(5)
    thread.join(timeout=5)


def test_lock_replaced_while_held_still_fails_closed(root):
    with pytest.raises(BlockingIOError):
        with _read_lock(root, (".asset-batch.lock",)):
            replacement = root / ".replacement-lock"
            replacement.write_text("")
            replacement.replace(root / ".asset-batch.lock")
