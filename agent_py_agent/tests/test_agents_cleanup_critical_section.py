"""review32 的业务合同：合成 owner，真实锁/仓库，异步交错不在持锁方等待写方。"""
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import PersonaMutationRequest
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.memory_store import agents_migration as migration
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.tests.test_agents_phase0_guard import _EXCLUDED_CHAT, _curator, _runtime
from agent_py_agent.tests.test_memory_curator_v2 import _StaticStructuredBackend, _valid_output

RULE = "输出先说明结果，再给出验证依据。"


def _setup(tmp_path):
    service, conversations, threads = _runtime(tmp_path)
    repo = service.persona
    home = SimpleNamespace(owner_home_dir=repo.owner_home, owner_agents_md=repo.path_for("agents"),
        owner_memory_candidates_jsonl=service.candidates.path,
        owner_memory_policy_json=repo.owner_home / "memory_policy.json")
    return service, conversations, threads, home


def _seed(repo, text=RULE):
    return repo.mutate(PersonaMutationRequest(target="agents", action="add", content=text,
        source="memory-promotion:synthetic:contract"))


def _apply(service, request, store=None):
    repo = service.persona
    home = SimpleNamespace(owner_home_dir=repo.owner_home, owner_agents_md=repo.path_for("agents"),
        owner_memory_candidates_jsonl=service.candidates.path,
        owner_memory_policy_json=repo.owner_home / "memory_policy.json")
    return migration.apply_agents_cleanup(home_paths=home, persona=service.persona,
        candidate_service=service.candidates, request=request, curator_state_store=store)


# LLM: 历史超限正文是迁移输入，不能再通过当前新增硬帽构造；不修改生产限制或迁移实现。
# 函数用途: 写入500条旧版自动晋升记录及匹配快照，保持原规模和归属判定输入。
def _seed_legacy_large_agents(repo):
    from hashlib import sha256

    from agent_py_agent.agent.capability.persona_repository import persona_entry_id

    text, records = "# AGENTS\n\n", []
    for i in range(500):
        content = f"规模约定{i:03}：" + "审核证据后再交付。" * 12
        text += f"- {content}\n"
        digest = sha256(text.encode()).hexdigest()
        backup = repo._write_version_snapshot("agents", i + 1, digest, text)
        records.append({"target": "agents", "action": "add", "version": i + 1,
            "entry_id": persona_entry_id("agents", content), "sha256": digest,
            "source": "memory-promotion:synthetic:contract",
            "backup_ref": str(backup.relative_to(repo.owner_home))})
    repo.path_for("agents").write_text(text)
    json_io.append_private_jsonl_records(repo.versions_path, records)
    assert len(text) > 20_000
    assert len(repo.list_entries("agents")["entries"]) == 500


# LLM: 在真实锁/仓库上迁移历史500条超限正文，保留全阶段持锁、单批删除及锁序断言。
# 函数用途: 验证大规模清理不会放开临界区或与忙碌curator重叠。
def test_500_entries_single_critical_section_and_busy_curator_cost(tmp_path, monkeypatch):
    service, conversations, _, home = _setup(tmp_path)
    repo = service.persona
    _seed_legacy_large_agents(repo)
    request = migration.AgentsCleanupRequest("scale-500", ("excluded-scale",),
        plan_digest=migration.plan_agents_cleanup(persona=repo).plan_digest)
    store = MemoryCuratorStateStore(repo.owner_home / "memory/curator/state.json")
    store.acquire(reason="admin", config_revision="synthetic", lease_seconds=300)
    with pytest.raises(migration.AgentsCleanupError, match="AGENTS_CLEANUP_CURATOR_BUSY"):
        _apply(service, request, store)
    # 无忙租约的独立隔离 store；不伪造在途 curator 的完成事实。
    store = MemoryCuratorStateStore(tmp_path / "free/state.json")
    active, stages, batches, edges, acquisitions = [], [], [], [], []
    original_lock = json_io._locked_json_path

    @contextmanager
    def trace(path, *, blocking=True):
        with original_lock(path, blocking=blocking):
            edges.extend((old, path) for old in active)
            active.append(path)
            acquisitions.append(path)
            try:
                yield
            finally:
                active.remove(path)

    monkeypatch.setattr(json_io, "_locked_json_path", trace)
    for name in ("_plan_agents_cleanup_from_report", "_ensure_snapshot", "_archive_planned", "_remove_batch",
                 "_append_conversations", "_expire_thread_snapshots", "_write_record"):
        original = getattr(migration, name)

        def probe(*args, _name=name, _original=original, **kwargs):
            stages.append((_name, repo.path_for("agents") in active))
            if _name == "_remove_batch":
                batches.append(len(args[1]))
            return _original(*args, **kwargs)

        monkeypatch.setattr(migration, name, probe)
    result = _apply(service, request, store)
    assert result["removed_count"] == 500
    assert batches == [500], batches
    assert stages and all(held for _, held in stages), stages
    assert "_plan_agents_cleanup_from_report" in {name for name, _ in stages}
    assert acquisitions.count(repo.path_for("agents")) == 1
    assert (home.owner_memory_policy_json, repo.path_for("agents")) in edges
    assert not any(b == home.owner_memory_policy_json for a, b in edges if a == repo.path_for("agents"))


