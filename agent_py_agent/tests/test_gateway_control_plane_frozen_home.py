"""C 冷组合冻结同次规范 home；普通 CLI 继续原环境解析合同。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space.home_root import configured_home_root
from agent_py_agent.cli import common, gateway_control_plane
from agent_py_agent.cli.workspace_resolution import runtime_paths_for_config


# LLM: 测试只创建显式tmp配置；参数是否省略home用于重现原环境选源，不读日常home或构造业务Agent。
# 函数用途: 构造空home配置，确保实际home来自本测试显式设置的环境。
def _args(tmp_path, *, empty_field=False):
    path = tmp_path / "minimal.yaml"
    path.write_text(('my_agent_home: ""\n' if empty_field else "")
                    + "model_backend: echo\nenable_plugins: false\nenable_subagents: false\n")
    return SimpleNamespace(config=str(path), workspace_root="")


@pytest.mark.parametrize("empty_field", [False, True])
def test_frozen_business_factory_does_not_reread_selected_home_environment(tmp_path, monkeypatch, empty_field):
    original = (tmp_path / "original-home").resolve()
    later = (tmp_path / "later-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(original))
    args = _args(tmp_path, empty_field=empty_field)
    config, roots = common.agent_configuration(args)
    assert config.my_agent_home == ""
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    called = []

    def record(config, root, *, workspace_roots):
        made = SimpleNamespace(config=config, home=Path(configured_home_root(config)).resolve(),
                               root=root, roots=workspace_roots)
        called.append(made)
        return made

    monkeypatch.setattr(common, "SimpleAgent", record)
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "frozen-home-review")
        control = gateway_control_plane.build_control_plane(args, paths, controller, common.make_agent)
        assert control.home_root == original and control.agent_root == roots[0]
        monkeypatch.setenv("MY_AGENT_HOME", str(later))
        made = control.start_business(control.agent_factory)
        assert len(called) == 1 and made is called[0]
        assert made.home == original and made.config.my_agent_home == str(original)
        assert made.root == roots[0] and made.roots == roots
        assert made.config is not control.config
    assert not later.exists()


def test_ordinary_cli_still_resolves_environment_at_its_original_call_boundary(tmp_path, monkeypatch):
    original = (tmp_path / "original-home").resolve()
    later = (tmp_path / "later-home").resolve()
    args = _args(tmp_path)
    monkeypatch.setenv("MY_AGENT_HOME", str(original))
    first, first_roots = common.agent_configuration(args)
    monkeypatch.setenv("MY_AGENT_HOME", str(later))
    second, second_roots = common.agent_configuration(args)
    assert first.my_agent_home == second.my_agent_home == ""
    assert first_roots[0].is_relative_to(original)
    assert second_roots[0].is_relative_to(later)
    assert not original.exists() and not later.exists()


@pytest.mark.parametrize("real_agent", [False, True])
def test_default_home_alias_is_frozen_for_original_constructor(tmp_path, monkeypatch, real_agent):
    # 默认别名也只能位于本测试假HOME中，绝不接触真实~/.my-agent。
    monkeypatch.setenv("HOME", str(tmp_path / "synthetic-user"))
    selected = (tmp_path / "synthetic-user" / ".my-agent").resolve()
    later = (tmp_path / "later-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(selected))
    args = _args(tmp_path)
    config, roots = common.agent_configuration(args)
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    if not real_agent:
        monkeypatch.setattr(common, "SimpleAgent", lambda config, root, *, workspace_roots:
                            SimpleNamespace(config=config, home_paths=SimpleNamespace(
                                root=Path(configured_home_root(config)).resolve())))
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "default-alias-review")
        control = gateway_control_plane.build_control_plane(args, paths, controller, common.make_agent)
        monkeypatch.setenv("MY_AGENT_HOME", str(later))
        made = control.start_business(control.agent_factory)
        assert made.home_paths.root == selected
        assert configured_home_root(made.config) == str(selected)
        from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
        from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
        scoped = _config_with_owner(made.config, OwnerIdentity.provider_user("local", "snapshot-user"))
        assert configured_home_root(scoped) == str(selected)
        assert configured_home_root(replace(scoped, model_name="synthetic-model")) == str(selected)
        with pytest.raises(ValueError, match="configured_home_snapshot_invalid"):
            configured_home_root(replace(scoped, my_agent_home=str(later)))
        assert control.agent_root == roots[0]
        if real_agent:
            assert made.home_paths.owner_home_dir.is_relative_to(selected)
            assert made.backend.__class__.__name__ == "EchoBackend"
    assert not later.exists()


def test_user_yaml_cannot_supply_internal_home_snapshot(tmp_path, monkeypatch):
    from dataclasses import fields

    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.settings.config_sources import public_config_keys
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry

    snapshot = next(item for item in fields(AgentConfig) if item.name == "host_home_snapshot")
    assert snapshot.metadata["host_only"] is True
    assert "host_home_snapshot" not in public_config_keys(AgentConfig)
    assert "host_home_snapshot" not in parameter_registry()
    selected = (tmp_path / "selected-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(selected))
    args = _args(tmp_path)
    with Path(args.config).open("a") as stream:
        stream.write('host_home_snapshot: {root: "foreign", configured_value: "foreign"}\n')
    config, roots = common.agent_configuration(args)
    assert config.host_home_snapshot is None
    assert configured_home_root(config) == str(selected)
    assert roots[0].is_relative_to(selected)
    assert any("host_home_snapshot" in warning for warning in config.config_warnings)
    assert not selected.exists()
