# LLM: C只接原工具账本的本地脱敏/原checkpoint，JSON扫描门外、完整raw CAS与单txn末写门内；默认False原算法。
# 模块用途: 按原formal前隐私链清除账本正文，保原身份/终态与真实partial。
from __future__ import annotations

"""Exact plaintext redaction for structured tool ledgers.

Operation identity, status, hashes, and idempotency keys remain intact.  Only
caller-supplied plaintext is removed from JSON payload columns for one tool.
"""

import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

_DELETED_CONTENT = "[deleted-content]"


# LLM: 策略继承原Store.coordinator_io，不缓存source/新上下文；C仅获批固定叶，不能授其它mixin任意DML。
# 类用途: 擦除原工具账本中精确正文，保持唯一操作身份和原WAL清理。
class LocalStoreLedgerRedactionMixin:
    # LLM: 删除事实不能删掉幂等身份，也不能靠自然语言猜测；只按调用方给出的精确正文改写 JSON 载荷。
    # LLM: C显式分支只拆原JSON准备与同库raw CAS，secure_delete/commit/checkpoint顺序保持；不跳过condense触发。
    # 函数用途: 在原formal前脱敏精确工具正文，不改操作状态或重放身份。
    def redact_tool_ledger_content(
        self,
        *,
        tool: str,
        contents: Collection[str],
    ) -> int:
        clean_tool = str(tool or "").strip()
        variants = _content_variants(contents)
        if not clean_tool or not variants:
            return 0

        if self.coordinator_io:
            return self._redact_tool_ledger_coordinator(clean_tool, variants)
        changed_rows = 0
        with self._connection() as conn:
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("BEGIN IMMEDIATE")
            changed_rows += _redact_runtime_gate_rows(
                conn,
                tool=clean_tool,
                variants=variants,
            )
            changed_rows += _redact_tool_operation_rows(
                conn,
                tool=clean_tool,
                variants=variants,
            )
            conn.commit()
        self._truncate_redacted_wal()
        return changed_rows

    # LLM: WAL 可能保留更新前页；checkpoint 只是物理清理，忙时不能改变已完成的逻辑删除结果。
    # LLM: C只把原helper完整调用交当前scope，普通sqlite busy保持原语义，实际native对象不得吞或换源补偿。
    # 函数用途: 在原删除提交后尽力截断WAL，源失效保真实已提交partial。
    def _truncate_redacted_wal(self) -> None:
        if self.coordinator_io:
            return self._local_write(self._truncate_redacted_wal_current)
        return self._truncate_redacted_wal_current()

    # LLM: 复用原checkpoint实现与sqlite忙降级，C外层固定window已核源，原_connection不会借F1连接。
    # 函数用途: 对原库尽力截断已删正文WAL，普通busy不改逻辑删除事实。
    def _truncate_redacted_wal_current(self):
        try:
            with self._connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass


    # LLM: 原JSON递归/替换在Root外，BEGIN后完整raw CAS才原UPDATE/commit，formal前调用顺序由原上层保持。
    # 函数用途: 为C精确脱敏原两类工具账本，不将任意扫描或业务放入Root。
    def _redact_tool_ledger_coordinator(self, tool, variants):
        identity = _ledger_database_identity(self.db_path)
        with self._connection() as conn:
            runtime = _runtime_ledger_snapshot(conn, tool)
            operations = _operation_ledger_snapshot(conn, tool)
        if _ledger_database_identity(self.db_path) != identity:
            raise BlockingIOError("local_store_redaction_database_changed")
        plan = _prepare_ledger_redaction(runtime, operations, variants, identity)

        # LLM: 已持原SQLite写事务，先全raw CAS；仅执行门外准备的原字段UPDATE，不解析正文JSON。
        # 函数用途: 在原单库事务内原子应用精确脱敏，变化则零DML忙返回。
        def operation(conn):
            if (not _ledger_snapshot_equal(_runtime_ledger_snapshot(conn, tool), plan.runtime)
                    or not _ledger_snapshot_equal(_operation_ledger_snapshot(conn, tool), plan.operations)
                    or _ledger_database_identity(self.db_path) != plan.db_identity):
                raise BlockingIOError("local_store_redaction_snapshot_changed")
            for parameters, runtime_gate, run_id, operation_id in plan.runtime_updates:
                conn.execute(
                    "UPDATE runtime_gate_ledger SET parameters_json = ?, runtime_gate_json = ? "
                    "WHERE run_id = ? AND operation_id = ?",
                    (parameters, runtime_gate, run_id, operation_id),
                )
            for result, owner_id, run_id, operation_id in plan.operation_updates:
                conn.execute(
                    "UPDATE tool_operations SET result_json = ? "
                    "WHERE owner_id = ? AND run_id = ? AND operation_id = ?",
                    (result, owner_id, run_id, operation_id),
                )
            return len(plan.runtime_updates) + len(plan.operation_updates)
        changed = self._coordinator_transaction(operation, secure_delete=True)
        self._truncate_redacted_wal()
        return changed


