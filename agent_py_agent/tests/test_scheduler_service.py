# LLM: 测试使用隔离目录内真实owner账本/Wake/CAS，不请求模型或外发；同job互斥不扩大为owner串行。
# 模块用途: 验证定时任务准入、恢复、并发与原任务绑定合同。
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.recovery import RecoveryAction
from agent_py_agent.agent.conversation.capability_selection_state import TaskCapabilitySelection
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
)
from agent_py_agent.agent.scheduler.service import SchedulerService


def _setup(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-1",
            "now": 900.0,
        }
    )
    repository = SchedulerRepository(
        tmp_path / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="user-1",
    )
    return store, thread, repository


def _create(repository, thread_id: str, *, name: str, skill_refs=None):
    return repository.create_job(
        SchedulerJobCreateRequest(
            name=name,
            prompt=f"do {name}",
            thread_id=thread_id,
            source_task_id="",
            schedule={
                "kind": "every",
                "every_seconds": 600,
                "anchor_at": 1_000,
                "timezone": "UTC",
            },
            misfire_grace_seconds=60,
            skill_refs=skill_refs or [],
            source_request_id=name,
            now=900,
        )
    )[0]


def test_service_enqueues_claims_and_finishes_in_the_same_thread(tmp_path) -> None:
    store, thread, repository = _setup(tmp_path)
    first = _create(repository, thread.thread_id, name="first")
    second = _create(repository, thread.thread_id, name="second")
    service = SchedulerService(repository, conversation_store=store)

    wake_ids = service.enqueue_ready_runs(now=1_000)
    signals = store.wakes.pending()
    assert len(wake_ids) == 2
    assert len(signals) == 2
    assert {signal.thread_id for signal in signals} == {thread.thread_id}
    assert {signal.metadata["scheduler_job_id"] for signal in signals} == {
        first["job_id"],
        second["job_id"],
    }

    result = service.claim_wake(signals[0], lease_seconds=60, now=1_001)
    assert result.status == "claimed"
    assert result.claim is not None
    link = store.tasks.load(result.claim.run_id)
    assert link is not None, "真实定时运行准入必须先建立原会话任务链接"
    assert (link.task_id, link.thread_id, link.status) == (
        result.claim.run_id, thread.thread_id, "active",
    )
    assert link.goal == result.claim.run["prompt"]
    assert link.skill_snapshot_refs == () and link.capability_selection is None
    terminal = service.finish(
        result.claim,
        status="done",
        response="complete",
        delivery_status="sent",
        now=1_002,
    )
    assert terminal is not None
    history, errors = repository.history(job_id=str(terminal["job_id"]))
    assert errors == []
    assert history[0]["response"] == "complete"


def test_service_reuses_deduped_wake_after_restart(tmp_path) -> None:
    store, thread, repository = _setup(tmp_path)
    job = _create(repository, thread.thread_id, name="restart")
    repository.reserve_due_runs(now=1_000)

    restarted_repository = SchedulerRepository(
        repository.root,
        owner_provider="feishu",
        owner_kind="user",
        owner_id="user-1",
    )
    restarted = SchedulerService(restarted_repository, conversation_store=store)
    first_ids = restarted.enqueue_ready_runs(now=1_001)
    second_ids = restarted.enqueue_ready_runs(now=1_002)
    assert first_ids == second_ids
    assert len(store.wakes.pending()) == 1
    assert store.wakes.pending()[0].metadata["scheduler_job_id"] == job["job_id"]


def test_stale_skill_snapshot_fails_before_model_execution(tmp_path) -> None:
    store, thread, repository = _setup(tmp_path)
    job = _create(
        repository,
        thread.thread_id,
        name="skill",
        skill_refs=[{"stable_id": "shared:skill", "content_sha256": "a" * 64}],
    )
    snapshot = SimpleNamespace(resolve_reference=lambda _reference: SimpleNamespace(content_sha256="b" * 64))
    service = SchedulerService(
        repository,
        conversation_store=store,
        skill_snapshot_provider=lambda: snapshot,
    )
    assert service.enqueue_ready_runs(now=1_000) == []
    assert store.wakes.pending() == []
    history, _errors = repository.history(job_id=str(job["job_id"]))
    assert history[0]["status"] == "failed"
    assert history[0]["error_code"] == "SCHEDULER_SKILL_SNAPSHOT_STALE"


