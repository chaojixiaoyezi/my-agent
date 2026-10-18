"""正式CLI按内部count选择原C入口；不启动worker、HTTP或真实模型。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import current_coordinator
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.cli import common, gateway_process
from agent_py_agent.cli.workspace_resolution import runtime_paths_for_config
from agent_py_agent.tests.test_gateway_control_plane_frozen_home import _args


# LLM: 测试哨兵只中止实际factory，不替代C/owner权限或生成业务结果。
# 类用途: 确定原入口到达业务构造边界后立即结束测试。
class FactoryObserved(BaseException):
    pass


def test_positive_count_holds_original_c_before_business_factory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    args = _args(tmp_path)
    args._gateway_worker_count = 2
    args._gateway_c_run_id = "cli-original-c"
    config, _ = common.agent_configuration(args)
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    observed = []
    before_business = []
    owner = resolve_owner_home(home)

    def no_http(*_args, **_kwargs):
        assert current_coordinator(paths.root, 2) is not None
        # 锁/运行目录的规范路径本来就在local/main/workspace下；目录祖先不是业务初始化证据。
        assert not owner.permissions_json.exists()
        assert not owner.agents_md.exists()
        assert not owner.curator_state_json.exists()
        before_business.append(True)
        return None

    def factory(actual):
        lease = current_coordinator(paths.root, 2)
        assert lease is not None and lease.c_run_id == "cli-original-c"
        # 原P2可在调用factory之前物化已经获准的owner，不能把它当锁外初始化。
        assert before_business == [True]
        observed.append(actual)
        raise FactoryObserved

    monkeypatch.setattr(gateway_process, "make_agent", factory)
    monkeypatch.setattr(gateway_process, "_start_gateway_http", no_http)
    with pytest.raises(FactoryObserved):
        gateway_process.cmd_gateway_run(args)
    assert len(observed) == 1
    assert current_coordinator(paths.root, 2) is None


def test_default_count_does_not_acquire_c_or_prepare_home_before_factory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    args = _args(tmp_path)
    config, _ = common.agent_configuration(args)
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    calls = []

    def factory(actual):
        assert current_coordinator(paths.root, 2) is None
        assert not home.exists()
        calls.append(actual)
        raise FactoryObserved

    monkeypatch.setattr(gateway_process, "make_agent", factory)
    with pytest.raises(FactoryObserved):
        gateway_process.cmd_gateway_run(args)
    assert calls == [args]
    assert not home.exists()


def test_positive_count_predecessor_timeout_refuses_before_config_or_factory(tmp_path, monkeypatch, capsys):
    home = tmp_path / "must-not-create"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    calls = []
    args = SimpleNamespace(config=str(tmp_path / "missing.yaml"), after_pid=123, _gateway_worker_count=2)
    monkeypatch.setattr(gateway_process, "wait_for_predecessor_exit", lambda _pid: False)
    monkeypatch.setattr(gateway_process, "make_agent", lambda _args: calls.append(True) or (_ for _ in ()).throw(FactoryObserved()))
    assert gateway_process.cmd_gateway_run(args) == 2
    assert calls == [] and not home.exists()
    assert "GATEWAY_PREDECESSOR_TIMEOUT" in capsys.readouterr().err


@pytest.mark.parametrize("count", [True, -1, "2", 1.5, None])
def test_invalid_internal_count_refuses_without_business_initialization(tmp_path, monkeypatch, capsys, count):
    home = tmp_path / "must-not-create"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    args = SimpleNamespace(config=str(tmp_path / "missing.yaml"), _gateway_worker_count=count)
    calls = []
    monkeypatch.setattr(gateway_process, "make_agent", lambda _args: calls.append(True) or (_ for _ in ()).throw(FactoryObserved()))
    assert gateway_process.cmd_gateway_run(args) == 2
    assert calls == [] and not home.exists()
    assert "COORDINATOR_PLATFORM_OR_MODE_UNSUPPORTED" in capsys.readouterr().err


def test_context_transports_original_control_and_count_without_authorizing_it(tmp_path):
    control = object()
    agent = object()
    paths = gateway_paths_from_root(tmp_path / "gateway")
    args = SimpleNamespace(config=str(tmp_path / "config.yaml"), _gateway_worker_count=2, _gateway_control_plane=control)
    context = gateway_process._gateway_run_context_from_args(gateway_process._GatewayRunBuildRequest(agent, paths, args))
    assert context.agent is agent and context.paths is paths
    assert context.control_plane is control and context.coordinator_worker_count == 2
    assert context.drain_runtime is None
