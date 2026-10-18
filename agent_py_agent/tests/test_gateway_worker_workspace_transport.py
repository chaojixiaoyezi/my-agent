"""C原构造根跨进程运输；只用临时目录、Echo和Popen边界观察，不启动W/native。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import (
    WorkerBootstrapError,
    freeze_worker_bootstrap,
    require_worker_bootstrap,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
from agent_py_agent.cli import common, gateway_control_plane, gateway_worker, gateway_worker_runtime
from agent_py_agent.cli.workspace_resolution import runtime_paths_for_config
from agent_py_agent.tests.test_gateway_control_plane_frozen_home import _args
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import worker as worker


def test_c_resolved_workspace_tuple_is_frozen_independently_of_home_and_later_config(tmp_path, monkeypatch):
    selected = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(selected))
    args = _args(tmp_path)
    roots = (selected / "owners/local/main/first", selected / "owners/local/main/second")
    args.workspace_root = [str(root) for root in roots]
    config, actual = common.agent_configuration(args)
    assert tuple(actual) == roots
    paths = gateway_paths_from_root(runtime_paths_for_config(config)["gateway_workspace"])
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "workspace-transport")
        control = gateway_control_plane.build_control_plane(args, paths, controller, common.make_agent)
        assert control.workspace_roots == roots and control.agent_root == roots[0]
        args.workspace_root[:] = [str(tmp_path / "later")]
        control.config.workspace_root = [str(tmp_path / "config-later")]
        assert control.workspace_roots == roots and control.agent_root != control.home_root


@pytest.mark.parametrize("change", ["replace", "drop", "reorder"])
def test_env_and_cli_require_exact_original_workspace_tuple(worker, monkeypatch, change):
    frozen, _, root, _, _ = worker
    roots = (root, root / "second")
    launch = replace(frozen.launch, workspace_roots=roots)
    marker = mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"]
    assert json.loads(marker)["workspace_roots"] == [str(p) for p in roots]
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", marker)
    changed = {"replace": (root / "other",), "drop": (), "reorder": tuple(reversed(roots))}[change]
    assert mode.establish_worker_mode(replace(launch, workspace_roots=changed))["ok"] is False
    with pytest.raises(WorkerBootstrapError):
        require_worker_bootstrap(freeze_worker_bootstrap(launch), replace(launch, workspace_roots=changed))


@pytest.mark.parametrize("roots", [None, [], 7, True, "root", (Path("relative"),), (Path("/a/../b"),), (Path("/a"), Path("/a")), ([],)])
def test_invalid_workspace_carrier_is_rejected_without_initialization(worker, monkeypatch, roots):
    frozen, _, _, _, _ = worker
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(frozen.launch, {})["MY_AGENT_GATEWAY_WORKER"])
    changed = replace(frozen.launch, workspace_roots=roots)
    assert mode.establish_worker_mode(changed) == {"ok": False, "error_code": "WORKER_MODE_INVALID"}
    with pytest.raises((ValueError, WorkerBootstrapError)):
        mode.worker_environment(changed, {})


def test_legacy_pure_launch_cannot_be_used_for_real_worker_pool(worker):
    frozen, config, root, _, _ = worker
    legacy = replace(frozen.launch, workspace_roots=())
    before = sorted(str(p) for p in frozen.launch.owner_root.rglob("*"))
    with pytest.raises(WorkerBootstrapError, match="workspace"):
        create_worker_pool(freeze_worker_bootstrap(legacy), config, root)
    assert sorted(str(p) for p in frozen.launch.owner_root.rglob("*")) == before


def test_real_pool_keeps_c_constructor_roots_and_original_owner_wall(worker):
    frozen, config, root, owner, _ = worker
    roots = (root, root / "second")
    root.joinpath("second").mkdir()
    frozen = freeze_worker_bootstrap(replace(frozen.launch, workspace_roots=roots))
    ensure_owner_home(frozen.launch.owner_root, owner)
    agent = create_worker_pool(frozen, config, root).get(owner)
    assert agent.root == root and tuple(agent.workspace_roots) == roots
    assert agent.root != agent.home_paths.root
    assert agent.effective_workspace_roots == [agent.home_paths.owner_home_dir]
    assert agent.effective_workspace_root == agent.home_paths.owner_home_dir


@pytest.mark.parametrize("changed", ["root", "roots", "missing"])
def test_direct_worker_constructor_cannot_bypass_frozen_workspace_arguments(worker, changed):
    frozen, config, root, owner, _ = worker
    roots = (root, root / "second")
    frozen = freeze_worker_bootstrap(replace(frozen.launch, workspace_roots=roots))
    ensure_owner_home(frozen.launch.owner_root, owner)
    foreign = root / "must-not-create"
    actual_root = foreign if changed == "root" else root
    actual_roots = [root, foreign] if changed == "roots" else None if changed == "missing" else list(roots)
    with pytest.raises(WorkerBootstrapError, match="workspace"):
        SimpleAgent(_config_with_owner(config, owner), actual_root, workspace_roots=actual_roots, worker_bootstrap=frozen)
    assert not foreign.exists()


def test_internal_argv_and_environment_transport_same_roots_without_process_spawn(worker, monkeypatch):
    frozen, _, root, _, _ = worker
    roots = (root, root / "second")
    launch = replace(frozen.launch, workspace_roots=roots)
    captured = []
    monkeypatch.setattr(gateway_worker_runtime.subprocess, "Popen", lambda command, **options: captured.append((command, options)) or "not-a-process")
    actual = gateway_worker_runtime.CoordinatorWorkers(SimpleNamespace(), root / "config.yaml", 0)
    assert actual._spawn(launch) == "not-a-process"
    command, options = captured[0]
    parsed = gateway_worker._worker_parser().parse_args(command[command.index("worker") + 1:])
    assert tuple(parsed.workspace_root) == roots
    assert json.loads(options["env"]["MY_AGENT_GATEWAY_WORKER"])["workspace_roots"] == [str(p) for p in roots]
    assert command[0] == gateway_worker_runtime.sys.executable


@pytest.mark.parametrize("field", ["root", "workspace_roots"])
def test_cached_worker_agent_cannot_change_constructor_roots(worker, field):
    frozen, config, root, owner, _ = worker
    ensure_owner_home(frozen.launch.owner_root, owner)
    pool = create_worker_pool(frozen, config, root)
    agent = pool.get(owner)
    setattr(agent, field, root / "foreign" if field == "root" else [root / "foreign"])
    with pytest.raises(WorkerBootstrapError, match="workspace"):
        pool.get(owner)
    with pytest.raises(WorkerBootstrapError, match="workspace"):
        pool.peek(owner)


@pytest.mark.parametrize("tamper", [False, True])
def test_early_worker_entry_validates_c_roots_before_runtime_import_call(worker, monkeypatch, tamper):
    from agent_py_agent.agent.gateway_parts import multiprocess_worker

    frozen, _, root, _, _ = worker
    launch = replace(frozen.launch, workspace_roots=(root, root / "second"))
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"])
    reached = []
    monkeypatch.setattr(multiprocess_worker, "run_worker", lambda actual, *_args, **_kwargs: reached.append(actual) or 0)
    command = ["gateway", "worker", "--index", str(launch.partition.index), "--of", str(launch.partition.count),
               "--run", launch.partition.c_run_id, "--gateway-root", str(launch.gateway_root),
               "--owner-root", str(launch.owner_root), "--parent-pid", str(launch.parent_pid)]
    for path in (root / "foreign",) if tamper else launch.workspace_roots:
        command.extend(["--workspace-root", str(path)])
    assert gateway_worker.dispatch_worker_entry(command) == (2 if tamper else 0)
    assert reached == ([] if tamper else [launch])