# LLM: 通过真实 repository 和 wake 生产者准备准入，不预建 TaskLink；仅隔离目录有文件写入。
# 函数用途: 给领取边界测试提供同一准确 run/wake，避免夹具提前掩盖首轮缺链接的问题。
def _queued_execution(tmp_path):
    store, thread, repository = _setup(tmp_path)
    _create(repository, thread.thread_id, name="binding")
    service = SchedulerService(repository, conversation_store=store)
    service.enqueue_ready_runs(now=1_000)
    signal = store.wakes.pending()[0]
    return store, thread, repository, service, signal


def test_claim_preserves_existing_task_pins_and_selection_across_reclaim(tmp_path) -> None:
    store, thread, repository, service, signal = _queued_execution(tmp_path)
    task_id = signal.root_task_id
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": task_id, "goal": "已有目标"},
        capability_selection=TaskCapabilitySelection.pending(),
    )
    reference = {
        "kind": "capability_package", "stable_id": "capability:fixture", "name": "fixture",
        "source": "capability_package", "package_id": "fixture",
        "content_sha256": "a" * 64, "activation_id": "b" * 64,
    }
    store.tasks.pin_skill_reference(task_id=task_id, thread_id=thread.thread_id, reference=reference)
    path = store.storage.task_path(task_id)
    before = path.read_bytes()

    first = service.claim_wake(signal, lease_seconds=60, now=1_001)
    assert first.status == "claimed" and first.claim is not None
    assert repository.get_active_run(task_id)["started_at"] == 0
    repository.mark_run_running(task_id, first.claim.claim_id, now=1_001)
    assert service.release(first.claim, now=1_002)
    second = service.claim_wake(signal, lease_seconds=60, now=1_003)

    assert second.status == "claimed" and second.claim is not None
    assert second.claim.claim_id != first.claim.claim_id
    assert path.read_bytes() == before
    assert store.tasks.load(task_id).skill_snapshot_refs == (reference,)
    assert repository.get_active_run(task_id)["started_at"] == 1_001


@pytest.mark.parametrize("damage", ["json", "task", "thread", "status", "missing_started", "missing_thread"])
def test_claim_settles_invalid_task_binding_without_rebuilding_it(tmp_path, monkeypatch, damage) -> None:
    store, thread, repository, service, signal = _queued_execution(tmp_path)
    task_id = signal.root_task_id
    path = store.storage.task_path(task_id)
    if damage == "missing_started":
        claimed = repository.claim_run(task_id, lease_seconds=60, now=1_000)
        repository.mark_run_running(task_id, str(claimed["claim_id"]), now=1_000)
        repository.release_run_claim(task_id, str(claimed["claim_id"]), now=1_001)
    elif damage == "missing_thread":
        store.storage.thread_path(thread.thread_id).unlink()
    else:
        store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "原任务"})
        payload = json.loads(path.read_text(encoding="utf-8"))
        if damage == "task":
            payload["task_id"] = "other-task"
        elif damage == "thread":
            payload["thread_id"] = "other-thread"
        elif damage == "status":
            payload["status"] = "unrecognized-status"
        path.write_text("{" if damage == "json" else json.dumps(payload), encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    settled_claims = []
    finish_run = repository.finish_run

    # LLM: 终态 payload 按原协议清理 claim_id；在原 CAS 入口观察真实领取身份，不给历史加第二个字段。
    # 函数用途: 确认失败实际结算当前持有的 claim，再验证落盘历史已释放租约。
    def record_finish(run_id, claim_id, result):
        assert repository.get_active_run(run_id)["claim_id"] == claim_id
        settled_claims.append((run_id, claim_id))
        return finish_run(run_id, claim_id, result)

    monkeypatch.setattr(repository, "finish_run", record_finish)

    result = service.claim_wake(signal, lease_seconds=60, now=1_002)

    assert result.status == "stale" and result.claim is None
    assert (path.read_bytes() if path.exists() else None) == before
    assert repository.get_active_run(task_id) is None
    history, errors = repository.history()
    assert not errors and len(history) == 1
    assert history[0]["status"] == "failed"
    assert history[0]["error_code"] == "SCHEDULER_TASK_BINDING_INVALID"
    assert settled_claims[0][0] == task_id and settled_claims[0][1]
    assert len(settled_claims) == 1
    assert history[0]["run_id"] == task_id and history[0]["claim_id"] == ""
    contract = error_contract(history[0]["error_code"])
    assert (contract.code, contract.category, contract.retryable, contract.recommended_action) == (
        history[0]["error_code"], "state", False, RecoveryAction.REPORT_BLOCKER.value,
    )


@pytest.mark.parametrize("status, expected", [("completed", "done"), ("interrupted", "cancelled"), ("failed", "failed")])
def test_claim_settles_terminal_task_without_reopening_it(tmp_path, status, expected) -> None:
    store, thread, repository, service, signal = _queued_execution(tmp_path)
    task_id = signal.root_task_id
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "status": status, "goal": "原任务"})
    path = store.storage.task_path(task_id)
    before = path.read_bytes()

    result = service.claim_wake(signal, lease_seconds=60, now=1_001)

    assert result.status == "stale" and result.claim is None
    assert path.read_bytes() == before
    assert repository.get_active_run(task_id) is None
    history, errors = repository.history()
    assert not errors and history[0]["status"] == expected
    assert history[0]["error_code"] == ("" if expected == "done" else f"SCHEDULED_TASK_{status.upper()}")


