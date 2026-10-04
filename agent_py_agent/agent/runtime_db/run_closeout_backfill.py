# LLM: 历史悬挂 run 的一次性结构化补收口迁移（rcb）。背景：rco 修好主链路前，非终态结束
#   （blocked 等）的尝试只关 attempt、run/task_run 停在 created。本模块是**显式一次性迁移**，
#   不是永久兜底清扫：迁移记录写 runtime.db 的 metadata（key=RUN_CLOSEOUT_BACKFILL_KEY，含迁移
#   ID/执行时间/分类计数），处理完成后同一库重复调用是空操作；处理本身幂等（已补的 run 不再
#   满足候选条件），所以中途崩溃后重跑只会补剩余部分。
#   判定只读结构化字段：attempt 已静止（终态去掉 unknown——结果不明必须等人工恢复）、无未结算
#   操作（unsettled_attempt_operations）、无活跃执行权锁（has_active_exec_lock，持主存活即视为
#   在跑）、尝试结束时间超过宽限下限（RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS，避免与刚结束、正在
#   收口的回合抢）。rcb-v1 修订（2026-10-04，3a 裁定）：attempt 状态是 recovered（人工处置过
#   unknown、没有正常完成）且没有结束事实时补成 cancelled（runtime_reason=attempt_recovered，
#   与 run_takeover 的 TAKEN_OVER→cancelled 同族）；其余没有结束事实的记录仍跳过。
#   终态分族与 agent_core/runtime_mixin._nonterminal_run_closeout_status 同口径
#   （等用户族/可续跑族一律不动；取消族→cancelled；其余不可续跑族→failed；ok→done 如实补账），
#   该函数在本模块独立实现是为了避免 runtime_db → agent_core 的跨层 import，一致性由
#   test_run_closeout_backfill.py 的对拍用例钉住。
#   每条补收口：settle_agent_run 精确 CAS（expected_attempt_status）收口 run 并写 agent_run.completed，
#   随后追加 run_closeout.backfilled 审计事件（字段 run_id/旧状态/新状态/依据的结构化原因），
#   最后对涉及的 task_run 用树终态 CAS 补关（link 闸留给发现层——本模块没有会话存储访问）。
#   预览函数只读打开（RuntimeRepository(read_only=True)，SQLite URI mode=ro），不改任何字节。
# 模块用途: 把历史悬挂的 agent_run/task_run 按结构化判定补到终态，一次执行、可审计、可预览。
from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any

from .operations import AGENT_RUN_TERMINAL_STATUSES, ATTEMPT_STATUS_RECOVERED
from .repository import _QUIESCENT_ATTEMPT_STATUSES, RuntimeRepository

_LOGGER = logging.getLogger(__name__)

#: metadata 幂等键（迁移 ID 即键名；值 JSON 带迁移 ID、执行时间、分类计数）。
#: 版本决策（rcb-v1 修订，2026-10-04）：recovered→cancelled 的规则扩展**不升版本**——
#:   生产与开发环境都没有库执行过 v1（3a 在 21 个 owner 的只读副本上预览确认），
#:   v1 也尚未并入任何部署分支；升 v2 只会留下"v1 键在但规则已变"的歧义与无对象的历史处理。
RUN_CLOSEOUT_BACKFILL_KEY = "run_closeout_backfill.v1"
#: 补收口宽限下限（秒）：只处理尝试结束超过该时长的记录，避免和刚结束、正在收口的回合抢。
#: 维护循环默认每天跑一次，6 小时足以让任何真实收口/续跑/重试先完成，又远小于"隔夜悬挂"。
RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS = 6 * 3600
#: 每条补收口的审计事件类型（结构化字段，不写正文）。
RUN_CLOSEOUT_BACKFILLED_EVENT = "run_closeout.backfilled"
#: 收口来源标记（结构化，不从正文推断）。
RUN_CLOSEOUT_BACKFILL_SOURCE = "run_closeout_backfill"
#: 普通补收口的依据原因（结束事实分族路径）。
RUN_CLOSEOUT_BACKFILL_REASON = "historical_hung_run_backfill"
#: recovered 特判补收口的依据原因（3a 裁定；与 run_takeover 的 TAKEN_OVER→cancelled 同族）。
RECOVERED_BACKFILL_REASON = "attempt_recovered"

