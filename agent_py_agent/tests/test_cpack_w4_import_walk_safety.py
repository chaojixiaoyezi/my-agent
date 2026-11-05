"""W4 来源树安全与预算；Windows 无 FD 时必须明确拒绝而非路径降级。"""
from __future__ import annotations

import os
import socket
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability import external_plugin_import as imp
from agent_py_agent.agent.plugin_package import PackageReadLimits
from agent_py_agent.tests.test_cpack_w4_import_synthesis import put, requires_fd


def refused(root, code, *, limits=None):
    with pytest.raises(imp.ExternalImportError) as error:
        imp.import_claude_plugin(imp.ExternalImportRequest(root, limits=limits or PackageReadLimits()))
    assert error.value.code == code
    return error.value


def test_platform_without_fd_is_an_executed_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_fd", set())
    assert refused(tmp_path, "PLUGIN_IMPORT_SOURCE_INVALID").detail == "platform_unsupported"


@requires_fd
@pytest.mark.parametrize("name", ["a:b", "a\\b", "line\nbreak", "bad\x7f"])
def test_control_or_separator_name_refuses_whole_tree(tmp_path, name):
    put(tmp_path, "CAPABILITY.md", "# 正文\n")
    put(tmp_path, name, "data")
    refused(tmp_path, "PLUGIN_IMPORT_PATH_UNSAFE")


@requires_fd
@pytest.mark.parametrize("kind", ["file_link", "dir_link", "fifo", "socket"])
def test_special_types_refuse_even_before_hidden_filter(tmp_path, kind):
    put(tmp_path, "CAPABILITY.md", "# 方法\n")
    target = tmp_path / ".hidden-unsafe"
    channel = None
    if kind == "file_link":
        target.symlink_to(tmp_path / "CAPABILITY.md")
    elif kind == "dir_link":
        target.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        channel = socket.socket(socket.AF_UNIX)
        channel.bind(str(target))
    try:
        refused(tmp_path, "PLUGIN_IMPORT_PATH_UNSAFE")
    finally:
        if channel is not None:
            channel.close()


@requires_fd
def test_root_link_and_parent_escape_refuse(tmp_path):
    root = tmp_path / "source"
    put(root, "CAPABILITY.md", "# 方法\n")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    refused(alias, "PLUGIN_IMPORT_PATH_UNSAFE")
    refused(root / ".." / "source", "PLUGIN_IMPORT_PATH_UNSAFE")


@requires_fd
def test_regular_file_is_invalid_source_not_an_unsafe_tree(tmp_path):
    source = put(tmp_path, "plain-source", "data")
    refused(source, "PLUGIN_IMPORT_SOURCE_INVALID")


@requires_fd
def test_member_budget_stops_before_reading_over_limit(tmp_path, monkeypatch):
    for name in ("a", "b", "c", "d"):
        put(tmp_path, name, "data")
    reads = []
    real = imp.read_bytes_beneath

    def read(root, parts, **kwargs):
        reads.append("/".join(parts))
        return real(root, parts, **kwargs)

    monkeypatch.setattr(imp, "read_bytes_beneath", read)
    refused(tmp_path, "PLUGIN_IMPORT_LIMIT_EXCEEDED", limits=replace(PackageReadLimits(), members=2))
    assert reads == ["a", "b"]


@requires_fd
@pytest.mark.parametrize("limit", ["member_bytes", "archive_bytes", "expanded_bytes"])
def test_byte_budgets_count_unimported_files_too(tmp_path, limit):
    put(tmp_path, "hooks/hooks.json", "x" * 20)
    refused(tmp_path, "PLUGIN_IMPORT_LIMIT_EXCEEDED", limits=replace(PackageReadLimits(), **{limit: 10}))


@requires_fd
def test_deep_tree_and_long_path_are_bounded(tmp_path):
    deep = "/".join(["d"] * 33)
    put(tmp_path, deep + "/x", "x")
    refused(tmp_path, "PLUGIN_IMPORT_LIMIT_EXCEEDED")


@requires_fd
def test_walk_reads_each_regular_file_once_and_never_reads_hidden(tmp_path, monkeypatch):
    put(tmp_path, "CAPABILITY.md", "# 方法\n")
    put(tmp_path, "sub/a", "a")
    put(tmp_path, ".cache/hidden", "hidden")
    reads, listings = [], []
    real_read, real_list = imp.read_bytes_beneath, os.listdir

    def read(root, parts, **kwargs):
        reads.append("/".join(parts))
        return real_read(root, parts, **kwargs)

    def listing(fd):
        assert isinstance(fd, int)
        st = os.fstat(fd)
        listings.append((st.st_dev, st.st_ino))
        return real_list(fd)

    monkeypatch.setattr(imp, "read_bytes_beneath", read)
    monkeypatch.setattr(os, "listdir", listing)
    monkeypatch.setattr(os, "supports_fd", os.supports_fd | {listing})
    imp.import_claude_plugin(imp.ExternalImportRequest(tmp_path))
    assert reads == ["CAPABILITY.md", "sub/a"]
    assert len(listings) == len(set(listings)) == 2
