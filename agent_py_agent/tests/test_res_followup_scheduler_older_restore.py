"""SUB-35: restored active scheduler snapshots must not replay canonical terminal runs."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

import agent_py_agent.agent.scheduler.recovery as recovery_module
import agent_py_agent.agent.scheduler.repository as repository_module
from agent_py_agent.agent.scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
    SchedulerStateError,
)


# LLM: Tests use a private canonical owner layout and real repository locks/history, never production data.
# 函数用途: 创建独立的真实调度仓库。
def _repository(root: Path) -> SchedulerRepository:
    return SchedulerRepository(root, owner_provider="local", owner_kind="main", owner_id="local/main")


# LLM: Public transitions produce the backup before a real terminal commit; old claims deliberately precede terminal epoch.
# 函数用途: 完成一次真实运行，同时保留指定活动阶段的旧 store 字节。
def _finished_backup(tmp_path, state, status="done"):
    repository = _repository(tmp_path / "owner" / "data" / "scheduler")
    job, _ = repository.create_job(SchedulerJobCreateRequest(
        name="older-restore", prompt="execute once", thread_id="thread-restore",
        schedule={"kind": "every", "every_seconds": 60, "anchor_at": 1000, "timezone": "UTC"},
        misfire_grace_seconds=600, now=900,
    ))
    run = repository.reserve_due_runs(now=1000)[0]
    backups = {"queued": repository.store_path.read_bytes()}
    claim = repository.claim_run(str(run["run_id"]), lease_seconds=10000, now=1001)
    assert claim is not None
    backups["claimed"] = repository.store_path.read_bytes()
    repository.mark_run_running(str(run["run_id"]), str(claim["claim_id"]), now=1002)
    backups["running"] = repository.store_path.read_bytes()
    if state == "waiting":
        repository.park_run_waiting(str(run["run_id"]), str(claim["claim_id"]), now=1003)
        backups["waiting"] = repository.store_path.read_bytes()
        repository.finish_waiting_run(str(run["run_id"]), SchedulerRunFinish(status=status, now=1004))
    else:
        repository.finish_run(str(run["run_id"]), str(claim["claim_id"]), SchedulerRunFinish(status=status, now=1004))
    return repository, str(job["job_id"]), str(run["run_id"]), backups[state]


# LLM: An operator can replace the inode or copy bytes in place; cached and cold objects must observe the restored version.
# 函数用途: 把旧 store 原样放回，不改变仍然存在的 history。
def _restore(repository, backup, cold):
    if cold:
        staged = repository.store_path.with_name("older-store.json")
        staged.write_bytes(backup)
        staged.replace(repository.store_path)
        return _repository(repository.root)
    repository.store_path.write_bytes(backup)
    return repository


# LLM: A failed claim is the execution admission boundary; unchanged history bytes prove reconciliation did not reappend a result.
# 函数用途: 旧排队/领取副本不再得到第二份执行权，所有规范终态都只留一条历史。
@pytest.mark.parametrize("state", ["queued", "claimed"])
@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "skipped", "unknown"])
@pytest.mark.parametrize("cold", [False, True])
def test_restored_finished_run_is_not_claimed_again(tmp_path, state, status, cold):
    repository, _job_id, run_id, backup = _finished_backup(tmp_path, state, status)
    history = repository.history_path.read_bytes()
    repository = _restore(repository, backup, cold)
    assert repository.claim_run(run_id, lease_seconds=30, now=1005) is None
    assert repository.claim_run(run_id, lease_seconds=30, now=1006) is None
    assert repository.get_active_run(run_id) is None
    assert run_id not in json.loads(repository.store_path.read_bytes())["runs"]
    assert repository.history_path.read_bytes() == history
    rows, errors = repository.history()
    assert not errors and [(row["run_id"], row["status"]) for row in rows] == [(run_id, status)]


# LLM: Live-looking running/claimed leases and waiting rows must not block a new manual or due reservation once terminal history exists.
# 函数用途: 不靠租约过期或进程死亡，也能清掉旧活跃副本并继续预约同一任务。
@pytest.mark.parametrize("state", ["queued", "claimed", "running", "waiting"])
@pytest.mark.parametrize("entry", ["manual", "due", "recovery"])
def test_restored_terminal_run_does_not_block_job(tmp_path, state, entry):
    repository, job_id, run_id, backup = _finished_backup(tmp_path, state)
    history = repository.history_path.read_bytes()
    repository = _restore(repository, backup, False)
    if entry == "recovery":
        repository.recover_interrupted_executions(now=1005)
        assert repository.get_active_run(run_id) is None
        reserved = repository.reserve_manual_run(job_id, now=1006)
    elif entry == "manual":
        reserved = repository.reserve_manual_run(job_id, now=1005)
    else:
        repository.recover_interrupted_executions(now=1060)
        runs = repository.reserve_due_runs(now=1060)
        assert len(runs) == 1
        reserved = runs[0]
    assert reserved["job_id"] == job_id and reserved["run_id"] != run_id
    assert repository.get_active_run(run_id) is None
    assert repository.history_path.read_bytes() == history


# LLM: Missing canonical history supplies no terminal proof; normal claim and active-job exclusion remain unchanged.
# 函数用途: 历史被删或没有命中时，不把仍可运行的旧记录误清理。
@pytest.mark.parametrize("history_kind", ["missing", "unrelated", "nonterminal", "invalid", "foreign_owner", "foreign_job", "foreign_thread"])
def test_history_without_matching_valid_terminal_does_not_clear_run(tmp_path, history_kind):
    repository, job_id, run_id, backup = _finished_backup(tmp_path, "queued")
    terminal = json.loads(repository.history_path.read_bytes())
    if history_kind == "missing":
        repository.history_path.unlink()
    else:
        if history_kind == "unrelated":
            terminal["run_id"] = "srun_unrelated"
        elif history_kind == "nonterminal":
            terminal["status"] = "queued"
        elif history_kind == "invalid":
            terminal = {"run_id": run_id, "status": "done"}
        elif history_kind == "foreign_owner":
            terminal["owner"] = {**terminal["owner"], "id": "other-owner"}
        elif history_kind == "foreign_job":
            terminal["job_id"] = "job_other"
        else:
            terminal["thread_id"] = "thread-other"
        repository.history_path.write_text(json.dumps(terminal) + "\n")
    repository = _restore(repository, backup, False)
    history = repository.history_path.read_bytes() if repository.history_path.exists() else None
    with pytest.raises(SchedulerConflictError, match="active run"):
        repository.reserve_manual_run(job_id, now=1005)
    claim = repository.claim_run(run_id, lease_seconds=30, now=1005)
    assert claim is not None and claim["run_id"] == run_id
    assert repository.history_path.read_bytes() == history if history is not None else not repository.history_path.exists()


# LLM: Unreadable source bytes cannot authorize a terminal-membership decision or overwrite the restored snapshot.
# 函数用途: 历史不可读时保持失败关闭，不先清活动记录再发现错误。
def test_unreadable_history_does_not_mutate_restored_store(tmp_path):
    repository, _job_id, run_id, backup = _finished_backup(tmp_path, "queued")
    with repository.history_path.open("ab") as handle:
        handle.write(b"\xff\n")
    repository = _restore(repository, backup, False)
    with pytest.raises(SchedulerStateError, match="history is unreadable"):
        repository.claim_run(run_id, lease_seconds=30, now=1005)
    assert repository.store_path.read_bytes() == backup


# LLM: Failed explicit version checks must precede any reconciliation commit.
# 函数用途: 错误版本的恢复请求不落盘清理。
def test_scoped_recovery_rejects_wrong_version_before_writing(tmp_path):
    repository, job_id, _run_id, backup = _finished_backup(tmp_path, "running")
    repository = _restore(repository, backup, False)
    with pytest.raises(SchedulerConflictError, match="version changed"):
        repository.recover_interrupted_executions(now=1005, job_id=job_id, expected_version=999)
    assert repository.store_path.read_bytes() == backup


# LLM: Two genuine histories share one owner; an explicit job control may settle only its selected run.
# 函数用途: 同 owner 多任务恢复仍保留 job 选择边界，并返回完成的运行编号。
def test_scoped_recovery_only_settles_selected_job(tmp_path):
    repository, job_id, run_id, backup = _finished_backup(tmp_path / "one", "running")
    other, _other_job, other_run, other_backup = _finished_backup(tmp_path / "two", "running")
    store = json.loads(backup)
    second = json.loads(other_backup)
    store["jobs"].update(second["jobs"])
    store["runs"].update(second["runs"])
    repository.history_path.write_bytes(repository.history_path.read_bytes() + other.history_path.read_bytes())
    repository = _restore(repository, json.dumps(store).encode(), False)
    version = int(store["jobs"][job_id]["version"])
    assert repository.recover_interrupted_executions(now=1005, job_id=job_id, expected_version=version) == [run_id]
    assert repository.get_active_run(run_id) is None
    assert repository.get_active_run(other_run) is not None
    assert repository.get_job(job_id)["last_run_status"] == "done"


# LLM: The existing synthetic runtime creates the real authenticated fallback marker, then the original queued bytes are restored.
# 函数用途: 合法来源线程兜底后，旧队列副本也不能重新领取；没有真实模型或网络调用。
@pytest.mark.parametrize("mode", ["followup", "isolated"])
def test_restored_pre_fallback_run_is_not_reclaimed(tmp_path, mode):
    from agent_py_agent.tests.test_v13_w1_scheduler_thread_quota import fallback_case

    case = fallback_case(tmp_path, mode=mode)
    repository = case.agent.scheduler_repository
    run_id = str(repository.reserve_due_runs(now=1000)[0]["run_id"])
    backup = repository.store_path.read_bytes()
    assert case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1000)
    rows, errors = repository.history()
    assert not errors and rows[0]["status"] == "done"
    assert rows[0]["thread_fallback"]["original_thread_id"] == case.source.thread_id
    history = repository.history_path.read_bytes()
    repository = _restore(repository, backup, False)
    assert repository.claim_run(run_id, lease_seconds=30, now=1005) is None
    assert repository.get_active_run(run_id) is None
    assert repository.history_path.read_bytes() == history


# LLM: Heartbeats have no replay-admission authority and must not scan unrelated lifetime history.
# 函数用途: 续租不因这轮修复新增历史查询或整账扫描。
def test_normal_heartbeat_does_not_query_history(tmp_path, monkeypatch):
    repository, _job_id, run_id, backup = _finished_backup(tmp_path, "claimed")
    repository.history_path.unlink()
    repository = _restore(repository, backup, False)
    claim = repository.get_active_run(run_id)

    def forbidden(_requested):
        pytest.fail("heartbeat must not query terminal history")

    monkeypatch.setattr(repository, "_history_membership_unlocked", forbidden)
    assert repository.heartbeat_run(run_id, str(claim["claim_id"]), lease_seconds=30, now=1005)


# 函数用途: 构造一条带死亡租约信息的 running 快照，便于测试恢复与身份筛选反例。
def _mark_running_dead(repository: SchedulerRepository, run_id: str, backup: bytes) -> dict:
    store = json.loads(backup)
    run = store["runs"][run_id]
    run["claim_expires_at"] = 1_005.0
    run["runner_pid"] = 919191
    run["runner_start_time"] = "7"
    repository.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return dict(run)


@pytest.mark.parametrize(
    ("scenario", "expected_response"),
    [
        ("later_wrong_epoch", "correct"),
        ("later_wrong_thread", "correct"),
        ("later_wrong_job", "correct"),
        ("later_nonterminal", "correct"),
        ("later_corrupt_and_invalid", "correct"),
        ("latest_matching_terminal", "newer"),
        ("only_wrong_epoch", None),
        ("only_wrong_thread", None),
        ("only_wrong_job", None),
        ("only_nonterminal", None),
        ("absent", None),
    ],
)
def test_batch_and_single_terminal_selection_match_six_identity_counterexamples(
    tmp_path, monkeypatch, scenario: str, expected_response: str | None
) -> None:
    """流式前向匹配必须与单条筛选完全一致：选中最新精确匹配终态，不被尾部不兼容/非终态/坏行遮蔽。"""
    repository, _job_id, run_id, backup = _finished_backup(tmp_path, "running")
    terminal = json.loads(repository.history_path.read_bytes())
    terminal["response"] = "correct"
    run = _mark_running_dead(repository, run_id, backup)

    if scenario == "later_wrong_epoch":
        lines = [
            json.dumps(terminal),
            json.dumps({**terminal, "claim_epoch": terminal["claim_epoch"] + 1, "response": "wrong"}),
        ]
    elif scenario == "later_wrong_thread":
        lines = [
            json.dumps(terminal),
            json.dumps({**terminal, "thread_id": "thread-other", "response": "wrong"}),
        ]
    elif scenario == "later_wrong_job":
        lines = [
            json.dumps(terminal),
            json.dumps({**terminal, "job_id": "job_other", "response": "wrong"}),
        ]
    elif scenario == "later_nonterminal":
        lines = [
            json.dumps(terminal),
            json.dumps({**terminal, "status": "running", "response": "wrong"}),
        ]
    elif scenario == "later_corrupt_and_invalid":
        lines = [
            json.dumps(terminal),
            "{broken-json",
            '["not-an-object"]',
            json.dumps({"run_id": run_id, "status": "done", "response": "wrong"}),
            json.dumps({**terminal, "owner": {**terminal["owner"], "id": "foreign"}, "response": "wrong"}),
        ]
    elif scenario == "latest_matching_terminal":
        lines = [
            json.dumps({**terminal, "response": "older"}),
            json.dumps({**terminal, "response": "newer"}),
        ]
    elif scenario == "only_wrong_epoch":
        lines = [json.dumps({**terminal, "claim_epoch": terminal["claim_epoch"] + 1, "response": "wrong"})]
    elif scenario == "only_wrong_thread":
        lines = [json.dumps({**terminal, "thread_id": "thread-other", "response": "wrong"})]
    elif scenario == "only_wrong_job":
        lines = [json.dumps({**terminal, "job_id": "job_other", "response": "wrong"})]
    elif scenario == "only_nonterminal":
        lines = [json.dumps({**terminal, "status": "running", "response": "wrong"})]
    else:
        lines = [json.dumps({**terminal, "run_id": "srun_other", "response": "other"})]

    repository.history_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    single = recovery_module.existing_terminal(repository, run)
    batch = recovery_module.existing_terminals(repository, {run_id: run}).get(run_id)
    assert single == batch
    if expected_response is None:
        assert single is None
    else:
        assert single is not None and single["response"] == expected_response

    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")
    recovered = recovery_module.recover_runs(repository, current=1_006.0)
    assert recovered == [run_id]
    assert repository.get_active_run(run_id) is None

    if expected_response is None:
        job = repository.get_job(_job_id)
        assert job["last_run_status"] == "unknown"
        assert job["last_error_code"] == "SCHEDULER_RUNNER_DIED"
        persisted = [
            json.loads(line)
            for line in repository.history_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("{broken")
        ]
        unknown_rows = [
            row for row in persisted if row.get("run_id") == run_id and row.get("status") == "unknown"
        ]
        if scenario == "absent":
            assert len(unknown_rows) == 1
            assert unknown_rows[0]["error_code"] == "SCHEDULER_RUNNER_DIED"
        else:
            # 历史文件已有同 run_id 干扰行时，保留原 _new_history_rows_unlocked 按 run_id 去重语义
            assert len(repository.history_path.read_text(encoding="utf-8").splitlines()) == len(lines)
    else:
        assert repository.get_job(_job_id)["last_run_status"] == "done"
        # 不得在已有精确匹配终态时重复追加一条记录
        assert len(repository.history_path.read_text(encoding="utf-8").splitlines()) == len(lines)


@pytest.mark.parametrize("fault", ["unreadable_utf8", "replaced_during_scan"])
def test_batch_recovery_aborts_before_settlement_on_unreadable_or_replaced_history(
    tmp_path, monkeypatch, fault: str
) -> None:
    """流式扫描期间历史不可读或被替换，必须在任何结算写盘前中止并保留活跃记录。"""
    repository, _job_id, run_id, backup = _finished_backup(tmp_path, "running")
    terminal = json.loads(repository.history_path.read_bytes())
    _mark_running_dead(repository, run_id, backup)
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")

    if fault == "unreadable_utf8":
        with repository.history_path.open("ab") as handle:
            handle.write(b"\xff\n")
    else:
        staged = repository.history_path.with_name("replacement.jsonl")
        staged.write_text(json.dumps({**terminal, "response": "replaced"}) + "\n", encoding="utf-8")
        original_parse = repository._parse_run
        replaced = False

        def _replace_during_scan(raw, *, terminal: bool = False):
            nonlocal replaced
            if terminal and not replaced:
                replaced = True
                staged.replace(repository.history_path)
            return original_parse(raw, terminal=terminal)

        monkeypatch.setattr(repository, "_parse_run", _replace_during_scan)

    store_before = repository.store_path.read_bytes()
    history_before = repository.history_path.read_bytes()

    with pytest.raises(SchedulerStateError) as caught:
        recovery_module.recover_runs(repository, current=1_006.0)
    assert caught.value.error_code == "SCHEDULER_HISTORY_READ_FAILED"
    assert repository.store_path.read_bytes() == store_before
    if fault == "unreadable_utf8":
        assert repository.history_path.read_bytes() == history_before
    assert repository.get_active_run(run_id) is not None


def test_partial_index_recovery_preserves_persisted_terminal_and_settles_absent_as_unknown(
    tmp_path, monkeypatch
) -> None:
    """历史索引处于 partial 状态时，不能把未回填区间当成 absent 误写 unknown。"""
    repository = _repository(tmp_path / "partial" / "data" / "scheduler")
    for idx in range(2):
        repository.create_job(
            SchedulerJobCreateRequest(
                name=f"job-{idx}",
                prompt="run",
                thread_id=f"th-{idx}",
                source_request_id=f"req-{idx}",
                schedule={"kind": "every", "every_seconds": 60, "anchor_at": 1000, "timezone": "UTC"},
                misfire_grace_seconds=600,
                now=900,
            )
        )
    due = repository.reserve_due_runs(now=1_000.0)
    assert len(due) == 2
    runs: list[dict] = []
    for row in due:
        claim = repository.claim_run(str(row["run_id"]), lease_seconds=10, now=1_001.0)
        assert claim is not None
        repository.mark_run_running(str(row["run_id"]), str(claim["claim_id"]), now=1_002.0)
        runs.append(repository.get_active_run(str(row["run_id"])))

    backup = repository.store_path.read_bytes()
    # 先真写入第一条完成历史并保持第二条未完成，再恢复旧 store 且制造 partial 索引状态
    repository.finish_run(
        str(runs[0]["run_id"]),
        str(runs[0]["claim_id"]),
        SchedulerRunFinish(status="done", response="already-committed", now=1_003.0),
    )
    persisted_row = json.loads(repository.history_path.read_text(encoding="utf-8").splitlines()[0])
    # 写入若干前置填充行 + 尾部的 persisted_row，并将索引置为 partial（只索引前 1 行）
    pad_row = {**persisted_row, "run_id": "srun_partial_pad"}
    repository.history_path.write_text(
        json.dumps(pad_row, sort_keys=True)
        + "\n"
        + json.dumps(persisted_row, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with closing(repository._history_index._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        state = repository._history_index._state(conn)
        state["cursor"] = 1  # 人为滞后于文件总字节，模拟 partial 索引
        state["complete"] = False
        repository._history_index._save(conn, state)
    assert repository._history_index.membership({str(runs[0]["run_id"])}) == (frozenset(), False)

    store = json.loads(backup)
    for run in runs:
        store["runs"][str(run["run_id"])]["claim_expires_at"] = 999.0
        store["runs"][str(run["run_id"])]["runner_pid"] = 71
        store["runs"][str(run["run_id"])]["runner_start_time"] = "7"
    repository.store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repository_module, "_process_state", lambda _pid: "dead")

    recovered = recovery_module.recover_runs(repository, current=1_006.0)
    assert sorted(recovered) == sorted([str(runs[0]["run_id"]), str(runs[1]["run_id"])])
    assert repository.get_job(str(runs[0]["job_id"]))["last_run_status"] == "done"
    assert repository.get_job(str(runs[1]["job_id"]))["last_run_status"] == "unknown"
    rows = [json.loads(line) for line in repository.history_path.read_text(encoding="utf-8").splitlines()]
    by_id = [row for row in rows if row["run_id"] == str(runs[0]["run_id"])]
    assert len(by_id) == 1 and by_id[0]["status"] == "done"
