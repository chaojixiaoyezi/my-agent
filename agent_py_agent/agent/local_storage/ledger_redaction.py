from __future__ import annotations

"""Exact plaintext redaction for structured tool ledgers.

Operation identity, status, hashes, and idempotency keys remain intact.  Only
caller-supplied plaintext is removed from JSON payload columns for one tool.
"""

import json
import sqlite3
from collections.abc import Collection
from typing import Any

_DELETED_CONTENT = "[deleted-content]"


class LocalStoreLedgerRedactionMixin:
    # LLM: 删除事实不能删掉幂等身份，也不能靠自然语言猜测；只按调用方给出的精确正文改写 JSON 载荷。
    # 函数用途: 擦除某工具审计/幂等账本中的指定正文，同时保留操作状态和重放身份。
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
    # 函数用途: 尽力截断本次精确改写前的 WAL 页面，减少已删正文的磁盘残留。
    def _truncate_redacted_wal(self) -> None:
        try:
            with self._connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass


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
