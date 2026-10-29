# LLM: 原事实事务、owner及claim合同不变，历史去重只查询本批ID且不能用坏读提供假阴性。
# 模块用途: 验证调度仓库原合同及有覆盖证明的索引去重。
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_py_agent.agent.scheduler.repository as repository_module
from agent_py_agent.agent.memory_store import candidates as candidates_module
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.scheduler import event_effects as effects_module
from agent_py_agent.agent.scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
    SchedulerStateError,
)
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer, OwnerQuotaExceeded


def _repository(tmp_path, *, owner_id: str = "u-1") -> SchedulerRepository:
    return SchedulerRepository(
        tmp_path / owner_id / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id=owner_id,
        default_timezone="Asia/Shanghai",
    )


def _every(anchor: float = 1_000, seconds: int = 600) -> dict[str, object]:
    return {
        "kind": "every",
        "every_seconds": seconds,
        "anchor_at": anchor,
        "timezone": "UTC",
    }


def _create(
    repository: SchedulerRepository,
    *,
    name: str = "job",
    source_request_id: str = "request:call",
    now: float = 900,
    schedule: dict[str, object] | None = None,
    grace: int | None = None,
):
    return repository.create_job(
        SchedulerJobCreateRequest(
            name=name,
            prompt=f"execute {name}",
            thread_id="thread-1",
            source_task_id="task-1",
            schedule=schedule or _every(),
            misfire_grace_seconds=grace,
            skill_refs=[],
            source_request_id=source_request_id,
            now=now,
        )
    )


# LLM: Test-only helpers build a real persisted scheduler run and alter only its isolated claim facts.
# 函数用途: 统一准备到期 run，确保 claim fencing 用例经过真实 repository，而不是手拼模型。
def _claimed_due_run(repository: SchedulerRepository) -> dict[str, object]:
    _create(repository)
    run = repository.reserve_due_runs(now=1_000)[0]
    claimed = repository.claim_run(str(run["run_id"]), lease_seconds=10, now=1_001)
    assert claimed is not None
    return claimed


# LLM: Mutate only the pytest temporary owner ledger to model an expired runner identity.
# 函数用途: 为死亡证明测试准备可控的 lease/PID/starttime（字符串指纹或旧数字记录），避免依赖真实进程状态。
def _expire_claim(
    repository: SchedulerRepository,
    claim: dict[str, object],
    *,
    runner_pid: int,
    runner_start_time: str | float | None,
) -> None:
    store = json.loads(repository.store_path.read_text(encoding="utf-8"))
    run_id = str(claim["run_id"])
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_011.0
    run["runner_pid"] = runner_pid
    if runner_start_time is None:
        run.pop("runner_start_time", None)
    else:
        run["runner_start_time"] = runner_start_time
    repository.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_crud_cas_and_same_tool_call_deduplication(tmp_path) -> None:
    repository = _repository(tmp_path)
    created, deduped = _create(repository)
    repeated, repeated_deduped = _create(repository)
    assert deduped is False
    assert repeated_deduped is True
    assert repeated["job_id"] == created["job_id"]

    # Empty request identities are never treated as a global idempotency key.
    first, _ = _create(repository, name="no-key", source_request_id="")
    second, _ = _create(repository, name="no-key", source_request_id="")
    assert first["job_id"] != second["job_id"]

    updated = repository.update_job(
        str(created["job_id"]),
        patch={"name": "renamed"},
        expected_version=1,
        now=910,
    )
    assert updated["name"] == "renamed"
    assert updated["version"] == 2
    with pytest.raises(SchedulerConflictError):
        repository.pause_job(str(created["job_id"]), expected_version=1, now=920)
    paused = repository.pause_job(str(created["job_id"]), expected_version=2, now=920)
    resumed = repository.resume_job(
        str(created["job_id"]), expected_version=int(paused["version"]), now=930
    )
    deleted = repository.delete_job(
        str(created["job_id"]), expected_version=int(resumed["version"]), now=940
    )
    assert deleted["status"] == "deleted"


def test_past_one_shot_error_reports_current_clock_for_structured_retry(tmp_path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ScheduleValidationError) as raised:
        _create(
            repository,
            now=1_784_423_093,
            schedule={"kind": "at", "at": "1970-01-01T00:01:30Z", "timezone": "UTC"},
        )

    message = str(raised.value)
    assert "1970-01-01T00:01:30Z" in message
    assert "Current time: 2026-07-19T01:04:53Z" in message
    assert "future absolute ISO-8601" in message


def test_due_reservation_advances_before_execution_and_does_not_starve(tmp_path) -> None:
    repository = _repository(tmp_path)
    first, _ = _create(repository, name="first", source_request_id="first")
    second, _ = _create(repository, name="second", source_request_id="second")
    third, _ = _create(repository, name="third", source_request_id="third")
    repository.reserve_manual_run(str(first["job_id"]), now=995)

    reserved = repository.reserve_due_runs(now=1_000, limit=2)
    assert {run["job_id"] for run in reserved} == {second["job_id"], third["job_id"]}
    for run in reserved:
        job = repository.get_job(str(run["job_id"]))
        assert job["next_run_at"] == 1_600
        assert job["version"] == 2


