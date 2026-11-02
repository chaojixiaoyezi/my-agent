"""R8-R1：可复用后台身份连续接后继任务时，原取消所需的终态 claim 证明不因 claim 替换或归档而丢失。

只替换外部模型；业务入口、调度器、SessionTask/Claim 存储与 RuntimeDB 均为原生组合。
"""
import time
from copy import deepcopy
from dataclasses import replace
from threading import Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import store_claims
from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
from agent_py_agent.agent.conversation.session_list_index import SessionListIndex
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskStore
from agent_py_agent.agent.gateway_parts import io as gateway_io
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_mega_background_lifecycle_controls import (
    edit_task,
    eventually_retired,
    native_cancellation_case,
    ready_index,
)

GUARDED_TIMEOUT = AgentConfig().request_timeout
DIRECT_MODEL = 0


def _identity(task):
    # 原任务身份：取消/退休/检查点都不能改写这些绑定。
    return (task.task_id, task.sender_thread_id, task.target_thread_id, task.origin_task_id,
            task.metadata.get("business_context"), task.body_dedupe_key, task.body_guidance_id,
            task.conversation_request_id, task.task_run_id)


def _start_cancelled_original(tmp_path, *, request_timeout=GUARDED_TIMEOUT, execution_only=False, while_running=None):
    """原业务任务在真实模型调用中被取消；原调度片跑在可显式 join 的独立线程里。"""
    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path, request_timeout=request_timeout)
    store, outcome = agent.conversation_store, {}

    def original_slice():
        try:
            outcome["reports"] = scheduler.tick()
        except BaseException as exc:  # noqa: BLE001 - 交给测试线程断言
            outcome["error"] = exc

    original_tick = Thread(target=original_slice, name="r8-original-slice")
    original_tick.start()
    case = SimpleNamespace(agent=agent, backend=backend, scheduler=scheduler, call=call, task_id=task_id, view=view,
                           store=store, repo=agent.subagents.runtime_db, original_tick=original_tick, outcome=outcome)
    try:
        assert backend.waiting.wait(15), "native scheduler did not reach the original model call"
        task = store.session_tasks.require(task_id)
        case.target = task.target_thread_id
        case.space_id = task.metadata["business_context"]["space_id"]
        case.claim_path = store.storage.background_claim_path(case.target)
        case.task_path = store.session_tasks._path(task_id)
        case.original_claim = store.claims.load(case.target)
        case.authority = case.original_claim["runtime_authority"]
        if while_running is not None:
            while_running(case)
        if execution_only:
            case.action, case.params = "executions.cancel", {
                "run_id": case.authority["run_id"], "attempt_id": case.authority["attempt_id"], "include_descendants": True}
        else:
            case.action, case.params = "tasks.cancel", {"task_id": task_id, "include_descendants": True}
        assert call(case.action, case.params)["state"] == "pending"
    except BaseException:
        _cleanup(case)
        raise
    return case


def _wait_original_slice(case):
    case.original_tick.join(timeout=15)
    assert not case.original_tick.is_alive(), "original scheduler slice did not return"
    assert "error" not in case.outcome, case.outcome.get("error")


def _join_original_worker(case):
    # 逻辑cancelled不是物理退出；释放并显式join原模型worker后才允许核收。
    case.backend.release.set()
    worker = case.backend.waiting_worker
    worker.join(timeout=15)
    assert not worker.is_alive() and case.backend.returned.is_set()
    _wait_original_slice(case)
    assert case.backend.calls == 2
    claim = case.store.claims.load(case.target)
    assert claim["claim_id"] == case.original_claim["claim_id"] and claim["status"] == "cancelled"
    assert claim["finished_at"] > 0
    case.terminal_claim = claim


def _cleanup(case):
    case.backend.release.set()
    if case.backend.waiting_worker is not None:
        case.backend.waiting_worker.join(timeout=15)
    case.original_tick.join(timeout=15)


def _successor_backend(original):
    class SuccessorBackend(type(original)):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            return ModelResponse(text=f"successor result {self.calls}", backend=self.name)

    return SuccessorBackend()