def _redact_runtime_gate_rows(
    conn: sqlite3.Connection,
    *,
    tool: str,
    variants: tuple[str, ...],
) -> int:
    changed = 0
    rows = conn.execute(
        """
        SELECT run_id, operation_id, parameters_json, runtime_gate_json
        FROM runtime_gate_ledger
        WHERE tool = ?
        """,
        (tool,),
    ).fetchall()
    for row in rows:
        parameters, parameters_changed = _redact_json_text(row["parameters_json"], variants)
        runtime_gate, gate_changed = _redact_json_text(row["runtime_gate_json"], variants)
        if not (parameters_changed or gate_changed):
            continue
        conn.execute(
            """
            UPDATE runtime_gate_ledger
            SET parameters_json = ?, runtime_gate_json = ?
            WHERE run_id = ? AND operation_id = ?
            """,
            (parameters, runtime_gate, row["run_id"], row["operation_id"]),
        )
        changed += 1
    return changed


def _redact_tool_operation_rows(
    conn: sqlite3.Connection,
    *,
    tool: str,
    variants: tuple[str, ...],
) -> int:
    changed = 0
    rows = conn.execute(
        """
        SELECT owner_id, run_id, operation_id, result_json
        FROM tool_operations
        WHERE tool = ?
        """,
        (tool,),
    ).fetchall()
    for row in rows:
        result, result_changed = _redact_json_text(row["result_json"], variants)
        if not result_changed:
            continue
        conn.execute(
            """
            UPDATE tool_operations
            SET result_json = ?
            WHERE owner_id = ? AND run_id = ? AND operation_id = ?
            """,
            (result, row["owner_id"], row["run_id"], row["operation_id"]),
        )
        changed += 1
    return changed


def _redact_json_text(raw: object, variants: tuple[str, ...]) -> tuple[str, bool]:
    text = str(raw or "")
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return _replace_variants(text, variants)
    redacted, changed = _redact_value(decoded, variants)
    if not changed:
        return text, False
    return json.dumps(redacted, ensure_ascii=False, sort_keys=True), True


def _redact_value(value: Any, variants: tuple[str, ...]) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        result: dict[Any, Any] = {}
        for key, item in value.items():
            result[key], item_changed = _redact_value(item, variants)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        changed = False
        result: list[Any] = []
        for item in value:
            redacted, item_changed = _redact_value(item, variants)
            result.append(redacted)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, str):
        return _replace_variants(value, variants)
    return value, False


def _replace_variants(value: str, variants: tuple[str, ...]) -> tuple[str, bool]:
    redacted = value
    for content in variants:
        redacted = redacted.replace(content, _DELETED_CONTENT)
    return redacted, redacted != value