USER_WRITER = '''
import json,sys
from pathlib import Path
from agent_py_agent.agent.capability.persona_repository import PersonaRepository,PersonaMutationRequest
p=Path(sys.argv[1]); ready=Path(sys.argv[2]); text=sys.argv[3]
r=PersonaRepository(owner_home=p,agents_path=p/'AGENTS.md',soul_path=p/'SOUL.md',user_path=p/'USER.md')
ready.write_text('ready')
rows=r.list_entries('agents')['entries']
for row in rows:
    if row['content']==text:
        try: r.mutate(PersonaMutationRequest(target='agents',action='remove',entry_id=row['entry_id']))
        except Exception as e:
            from agent_py_agent.agent.capability.persona_repository import PersonaEntryNotFoundError
            if not isinstance(e,PersonaEntryNotFoundError): raise
r.mutate(PersonaMutationRequest(target='agents',action='add',content=text))
print(json.dumps({'last_source':r.history('agents')[-1]['source']}))
'''


def _wait_writer_ready(child, ready):
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert ready.exists() and child.poll() is None


# LLM: 拦截实际锁内重判定函数，保留真实子进程写入和所有持锁断言。
# 函数用途: 在迁移已持锁时发起并发用户改写，验证用户内容不会被迁移吞掉。
def _resume_probes(state, monkeypatch):
    repo, ready = state.repo, state.ready
    original = migration._plan_agents_cleanup_from_report
    original_lock = json_io._locked_json_path
    original_tail = migration.versions_ledger_tail

    @contextmanager
    def trace(path, *, blocking=True):
        with original_lock(path, blocking=blocking):
            state.held.append(path)
            try:
                yield
            finally:
                state.held.remove(path)

    def reclassify(*args, **kwargs):
        plan = original(*args, **kwargs)
        assert repo.path_for("agents") in state.held
        assert len(plan.removable) == 1
        state.children.append(subprocess.Popen([sys.executable, "-c", USER_WRITER,
            str(repo.owner_home), str(ready), RULE],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        _wait_writer_ready(state.children[-1], ready)
        return plan

    monkeypatch.setattr(json_io, "_locked_json_path", trace)
    monkeypatch.setattr(migration, "_plan_agents_cleanup_from_report", reclassify)

    def tail(path):
        state.tail_reads.append(repo.path_for("agents") in state.held)
        return original_tail(path)

    monkeypatch.setattr(migration, "versions_ledger_tail", tail)
    return lambda **kwargs: original(kwargs["persona"], kwargs["persona"].list_entries("agents"))


def test_resume_reclassification_and_tail_must_share_persona_lock(tmp_path, monkeypatch):
    service, _, _, _ = _setup(tmp_path)
    repo = service.persona
    _seed(repo)
    request = migration.AgentsCleanupRequest("resume-aba",
        plan_digest=migration.plan_agents_cleanup(persona=repo).plan_digest)
    with monkeypatch.context() as patch:
        patch.setattr(migration, "_remove_batch", lambda *args: (_ for _ in ()).throw(RuntimeError("stop")))
        with pytest.raises(RuntimeError, match="stop"):
            _apply(service, request)
    state = SimpleNamespace(repo=repo, ready=tmp_path / "writer-ready", held=[], children=[], tail_reads=[])
    original = _resume_probes(state, monkeypatch)
    try:
        result = _apply(service, migration.AgentsCleanupRequest("resume-aba"))
    finally:
        outputs = [child.communicate(timeout=10) for child in state.children]
    assert result["status"] == "completed"
    assert state.children and all(child.returncode == 0 for child in state.children), outputs
    assert state.tail_reads and all(state.tail_reads), state.tail_reads
    assert RULE in repo.path_for("agents").read_text()
    assert repo.history("agents")[-1]["source"] == "agent_tool"
    assert original(persona=repo).removable == ()


@pytest.mark.parametrize("count", [1, 40])
def test_user_readd_waits_for_whole_delete_and_survives(tmp_path, monkeypatch, count):
    service, _, _, home = _setup(tmp_path)
    for i in range(count):
        _seed(service.persona, RULE if i == count - 1 else f"合成自动约定{i}。")
    request = migration.AgentsCleanupRequest("user-readd",
        plan_digest=migration.plan_agents_cleanup(persona=service.persona).plan_digest)
    children = []
    original = migration._remove_batch

    def delete(*args):
        children.append(subprocess.Popen([sys.executable, "-c", USER_WRITER,
            str(service.persona.owner_home), str(tmp_path / "ready"), RULE],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert (tmp_path / "ready").exists() and children[-1].poll() is None
        return original(*args)

    monkeypatch.setattr(migration, "_remove_batch", delete)
    try:
        result = _apply(service, request)
    finally:
        outputs = [child.communicate(timeout=10) for child in children]
    assert result["removed_count"] == count
    assert len(children) == 1 and children[0].returncode == 0, outputs
    assert RULE in service.persona.path_for("agents").read_text()
    assert service.persona.history("agents")[-1]["source"] == "agent_tool"
    assert migration.plan_agents_cleanup(persona=service.persona).removable == ()


def _curator_with_input(tmp_path):
    service, conversations, threads, _ = _setup(tmp_path)
    _seed(service.persona)
    thread_id = threads[_EXCLUDED_CHAT].thread_id
    message = conversations.messages.append({"thread_id": thread_id, "role": "user",
        "content": "个人电脑使用 macOS。", "now": 11.0})
    curator = _curator(tmp_path, conversations, service.candidates)
    curator.timezone_name = "UTC"
    backend = _StaticStructuredBackend(_valid_output(thread_id, message.message_id, message.content))
    curator.backend = backend
    curator.dependencies = replace(curator.dependencies, backend=backend)
    return service, curator


def _assert_expired_guard(curator, lease, monkeypatch):
    from agent_py_agent.agent.memory_store import curator_state
    from agent_py_agent.agent.memory_store.curator_lease import renew_curator_lease
    future = datetime.fromisoformat(lease["expires_at"]) + timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    with monkeypatch.context() as patch:
        patch.setattr(curator_state, "datetime", Clock)
        assert curator.state_store.acquire(reason="migration", config_revision="synthetic",
            lease_seconds=300, now=future) is None
        assert curator.committer.recover_and_acquire(curator_state.CuratorLeaseRequest(
            "admin", "synthetic", 300, future)) is None
        renew_curator_lease(curator.state_store, lease["lease_id"], 300)
        assert curator.state_store.acquire(reason="migration", config_revision="synthetic",
            lease_seconds=300, now=future) is None
        assert curator.committer.recover_and_acquire(curator_state.CuratorLeaseRequest(
            "admin", "synthetic", 300, future)) is None


@pytest.mark.parametrize("mode", ["busy", "expired", "callback_error"])
def test_curator_postcommit_promotion_must_not_overlap_migration(tmp_path, monkeypatch, mode):
    service, curator = _curator_with_input(tmp_path)
    entered, release = threading.Event(), threading.Event()
    result = {}

    def promotion(cid):
        result["lease"] = curator.state_store.load().active_lease
        entered.set()
        assert release.wait(10)
        if mode == "callback_error":
            raise RuntimeError("synthetic promotion failure")
        return service.promote(cid, automatic=True)

    curator.promotion_callback = promotion
    worker = threading.Thread(target=lambda: result.update(run=curator.run(reason="admin", force=True)))
    worker.start()
    try:
        assert entered.wait(5)
        assert result["lease"], result
        if mode == "expired":
            _assert_expired_guard(curator, result["lease"], monkeypatch)
        request = migration.AgentsCleanupRequest("postcommit",
            plan_digest=migration.plan_agents_cleanup(persona=service.persona).plan_digest)
        with pytest.raises(migration.AgentsCleanupError, match="AGENTS_CLEANUP_CURATOR_BUSY"):
            _apply(service, request, curator.state_store)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert result["run"].status == "succeeded"
    state = curator.state_store.load()
    assert state.active_lease == {}
    assert state.processed_count == 1
    assert state.last_committed_run_id == result["run"].run_id
    assert curator.state_store.acquire(reason="migration", config_revision="synthetic", lease_seconds=300)


def test_backlog_promotion_also_holds_curator_lease(tmp_path, monkeypatch):
    service, conversations, _, _ = _setup(tmp_path)
    curator = _curator(tmp_path, conversations, service.candidates)
    # 只隔离积压候选选择；候选only不再触发工作，先由原显式pending入口启动合法批次，再核真实租约和提交链。
    monkeypatch.setattr(curator, "_pending_promotable_candidate_ids", lambda **kwargs: ["backlog"])
    seen = []

    def promote(ids):
        seen.append(curator.state_store.load().active_lease)
        return 1, (), ()

    monkeypatch.setattr(curator, "_promote_eligible", promote)
    curator.request("task_complete")
    result = curator.run_if_due(now=datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    assert result.status == "succeeded"
    assert seen and all(seen), seen
    assert curator.state_store.load().active_lease == {}
    assert result.reason == "task_complete"
    assert curator.state_store.load().pending_reasons == []


def _metadata_test_change(actual, field):
    lease = actual["active_lease"]
    changes = {"phase": ("active_lease", "phase", "interrupted"),
        "candidate_count": ("root", "candidate_count", "changed"),
        "per_thread_cursors": ("root", "per_thread_cursors", "changed"),
        "bad_time": ("active_lease", "expires_at", "invalid"),
        "heartbeat_past_expiry": ("active_lease", "heartbeat_at", (datetime.fromisoformat(lease["expires_at"]) + timedelta(seconds=1)).isoformat()),
        "time_backwards": ("active_lease", "expires_at", (datetime.fromisoformat(lease["expires_at"]) - timedelta(seconds=1)).isoformat())}
    changes.update({key: ("active_lease", key, "different") for key in ("lease_id", "run_id", "pid")})
    changes.update({key: ("active_lease", key, (datetime.fromisoformat(lease[key]) + timedelta(seconds=30)).isoformat())
        for key in ("heartbeat_at", "expires_at")})
    change = changes.get(field)
    if change:
        scope, key, value = change
        target = actual if scope == "root" else lease
        target[key] = value


@pytest.mark.parametrize("field", ["none", "phase", "heartbeat_at", "expires_at", "lease_id", "run_id", "pid", "candidate_count", "per_thread_cursors", "wrong_file", "bad_time", "time_backwards", "heartbeat_past_expiry"])
def test_recovery_accepts_only_same_lease_metadata(tmp_path, field):
    from agent_py_agent.agent.memory_store.curator_commit import _lease_metadata_only_change
    stamp = datetime.now(timezone.utc).isoformat()
    expected = {"candidate_count": 1, "per_thread_cursors": {"thread": "message"}, "active_lease": {
        "lease_id": "lease", "run_id": "run", "pid": 123,
        "phase": "executing", "heartbeat_at": stamp,
        "expires_at": (datetime.fromisoformat(stamp) + timedelta(minutes=5)).isoformat()}}
    actual = json.loads(json.dumps(expected))
    valid = field in {"none", "phase", "heartbeat_at", "expires_at"}
    _metadata_test_change(actual, field)
    path = tmp_path / "state.json"
    target = tmp_path / "candidate.json" if field == "wrong_file" else path
    assert _lease_metadata_only_change(target, path, json.dumps(expected), json.dumps(actual)) is valid


def test_failure_audit_interruption_also_retires_execution(tmp_path, monkeypatch):
    _, curator = _curator_with_input(tmp_path)

    def fail(*args):
        raise RuntimeError("ordinary model failure")

    def interrupt(*args):
        raise KeyboardInterrupt("failure audit stop")

    monkeypatch.setattr(curator, "_execute", fail)
    monkeypatch.setattr(curator.run_log, "append", interrupt)
    with pytest.raises(KeyboardInterrupt, match="failure audit stop"):
        curator.run(reason="admin")
    from agent_py_agent.agent.memory_store.curator_state import execution_lease_protected
    assert not execution_lease_protected(curator.state_store.load().active_lease)


def _wait_heartbeat_change(store, first):
    deadline = threading.Event()
    for _ in range(100):
        deadline.wait(0.01)
        if store.load().active_lease["heartbeat_at"] != first:
            return
    pytest.fail("periodic heartbeat did not renew")


@pytest.mark.parametrize("stage", ["_execute", "_post_commit_report"])
@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_caught_baseexception_cannot_leave_execution_protected_forever(tmp_path, monkeypatch, stage, error):
    service, curator = _curator_with_input(tmp_path)

    def interrupt(*args):
        raise error("caught synthetic stop")

    monkeypatch.setattr(curator, stage, interrupt)
    with pytest.raises(error, match="caught synthetic stop"):
        curator.run(reason="admin")
    from agent_py_agent.agent.memory_store.curator_state import execution_lease_protected
    lease = curator.state_store.load().active_lease
    assert not execution_lease_protected(lease), lease
    # 原崩溃恢复合同仍保留短期 busy；已经退出的执行在期限后不再永久阻塞。
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert curator.state_store.acquire(reason="migration", config_revision="synthetic",
        lease_seconds=300, now=future) is not None


def test_periodic_heartbeat_and_wrong_lease_do_not_steal(tmp_path):
    from agent_py_agent.agent.memory_store.curator_lease import (
        curator_lease_heartbeat,
        release_curator_lease,
        renew_curator_lease,
    )
    from agent_py_agent.agent.memory_store.curator_state import CuratorLeaseLostError
    store = MemoryCuratorStateStore(tmp_path / "state.json")
    _, lease = store.acquire(reason="admin", config_revision="test", lease_seconds=30)
    with curator_lease_heartbeat(store, lease["lease_id"], 0.06):
        first = store.load().active_lease["heartbeat_at"]
        _wait_heartbeat_change(store, first)
        assert store.load().active_lease["heartbeat_at"] != first
    before = store.path.read_bytes()
    with pytest.raises(CuratorLeaseLostError):
        renew_curator_lease(store, "other-lease", 300)
    release_curator_lease(store, "other-lease")
    assert store.path.read_bytes() == before


def test_inflight_heartbeat_error_is_checked_after_join(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import curator_lease
    store = MemoryCuratorStateStore(tmp_path / "state.json")
    _, lease = store.acquire(reason="admin", config_revision="test", lease_seconds=30)
    heartbeat = SimpleNamespace(error=None, stop=threading.Event())
    heartbeat.thread = SimpleNamespace(start=lambda: None,
        join=lambda: setattr(heartbeat, "error", RuntimeError("inflight heartbeat failed")))
    monkeypatch.setattr(curator_lease, "_LeaseHeartbeat", lambda *args: heartbeat)
    with pytest.raises(RuntimeError, match="inflight heartbeat failed"):
        with curator_lease.curator_lease_heartbeat(store, lease["lease_id"], 30):
            pass