def test_due_reservation_poll_without_state_change_is_read_only(tmp_path, monkeypatch) -> None:
    owner = tmp_path / "u-1"
    repository = SchedulerRepository(
        owner / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-1",
        quota_enforcer=OwnerQuotaEnforcer(owner, max_bytes=1024 * 1024),
    )
    job, _deduped = _create(repository)

    # First cover an ordinary poll before the job is due.  Then cover a due
    # job which cannot be selected because it already has an active manual
    # run.  Neither path changes durable scheduler state.
    repository.reserve_manual_run(str(job["job_id"]), now=999)
    original_store = repository.store_path.read_bytes()
    original_mtime = repository.store_path.stat().st_mtime_ns

    def fail_if_quota_scanned(_root):
        raise AssertionError("a no-op scheduler poll must not scan owner quota")

    monkeypatch.setattr(
        "agent_py_agent.agent.user_space.owner_quota.owner_logical_usage_bytes",
        fail_if_quota_scanned,
    )

    assert repository.reserve_due_runs(now=999) == []
    assert repository.reserve_due_runs(now=1_000) == []
    assert repository.store_path.read_bytes() == original_store
    assert repository.store_path.stat().st_mtime_ns == original_mtime


def test_misfire_claim_takeover_finish_and_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    repository = _repository(tmp_path)
    skipped_job, _ = _create(
        repository,
        name="skip",
        source_request_id="skip",
        grace=0,
    )
    assert repository.reserve_due_runs(now=1_001) == []
    skipped = repository.history(job_id=str(skipped_job["job_id"]))[0]
    assert skipped[0]["status"] == "skipped"
    assert skipped[0]["error_code"] == "SCHEDULER_MISFIRE_GRACE_EXPIRED"

    job, _ = _create(repository, name="run", source_request_id="run", grace=100)
    run = repository.reserve_due_runs(now=1_000)[0]
    first_claim = repository.claim_run(str(run["run_id"]), lease_seconds=10, now=1_001)
    assert first_claim is not None
    assert repository.claim_run(str(run["run_id"]), lease_seconds=10, now=1_005) is None
    takeover = repository.claim_run(str(run["run_id"]), lease_seconds=10, now=1_012)
    assert takeover is not None
    with pytest.raises(SchedulerConflictError):
        repository.finish_run(
            str(run["run_id"]),
            str(first_claim["claim_id"]),
            SchedulerRunFinish(status="done", now=1_013),
        )
    terminal = repository.finish_run(
        str(run["run_id"]),
        str(takeover["claim_id"]),
        SchedulerRunFinish(
            status="done",
            response="finished",
            delivery_status="sent",
            now=1_014,
        ),
    )
    assert terminal["status"] == "done"
    history, errors = repository.history(job_id=str(job["job_id"]))
    assert errors == []
    assert history[0]["response"] == "finished"
    assert repository.get_active_run(str(run["run_id"])) is None


def test_expired_claim_does_not_transfer_while_runner_is_alive(tmp_path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    first = _claimed_due_run(repository)
    _expire_claim(repository, first, runner_pid=71, runner_start_time="7")
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
    monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: "7")

    replacement = repository.claim_run(str(first["run_id"]), lease_seconds=10, now=1_012)

    assert replacement is None
    current = repository.get_active_run(str(first["run_id"]))
    assert current["claim_id"] == first["claim_id"]
    assert current["claim_epoch"] == first["claim_epoch"]


@pytest.mark.parametrize("unverifiable", ["missing_pid", "permission_error", "missing_start_time"])
def test_expired_claim_fails_closed_without_death_proof(tmp_path, monkeypatch, unverifiable) -> None:
    repository = _repository(tmp_path)
    first = _claimed_due_run(repository)
    if unverifiable == "missing_pid":
        _expire_claim(repository, first, runner_pid=0, runner_start_time=None)
    else:
        _expire_claim(repository, first, runner_pid=71, runner_start_time="7")
    if unverifiable == "permission_error":
        def deny_process_probe(_pid, _signal):
            raise PermissionError("process existence cannot be confirmed")

        monkeypatch.setattr(repository_module.os, "kill", deny_process_probe)
        monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: None)
    elif unverifiable == "missing_start_time":
        monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
        monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: None)

    replacement = repository.claim_run(str(first["run_id"]), lease_seconds=10, now=1_012)

    assert replacement is None
    current = repository.get_active_run(str(first["run_id"]))
    assert current["claim_id"] == first["claim_id"]
    assert current["status"] == "claimed"


