from __future__ import annotations

"""SLP-2A scheduler claim 栅栏回归：租约过期后只有死亡证明才清除旧 claim。

活着或身份不可核验时保留原 claim；死亡后先转 queued，再用新 epoch 唯一领取。
"""

import json

import pytest

import agent_py_agent.agent.scheduler.recovery as recovery_module
import agent_py_agent.agent.scheduler.repository as repository_module
from agent_py_agent.agent.scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)


def _repository(tmp_path) -> SchedulerRepository:
    return SchedulerRepository(
        tmp_path / "u-1" / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="u-1",
        default_timezone="Asia/Shanghai",
    )


def _every(anchor: float = 1_000, seconds: int = 600) -> dict[str, object]:
    return {"kind": "every", "every_seconds": seconds, "anchor_at": anchor}


def _make_claimed_run(repo: SchedulerRepository) -> str:
    repo.create_job(
        SchedulerJobCreateRequest(
            name="任务",
            prompt="run",
            thread_id="th-1",
            source_task_id="t-1",
            schedule=_every(anchor=1_000, seconds=60),
            now=1_000.0,
        )
    )
    due = repo.reserve_due_runs(now=1_061.0)  # 刚 due(lateness=1<grace), 避开 misfire
    run_id = str(due[0]["run_id"])
    repo.claim_run(run_id, lease_seconds=10, now=1_062.0)
    return run_id


# LLM: Change only a temporary scheduler ledger so recovery tests use deterministic process identities.
# 函数用途: 在隔离存储里注入过期 lease 与假 PID/starttime，不探测真实 runner。
def _force_crash_state(repo: SchedulerRepository, run_id: str, *, pid: int) -> None:
    """模拟崩溃残留: 租约过期 + 指定 runner_pid。"""
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0  # 过期(now=1_006 时)
    run["runner_pid"] = pid
    run["runner_start_time"] = "7"
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_dead_runner_is_cleared_then_reclaimed_with_one_new_epoch(tmp_path, monkeypatch) -> None:
    """死亡证明先清除旧 claim；只有一次新 epoch 可重新领取。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    first = repo.get_active_run(run_id)
    _force_crash_state(repo, run_id, pid=71)
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == [run_id]
    assert repo.recover_interrupted_executions(now=1_006.5) == [], "第二个回收者看到已清成 queued，不再动"
    queued = repo.get_active_run(run_id)
    assert queued is not None
    assert queued["status"] == "queued" and queued["claim_id"] == ""
    assert queued["claim_epoch"] == first["claim_epoch"]

    replacement = repo.claim_run(run_id, lease_seconds=10, now=1_007.0)
    assert replacement is not None
    assert replacement["claim_epoch"] == first["claim_epoch"] + 1
    assert repo.claim_run(run_id, lease_seconds=10, now=1_008.0) is None
    assert repo.heartbeat_run(run_id, str(first["claim_id"]), lease_seconds=10, now=1_009) is False
    with pytest.raises(SchedulerConflictError):
        repo.finish_run(
            run_id,
            str(first["claim_id"]),
            SchedulerRunFinish(status="done", now=1_010),
        )
    assert repo.get_active_run(run_id) == replacement


# 函数用途: 已 mark running 的 run 执行者死亡后落 unknown 终态，不重新排队、不能再领取（SLP-2B 接线前不自动重跑）。
@pytest.mark.parametrize("entry", ["recover", "claim"])
def test_dead_runner_after_running_becomes_unknown_not_requeued(tmp_path, monkeypatch, entry) -> None:
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    first = repo.get_active_run(run_id)
    repo.mark_run_running(run_id, str(first["claim_id"]), now=1_063.0)
    _force_crash_state(repo, run_id, pid=71)
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")

    if entry == "recover":
        assert repo.recover_interrupted_executions(now=1_006.0) == [run_id]
    else:
        assert repo.claim_run(run_id, lease_seconds=10, now=1_006.0) is None
    assert repo.get_active_run(run_id) is None, "unknown 是终态"
    assert repo.claim_run(run_id, lease_seconds=10, now=1_007.0) is None
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    assert run_id not in store["runs"], "unknown 终态进入正常历史，不留在活动账本"
    history, errors = repo.history()
    assert errors == [] and history[0]["run_id"] == run_id and history[0]["status"] == "unknown"


def test_live_process_keeps_state_fail_closed(tmp_path, monkeypatch) -> None:
    """租约过期但进程存活 -> 保持原态(fail-closed 不猜), 不归 unknown。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    _force_crash_state(repo, run_id, pid=71)
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
    monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: "7")

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == []  # 未证实死亡, 不动
    run = repo.get_active_run(run_id)
    assert run is not None
    assert run["status"] == "claimed"  # 状态不变


