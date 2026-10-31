"""W8：真实 owner 调度账本、可控时钟与旧定义迟到收口。"""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.scheduler.monitor_state import condition_matches, due_rule_views
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)


class Clock:
    def __init__(self, now=900):
        self.now = now

    def advance(self, now):
        self.now = now
        return now


def at(when):
    return {"kind": "at", "at": datetime.fromtimestamp(when, timezone.utc).isoformat(), "timezone": "UTC"}


def every(when=1000):
    return {"kind": "every", "every_seconds": 600, "anchor_at": when, "timezone": "UTC"}


def repository(tmp_path):
    return SchedulerRepository(tmp_path / "scheduler", owner_provider="local", owner_kind="main", owner_id="main", default_timezone="UTC")


def create(repo, schedule, *, monitor=False, rules=None):
    definition = {"id": "probe", "name": "动作 A", "schedule": schedule, "use_llm": False,
                  "tool": {"name": "read_file", "arguments": {"path": "a.json"}}}
    return repo.create_job(SchedulerJobCreateRequest(name="测试监控", prompt="动作 A", thread_id="thread-test",
        schedule=schedule, rules=rules or ([definition] if monitor else None), now=900))[0]


def start(repo, job, clock):
    run = repo.reserve_due_runs(now=clock.advance(1000))[0]
    claimed = repo.claim_run(run["run_id"], lease_seconds=300, now=1001)
    repo.mark_run_running(run["run_id"], claimed["claim_id"], now=1002)
    return claimed


def patch_rules(job, **patch):
    runtime = {"next_run_at", "last_run_at", "last_run_status", "last_error_code", "run_count",
               "consecutive_failures", "backoff_until", "precheck_checkpoint", "result", "definition_generation"}
    return [{**{key: deepcopy(value) for key, value in rule.items() if key not in runtime}, **patch} for rule in job["rules"]]


@pytest.mark.parametrize("monitor", [False, True])
@pytest.mark.parametrize("old_schedule", [at(1000), every()])
def test_old_run_cannot_consume_replacement_future_at(tmp_path, monitor, old_schedule):
    repo, clock = repository(tmp_path), Clock()
    job = create(repo, old_schedule, monitor=monitor)
    run = start(repo, job, clock)
    before = repo.get_job(job["job_id"])
    patch = {"rules": patch_rules(before, schedule=at(2000))} if monitor else {"schedule": at(2000)}
    changed = repo.update_job(job["job_id"], patch=patch, expected_version=before["version"], now=1010)
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="done", now=clock.advance(1020)))
    current = repo.get_job(job["job_id"])
    assert current["status"] == "active"
    assert current["next_run_at"] == changed["next_run_at"] == 2000
    assert repo.reserve_due_runs(now=1999) == []
    fresh = repo.reserve_due_runs(now=clock.advance(2000))
    assert len(fresh) == 1 and fresh[0]["scheduled_for"] == 2000
    assert fresh[0]["definition_generation"] == run["definition_generation"] + 1
    assert repo.history()[0][0]["run_id"] == run["run_id"]
    assert repo.history()[0][0]["status"] == "done"


def test_late_action_a_does_not_publish_action_b_result(tmp_path):
    repo, clock = repository(tmp_path), Clock()
    upstream = {"id": "probe", "name": "探测", "schedule": every(), "use_llm": False,
                "tool": {"name": "read_file", "arguments": {"path": "a.json"}}}
    downstream = {"id": "report", "name": "汇报", "schedule": every(1100), "use_llm": True, "prompt": "汇报 B",
                  "depends_on": ["probe"], "condition": {"rule_id": "probe", "pointer": "/ok", "op": "truthy", "max_age_seconds": 300}}
    job = create(repo, every(), rules=[upstream, downstream])
    run = start(repo, job, clock)
    repo.record_rule_result(run["run_id"], run["claim_id"], {"ok": True, "action": "A"})
    before = repo.get_job(job["job_id"])
    changed_rules = [dict(upstream, tool={"name": "read_file", "arguments": {"path": "b.json"}}), downstream]
    repo.update_job(job["job_id"], patch={"rules": changed_rules}, expected_version=before["version"], now=1010)
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="done", now=1020))
    current = repo.get_job(job["job_id"])
    view = next(view for view in due_rule_views(current, 1100) if view["rule_id"] == "report")
    assert not condition_matches(current, view, 1100)
    assert current["rules"][0].get("result") is None
    assert repo.reserve_due_runs(now=1100) == []
    historical = repo.history()[0]
    old = next(row for row in historical if row["run_id"] == run["run_id"])
    assert old["structured_result"] == {"ok": True, "action": "A"}