# 等待用户族（与 runtime_mixin._WAITING_USER_RUNTIME_STATUSES 同口径）：保留非终态等用户动作。
_WAITING_USER_RUNTIME_STATUSES = frozenset({"needs_user_input", "approval_required"})
# 终态别名（与 runtime_mixin._RUN_STATUS_ALIASES 同口径）：ok→done、取消族→cancelled。
_RUN_STATUS_ALIASES = {"ok": "done", "user_stop": "cancelled", "conversation_control": "cancelled"}


# LLM: 与 runtime_mixin._nonterminal_run_closeout_status 同口径的独立实现（见模块头注释）：
#   等用户族保留；其余非终态用共享技术续跑 gate 分族——可续跑族保留非终态，不可续跑族收口
#   failed；终态别名（ok→done 等）如实补账。返回空串表示"不动"。
# 函数用途: 给一条历史结束事实选出补收口终态；空串表示跳过。
def _backfill_target_status(runtime_status: str, runtime_reason: str, runtime_source: str) -> str:
    terminal = _RUN_STATUS_ALIASES.get(runtime_status, runtime_status or "")
    if terminal in AGENT_RUN_TERMINAL_STATUSES:
        return terminal
    if terminal in ("", "created", "running"):
        return ""
    if runtime_status in _WAITING_USER_RUNTIME_STATUSES:
        return ""
    from ..turn_end import should_continue_task

    should, _ = should_continue_task(
        SimpleNamespace(
            runtime_status=runtime_status,
            runtime_reason=runtime_reason,
            runtime_source=runtime_source,
        )
    )
    return "" if should else "failed"


# LLM: 候选只读一条 SQL：run 未终态、current attempt 已结束且超过宽限下限。unknown attempt
#   不在这里排除，交给逐条分类（unknown 在静止集之外，会被 attempt_not_quiescent 跳过）。
# 函数用途: 列出可能悬挂的 run 行，供逐条分类。
def _scan_candidates(conn: Any, *, cutoff: float) -> list[Any]:
    return conn.execute(
        """
        SELECT r.agent_run_id, r.run_id, r.status AS run_status, r.task_run_id,
               r.current_attempt_id, a.status AS attempt_status, a.ended_at AS attempt_ended_at
        FROM agent_runs r JOIN agent_attempts a ON a.attempt_id = r.current_attempt_id
        WHERE r.status IN ('', 'created') AND a.ended_at > 0 AND a.ended_at <= ?
        ORDER BY a.ended_at
        """,
        (cutoff,),
    ).fetchall()


# LLM: 结束事实取该 attempt 最新的 agent_attempt.completed 事件 payload（runtime_status/
#   runtime_reason/runtime_source 是 rco 收口写入的结构化字段）；没有该事件的历史记录不补，
#   避免新造分族判据。坏 JSON 视为无事实。
# 函数用途: 读一条尝试结束的结构化事实；没有则返回 None。
def _completion_fact(repo: RuntimeRepository, attempt_id: str) -> dict[str, Any] | None:
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runtime_events WHERE attempt_id = ? "
            "AND event_type = 'agent_attempt.completed' ORDER BY seq DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


# LLM: 逐条分类只做结构化判定（静止集/未结算操作/活跃锁/结束事实/分族），返回
#   (目标终态, 跳过原因, 补收口依据原因)；目标终态为空串表示不动。判定顺序先排除"可能还在跑"的
#   形态。rcb-v1 修订：recovered（人工处置过 unknown）没有结束事实时补成 cancelled、
#   依据原因记 attempt_recovered；unsettled/lock 检查在这条之前，recovered 同样受其约束。
# 函数用途: 判定一条候选 run 能不能补收口、补成什么、依据原因是什么。
def _classify_candidate(repo: RuntimeRepository, row: Any, *, now: float) -> tuple[str, str, str]:
    if str(row["attempt_status"]) not in _QUIESCENT_ATTEMPT_STATUSES:
        return "", "attempt_not_quiescent", ""
    attempt_id = str(row["current_attempt_id"])
    if repo.unsettled_attempt_operations(attempt_id):
        return "", "unsettled_operations", ""
    if repo.has_active_exec_lock(str(row["agent_run_id"]), now=now):
        return "", "active_exec_lock", ""
    fact = _completion_fact(repo, attempt_id)
    if fact is None:
        if str(row["attempt_status"]) == ATTEMPT_STATUS_RECOVERED:
            return "cancelled", "", RECOVERED_BACKFILL_REASON
        return "", "no_completion_fact", ""
    target = _backfill_target_status(
        str(fact.get("runtime_status") or ""),
        str(fact.get("runtime_reason") or ""),
        str(fact.get("runtime_source") or ""),
    )
    return (target, "", RUN_CLOSEOUT_BACKFILL_REASON) if target else ("", "kept_open", "")


