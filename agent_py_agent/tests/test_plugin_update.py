"""/plugins update：同一插件的新版本包替换已停用安装，配置结构不变时保留私有配置；已启用、ID 不一致、同包、未安装分别拒绝或不变。"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_update import PluginUpdateRequest, plan_package_update
from agent_py_agent.tests.plugin_activation_fixtures import _SERVER, installed_runtime_plugin
from agent_py_agent.tests.plugin_wheel_fixtures import make_wheel
from agent_py_agent.tests.test_plugin_package import _manifest

_TOOLS_V2 = [{"name": "read", "description": "读取文件（v2）", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]


# 函数用途: 用同一插件 ID 构造 2.0 版包；settings_schema 可选覆盖以模拟配置结构变化。
def _v2_bundle(tmp_path, *, settings_schema=None, version="2.0"):
    code = _SERVER.replace("__TOOLS__", repr(_TOOLS_V2))
    wheel = make_wheel(version=version, files={"peek/__init__.py": b"", "peek/__main__.py": code.encode()})
    manifest = _manifest(wheel[1])
    manifest["version"] = version
    manifest["entry_wheel"] = f"wheels/peek-{version}-py3-none-any.whl"
    manifest["wheels"] = [{"path": manifest["entry_wheel"], "sha256": hashlib.sha256(wheel[1]).hexdigest()}]
    if settings_schema is not None:
        manifest["settings_schema"] = settings_schema
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        archive.writestr(manifest["entry_wheel"], wheel[1])
    path = tmp_path / f"peek-{version}.zip"
    path.write_bytes(data.getvalue())
    return path


def _entry(service):
    return service.installations.snapshot()[0]


def test_update_replaces_package_and_restores_compatible_settings(tmp_path):
    service = installed_runtime_plugin(tmp_path, configure=True)
    before = _entry(service)
    assert before.settings_json is not None and before.manifest.version == "1.0"
    v2 = _v2_bundle(tmp_path, settings_schema={"type": "object", "properties": {"note": {"type": "string"}},
                                              "required": ["note"], "additionalProperties": False})
    revision = service.catalog().revision
    result = service.command(f'/plugins update sample-peek "{v2}"', revision=revision, request_id="update")
    assert result["state"] == "succeeded", result
    details = result["details"]
    assert details["outcome"] == "updated" and details["settings_restored"] is True and details["settings_reason"] == "restored"
    assert details["previous_version"] == "1.0" and details["package_version"] == "2.0" and details["enabled"] is False
    assert "private-settings-value" not in json.dumps(result), "配置正文不进入结果"
    after = _entry(service)
    assert after.manifest.version == "2.0" and after.package_sha256 != before.package_sha256
    assert after.revision == before.revision + 2, "install + configure 两步回执"
    assert after.settings_json == before.settings_json and after.last_commit.action == "configure"
    assert after.activation is None and not after.enabled
    assert inspect_plugin_package(service.installations.package_bytes(after)).manifest.version == "2.0"
    replay = service.command(f'/plugins update sample-peek "{v2}"', revision=revision, request_id="update")
    assert replay["details"] == details, "同一请求编号按原目录版本复读原结果"
    listed = service.command("/plugins list", revision=service.catalog().revision, request_id="list")["message"]
    assert "sample-peek 2.0" in listed


def test_update_clears_incompatible_settings_and_reports_it(tmp_path):
    service = installed_runtime_plugin(tmp_path, configure=True)
    v2 = _v2_bundle(tmp_path, settings_schema={"type": "object", "properties": {"level": {"type": "integer"}},
                                              "required": ["level"], "additionalProperties": False})
    result = service.command(f'/plugins update sample-peek "{v2}"', revision=service.catalog().revision, request_id="update")
    assert result["state"] == "succeeded", result
    assert result["details"]["settings_restored"] is False and result["details"]["settings_reason"] == "incompatible"
    after = _entry(service)
    assert after.settings_json is None and after.settings_revision == 0 and after.last_commit.action == "install"
    assert after.manifest.version == "2.0"


def test_update_refuses_enabled_plugin_and_requires_matching_identity(tmp_path):
    service = installed_runtime_plugin(tmp_path, configure=True)
    enabled = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
    assert enabled["state"] == "succeeded", enabled
    v2 = _v2_bundle(tmp_path)
    try:
        refused = service.command(f'/plugins update sample-peek "{v2}"', revision=service.catalog().revision, request_id="update-enabled")
        assert refused["state"] == "failed" and refused["details"]["reason"] == "activation_unsettled", refused
        assert _entry(service).manifest.version == "1.0" and _entry(service).enabled
        wrong = service.command(f'/plugins update other-plugin "{v2}"', revision=service.catalog().revision, request_id="update-wrong")
        assert wrong["state"] == "failed" and wrong["details"]["reason"] == "plugin_missing", wrong
    finally:
        disabled = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        assert disabled["state"] == "succeeded", disabled
    same = service.command(f'/plugins update sample-peek "{tmp_path / "source.zip"}"', revision=service.catalog().revision, request_id="update-same")
    assert same["state"] == "succeeded" and same["details"]["outcome"] == "unchanged", same


def test_plan_rejects_identity_and_revision_conflicts_without_writing(tmp_path):
    service = installed_runtime_plugin(tmp_path)
    entry = _entry(service)
    package = inspect_plugin_package(_v2_bundle(tmp_path).read_bytes())
    operation = "host-command:" + "a" * 64
    for request, reason in (
        (PluginUpdateRequest("sample-peek", package, operation, entry.revision + 5), "revision_conflict"),
        (PluginUpdateRequest("another", package, operation, 0), "plugin_missing"),
    ):
        try:
            plan_package_update(request, (entry,))
        except PluginInstallationError as exc:
            assert exc.reason == reason and exc.commit_state == "not_committed"
        else:
            raise AssertionError(reason)
    plan = plan_package_update(PluginUpdateRequest("sample-peek", package, operation, entry.revision), (entry,))
    assert plan.commit is not None and plan.result.outcome == "updated" and plan.result.settings_reason == "none"
    assert plan.entries[0].revision == entry.revision + 1 and plan.entries[0].last_commit.action == "install"
