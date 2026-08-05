from __future__ import annotations

"""Durable idempotency index for prepare-learned source record keys.

Adapters may fetch overlapping time windows, pages or cursor ranges.  Their
``record_keys`` identify source delivery positions, not record contents or
business/device ids.  This index makes those positions durable per watch so a
repeated source page cannot create a second delivery while two distinct source
positions remain distinct even when their contents are byte-for-byte equal.
The harvester transaction remains the commit authority: this module is queried
before spool append and written only after the matching cursor/spool snapshot
has committed.
"""

import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from .watch_state import WatchState, state_dir

_SCHEMA_VERSION = "audit-source-record-index.v1"
_QUERY_CHUNK = 400


class SourceRecordIndexError(OSError):
    """The record-key ledger cannot currently prove idempotency."""


def unseen_source_record_keys(
    state: WatchState,
    keys: Sequence[str],
) -> list[bool]:
    """Return a positional mask, suppressing committed and same-batch keys."""

    normalized = _normalized_keys(keys)
    if not normalized:
        return []
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(state)
        _ensure_schema(connection)
        connection.commit()
        committed = _committed_keys(connection, normalized)
    except (OSError, sqlite3.Error) as exc:
        raise SourceRecordIndexError("来源记录去重索引当前不可用") from exc
    finally:
        if connection is not None:
            connection.close()
    accepted: set[str] = set()
    mask: list[bool] = []
    for key in normalized:
        unseen = key not in committed and key not in accepted
        mask.append(unseen)
        if unseen:
            accepted.add(key)
    return mask


def commit_source_record_keys(
    state: WatchState,
    keys: Iterable[str],
    *,
    transaction_id: str,
) -> int:
    """Idempotently commit keys for one already-committed harvest transaction."""

    normalized = list(dict.fromkeys(_normalized_keys(list(keys))))
    if not normalized:
        return 0
    selected_transaction = str(transaction_id or "").strip()
    if not selected_transaction:
        raise SourceRecordIndexError("来源记录去重提交缺少 transaction_id")
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(state)
        _ensure_schema(connection)
        before = connection.total_changes
        connection.executemany(
            "INSERT OR IGNORE INTO committed_record_keys"
            " (record_key, transaction_id, committed_at) VALUES (?, ?, ?)",
            (
                (key, selected_transaction, time.time())
                for key in normalized
            ),
        )
        connection.commit()
        return max(0, connection.total_changes - before)
    except (OSError, sqlite3.Error) as exc:
        raise SourceRecordIndexError("来源记录去重索引提交失败") from exc
    finally:
        if connection is not None:
            connection.close()


def source_record_index_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.record-keys.sqlite3"


def _connect(state: WatchState) -> sqlite3.Connection:
    path = source_record_index_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS metadata"
        " (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS committed_record_keys ("
        " record_key TEXT PRIMARY KEY,"
        " transaction_id TEXT NOT NULL,"
        " committed_at REAL NOT NULL"
        ") WITHOUT ROWID"
    )
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )
    elif str(row[0] or "") != _SCHEMA_VERSION:
        raise SourceRecordIndexError("来源记录去重索引版本不兼容")


def _committed_keys(
    connection: sqlite3.Connection,
    keys: Sequence[str],
) -> set[str]:
    committed: set[str] = set()
    unique = list(dict.fromkeys(keys))
    for offset in range(0, len(unique), _QUERY_CHUNK):
        chunk = unique[offset : offset + _QUERY_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT record_key FROM committed_record_keys "  # noqa: S608
            f"WHERE record_key IN ({placeholders})",
            chunk,
        )
        committed.update(str(row[0]) for row in rows)
    return committed


def _normalized_keys(keys: Sequence[str]) -> list[str]:
    normalized = [str(key or "").strip() for key in keys]
    if any(not key or len(key) > 640 for key in normalized):
        raise SourceRecordIndexError("来源记录唯一键为空或超过 640 字符")
    return normalized


__all__ = [
    "SourceRecordIndexError",
    "commit_source_record_keys",
    "source_record_index_path",
    "unseen_source_record_keys",
]
