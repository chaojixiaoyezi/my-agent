"""验证 SchedulerHistoryIndex.membership 索引分片核对与 reserve_due_runs O(1) 活动索引。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

import pytest

import agent_py_agent.agent.scheduler.monitor_state as monitor_state_module
import agent_py_agent.agent.scheduler.repository as repository_module
from agent_py_agent.agent.scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
    SchedulerStateError,
)
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer


def _indexed_repo(tmp_path, *, owner_id: str = "u-w6") -> SchedulerRepository:
    home = tmp_path / owner_id
    return SchedulerRepository(
        home / "data" / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id=owner_id,
        default_timezone="UTC",
        quota_enforcer=OwnerQuotaEnforcer(home, max_bytes=0),
    )


def _create_every_job(
    repo: SchedulerRepository,
    *,
    name: str,
    anchor: float = 1_000.0,
    every_seconds: int = 60,
    now: float = 900.0,
) -> dict[str, object]:
    job, _created = repo.create_job(
        SchedulerJobCreateRequest(
            name=name,
            prompt=f"execute {name}",
            thread_id=f"thread-{name}",
            source_task_id=f"task-{name}",
            schedule={
                "kind": "every",
                "every_seconds": every_seconds,
                "anchor_at": anchor,
                "timezone": "UTC",
            },
            skill_refs=[],
            source_request_id=f"req:{name}",
            now=now,
        )
    )
    return job


def _drain_history_backfill(repo: SchedulerRepository, *, max_rounds: int = 32) -> dict[str, object]:
    result: dict[str, object] = {}
    for _ in range(max_rounds):
        result = repo.maintain_history_index()
        if result.get("complete"):
            return result
    raise AssertionError(f"history backfill did not complete: {result}")


def _set_history_state_fields(repo: SchedulerRepository, **updates: object) -> None:
    with closing(repo._history_index._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        state = repo._history_index._state(conn)
        state.update(updates)
        repo._history_index._save(conn, state)


def test_history_index_membership_skips_fallback_after_corrupt_row(tmp_path, monkeypatch) -> None:
    """OwnerReadIndex 因坏行停滞时，已回填的 SchedulerHistoryIndex.membership 应零扫描返回权威归属。"""
    repo = _indexed_repo(tmp_path)
    repo.history_path.write_text(
        "".join(
            [
                json.dumps({"run_id": "done-0", "job_id": "j0", "status": "done", "ended_at": 100}) + "\n",
                '{"truncated-json\n',
                '["not-an-object"]\n',
                json.dumps({"run_id": "done-1", "job_id": "j1", "status": "done", "ended_at": 101}) + "\n",
                json.dumps({"run_id": "done-2", "job_id": "j2", "status": "done", "ended_at": 102, "owner": {"id": "other"}}) + "\n",
            ]
        ),
        encoding="utf-8",
    )
    _drain_history_backfill(repo)

    # 创建一个真实活动 run，使 recover_interrupted_executions 进入 _history_membership_unlocked 而不是空表早退。
    _create_every_job(repo, name="active-job", anchor=1_000.0, every_seconds=600, now=900.0)
    reserved = repo.reserve_due_runs(now=1_000.0)
    assert len(reserved) == 1
    claimed = repo.claim_run(str(reserved[0]["run_id"]), lease_seconds=60, now=1_001.0)
    assert claimed is not None
    repo.mark_run_running(str(claimed["run_id"]), str(claimed["claim_id"]), now=1_002.0)

    read_index = repo._history_read_index
    assert read_index is not None
    assert read_index.advance("scheduler", repo._history_source_parts)["status"] != "complete"

    fallback_calls: list[frozenset[str]] = []
    original_fallback = repository_module._history_membership_fallback

    def _spy_fallback(path, requested, **kwargs):
        fallback_calls.append(requested)
        return original_fallback(path, requested, **kwargs)

    monkeypatch.setattr(repository_module, "_history_membership_fallback", _spy_fallback)

    fd_dir = "/dev/fd"
    before_fds = len(os.listdir(fd_dir)) if os.path.isdir(fd_dir) else None
    for _ in range(5):
        assert repo.recover_interrupted_executions(now=1_005.0) == []
    with repo._mutation_scope():
        fresh_only = repo._new_history_rows_unlocked(
            [
                {"run_id": "done-1"},
                {"run_id": "done-2"},
                {"run_id": "fresh-3"},
                {"run_id": "fresh-3"},
            ]
        )
    assert fresh_only == [{"run_id": "fresh-3"}, {"run_id": "fresh-3"}]
    assert fallback_calls == []

    found, authoritative = repo._history_index.membership(["done-0", "done-1", "done-2", "absent"])
    assert authoritative is True
    assert found == frozenset({"done-0", "done-1", "done-2"})
    assert repo._history_index.membership([]) == (frozenset(), True)
    if before_fds is not None:
        assert len(os.listdir(fd_dir)) == before_fds


def test_history_index_membership_chunks_over_500_run_ids(tmp_path, monkeypatch) -> None:
    """超过 500 个 run_id 时应分片查询（每片 <= 500 绑定参数），且保持零后备全扫。"""
    repo = _indexed_repo(tmp_path)
    present_ids = {f"run-{idx}" for idx in range(0, 1250, 2)}
    lines = [
        json.dumps({"run_id": run_id, "job_id": "j", "status": "done", "ended_at": 100}) + "\n"
        for run_id in sorted(present_ids)
    ]
    lines.append('{"corrupt-json\n')
    repo.history_path.write_text("".join(lines), encoding="utf-8")
    _drain_history_backfill(repo)

    chunk_sizes: list[int] = []
    original_connect = repo._history_index._connect

    class _ConnProxy:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql: str, params=()):
            if "FROM history_effects WHERE generation=? AND run_id IN" in sql:
                chunk_sizes.append(len(params) - 1)
            return self._inner.execute(sql, params)

        def close(self) -> None:
            self._inner.close()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(
        repo._history_index,
        "_connect",
        lambda *, read_only=False: _ConnProxy(original_connect(read_only=read_only)),
    )

    fallback_calls: list[int] = []
    original_fallback = repository_module._history_membership_fallback

    def _spy_fallback(path, requested, **kwargs):
        fallback_calls.append(len(requested))
        return original_fallback(path, requested, **kwargs)

    monkeypatch.setattr(repository_module, "_history_membership_fallback", _spy_fallback)

    requested_list = [f"run-{idx}" for idx in range(1250)]
    requested_list.extend(["run-0", "run-2", "run-1248"])
    with repo._mutation_scope():
        found = repo._history_membership_unlocked(frozenset(requested_list))
    assert found == present_ids
    assert fallback_calls == []
    assert chunk_sizes == [500, 500, 250]
    assert all(1 <= size <= 500 for size in chunk_sizes)

    # 验证超过 OwnerReadIndex 4096 阈值的批次也走分片索引且不触发全扫。
    large_requested = frozenset(f"run-{idx}" for idx in range(4200))
    with repo._mutation_scope():
        large_found = repo._history_membership_unlocked(large_requested)
    assert large_found == present_ids
    assert fallback_calls == []


def test_standalone_repo_large_history_membership_zero_fallback(tmp_path, monkeypatch) -> None:
    """无 OwnerReadIndex 的独立 SchedulerRepository 在大 history.jsonl 追平后，预约与归档均零扫描。"""
    repo = SchedulerRepository(
        tmp_path / "standalone" / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="standalone-u",
        default_timezone="UTC",
    )
    assert repo._history_read_index is None

    # 写入约 1.2 MiB 历史行并在索引中回填追平（每行远小于 1 MiB 阈值）。
    pad = "p" * 512
    lines = [
        json.dumps({"run_id": f"hist-{idx}", "job_id": "j-old", "status": "done", "ended_at": 500 + idx, "pad": pad}) + "\n"
        for idx in range(2100)
    ]
    repo.history_path.write_text("".join(lines), encoding="utf-8")
    assert len(repo.history_path.read_bytes()) > 1_000_000
    _drain_history_backfill(repo, max_rounds=32)

    _create_every_job(repo, name="standalone-job", anchor=1_000.0, every_seconds=600, now=900.0)
    fallback_calls: list[frozenset[str]] = []
    original_fallback = repository_module._history_membership_fallback

    def _spy_fallback(path, requested, **kwargs):
        fallback_calls.append(requested)
        return original_fallback(path, requested, **kwargs)

    monkeypatch.setattr(repository_module, "_history_membership_fallback", _spy_fallback)

    # 先预约一条活动 run 并领取，再走一次 reserve_due_runs（触发 _reconcile_history_runs_unlocked）与 finish_run。
    reserved = repo.reserve_due_runs(now=1_000.0)
    assert len(reserved) == 1
    claimed = repo.claim_run(str(reserved[0]["run_id"]), lease_seconds=60, now=1_001.0)
    assert claimed is not None
    assert repo.reserve_due_runs(now=1_002.0) == []
    repo.mark_run_running(str(claimed["run_id"]), str(claimed["claim_id"]), now=1_003.0)
    repo.finish_run(str(claimed["run_id"]), str(claimed["claim_id"]), SchedulerRunFinish(status="done", now=1_004.0))

    with repo._mutation_scope():
        fresh = repo._new_history_rows_unlocked([{"run_id": "hist-0"}, {"run_id": "hist-2099"}, {"run_id": "new-run"}])
    assert fresh == [{"run_id": "new-run"}]
    assert fallback_calls == []


def test_history_index_membership_falls_back_when_non_authoritative(tmp_path, monkeypatch) -> None:
    """滞后、未封口、超长行、owner/指纹不匹配、并发追加及缺源等非权威场景必须回退或按原合同失败。"""
    # 1) partial (cursor < size)
    repo = _indexed_repo(tmp_path, owner_id="partial")
    repo.history_path.write_text(json.dumps({"run_id": "r1"}) + "\n", encoding="utf-8")
    _drain_history_backfill(repo)
    with repo.history_path.open("ab") as handle:
        handle.write((json.dumps({"run_id": "r2"}) + "\n").encode("utf-8"))
    assert repo._history_index.membership({"r1", "r2"}) == (frozenset(), False)
    with repo._mutation_scope():
        assert repo._history_membership_unlocked(frozenset({"r1", "r2"})) == frozenset({"r1", "r2"})

    # 2) SCHEDULER_HISTORY_ROW_TOO_LARGE (>1 MiB 行虽被索引跳过但扫描可读)
    repo_large = _indexed_repo(tmp_path, owner_id="row-too-large")
    giant_row = json.dumps({"run_id": "giant-1", "pad": "a" * 1_100_000}) + "\n"
    repo_large.history_path.write_text(
        json.dumps({"run_id": "small-1"}) + "\n" + giant_row,
        encoding="utf-8",
    )
    _drain_history_backfill(repo_large)
    assert repo_large._history_index.membership({"giant-1", "small-1"}) == (frozenset(), False)
    with repo_large._mutation_scope():
        assert repo_large._history_membership_unlocked(frozenset({"giant-1", "small-1"})) == frozenset({"giant-1", "small-1"})

    # 3) pending / skipping / owner_mismatch / head_tail_mismatch
    for case in ("pending", "skipping", "owner"):
        r_case = _indexed_repo(tmp_path, owner_id=f"flag-{case}")
        r_case.history_path.write_text(json.dumps({"run_id": "ok-1"}) + "\n", encoding="utf-8")
        _drain_history_backfill(r_case)
        if case == "pending":
            _set_history_state_fields(r_case, pending="7b")
        elif case == "skipping":
            _set_history_state_fields(r_case, skipping=True)
        else:
            _set_history_state_fields(r_case, owner="other-owner")
        assert r_case._history_index.membership({"ok-1"}) == (frozenset(), False)
        with r_case._mutation_scope():
            assert r_case._history_membership_unlocked(frozenset({"ok-1"})) == frozenset({"ok-1"})

    # 等长中部改写使 head/tail 与 stamp 不符
    repo_rewrite = _indexed_repo(tmp_path, owner_id="rewrite")
    prefix = json.dumps({"run_id": "row-000", "pad": "x" * 2048}) + "\n"
    middle = json.dumps({"run_id": "old-row", "pad": "m" * 2048}) + "\n"
    suffix = json.dumps({"run_id": "row-999", "pad": "z" * 2048}) + "\n"
    repo_rewrite.history_path.write_text(prefix + middle + suffix, encoding="utf-8")
    _drain_history_backfill(repo_rewrite)
    with repo_rewrite.history_path.open("r+b") as handle:
        handle.seek(len(prefix.encode("utf-8")))
        handle.write(middle.replace("old-row", "new-row").encode("utf-8"))
    assert repo_rewrite._history_index.membership({"old-row", "new-row"}) == (frozenset(), False)
    with repo_rewrite._mutation_scope():
        assert repo_rewrite._history_membership_unlocked(frozenset({"old-row", "new-row"})) == frozenset({"new-row"})

    # 4) 查询返回后与写锁提交前之间发生外部追加时，应放弃旧结果并回退读到新行
    repo_race = _indexed_repo(tmp_path, owner_id="race")
    repo_race.history_path.write_text('{"run_id":"old"}\n{"bad\n', encoding="utf-8")
    _drain_history_backfill(repo_race)
    orig_membership = repo_race._history_index.membership

    def _race_membership(run_ids):
        result = orig_membership(run_ids)
        with repo_race.history_path.open("ab") as handle:
            handle.write(b'{"run_id":"late"}\n')
        return result

    monkeypatch.setattr(repo_race._history_index, "membership", _race_membership)
    with repo_race._mutation_scope():
        assert repo_race._history_membership_unlocked(frozenset({"late"})) == frozenset({"late"})

    # 5) 曾见过来源后被删除或变成符号链接：membership 不得返回 True，仓库继续走原日志/失败关闭合同
    repo_missing = _indexed_repo(tmp_path, owner_id="missing")
    repo_missing.history_path.write_text('{"run_id":"persisted"}\n', encoding="utf-8")
    _drain_history_backfill(repo_missing)
    with repo_missing._mutation_scope():
        assert repo_missing._new_history_rows_unlocked([{"run_id": "persisted"}]) == []
    repo_missing.history_path.unlink()
    assert repo_missing._history_index.membership({"persisted"}) == (frozenset(), False)
    with repo_missing._mutation_scope():
        assert repo_missing._new_history_rows_unlocked([{"run_id": "fresh"}]) == [{"run_id": "fresh"}]

    moved = repo_missing.history_path.with_name("moved.jsonl")
    moved.write_text('{"run_id":"moved"}\n', encoding="utf-8")
    repo_missing.history_path.symlink_to(moved)
    assert repo_missing._history_index.membership({"moved"}) == (frozenset(), False)
    with pytest.raises(SchedulerStateError) as caught:
        with repo_missing._mutation_scope():
            repo_missing._new_history_rows_unlocked([{"run_id": "fresh"}])
    assert caught.value.error_code == "SCHEDULER_HISTORY_READ_FAILED"


def _measure_reserve_parse_calls(base_dir, *, blocked_count: int, monkeypatch) -> tuple[int, list[str]]:
    repo = SchedulerRepository(
        base_dir / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id=f"scale-{blocked_count}",
        default_timezone="UTC",
    )
    blocked_ids: list[str] = []
    for idx in range(blocked_count):
        job = _create_every_job(repo, name=f"blocked-{idx}", anchor=1_000.0, every_seconds=60, now=900.0)
        blocked_ids.append(str(job["job_id"]))
    reserved_blocked = repo.reserve_due_runs(now=1_000.0, limit=blocked_count + 10)
    assert len(reserved_blocked) == blocked_count

    ready_ids: list[str] = []
    for idx in range(2):
        ready = _create_every_job(repo, name=f"ready-{idx}", anchor=1_200.0, every_seconds=600, now=900.0)
        ready_ids.append(str(ready["job_id"]))

    parse_calls = 0
    has_active_calls = 0
    original_parse_run = repo._parse_run
    original_has_active = repo._job_has_active_run

    def _spy_parse_run(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse_run(*args, **kwargs)

    def _spy_has_active(*args, **kwargs):
        nonlocal has_active_calls
        has_active_calls += 1
        return original_has_active(*args, **kwargs)

    monkeypatch.setattr(repo, "_parse_run", _spy_parse_run)
    monkeypatch.setattr(repo, "_job_has_active_run", _spy_has_active)

    due_reserved = repo.reserve_due_runs(now=1_200.0, limit=32)
    due_parse_calls = parse_calls
    assert has_active_calls == 0
    assert sorted(str(item["job_id"]) for item in due_reserved) == sorted(ready_ids)

    # reserve_manual_run 仍使用 _job_has_active_run 阻挡重复手动触发。
    with pytest.raises(SchedulerConflictError):
        repo.reserve_manual_run(blocked_ids[0], now=1_200.0)
    assert has_active_calls == 1
    return due_parse_calls, ready_ids


def test_reserve_due_runs_linear_parse_count_with_many_active_runs(tmp_path, monkeypatch) -> None:
    """大量活动 run 阻塞到期任务时，reserve_due_runs 的 _parse_run 次数应随 R 线性而非 O(J*R) 平方增长。"""
    calls_80, _ = _measure_reserve_parse_calls(tmp_path / "n80", blocked_count=80, monkeypatch=monkeypatch)
    calls_160, _ = _measure_reserve_parse_calls(tmp_path / "n160", blocked_count=160, monkeypatch=monkeypatch)

    assert calls_80 <= 80 * 3
    assert calls_160 / max(1, calls_80) <= 2.1


def test_reserve_due_runs_rule_lanes_intra_pass_and_skip_non_blocking(tmp_path, monkeypatch) -> None:
    """rule 独立车道阻塞、单轮内更新阻挡重复预约，以及跳过（misfire）不得误阻挡同 job 其它 rule。"""
    repo = _indexed_repo(tmp_path, owner_id="lanes")
    rules = [
        {
            "id": "rule-skip",
            "name": "misfire skip rule",
            "schedule": {"kind": "every", "every_seconds": 60, "anchor_at": 1_000, "timezone": "UTC"},
            "use_llm": True,
            "prompt": "check skip",
            "policy": {"misfire": "skip"},
        },
        {
            "id": "rule-run",
            "name": "normal rule",
            "schedule": {"kind": "every", "every_seconds": 300, "anchor_at": 1_000, "timezone": "UTC"},
            "use_llm": True,
            "prompt": "check run",
            "policy": {"misfire": "coalesce"},
        },
        {
            "id": "rule-other",
            "name": "independent rule",
            "schedule": {"kind": "every", "every_seconds": 1_800, "anchor_at": 1_000, "timezone": "UTC"},
            "use_llm": True,
            "prompt": "check other",
            "policy": {"misfire": "coalesce"},
        },
    ]
    monitor_job, _ = repo.create_job(
        SchedulerJobCreateRequest(
            name="monitor",
            prompt="",
            schedule={},
            thread_id="thread-monitor",
            rules=rules,
            misfire_grace_seconds=5,
            now=900.0,
            source_request_id="req:monitor",
        )
    )
    monitor_id = str(monitor_job["job_id"])

    # rule-skip 在 now=1200 超出 5s 宽限被 skip，同 job 的 rule-run / rule-other 仍必须正常预约。
    first_runs = repo.reserve_due_runs(now=1_200.0)
    assert sorted(str(run["rule_id"]) for run in first_runs) == ["rule-other", "rule-run"]

    # 完成 rule-run，保持 rule-other 活动；到 1500 只预约 rule-run。
    run_to_finish = next(run for run in first_runs if run["rule_id"] == "rule-run")
    claimed = repo.claim_run(str(run_to_finish["run_id"]), lease_seconds=60, now=1_201.0)
    assert claimed is not None
    repo.mark_run_running(str(claimed["run_id"]), str(claimed["claim_id"]), now=1_202.0)
    repo.finish_run(str(claimed["run_id"]), str(claimed["claim_id"]), SchedulerRunFinish(status="done", now=1_205.0))

    # 验证同一 pass 内若同一 (job_id, rule_id) 出现两次到期视图，首次预约更新 active_rules 后第二次会被立即跳过；
    # 同 job 无 rule_id 视图也会被 active_jobs 阻挡。
    plain_job = _create_every_job(repo, name="plain", anchor=1_500.0, every_seconds=60, now=900.0)
    plain_id = str(plain_job["job_id"])
    original_due_views = monitor_state_module.due_rule_views

    def _duplicated_views(job, now_ts):
        views = original_due_views(job, now_ts)
        if str(job["job_id"]) == monitor_id:
            dup_rule = [dict(view) for view in views if view.get("rule_id") == "rule-run"]
            empty_rule_view = dict(job)
            empty_rule_view["rule_id"] = ""
            return views + dup_rule + [empty_rule_view]
        if str(job["job_id"]) == plain_id:
            return views + [dict(view) for view in views]
        return views

    monkeypatch.setattr(monitor_state_module, "due_rule_views", _duplicated_views)

    second_runs = repo.reserve_due_runs(now=1_500.0)
    assert sorted((str(run["job_id"]), str(run.get("rule_id") or "")) for run in second_runs) == sorted(
        [(monitor_id, "rule-run"), (plain_id, "")]
    )