@pytest.mark.parametrize("field", ["thread_id", "root_task_id"])
def test_claim_rejects_mismatched_wake_before_binding(tmp_path, field) -> None:
    store, _thread, repository, service, signal = _queued_execution(tmp_path)
    task_id = signal.root_task_id
    before = repository.get_active_run(task_id)

    result = service.claim_wake(replace(signal, **{field: "other-identity"}), lease_seconds=60, now=1_001)

    assert result.status == "stale" and result.claim is None
    assert store.tasks.load(task_id) is None
    assert repository.get_active_run(task_id) == before
    assert repository.history()[0] == []


@pytest.mark.parametrize("stage", ["bind", "check_run_claim"])
def test_admission_exception_settles_the_acquired_claim(tmp_path, monkeypatch, stage) -> None:
    store, _thread, repository, service, signal = _queued_execution(tmp_path)

    # LLM: 仅注入确定性的本地存储异常，不启动后台进程或网络。
    # 函数用途: 核对已取得的原 claim 在准入写入失败后仍能按原协议终结。
    def fail(*_args, **_kwargs):
        raise OSError("fixture admission failure")

    monkeypatch.setattr(store.tasks if stage == "bind" else repository, stage, fail)
    result = service.claim_wake(signal, lease_seconds=60, now=1_001)

    assert result.status == "stale" and result.claim is None
    assert repository.get_active_run(signal.root_task_id) is None
    history, errors = repository.history()
    assert not errors and history[0]["status"] == "failed"
    assert history[0]["error_code"] == "SCHEDULER_TASK_BINDING_INVALID"


def test_admission_settlement_failure_is_not_reported_as_a_closed_claim(tmp_path, monkeypatch) -> None:
    store, _thread, repository, service, signal = _queued_execution(tmp_path)

    # LLM: 首次准入错误和其后结算错误分别保留，不能把无法落盘的结算声明为成功。
    # 函数用途: 验证原异常因果和仍待处理的 claim 不被返回 stale 的路径吞掉。
    def fail_binding(*_args, **_kwargs):
        raise ValueError("fixture binding failure")

    # LLM: 结算持久化不可用时异常必须向上传播，不产生成功或已关闭的伪造回执。
    # 函数用途: 模拟本地结算写入失败，保留原准入错误为异常上下文。
    def fail_settlement(*_args, **_kwargs):
        raise OSError("fixture settlement failure")

    monkeypatch.setattr(store.tasks, "bind", fail_binding)
    monkeypatch.setattr(repository, "finish_run", fail_settlement)
    with pytest.raises(OSError, match="fixture settlement failure") as error:
        service.claim_wake(signal, lease_seconds=60, now=1_001)
    assert isinstance(error.value.__context__, ValueError)
    assert repository.get_active_run(signal.root_task_id)["status"] == "claimed"
    assert repository.history()[0] == []


