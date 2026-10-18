"""C 与原 CLI 共用实际配置/工作区解析；不启动 C/W 进程或模型。"""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.configuration import AuthConfigurationError
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import common
from agent_py_agent.cli.workspace_resolution import runtime_paths_for_config


@pytest.fixture
def case(tmp_path, monkeypatch):
    home = (tmp_path / "explicit-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    assert home != (Path.home() / ".my-agent").resolve()
    config_path = tmp_path / "synthetic.yaml"
    config_path.write_text(f'my_agent_home: "{home}"\nmodel_backend: echo\nenable_plugins: false\nenable_subagents: false\n')
    args = SimpleNamespace(config=str(config_path), workspace_root="")
    return home, args


def _module():
    from agent_py_agent.cli import gateway_control_plane
    return gateway_control_plane


# LLM: 此factory只记录原CLI实际交给SimpleAgent的构造参数，不充当Agent或运行任何业务。
# 函数用途: 比较C冷准备与普通CLI共同使用的配置和constructor root。
def _record_agent(monkeypatch):
    calls = []
    def factory(config, root, *, workspace_roots):
        result = SimpleNamespace(config=config, root=root, workspace_roots=workspace_roots)
        calls.append(result)
        return result
    monkeypatch.setattr(common, "SimpleAgent", factory)
    return calls


def test_common_configuration_and_make_agent_use_identical_owner_roots(case, monkeypatch):
    home, args = case
    config, roots = common.agent_configuration(args)
    calls = _record_agent(monkeypatch)
    agent = common.make_agent(args)
    assert config.my_agent_home == agent.config.my_agent_home == str(home)
    assert roots == agent.workspace_roots and roots[0] == agent.root
    assert roots[0].is_relative_to(home)
    assert len(calls) == 1 and not home.exists()


def test_control_plane_does_not_construct_agent_or_create_owner_home(case, monkeypatch):
    home, args = case
    config, roots = common.agent_configuration(args)
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    calls = _record_agent(monkeypatch)
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "construction-c")
        # 原lease可创建控制锁的父目录；冷组合本身不能播种任何owner业务文件。
        before = {path.relative_to(home): path.read_bytes()
                  for path in home.rglob("*") if path.is_file()}
        control = _module().build_control_plane(args, paths, controller, common.make_agent)
        assert control.config.my_agent_home == str(home)
        assert control.agent_root == roots[0]
        assert control.home_root == home
        assert control.controller() is controller
        after = {path.relative_to(home): path.read_bytes()
                 for path in home.rglob("*") if path.is_file()}
        assert not calls and after == before


def test_business_factory_keeps_selected_config_and_constructor_roots(case, monkeypatch):
    home, args = case
    config, roots = common.agent_configuration(args)
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    calls = _record_agent(monkeypatch)
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "frozen-c")
        control = _module().build_control_plane(args, paths, controller, common.make_agent)
        original = copy.deepcopy(control.config)
        Path(args.config).write_text(f'my_agent_home: "{home / "foreign"}"\nenable_subagents: true\n')
        args.workspace_root = str(home / "foreign-workspace")
        monkeypatch.setattr(common, "load_config", lambda *_: pytest.fail("frozen C factory reloaded configuration"))
        made = control.start_business(control.agent_factory)
        assert len(calls) == 1 and made is calls[0]
        assert made.root == roots[0] and made.workspace_roots == roots
        assert made.config.my_agent_home == original.my_agent_home
        assert made.config.enable_subagents is False
        assert made.config is not control.config
        assert not (home / "foreign").exists()


def test_control_plane_rejects_changed_gateway_root_before_business(case, monkeypatch):
    home, args = case
    common.agent_configuration(args)
    paths = gateway_paths_from_root(home.parent / "different-gateway")
    calls = _record_agent(monkeypatch)
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "wrong-root-c")
        with pytest.raises(ValueError, match="gateway_root_changed"):
            _module().build_control_plane(args, paths, controller, common.make_agent)
        assert not calls and not (home / "owners").exists()


def test_control_credential_preflight_preserves_original_auth_configuration_gate(case):
    _home, args = case
    config, _ = common.agent_configuration(args)
    config.auth_enabled = False
    config.gateway_per_user_owner_scoping = True
    reached = []
    control = SimpleNamespace(config=config, prepare_local_credential=lambda: reached.append(True))
    with pytest.raises(AuthConfigurationError) as error:
        _module().preflight_control_credential(control)
    assert error.value.reason_code == "multiowner_without_auth" and reached == []


def test_control_composition_binds_original_metadata_and_rejection_producers(case):
    _home, args = case
    config, _ = common.agent_configuration(args)
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    from agent_py_agent.agent.common.canonical_source_contract import (
        canonical_child_source_contract,
    )
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )
    from agent_py_agent.agent.subagents import canonical_source
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "metadata-c")
        _module().build_control_plane(args, paths, controller, common.make_agent)
        contract = canonical_child_source_contract()
        assert contract.validate is canonical_source.validate_orphan_payload
        assert contract.identity is canonical_source.orphan_source_identity
        assert contract.has_work is canonical_source.orphan_has_work
        from agent_py_agent.agent.gateway_parts.request_transactions import (
            missing_request_context_rejection,
        )
        assert isinstance(missing_request_context_rejection("synthetic"), RequestExecutionContextUnavailable)
