"""W11：只在临时布局验证启用交集及平台规格，不把规则投影当作真实隔离。"""
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.plugin_permissions.paths import host_exclusive_paths
from agent_py_agent.agent.plugin_permissions.state import legacy_permissions
from agent_py_agent.agent.plugin_sandbox import (
    PluginRestrictedSandbox,
    PluginV8Sandbox,
    plugin_sandbox_spec,
    sandboxed_plugin_argv,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_plugin_legacy_management import (
    confirm,
    fake_processes,
    fake_sandbox_ready,
    installed_manager,
    preview,
)
from agent_py_agent.tests.test_plugin_management import manager


def _layout(tmp_path, monkeypatch, *, ordinary=False):
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    root = tmp_path / "canonical-data"
    identity = OwnerIdentity.local_user("member") if ordinary else OwnerIdentity.local_main()
    owner = resolve_owner_home(root, identity)
    cwd = owner.plugins_dir / "environments" / "environment" / "files"
    data = owner.plugins_dir / "data" / "example"
    business = owner.tasks_dir / "business"
    for path in (cwd, data, business, root / "config" / "model-profiles", root / "secrets"):
        path.mkdir(parents=True, exist_ok=True)
    (root / "config" / "agent_config.yaml").write_text("api_key: fixture-not-a-secret\n")
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    return os_home, root, owner, cwd, data, business


@pytest.mark.parametrize("kind", ["v8", "legacy"])
def test_split_home_spec_hides_trusted_canonical_root(tmp_path, monkeypatch, kind):
    os_home, root, owner, cwd, data, business = _layout(tmp_path, monkeypatch)
    policy = (PluginV8Sandbox(False, os_home) if kind == "v8" else
              PluginRestrictedSandbox((business,), (), (), False, os_home))
    spec = plugin_sandbox_spec(cwd=cwd, data_dir=data, owner_home=owner.home_dir, sandbox_policy=policy)
    assert root in spec.private_read_roots
    assert os_home in spec.private_read_roots
    assert data in spec.extra_write_roots


@pytest.mark.parametrize("ordinary", [False, True])
@pytest.mark.parametrize("restricted", [False, True])
def test_host_sources_hidden_even_in_broad_business_view(tmp_path, monkeypatch, ordinary, restricted):
    os_home, root, owner, cwd, data, business = _layout(tmp_path, monkeypatch, ordinary=ordinary)
    policy = PluginRestrictedSandbox((business,), (business,), (), False, os_home) if restricted else None
    spec = plugin_sandbox_spec(cwd=cwd, data_dir=data, owner_home=owner.home_dir, sandbox_policy=policy)
    for source in (root / "config" / "agent_config.yaml", root / "config" / "model-profiles", root / "secrets"):
        assert any(source == hidden or hidden in source.parents for hidden in spec.hidden_paths)
    assert not any(business == hidden or hidden in business.parents for hidden in spec.hidden_paths)
    assert not any(cwd == hidden or hidden in cwd.parents for hidden in spec.hidden_paths)


@pytest.mark.parametrize("kind", ["read_roots", "write_roots", "program_roots"])
def test_enable_confirmation_and_persistent_grant_use_intersection(tmp_path, monkeypatch, kind):
    service = installed_manager(tmp_path)
    fake_sandbox_ready(monkeypatch)
    fake_processes(monkeypatch)
    parent = tmp_path / "projects"
    allowed = parent / "allowed"
    denied = parent / "sibling"
    allowed.mkdir(parents=True)
    denied.mkdir()
    policy = PathAccessPolicy.from_values(mode="full", owner_scope_root=allowed,
                                         agent_home_root=service.context.owner.root)
    service = PluginManagement(replace(service.context, workspace=allowed, path_policy=policy))
    flag = {"read_roots": "read-root", "write_roots": "write-root", "program_roots": "program-root"}[kind]
    first = preview(service, f'--{flag} "{parent}" --{flag} "{denied}"')
    permissions = first["details"]["confirmation"]["permissions"]
    assert [row["path"] for row in permissions[kind]] == [str(allowed)]
    result = confirm(service, first)
    assert result["state"] == "succeeded", result
    grant = legacy_permissions(service.installations.snapshot()[0])
    assert [row["path"] for row in grant["permissions"][kind]] == [str(allowed)]


@pytest.mark.parametrize("admin_role", [False, True])
def test_ordinary_owner_admin_role_cannot_remove_or_widen_scope(tmp_path, admin_role):
    service, _ = manager(tmp_path)
    owner = resolve_owner_home(tmp_path / "data", OwnerIdentity.local_user("member"))
    narrow = owner.tasks_dir / "one-task"
    policy = PathAccessPolicy.from_values(mode="full", owner_scope_root=narrow, agent_home_root=owner.root)
    normalized = PluginManagement(replace(service.context, owner=owner, is_admin=admin_role,
                                         path_policy=policy, workspace=narrow)).context
    assert normalized.path_policy.owner_scope_root == narrow
    assert normalized.process_sandbox is True
    assert normalized.legacy_sandbox_default is True


def test_host_source_projection_uses_schema_and_retains_resolved_reference(tmp_path):
    home = home_paths(tmp_path / "data")
    current = tmp_path / "current-secret"
    original = tmp_path / "resolved-secret"
    config = AgentConfig(feishu_app_secret=f"file:{current}", config_path=str(tmp_path / "agent_config.yaml"))
    config.config_sources["feishu_app_secret"] = {"source": f"file:{original}"}
    paths = host_exclusive_paths(home, config)
    assert current in paths and original in paths
    assert Path(config.config_path) in paths


def test_linux_hidden_sources_overlay_broad_read_and_write_mounts(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_sandbox

    os_home, _root, owner, cwd, data, _business = _layout(tmp_path, monkeypatch)
    private = owner.home_dir / "config"
    private.mkdir()
    (private / "agent_config.yaml").write_text("fixture-only")
    policy = PluginRestrictedSandbox((owner.home_dir,), (owner.home_dir,), (), False, os_home)
    spec = plugin_sandbox_spec(cwd=cwd, data_dir=data, owner_home=owner.home_dir, sandbox_policy=policy)
    spec = replace(spec, bwrap_path="/usr/bin/bwrap", deny_gateway_ports=())
    monkeypatch.setattr(plugin_sandbox.platform, "system", lambda: "Linux")
    fake_sandbox_ready(monkeypatch)
    argv = sandboxed_plugin_argv(["/usr/bin/true"], spec)
    hidden_positions = [index for index in range(len(argv) - 1) if argv[index:index + 2] == ["--tmpfs", str(private)]]
    write_positions = [index for index in range(len(argv) - 2) if argv[index:index + 3] == ["--bind", str(owner.home_dir), str(owner.home_dir)]]
    assert hidden_positions and write_positions
    assert max(hidden_positions) > max(write_positions)


def test_darwin_hidden_sources_also_deny_execution(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_sandbox

    os_home, root, owner, cwd, data, business = _layout(tmp_path, monkeypatch)
    policy = PluginRestrictedSandbox((business,), (), (), False, os_home)
    spec = plugin_sandbox_spec(cwd=cwd, data_dir=data, owner_home=owner.home_dir, sandbox_policy=policy)
    spec = replace(spec, macos_sandbox_exec="/usr/bin/sandbox-exec", deny_gateway_ports=())
    monkeypatch.setattr(plugin_sandbox.platform, "system", lambda: "Darwin")
    fake_sandbox_ready(monkeypatch)
    argv = sandboxed_plugin_argv(["/usr/bin/true"], spec)
    profile = argv[argv.index("-p") + 1]
    assert f'(deny process-exec (subpath "{root / "config"}"))' in profile


def test_disjoint_ordinary_scope_is_rejected_instead_of_replaced(tmp_path):
    service, _ = manager(tmp_path)
    owner = resolve_owner_home(tmp_path / "data", OwnerIdentity.local_user("member"))
    policy = PathAccessPolicy.from_values(mode="full", owner_scope_root=tmp_path / "unrelated", agent_home_root=owner.root)
    with pytest.raises(ValueError, match="范围"):
        PluginManagement(replace(service.context, owner=owner, path_policy=policy))


def test_admin_projection_hides_other_owners_host_configuration(tmp_path, monkeypatch):
    os_home, root, owner, cwd, data, business = _layout(tmp_path, monkeypatch)
    other = resolve_owner_home(root, OwnerIdentity.local_user("other"))
    other_config = other.home_dir / "config"
    other_config.mkdir(parents=True)
    (other_config / "agent_config.yaml").write_text("fixture-only")
    policy = PluginRestrictedSandbox((business,), (), (), False, os_home)
    spec = plugin_sandbox_spec(cwd=cwd, data_dir=data, owner_home=owner.home_dir, sandbox_policy=policy)
    assert other_config in spec.hidden_paths