@pytest.mark.parametrize("task_status", ["active", "completed"])
def test_replaced_claim_during_admission_does_not_consume_pending_wake(
    tmp_path, monkeypatch, task_status,
) -> None:
    import agent_py_agent.agent.scheduler.repository as repository_module
    from agent_py_agent.agent.conversation.runtime import _claim_scheduler_wake

    store, thread, repository, service, signal = _queued_execution(tmp_path)
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": signal.root_task_id,
        "status": task_status, "goal": "原任务",
    })
    path = store.storage.task_path(signal.root_task_id)
    before = path.read_bytes()
    prepare = service._prepare_task_link
    replacement = {}

    # LLM: 在原准入回读后用真实repository替换已到期claim，触发原CAS冲突；不使用并发计时或网络。
    # 函数用途: 确定性覆盖运行准入与终态结算两条路径，防止旧持有者把新持有者的wake确认掉。
    def replace_expired_claim(claim, *, now):
        status = prepare(claim, now=now)
        current = repository.claim_run(claim.run_id, lease_seconds=60, now=1_062)
        assert current is not None and current["claim_id"] != claim.claim_id
        replacement.update(current)
        return status

    monkeypatch.setattr(service, "_prepare_task_link", replace_expired_claim)
    scheduler = SimpleNamespace(scheduler_service=service, store=store, claim_ttl_seconds=60)

    result = _claim_scheduler_wake(scheduler, signal, now=1_001)

    assert result.stop and result.claim is None
    assert store.wakes.pending_one(signal.wake_signal_id) is not None
    assert repository.get_active_run(signal.root_task_id) == replacement
    assert repository.history() == ([], [])
    assert path.read_bytes() == before


# LLM: 首轮经原领取及释放建立 started_at，再冻结原 wake；TaskLink 的 completed 不能用假 active 绕过。
# 函数用途: 在隔离存储准备真实欠投递的定时执行，返回尚未包含冻结字段的旧队列投影。
def _queued_frozen_delivery(tmp_path):
    store, thread, repository, service, signal = _queued_execution(tmp_path)
    first = service.claim_wake(signal, now=1_001)
    assert first.claim is not None
    repository.mark_run_running(signal.root_task_id, first.claim.claim_id, now=1_001)
    service.release(first.claim, now=1_002)
    store.tasks.update_status({"task_id": signal.root_task_id, "status": "completed", "now": 1_002})
    store.wakes.cache_delivery(signal.wake_signal_id, {
        "schema_version": "wake-owner-delivery.v2", "task_id": signal.root_task_id,
        "reason": "scheduled_job_due", "content": "原模型已完成的冻结答复",
        "external_sent": False, "receipt_id": "", "ownership": "external",
    })
    assert "owner_delivery" not in signal.metadata
    return store, thread, repository, service, signal


def test_claim_rereads_canonical_frozen_delivery_without_marking_new_execution(tmp_path) -> None:
    store, _thread, repository, service, signal = _queued_frozen_delivery(tmp_path)
    task_before = store.storage.task_path(signal.root_task_id).read_bytes()

    result = service.claim_wake(signal, now=1_003)

    assert result.status == "claimed" and result.claim is not None
    active = repository.get_active_run(signal.root_task_id)
    assert active["status"] == "claimed", "已完成任务只领取原投递，不重新标为 running"
    assert active["claim_id"] == result.claim.claim_id
    assert active["started_at"] == 1_001
    assert repository.history()[0] == []
    assert store.storage.task_path(signal.root_task_id).read_bytes() == task_before
    assert store.wakes.pending_one(signal.wake_signal_id) is not None


@pytest.mark.parametrize("fault", [
    "missing", "bad_json", "wake_signal_id", "thread_id", "root_task_id", "reason",
])
def test_claim_releases_when_canonical_pending_cannot_be_confirmed(tmp_path, fault) -> None:
    store, _thread, repository, service, signal = _queued_frozen_delivery(tmp_path)
    path = store.storage.wake_signal_path(signal)
    if fault == "missing":
        path.unlink()
    elif fault == "bad_json":
        path.write_text("{", encoding="utf-8")
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[fault] = "different-identity"
        path.write_text(json.dumps(payload), encoding="utf-8")
    pending_before = path.read_bytes() if path.exists() else None

    result = service.claim_wake(signal, now=1_003)

    assert result.status == "busy" and result.claim is None
    active = repository.get_active_run(signal.root_task_id)
    assert active["status"] == "queued" and active["claim_id"] == ""
    assert repository.history()[0] == []
    assert store.tasks.load(signal.root_task_id).status == "completed"
    assert (path.read_bytes() if path.exists() else None) == pending_before


