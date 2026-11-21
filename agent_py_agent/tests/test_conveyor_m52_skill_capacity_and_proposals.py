import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning_proposals as proposals
from agent_py_agent.agent.capability.skill_learning_protection import learning_protection_for_agent
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision


def _host(tmp_path, jobs):
    return SimpleNamespace(
        config=SimpleNamespace(retention_scan_slice_ms=60_000),
        home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_skill_learning_dir=tmp_path / "learning"),
        conversation_store=SimpleNamespace(tasks=SimpleNamespace(unsettled_report=lambda: ([], []))),
        scheduler_repository=SimpleNamespace(list_jobs=lambda **_: (jobs, [])),
        subagents=SimpleNamespace(list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[])),
    )


def test_protection_reaches_1500_sources(tmp_path):
    jobs = [{"allowed_skills": ["owner:last"]} if number == 1499 else {} for number in range(1500)]
    first = _host(tmp_path, jobs)
    assert not learning_protection_for_agent(first)["protection_complete"]
    store = SkillLearningStore.for_home(first.home_paths)
    with store.state_lock():
        registry = store.load_registry()
        saved = registry.protection_scan
        # 进度按来源分别保存：监控清单只记自己的下标续点与已读前缀的引用投影。
        assert saved["schema"] == "skill-protection-progress.v2" and saved["sources"]["scheduler"]["after"] > 0
        store.save_registry(replace(registry, daily_calls=1))
    for _ in range(20):
        result = learning_protection_for_agent(_host(tmp_path, jobs))
        if result["protection_complete"]:
            break
    assert result["protection_complete"]
    assert result["protected_ids"] == ("owner:last",)
    assert store.load_registry().daily_calls == 1


@pytest.fixture
def proposal_store(tmp_path):
    store = SkillLearningStore(tmp_path / "learning", tmp_path / "skills")
    store.directory.mkdir()
    row = {
        "schema_version": "skill-learning-proposal.v1", "status": "pending", "revision": 1,
        "request": {"request_key": "sample"}, "decision": {"name": "sample"},
        "expected_revision": 0, "expected_sha256": "",
    }
    proposal_id = row["proposal_id"] = proposals._identity_id(row)
    proposals.write_text_atomic_beneath(store.directory, ("proposals", proposal_id + ".json"), json.dumps(row))
    bad_name = "learning-proposal-" + "a" * 32 + ".json"
    proposals.write_text_atomic_beneath(
        store.directory, ("proposals_overflow", bad_name), "{",
    )
    return store, row, bad_name


def test_exact_read_does_not_enumerate_proposal_directories(proposal_store, monkeypatch):
    store, row, _ = proposal_store
    proposal_id = row["proposal_id"]
    reads = []
    original = proposals._read_proposal

    def read(store, folder, requested_id):
        reads.append((folder, requested_id))
        return original(store, folder, requested_id)

    monkeypatch.setattr(proposals, "_read_proposal", read)
    assert proposals.learning_proposal(store, proposal_id) == row
    assert sorted(reads) == [("proposals", proposal_id), ("proposals_overflow", proposal_id)]


def test_proposal_pages_report_the_unreadable_row_and_keep_exact_resolution(proposal_store):
    store, row, bad_name = proposal_store
    found, errors, after = [], [], {}
    while after is not None:
        page, diagnostics, after = proposals.learning_proposal_page(store, after=after, limit=1)
        found.extend(page)
        errors.extend(diagnostics)
    assert found == [row]
    assert [(error.folder, error.file_name, error.error) for error in errors] == [
        ("proposals_overflow", bad_name, "JSONDecodeError"),
    ]
    resolution = proposals.resolve_learning_proposal(store, row["proposal_id"], 1, decision="reject")
    assert (resolution["status"], resolution["revision"]) == ("rejected", 2)
    assert (store.directory / "proposals_overflow" / bad_name).read_text() == "{"


def test_displacement_rechecks_reference_generation(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_publish

    jobs = []
    agent = _host(tmp_path, jobs)
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(decision("existing-method"), max_skills=1))
    previous = store.skill_path("existing-method").read_bytes()
    original = skill_learning_publish._registered_publication

    def introduce_reference(*args):
        registration = original(*args)
        jobs.append({"allowed_skills": ["owner:existing-method"]})
        return registration

    monkeypatch.setattr(skill_learning_publish, "_registered_publication", introduce_reference)
    with pytest.raises(SkillLearningGateError) as error:
        publish_learned_skill(store, PublishRequest(
            decision("new-method"), max_skills=1,
            protection_provider=lambda: learning_protection_for_agent(agent),
        ))
    assert error.value.detail["reason"] == "protection_generation_changed"
    assert store.skill_path("existing-method").read_bytes() == previous
    assert set(store.load_registry().skills) == {"existing-method"}
    assert not store.skill_path("new-method").exists()