def _assign_successor(case, index):
    return case.call("tasks.assign", {"space_id": case.space_id, "agent_id": case.view["agent_id"],
        "instruction": f"independent successor {index}"}, idempotency_key=f"r8-successor-{index}")["task_id"]


def _run_successors(case, count):
    # 同一可用/复用身份上的普通后继：每个都经原调度器领取新claim并完成。
    case.agent.backend = backend = _successor_backend(case.backend)
    successor_ids = []
    for index in range(count):
        successor_ids.append(_assign_successor(case, index))
        case.scheduler.tick()
        assert case.store.session_tasks.require(successor_ids[-1]).status == "done"
    assert backend.calls == count
    if count:
        claim = case.store.claims.load(case.target)
        assert claim["claim_id"] != case.original_claim["claim_id"] and claim["status"] == "finished"
    return successor_ids, backend


def _control(case):
    return case.store.session_tasks.require(case.task_id).metadata["cancellation_control"]


def _retire(case, key):
    ready_index(case.store)
    return case.call("background_agents.retire", {"agent_id": case.view["agent_id"], "disposition": "drain"},
                     expected_revision=case.view["revision"], idempotency_key=key)


@pytest.mark.parametrize("successors", [0, 1, 2, 4])
@pytest.mark.parametrize("execution_only", [False, True], ids=["task-cancel", "execution-cancel"])
@pytest.mark.parametrize("request_timeout", [GUARDED_TIMEOUT, DIRECT_MODEL], ids=["guarded-240s", "direct-model"])
def test_original_cancellation_and_retirement_converge_after_healthy_successors(
        tmp_path, request_timeout, execution_only, successors):
    assert GUARDED_TIMEOUT == 240
    case = _start_cancelled_original(tmp_path, request_timeout=request_timeout, execution_only=execution_only)
    try:
        _join_original_worker(case)
        before = case.store.session_tasks.require(case.task_id)
        successor_ids, successor_backend = _run_successors(case, successors)
        successor_rows = [case.store.session_tasks.require(identifier).to_dict() for identifier in successor_ids]
        current_claim = case.claim_path.read_bytes()
        control = _control(case)
        # 只有原取消账得到持久证明；物理/资源确认仍待真实重试核收。
        assert control["outstanding_refs"] and control["claim_ref"]["claim_id"] == case.original_claim["claim_id"]
        assert control.get("claim_finished") is (True if successors else None)
        assert {key: value for key, value in control.items() if key != "claim_finished"} == before.metadata[
            "cancellation_control"]
        if execution_only:
            done = case.call(case.action, case.params)
            assert done["state"] == "completed", done
            assert case.call(case.action, case.params) == done
            assert case.call("tasks.cancel", {"task_id": case.task_id, "include_descendants": True})["state"] == "completed"
        else:
            # 退休对账消费同一原取消账；修复前两次以上后继后这里永远停在retiring。
            _retire(case, "r8-retire-task")
            retired = eventually_retired(case.agent, case.view["agent_id"])
            assert identity_outstanding(case.agent, retired) == ([], [])
            done = case.call(case.action, case.params)
            assert done["state"] == "completed", done
            assert case.call(case.action, case.params) == done
        assert not _control(case)["outstanding_refs"]
        assert _identity(case.store.session_tasks.require(case.task_id)) == _identity(before)
        assert [case.store.session_tasks.require(identifier).to_dict() for identifier in successor_ids] == successor_rows
        assert case.claim_path.read_bytes() == current_claim
        assert successor_backend.calls == successors and case.backend.calls == 2
        assert case.repo.get_agent_run_exec_lock(case.authority["agent_run_id"]) is None
        assert not (case.agent.home_paths.owner_home_dir / "after-cancel-result.txt").exists()
    finally:
        _cleanup(case)


def _fail_write_once(monkeypatch, predicate):
    real, failures = gateway_io.write_private_json_file_atomic_no_newline_unlocked, []

    def write(path, payload, **kwargs):
        if not failures and predicate(path, payload):
            failures.append(path)
            raise OSError("simulated single durable write failure")
        return real(path, payload, **kwargs)

    monkeypatch.setattr(gateway_io, "write_private_json_file_atomic_no_newline_unlocked", write)
    return failures