def test_dependency_checks_generation_as_well_as_age(tmp_path):
    repo = repository(tmp_path)
    job = create(repo, every(), monitor=True)
    run = start(repo, job, Clock())
    repo.record_rule_result(run["run_id"], run["claim_id"], {"ok": True})
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="done", now=1010))
    current = repo.get_job(job["job_id"])
    view = {"depends_on": ["probe"], "condition": {"rule_id": "probe", "pointer": "/ok", "op": "truthy", "max_age_seconds": 10}}
    assert condition_matches(current, view, 1011)
    assert not condition_matches(current, view, 1021)
    forged = deepcopy(current)
    forged["rules"][0]["result"]["definition_generation"] = 0
    assert not condition_matches(forged, view, 1011)
    assert not condition_matches(forged, {**view, "condition": None}, 1011)
    assert current["rules"][0]["result"]["definition_generation"] == run["definition_generation"]


@pytest.mark.parametrize("monitor", [False, True])
def test_only_display_name_and_claim_versions_keep_generation(tmp_path, monitor):
    repo = repository(tmp_path)
    job = create(repo, every(), monitor=monitor)
    assert (job["rules"][0] if monitor else job)["definition_generation"] == 1
    run = start(repo, job, Clock())
    before = repo.get_job(job["job_id"])
    patch = {"name": "新展示名"}
    if monitor:
        patch["rules"] = patch_rules(before, name="新规则展示名")
    changed = repo.update_job(job["job_id"], patch=patch, expected_version=before["version"], now=1010)
    assert changed["version"] > job["version"]
    assert (changed["rules"][0] if monitor else changed)["definition_generation"] == run["definition_generation"] == 1
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="done", now=1020))
    assert (repo.get_job(job["job_id"])["rules"][0] if monitor else repo.get_job(job["job_id"]))["definition_generation"] == 1


@pytest.mark.parametrize("operation", ["pause_job", "delete_job"])
@pytest.mark.parametrize("status", ["done", "failed"])
@pytest.mark.parametrize("monitor", [False, True])
def test_late_completion_preserves_pause_delete_and_backoff(tmp_path, operation, status, monitor):
    repo = repository(tmp_path)
    job = create(repo, every(), monitor=monitor)
    run = start(repo, job, Clock())
    current = repo.get_job(job["job_id"])
    changed = getattr(repo, operation)(job["job_id"], expected_version=current["version"], now=1010)
    repo.record_rule_result(run["run_id"], run["claim_id"], {"ok": True})
    repo.record_precheck(run["run_id"], run["claim_id"], {"status": "changed", "fingerprint": "old"})
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status=status, now=1020))
    after = repo.get_job(job["job_id"], include_deleted=True)
    assert after["status"] == changed["status"]
    for key in ("next_run_at", "paused_at", "paused_reason", "backoff_until", "consecutive_failures", "precheck_checkpoint", "result"):
        assert after.get(key) == changed.get(key)
    if monitor:
        for key in ("enabled", "next_run_at", "backoff_until", "consecutive_failures", "precheck_checkpoint", "result"):
            assert after["rules"][0].get(key) == changed["rules"][0].get(key)
    assert repo.reserve_due_runs(now=1600) == []