def _scheduled_runtime_host(tmp_path, job_count):
    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        SchedulerRepository,
    )
    from agent_py_agent.tests.test_skill_learning_maintenance import _protection_runtime

    agent, agent_run, _ = _protection_runtime(tmp_path)
    scheduler = SchedulerRepository(tmp_path / "data/scheduler", owner_provider="local", owner_kind="main",
                                    owner_id="local/main")
    for number in range(job_count):
        scheduler.create_job(SchedulerJobCreateRequest(
            name=f"job-{number}", prompt="check", thread_id="thread_fixture",
            schedule={"kind": "every", "every_seconds": 90, "anchor_at": 1000, "timezone": "UTC"},
            skill_refs=[{"stable_id": f"owner:job-{number}", "content_sha256": "a" * 64}], now=1000,
        ))
    agent.home_paths = SimpleNamespace(owner_home_dir=tmp_path, owner_skill_learning_dir=tmp_path / "learning")
    agent.scheduler_repository = scheduler
    agent.subagents = SimpleNamespace(list_runs_report=lambda: SimpleNamespace(runs=[], load_errors=[]))
    # 生产条目与字节预算；墙钟放宽，使每片只受128条约束，结果与机器快慢无关。
    agent.config = SimpleNamespace(retention_scan_slice_entries=128, retention_scan_slice_bytes=8 * 1024 * 1024,
                                   retention_scan_slice_ms=60_000)
    return agent, agent_run


def _ordinary_activity(agent, agent_run, tick):
    """一次运行库心跳写入（落在WAL）与一次监控开工计数更新（原子替换账本文件），都不改变任何引用。"""
    from agent_py_agent.agent.scheduler.monitor_state import record_start

    with agent.runtime_db.transaction() as conn:
        conn.execute("UPDATE agent_runs SET updated_at=? WHERE agent_run_id=?", (5000 + tick, agent_run["agent_run_id"]))
    scheduler = agent.scheduler_repository
    with scheduler._mutation_scope() as (store, admission):
        job_id = sorted(store["jobs"])[0]
        store["jobs"][job_id] = record_start(store["jobs"][job_id], {}, 6000 + tick)
        scheduler._write_store_unlocked(store, admission)


def test_protection_completes_across_runtime_writes_and_scheduler_updates_between_calls(tmp_path):
    agent, agent_run = _scheduled_runtime_host(tmp_path, 200)
    for call in range(8):
        result = learning_protection_for_agent(agent)
        if result["protection_complete"]:
            break
        _ordinary_activity(agent, agent_run, call)
    else:
        pytest.fail("protection never completed while the owner stayed active")
    assert call > 0, "200 jobs must need more than one 128-entry call"
    assert set(result["protected_ids"]) == {"owner:held", *(f"owner:job-{number}" for number in range(200))}
    assert result.recheck()


def _settled_task_runs(repo, labels, *, first_created_at):
    """登记若干已确证退出的任务运行，引用只在任务运行元数据里（与 _protection_runtime 同形）；创建时间逐个递增。"""
    runs = []
    for offset, label in enumerate(labels):
        task = repo.create_task(owner_id="owner", thread_id="missing-thread", conversation_task_id=f"link-{label}")
        run = repo.create_task_run(task_id=task["task_id"], status="created",
                                   metadata={"allowed_skills": [f"owner:{label}"]})
        agent_run = repo.create_agent_run(task_run_id=run["task_run_id"], run_id=f"protection-{label}")
        attempt = repo.create_attempt(agent_run["agent_run_id"])
        repo.settle_agent_attempt(agent_run_id=agent_run["agent_run_id"], attempt_id=attempt["attempt_id"])
        with repo.transaction() as conn:
            conn.execute("UPDATE agent_attempts SET metadata_json=? WHERE attempt_id=?", (
                '{"executor":{"schema_version":"attempt-executor.v1","status":"exited","ended_at":1}}',
                attempt["attempt_id"],
            ))
            conn.execute("UPDATE task_runs SET created_at=? WHERE task_run_id=?",
                         (first_created_at + offset, run["task_run_id"]))
        runs.append(run)
    return runs


