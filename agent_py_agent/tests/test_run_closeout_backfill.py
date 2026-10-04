"""rcb：历史悬挂 run 一次性补收口迁移的用例。

覆盖：各类悬挂记录按结构化分族补收口（blocked→failed、取消→cancelled、ok→done）；
可续跑族、等用户族、未结算操作、活跃执行锁、最近活动、unknown attempt、无结束事实
一律不动；迁移只执行一次（第二次空操作、记录不变）；预览只读（字节与 mtime 不变）
且与应用同一套判据；分族与 runtime_mixin 对拍一致；owner 维护集成。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.run_closeout_backfill import (
    RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS,
    RUN_CLOSEOUT_BACKFILL_KEY,
    RUN_CLOSEOUT_BACKFILLED_EVENT,
    apply_run_closeout_backfill,
    preview_run_closeout_backfill,
)

#: 固定"现在"，让时间下限判定可控（不用真实时钟）。
NOW = 2_000_000_000.0
#: 超过宽限下限 1 小时的尝试结束时间（应被视为历史悬挂）。
OLD_ENDED_AT = NOW - RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS - 3600
#: 常用结束事实：协议违规 blocked（不可续跑族）。
BLOCKED_FACT = {
    "runtime_status": "blocked",
    "runtime_reason": "TOOL_PROTOCOL_VIOLATION",
    "runtime_source": "tool_loop",
}


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _hung_run(repo, run_id, fact, *, ended_at=OLD_ENDED_AT):
    """造一条悬挂记录：run 未终态、attempt 已结算并带结构化结束事实（fact 字典）。"""
    rec = repo.record_run_creation(owner_id="local/main", goal=run_id, run_id=run_id, role="main")
    repo.settle_agent_attempt(
        agent_run_id=rec["agent_run_id"],
        attempt_id=rec["attempt_id"],
        payload=dict(fact),
        now=ended_at,
    )
    return rec


def _run_status(repo, rec):
    return str(repo.get_agent_run(rec["agent_run_id"])["status"])


def _events(repo, agent_run_id, event_type):
    with repo._runtime_connection() as conn:
        return conn.execute(
            "SELECT * FROM runtime_events WHERE agent_run_id = ? AND event_type = ? ORDER BY seq",
            (agent_run_id, event_type),
        ).fetchall()


def _task_run_row(repo, task_run_id):
    with repo._runtime_connection() as conn:
        return conn.execute(
            "SELECT status, closed_at FROM task_runs WHERE task_run_id = ?", (task_run_id,),
        ).fetchone()


def _insert_executing_op(repo, rec):
    """造一条未结算操作（EXECUTING 且已启动）。"""
    with repo._runtime_connection() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
            "VALUES('op-rcb-1', ?, ?, 1, 0, 'test', 'EXECUTING', ?, ?, ?)",
            (rec["agent_run_id"], rec["attempt_id"], OLD_ENDED_AT, OLD_ENDED_AT, OLD_ENDED_AT),
        )
        conn.commit()


def _insert_active_lock(repo, rec):
    """造一把活跃执行权锁（持主为当前进程，holder_is_alive 为真）。"""
    with repo._runtime_connection() as conn:
        conn.execute(
            "INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance, pid, start_token, "
            "attempt_id, attempt_generation, workspace_epoch, lease_expires_at, created_at, updated_at) "
            "VALUES('lock-rcb-1', ?, 'test', ?, '', ?, 1, 1, ?, ?, ?)",
            (
                f"attempt-exec:{rec['agent_run_id']}", os.getpid(), rec["attempt_id"],
                NOW + 3600, OLD_ENDED_AT, OLD_ENDED_AT,
            ),
        )
        conn.commit()


def _mark_attempt_unknown(repo, rec):
    """把 attempt 标成 unknown（不在静止集，必须等人工恢复）。"""
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_attempts SET status = 'unknown' WHERE attempt_id = ?", (rec["attempt_id"],),
        )
        conn.commit()


def _drop_completion_fact(repo, rec):
    """删掉结束事实（模拟没有 agent_attempt.completed 的历史记录）。"""
    with repo._runtime_connection() as conn:
        conn.execute(
            "DELETE FROM runtime_events WHERE attempt_id = ? AND event_type = 'agent_attempt.completed'",
            (rec["attempt_id"],),
        )
        conn.commit()


def _make_recovered_attempt(repo, rec):
    """用真实 recover 出口把 attempt 从 unknown 置为 recovered（recover 只从 unknown 来、
    没有 agent_attempt.completed 结束事实），并把结束时间固定回测试时钟。"""
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_attempts SET status = 'unknown' WHERE attempt_id = ?", (rec["attempt_id"],),
        )
        conn.execute(
            "DELETE FROM runtime_events WHERE attempt_id = ? AND event_type = 'agent_attempt.completed'",
            (rec["attempt_id"],),
        )
        conn.commit()
    repo.recover_attempt_unknown(rec["attempt_id"], operator="test", effect_disposition="confirmed_noop")
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_attempts SET ended_at = ? WHERE attempt_id = ?", (OLD_ENDED_AT, rec["attempt_id"]),
        )
        conn.commit()


def test_apply_settles_hung_runs_by_family_and_records_migration(repo):
    """三条悬挂按分族补收口：blocked→failed、user_stop→cancelled、ok→done；
    事件、迁移记录与 task_run 关闭齐全。"""
    blocked = _hung_run(repo, "hung-blocked", BLOCKED_FACT)
    stopped = _hung_run(repo, "hung-stop", {"runtime_status": "user_stop"})
    finished = _hung_run(repo, "hung-ok", {"runtime_status": "ok"})

    result = apply_run_closeout_backfill(repo, now=NOW)

    assert result["executed"] is True
    assert result["settled"] == {"failed": 1, "cancelled": 1, "done": 1}
    assert _run_status(repo, blocked) == "failed"
    assert _run_status(repo, stopped) == "cancelled"
    assert _run_status(repo, finished) == "done"
    assert len(_events(repo, blocked["agent_run_id"], "agent_run.completed")) == 1
    backfilled = _events(repo, blocked["agent_run_id"], RUN_CLOSEOUT_BACKFILLED_EVENT)
    assert len(backfilled) == 1
    payload = json.loads(backfilled[0]["payload_json"])
    assert payload["run_id"] == "hung-blocked"
    assert payload["previous_status"] == "created"
    assert payload["new_status"] == "failed"
    assert payload["runtime_source"] == "run_closeout_backfill"
    with repo._runtime_connection() as conn:
        record = json.loads(conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (RUN_CLOSEOUT_BACKFILL_KEY,),
        ).fetchone()["value"])
    assert record["migration_id"] == RUN_CLOSEOUT_BACKFILL_KEY
    assert record["settled"] == {"failed": 1, "cancelled": 1, "done": 1}
    assert record["task_runs_closed"] == 3
    for rec in (blocked, stopped, finished):
        assert _task_run_row(repo, rec["task_run_id"])["closed_at"] > 0


def test_apply_skips_continuable_waiting_and_recent(repo):
    """可续跑族、等用户族、最近活动（未过时间下限）三类不动。"""
    continuable = _hung_run(repo, "hung-cont", {
        "runtime_status": "unfinished",
        "runtime_reason": "TOOL_ROUND_LIMIT_REACHED",
        "runtime_source": "tool_loop",
    })
    waiting = _hung_run(repo, "hung-wait", {"runtime_status": "needs_user_input"})
    recent = _hung_run(repo, "hung-recent", BLOCKED_FACT, ended_at=NOW - 3600)

    result = apply_run_closeout_backfill(repo, now=NOW)

    assert result["executed"] is True
    assert result["settled"] == {"failed": 0, "cancelled": 0, "done": 0}
    for rec in (continuable, waiting, recent):
        assert _run_status(repo, rec) == "created"
    assert result["candidates"] == 2  # recent 未过时间下限，不在候选
    assert result["skipped"]["kept_open"] == 2


def test_apply_skips_unsettled_locked_unknown_and_no_fact(repo):
    """未结算操作、活跃执行锁、unknown attempt、无结束事实四类不动。"""
    unsettled = _hung_run(repo, "hung-ops", BLOCKED_FACT)
    locked = _hung_run(repo, "hung-locked", BLOCKED_FACT)
    unknown = _hung_run(repo, "hung-unknown", BLOCKED_FACT)
    no_fact = _hung_run(repo, "hung-nofact", BLOCKED_FACT)
    _insert_executing_op(repo, unsettled)
    _insert_active_lock(repo, locked)
    _mark_attempt_unknown(repo, unknown)
    _drop_completion_fact(repo, no_fact)

    result = apply_run_closeout_backfill(repo, now=NOW)

    assert result["executed"] is True
    assert result["settled"] == {"failed": 0, "cancelled": 0, "done": 0}
    for rec in (unsettled, locked, unknown, no_fact):
        assert _run_status(repo, rec) == "created"
    assert result["candidates"] == 4
    assert result["skipped"]["unsettled_operations"] == 1
    assert result["skipped"]["active_exec_lock"] == 1
    assert result["skipped"]["attempt_not_quiescent"] == 1
    assert result["skipped"]["no_completion_fact"] == 1


def test_apply_is_one_shot(repo):
    """迁移只执行一次：第二次调用是空操作，迁移记录不变、无新事件。"""
    rec = _hung_run(repo, "hung-once", BLOCKED_FACT)
    first = apply_run_closeout_backfill(repo, now=NOW)
    assert first["executed"] is True
    with repo._runtime_connection() as conn:
        record_before = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (RUN_CLOSEOUT_BACKFILL_KEY,),
        ).fetchone()["value"]

    second = apply_run_closeout_backfill(repo, now=NOW + 100)

    assert second == {
        "executed": False, "reason": "already_migrated", "migration_id": RUN_CLOSEOUT_BACKFILL_KEY,
    }
    with repo._runtime_connection() as conn:
        record_after = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (RUN_CLOSEOUT_BACKFILL_KEY,),
        ).fetchone()["value"]
    assert record_after == record_before
    assert len(_events(repo, rec["agent_run_id"], RUN_CLOSEOUT_BACKFILLED_EVENT)) == 1
    assert _run_status(repo, rec) == "failed"


def test_preview_is_read_only_and_counts_agree(repo):
    """预览只读（库文件字节与 mtime 不变），计数与应用结果一致。"""
    _hung_run(repo, "hung-p1", BLOCKED_FACT)
    _hung_run(repo, "hung-p2", {"runtime_status": "needs_user_input"})
    db_path = repo.db_path
    before_bytes = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    preview = preview_run_closeout_backfill(db_path, now=NOW)

    assert preview["already_migrated"] is False
    assert preview["eligible"]["failed"] == 1
    assert preview["skipped"]["kept_open"] == 1
    assert db_path.read_bytes() == before_bytes
    assert db_path.stat().st_mtime_ns == before_mtime

    result = apply_run_closeout_backfill(repo, now=NOW)
    assert result["settled"]["failed"] == preview["eligible"]["failed"]


def test_backfill_family_matches_runtime_mixin_nonterminal_closeout():
    """分族对拍：非终态域内与 runtime_mixin._nonterminal_run_closeout_status 逐例一致
    （rcb 独立实现只为避免跨层 import，口径漂移必须被这里抓住）。
    rcob 核对：这里的 "blocked" 是主代理收口写入的 runtime_status 域（协议违规等，照旧收
    failed）；子代理 BLOCKED 是可恢复等待，不写 agent_attempt.completed 事实、attempt 也不
    终态，天然不在本对拍域与候选内（见 test_apply_skips_waiting_attempt_without_end）。"""
    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.runtime_db.run_closeout_backfill import _backfill_target_status

    nonterminal_cases = [
        ("blocked", "MISSING_EVIDENCE", "acceptance_gate"),
        ("blocked", "TOOL_PROTOCOL_VIOLATION", "tool_loop"),
        ("unfinished", "TOOL_ROUND_LIMIT_REACHED", "tool_loop"),
        ("unfinished", "TOOL_OPERATION_OUTCOME_UNKNOWN", "tool_loop"),
        ("unfinished", "TOOL_CALL_UNCLOSED", "tool_protocol_adapter"),
        ("needs_user_input", "", ""),
        ("approval_required", "", ""),
        ("context_overflow", "CONTEXT_OVERFLOW", "preflight"),
    ]
    for status, reason, source in nonterminal_cases:
        assert _backfill_target_status(status, reason, source) == (
            runtime_mixin._nonterminal_run_closeout_status(status, reason, source)
        ), (status, reason, source)
    # 终态别名由 backfill 独立处理（与 runtime_mixin._RUN_STATUS_ALIASES 同口径）。
    assert _backfill_target_status("ok", "", "") == "done"
    assert _backfill_target_status("user_stop", "", "") == "cancelled"
    assert _backfill_target_status("conversation_control", "", "") == "cancelled"


def test_apply_skips_waiting_attempt_without_end(repo):
    """rcob：可恢复等待的真实形态——run created、attempt 仍在跑（ended_at=0，子代理
    BLOCKED 等批复就是这个形态）不在候选，不会被补收口。"""
    repo.record_run_creation(owner_id="local/main", goal="blocked-wait", run_id="hung-waiting", role="worker")

    result = apply_run_closeout_backfill(repo, now=NOW)

    assert result["candidates"] == 0
    assert result["settled"] == {"failed": 0, "cancelled": 0, "done": 0}


def test_owner_maintenance_runs_backfill_once(tmp_path):
    """owner 维护集成：到期维护跑一次迁移并把回执写进 maintenance.json；
    下一轮维护是空操作（already_migrated）。"""
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path)
    repo = RuntimeRepository(runtime_db_path(Path(home.owner_home_dir)))
    _hung_run(repo, "hung-maint", BLOCKED_FACT)

    first = run_owner_retention_if_due(home, now=NOW)
    assert first.ran is True
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["run_closeout_backfill"]["executed"] is True
    assert marker["run_closeout_backfill"]["settled"]["failed"] == 1

    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    policy["maintenance_interval_seconds"] = 100
    home.owner_retention_json.write_text(json.dumps(policy), encoding="utf-8")
    second = run_owner_retention_if_due(home, now=NOW + 200)
    assert second.ran is True
    marker2 = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker2["run_closeout_backfill"]["executed"] is False
    assert marker2["run_closeout_backfill"]["reason"] == "already_migrated"


def test_apply_settles_recovered_attempt_as_cancelled(repo):
    """rcb-v1 修订：recovered（人工处置过 unknown）且无未结算操作/无锁/超宽限 → cancelled，
    runtime_reason=attempt_recovered；预览单独计数这一类。"""
    rec = _hung_run(repo, "hung-recovered", BLOCKED_FACT)
    _make_recovered_attempt(repo, rec)

    preview = preview_run_closeout_backfill(repo.db_path, now=NOW)
    assert preview["eligible"]["cancelled"] == 1
    assert preview["recovered_cancelled"] == 1

    result = apply_run_closeout_backfill(repo, now=NOW)

    assert result["settled"]["cancelled"] == 1
    assert result["recovered_cancelled"] == 1
    assert _run_status(repo, rec) == "cancelled"
    completed = _events(repo, rec["agent_run_id"], "agent_run.completed")
    assert len(completed) == 1
    payload = json.loads(completed[0]["payload_json"])
    assert payload["runtime_reason"] == "attempt_recovered"
    assert payload["runtime_source"] == "run_closeout_backfill"


def test_apply_skips_recovered_with_unsettled_or_locked(repo):
    """recovered 但有未结算操作或活跃执行锁 → 不动（与其它路径同一套前置检查）。"""
    unsettled = _hung_run(repo, "hung-rec-ops", BLOCKED_FACT)
    _make_recovered_attempt(repo, unsettled)
    _insert_executing_op(repo, unsettled)
    locked = _hung_run(repo, "hung-rec-locked", BLOCKED_FACT)
    _make_recovered_attempt(repo, locked)
    _insert_active_lock(repo, locked)

    result = apply_run_closeout_backfill(repo, now=NOW)

    assert result["settled"] == {"failed": 0, "cancelled": 0, "done": 0}
    assert result["recovered_cancelled"] == 0
    assert _run_status(repo, unsettled) == "created"
    assert _run_status(repo, locked) == "created"
    assert result["skipped"]["unsettled_operations"] == 1
    assert result["skipped"]["active_exec_lock"] == 1