def _retry_successor_slice(case, successor_id):
    wake = next(w for w in case.store.wakes.pending(limit=0) if w.metadata.get("session_task_id") == successor_id)
    state, error = case.store.wakes.attempts.state_report(wake.wake_signal_id)
    assert error is None and state is not None
    case.scheduler.tick_thread(case.target, now=max(time.time(), state.next_attempt_at,
                                                    case.scheduler._wake_retry_after.get(wake.wake_signal_id, 0.0)))


def test_single_checkpoint_write_failure_retains_old_claim_and_same_acquisition_retry_succeeds(tmp_path, monkeypatch):
    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        old_claim, old_task = case.claim_path.read_bytes(), case.task_path.read_bytes()
        failures = _fail_write_once(monkeypatch, lambda path, payload: path == case.task_path)
        case.agent.backend = successor_backend = _successor_backend(case.backend)
        first = _assign_successor(case, 0)
        with pytest.raises(OSError, match="single durable write failure"):
            case.scheduler.tick()
        assert failures == [case.task_path]
        # 旧终态claim原样保留，后继未进入模型，原取消账未被半写。
        assert case.claim_path.read_bytes() == old_claim and case.task_path.read_bytes() == old_task
        assert successor_backend.calls == 0 and case.store.session_tasks.require(first).status == "queued"
        _retry_successor_slice(case, first)
        assert successor_backend.calls == 1 and case.store.session_tasks.require(first).status == "done"
        assert _control(case)["claim_finished"] is True
        assert case.store.claims.load(case.target)["previous_claim"]["claim_id"] == case.original_claim["claim_id"]
        second = _assign_successor(case, 1)
        case.scheduler.tick()
        assert case.store.session_tasks.require(second).status == "done"
        assert case.call(case.action, case.params)["state"] == "completed"
        _retire(case, "r8-retire-after-checkpoint-retry")
        assert eventually_retired(case.agent, case.view["agent_id"]).metadata["background_agent"]["identity_state"] == "retired"
    finally:
        _cleanup(case)


def test_checkpoint_success_then_replacement_failure_is_idempotent_on_retry(tmp_path, monkeypatch):
    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        old_claim = case.claim_path.read_bytes()
        failures = _fail_write_once(monkeypatch, lambda path, payload: (
            path == case.claim_path and payload.get("claim_id") != case.original_claim["claim_id"]))
        inner = gateway_io.write_private_json_file_atomic_no_newline_unlocked  # 已含单次claim写失败注入
        task_writes = []

        def count_task_writes(path, payload, **kwargs):
            if path == case.task_path:
                task_writes.append(payload["metadata"]["cancellation_control"].get("claim_finished"))
            return inner(path, payload, **kwargs)

        monkeypatch.setattr(gateway_io, "write_private_json_file_atomic_no_newline_unlocked", count_task_writes)
        case.agent.backend = successor_backend = _successor_backend(case.backend)
        first = _assign_successor(case, 0)
        with pytest.raises(OSError, match="single durable write failure"):
            case.scheduler.tick()
        assert failures == [case.claim_path] and task_writes == [True]
        checkpointed = case.task_path.read_bytes()
        assert _control(case)["claim_finished"] is True
        assert case.claim_path.read_bytes() == old_claim and successor_backend.calls == 0
        _retry_successor_slice(case, first)
        # 重试只复用已保存证明，不再写原任务；新claim正常替换。
        assert task_writes == [True] and case.task_path.read_bytes() == checkpointed
        assert successor_backend.calls == 1 and case.store.session_tasks.require(first).status == "done"
        second = _assign_successor(case, 1)
        case.scheduler.tick()
        assert case.store.session_tasks.require(second).status == "done"
        assert case.call(case.action, case.params)["state"] == "completed"
    finally:
        _cleanup(case)


