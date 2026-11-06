"""Linux 默认权限自愈与文件发现优化：真实临时目录、拒绝反例及系统调用预算。"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import recovery_pending_index as recovery
from agent_py_agent.agent.tooling import _filesystem_find as find
from agent_py_agent.agent.tooling import _filesystem_helpers as helpers
from agent_py_agent.agent.tooling._filesystem_read import (
    FileSystemAccessOptions,
    FileSystemTool,
)


def test_projection_tightens_agent_home_under_umask_0002(tmp_path):
    home = tmp_path / ".my-agent"
    previous = os.umask(0o002)
    try:
        data = home / "data"
        data.mkdir(parents=True)
        home.chmod(0o775)
        data.chmod(0o775)
        path = data / "gateway" / "projection_state" / "pending.sqlite3"
        with recovery.private_projection_connection(path) as db:
            db.execute("CREATE TABLE sample (value INTEGER)")
            db.execute("INSERT INTO sample VALUES (42)")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with recovery.private_projection_connection(path) as db:
        assert db.execute("SELECT value FROM sample").fetchall() == [(42,)]


def test_projection_accepts_private_group_ancestor_without_chmod(tmp_path):
    ancestor = tmp_path / "private-group-home"
    ancestor.mkdir()
    os.chown(ancestor, -1, os.getegid())
    ancestor.chmod(0o775)
    assert ancestor.stat().st_gid == os.getegid()
    path = ancestor / "data" / "projection_state" / "pending.sqlite3"
    with recovery.private_projection_connection(path) as db:
        assert db.execute("SELECT 1").fetchone() == (1,)
    assert stat.S_IMODE(ancestor.stat().st_mode) == 0o775


def test_projection_verify_reapplies_same_agent_home_policy(tmp_path):
    ancestor = tmp_path / ".my-agent"
    path = ancestor / "data" / "projection_state" / "pending.sqlite3"
    with recovery._projection_directory(path) as (_, verify):
        ancestor.chmod(0o775)
        verify()
        assert stat.S_IMODE(ancestor.stat().st_mode) == 0o700


@pytest.mark.parametrize("inside_agent_home", [False, True])
def test_projection_rejects_world_write_without_chmod(tmp_path, inside_agent_home):
    ancestor = tmp_path / (".my-agent" if inside_agent_home else "shared")
    ancestor.mkdir()
    ancestor.chmod(0o777)
    path = ancestor / "data" / "pending.sqlite3"
    with pytest.raises(PermissionError, match="not a trusted directory"):
        with recovery.private_projection_connection(path):
            pytest.fail("world-writable ancestor admitted")
    assert stat.S_IMODE(ancestor.stat().st_mode) == 0o777
    assert not path.exists()


@pytest.mark.parametrize("changed_field", ["uid", "gid"])
def test_projection_rejects_foreign_owner_or_group(tmp_path, monkeypatch, changed_field):
    ancestor = tmp_path / "shared-group"
    ancestor.mkdir()
    ancestor.chmod(0o775)
    expected = ancestor.stat()
    original = recovery.os.fstat

    def fstat(descriptor):
        info = original(descriptor)
        if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
            fields = list(info)
            fields[4 if changed_field == "uid" else 5] = max(os.geteuid(), os.getegid()) + 1001
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(recovery.os, "fstat", fstat)
    path = ancestor / "data" / "pending.sqlite3"
    with pytest.raises(PermissionError, match="not a trusted directory"):
        with recovery.private_projection_connection(path):
            pytest.fail("foreign ownership admitted")
    assert stat.S_IMODE(ancestor.stat().st_mode) == 0o775
    assert not path.exists()


def test_projection_verify_rejects_world_write_drift(tmp_path):
    ancestor = tmp_path / ".my-agent"
    path = ancestor / "data" / "projection_state" / "pending.sqlite3"
    with recovery._projection_directory(path) as (_, verify):
        ancestor.chmod(0o777)
        with pytest.raises((PermissionError, ValueError)):
            verify()
    assert stat.S_IMODE(ancestor.stat().st_mode) == 0o777


def test_projection_rejects_symlink_parent_without_chmod_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o775)
    (tmp_path / ".my-agent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        with recovery.private_projection_connection(tmp_path / ".my-agent" / "pending.sqlite3"):
            pytest.fail("symlink ancestor admitted")
    assert stat.S_IMODE(outside.stat().st_mode) == 0o775
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("mode", [0o1770, 0o1777])
def test_projection_preserves_explicit_sticky_ancestor_exception(tmp_path, mode):
    ancestor = tmp_path / ".my-agent"
    ancestor.mkdir()
    ancestor.chmod(mode)
    path = ancestor / "data" / "projection_state" / "pending.sqlite3"
    with recovery._projection_directory(path) as (_, verify):
        assert stat.S_IMODE(ancestor.stat().st_mode) == mode
        verify()
        assert stat.S_IMODE(ancestor.stat().st_mode) == mode
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def _runtime_fixture(tmp_path):
    owner = tmp_path / "owner"
    runtime = owner / "workspace" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "snapshot.py").write_text("runtime")
    (owner / "visible.py").write_text("visible")
    (owner / "workspace" / "project.py").write_text("project")
    return owner, runtime


@pytest.mark.parametrize("backend", ["python", "native", "snapshot"])
def test_find_prunes_runtime_before_descent(tmp_path, monkeypatch, backend):
    owner, runtime = _runtime_fixture(tmp_path)
    tool = find.FindFilesTool(owner, 100, access_options=FileSystemAccessOptions(owner_scope_root=str(owner)))
    original = helpers.os.scandir

    def scandir(path):
        assert Path(path) != runtime, "runtime must be pruned before scandir"
        return original(path)

    monkeypatch.setattr(helpers.os, "scandir", scandir)
    if backend == "python":
        monkeypatch.setattr(find, "_find_directory_matches_with_rg", lambda *_: None)
    params = {"pattern": "*.py", "include_ignored": False, "snapshot": backend == "snapshot"}
    result = tool.execute(params)
    assert result.ok
    assert "visible.py" in result.output and "workspace/project.py" in result.output
    assert "snapshot.py" not in result.output
    assert result.result_envelope["page_window"]["complete"]


@pytest.mark.parametrize("explicit_root", [False, True])
def test_find_runtime_can_be_requested_explicitly(tmp_path, explicit_root):
    owner, runtime = _runtime_fixture(tmp_path)
    tool = find.FindFilesTool(owner, 100, access_options=FileSystemAccessOptions(owner_scope_root=str(owner)))
    params = {"pattern": "*.py", "include_ignored": not explicit_root}
    if explicit_root:
        params["path"] = str(runtime)
    result = tool.execute(params)
    assert result.ok and "snapshot.py" in result.output
    assert result.result_envelope["page_window"]["complete"]


def _discovery_stream(root, tool=None, observe=None):
    state = find._FindScanState(started_at=time.monotonic())
    stream = helpers._iter_discovery_files(
        tool or FileSystemTool(root), root, include_ignored=False, ignored_names=(".git",),
        scan_state=state, admit=find._admit_find_candidate, observe=observe,
    )
    return stream, state


def test_discovery_nofollow_work_is_once_per_dequeued_path(tmp_path, monkeypatch):
    root = tmp_path / "one" / "two" / "three"
    root.mkdir(parents=True)
    expected = []
    for number in range(80):
        path = root / f"file-{number:03d}.txt"
        path.write_text("sample")
        expected.append(path)
    original = helpers._discovery_nofollow_stat
    calls = []

    def observed(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(helpers, "_discovery_nofollow_stat", observed)
    stream, state = _discovery_stream(root)
    assert list(stream) == expected
    assert not state.limited and state.candidates == len(expected)
    assert len(calls) <= len(expected) + 1, f"redundant full ancestor walks: {len(calls)}"


def test_discovery_rechecks_queued_file_symlink(tmp_path):
    (tmp_path / "a.txt").write_text("first")
    later = tmp_path / "z.txt"
    later.write_text("original")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside")
    stream, state = _discovery_stream(tmp_path)
    assert next(stream).name == "a.txt"
    later.unlink()
    later.symlink_to(outside)
    assert list(stream) == []
    assert not state.limited


def test_discovery_rechecks_queued_directory_symlink(tmp_path):
    (tmp_path / "a.txt").write_text("first")
    later = tmp_path / "zdir"
    later.mkdir()
    (later / "hidden.txt").write_text("must not be traversed")
    stream, state = _discovery_stream(tmp_path)
    assert next(stream).name == "a.txt"
    moved = tmp_path / "moved"
    later.rename(moved)
    later.symlink_to(moved, target_is_directory=True)
    assert list(stream) == []
    assert not state.limited


def test_discovery_rechecks_queued_file_policy(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("first")
    later = tmp_path / "z.txt"
    later.write_text("revoked")
    tool = FileSystemTool(tmp_path)
    stream, state = _discovery_stream(tmp_path, tool=tool)
    assert next(stream).name == "a.txt"
    original = tool.resolve_path

    def resolve(path):
        if path == later:
            raise ValueError("revoked")
        return original(path)

    monkeypatch.setattr(tool, "resolve_path", resolve)
    assert list(stream) == []
    assert not state.limited
