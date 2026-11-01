"""真实 probe 入口和两个 Manager 实例交错，保留原收口/通知事实。"""
from __future__ import annotations

import copy

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager


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