# LLM: 补收口一条：settle_agent_run 用精确 attempt CAS 收口（成功即写 agent_run.completed），
#   随后追加 run_closeout.backfilled 审计事件。事件写失败不回滚已收口的 run（如实计入
#   event_write_errors），因为收口本身才是主事实。decision=(目标终态, 依据原因) 由分类决定。
# 函数用途: 把一条悬挂 run 补到目标终态并留审计事件。
def _apply_candidate(repo: RuntimeRepository, row: Any, decision: tuple[str, str], *, now: float) -> tuple[bool, bool]:
    target, runtime_reason = decision
    result = repo.settle_agent_run(
        agent_run_id=str(row["agent_run_id"]),
        status=target,
        attempt_id=str(row["current_attempt_id"]),
        expected_attempt_status=str(row["attempt_status"]),
        payload={
            "status": target,
            "runtime_status": "backfilled",
            "runtime_reason": runtime_reason,
            "runtime_source": RUN_CLOSEOUT_BACKFILL_SOURCE,
            "run_id": str(row["run_id"]),
            "previous_run_status": str(row["run_status"]),
        },
        now=now,
    )
    if not bool(result.get("settled")):
        return False, False
    event_written = _write_backfilled_event(repo, row, decision, now=now)
    return True, event_written


# LLM: 审计事件与收口分开两个事务（settle 自带事务不可嵌套）：失败只记日志并返回 False，
#   由调用方计入 event_write_errors，绝不影响已提交的收口。
# 函数用途: 为一条补收口追加 run_closeout.backfilled 结构化事件。
def _write_backfilled_event(repo: RuntimeRepository, row: Any, decision: tuple[str, str], *, now: float) -> bool:
    target, runtime_reason = decision
    try:
        with repo.transaction() as conn:
            repo._append_event_conn(
                conn,
                event_type=RUN_CLOSEOUT_BACKFILLED_EVENT,
                attempt_id=str(row["current_attempt_id"]),
                agent_run_id=str(row["agent_run_id"]),
                task_run_id=str(row["task_run_id"]),
                payload={
                    "run_id": str(row["run_id"]),
                    "previous_status": str(row["run_status"]),
                    "new_status": target,
                    "runtime_status": "backfilled",
                    "runtime_reason": runtime_reason,
                    "runtime_source": RUN_CLOSEOUT_BACKFILL_SOURCE,
                },
            )
        return True
    except Exception:  # noqa: BLE001 审计事件失败不反噬已收口的运行
        _LOGGER.warning(
            "run_closeout.backfilled 事件写入失败（run 已收口）：agent_run_id=%s",
            str(row["agent_run_id"]), exc_info=True,
        )
        return False


# LLM: 迁移主入口：先查 metadata 幂等键（已完成即空操作），再扫描→逐条分类→补收口→
#   对涉及的 task_run 补关，最后写迁移记录。重复调用是空操作；中途崩溃后重跑只补剩余
#   （处理幂等），结果计数如实写进迁移记录与返回值。
# 函数用途: 对一个 owner 的 runtime.db 执行一次历史悬挂补收口迁移。
def apply_run_closeout_backfill(repo: RuntimeRepository, *, now: float | None = None) -> dict[str, Any]:
    current = float(now if now is not None else time.time())
    if _migration_record(repo) is not None:
        return {"executed": False, "reason": "already_migrated", "migration_id": RUN_CLOSEOUT_BACKFILL_KEY}
    cutoff = current - RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS
    with repo._runtime_connection() as conn:
        candidates = _scan_candidates(conn, cutoff=cutoff)
    counts: dict[str, int] = {"failed": 0, "cancelled": 0, "done": 0}
    recovered_cancelled = 0
    skipped: dict[str, int] = {}
    event_write_errors = 0
    touched_task_runs: list[str] = []
    for row in candidates:
        target, skip_reason, runtime_reason = _classify_candidate(repo, row, now=current)
        if not target:
            skipped[skip_reason] = skipped.get(skip_reason, 0) + 1
            continue
        settled, event_written = _apply_candidate(repo, row, (target, runtime_reason), now=current)
        if not settled:
            skipped["settle_rejected"] = skipped.get("settle_rejected", 0) + 1
            continue
        counts[target] = counts.get(target, 0) + 1
        if runtime_reason == RECOVERED_BACKFILL_REASON:
            recovered_cancelled += 1
        if not event_written:
            event_write_errors += 1
        task_run_id = str(row["task_run_id"] or "")
        if task_run_id and task_run_id not in touched_task_runs:
            touched_task_runs.append(task_run_id)
    task_runs_closed = _close_touched_task_runs(repo, touched_task_runs)
    record = {
        "migration_id": RUN_CLOSEOUT_BACKFILL_KEY,
        "executed_at": current,
        "grace_seconds": RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS,
        "candidates": len(candidates),
        "settled": counts,
        "recovered_cancelled": recovered_cancelled,
        "skipped": skipped,
        "task_runs_closed": task_runs_closed,
        "event_write_errors": event_write_errors,
    }
    _write_migration_record(repo, record)
    return {"executed": True, **record}