def test_claim_releases_and_propagates_canonical_pending_read_error(tmp_path, monkeypatch) -> None:
    store, _thread, repository, service, signal = _queued_frozen_delivery(tmp_path)
    path = store.storage.wake_signal_path(signal)
    pending_before = path.read_bytes()
    failure = OSError("fixture pending read failed")

    # LLM: 仅替换入口只读查询来模拟其向上抛错；真实 claim 领取与释放不能被替换。
    # 函数用途: 验证读失败保留原异常，不伪装成任务绑定损坏或投递成功。
    def fail_read(_wake_signal_id):
        raise failure

    monkeypatch.setattr(store.wakes, "pending_one", fail_read)
    with pytest.raises(OSError) as caught:
        service.claim_wake(signal, now=1_003)

    assert caught.value is failure
    active = repository.get_active_run(signal.root_task_id)
    assert active["status"] == "queued" and active["claim_id"] == ""
    assert repository.history()[0] == []
    assert path.read_bytes() == pending_before


# LLM: 只构造旧版本可能遗留的合法queued副本；通过原owner事务落盘，不伪造领取或执行结果。
# 函数用途: 为同job多份积压的升级场景创建第二条真实运行。
def _legacy_peer(repository, run, *, now=1001):
    from agent_py_agent.agent.scheduler.repository import _reserved_run

    job = repository.get_job(run["job_id"])
    duplicate = _reserved_run(job, trigger="manual", scheduled_for=now, dispatch_after=now, now=now)
    with repository._mutation_scope() as (store, admission):
        store["runs"][duplicate["run_id"]] = duplicate
        repository._write_store_unlocked(store, admission)
    return duplicate


# LLM: 正常单规则已按job阻止预留；旧queued副本也不能在已claimed/running/waiting时被领取。
# 函数用途: 复现只检查run自身租约而绕过job互斥的入口。
@pytest.mark.parametrize("status", ["claimed", "running", "waiting"])
def test_same_job_legacy_peer_cannot_claim_until_terminal(tmp_path, status):
    store, _thread, repository, service, signal = _queued_execution(tmp_path)
    first = service.claim_wake(signal, now=1001).claim
    assert first is not None
    if status in {"running", "waiting"}:
        repository.mark_run_running(first.run_id, first.claim_id, now=1001)
    if status == "waiting":
        service.park_waiting(first, now=1001)
    peer = _legacy_peer(repository, first.run)
    assert repository.claim_run(peer["run_id"], lease_seconds=60, now=1002) is None
    assert repository.get_active_run(first.run_id)["status"] == status
    assert repository.get_active_run(peer["run_id"])["status"] == "queued"
    if status == "waiting":
        from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
        repository.finish_waiting_run(first.run_id, SchedulerRunFinish(status="done", now=1003))
    else:
        assert service.finish(first, status="done", now=1003) is not None
    assert repository.claim_run(peer["run_id"], lease_seconds=60, now=1004) is not None
    assert store.tasks.load(first.run_id) is not None


# LLM: queued带wake即已派发，首轮尚未claim也须占同job额度；另一个job不受影响。
# 函数用途: 复现同job旧队列再次发wake，并验证合法不同job并发。
def test_dispatch_defers_duplicate_job_but_allows_distinct_job(tmp_path):
    store, thread, repository, service, signal = _queued_execution(tmp_path)
    original = repository.get_active_run(signal.root_task_id)
    peer = _legacy_peer(repository, original)
    other = _create(repository, thread.thread_id, name="different")
    wakes = service.enqueue_ready_runs(now=1002)
    signals = store.wakes.pending()
    assert {row.metadata["scheduler_job_id"] for row in signals} == {original["job_id"], other["job_id"]}
    assert len(signals) == 2 and len(wakes) == 2
    assert repository.get_active_run(peer["run_id"])["wake_signal_id"] == ""
    assert repository.get_active_run(original["run_id"])["wake_signal_id"] == signal.wake_signal_id


