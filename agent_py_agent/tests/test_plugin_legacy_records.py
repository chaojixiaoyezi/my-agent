"""原安装表持久授权与同源文字合同；不启动 OS 沙箱或真实通道。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.plugin_permissions.display import permission_lines, permission_summary
from agent_py_agent.agent.plugin_permissions.grants import permission_details, select_permissions
from agent_py_agent.agent.plugin_permissions.state import (
    canonical_permission_json,
    legacy_permissions,
)
from agent_py_agent.tests.test_plugin_activation import activation_fixture, publication, revocation
from agent_py_agent.tests.test_plugin_legacy_state import upgrade_source


def granted(tmp_path, mode="restricted"):
    store, entry, request = activation_fixture(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    details = permission_details(entry, request.activation.plan,
                                 select_permissions({"read_roots": [str(root)], "network": True}), mode)
    target = replace(request.activation, permission_json=canonical_permission_json(details))
    prepared = store.change_activation(replace(request, activation=target))
    active = store.change_activation(publication(prepared)).installation
    return store, active, details


def test_grant_persists_and_is_covered_by_activation_digest(tmp_path):
    store, active, details = granted(tmp_path)
    assert legacy_permissions(store.snapshot()[0]) == json.loads(canonical_permission_json(details))
    original = replace(active.activation, permission_json=None)
    assert original.content_sha256 != active.activation.content_sha256
    payload = active.activation.to_payload()
    assert PluginActivation.from_payload(payload) == active.activation
    assert "permission_grant" not in original.to_payload()


def test_same_activation_cannot_change_permissions_at_revoke(tmp_path):
    store, active, details = granted(tmp_path)
    details["permissions"]["network"] = False
    wrong = replace(active.activation, phase="revoked", permission_json=canonical_permission_json(details))
    with pytest.raises(PluginInstallationError, match="同一代次不能更换授权"):
        store.change_activation(PluginActivationRequest("disable-new", active.revision, wrong))
    assert store.snapshot()[0] == active
    assert legacy_permissions(store.change_activation(revocation(active)).installation) is None


@pytest.mark.parametrize("change", [lambda p: p.update(activation_id="b" * 64),
                                   lambda p: p.update(policy_version=True),
                                   lambda p: p["permissions"].update(network=1),
                                   lambda p: p["permissions"].update(desktop_ipc=True)])
def test_malformed_or_rebound_grant_rejected(tmp_path, change):
    _, active, details = granted(tmp_path)
    change(details)
    with pytest.raises(ValueError):
        replace(active.activation, permission_json=canonical_permission_json(details))


def test_summary_is_shared_honest_and_redacts_nonadmin(tmp_path):
    _, active, details = granted(tmp_path)
    admin = permission_summary(active, admin=True)
    ordinary = permission_summary(active, admin=False)
    path = details["permissions"]["read_roots"][0]["path"]
    assert path in admin and path not in ordinary
    assert "隔离未验证" in admin and "已隔离" not in admin
    assert "这个插件能连网，也能连本机端口" in permission_lines(details)
    assert permission_summary(active, admin=True, sandbox_default=False) == admin


def test_only_explicit_migration_shows_compatibility(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    store.change_activation(publication(store.change_activation(request)))
    assert "兼容中" not in permission_summary(store.snapshot()[0], admin=True)
    upgrade_source(store)
    assert "兼容中（重新启用后按新规则）" in permission_summary(store.snapshot()[0], admin=True)


def test_wide_preview_explicitly_warns_unbounded_permissions(tmp_path):
    _, active, details = granted(tmp_path, "wide")
    assert "不限制系统用户可读写范围" in permission_lines(details)
    assert "宽权限" in permission_summary(active, admin=True)