def _content_variants(contents: Collection[str]) -> tuple[str, ...]:
    variants: set[str] = set()
    for value in contents:
        content = str(value or "")
        if not content:
            continue
        variants.add(content)
        variants.add(json.dumps(content, ensure_ascii=False)[1:-1])
    return tuple(sorted(variants, key=len, reverse=True))


__all__ = ["LocalStoreLedgerRedactionMixin"]


# LLM: 仅本次栈内准备事实与原SQL参数，不是缓存/恢复账/权限；不保存source或事务连接。
# 类用途: 将完整原行与门外计算的脱敏结果传给同一次C本地事务。
@dataclass(frozen=True)
class _LedgerRedactionPlan:
    runtime: tuple
    operations: tuple
    runtime_updates: tuple
    operation_updates: tuple
    db_identity: tuple
    content_bytes: int


# LLM: 只按原tool参数匹配完整raw行，稳定原主键排序，不将hash/count/JSON规范化替代实际值。
# 函数用途: 读取待脱敏runtime账本的完整当前态供CAS。
def _runtime_ledger_snapshot(conn, tool):
    cursor = conn.execute("SELECT * FROM runtime_gate_ledger WHERE tool = ? ORDER BY run_id, operation_id", (tool,))
    return tuple(column[0] for column in cursor.description), tuple(tuple(row) for row in cursor.fetchall())


# LLM: 同原tool范围和完整三字段主键，任何字段/类型变化都使准备快照失效，不影响别的tool行。
# 函数用途: 读取原工具操作账本完整当前态供CAS。
def _operation_ledger_snapshot(conn, tool):
    cursor = conn.execute(
        "SELECT * FROM tool_operations WHERE tool = ? ORDER BY owner_id, run_id, operation_id", (tool,))
    return tuple(column[0] for column in cursor.description), tuple(tuple(row) for row in cursor.fetchall())


# LLM: 只比较本次原库文件身份，不用mtime将无关事务误当原行变化；真正行裁决在同SQL写事务全raw比较。
# 函数用途: 拒绝准备期间数据库文件被替换。
def _ledger_database_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


# LLM: 原sqlite标量类型和值都精确比较，不能将1/1.0、None或不同JSON字节折叠为相同。
# 函数用途: 在DML之前核对准备时的完整原行是否仍准确。
def _ledger_snapshot_equal(current, expected):
    columns, rows = current
    old_columns, old_rows = expected
    return (columns == old_columns and len(rows) == len(old_rows) and all(
        len(row) == len(old) and all(type(a) is type(b) and a == b for a, b in zip(row, old))
        for row, old in zip(rows, old_rows)))


# LLM: 原_redact_json_text/_content_variants算法只在Root外执行；只准备原两类UPDATE字段，不修改状态或创建另一账。
# 函数用途: 解析原JSON/原fallback并准备精确SQL参数与实际输入字节观测。
def _prepare_ledger_redaction(runtime, operations, variants, identity):
    runtime_updates, operation_updates = [], []
    content_bytes = 0
    for values in runtime[1]:
        row = dict(zip(runtime[0], values))
        parameters, parameters_changed = _redact_json_text(row["parameters_json"], variants)
        gate, gate_changed = _redact_json_text(row["runtime_gate_json"], variants)
        content_bytes += len(str(row["parameters_json"]).encode("utf-8")) + len(str(row["runtime_gate_json"]).encode("utf-8"))
        if parameters_changed or gate_changed:
            runtime_updates.append((parameters, gate, row["run_id"], row["operation_id"]))
    for values in operations[1]:
        row = dict(zip(operations[0], values))
        result, changed = _redact_json_text(row["result_json"], variants)
        content_bytes += len(str(row["result_json"]).encode("utf-8"))
        if changed:
            operation_updates.append((result, row["owner_id"], row["run_id"], row["operation_id"]))
    return _LedgerRedactionPlan(runtime, operations, tuple(runtime_updates), tuple(operation_updates), identity, content_bytes)
