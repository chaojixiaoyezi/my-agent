"""权限状态的显式版本迁移与固定代次合同，不跑任何插件。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.plugin_permissions.state import legacy_permissions
from agent_py_agent.tests.test_plugin_activation import activation_fixture, publication, revocation


def upgrade_source(store):
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    payload["schema_version"] = "plugin_installations.v3"
    for row in payload["installations"]:
        row.pop("legacy_permission_grant", None)
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize("phase", ["inactive", "preparing", "active", "revoked"])
def test_only_old_enabled_activation_receives_compatibility(tmp_path, phase):
    store, _, request = activation_fixture(tmp_path)
    if phase != "inactive":
        prepared = store.change_activation(request)
        if phase in {"active", "revoked"}:
            store.change_activation(publication(prepared))
        if phase == "revoked":
            store.change_activation(revocation(store.snapshot()[0]))
    path = upgrade_source(store)
    before = path.read_bytes()
    entry = store.snapshot()[0]
    grant = legacy_permissions(entry)
    if phase == "active":
        assert grant is not None and grant.get("mode") == "legacy_compat"
        assert grant.get("activation_id") == entry.activation_id
    else:
        assert grant is None
    assert path.read_bytes() == before


def test_new_table_missing_field_never_gets_compatibility(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    store.change_activation(publication(store.change_activation(request)))
    path = store.root / "installations.json"
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == "plugin_installations.v4"
    assert legacy_permissions(store.snapshot()[0]) is None
    payload["installations"][0].pop("legacy_permission_grant")
    path.write_text(json.dumps(payload))
    with pytest.raises(PluginInstallationError):
        store.snapshot()


def test_revoke_and_release_end_the_compatibility_not_just_display(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    store.change_activation(publication(store.change_activation(request)))
    upgrade_source(store)
    old = store.snapshot()[0]
    assert legacy_permissions(old) is not None
    revoked = store.change_activation(revocation(old)).installation
    assert legacy_permissions(revoked) is None
    assert json.loads((store.root / "installations.json").read_text())["schema_version"] == "plugin_installations.v4"
    from agent_py_agent.agent.plugin_activation import prepare_release
    released = prepare_release("disable-a", revoked, (revoked,)).installation
    assert released.activation is None and legacy_permissions(released) is None


def test_migration_survives_new_install_commit_and_restart(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    store.change_activation(publication(store.change_activation(request)))
    upgrade_source(store)
    old = store.snapshot()[0]
    store.change_activation(revocation(old))
    assert store.snapshot()[0].activation.phase == "revoked"
    assert legacy_permissions(store.snapshot()[0]) is None
    saved = json.loads((store.root / "installations.json").read_text())
    assert saved["migration"]["from_schema"] == "plugin_installations.v3"


def test_permission_binding_cannot_move_to_another_activation(tmp_path):
    store, _, request = activation_fixture(tmp_path)
    store.change_activation(publication(store.change_activation(request)))
    upgrade_source(store)
    old = store.snapshot()[0]
    grant = legacy_permissions(old)
    assert grant is not None
    bad = {**grant, "activation_id": "b" * 64}
    with pytest.raises(ValueError):
        replace(old, legacy_permission_json=json.dumps(bad, sort_keys=True, separators=(",", ":")))