def _mutated(claim, field):
    claim = deepcopy(claim)
    authority = claim["runtime_authority"]
    if field == "claim":
        claim["claim_id"] = authority["claim_id"] = "bgclaim-not-original"
    elif field == "thread":
        claim["thread_id"] = claim["claim_scope_id"] = "thread-not-original"
    elif field == "claim_task":
        claim["task_id"] = "not-original-claim-task"
    elif field in {"run_id", "agent_run_id", "attempt_id", "task_id"}:
        authority[field] = f"not-original-{field}"
    elif field == "running":
        claim["status"] = "running"
    elif field == "unfinished":
        claim["finished_at"] = 0
    return claim


@pytest.mark.parametrize("field,expected", [
    ("claim", False), ("running", False), ("unfinished", False),
    ("thread", "conflict"), ("claim_task", "conflict"), ("run_id", "conflict"),
    ("agent_run_id", "conflict"), ("attempt_id", "conflict"), ("task_id", "conflict"),
])
def test_wrong_claim_identity_cannot_save_proof(tmp_path, field, expected):
    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        before, control = case.task_path.read_bytes(), _control(case)
        helper = SessionTaskStore(case.store.storage)
        claim = _mutated(case.terminal_claim, field)
        if expected == "conflict":
            with pytest.raises(ValueError, match="SESSION_TASK_CLAIM_PROOF_CONFLICT"):
                helper.preserve_terminal_cancellation_claim(claim)
        else:
            assert helper.preserve_terminal_cancellation_claim(claim) is False
        assert case.task_path.read_bytes() == before and "claim_finished" not in _control(case)
        # 原始准确证明仍可保存，且只补 claim_finished；重复保存幂等且不再写原任务。
        assert helper.preserve_terminal_cancellation_claim(case.terminal_claim) is True
        saved = case.task_path.read_bytes()
        assert helper.preserve_terminal_cancellation_claim(case.terminal_claim) is True
        assert case.task_path.read_bytes() == saved
        assert _control(case) == {**control, "claim_finished": True} and control["outstanding_refs"]
    finally:
        _cleanup(case)


@pytest.mark.parametrize("wrong", ["owner", "task", "thread", "claim", "run", "attempt"])
def test_wrong_identity_on_disk_cannot_clear_outstanding_reference(tmp_path, wrong):
    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        if wrong == "owner":
            def foreign_owner(task):
                control = task.metadata["cancellation_control"]
                return replace(task, metadata={**task.metadata, "cancellation_control": {
                    **control, "execution_ref": {**control["execution_ref"], "owner_id": "foreign-owner"}}})

            edit_task(case.store, case.task_id, foreign_owner)
        else:
            original = case.store.session_tasks.require(case.task_id)
            # 另一条真实canonical任务（无派活wake），只用于让旧claim错误地指向它。
            other = case.store.session_tasks.create(SessionTaskDraft(
                original.sender_thread_id, case.target, "unrelated canonical task")).task_id
            mutation = {"thread": "thread", "claim": "claim", "run": "run_id", "attempt": "attempt_id"}.get(wrong)

            def tamper(raw):
                if wrong == "task":
                    raw = deepcopy(raw)
                    raw["runtime_authority"]["invocation_run_id"] = other
                    return raw
                return _mutated(raw, mutation)

            gateway_io.update_json_file_atomic(case.claim_path, tamper, require_existing=True)
        tampered_claim, task_before = case.claim_path.read_bytes(), case.task_path.read_bytes()
        case.agent.backend = successor_backend = _successor_backend(case.backend)
        successor = _assign_successor(case, 0)
        if wrong in {"owner", "thread", "run", "attempt"}:
            # 同一原claim ID却身份冲突：不吞冲突，不替换旧证据，后继模型不启动。
            with pytest.raises(ValueError, match="SESSION_TASK_CLAIM_PROOF_CONFLICT"):
                case.scheduler.tick()
            assert case.claim_path.read_bytes() == tampered_claim and successor_backend.calls == 0
            assert case.store.session_tasks.require(successor).status == "queued"
        else:
            for index in (0, 1):
                if index:
                    successor = _assign_successor(case, index)
                case.scheduler.tick()
                assert case.store.session_tasks.require(successor).status == "done"
        assert case.task_path.read_bytes() == task_before and "claim_finished" not in _control(case)
        retried = case.call(case.action, case.params)
        assert retried["state"] == "pending" and retried["outstanding_refs"]
        assert _control(case)["outstanding_refs"] and "claim_finished" not in _control(case)
    finally:
        _cleanup(case)


