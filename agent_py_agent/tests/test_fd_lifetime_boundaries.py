"""缓存相位不算进程泄漏，真正新生未关闭socket/pipe仍须严格报错。"""

import errno
import gc
import os
import socket
from pathlib import Path

import pytest

from agent_py_agent.agent.user_space import owner_quota as quota
from agent_py_agent.tests.support.fd_lifetime import (
    assert_fd_unchanged,
    close_owner_quota_scan,
    fd_baseline,
)

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir() and not Path("/dev/fd").is_dir(),
    reason="系统未提供本测试使用的FD目录",
)


@pytest.mark.parametrize("kind", ["pipe", "socket"])
def test_real_unclosed_pipe_or_socket_is_still_a_zero_delta_failure(kind):
    before = fd_baseline()
    handles = os.pipe() if kind == "pipe" else socket.socketpair()
    try:
        with pytest.raises(AssertionError, match="added"):
            assert_fd_unchanged(before)
    finally:
        for handle in handles:
            os.close(handle) if kind == "pipe" else handle.close()
    assert_fd_unchanged(before)


def test_previous_case_cycle_is_collected_only_before_baseline(tmp_path, monkeypatch):
    class PreviousCase:
        def __init__(self):
            self.file = (tmp_path / "previous").open("wb")
            self.cycle = self

    enabled = gc.isenabled()
    gc.disable()
    try:
        previous = PreviousCase()
        descriptor = previous.file.fileno()
        del previous
        before = fd_baseline()
        with pytest.raises(OSError) as raised:
            os.fstat(descriptor)
        assert raised.value.errno == errno.EBADF
        monkeypatch.setattr(gc, "collect", lambda: pytest.fail("end must not hide a leak by GC"))
        assert_fd_unchanged(before)
    finally:
        if enabled:
            gc.enable()


def test_quota_cache_close_is_owner_scoped_and_preserves_checkpoint(tmp_path):
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
        for index in range(20):
            (root / f"item-{index}").write_text("quota")
        assert (
            quota.reconcile_owner_quota_slice(root, max_entries=1, max_ms=25)["complete"] is False
        )
    keys = [(os.getpid(), str(root.resolve())) for root in roots]
    saved = [(root / quota._QUOTA_LEDGER).read_bytes() for root in roots]
    try:
        assert all(key in quota._QUOTA_SCANS for key in keys)
        close_owner_quota_scan(roots[0])
        assert keys[0] not in quota._QUOTA_SCANS and keys[1] in quota._QUOTA_SCANS
        assert [(root / quota._QUOTA_LEDGER).read_bytes() for root in roots] == saved
        # 冷恢复仍沿同一个正式持久cursor推进，不能删头重建或把partial当complete。
        for _ in range(100):
            if quota.reconcile_owner_quota_slice(roots[0], max_entries=128, max_ms=25)["complete"]:
                break
        else:
            pytest.fail("saved quota checkpoint did not converge")
    finally:
        for root in roots:
            close_owner_quota_scan(root)


def test_old_handle_close_cannot_offset_a_new_socket_leak():
    outer = fd_baseline()
    old = os.pipe()
    current = ()
    try:
        before = fd_baseline()
        for descriptor in old:
            os.close(descriptor)
        old = ()
        current = socket.socketpair()
        # 全局数量仍相等；身份差分必须揭示新socket，不能被旧pipe关闭抵消。
        with pytest.raises(AssertionError, match="new_resources"):
            assert_fd_unchanged(before)
    finally:
        for descriptor in old:
            os.close(descriptor)
        for handle in current:
            handle.close()
    assert_fd_unchanged(outer)