def test_clean_store_no_recovery(tmp_path) -> None:
    """无过期 claim -> 无恢复动作(无噪音)。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)  # claim 未过期(lease=10, expires=1012)
    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == []
    assert repo.get_active_run(run_id)["status"] == "claimed"


# ---------------------------------------------------------------- P1-4 收口(seq1562): fail-closed 三情形

def test_missing_pid_keeps_state_fail_closed(tmp_path) -> None:
    """runner_pid 缺失 -> 不可证实, 保持原态(不归 unknown)。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0  # 过期
    run.pop("runner_pid", None)  # 身份缺失
    run.pop("runner_start_time", None)
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert repo.recover_interrupted_executions(now=1_006.0) == []
    assert repo.get_active_run(run_id)["status"] == "claimed"  # fail-closed 保持


def test_pid_reuse_detected_by_start_time_then_requeued(tmp_path, monkeypatch) -> None:
    """pid 存活但 start_time 不匹配(pid 复用) -> 旧 claim 清成 queued。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    first = repo.get_active_run(run_id)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0
    run["runner_pid"] = 71  # pid 存活(复用场景)
    run["runner_start_time"] = "1"  # 记录的启动指纹(旧代)
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # 当前进程实际 start_time 与记录不同(模拟 pid 复用): 死亡证明成立
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
    monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: "999")

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == [run_id]  # pid 复用 = 原进程已死
    queued = repo.get_active_run(run_id)
    assert queued["status"] == "queued" and queued["claim_id"] == ""
    replacement = repo.claim_run(run_id, lease_seconds=10, now=1_007.0)
    assert replacement["claim_epoch"] == first["claim_epoch"] + 1


# LLM: macOS 没有 /proc，指纹是 ps lstart 字符串；同 pid 但指纹不同同样构成死亡证明（schedstart 跨平台口径）。
# 函数用途: 验证非 Linux 的字符串指纹在可读且不同时判死并重排队，不需要真实 macOS。
def test_macos_style_fingerprint_mismatch_proves_pid_reuse(tmp_path, monkeypatch) -> None:
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    first = repo.get_active_run(run_id)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0
    run["runner_pid"] = 71
    run["runner_start_time"] = "Fri Oct  3 09:00:00 2026"
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")
    monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: "Sat Oct  4 10:00:00 2026")

    recovered = repo.recover_interrupted_executions(now=1_006.0)
    assert recovered == [run_id]  # 同 pid 但指纹不同 = 原进程已死
    queued = repo.get_active_run(run_id)
    assert queued["status"] == "queued" and queued["claim_id"] == ""
    replacement = repo.claim_run(run_id, lease_seconds=10, now=1_007.0)
    assert replacement["claim_epoch"] == first["claim_epoch"] + 1


# LLM: 旧落盘记录是 /proc ticks 数字；只有当前指纹可转数字（Linux）时才比较，转不了（macOS lstart）不可核验、绝不判死。
# 函数用途: 验证旧数字记录的三种兼容结果：不可比保持、可比且相等保持、可比且不同判死。
def test_legacy_numeric_record_only_compared_when_convertible(tmp_path, monkeypatch) -> None:
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    _force_crash_state(repo, run_id, pid=71)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    store["runs"][run_id]["runner_start_time"] = 7.0  # 旧口径数字记录
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "alive")

    # macOS 形态：当前指纹是 ps lstart，转不了数字 → 不可核验，保持原态
    monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: "Fri Oct  3 09:00:00 2026")
    assert repo.recover_interrupted_executions(now=1_006.0) == []
    assert repo.get_active_run(run_id)["status"] == "claimed"

    # Linux 形态：当前指纹是同值 ticks 字符串，可转数字且相等 → 存活
    monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: "7")
    assert repo.recover_interrupted_executions(now=1_006.0) == []

    # Linux 形态：可转但不同 → pid 复用判死
    monkeypatch.setattr(repository_module, "process_start_time", lambda _pid: "999")
    assert repo.recover_interrupted_executions(now=1_006.0) == [run_id]
    assert repo.get_active_run(run_id)["status"] == "queued"


def test_unverifiable_oserror_keeps_state_fail_closed(tmp_path, monkeypatch) -> None:
    """os.kill 抛非 ProcessLookupError 的 OSError(不可判定) -> 保持原态。"""
    repo = _repository(tmp_path)
    run_id = _make_claimed_run(repo)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0
    run["runner_pid"] = 71
    store["runs"][run_id] = run
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    def _raising_kill(pid, sig):  # 非 ProcessLookupError 的 OSError(不可判定)
        raise OSError("unverifiable")

    monkeypatch.setattr(repository_module.os, "kill", _raising_kill)
    assert repo.recover_interrupted_executions(now=1_006.0) == []
    assert repo.get_active_run(run_id)["status"] == "claimed"  # fail-closed 保持


def _prepare_dead_running_batch(repo: SchedulerRepository, count: int) -> tuple[dict, list[str]]:
    """创建一条样本终态记录，以及 count 个租约过期并进入 running 的运行记录。"""
    repo.create_job(
        SchedulerJobCreateRequest(
            name="seed",
            prompt="run",
            thread_id="th-seed",
            source_task_id="t-seed",
            source_request_id="req-seed",
            schedule=_every(anchor=1_000, seconds=600),
            now=999.0,
        )
    )
    seed_run = repo.reserve_due_runs(now=1_000.0)[0]
    seed_claim = repo.claim_run(str(seed_run["run_id"]), lease_seconds=10, now=1_001.0)
    assert seed_claim is not None
    repo.mark_run_running(str(seed_run["run_id"]), str(seed_claim["claim_id"]), now=1_002.0)
    seed_terminal = repo.finish_run(
        str(seed_run["run_id"]),
        str(seed_claim["claim_id"]),
        SchedulerRunFinish(status="done", response="seed-done", now=1_003.0),
    )

    for idx in range(count):
        repo.create_job(
            SchedulerJobCreateRequest(
                name=f"worker-{idx}",
                prompt="run",
                thread_id=f"th-{idx}",
                source_task_id=f"t-{idx}",
                source_request_id=f"req-batch-{idx}",
                schedule=_every(anchor=1_061, seconds=60),
                now=1_060.0,
            )
        )
    due = repo.reserve_due_runs(now=1_061.0, limit=count + 5)
    assert len(due) == count
    run_ids: list[str] = []
    for row in due:
        run_id = str(row["run_id"])
        claim = repo.claim_run(run_id, lease_seconds=10, now=1_062.0)
        assert claim is not None
        repo.mark_run_running(run_id, str(claim["claim_id"]), now=1_063.0)
        run_ids.append(run_id)

    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    for run_id in run_ids:
        store["runs"][run_id]["claim_expires_at"] = 1_005.0
        store["runs"][run_id]["runner_pid"] = 71
        store["runs"][run_id]["runner_start_time"] = "7"
    repo.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return seed_terminal, run_ids


@pytest.mark.parametrize("dead_count", [20, 40])
def test_batch_dead_running_recovery_streams_history_once_with_bounded_matches(
    tmp_path, monkeypatch, dead_count: int
) -> None:
    """批量恢复 20/40 个已开跑死亡运行时，仅流式扫描一次历史且保留 O(K) 匹配结果。"""
    repo = _repository(tmp_path)
    seed_terminal, dead_run_ids = _prepare_dead_running_batch(repo, dead_count)

    history_rows = 3_000
    with repo.history_path.open("w", encoding="utf-8") as handle:
        for idx in range(history_rows):
            handle.write(
                json.dumps(
                    {**seed_terminal, "run_id": f"srun_hist_{idx:05d}"},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    assert not hasattr(recovery_module, "read_jsonl_objects_report")

    fallback_accept_calls: list[int] = []
    accept_invocations: list[str] = []
    batch_sizes: list[int] = []
    single_calls: list[str] = []

    original_fallback = repository_module._history_membership_fallback
    original_existing_terminals = recovery_module.existing_terminals
    original_existing_terminal = recovery_module.existing_terminal

    def _spy_fallback(path, requested, *, allow_missing, accept=None):
        if accept is not None:
            fallback_accept_calls.append(len(requested))

            def _wrapped_accept(raw):
                accept_invocations.append(str(raw.get("run_id") or ""))
                return accept(raw)

            return original_fallback(
                path, requested, allow_missing=allow_missing, accept=_wrapped_accept
            )
        return original_fallback(path, requested, allow_missing=allow_missing, accept=None)

    def _spy_existing_terminals(repository_arg, runs_arg):
        result = original_existing_terminals(repository_arg, runs_arg)
        batch_sizes.append(len(result))
        return result

    def _spy_existing_terminal(repository_arg, run_arg):
        single_calls.append(str(run_arg.get("run_id") or ""))
        return original_existing_terminal(repository_arg, run_arg)

    monkeypatch.setattr(repository_module, "_history_membership_fallback", _spy_fallback)
    monkeypatch.setattr(recovery_module, "existing_terminals", _spy_existing_terminals)
    monkeypatch.setattr(recovery_module, "existing_terminal", _spy_existing_terminal)

    recovered = repo.recover_interrupted_executions(now=1_006.0)

    assert sorted(recovered) == sorted(dead_run_ids)
    assert single_calls == []
    assert fallback_accept_calls == [dead_count]
    assert accept_invocations == []  # 3000 条无关历史行不得触发 accept 解析
    assert batch_sizes == [0]
    assert json.loads(repo.store_path.read_text(encoding="utf-8"))["runs"] == {}

    persisted = [
        json.loads(line)
        for line in repo.history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(persisted) == history_rows + dead_count
    by_id = {row["run_id"]: row for row in persisted}
    for run_id in dead_run_ids:
        assert by_id[run_id]["status"] == "unknown"
        assert by_id[run_id]["error_code"] == "SCHEDULER_RUNNER_DIED"
        assert by_id[run_id]["claim_epoch"] == 1


def test_batch_recover_runs_preserves_persisted_terminals_in_single_scan_and_dedupes(
    tmp_path, monkeypatch
) -> None:
    """同批 20 个死亡运行中部分已有持久化终态时，仅扫描一次且不重复追加已存在终态。"""
    repo = _repository(tmp_path)
    seed_terminal, dead_run_ids = _prepare_dead_running_batch(repo, 20)
    store = json.loads(repo.store_path.read_text(encoding="utf-8"))
    persisted_ids = dead_run_ids[:2]
    persisted_rows = [
        {
            **store["runs"][run_id],
            "status": "done",
            "ended_at": 1_004.0,
            "updated_at": 1_004.0,
            "claim_id": "",
            "claim_expires_at": 0.0,
            "response": f"persisted-{idx}",
        }
        for idx, run_id in enumerate(persisted_ids)
    ]
    with repo.history_path.open("w", encoding="utf-8") as handle:
        for idx in range(2_500):
            handle.write(
                json.dumps({**seed_terminal, "run_id": f"srun_pad_{idx:05d}"}, sort_keys=True) + "\n"
            )
            if idx == 500:
                handle.write(json.dumps(persisted_rows[0], sort_keys=True) + "\n")
            if idx == 1_500:
                handle.write(json.dumps(persisted_rows[1], sort_keys=True) + "\n")
                # 后面的错 epoch 行不得覆盖前面精确匹配的终态
                handle.write(
                    json.dumps(
                        {**persisted_rows[1], "claim_epoch": 2, "response": "wrong-epoch"},
                        sort_keys=True,
                    )
                    + "\n"
                )

    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    accept_calls: list[int] = []
    accept_rows: list[str] = []
    original_fallback = repository_module._history_membership_fallback

    def _spy_fallback(path, requested, *, allow_missing, accept=None):
        if accept is not None:
            accept_calls.append(len(requested))

            def _wrapped(raw):
                accept_rows.append(str(raw.get("run_id") or ""))
                return accept(raw)

            return original_fallback(path, requested, allow_missing=allow_missing, accept=_wrapped)
        return original_fallback(path, requested, allow_missing=allow_missing, accept=None)

    monkeypatch.setattr(repository_module, "_history_membership_fallback", _spy_fallback)
    recovered = recovery_module.recover_runs(repo, current=1_006.0)

    assert sorted(recovered) == sorted(dead_run_ids)
    assert accept_calls == [20]
    assert sorted(accept_rows) == sorted([persisted_ids[0], persisted_ids[1], persisted_ids[1]])

    lines = [
        json.loads(line)
        for line in repo.history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # 2500 条填充 + 2 条既有终态 + 1 条错 epoch 干扰 + 18 条新落盘 unknown（2 条已存在不得重复追加）
    assert len(lines) == 2_500 + 3 + 18
    counts = {}
    for row in lines:
        counts[row["run_id"]] = counts.get(row["run_id"], 0) + 1
    assert counts[persisted_ids[0]] == 1
    assert counts[persisted_ids[1]] == 2  # 原 epoch=1 与干扰 epoch=2 各一条，未追加 unknown


def test_settle_dead_run_single_run_backward_compatibility_and_empty_map(tmp_path, monkeypatch) -> None:
    """单条 settle_dead_run(repo, store, run, current=...) 向后兼容，且显式空映射 {} 不退回扫盘。"""
    repo = _repository(tmp_path / "single")
    run_id = _make_claimed_run(repo)
    first = repo.get_active_run(run_id)
    repo.mark_run_running(run_id, str(first["claim_id"]), now=1_063.0)

    store = repo._store_snapshot()
    run = dict(store["runs"][run_id])
    terminal = recovery_module.settle_dead_run(repo, store, run, current=1_090.0)
    assert terminal["status"] == "unknown"
    assert terminal["error_code"] == "SCHEDULER_RUNNER_DIED"
    assert run_id not in store["runs"]

    # 历史中已有精确终态时，不传 terminal_by_run 的单条调用也应命中该终态
    repo2 = _repository(tmp_path / "hit")
    run_id2 = _make_claimed_run(repo2)
    claim2 = repo2.get_active_run(run_id2)
    repo2.mark_run_running(run_id2, str(claim2["claim_id"]), now=1_063.0)
    store2 = repo2._store_snapshot()
    run2 = dict(store2["runs"][run_id2])
    persisted = {
        **run2,
        "status": "done",
        "ended_at": 1_064.0,
        "updated_at": 1_064.0,
        "claim_id": "",
        "claim_expires_at": 0.0,
        "response": "single-hit",
    }
    repo2.history_path.write_text(json.dumps(persisted, sort_keys=True) + "\n", encoding="utf-8")
    res_hit = recovery_module.settle_dead_run(repo2, store2, run2, current=1_090.0)
    assert res_hit["status"] == "done" and res_hit["response"] == "single-hit"

    # 显式传入 terminal_by_run={}（falsy 非 None）时，不得再触发 existing_terminal 扫盘
    monkeypatch.setattr(
        recovery_module,
        "existing_terminal",
        lambda *_args, **_kwargs: pytest.fail("empty terminal_by_run map must not trigger rescan"),
    )
    store3 = repo2._store_snapshot()
    res_empty_map = recovery_module.settle_dead_run(
        repo2, store3, run2, current=1_090.0, terminal_by_run={}
    )
    assert res_empty_map["status"] == "unknown"
    assert res_empty_map["error_code"] == "SCHEDULER_RUNNER_DIED"