def test_dead_runner_is_cas_cleared_before_one_new_epoch_claim(tmp_path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    first = _claimed_due_run(repository)
    _expire_claim(repository, first, runner_pid=71, runner_start_time="7")
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    writes: list[str] = []
    write_store = repository._write_store_unlocked

    # LLM: 观察真实 repository 持锁写出的中间状态，不替换状态迁移实现。
    # 函数用途: 证明死亡证明先把旧 claim CAS 回 queued，之后才持久化唯一新 epoch。
    def capture_write(store, admission, *, history_records=None):
        writes.append(str(store["runs"][str(first["run_id"])]["status"]))
        return write_store(store, admission, history_records=history_records)

    monkeypatch.setattr(repository, "_write_store_unlocked", capture_write)

    replacement = repository.claim_run(str(first["run_id"]), lease_seconds=10, now=1_012)

    assert replacement is not None
    assert writes == ["queued", "claimed"]
    assert replacement["claim_epoch"] == first["claim_epoch"] + 1
    assert repository.claim_run(str(first["run_id"]), lease_seconds=10, now=1_013) is None


def test_finish_and_heartbeat_reject_claim_epoch_mismatch(tmp_path) -> None:
    repository = _repository(tmp_path)
    claim = _claimed_due_run(repository)
    store = json.loads(repository.store_path.read_text(encoding="utf-8"))
    run = store["runs"][str(claim["run_id"])]
    run["claim_epoch"] = int(run.get("claim_epoch") or 0) + 1
    repository.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert repository.heartbeat_run(
        str(claim["run_id"]), str(claim["claim_id"]), lease_seconds=10, now=1_002
    ) is False
    with pytest.raises(SchedulerConflictError):
        repository.finish_run(
            str(claim["run_id"]),
            str(claim["claim_id"]),
            SchedulerRunFinish(status="done", now=1_003),
        )


def test_sleep_past_misfire_grace_keeps_one_skipped_run_per_job(tmp_path) -> None:
    repository = _repository(tmp_path)
    first, _ = _create(repository, name="first", source_request_id="first", grace=30)
    second, _ = _create(repository, name="second", source_request_id="second", grace=30)

    assert repository.reserve_due_runs(now=10_000) == []
    history, errors = repository.history()
    assert errors == []
    assert len(history) == 2
    assert {str(run["job_id"]) for run in history} == {str(first["job_id"]), str(second["job_id"])}
    assert all(run["status"] == "skipped" for run in history)
    assert repository.reserve_due_runs(now=10_001) == []
    assert len(repository.history()[0]) == 2


def test_owner_identity_and_corrupt_store_fail_closed(tmp_path) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    other = SchedulerRepository(
        repository.root,
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-2",
    )
    with pytest.raises(SchedulerStateError):
        other.list_jobs()

    repository.store_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(SchedulerStateError):
        repository.list_jobs()


def test_scheduler_store_and_history_share_owner_quota_admission(tmp_path) -> None:
    owner = tmp_path / "u-1"
    repository = SchedulerRepository(
        owner / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-1",
        quota_enforcer=OwnerQuotaEnforcer(owner, max_bytes=1),
    )

    with pytest.raises(OwnerQuotaExceeded):
        _create(repository)

    assert not repository.store_path.exists()
    assert not repository.history_path.exists()


# LLM: 计数保留原读取器的坏账和异常语义，不用模拟结果证明缓存生效。
# 函数用途: 记录目标模块真正执行的 JSONL 全量读取。
def _jsonl_read_spy(monkeypatch, module):
    original = module.read_jsonl_objects_report
    calls = []

    # LLM: 透传原读取结果，仅记录路径与调用上下文。
    # 函数用途: 统计实际全量读取入口次数。
    def read(path, **kwargs):
        calls.append((path, kwargs.get("context")))
        return original(path, **kwargs)

    monkeypatch.setattr(module, "read_jsonl_objects_report", read)
    return calls


# LLM: 完整前缀证明可重读原字节，但旧行不应重新解码；不再认可不安全的首尾锚点性能口径。
# 函数用途: 旁听真实索引解码的记录数。
def _history_decode_spy(monkeypatch, repository):
    original, decoded = repository._history_read_index._decode, []

    # LLM: 原校验、解析和数据库提交照常运行。
    # 函数用途: 收集本次真正解码的来源行。
    def decode(raw, *args, **kwargs):
        decoded.append(raw)
        return original(raw, *args, **kwargs)

    monkeypatch.setattr(repository._history_read_index, "_decode", decode)
    return decoded


# LLM: 真实finish仍先去重再追加；完整前缀校验不等于重新解码旧行。
# 函数用途: 验证原完成入口只解码新增行并保留跨批去重。
def test_opt7_finish_run_uses_incremental_history_ids(tmp_path, monkeypatch):
    repository, _home = _indexed_history_repository(tmp_path)
    claim = _claimed_due_run(repository)
    repository.history_path.write_text(json.dumps({"run_id": "srun_old", "padding": "x" * 12000}) + "\n")
    decoded = _history_decode_spy(monkeypatch, repository)
    assert repository._new_history_rows_unlocked([{"run_id": "srun_old"}]) == []
    assert len(decoded) == 1
    with repository.history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"run_id": "srun_external", "summary": "保留\u2028同一行"}, ensure_ascii=False) + "\n")
    terminal = repository.finish_run(str(claim["run_id"]), str(claim["claim_id"]), SchedulerRunFinish("done", now=1010))
    assert terminal["status"] == "done"
    assert repository._new_history_rows_unlocked([
        {"run_id": "srun_old"}, {"run_id": "srun_external"}, {"run_id": claim["run_id"]},
    ]) == []
    assert len(decoded) == 3
    assert repository._new_history_rows_unlocked([{"run_id": claim["run_id"]}]) == []
    assert len(decoded) == 3


