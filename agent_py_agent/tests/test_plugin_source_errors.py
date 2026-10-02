"""插件来源读取的结构化原因：相对路径按会话工作区根解析；不存在、越权、链接、格式无效分别回执，不再共用一句泛化文案。"""
from __future__ import annotations

import os

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_sources import (
    PluginSourceError,
    plugin_source_error_envelope,
    read_plugin_source,
    source_unauthorized_message,
)
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


def test_unauthorized_source_carries_only_the_current_owner_root_as_a_structured_field(tmp_path):
    workspace, other = tmp_path / "owner-a" / "ws", tmp_path / "owner-b"
    workspace.mkdir(parents=True)
    other.mkdir()
    (other / "pkg.zip").write_bytes(_bundle())
    scoped = PathAccessPolicy.from_values(mode="normal", owner_scope_root=str(tmp_path / "owner-a"))
    with pytest.raises(PluginSourceError) as denied:
        read_plugin_source(str(other / "pkg.zip"), workspace, scoped, max_bytes=1 << 20)
    assert plugin_source_error_envelope(denied.value, scoped) == {
        "reason": "source_unauthorized", "source_base": str(workspace), "allowed_root": str(scoped.owner_scope_root)}
    assert str(other) not in str(denied.value), "不回显越权路径，也不提别的 owner"
    assert str(denied.value) == source_unauthorized_message(plugin_source_error_envelope(denied.value, scoped)), "模型与用户看到同一句"
    with pytest.raises(PluginSourceError) as missing:
        read_plugin_source("nope.zip", workspace, scoped, max_bytes=1 << 20)
    assert "allowed_root" not in plugin_source_error_envelope(missing.value, scoped), "只有越权才给允许的根"
    full = PathAccessPolicy.from_values(mode="full")
    assert "allowed_root" not in plugin_source_error_envelope(denied.value, full), "没有 owner 根时不编造"


def test_unauthorized_message_without_an_owner_root_points_at_the_session_workspace():
    text = source_unauthorized_message({"source_base": "/ws"})
    assert "当前会话工作区（或显式授权的目录）" in text and "/ws" in text


def test_install_and_update_replies_tell_the_user_where_to_put_the_package(tmp_path):
    root = tmp_path / "owner-root"
    root.mkdir()
    service, _source = manager(tmp_path, workspace=root,
                               path_policy=PathAccessPolicy.from_values(mode="normal", owner_scope_root=str(root)))
    (root / "pkg.zip").write_bytes(_bundle())
    installed = service.command('/plugins install "pkg.zip"', revision=service.catalog().revision, request_id="inside")
    assert installed["state"] == "succeeded", installed
    outside = tmp_path / "elsewhere" / "pkg.zip"
    outside.parent.mkdir()
    outside.write_bytes(_bundle())
    for command in (f'/plugins install "{outside}"', f'/plugins update sample-peek "{outside}"'):
        result = service.command(command, revision=service.catalog().revision, request_id=command.split()[1])
        assert result["details"]["reason"] == "source_unauthorized", result
        assert result["details"]["allowed_root"] == str(root.resolve())
        assert result["message"].startswith(source_unauthorized_message(result["details"]))
        assert str(root.resolve()) in result["message"] and str(outside) not in result["message"]
        assert "读取未获授权。" not in result["message"], "不再是通用的参数错误说明"
