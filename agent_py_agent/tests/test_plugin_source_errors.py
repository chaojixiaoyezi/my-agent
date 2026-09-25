"""插件来源读取的结构化原因：相对路径按会话工作区根解析；不存在、越权、链接、格式无效分别回执，不再共用一句泛化文案。"""
from __future__ import annotations

import os

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_sources import PluginSourceError, read_plugin_source
from agent_py_agent.tests.test_plugin_management import manager
from agent_py_agent.tests.test_plugin_package import _bundle


def test_read_plugin_source_reports_structured_reasons(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "pkg.zip").write_bytes(_bundle())
    policy = PathAccessPolicy.from_values(mode="full")
    assert read_plugin_source("pkg.zip", workspace, policy, max_bytes=1 << 20) == _bundle(), "相对路径按工作区根解析"
    with pytest.raises(PluginSourceError) as missing:
        read_plugin_source("nope/pkg.zip", workspace, policy, max_bytes=1 << 20)
    assert missing.value.reason == "not_found" and missing.value.base == workspace
    assert "nope/pkg.zip" in str(missing.value) and str(workspace) in str(missing.value), "文案点明按哪个目录解析"
    link = tmp_path / "link.zip"
    os.symlink(workspace / "pkg.zip", link)
    with pytest.raises(PluginSourceError) as linked:
        read_plugin_source(str(link), workspace, policy, max_bytes=1 << 20)
    assert linked.value.reason == "symlink"
    outside = tmp_path / "outside.zip"
    outside.write_bytes(_bundle())
    scoped = PathAccessPolicy.from_values(mode="normal", owner_scope_root=str(workspace))
    with pytest.raises(PluginSourceError) as denied:
        read_plugin_source(str(outside), workspace, scoped, max_bytes=1 << 20)
    assert denied.value.reason == "unauthorized" and str(outside) not in str(denied.value), "越权文案不回显路径"


def test_install_command_distinguishes_missing_unauthorized_and_invalid_sources(tmp_path):
    service, source = manager(tmp_path)
    revision = service.catalog().revision
    missing = service.command('/plugins install "missing-dir/pkg.zip"', revision=revision, request_id="missing")
    assert missing["state"] == "failed" and missing["details"]["reason"] == "source_not_found", missing
    assert missing["details"]["source_base"] == str(service.context.workspace)
    assert "missing-dir/pkg.zip" in missing["output"] and str(service.context.workspace) in missing["output"]
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    invalid = service.command(f'/plugins install "{bad}"', revision=revision, request_id="invalid")
    assert invalid["state"] == "failed" and invalid["details"]["reason"].startswith("package_"), invalid
    assert "格式无效" in invalid["output"] and "不存在" not in invalid["output"]
    relative = service.command(f'/plugins install "{source.name}"', revision=revision, request_id="relative")
    assert relative["state"] == "succeeded", relative
    assert relative["details"]["plugin_id"] == "sample-peek", "同一文件用相对路径按会话工作区解析后安装成功"