# LLM: 替换、截断、同尺寸恢复mtime及首部改写均撤销投影，warm页不再解码旧行。
# 函数用途: 验证各种非追加变化后的正确去重和真实解码成本。
@pytest.mark.parametrize("change", ["replace", "truncate", "same-size", "anchor-rewrite"])
def test_opt7_history_ids_fall_back_after_rewrites(tmp_path, monkeypatch, change):
    repository, _home = _indexed_history_repository(tmp_path)
    original = json.dumps({"padding": "x" * 12000, "run_id": "srun_old"}) + "\n"
    repository.history_path.write_text(original)
    decoded = _history_decode_spy(monkeypatch, repository)
    assert repository._new_history_rows_unlocked([{"run_id": "srun_old"}]) == []
    stamp = repository.history_path.stat()
    updated = original.replace("srun_old", "srun_new")
    if change == "replace":
        other = repository.root / "replacement.jsonl"
        other.write_text(updated)
        other.replace(repository.history_path)
    elif change == "truncate":
        repository.history_path.write_text('{"run_id":"srun_new"}\n')
    elif change == "anchor-rewrite":
        repository.history_path.write_text(updated + '{"run_id":"srun_tail"}\n')
    else:
        repository.history_path.write_text(updated)
        os.utime(repository.history_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    expected = [{"run_id": "srun_old"}]
    assert repository._new_history_rows_unlocked(expected + [{"run_id": "srun_new"}]) == expected
    assert len(decoded) == 2 + (change == "anchor-rewrite")
    repository._new_history_rows_unlocked(expected)
    assert len(decoded) == 2 + (change == "anchor-rewrite")


# LLM: 坏行沿原过滤口径跳过，索引未知必须后备；无LF的合法末行不得丢失。
# 函数用途: 验证坏追加重复读取及未完成尾行的真实去重语义。
@pytest.mark.parametrize("suffix", ['{broken\n', '[]\n', '\ufffd\n', '{"run_id":"srun_tail"}'])
def test_opt7_history_bad_or_unterminated_tail_does_not_stick(tmp_path, monkeypatch, suffix):
    repository, _home = _indexed_history_repository(tmp_path)
    repository.history_path.write_text('{"run_id":"srun_old"}\n')
    scans = []
    original = repository_module._history_membership_fallback

    # LLM: 只旁听后备，不替换规范来源读取。
    # 函数用途: 记录不完整索引的真实后备次数。
    def fallback(*args, **kwargs):
        scans.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(repository_module, "_history_membership_fallback", fallback)
    assert repository._new_history_rows_unlocked([{"run_id": "srun_old"}]) == []
    with repository.history_path.open("a") as handle:
        handle.write(suffix)
    assert repository._new_history_rows_unlocked([{"run_id": "srun_old"}]) == []
    if suffix.endswith("\n"):
        assert len(scans) == 1
        assert repository._new_history_rows_unlocked([{"run_id": "srun_old"}]) == []
        assert len(scans) == 2
    else:
        # 合法无LF尾行可被索引；后续连写必须撤销它，不能继承旧行边界。
        assert len(scans) == 0
        assert repository._new_history_rows_unlocked([{"run_id": "srun_tail"}]) == []
        with repository.history_path.open("a") as handle:
            handle.write('{"run_id":"srun_late"}\n')
        rows = [{"run_id": "srun_old"}, {"run_id": "srun_tail"}, {"run_id": "srun_late"}]
        assert repository._new_history_rows_unlocked(rows) == rows[1:]
        assert len(scans) == 1


# LLM: 无LF末行自身合法，追加后若连成坏JSON必须失效，不能继续去重已不在权威记录中的ID。
# 函数用途: 验证无换行原尾行不会被当成安全追加边界。
def test_opt7_history_original_unterminated_row_is_not_append_boundary(tmp_path):
    repository = _repository(tmp_path)
    repository.history_path.write_text('{"run_id":"srun_old"}')
    assert repository._new_history_rows_unlocked([{"run_id": "srun_old"}]) == []
    with repository.history_path.open("a") as handle:
        handle.write('{"run_id":"srun_new"}\n')
    rows = [{"run_id": "srun_old"}, {"run_id": "srun_new"}]
    assert repository._new_history_rows_unlocked(rows) == rows


# LLM: 只返回不可变本批命中，线程并发仍使用原事务锁；多来源只共享owner唯一数据库。
# 函数用途: 迁移旧全集缓存测试并保留结果隔离、并发和有界返回覆盖。
def test_opt7_history_ids_cache_is_private_thread_safe_and_bounded(tmp_path):
    repository, home = _indexed_history_repository(tmp_path)
    repository.history_path.write_text(''.join(json.dumps({'run_id': f'srun_{n}'}) + '\n' for n in range(400)))
    requested = frozenset({'srun_1', 'absent'})
    result = _history_members(repository, requested)
    assert result == frozenset({'srun_1'})
    with pytest.raises(AttributeError):
        result.clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(rows == frozenset({'srun_1'}) for rows in pool.map(
            lambda ids: _history_members(repository, ids), [requested] * 12))
    for number in range(35):
        other, _home = _indexed_history_repository(tmp_path, relative=('data', f'history-{number}'))
        other.history_path.write_text('{"run_id":"srun_one"}\n')
        assert _history_members(other, {'srun_one', 'absent'}) == frozenset({'srun_one'})
    assert list((home / 'cache').iterdir()) == [home / 'cache' / 'owner_read_index.sqlite3']
    assert not hasattr(repository_module, '_HISTORY_RUN_IDS_CACHE')


# LLM: effect_run仍按原owner/schema解析，返回嵌套对象必须独立，不因缓存复活已变更历史。
# 函数用途: 验证副作用查询复用历史解析并即时看见同mtime内容更新。
def test_opt7_effect_run_history_cache_and_nested_isolation(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    claim = _claimed_due_run(repository)
    terminal = repository.finish_run(str(claim["run_id"]), str(claim["claim_id"]), SchedulerRunFinish("done", now=1010))
    calls = _jsonl_read_spy(monkeypatch, effects_module)
    first = repository.effect_run(terminal["run_id"])
    first["owner"]["id"] = "caller-poison"
    assert repository.effect_run(terminal["run_id"]) == terminal
    assert len(calls) == 1
    before = repository.history_path.stat()
    repository.history_path.write_text(repository.history_path.read_text().replace('"status": "done"', '"status": "fail"'))
    os.utime(repository.history_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert repository.effect_run(terminal["run_id"]) is None
    assert len(calls) == 2


# LLM: 候选写入仍走真实Service和用户原话引用，避免只测手工构造的缓存记录。
# 函数用途: 创建可回放并带嵌套来源字段的临时候选服务。
def _opt7_candidate_case(tmp_path):
    service = candidates_module.CandidateService(tmp_path / "memory" / "candidates.jsonl")
    observation = CandidateObservation(
        candidate_type="long_term_fact", content="个人电脑使用 macOS。", subject_key="device.os",
        scope=MemoryScope("personal", "personal"), origin="user_explicit",
        source_message_refs=({"message_id": "message-1", "thread_id": "thread-1", "role": "user",
                              "content_hash": "sha256:example", "quote": "个人电脑使用 macOS"},),
        observed_at="2026-10-22T10:00:00+00:00", confidence=0.99,
    )
    candidate = service.observe(observation)
    return service, observation, candidate


# LLM: from_record仍执行真实schema校验；计数只观察建模是否被缓存省掉。
# 函数用途: 统计候选完整模型解析次数。
def _candidate_model_spy(monkeypatch):
    original = candidates_module.MemoryCandidate.from_record
    calls = []

    # LLM: 绑定原classmethod后再替换，防spy递归调用自身。
    # 函数用途: 记录候选负载并透传真实构造器。
    def parse(_cls, payload):
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(candidates_module.MemoryCandidate, "from_record", classmethod(parse))
    return calls


# LLM: 缓存对象不得泄漏可变列表、scope或审核历史；冷热返回均需与源文件隔离。
# 函数用途: 验证list/get和跨Service读取省去重复解析但不共享候选对象。
def test_opt7_candidates_cache_skips_parse_and_deep_clones(tmp_path, monkeypatch):
    service, _observation, candidate = _opt7_candidate_case(tmp_path)
    calls = _candidate_model_spy(monkeypatch)
    reads = _jsonl_read_spy(monkeypatch, candidates_module)
    first = service.list()[0]
    first.content = "caller changed"
    first.scope["scope_key"] = "caller"
    first.source_message_refs[0]["message_id"] = "caller"
    first.status_history[-1]["status"] = "caller"
    assert service.get(candidate.candidate_id) == candidate
    assert candidates_module.CandidateService(service.path).list() == [candidate]
    assert len(calls) == len(reads) == 1


# LLM: 摘要必须识别同inode/尺寸/mtime的原位改写；时间戳本身不能替代候选内容版本。
# 函数用途: 验证候选内容变化和原子替换均即时使缓存失效。
@pytest.mark.parametrize("change", ["same-size", "replace", "truncate", "missing"])
def test_opt7_candidates_cache_invalidates_on_external_changes(tmp_path, monkeypatch, change):
    service, _observation, candidate = _opt7_candidate_case(tmp_path)
    calls = _candidate_model_spy(monkeypatch)
    assert service.list() == [candidate]
    before = service.path.stat()
    updated = service.path.read_text().replace('"confidence": 0.99', '"confidence": 0.98')
    if change == "replace":
        replacement = service.path.with_suffix(".new")
        replacement.write_text(updated)
        replacement.replace(service.path)
    elif change == "same-size":
        service.path.write_text(updated)
        os.utime(service.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "truncate":
        service.path.write_text("")
    else:
        service.path.unlink()
    rows = service.list()
    assert rows == [] if change in {"truncate", "missing"} else rows[0].confidence == 0.98
    count = len(calls)
    assert service.list() == rows
    assert len(calls) == count


# LLM: 坏JSON、UTF-8、非对象、schema及重复ID不能缓存为健康候选，也不能返回上代成功结果。
# 函数用途: 验证候选坏账逐次拒绝且修复后重新读取。
@pytest.mark.parametrize("bad", ["json", "utf8", "shape", "schema", "duplicate"])
def test_opt7_candidates_cache_never_hides_corruption(tmp_path, monkeypatch, bad):
    service, _observation, candidate = _opt7_candidate_case(tmp_path)
    assert service.list() == [candidate]
    original = service.path.read_bytes()
    corrupt = {
        "json": b"{broken\n", "utf8": b"\xff\n", "shape": b"[]\n",
        "schema": b"{}\n", "duplicate": original + original,
    }[bad]
    service.path.write_bytes(corrupt)
    reads = _jsonl_read_spy(monkeypatch, candidates_module)
    messages = []
    for _ in range(2):
        with pytest.raises(candidates_module.CandidateStoreCorruptError) as error:
            service.list()
        messages.append(str(error.value))
    assert messages[0] == messages[1]
    assert len(reads) == 2
    service.path.write_bytes(original)
    calls = _candidate_model_spy(monkeypatch)
    assert service.get(candidate.candidate_id) == candidate
    assert service.get(candidate.candidate_id) == candidate
    assert len(calls) == 1


# LLM: 原写入和配额锁不能被缓存绕过；写后读取必须反映原合并规则的新状态。
# 函数用途: 验证observe和审核写入使候选解析缓存失效。
def test_opt7_candidate_writes_invalidate_cached_models(tmp_path, monkeypatch):
    from dataclasses import replace

    service, observation, candidate = _opt7_candidate_case(tmp_path)
    assert service.list() == [candidate]
    updated = service.observe(replace(observation, source_message_refs=(
        {**observation.source_message_refs[0], "message_id": "message-2"},
    )))
    assert updated.occurrence_count == 2
    calls = _candidate_model_spy(monkeypatch)
    assert service.get(candidate.candidate_id) == updated
    assert service.get(candidate.candidate_id) == updated
    assert len(calls) == 1
    reviewed = service.transition(candidate.candidate_id, "approved", reviewer="test")
    assert service.get(candidate.candidate_id) == reviewed
    assert len(calls) == 2


# LLM: 缓存总路径数和原始字节预算都必须有界，超预算仅丢性能，不丢候选或关闭校验。
# 函数用途: 验证候选缓存LRU和字节预算回退。
def test_opt7_candidate_cache_path_and_byte_limits(tmp_path, monkeypatch):
    cache = getattr(candidates_module, "_CANDIDATE_LOAD_CACHE", None)
    assert cache is not None
    for number in range(10):
        service, _observation, candidate = _opt7_candidate_case(tmp_path / str(number))
        assert service.list() == [candidate]
    assert len(cache) <= 8
    assert sum(entry[0][1] for entry in cache.values()) <= 32 * 1024 * 1024
    monkeypatch.setattr(candidates_module, "_CANDIDATE_LOAD_CACHE_MAX_BYTES", 0)
    service, _observation, candidate = _opt7_candidate_case(tmp_path / "over-budget")
    calls = _candidate_model_spy(monkeypatch)
    assert service.list() == service.list() == [candidate]
    assert len(calls) == 2


# LLM: 三个读取入口共享同一历史解析，但owner/schema、取消集合和公开返回隔离仍由原入口负责。
# 函数用途: 验证历史列表和待取消查询不会绕过副作用历史缓存重解析全文件。
def test_opt7_history_readers_share_cache_without_exposing_rows(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    claim = _claimed_due_run(repository)
    terminal = repository.finish_run(str(claim["run_id"]), str(claim["claim_id"]), SchedulerRunFinish("done", now=1010))
    store = json.loads(repository.store_path.read_text())
    store["jobs"][terminal["job_id"]]["cancel_pending_run_ids"] = [terminal["run_id"]]
    repository.store_path.write_text(json.dumps(store))
    effect_reads = _jsonl_read_spy(monkeypatch, effects_module)
    repo_reads = _jsonl_read_spy(monkeypatch, repository_module)
    assert repository.effect_run(terminal["run_id"]) == terminal
    rows, errors = repository.history()
    assert errors == [] and rows == [terminal]
    rows[0]["owner"]["id"] = "caller"
    pending = repository.pending_cancellation_runs()
    assert pending == [terminal]
    pending[0]["owner"]["id"] = "caller"
    assert repository.effect_run(terminal["run_id"]) == terminal
    assert len(effect_reads) + len(repo_reads) == 1


# LLM: 报告缓存按原始文件尺寸做全局预算；超限与坏行只能回退，不能截断历史或重用旧错误上下文。
# 函数用途: 验证历史报告8路径/16MiB上限及坏账逐次报告。
def test_opt7_history_report_cache_limits_and_corruption(tmp_path, monkeypatch):
    for number in range(10):
        path = tmp_path / f"{number}.jsonl"
        path.write_text('{"run_id":"srun_one"}\n')
        assert effects_module._cached_history_report(path, context="test").records == [{"run_id": "srun_one"}]
    cache = effects_module._HISTORY_REPORT_CACHE
    assert len(cache) <= 8
    assert sum(entry[0][1] for entry in cache.values()) <= 16 * 1024 * 1024
    reads = _jsonl_read_spy(monkeypatch, effects_module)
    path = tmp_path / "corrupt.jsonl"
    path.write_text('{broken\n')
    for context in ("first", "second"):
        report = effects_module._cached_history_report(path, context=context)
        assert report.records == [] and len(report.load_errors) == 1
        assert report.load_errors[0]["context"] == context
    assert len(reads) == 2
    path.write_text('{"run_id":"srun_one"}\n')
    monkeypatch.setattr(effects_module, "_HISTORY_REPORT_CACHE_MAX_BYTES", 0)
    for _ in range(2):
        assert effects_module._cached_history_report(path, context="test").records == [{"run_id": "srun_one"}]
    assert len(reads) == 4


# LLM: open与fstat之间发生同目录原子replace时旧inode的st_nlink降为0，必须标transient_open_race重试；直接删除或多硬链仍硬拒。
# 函数用途: 验证read_atomic_store_bytes在dir_fd及portable分支的行为，以及open到fstat窗口的瞬态/非瞬态区分。
def test_store_snapshot_open_to_fstat_atomic_replace_retries_transient_race(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs as fs
    from agent_py_agent.agent.scheduler.store_snapshot import read_atomic_store_bytes

    supported = fs._supports_dir_fd()
    monkeypatch.setattr(fs, "_supports_dir_fd", lambda: supported)
    store = tmp_path / "store.json"
    store.write_bytes(b'{"v":1}')
    if not supported:
        assert read_atomic_store_bytes(store) == b'{"v":1}'
        return
    original_open = os.open
    calls = []

    def race_open(name, flags, mode=0o777, **kwargs):
        fd = original_open(name, flags, mode, **kwargs)
        if name == "store.json" and not calls:
            calls.append(1)
            replacement = tmp_path / "store.json.tmp"
            replacement.write_bytes(b'{"v":2}')
            os.replace(replacement, store)
        return fd

    monkeypatch.setattr(os, "open", race_open)
    assert read_atomic_store_bytes(store) == b'{"v":2}'
    assert len(calls) == 1

    hard_calls = []
    extra = tmp_path / "extra.link"

    def hardlink_open(name, flags, mode=0o777, **kwargs):
        fd = original_open(name, flags, mode, **kwargs)
        if name == "store.json" and not hard_calls:
            hard_calls.append(1)
            try:
                os.link(store, extra)
            except PermissionError:
                os.unlink(store)
        return fd

    monkeypatch.setattr(os, "open", hardlink_open)
    with pytest.raises(fs.NoFollowPathError) as caught:
        read_atomic_store_bytes(store)
    assert caught.value.transient_open_race is False and len(hard_calls) == 1


# LLM: 保留小文件追加、跨4KiB和首部改写加追加覆盖，用真实索引解码代替已删除全集helper。
# 函数用途: 验证增量只解码新增行，前缀改写撤销原membership。
def test_history_run_ids_small_extend_updates_head_digest(tmp_path, monkeypatch):
    repository, _home = _indexed_history_repository(tmp_path)
    history = repository.history_path
    history.write_text('{"run_id":"r1"}\n', encoding="utf-8")
    reads = _history_decode_spy(monkeypatch, repository)
    assert _history_members(repository, {'r1', 'absent'}) == frozenset({'r1'})
    assert len(reads) == 1
    with history.open("ab") as handle:
        handle.write(b'{"run_id":"r2"}\n')
    assert _history_members(repository, {'r1', 'r2'}) == frozenset({'r1', 'r2'})
    assert _history_members(repository, {'r2'}) == frozenset({'r2'})
    assert len(reads) == 2
    with history.open("ab") as handle:
        handle.write((json.dumps({"run_id": "r3", "pad": "x" * 5000}) + "\n").encode("utf-8"))
    assert _history_members(repository, {'r1', 'r2', 'r3'}) == frozenset({'r1', 'r2', 'r3'})
    assert _history_members(repository, {'r3'}) == frozenset({'r3'})
    assert len(reads) == 3

    large = history
    old_line = json.dumps({"run_id": "old-id", "pad": "x" * 12000}) + "\n"
    new_line = json.dumps({"run_id": "new-id", "pad": "x" * 12000}) + "\n"
    large.write_text(old_line, encoding="utf-8")
    assert _history_members(repository, {'old-id', 'new-id', 'append'}) == frozenset({'old-id'})
    with large.open("r+b") as handle:
        handle.write(new_line.encode("utf-8"))
        handle.seek(0, 2)
        handle.write(b'{"run_id":"append"}\n')
    assert _history_members(repository, {'old-id', 'new-id', 'append'}) == frozenset({'new-id', 'append'})
    assert len(reads) == 6


# LLM: home只来自显式配额对象，构造不应提前创建读数据库。
# 函数用途: 创建带可信owner根的调度测试实例。
def _indexed_history_repository(tmp_path, *, relative=('data', 'scheduler')):
    home = tmp_path / 'owner'
    repository = SchedulerRepository(home.joinpath(*relative), owner_provider='feishu',
        owner_kind='user', owner_id='u-1', quota_enforcer=OwnerQuotaEnforcer(home, max_bytes=0))
    assert getattr(repository, '_history_read_index', None) is not None
    return repository, home


# LLM: 测试保持原变更事务锁序，不建立新的写方协议。
# 函数用途: 在正式事务内取得本批去重身份。
def _history_members(repository, ids):
    membership = getattr(repository, '_history_membership_unlocked', None)
    assert callable(membership), 'batch membership is not wired'
    with repository._mutation_scope():
        return membership(frozenset(ids))


# LLM: 索引只解码增量，本批结果不能含其它lifetime ID；源中部改写仍必须重建。
# 函数用途: 验证增量解码、固定owner数据库与完整前缀校验。
def test_indexed_history_batch_and_middle_rewrite(tmp_path, monkeypatch):
    repository, home = _indexed_history_repository(tmp_path)
    assert not (home / 'cache').exists()
    index, decoded = repository._history_read_index, []
    original = index._decode

    # LLM: 真实解码和来源检查照常执行，不用计时替代性能证据。
    # 函数用途: 统计实际来源解码次数。
    def decode(*args, **kwargs):
        decoded.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(index, '_decode', decode)
    head = json.dumps({'run_id': 'head', 'pad': 'h' * 5000}) + '\n'
    middle = json.dumps({'run_id': 'old-middle', 'pad': 'm' * 5000}) + '\n'
    tail = json.dumps({'run_id': 'tail', 'pad': 't' * 5000}) + '\n'
    repository.history_path.write_text(head + middle + tail)
    assert _history_members(repository, {'old-middle', 'absent'}) == frozenset({'old-middle'})
    assert _history_members(repository, {'tail'}) == frozenset({'tail'})
    assert len(decoded) == 3
    with repository.history_path.open('ab') as handle:
        handle.write(b'{"run_id":"added"}\n')
    assert _history_members(repository, {'added'}) == frozenset({'added'})
    assert len(decoded) == 4
    with repository.history_path.open('r+b') as handle:
        handle.seek(len(head.encode()))
        handle.write(middle.replace('old-middle', 'new-middle').encode())
        handle.seek(0, 2)
        handle.write(b'{"run_id":"again"}\n')
    assert _history_members(repository, {'old-middle', 'new-middle', 'again'}) == frozenset({'new-middle', 'again'})
    assert (home / 'cache' / 'owner_read_index.sqlite3').is_file()
    assert not (repository.root / 'cache').exists()


# LLM: 不完整推进、不可用查询和落后边界都不能给当前写事务负membership。
# 函数用途: 验证读索引故障回到真实规范流水，且不修改源。
@pytest.mark.parametrize('fault', ['partial', 'unavailable', 'stale', 'membership'])
def test_indexed_history_falls_back_without_false_negative(tmp_path, monkeypatch, fault):
    repository, _home = _indexed_history_repository(tmp_path)
    repository.history_path.write_text('{"run_id":"persisted"}\n')
    index = repository._history_read_index
    view = index.advance('scheduler', repository._history_source_parts)
    assert view['status'] == 'complete'
    changed = dict(view)
    if fault in {'partial', 'unavailable'}:
        changed['status'] = fault
    elif fault == 'stale':
        changed['through'] -= 1
    monkeypatch.setattr(index, 'advance', lambda *_args, **_kwargs: changed)
    if fault == 'membership':
        monkeypatch.setattr(index, 'membership', lambda *_: {
            'ids': frozenset(), 'coverage': {**view, 'status': 'unavailable'}})
    before = repository.history_path.read_bytes()
    assert _history_members(repository, {'persisted', 'fresh'}) == frozenset({'persisted'})
    assert repository.history_path.read_bytes() == before


# LLM: 索引查询期间即使出现外部追加，旧through的负结果也必须撤销。
# 函数用途: 验证本次规范提交边界在membership之后再次检查。
def test_indexed_history_rechecks_source_after_membership(tmp_path, monkeypatch):
    repository, _home = _indexed_history_repository(tmp_path)
    repository.history_path.write_text('{"run_id":"old"}\n')
    index = repository._history_read_index
    original = index.membership

    # LLM: 先取得真实结果，再对规范历史追加真实行。
    # 函数用途: 建立查询与返回之间的来源变化窗口。
    def changed(view, ids):
        result = original(view, ids)
        with repository.history_path.open('ab') as handle:
            handle.write(b'{"run_id":"late"}\n')
        return result

    monkeypatch.setattr(index, 'membership', changed)
    assert _history_members(repository, {'late'}) == frozenset({'late'})


# LLM: 去重保持原dict/run_id与同批重复语义，不套用公开history的owner/schema过滤。
# 函数用途: 验证生产接点只请求本批ID而非复制全集。
def test_new_history_rows_passes_only_batch_to_membership(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    original = getattr(repository, '_history_membership_unlocked', None)
    assert callable(original)
    repository.history_path.write_text('{"run_id":"present","owner":{"id":"other"}}\n{"unrelated":true}\n')
    observed = []

    # LLM: 不伪造去重结果，旁听生产实际传参。
    # 函数用途: 记录实际批次身份。
    def membership(ids):
        observed.append(ids)
        return original(ids)

    monkeypatch.setattr(repository, '_history_membership_unlocked', membership)
    rows = [{'run_id': 'present'}, {'run_id': 'fresh'}, {'run_id': 'fresh'}, {}]
    with repository._mutation_scope():
        assert repository._new_history_rows_unlocked(rows) == rows[1:3]
    assert observed == [frozenset({'present', 'fresh', ''})]


# LLM: 读不出历史不能把本批运行当成未出现；首次缺失和曾见来源删除也不相同。
# 函数用途: 验证后备路径对UTF8、nofollow及删除失败关闭。
@pytest.mark.parametrize('fault', ['utf8', 'symlink', 'deletion'])
def test_history_read_failure_blocks_dedupe_decision(tmp_path, fault):
    repository = _repository(tmp_path)
    assert repository._new_history_rows_unlocked([{'run_id': 'fresh'}]) == [{'run_id': 'fresh'}]
    repository.history_path.write_text('{"run_id":"old"}\n')
    assert repository._new_history_rows_unlocked([{'run_id': 'old'}]) == []
    if fault == 'utf8':
        with repository.history_path.open('ab') as handle:
            handle.write(b'\xff\n')
    elif fault == 'symlink':
        moved = repository.history_path.with_name('moved.jsonl')
        repository.history_path.rename(moved)
        repository.history_path.symlink_to(moved)
    else:
        repository.history_path.unlink()
    with pytest.raises(SchedulerStateError) as caught:
        repository._new_history_rows_unlocked([{'run_id': 'fresh'}])
    assert caught.value.error_code == 'SCHEDULER_HISTORY_READ_FAILED'


# LLM: 单条查询(get/locked_snapshot)命中缓存时只复制匹配项，避免大候选账本在晋升循环中每次深拷贝全量列表。
# 函数用途: 验证128MiB候选缓存上限，以及get/locked_snapshot只深拷贝目标候选。
def test_candidate_get_and_locked_snapshot_single_deepcopy_on_cache_hit(tmp_path, monkeypatch):
    assert candidates_module._CANDIDATE_LOAD_CACHE_MAX_BYTES == 128 * 1024 * 1024
    service, _observation, _first = _opt7_candidate_case(tmp_path / "single-copy")
    second = service.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="第二条候选用于验证单条深拷贝。",
            subject_key="device.editor",
            scope=MemoryScope("personal", "personal"),
            origin="user_explicit",
            source_message_refs=(
                {
                    "message_id": "message-2",
                    "thread_id": "thread-1",
                    "role": "user",
                    "content_hash": "sha256:example2",
                    "quote": "第二条候选用于验证单条深拷贝",
                },
            ),
            observed_at="2026-10-22T10:05:00+00:00",
            confidence=0.95,
        )
    )
    assert len(service.list()) == 2
    real_deepcopy = candidates_module.deepcopy
    copied_ids: list[str] = []

    def spy_deepcopy(value, *args, **kwargs):
        if isinstance(value, candidates_module.MemoryCandidate):
            copied_ids.append(value.candidate_id)
        return real_deepcopy(value, *args, **kwargs)

    monkeypatch.setattr(candidates_module, "deepcopy", spy_deepcopy)
    monkeypatch.setattr(
        candidates_module,
        "read_jsonl_objects_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache hit should avoid disk reload")),
    )
    fetched = service.get(second.candidate_id)
    assert fetched == second
    assert copied_ids == [second.candidate_id]
    fetched.content = "mutated-by-caller"
    copied_ids.clear()
    with service.locked_snapshot(second) as current:
        assert current == second
        assert current.content == second.content
    assert copied_ids == [second.candidate_id]
    with pytest.raises(KeyError):
        service.get("mc_missing")