# LLM: 涉及的 task_run 用现有树终态 CAS 补关（根 run 已收口后树即终态）；单条失败只记日志，
#   不阻断其余 task_run 与迁移记录。link 闸不在本模块做（没有会话存储访问），
#   活跃 link 的 task_run 由发现层的补关通道按原口径处理。
# 函数用途: 对本次补收口涉及的 task_run 尝试关闭，返回实际关掉的条数。
def _close_touched_task_runs(repo: RuntimeRepository, task_run_ids: list[str]) -> int:
    closed = 0
    for task_run_id in task_run_ids:
        try:
            result = repo.settle_task_run_if_agent_tree_terminal(task_run_id=task_run_id)
        except Exception:  # noqa: BLE001 单条 task_run 失败不影响迁移记录
            _LOGGER.warning("task_run 补关失败：task_run_id=%s", task_run_id, exc_info=True)
            continue
        if bool(result.get("settled")):
            closed += 1
    return closed


# LLM: 迁移记录只读 metadata 键；坏 JSON 或非对象视为无记录（按未迁移处理，重跑幂等）。
# 函数用途: 读本库的迁移记录；没有则返回 None。
def _migration_record(repo: RuntimeRepository) -> dict[str, Any] | None:
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (RUN_CLOSEOUT_BACKFILL_KEY,),
        ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row["value"] or "{}"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# LLM: 迁移记录写 metadata（与 pending_events_page 的游标同一种 upsert 写法）；写失败上抛——
#   没有记录时下次维护会重跑（幂等），但本轮必须让调用方知道没记上。
# 函数用途: 把本次迁移的结构化记录写进 metadata。
def _write_migration_record(repo: RuntimeRepository, record: dict[str, Any]) -> None:
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (RUN_CLOSEOUT_BACKFILL_KEY, json.dumps(record, ensure_ascii=False)),
        )


# LLM: 只读预览：给一个 runtime.db 路径，只读打开（不初始化 schema、不写任何字节），
#   返回会补哪些类、各多少条、哪些跳过，供生产只读副本核对数字。与应用函数同一套分类判据。
# 函数用途: 预览一次迁移会做的分类与计数，不改动数据库。
def preview_run_closeout_backfill(db_path: Any, *, now: float | None = None) -> dict[str, Any]:
    current = float(now if now is not None else time.time())
    repo = RuntimeRepository(db_path, read_only=True)
    record = _migration_record(repo)
    cutoff = current - RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS
    with repo._runtime_connection() as conn:
        candidates = _scan_candidates(conn, cutoff=cutoff)
    eligible: dict[str, int] = {"failed": 0, "cancelled": 0, "done": 0}
    recovered_cancelled = 0
    skipped: dict[str, int] = {}
    for row in candidates:
        target, skip_reason, runtime_reason = _classify_candidate(repo, row, now=current)
        if not target:
            skipped[skip_reason] = skipped.get(skip_reason, 0) + 1
            continue
        eligible[target] = eligible.get(target, 0) + 1
        if runtime_reason == RECOVERED_BACKFILL_REASON:
            recovered_cancelled += 1
    return {
        "migration_id": RUN_CLOSEOUT_BACKFILL_KEY,
        "already_migrated": record is not None,
        "candidates": len(candidates),
        "eligible": eligible,
        # 其中来自 recovered 特判（人工处置过 unknown）的条数，供核数时单独看这一类。
        "recovered_cancelled": recovered_cancelled,
        "skipped": skipped,
    }


__all__ = [
    "RECOVERED_BACKFILL_REASON",
    "RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS",
    "RUN_CLOSEOUT_BACKFILL_KEY",
    "apply_run_closeout_backfill",
    "preview_run_closeout_backfill",
]