def test_running_worker_keeps_cancellation_pending_after_claim_proof(tmp_path):
    case = _start_cancelled_original(tmp_path)
    try:
        _wait_original_slice(case)
        worker = case.backend.waiting_worker
        assert worker.is_alive() and not case.backend.returned.is_set()
        successor_ids, _backend = _run_successors(case, 2)
        # claim 已终态的证明独立于物理worker；原worker未退出前仍不能完成取消或退休。
        assert _control(case)["claim_finished"] is True and worker.is_alive()
        assert case.call(case.action, case.params)["state"] == "pending"
        assert _retire(case, "r8-retire-running-worker")["agent"]["identity_state"] == "retiring"
        _join_original_worker_after_successors(case)
        assert case.call(case.action, case.params)["state"] == "completed"
        assert eventually_retired(case.agent, case.view["agent_id"]).metadata["background_agent"]["identity_state"] == "retired"
        assert all(case.store.session_tasks.require(identifier).status == "done" for identifier in successor_ids)
    finally:
        _cleanup(case)


def _join_original_worker_after_successors(case):
    case.backend.release.set()
    case.backend.waiting_worker.join(timeout=15)
    assert not case.backend.waiting_worker.is_alive() and case.backend.returned.is_set()


def test_unknown_worker_outcome_keeps_cancellation_pending_after_claim_proof(tmp_path):
    from agent_py_agent.agent.conversation.business_contracts import HostParentExecutionRef

    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        ref = HostParentExecutionRef.from_dict(_control(case)["execution_ref"])
        # 旧版无原worker身份的未知标记：claim终态证明不能替它补签物理退出。
        case.store.session_tasks.record_execution_cancellation_control(case.task_id, execution_ref=ref,
            outstanding_refs=[], model_worker_exit_unknown=True)
        _run_successors(case, 2)
        assert _control(case)["claim_finished"] is True and _control(case)["model_worker_exit_unknown"] is True
        for _ in range(2):
            assert case.call(case.action, case.params)["state"] == "pending"
        assert _control(case)["outstanding_refs"]
    finally:
        _cleanup(case)


def test_unsettled_original_operation_keeps_cancellation_pending_until_settled(tmp_path):
    operations = []

    def start_operation(case):
        row = case.repo.create_tool_operation(agent_run_id=case.authority["agent_run_id"],
            attempt_id=case.authority["attempt_id"], operation_type="r8_unsettled_probe")
        case.repo.mark_operation_executing(row["operation_id"])
        operations.append(row["operation_id"])

    case = _start_cancelled_original(tmp_path, while_running=start_operation)
    try:
        _join_original_worker(case)
        assert case.repo.unsettled_attempt_operations(case.authority["attempt_id"])
        _run_successors(case, 2)
        assert _control(case)["claim_finished"] is True
        assert case.call(case.action, case.params)["state"] == "pending"
        case.repo.settle_operation(operations[0], "FAILED", {"reason": "r8 settled after observation"})
        assert not case.repo.unsettled_attempt_operations(case.authority["attempt_id"])
        assert case.call(case.action, case.params)["state"] == "completed"
    finally:
        _cleanup(case)


