"""健康检查只提交诊断字段，不回滚并发收口和执行轮。"""
from __future__ import annotations

import copy
from dataclasses import asdict

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import ChannelProbeCheck, FailureType


@pytest.fixture(params=[False, True], ids=["LOCAL_FILE", "MANAGED"])
def manager(tmp_path, request):
    return SubAgentManager(
        tmp_path / "workspace", owner_id="local/main",
        owner_home_dir=str(tmp_path / "owner") if request.param else "",
    )


def _checks(*, broken=False):
    return [ChannelProbeCheck(name="fixture", ok=not broken, severity="P0", summary="controlled probe")]


@pytest.mark.parametrize("pending_before,pending_after", [(False, True), (True, False)])
def test_probe_does_not_erase_or_resurrect_closeout_pending(manager, pending_before, pending_after):
    task = manager.create_run(goal="closeout race", role="worker")

    def initialize(current):
        current.status = "DONE"
        current.runner_attempts = 1
        current.runner_active_attempt_id = "attempt-original"
        if pending_before:
            current.attributes["runtime_closeout_pending"] = {"attempt_id": "attempt-original"}

    manager.mutate(task.id, initialize)
    stale = manager.load(task.id)

    def closeout(current):
        if pending_after:
            current.attributes["runtime_closeout_pending"] = {"attempt_id": "attempt-original"}
        else:
            current.attributes.pop("runtime_closeout_pending", None)
        current.attributes["completion_notice_receipt"] = {"attempt_id": "attempt-original", "sent": True}

    manager.mutate(task.id, closeout)
    before = asdict(manager.load(task.id))
    manager.channel_probe.update_task_from_probe(stale, _checks(), 123.0)
    after = asdict(manager.load(task.id))
    assert bool(after["attributes"].get("runtime_closeout_pending")) is pending_after
    # canonical mutation 同步推进报告版本；其余报告字段仍必须逐字保真。
    assert after["latest_status_report"]["version"] == after["state_revision"]
    assert after["state_revision"] == before["state_revision"] + 1
    before["latest_status_report"]["version"] = after["state_revision"]
    allowed = {"channel_checks", "channel_status", "last_probe_at", "updated_at", "state_revision"}
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }
    assert after["last_probe_at"] == 123.0


def test_probe_updates_health_but_preserves_new_terminal_failure_and_attempt(manager):
    task = manager.create_run(goal="terminal probe race", role="worker")
    stale = manager.load(task.id)

    def terminal(current):
        current.status = "DONE"
        current.failure_type = "provider_timeout"
        current.runner_attempts = 2
        current.runner_active_attempt_id = "attempt-current"

    manager.mutate(task.id, terminal)
    manager.channel_probe.update_task_from_probe(stale, _checks(broken=True), 456.0)
    latest = manager.load(task.id)
    assert latest.status == "DONE"
    assert latest.failure_type == "provider_timeout"
    assert latest.runner_attempts == 2
    assert latest.runner_active_attempt_id == "attempt-current"
    assert latest.channel_status == "BROKEN"
    assert latest.last_probe_at == 456.0


def test_probe_cannot_change_new_attempt_failure(manager):
    task = manager.create_run(goal="attempt race", role="worker")
    stale = manager.load(task.id)

    def next_attempt(current):
        current.status = "RUNNING"
        current.failure_type = "provider_timeout"
        current.runner_attempts = 3
        current.runner_active_attempt_id = "attempt-new"

    manager.mutate(task.id, next_attempt)
    manager.channel_probe.update_task_from_probe(stale, _checks(broken=True), 789.0)
    latest = manager.load(task.id)
    assert latest.status == "RUNNING" and latest.runner_attempts == 3
    assert latest.runner_active_attempt_id == "attempt-new"
    assert latest.failure_type == "provider_timeout"


def test_live_probe_still_records_channel_failure(manager):
    task = manager.create_run(goal="live broken channel", role="worker")
    manager.channel_probe.update_task_from_probe(task, _checks(broken=True), 100.0)
    latest = manager.load(task.id)
    assert latest.channel_status == "BROKEN" and latest.failure_type == FailureType.CHANNEL.value


@pytest.mark.parametrize("managed", [False, True], ids=["LOCAL_FILE", "MANAGED"])
@pytest.mark.parametrize("pending_after", [False, True], ids=["clear-pending", "create-pending"])
def test_public_probe_does_not_roll_back_peer_closeout(tmp_path, monkeypatch, managed, pending_after):
    workspace = tmp_path / "workspace"
    home = str(tmp_path / "owner") if managed else ""
    manager = SubAgentManager(workspace, owner_home_dir=home, owner_id="local/main")
    peer = SubAgentManager(workspace, owner_home_dir=home, owner_id="local/main")
    task = manager.create_run(goal="real probe interleaving", role="worker")

    def initialize(current):
        current.status = "DONE"
        current.runner_attempts = 1
        current.runner_active_attempt_id = "attempt-observed"
        if not pending_after:
            current.attributes["runtime_closeout_pending"] = {"attempt_id": "attempt-observed"}

    manager.mutate(task.id, initialize)
    original = manager.channel_probe.write_channel_probe_files
    expected = []

    def finish_during_probe(snapshot, report):
        evidence = original(snapshot, report)

        def settle(current):
            if pending_after:
                current.attributes["runtime_closeout_pending"] = {"attempt_id": "attempt-observed"}
            else:
                current.attributes.pop("runtime_closeout_pending", None)
            current.attributes["completion_notice_receipt"] = {"attempt_id": "attempt-observed", "sent": True}

        expected.append(copy.deepcopy(peer.mutate(task.id, settle).attributes))
        return evidence

    monkeypatch.setattr(manager.channel_probe, "write_channel_probe_files", finish_during_probe)
    report = manager.channel_probe.probe_channel(task.id)
    current = peer.load(task.id)
    assert report.run_id == task.id and current.last_probe_at == report.created_at
    assert current.channel_status == report.channel_status
    assert current.status == "DONE" and current.runner_active_attempt_id == "attempt-observed"
    assert current.attributes == expected[0]
    assert bool(current.attributes.get("runtime_closeout_pending")) is pending_after
