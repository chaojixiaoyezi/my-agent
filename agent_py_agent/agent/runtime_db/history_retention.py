"""按持久游标小批瘦身或删除 runtime.db 终态历史，不改运行状态、时间戳或表结构。"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from .operations import (
    _ATTEMPT_TERMINAL_STATUSES,
    AGENT_RUN_TERMINAL_STATUSES,
    OP_CANCELLED,
    OP_FAILED,
    OP_SUCCEEDED,
)


@contextmanager
def _history_transaction(conn):
    """读状态前取得写事务；独立调用负责提交或回滚，已有事务由调用方收尾。"""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if owns_transaction:
            conn.rollback()
        raise
    else:
        if owns_transaction:
            conn.commit()


def _history_cursor(conn, table: str) -> int:
    """只读 metadata 中唯一的行号续点，不以通道投影覆盖数据库游标。"""
    row = conn.execute("SELECT value FROM metadata WHERE key=?",
                       ("history_retention_cursor:" + table,)).fetchone()
    return max(0, int(row[0])) if row is not None else 0


def _save_history_cursor(conn, table: str, cursor: int) -> None:
    """在同批事务中保存续点，回滚时不会越过未处理历史。"""
    conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 ("history_retention_cursor:" + table, str(cursor)))


def _history_window(conn, *, table: str, query: str, cutoff: float, max_rows: int, time_column: int):
    """读取有界窗口，近期行之前停止；只有真正到尾才将持久续点归零。"""
    if type(max_rows) is not int or max_rows <= 0:
        raise ValueError("历史保留批次条数必须是正整数")
    cursor = _history_cursor(conn, table)
    rows = conn.execute(query, (cursor, max_rows)).fetchall()
    old = []
    for row in rows:
        if row[time_column] >= cutoff:
            return old, cursor, True, len(old) + 1
        old.append(row)
        cursor = row[0]
    complete = len(rows) < max_rows
    return old, 0 if complete else cursor, complete, len(rows)


def _run_states(conn, identities) -> dict[str, str]:
    """一次批量读取本片 AgentRun 状态，不在候选循环中查询数据库。"""
    identities = sorted(set(identities) - {""})
    if not identities:
        return {}
    slots = ",".join("?" for _ in identities)
    return dict(conn.execute(f"SELECT agent_run_id,status FROM agent_runs WHERE agent_run_id IN ({slots})",
                             identities).fetchall())


def slim_terminal_tool_outcomes_step(conn, *, cutoff, max_rows) -> dict:
    """同事务瘦身到期且所属运行已终态的工具结果；CAS 保留原 status 与 updated_at。"""
    with _history_transaction(conn):
        rows, cursor, complete, items = _history_window(
            conn, table="tool_operations", cutoff=cutoff, max_rows=max_rows, time_column=4,
            query="SELECT t.rowid,t.operation_id,t.status,t.settled_at,t.created_at,t.updated_at,"
                  "t.agent_run_id,length(t.outcome_json) FROM tool_operations t "
                  "WHERE t.rowid>? ORDER BY t.rowid LIMIT ?")
        states = _run_states(conn, (row[6] for row in rows))
        eligible = {row[1]: row for row in rows
                    if row[2] in (OP_SUCCEEDED, OP_FAILED, OP_CANCELLED)
                    and 0 < row[3] < cutoff and states.get(row[6]) in AGENT_RUN_TERMINAL_STATUSES}
        slimmed, released = 0, 0
        if eligible:
            slots = ",".join("?" for _ in eligible)
            # 原文只批量读取一次，摘要与改写共享同一写事务快照。
            outcomes = conn.execute(
                f"SELECT operation_id,outcome_json FROM tool_operations WHERE operation_id IN ({slots}) "
                "AND json_extract(outcome_json,'$.retention') IS NOT 'slimmed'", tuple(eligible)).fetchall()
            for operation_id, original in outcomes:
                row = eligible[operation_id]
                raw = original.encode("utf-8")
                replacement = json.dumps({"retention": "slimmed", "original_sha256": hashlib.sha256(raw).hexdigest(),
                                          "original_bytes": len(raw)})
                changed = conn.execute(
                    "UPDATE tool_operations SET outcome_json=? WHERE operation_id=? AND status=? AND updated_at=?",
                    (replacement, operation_id, row[2], row[5])).rowcount
                slimmed += changed
                released += changed * max(0, len(raw) - len(replacement.encode("utf-8")))
        _save_history_cursor(conn, "tool_operations", cursor)
        return {"cursor": cursor, "complete": complete, "items": items, "bytes": released, "slimmed": slimmed}


def prune_terminal_runtime_events_step(conn, *, cutoff, max_rows) -> dict:
    """同事务删除终态运行的到期事件；活动 attempt、未知身份和近期事件始终保留。"""
    with _history_transaction(conn):
        rows, cursor, complete, items = _history_window(
            conn, table="runtime_events", cutoff=cutoff, max_rows=max_rows, time_column=1,
            query="SELECT seq,created_at,agent_run_id,task_run_id FROM runtime_events "
                  "WHERE seq>? ORDER BY seq LIMIT ?")
        states = _run_states(conn, (row[2] for row in rows))
        unsettled = set()
        if states:
            terminal = sorted(_ATTEMPT_TERMINAL_STATUSES)
            slots = ",".join("?" for _ in states)
            terminal_slots = ",".join("?" for _ in terminal)
            unsettled = {row[0] for row in conn.execute(
                f"SELECT DISTINCT agent_run_id FROM agent_attempts WHERE agent_run_id IN ({slots}) "
                f"AND status NOT IN ({terminal_slots})", (*states, *terminal)).fetchall()}
        task_ids = sorted({row[3] for row in rows if not row[2] and row[3]})
        tasks = {}
        if task_ids:
            slots = ",".join("?" for _ in task_ids)
            tasks = dict(conn.execute(f"SELECT task_run_id,status FROM task_runs WHERE task_run_id IN ({slots})",
                                      task_ids).fetchall())
        eligible = [seq for seq, _, agent_id, task_id in rows
                    if (states.get(agent_id) in AGENT_RUN_TERMINAL_STATUSES and agent_id not in unsettled
                        if agent_id else tasks.get(task_id) in AGENT_RUN_TERMINAL_STATUSES)]
        deleted = 0
        if eligible:
            slots = ",".join("?" for _ in eligible)
            deleted = conn.execute(f"DELETE FROM runtime_events WHERE seq IN ({slots})", eligible).rowcount
        _save_history_cursor(conn, "runtime_events", cursor)
        return {"cursor": cursor, "complete": complete, "items": items, "bytes": 0, "deleted_events": deleted}


def prune_llm_call_records_step(conn, *, owner_id, cutoff, max_rows) -> dict:
    """沿 owner 时间索引删除有界终态模型记录；终态取值与原记录写入方一致，不补建表。"""
    if type(max_rows) is not int or max_rows <= 0:
        raise ValueError("历史保留批次条数必须是正整数")
    with _history_transaction(conn):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_call_records'").fetchone() is None:
            return {"skipped": "table_absent"}
        # gap158_w1_llm_records._status 仅写这三种已结束状态，finished 是输入状态而非存储状态。
        rows = conn.execute(
            "SELECT record_id FROM llm_call_records WHERE owner_id=? AND created_at<? "
            "AND status IN ('completed','failed','cancelled') ORDER BY created_at,record_id LIMIT ?",
            (owner_id, cutoff, max_rows)).fetchall()
        deleted = 0
        if rows:
            identities = [row[0] for row in rows]
            slots = ",".join("?" for _ in identities)
            deleted = conn.execute(f"DELETE FROM llm_call_records WHERE record_id IN ({slots})", identities).rowcount
        complete = len(rows) < max_rows
        return {"cursor": "" if complete else rows[-1][0], "complete": complete, "items": len(rows),
                "bytes": 0, "deleted_llm_records": deleted}


def maintain_runtime_history_lane(request) -> dict:
    """在通道写门内分批改写或删除 owner 终态历史；策略关闭、保全或预算耗尽时不执行。"""
    from ..memory_store.retention_scan import _load_policy
    from ..memory_store.retention_scan_budget import RETENTION_SCAN_SLICE_ENTRIES
    from .repository import RuntimeRepository
    from .schema import runtime_db_path

    receipt = {"cursor": {}, "complete": True, "items": 0, "bytes": 0}
    db_path = runtime_db_path(Path(request.home.owner_home_dir))
    if not db_path.exists():
        return receipt
    receipt["cursor"] = dict(request.cursor)
    if request.legal_hold:
        receipt["deferred_reason"] = "legal_hold"
        return receipt
    # 复用严格策略加载，不自行解析 retention.json，也不以旧 tool_output_days 覆盖 v2 正式字段。
    policy, error = _load_policy(Path(request.home.owner_retention_json))
    if error is not None:
        receipt.update(complete=False, deferred_reason="retention_not_safe")
        return receipt
    if policy.legal_hold or not policy.maintenance_enabled:
        receipt["deferred_reason"] = "legal_hold" if policy.legal_hold else "maintenance_disabled"
        return receipt
    receipt.update(slimmed=0, deleted_events=0, deleted_llm_records=0)
    steps = [("tool_operations", policy.tool_output_days_after_terminal, slim_terminal_tool_outcomes_step),
             ("runtime_events", policy.audit_days, prune_terminal_runtime_events_step),
             ("llm_call_records", policy.audit_days, prune_llm_call_records_step)]
    steps = [(table, days, step) for table, days, step in steps if days > 0]
    if not steps:
        return receipt
    # 只打开既有库，避免维护入口隐式补表、补索引或在删除后的 owner 目录重新建库。
    repo = RuntimeRepository(db_path, existing_only=True)
    with repo._runtime_connection() as conn:
        for table in ("tool_operations", "runtime_events"):
            receipt["cursor"][table] = _history_cursor(conn, table)
    receipt["cursor"].setdefault("llm_call_records", "")
    remaining = RETENTION_SCAN_SLICE_ENTRIES
    if request.budget.max_items is not None:
        remaining = min(remaining, request.budget.max_items)
    for index, (table, days, step) in enumerate(steps):
        if remaining <= 0 or request.budget.remaining_ms() <= 0:
            receipt.update(complete=False, deferred_reason="budget_exhausted")
            break
        # 为后续类别预留条数，整片累计检查量也不能超过请求上限。
        limit = max(1, remaining // (len(steps) - index))

        def execute_step():
            """在现有通道写门中借短事务，仅处理当前类别的终态行。"""
            with repo.transaction() as conn:
                kwargs = {"owner_id": request.home.owner_id} if table == "llm_call_records" else {}
                return step(conn, cutoff=request.now - days * 86400, max_rows=limit, **kwargs)

        result = request.run_write(execute_step)
        receipt["cursor"][table] = result.get("cursor", "")
        receipt["complete"] = receipt["complete"] and result.get("complete", True)
        remaining -= result.get("items", 0)
        for key in ("items", "bytes", "slimmed", "deleted_events", "deleted_llm_records"):
            receipt[key] += result.get(key, 0)
    return receipt