# LLM: 同owner二十个不同job全部可同时持有原claim，不引入owner级粗锁或线程池串行执行。
# 函数用途: 锁定防重叠的准确隔离粒度。
def test_twenty_distinct_jobs_can_all_be_claimed(tmp_path):
    store, thread, repository = _setup(tmp_path)
    jobs = [_create(repository, thread.thread_id, name=f"parallel-{index}") for index in range(20)]
    service = SchedulerService(repository, conversation_store=store)
    assert len(service.enqueue_ready_runs(now=1000)) == 20
    claims = [service.claim_wake(signal, lease_seconds=1000, now=1001).claim for signal in store.wakes.pending()]
    assert all(claim is not None for claim in claims)
    assert {claim.run["job_id"] for claim in claims} == {job["job_id"] for job in jobs}
    for claim in claims:
        repository.mark_run_running(claim.run_id, claim.claim_id, now=1002)
    assert all(repository.get_active_run(claim.run_id)["status"] == "running" for claim in claims)


# LLM: due与run_now必须沿原事务观察到活动job；租约过期不等于已死亡，不允许新执行偷占。
# 函数用途: 核对正常周期与手动触发已有防线，不能因本补丁退化。
@pytest.mark.parametrize("status", ["queued", "claimed", "running", "waiting"])
def test_recurring_and_manual_trigger_defer_for_same_active_job(tmp_path, status):
    from agent_py_agent.agent.scheduler.repository import SchedulerConflictError

    _store, _thread, repository, service, signal = _queued_execution(tmp_path)
    run = repository.get_active_run(signal.root_task_id)
    if status != "queued":
        claim = service.claim_wake(signal, lease_seconds=6000, now=1001).claim
        if status in {"running", "waiting"}:
            repository.mark_run_running(claim.run_id, claim.claim_id, now=1001)
        if status == "waiting":
            service.park_waiting(claim, now=1001)
    assert repository.reserve_due_runs(now=1600) == []
    with pytest.raises(SchedulerConflictError):
        repository.reserve_manual_run(run["job_id"], now=1600)
    assert len(repository._valid_runs(repository._store_snapshot())[0]) == 1


# LLM: 两个独立repository实例共享原磁盘锁，Barrier只同步起点，不用单实例内存锁伪证跨调度器互斥。
# 函数用途: 验证旧多queued在同时claim时只能一份成功，不同run_id也不能绕过。
def test_competing_repository_claims_are_singleflight(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _store, _thread, repository, _service, signal = _queued_execution(tmp_path)
    first = repository.get_active_run(signal.root_task_id)
    second = _legacy_peer(repository, first)
    other_repository = SchedulerRepository(repository.root, owner_provider="feishu", owner_kind="user", owner_id="user-1")
    ready = Barrier(2)

    # LLM: 两个线程只调用真实CAS入口，不替换锁、存储或状态结果。
    # 函数用途: 同时竞争同job的两条run。
    def claim(repo, run):
        ready.wait(5)
        return repo.claim_run(run["run_id"], lease_seconds=60, now=1002)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(claim, repo, run) for repo, run in [(repository, first), (other_repository, second)]]
        results = [future.result(5) for future in futures]
    assert sum(value is not None for value in results) == 1
    assert {run["status"] for run in repository._valid_runs(repository._store_snapshot())[0]} == {"claimed", "queued"}


# LLM: 准入后发wake之前崩溃不能让别的run偷额度；重启沿同一原run重试，不重放或杀活动任务。
# 函数用途: 检查持久派发标记覆盖跨store窗口和重启恢复。
def test_dispatch_interruption_keeps_singleflight_on_restart(tmp_path, monkeypatch):
    store, thread, repository = _setup(tmp_path)
    _create(repository, thread.thread_id, name="restart-dispatch")
    service = SchedulerService(repository, conversation_store=store)
    original = store.wakes.raise_signal

    # LLM: 只在真正运输wake入口前抛中断，前置准入/prepare仍是产品实现。
    # 函数用途: 制造已占job额度但wake未提交的真实窗口。
    def fail(_request):
        raise RuntimeError("fixture wake not published")

    monkeypatch.setattr(store.wakes, "raise_signal", fail)
    with pytest.raises(RuntimeError, match="wake not published"):
        service.enqueue_ready_runs(now=1000)
    run = repository.queued_runs(now=1000)[0]
    assert run.get("dispatch_admitted") is True and not run["wake_signal_id"]
    peer = _legacy_peer(repository, run)
    monkeypatch.setattr(store.wakes, "raise_signal", original)
    restarted = SchedulerRepository(repository.root, owner_provider="feishu", owner_kind="user", owner_id="user-1")
    assert len(SchedulerService(restarted, conversation_store=store).enqueue_ready_runs(now=1002)) == 1
    assert store.wakes.pending()[0].root_task_id == run["run_id"]
    assert not restarted.get_active_run(peer["run_id"])["wake_signal_id"]


