"""3a 裁定的授权合同；只使用临时文件，不运行插件或 OS 沙箱。"""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_permissions.grants import (
    permission_details,
    permission_mode,
    select_permissions,
)
from agent_py_agent.agent.plugin_permissions.sandbox import policy_from_grant
from agent_py_agent.agent.plugin_runtime_facts import confirmation_code
from agent_py_agent.tests.test_plugin_activation import activation_fixture


def roots(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    program = tmp_path / "external-program"
    program.write_bytes(b"program-v1")
    return project, program


def test_selection_has_no_implicit_workspace_or_network(tmp_path):
    project, program = roots(tmp_path)
    selected = select_permissions({"read_roots": [str(project)], "program_roots": [str(program)], "network": True})
    assert [item.path for item in selected.read_roots] == [str(project)]
    assert selected.write_roots == () and selected.network is True
    assert len(selected.program_roots) == 1 and len(selected.program_roots[0].content_sha256) == 64
    assert select_permissions({}).network is False


@pytest.mark.parametrize("value", [None, [], {"network": "true"}, {"network": 1}, {"desktop_ipc": True}, {"read_roots": "x"}])
def test_invalid_or_unimplemented_permissions_are_rejected(value):
    with pytest.raises(ValueError):
        select_permissions(value)


@pytest.mark.parametrize("kind", ["read_roots", "write_roots", "program_roots"])
def test_cannot_grant_control_roots_or_their_ancestors(tmp_path, kind):
    control = tmp_path / "control"
    control.mkdir()
    for path in (control, tmp_path):
        with pytest.raises(ValueError):
            select_permissions({kind: [str(path)]}, (control,))


def test_path_alias_symlink_parent_escape_and_missing_root_rejected(tmp_path):
    project, _ = roots(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    for path in (str(alias), str(project / ".."), "~", ".", str(tmp_path / "missing"), "/"):
        with pytest.raises((ValueError, OSError)):
            select_permissions({"read_roots": [path]})


@pytest.mark.parametrize("default,previous,expected", [(True, "", "restricted"), (False, "", "wide"),
                                                       (False, "restricted", "restricted"),
                                                       (True, "legacy_compat", "restricted"),
                                                       (False, "legacy_compat", "wide")])
def test_default_only_controls_future_authorization(default, previous, expected):
    assert permission_mode(default, previous) == expected


def test_confirmation_binds_each_permission_and_plan_identity(tmp_path):
    _, entry, request = activation_fixture(tmp_path)
    project, program = roots(tmp_path)
    baseline = permission_details(entry, request.activation.plan, select_permissions({}), "restricted")
    assert baseline.get("activation_id") == PluginActivation(request.activation.plan, "preparing").activation_id
    assert baseline.get("package_sha256") == entry.package_sha256
    assert baseline.get("installation_ref") == entry.installation_ref
    assert baseline.get("policy_version") == 1
    selections = [{"read_roots": [str(project)]}, {"write_roots": [str(project)]}, {"network": True},
                  {"program_roots": [str(program)]}]
    for value in selections:
        detail = permission_details(entry, request.activation.plan, select_permissions(value), "restricted")
        assert confirmation_code(detail) != confirmation_code(baseline)
    for plan in (replace(request.activation.plan, operation_id="enable-b"),
                 replace(request.activation.plan, interpreter_fingerprint="b" * 64)):
        assert confirmation_code(permission_details(entry, plan, select_permissions({}), "restricted")) != confirmation_code(baseline)
    assert confirmation_code(permission_details(entry, request.activation.plan, select_permissions({}), "wide")) != confirmation_code(baseline)


def test_program_content_change_invalidates_confirmation(tmp_path):
    _, entry, request = activation_fixture(tmp_path)
    _, program = roots(tmp_path)
    value = {"program_roots": [str(program)]}
    before = permission_details(entry, request.activation.plan, select_permissions(value), "restricted")
    program.write_bytes(b"program-v2")
    after = permission_details(entry, request.activation.plan, select_permissions(value), "restricted")
    assert confirmation_code(before) != confirmation_code(after)


def test_restricted_policy_projects_roots_network_and_hidden_root(tmp_path):
    _, entry, request = activation_fixture(tmp_path)
    project, program = roots(tmp_path)
    details = permission_details(entry, request.activation.plan,
                                 select_permissions({"read_roots": [str(project)], "program_roots": [str(program)], "network": True}),
                                 "restricted")
    policy = policy_from_grant(details)
    assert policy.network is True and policy.execute_roots == (program,)
    assert policy.read_roots == (project,) and policy.write_roots == ()
    assert policy.hidden_read_root == Path.home()
