"""P4 冻结合同；不把尚未接线的合同单测算真实初始化验收。"""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import (
    WorkerBootstrapError,
    freeze_worker_bootstrap,
    require_worker_bootstrap,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    target = (tmp_path / "explicit-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    assert target != (Path.home() / ".my-agent").resolve()
    yield target
    assert not target.exists()


@pytest.fixture
def launch(tmp_path, monkeypatch):
    launch = mode.WorkerLaunch(WorkerPartition(0, 2, "c-one"), tmp_path / "gateway", tmp_path / "home", 101)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    return launch


def test_freeze_is_deeply_immutable_and_does_not_materialize_home(launch, tmp_path):
    frozen = freeze_worker_bootstrap(launch)
    require_worker_bootstrap(frozen, launch)
    with pytest.raises(FrozenInstanceError):
        frozen.mode = "coordinator"
    with pytest.raises(FrozenInstanceError):
        frozen.launch.owner_root = Path("/elsewhere")
    with pytest.raises(FrozenInstanceError):
        frozen.launch.partition.index = 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,value", [("owner_root", Path("/other")), ("gateway_root", Path("/other")),
                                        ("parent_pid", 102), ("partition", WorkerPartition(1, 2, "c-one")),
                                        ("partition", WorkerPartition(0, 3, "c-one")),
                                        ("partition", WorkerPartition(0, 2, "c-old"))])
def test_frozen_launch_changes_are_rejected_before_initialization(launch, field, value):
    frozen = freeze_worker_bootstrap(launch)
    with pytest.raises(WorkerBootstrapError) as error:
        require_worker_bootstrap(frozen, replace(launch, **{field: value}))
    assert error.value.to_payload() == {"ok": False, "error_code": "WORKER_BOOTSTRAP_INVALID",
                                        "reason": "frozen_launch_mismatch", "field": field}


@pytest.mark.parametrize("partition", [None, WorkerPartition(1, 2, "c-one"), WorkerPartition(0, 2, "c-old")])
def test_current_mode_cannot_be_changed_after_freeze(launch, monkeypatch, partition):
    frozen = freeze_worker_bootstrap(launch)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", partition)
    with pytest.raises(WorkerBootstrapError, match="worker_mode_mismatch"):
        require_worker_bootstrap(frozen, launch)


def test_freeze_requires_early_worker_mode(launch, monkeypatch):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    with pytest.raises(WorkerBootstrapError, match="worker_mode_mismatch"):
        freeze_worker_bootstrap(launch)


@pytest.mark.parametrize("field,value", [("owner_root", Path("relative")), ("parent_pid", True),
                                        ("partition", WorkerPartition(True, 2, "c-one")),
                                        ("partition", WorkerPartition(0, 0, "c-one")),
                                        ("partition", WorkerPartition(0, 2, "")),
                                        ("partition", SimpleNamespace(index=0, count=2, c_run_id="c-one"))])
def test_invalid_launch_cannot_be_frozen(launch, field, value):
    with pytest.raises(WorkerBootstrapError):
        freeze_worker_bootstrap(replace(launch, **{field: value}))


def test_bootstrap_mode_cannot_claim_coordinator(launch):
    frozen = replace(freeze_worker_bootstrap(launch), mode="coordinator")
    with pytest.raises(WorkerBootstrapError, match="worker_mode_mismatch"):
        require_worker_bootstrap(frozen, launch)


@pytest.mark.parametrize("code", ["WORKER_BOOTSTRAP_INVALID", "WORKER_SHARED_RESOURCE_INVALID",
                                  "WORKER_READY_REJECTED", "WORKER_REGISTRY_REJECTED"])
def test_bootstrap_rejection_codes_never_fall_back_or_replay(code):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.agent.contracts.recovery import RecoveryAction

    contract = error_contract(code)
    assert contract.code == code
    assert contract.retryable is False
    assert contract.recommended_action == RecoveryAction.REPORT_BLOCKER.value