def test_reference_added_in_an_earlier_runtime_window_fails_the_recheck(tmp_path):
    agent, agent_run = _scheduled_runtime_host(tmp_path, 0)
    with agent.runtime_db.transaction() as conn:
        conn.execute("UPDATE task_runs SET created_at=100 WHERE task_run_id=?", (agent_run["task_run_id"],))
    runs = _settled_task_runs(agent.runtime_db, ["first", "second", "third"], first_created_at=101)
    # 每片两条：四组任务运行按创建时间分成 [held, first] 与 [second, third] 两个窗口。
    for _ in range(8):
        result = learning_protection_for_agent(agent, scan_limit=2)
        if result["protection_complete"]:
            break
    else:
        pytest.fail("protection over two runtime windows never completed")
    assert {"owner:held", "owner:first", "owner:second", "owner:third"} <= set(result["protected_ids"])
    # 证明之后，第一窗口里已读过的任务运行新增了引用：最后窗口逐组未变，但证明已不覆盖当前事实。
    with agent.runtime_db.transaction() as conn:
        conn.execute("UPDATE task_runs SET metadata_json=? WHERE task_run_id=?", (
            json.dumps({"allowed_skills": ["owner:first", "owner:late"]}), runs[0]["task_run_id"]))
    assert "owner:late" not in result["protected_ids"]
    assert not result.recheck()


def _append_job_before_cursor(scheduler, tick):
    """经真实创建路径新建一条带引用的 job（创建时间晚于已有 job），再换成排在所有已有 id 之前的合法 id。

    job_id 是随机值，约一半新建 job 的 id 都会排在续点之前；这里取确定的那一种。
    """
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest

    job, _ = scheduler.create_job(SchedulerJobCreateRequest(
        name=f"appended-{tick}", prompt="check", thread_id="thread_fixture",
        schedule={"kind": "every", "every_seconds": 90, "anchor_at": 1000, "timezone": "UTC"},
        skill_refs=[{"stable_id": f"owner:appended-{tick}", "content_sha256": "a" * 64}], now=2000 + tick,
    ))
    with scheduler._mutation_scope() as (store, admission):
        raw = store["jobs"].pop(job["job_id"])
        raw["job_id"] = f"job_{tick:032x}"
        store["jobs"][raw["job_id"]] = raw
        scheduler._write_store_unlocked(store, admission)


def test_scheduler_jobs_created_between_calls_continue_the_keyset(tmp_path):
    agent, agent_run = _scheduled_runtime_host(tmp_path, 200)
    appended = set()
    for call in range(8):
        result = learning_protection_for_agent(agent)
        if result["protection_complete"]:
            break
        _ordinary_activity(agent, agent_run, call)
        _append_job_before_cursor(agent.scheduler_repository, call)
        appended.add(f"owner:appended-{call}")
    else:
        pytest.fail("protection never completed while jobs were created between calls")
    assert call > 0, "200 jobs must need more than one 128-entry call"
    assert set(result["protected_ids"]) == {
        "owner:held", *(f"owner:job-{number}" for number in range(200)), *appended,
    }
    assert result.recheck()


def _discovered(home, jobs):
    host = _host(home.owner_home_dir, jobs)
    host.home_paths = home
    snapshot = learning_protection_for_agent(host)
    assert snapshot["protection_complete"] and snapshot.recheck()
    return snapshot


