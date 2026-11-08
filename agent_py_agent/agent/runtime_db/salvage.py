"""离线读取源运行库，在全新规范库中保留可验证的表与事件前缀；不执行换库。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ..io.sqlite_scope import sqlite_connection_scope
from .recovery_guard import (
    close_runtime_connection,
    guarded_runtime_connect,
    mark_runtime_connection_idle,
)
from .repository import RuntimeRepository


@contextmanager
def _source_pinned_connection(source, target):
    """ATTACH 源的门覆盖整个物理连接；关闭未确认时由 w6 保留证明，不能提前放行 EX。"""
    conn = guarded_runtime_connect(source, lambda: sqlite3.connect(target.as_uri(), uri=True))
    try:
        yield conn
    finally:
        mark_runtime_connection_idle(conn, True)
        close_runtime_connection(conn)


@dataclass(frozen=True)
class RuntimeSalvageReceipt:
    """记录逐表复制和事件截断；ok 只核核心表与目标完整性，事件缺失单列。"""

    source: str
    target: str
    tables: dict[str, dict[str, object]]
    events_copied: int
    events_last_seq: int
    events_error: str
    integrity: str
    ok: bool

    def to_dict(self) -> dict:
        """生成独立的 JSON 回执，不向调用方暴露内部表结果字典。"""
        return asdict(self)


def _quote(name: str) -> str:
    """引用从 SQLite schema 取得的标识符，不把库内名称拼成可执行片段。"""
    return '"' + name.replace('"', '""') + '"'


def _rows(conn, sql, parameters=()):
    """完整取回当前有界查询并关闭游标，避免 DETACH 时仍持有读语句。"""
    with closing(conn.execute(sql, parameters)) as cursor:
        return cursor.fetchall()


def _execute(conn, sql, parameters=()):
    """执行单条语句并关闭游标；提交和回滚由所属复制步骤负责。"""
    with closing(conn.execute(sql, parameters)) as cursor:
        return cursor.rowcount


def _tables(conn, schema):
    return [row[0] for row in _rows(conn, f"SELECT name FROM {schema}.sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'runtime_events' ORDER BY name")]


def _columns(conn, table):
    target = [row[1] for row in _rows(conn, f"PRAGMA main.table_info({_quote(table)})")]
    source = {row[1] for row in _rows(conn, f"PRAGMA src.table_info({_quote(table)})")}
    return [column for column in target if column in source]


def _count(conn, schema, table):
    return int(_rows(conn, f"SELECT count(*) FROM {schema}.{_quote(table)}")[0][0])


def _copy_table(conn, table):
    """每表独立提交；指纹由规范安装器拥有，不计入源候选行或实际复制行。"""
    result = {"source_rows": None, "copied": 0, "error": ""}
    try:
        result["source_rows"] = _count(conn, "src", table)
        columns = _columns(conn, table)
        if not columns:
            raise sqlite3.DatabaseError("源表与规范表没有共同列")
        names = ",".join(map(_quote, columns))
        verb = "INSERT OR IGNORE" if table == "metadata" else "INSERT"
        condition = " WHERE key != ?" if table == "metadata" else ""
        parameters = ("sqlite_schema_fingerprint",) if table == "metadata" else ()
        fingerprint_rows = (_rows(conn, "SELECT count(*) FROM src.metadata WHERE key=?", parameters)[0][0]
                            if table == "metadata" else 0)
        result["source_rows"] -= fingerprint_rows
        copied = _execute(conn, f"{verb} INTO main.{_quote(table)}({names}) "
                          f"SELECT {names} FROM src.{_quote(table)}{condition}", parameters)
        conn.commit()
        result["copied"] = copied
    except sqlite3.DatabaseError as error:
        conn.rollback()
        result["error"] = str(error)
    return result


def _copy_events(conn, batch_rows):
    """只按序号前进；首个错误后停止读取，也保留坏批次中已经读出的前缀。"""
    copied, last_seq, error = 0, 0, ""
    try:
        columns = _columns(conn, "runtime_events")
        if "seq" not in columns:
            raise sqlite3.DatabaseError("源事件表缺少 seq 列")
        names = ",".join(map(_quote, columns))
        placeholders = ",".join("?" for _ in columns)
        while True:
            batch = []
            try:
                with closing(conn.execute(
                    f"SELECT {names} FROM src.runtime_events WHERE seq > ? ORDER BY seq LIMIT ?",
                    (last_seq, batch_rows),
                )) as cursor:
                    for row in cursor:
                        batch.append(row)
            except sqlite3.DatabaseError as failure:
                error = str(failure)
            if batch:
                with closing(conn.executemany(f"INSERT INTO main.runtime_events({names}) VALUES({placeholders})", batch)):
                    pass
                conn.commit()
                copied += len(batch)
                last_seq = int(batch[-1][columns.index("seq")])
            if error or len(batch) < batch_rows:
                break
    except sqlite3.DatabaseError as failure:
        conn.rollback()
        error = error or str(failure)
    return copied, last_seq, error


def _restore_watermark(conn, last_seq):
    """保留目标已提交水位；源水位可读时也保留它，避免全量保留清理后的序号回退。"""
    ddl = _rows(conn, "SELECT sql FROM main.sqlite_master WHERE type='table' AND name='runtime_events'")
    if not ddl or "AUTOINCREMENT" not in ddl[0][0].upper():
        return ""
    watermark, error = last_seq, ""
    try:
        rows = (_rows(conn, "SELECT seq FROM src.sqlite_sequence WHERE name='runtime_events'")
                if _rows(conn, "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name='sqlite_sequence'") else [])
        if rows:
            if type(rows[0][0]) is not int or rows[0][0] < 0:
                raise ValueError("源事件水位不是非负整数")
            watermark = max(watermark, rows[0][0])
    except (sqlite3.DatabaseError, ValueError) as failure:
        error = str(failure)
    if not _execute(conn, "UPDATE main.sqlite_sequence SET seq=max(seq, ?) WHERE name='runtime_events'", (watermark,)):
        _execute(conn, "INSERT INTO main.sqlite_sequence(name,seq) VALUES('runtime_events',?)", (watermark,))
    conn.commit()
    return error


def salvage_runtime_db(source: Path, target: Path, *, batch_rows: int | None = None) -> RuntimeSalvageReceipt:
    """读取只读源库并创建独立目标；事件尾部可截断，任一核心表失败均禁止接受目标。"""
    source, target = Path(source).absolute(), Path(target).absolute()
    if not source.exists():
        raise FileNotFoundError(f"源数据库不存在：{source}")
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(target) + suffix)
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"目标数据库或附属文件已存在：{path}")
    if batch_rows is None:
        from ..memory_store.retention_scan_budget import RETENTION_SCAN_SLICE_ENTRIES

        batch_rows = RETENTION_SCAN_SLICE_ENTRIES
    if type(batch_rows) is not int or batch_rows <= 0:
        raise ValueError("batch_rows 必须为正整数")
    # 独占预留新文件，第二个离线进程不能在检查后把同名目标当成自己的空库。
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    # 安装借用不继承调用方的连接缓存；离线工具退出时不留下目标库句柄。
    with sqlite_connection_scope(False):
        RuntimeRepository(target)
    tables, events_copied, events_last_seq, events_error = {}, 0, 0, ""
    with _source_pinned_connection(source, target) as conn:
        attached = False
        try:
            try:
                _execute(conn, "ATTACH DATABASE ? AS src", (source.as_uri() + "?mode=ro",))
                attached = True
                source_tables = set(_tables(conn, "src"))
            except sqlite3.DatabaseError as error:
                for table in _tables(conn, "main"):
                    tables[table] = {"source_rows": None, "copied": 0, "error": str(error)}
                events_error = str(error)
            else:
                for table in _tables(conn, "main"):
                    if table in source_tables:
                        tables[table] = _copy_table(conn, table)
                events_copied, events_last_seq, events_error = _copy_events(conn, batch_rows)
                watermark_error = _restore_watermark(conn, events_last_seq)
                events_error = events_error or watermark_error
        finally:
            if conn.in_transaction:
                conn.rollback()
            if attached:
                _execute(conn, "DETACH DATABASE src")
        try:
            integrity = str(_rows(conn, "PRAGMA integrity_check")[0][0])
        except sqlite3.DatabaseError as error:
            integrity = str(error)
    ok = integrity == "ok" and bool(tables) and all(not row["error"] and row["copied"] == row["source_rows"] for row in tables.values())
    return RuntimeSalvageReceipt(str(source), str(target), tables, events_copied, events_last_seq, events_error, integrity, ok)