def test_legacy_records_adapt_to_initial_generation_without_read_side_write(tmp_path):
    repo = repository(tmp_path)
    job = create(repo, every(), monitor=True)
    run = start(repo, job, Clock())
    with repo._mutation_scope() as (store, admission):
        for rule in store["jobs"][job["job_id"]]["rules"]:
            rule.pop("definition_generation", None)
        store["jobs"][job["job_id"]].pop("definition_generation", None)
        store["runs"][run["run_id"]].pop("definition_generation", None)
        repo._write_store_unlocked(store, admission)
    before = repo.store_path.read_bytes()
    loaded = repository(tmp_path)
    assert loaded.get_job(job["job_id"])["rules"][0]["definition_generation"] == 1
    assert loaded.get_active_run(run["run_id"])["definition_generation"] == 1
    assert repo.store_path.read_bytes() == before


def test_removed_and_readded_rule_does_not_reuse_running_generation(tmp_path):
    repo = repository(tmp_path)
    upstream = {"id": "probe", "name": "动作 A", "schedule": every(), "use_llm": False,
                "tool": {"name": "read_file", "arguments": {"path": "a.json"}}}
    keeper = {"id": "keeper", "name": "保留规则", "schedule": every(2000), "use_llm": True, "prompt": "保留"}
    job = create(repo, every(), rules=[upstream, keeper])
    run = start(repo, job, Clock())
    repo.record_rule_result(run["run_id"], run["claim_id"], {"old": True})
    current = repo.get_job(job["job_id"])
    removed = repo.update_job(job["job_id"], patch={"rules": [keeper]}, expected_version=current["version"], now=1010)
    readded = repo.update_job(job["job_id"], patch={"rules": [dict(upstream, schedule=every(1500)), keeper]},
        expected_version=removed["version"], now=1020)
    assert readded["rules"][0]["definition_generation"] > run["definition_generation"]
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="done", now=1030))
    assert repo.get_job(job["job_id"])["rules"][0].get("result") is None


@pytest.mark.parametrize("monitor", [False, True])
def test_old_failure_does_not_overwrite_new_definition_backoff_or_checkpoint(tmp_path, monitor):
    repo = repository(tmp_path)
    job = create(repo, every(), monitor=monitor)
    run = start(repo, job, Clock())
    current = repo.get_job(job["job_id"])
    patch = ({"rules": patch_rules(current, tool={"name": "read_file", "arguments": {"path": "b"}})}
             if monitor else {"prompt": "动作 B"})
    changed = repo.update_job(job["job_id"], patch=patch, expected_version=current["version"], now=1010)
    with repo._mutation_scope() as (store, admission):
        target = store["jobs"][job["job_id"]]["rules"][0] if monitor else store["jobs"][job["job_id"]]
        target.update(backoff_until=1700, consecutive_failures=3, precheck_checkpoint={"fingerprint": "B"})
        repo._write_store_unlocked(store, admission)
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="failed", now=1020))
    current = repo.get_job(job["job_id"])
    target = current["rules"][0] if monitor else current
    assert target["consecutive_failures"] == 3
    assert target["backoff_until"] == 1700
    assert target["precheck_checkpoint"] == {"fingerprint": "B"}
    assert target["next_run_at"] == (changed["rules"][0] if monitor else changed)["next_run_at"]