@pytest.mark.parametrize("source", ["scheduler", "runtime"])
@pytest.mark.parametrize("referenced", [True, False])
@pytest.mark.parametrize("path", ["restore", "usage_receipt", "cap_enforcement"])
def test_candidate_referenced_after_discovery_is_not_displaced(tmp_path, monkeypatch, path, referenced, source):
    from agent_py_agent.agent.capability import skill_learning_protection
    from agent_py_agent.agent.capability.skill_learning_maintenance import (
        _select_cap_victim_locked,
        control_learning,
        maintain_learning,
    )
    from agent_py_agent.agent.capability.skill_usage_ledger import record_skill_usage_event
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.tests.test_sm_w2_usage_strikes import _publish, _store_with_runtime

    home, _, store = _store_with_runtime(tmp_path)
    if path == "cap_enforcement":
        _publish(store, "alpha")
        _publish(store, "beta")
    else:
        _publish(store, "target")
        with store.state_lock():
            store._transition_archive_locked(store.load_registry(), "target")
        _publish(store, "victim")
    jobs = []
    if source == "runtime":
        agent, _ = _scheduled_runtime_host(tmp_path / "sources", 0)
        agent.home_paths = home
        earlier, *_ = _settled_task_runs(agent.runtime_db, ["first", "second", "third"], first_created_at=101)
        snapshot = _complete_protection(agent, scan_limit=2)
    else:
        snapshot = _discovered(home, jobs)
    registry = store.load_registry()
    managed = store.managed_records(registry, include_archived=False)
    victim = _select_cap_victim_locked(store, registry, managed, protected=set(snapshot["protected_ids"]))
    if referenced:
        if source == "runtime":
            with agent.runtime_db.transaction() as conn:
                conn.execute("UPDATE task_runs SET metadata_json=? WHERE task_run_id=?", (
                    json.dumps({"allowed_skills": [f"owner:{victim}"]}), earlier["task_run_id"],
                ))
        else:
            jobs.append({"allowed_skills": [f"owner:{victim}"]})

    if path == "restore":
        def restore():
            return control_learning(store, "target", operation="restore", max_skills=1,
                                    protected_ids=snapshot["protected_ids"],
                                    protection_complete=snapshot["protection_complete"])

        if referenced:
            with pytest.raises(SkillLearningGateError) as refused:
                restore()
            assert refused.value.detail["reason"] == "protection_generation_changed"
        else:
            restore()
    elif path == "usage_receipt":
        # 回执路径在恢复前自行取保护；这里交回发现时的快照，引用在其后出现。
        monkeypatch.setattr(skill_learning_protection, "learning_protection_for_agent", lambda _agent: snapshot)
        target = store.load_registry().skills["target"]
        agent = SimpleNamespace(skill_learning_store=store, config=AgentConfig(self_learning_max_skills=1))
        record_skill_usage_event(
            agent, event="manual_get", skill_id="owner:target", source="learned", task_id="after-discovery",
            body_chars=240, content_sha256=target.sha256, incarnation_id=target.maintenance["incarnation_id"],
            activation_id=target.maintenance["activation_id"],
        )
    else:
        result = maintain_learning(
            store, mode="autonomous", max_skills=1, batch_ms=60_000, protected_ids=snapshot["protected_ids"],
            protection_complete=snapshot["protection_complete"],
            usage_loader=lambda _store: {"coverage": {"lifetime_summary": "complete"}, "by_skill": {}},
        )
        assert result["backlog"] is referenced

    states = {name: record.maintenance.get("state") for name, record in store.load_registry().skills.items()}
    assert (states[victim] == "archived") is not referenced
    if path != "cap_enforcement":
        assert (states["target"] == "archived") is referenced


def _complete_protection(agent, *, scan_limit=128):
    for index in range(48):
        snapshot = learning_protection_for_agent(agent, scan_limit=scan_limit)
        if snapshot["protection_complete"]:
            assert index > 0, "the source must span multiple scans"
            return snapshot
    pytest.fail("protection discovery did not finish")


def _publish_at_capacity(store, snapshot):
    return publish_learned_skill(store, PublishRequest(
        decision("replacement"), max_skills=1, protection_provider=lambda: snapshot,
    ))


def test_quiet_runtime_windows_authorize_displacement(tmp_path):
    agent, _ = _scheduled_runtime_host(tmp_path, 0)
    _settled_task_runs(agent.runtime_db, [f"runtime-{i}" for i in range(129)], first_created_at=101)
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(decision("victim"), max_skills=1))
    snapshot = _complete_protection(agent)
    assert len(snapshot["protected_ids"]) == 130
    _publish_at_capacity(store, snapshot)
    assert store.load_registry().skills["victim"].maintenance["state"] == "archived"


def test_large_proposal_windows_authorize_displacement(tmp_path, monkeypatch):
    agent = _host(tmp_path, [])
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(decision("victim"), max_skills=1))
    body = decision().body + "\n" + "Check the resulting CSV files.\n" * 18_000
    for i in range(16):
        draft = replace(decision(f"proposal-{i}"), body=body)
        proposals.propose_learning(store, PublishRequest(draft, {"request_key": f"large-{i}"},
                                                           expected_sha256="a" * 64))
    assert sum(path.stat().st_size for path in (store.directory / "proposals").glob("*.json")) > 8 * 1024 * 1024
    snapshot = _complete_protection(agent)
    assert set(snapshot["protected_ids"]) == {f"owner:proposal-{i}" for i in range(16)}

    def no_proposal_body_read(*args, **kwargs):
        pytest.fail("admission reopened proposal bodies")

    monkeypatch.setattr(proposals, "learning_proposal_page", no_proposal_body_read)
    _publish_at_capacity(store, snapshot)
    assert store.load_registry().skills["victim"].maintenance["state"] == "archived"