def test_available_reuse_checkpoint_has_no_history_scan_or_predecessor_chain(tmp_path, monkeypatch):
    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        active, checkpoints = [], []
        original_helper = SessionTaskStore.preserve_terminal_cancellation_claim

        def observed(self, claim):
            active.append(claim.get("claim_id"))
            try:
                checkpoints.append(original_helper(self, claim))
                return checkpoints[-1]
            finally:
                active.pop()

        originals = {"list_page": SessionTaskStore.list_page, "list_report": SessionTaskStore.list_report,
                     "page": SessionListIndex.page}
        for owner, name in ((SessionTaskStore, "list_page"), (SessionTaskStore, "list_report"), (SessionListIndex, "page")):
            def guard(*args, _name=name, **kwargs):
                if active:
                    pytest.fail(f"terminal-claim checkpoint performed a history scan via {_name}")
                return originals[_name](*args, **kwargs)

            monkeypatch.setattr(owner, name, guard)
        monkeypatch.setattr(SessionTaskStore, "preserve_terminal_cancellation_claim", observed)
        control_keys, claim_keys, sizes = set(_control(case)), set(case.terminal_claim), []
        case.agent.backend = _successor_backend(case.backend)
        for index in range(4):
            identifier = _assign_successor(case, index)
            case.scheduler.tick()
            assert case.store.session_tasks.require(identifier).status == "done"
            claim = case.store.claims.load(case.target)
            # 只保留一层原有限摘要，不形成前驱链或新的状态字段。
            assert set(claim) == claim_keys and "previous_claim" not in claim["previous_claim"]
            sizes.append(len(str(claim["previous_claim"])))
        # 只有第一次替换原claim时保存证明；之后普通后继的旧claim没有相关未结取消。
        assert checkpoints == [True, False, False, False]
        assert max(sizes) - min(sizes) < 64
        assert set(_control(case)) == control_keys | {"claim_finished"}
    finally:
        _cleanup(case)


def test_stale_claim_archive_preserves_needed_proof_then_archives_after_settlement(tmp_path):
    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        retention = 60.0
        later = case.terminal_claim["finished_at"] + retention + 1
        before = case.claim_path.read_bytes()
        assert case.store.claims.archive_stale(current=later, retention_seconds=retention) == 0
        assert case.claim_path.read_bytes() == before and _control(case)["claim_finished"] is True
        assert case.store.claims.archive_stale(current=later, retention_seconds=retention) == 0
        assert case.call(case.action, case.params)["state"] == "completed"
        assert case.store.claims.archive_stale(current=later, retention_seconds=retention) == 1
        assert not case.claim_path.exists()
        assert case.call(case.action, case.params)["state"] == "completed"
    finally:
        _cleanup(case)


def test_stale_claim_archive_checkpoint_failure_leaves_original_evidence(tmp_path, monkeypatch):
    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        retention = 60.0
        later = case.terminal_claim["finished_at"] + retention + 1
        old_claim, old_task = case.claim_path.read_bytes(), case.task_path.read_bytes()
        lock = case.claim_path.with_name(case.claim_path.name + ".lock")
        failures = _fail_write_once(monkeypatch, lambda path, payload: path == case.task_path)
        assert case.store.claims.archive_stale(current=later, retention_seconds=retention) == 0
        assert failures == [case.task_path]
        assert case.claim_path.read_bytes() == old_claim and case.task_path.read_bytes() == old_task
        assert lock.exists()
        assert case.store.claims.archive_stale(current=later, retention_seconds=retention) == 0
        assert _control(case)["claim_finished"] is True and case.claim_path.read_bytes() == old_claim
    finally:
        _cleanup(case)


def test_stale_claim_archive_does_not_act_on_unlocked_snapshot(tmp_path, monkeypatch):
    case = _start_cancelled_original(tmp_path)
    try:
        _join_original_worker(case)
        stale = deepcopy(case.terminal_claim)
        retention = 60.0
        later = stale["finished_at"] + retention + 1
        # 锁外快照仍是旧终态claim，锁内重读时已被新的运行中claim替换：不得按旧快照归档。
        replacement = case.store.claims.acquire({"thread_id": case.target, "task_id": "r8-replacement", "reason": "probe"})
        assert replacement is not None and replacement["status"] == "running"
        current = case.claim_path.read_bytes()
        reads = []
        real_read = store_claims._read_claim_report

        def first_read_is_stale(path):
            reads.append(path)
            return (deepcopy(stale), None) if len(reads) == 1 else real_read(path)

        monkeypatch.setattr(store_claims, "_read_claim_report", first_read_is_stale)
        assert case.store.claims.archive_stale(current=later, retention_seconds=retention) == 0
        assert len(reads) == 2 and case.claim_path.read_bytes() == current
    finally:
        _cleanup(case)
