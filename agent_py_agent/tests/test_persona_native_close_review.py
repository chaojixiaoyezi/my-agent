"""原C本地IO scope自然退出后的真实拒绝，原FD关闭次生OSError不得改写native。"""

import os

import pytest

from agent_py_agent.agent.capability import persona_storage_io as storage
from agent_py_agent.agent.common.global_write_commit import (
    GlobalWriteCommitUnavailable,
    global_write_commit_scope,
)
from agent_py_agent.tests.test_persona_coordinator_io import Door


@pytest.mark.parametrize("kind", ["parent", "versions"])
def test_original_parent_and_versions_close_preserve_real_expired_source(
    tmp_path, monkeypatch, kind
):
    original_close = os.close
    target = []
    closed = []
    primary = []
    failed = []
    original_open = storage._openat_file

    def opening(parent, name, flags, mode):
        fd = original_open(parent, name, flags, mode)
        if name.endswith(".lock"):
            target.append(fd)
        return fd

    monkeypatch.setattr(storage, "_openat_file", opening)

    def closing(fd):
        original_close(fd)
        closed.append(fd)
        if target and fd == target[0] and not failed:
            failed.append(True)
            raise OSError("secondary original FD close")

    monkeypatch.setattr(os, "close", closing)
    scope = global_write_commit_scope(Door())
    scope.__enter__()
    active = True
    context = (
        storage.parent_directory(tmp_path / "anchor", create=True, coordinator_io=True)
        if kind == "parent"
        else storage.versions_lock(tmp_path / "versions.jsonl", coordinator_io=True)
    )
    try:
        with pytest.raises(GlobalWriteCommitUnavailable) as caught:
            with context as value:
                if kind == "parent":
                    target.append(value)
                scope.__exit__(None, None, None)
                active = False
                try:
                    storage.persona_io(
                        lambda: pytest.fail("scope退出后不能写"), coordinator_io=True
                    )
                except GlobalWriteCommitUnavailable as error:
                    primary.append(error)
                    raise
        assert caught.value is primary[0]
    finally:
        if active:
            scope.__exit__(None, None, None)
    assert target[0] in closed