# LLM: 只替换原PID判活探针证明死亡，恢复/unknown/history与queued派发均是真实现；超时本身不解除互斥。
# 函数用途: 验证每拍全量恢复能收回死亡peer，不必再次claim旧run才解除阻挡。
@pytest.mark.parametrize("liveness,dispatched", [("dead", True), ("alive", False), ("unverifiable", False)])
def test_enqueue_recovers_dead_peer_before_dispatching_queued(tmp_path, monkeypatch, liveness, dispatched):
    from agent_py_agent.agent.scheduler import repository as module

    _store, _thread, repository, service, signal = _queued_execution(tmp_path)
    claim = service.claim_wake(signal, lease_seconds=1, now=1001).claim
    repository.mark_run_running(claim.run_id, claim.claim_id, now=1001)
    peer = _legacy_peer(repository, claim.run)
    monkeypatch.setattr(module, "_process_state", lambda _pid: liveness)
    service.enqueue_ready_runs(now=1003)
    assert bool(repository.get_active_run(peer["run_id"])["wake_signal_id"]) is dispatched
    if dispatched:
        assert repository.get_active_run(claim.run_id) is None
        history, errors = repository.history()
        assert not errors and history[0]["status"] == "unknown"
        assert history[0]["error_code"] == "SCHEDULER_RUNNER_DIED"
    else:
        assert repository.get_active_run(claim.run_id)["status"] == "running"


# LLM: 相同run准入允许幂等重试，原Wake存储负责跨dispatcher去重；不按两个返回列表误判发了两份。
# 函数用途: 核对两份service并发发同run仍只有一个真实wake和一次成功claim。
def test_concurrent_same_run_dispatch_has_one_wake_and_claim(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store, thread, repository = _setup(tmp_path)
    _create(repository, thread.thread_id, name="same-run")
    other_repository = SchedulerRepository(repository.root, owner_provider="feishu", owner_kind="user", owner_id="user-1")
    services = [SchedulerService(repo, conversation_store=store) for repo in [repository, other_repository]]
    ready = Barrier(2)

    # LLM: 同步的只有入口时间，真实准入、prepare、dedupe和claim均保留。
    # 函数用途: 让两个dispatcher竞争同一原run。
    def dispatch(service):
        ready.wait(5)
        return service.enqueue_ready_runs(now=1000)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(dispatch, service) for service in services]
        results = [future.result(5) for future in futures]
    signals = store.wakes.pending()
    assert len(signals) == 1 and all(result == [signals[0].wake_signal_id] for result in results)
    assert services[0].claim_wake(signals[0], now=1001).claim is not None
    assert services[1].claim_wake(signals[0], now=1001).status == "busy"


# LLM: 限制的是不同job候选数，不能让旧重复队列占满limit导致别的job饥饿；不跳过原状态CAS。
# 函数用途: 复现同job积压的队首阻塞，保持其它job当拍派发。
def test_same_job_backlog_does_not_starve_distinct_job_at_small_limit(tmp_path):
    store, thread, repository, service, signal = _queued_execution(tmp_path)
    claim = service.claim_wake(signal, now=1001).claim
    repository.mark_run_running(claim.run_id, claim.claim_id, now=1001)
    for _index in range(8):
        _legacy_peer(repository, claim.run, now=999)
    other = _create(repository, thread.thread_id, name="not-starved")
    # 后到的新job仍应当拍获准，不能藏在被阻挡的旧queued之后。
    repository.reserve_due_runs(now=1002)
    wakes = service.enqueue_ready_runs(now=1002, limit=1)
    assert len(wakes) == 1
    assert store.wakes.pending_one(wakes[0]).metadata["scheduler_job_id"] == other["job_id"]
    assert repository.get_active_run(claim.run_id)["status"] == "running"
