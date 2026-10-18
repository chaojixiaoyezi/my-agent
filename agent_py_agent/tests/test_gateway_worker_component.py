"""W组件合同：真实模式/冻结配置，池构造边界只观察，不宣称实际owner或native启动。"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts import multiprocess_state as state
from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker
from agent_py_agent.agent.gateway_parts import multiprocess_worker_agent as agents
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
from agent_py_agent.agent.user_space.home_root import ConfiguredHomeSnapshot, configured_home_root
from agent_py_agent.tests.test_gateway_multiprocess_worker_dispatch import (  # noqa: F401
    enqueue,
    setup,
)


# LLM: 本异常只标出实际factory到达尚未移植的owner池边界，不模拟池返回或实际W运行。
# 类用途: 在只读观察配置后结束本次组件调用。
class PoolBoundaryObserved(Exception):
    pass


# LLM: 使用真实C环境编码/模式producer和原bootstrap，不以替身Agent或空validator取得许可；全部路径属于tmp。
# 函数用途: 为单进程纯组件准备正式启动结构，测试结束由monkeypatch恢复进程模式。
def launch_case(tmp_path, monkeypatch, selected):
    launch = mode.WorkerLaunch(WorkerPartition(0, 2, "worker-component"), tmp_path / "gateway", selected, os.getppid(), workspace_roots=(tmp_path / "business-workspace",))
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"])
    assert mode.establish_worker_mode(launch) == {"ok": True}
    return launch


@pytest.mark.parametrize("raw", ["empty", "literal_default", "resolved_default"])
def test_factory_freezes_trusted_launch_home_before_pool(tmp_path, monkeypatch, raw):
    fake_user = tmp_path / "synthetic-user"
    monkeypatch.setenv("HOME", str(fake_user))
    selected = (fake_user / ".my-agent").resolve()
    launch = launch_case(tmp_path, monkeypatch, selected)
    config_value = {"empty": "", "literal_default": "~/.my-agent", "resolved_default": str(selected)}[raw]
    path = tmp_path / "worker.yaml"
    path.write_text(f'my_agent_home: "{config_value}"\nmodel_backend: echo\nenable_plugins: false\n')
    monkeypatch.setenv("MY_AGENT_CONFIG", str(path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "later-home"))

    # LLM: 只在实际create_worker_pool调用前读取原配置，不返回fake pool或跳过真实权限门。
    # 函数用途: 确认同一typed事实保护延后构造，随后结束未接owner池的组件实验。
    def observe(frozen, config, root):
        assert frozen.launch is launch and root == launch.workspace_roots[0] and root != selected
        assert type(config.host_home_snapshot) is ConfiguredHomeSnapshot
        assert config.host_home_snapshot.root == selected
        monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "even-later-home"))
        assert Path(configured_home_root(config)) == selected
        raise PoolBoundaryObserved

    monkeypatch.setattr(agents, "create_worker_pool", observe)
    with pytest.raises(PoolBoundaryObserved):
        worker.create_request_pump(launch, None)
    assert not selected.exists()


def test_factory_rejects_explicit_foreign_home_before_pool(tmp_path, monkeypatch):
    selected = (tmp_path / "selected-home").resolve()
    launch = launch_case(tmp_path, monkeypatch, selected)
    config = tmp_path / "foreign.yaml"
    config.write_text(f'my_agent_home: "{tmp_path / "foreign-home"}"\nmodel_backend: echo\n')
    monkeypatch.setattr(agents, "create_worker_pool", lambda *_args: pytest.fail("foreign home reached pool"))
    with pytest.raises(WorkerBootstrapError) as error:
        worker.create_request_pump(launch, config)
    assert error.value.reason == "constructor_home_mismatch"
    assert not selected.exists()


def test_factory_preserves_real_cli05_typed_home_snapshot(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import common, gateway_control_plane
    from agent_py_agent.cli.workspace_resolution import runtime_paths_for_config
    from agent_py_agent.tests.test_gateway_control_plane_frozen_home import _args

    selected = (tmp_path / "original-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(selected))
    args = _args(tmp_path)
    config, _roots = common.agent_configuration(args)
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "real-home-producer")
        control = gateway_control_plane.build_control_plane(args, paths, controller, common.make_agent)
        assert type(control.config.host_home_snapshot) is ConfiguredHomeSnapshot
    launch = launch_case(tmp_path, monkeypatch, selected)
    from agent_py_agent.agent import settings

    monkeypatch.setattr(settings, "load_config", lambda _path: copy.deepcopy(control.config))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "later-home"))

    # LLM: 消费真实C producer的typed配置，仅观察池前边界，不物化owner或伪造启动成功。
    # 函数用途: 核配置loader交回的同次home事实在环境变化后仍保持。
    def observe(_frozen, received, _root):
        assert received.host_home_snapshot == control.config.host_home_snapshot
        assert configured_home_root(received) == str(selected)
        raise PoolBoundaryObserved

    monkeypatch.setattr(agents, "create_worker_pool", observe)
    with pytest.raises(PoolBoundaryObserved):
        worker.create_request_pump(launch, args.config)


@pytest.mark.parametrize("invalid,reason", [(True, "request_not_before_invalid"),
                         (-1, "request_not_before_invalid"),
                         (float("nan"), "claim_request_unreadable"),
                         (float("inf"), "claim_request_unreadable"),
                         (10**400, "request_not_before_invalid")],
                         ids=["boolean", "negative", "nan", "infinite", "huge_integer"])
def test_pending_invalid_time_is_reported_without_owner_materialization(setup, monkeypatch, invalid, reason):  # noqa: F811
    request = dict(setup.request, not_before_at=invalid)
    path = enqueue(setup, request)
    pump = WorkerRequestPump(setup.frozen, setup.pool, 1)
    errors = []
    monkeypatch.setattr(pump, "_report_rejection", lambda _path, error: errors.append(error))
    try:
        assert pump._candidate(path) is None
        assert len(errors) == 1 and errors[0].reason == reason
        assert setup.owners == [] and path.exists()
    finally:
        pump.close()


def test_projection_giant_timestamp_is_unhealthy():
    heartbeat = {"worker": 0, "of": 2, "c_run_id": "same", "ready": True,
                 "loop_health": "healthy", "updated_at": 10**400}
    assert state.stampable_workers([heartbeat], WorkerPartition(0, 2, "same"), 100.0) == []