def _indexed_children(tmp_path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    agent = _host(tmp_path, [])
    agent.subagents = SubAgentManager(tmp_path / "runs")
    children = [agent.subagents.create_run(goal="Check output", attributes={"skill_refs": [
        {"stable_id": f"owner:child-{i}"},
    ]}) for i in range(3)]
    return agent, sorted(children, key=lambda child: child.id)


def test_indexed_children_use_canonical_id_and_authorize_displacement(tmp_path):
    agent, children = _indexed_children(tmp_path)
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(decision("victim"), max_skills=1))
    snapshot = _complete_protection(agent, scan_limit=2)
    assert set(snapshot["protected_ids"]) == {f"owner:child-{i}" for i in range(len(children))}
    _publish_at_capacity(store, snapshot)
    assert store.load_registry().skills["victim"].maintenance["state"] == "archived"



def test_earlier_runtime_change_during_discovery_restarts_the_prefix(tmp_path):
    agent, _ = _scheduled_runtime_host(tmp_path, 0)
    earlier, *_ = _settled_task_runs(agent.runtime_db, ["first", "second", "third"], first_created_at=101)
    prior = learning_protection_for_agent(agent, scan_limit=2)
    assert not prior["protection_complete"]
    assert "owner:first" in prior["protected_ids"]
    with agent.runtime_db.transaction() as conn:
        conn.execute("UPDATE task_runs SET metadata_json=? WHERE task_run_id=?", (
            json.dumps({"allowed_skills": ["owner:victim"]}), earlier["task_run_id"],
        ))
    result = _complete_protection(agent, scan_limit=2)
    assert result.recheck()
    assert "owner:victim" in result["protected_ids"]
    assert "owner:first" not in result["protected_ids"]


def test_new_proposal_after_multiwindow_discovery_blocks_displacement(tmp_path):
    agent = _host(tmp_path, [])
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(decision("victim"), max_skills=1))
    for i in range(3):
        proposals.propose_learning(store, PublishRequest(decision(f"draft-{i}"), expected_sha256="a" * 64))

    snapshot = _complete_protection(agent, scan_limit=2)
    assert snapshot.recheck()
    proposals.propose_learning(store, PublishRequest(decision("victim"), expected_sha256="a" * 64))
    with pytest.raises(SkillLearningGateError) as error:
        _publish_at_capacity(store, snapshot)
    assert error.value.detail["reason"] == "protection_generation_changed"
    refreshed = _complete_protection(agent, scan_limit=2)
    assert refreshed.recheck() and "owner:victim" in refreshed["protected_ids"]
    assert store.load_registry().skills["victim"].maintenance["state"] != "archived"


def test_earlier_child_reference_change_invalidates_completed_proof(tmp_path):
    agent, children = _indexed_children(tmp_path)
    store = SkillLearningStore.for_home(agent.home_paths)
    publish_learned_skill(store, PublishRequest(decision("victim"), max_skills=1))
    snapshot = _complete_protection(agent, scan_limit=2)
    first = children[0]
    first.attributes["heartbeat_probe"] = 1
    agent.subagents.persistence.save(first)
    assert snapshot.recheck()
    first.attributes["skill_refs"].append({"stable_id": "owner:victim"})
    agent.subagents.persistence.save(first)
    with pytest.raises(SkillLearningGateError) as error:
        _publish_at_capacity(store, snapshot)
    assert error.value.detail["reason"] == "protection_generation_changed"
    refreshed = _complete_protection(agent, scan_limit=2)
    assert refreshed.recheck() and "owner:victim" in refreshed["protected_ids"]


@pytest.mark.parametrize("failure", ["missing", "mismatched_id"])
def test_indexed_child_page_cannot_certify_unreadable_or_mismatched_rows(tmp_path, monkeypatch, failure):
    agent, children = _indexed_children(tmp_path)
    persistence = agent.subagents.persistence
    first = children[0]
    if failure == "missing":
        (persistence.workspace / first.id / "task.json").unlink()
    else:
        original = persistence.list_runs_by_ids_report

        def wrong_identity(ids):
            report = original(ids)
            for child in report.runs:
                if child.id == first.id:
                    child.id = "unexpected-child"
            return report

        monkeypatch.setattr(persistence, "list_runs_by_ids_report", wrong_identity)
    result = learning_protection_for_agent(agent)
    assert not result["protection_complete"]
    assert not result.recheck()
