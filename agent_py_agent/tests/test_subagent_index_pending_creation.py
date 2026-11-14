"""精确 ID 预留与创建前后故障的副作用边界。"""
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.persistence.run_index import (
    SubagentRunIndex,
    SubagentRunIndexUnavailable,
)


def test_known_ids_during_pending_commit_retains_reservation_without_recovery(tmp_path):
    index = SubagentRunIndex(tmp_path)
    pending, release = Event(), Event()
    errors = []
    task = SubAgentTask(id="reserved", goal="work", thought="", plan=[], status="RUNNING")

    def write():
        try:
            index.mark_pending(task.id)
            pending.set()
            assert release.wait(10)
            index.upsert(task)
        except BaseException as exc:
            errors.append(exc)
            pending.set()

    writer = Thread(target=write)
    writer.start()
    try:
        assert pending.wait(10)
        reader = SubagentRunIndex(tmp_path, read_only=True)
        assert reader.known_ids(["reserved", "missing", "reserved"]) == {"reserved"}
        assert index.readiness() == {"complete": True, "pending": True}
        with pytest.raises(SubagentRunIndexUnavailable, match="pending canonical"):
            reader.active_counts()
    finally:
        release.set()
        writer.join(10)
    assert not writer.is_alive() and not errors
    assert index.known_ids(["reserved", "missing"]) == {"reserved"}
    assert index.active_counts() == (1, 0)


def test_known_ids_partial_coverage_still_refuses_negative_evidence(tmp_path):
    index = SubagentRunIndex(tmp_path)
    index.mark_pending("unknown-publication")
    with index.connect() as db:
        db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    with pytest.raises(SubagentRunIndexUnavailable, match="coverage is partial"):
        index.known_ids(["unknown-publication", "missing"])
    assert index.readiness()["pending"]


def test_known_ids_chunked_pending_ids_are_not_missing_or_published(tmp_path):
    index = SubagentRunIndex(tmp_path)
    candidates = [f"run-{i}" for i in range(1025)]
    for run_id in candidates[::256]:
        index.mark_pending(run_id)
    expected = set(candidates[::256])
    assert index.known_ids([*candidates, *reversed(candidates)]) == expected
    assert index.readiness()["pending"]
    assert not list(tmp_path.glob("*/canonical_state.json"))
    assert len(index.pending_rows(batch_size=64)) == len(expected)


@pytest.mark.parametrize("phase", ["first_preflight", "second_preflight", "materialization"])
def test_create_index_failure_receipt_respects_materialization_boundary(monkeypatch, phase):
    from agent_py_agent.agent.agent_core import orchestration_tools as module

    calls = []
    prepared = SimpleNamespace(prepared_runs=[])
    agent = SimpleNamespace(subagents=None)

    def prepare(*args):
        calls.append("prepare")
        if phase == "first_preflight" or (phase == "second_preflight" and calls.count("prepare") == 2):
            raise SubagentRunIndexUnavailable("subagent run index coverage is partial")
        return prepared

    def materialize(*args):
        calls.append("materialize")
        raise SubagentRunIndexUnavailable("subagent run index has pending canonical commits")

    monkeypatch.setattr(module, "_prepare_create_subagents", prepare)
    monkeypatch.setattr(module, "prepare_subagent_model_decision", lambda *args: object() if phase == "second_preflight" else None)
    monkeypatch.setattr(module, "decide_subagent_models", lambda *args: object())
    monkeypatch.setattr(module, "_materialize_create_subagents", materialize)
    outcome = module.execute_create_subagents_service(agent, {"items": []})
    assert not outcome.ok
    if phase == "materialization":
        assert calls == ["prepare", "materialize"]
        assert outcome.effect_outcome == "unknown"
        assert outcome.result_envelope["partial_creation_possible"] is True
    else:
        assert "materialize" not in calls
        assert outcome.reported_error_code == "SUBAGENT_RUN_INDEX_UNAVAILABLE"
        assert outcome.effect_outcome == "not_started"
        assert outcome.result_envelope["failure_stage"] == "preflight"
        assert outcome.result_envelope["partial_creation_possible"] is False
        assert "coverage is partial" in outcome.output and "list_agents" in outcome.output


def test_published_pending_run_recovers_same_identity_before_concurrent_reuse(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.agent_core.orchestration.create_constraints import resolve_create_run
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.base import CreateRunParams
    from agent_py_agent.agent.subagents.services.persistence import service

    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path)
    params = CreateRunParams(goal="同一工作", thought="", plan=[], attributes={"work_scope_key": "one-work"})
    original = service.write_private_json_file_atomic
    published_ids = []

    def fail_locator(path, payload, *args, **kwargs):
        if path.parent.parent == manager.workspace and path.name == "task.json":
            published_ids.append(payload["run_id"])
            raise OSError("locator interrupted after canonical publication")
        return original(path, payload, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(service, "write_private_json_file_atomic", fail_locator)
        with pytest.raises(OSError, match="locator interrupted"):
            manager.create_run(params=params)
    run_id, = published_ids
    assert manager.persistence.run_index.known_ids([run_id, "missing"]) == {run_id}
    recovery = manager.persistence.prepare_run_index()
    assert recovery["recovered"] == 1 and not recovery["errors"] and not recovery["pending"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        resolutions = list(pool.map(lambda _: resolve_create_run(manager, params), range(8)))
    assert {item.task.id for item in resolutions} == {run_id}
    assert all(item.reused for item in resolutions)
    assert manager.persistence.run_index.active_counts() == (1, 0)