def test_event_generation_is_the_only_definition_authority(tmp_path):
    from agent_py_agent.tests.test_scheduler_event_sources import create as event_create
    from agent_py_agent.tests.test_scheduler_event_sources import event
    from agent_py_agent.tests.test_scheduler_event_sources import repository as event_repo
    repo = event_repo(tmp_path)
    job = event_create(repo)
    assert "definition_generation" not in job
    repo.accept_source_event(event(), now=1000)
    run = repo.reserve_event_runs(now=1000)[0]
    assert run["definition_generation"] == job["event_source_state"][""]["generation"]
    renamed = repo.update_job(job["job_id"], patch={"name": "改展示名"}, expected_version=repo.get_job(job["job_id"])["version"], now=1010)
    assert renamed["event_source_state"][""]["generation"] == run["definition_generation"]
    changed = repo.update_job(job["job_id"], patch={"policy": {"failure_backoff_seconds": 60}}, expected_version=renamed["version"], now=1011)
    assert changed["event_source_state"][""]["generation"] == run["definition_generation"] + 1
    assert "definition_generation" not in changed


@pytest.mark.parametrize("remove_first", [False, True])
def test_time_to_event_conversion_never_reuses_retired_time_generation(tmp_path, remove_first):
    from agent_py_agent.tests.test_scheduler_event_sources import create as event_create
    from agent_py_agent.tests.test_scheduler_event_sources import repository as event_repo
    from agent_py_agent.tests.test_scheduler_event_sources import schedule as event_schedule

    repo = event_repo(tmp_path)
    upstream = {"id": "probe", "name": "动作 A", "schedule": every(), "use_llm": True, "prompt": "动作 A"}
    keeper = {"id": "keeper", "name": "保留", "schedule": every(2000), "use_llm": True, "prompt": "保留"}
    job = event_create(repo, rules=[upstream, keeper])
    run = start(repo, job, Clock())
    repo.record_rule_result(run["run_id"], run["claim_id"], {"old": True})
    before = repo.get_job(job["job_id"])
    changed = repo.update_job(job["job_id"], patch={"rules": [dict(upstream, prompt="动作 B"), keeper]},
        expected_version=before["version"], now=1010)
    previous_generation = changed["rules"][0]["definition_generation"]
    if remove_first:
        changed = repo.update_job(job["job_id"], patch={"rules": [keeper]}, expected_version=changed["version"], now=1011)
    converted = repo.update_job(job["job_id"], patch={"rules": [dict(upstream, schedule=event_schedule()), keeper]},
        expected_version=changed["version"], now=1020)
    assert converted["event_source_state"]["probe"]["generation"] > previous_generation
    assert "definition_generation" not in converted["rules"][0]
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="done", now=1030))
    current = repo.get_job(job["job_id"])
    assert current["rules"][0] == converted["rules"][0]
    assert not condition_matches(current, {"depends_on": ["probe"], "condition": None}, 1031)
    assert repo.history()[0][0]["run_id"] == run["run_id"]


@pytest.mark.parametrize("monitor", [False, True])
def test_explicit_manual_trial_reserved_after_pause_can_reset_backoff_without_enabling(tmp_path, monitor):
    repo = repository(tmp_path)
    job = create(repo, every(), monitor=monitor)
    with repo._mutation_scope() as (store, admission):
        target = store["jobs"][job["job_id"]]["rules"][0] if monitor else store["jobs"][job["job_id"]]
        target.update(consecutive_failures=2, backoff_until=4000)
        repo._write_store_unlocked(store, admission)
    paused = repo.pause_job(job["job_id"], expected_version=job["version"], now=1000)
    run = repo.reserve_manual_run(job["job_id"], rule_id="probe" if monitor else "", now=1100)
    claimed = repo.claim_run(run["run_id"], lease_seconds=60, now=1101)
    repo.mark_run_running(run["run_id"], claimed["claim_id"], now=1101)
    repo.finish_run(run["run_id"], claimed["claim_id"], SchedulerRunFinish(status="done", now=1102))
    current = repo.get_job(job["job_id"])
    assert current["status"] == "paused"
    target = current["rules"][0] if monitor else current
    assert target["consecutive_failures"] == target["backoff_until"] == 0
    assert target["next_run_at"] == (paused["rules"][0] if monitor else paused)["next_run_at"]
    if monitor:
        assert target["enabled"] is True
