"""保全原库并抢救可验证的权威；代次 EX 内按持久阶段校验、发布或继续上次恢复。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ..common import json_io
from ..common.nofollow_fs import read_bytes_beneath
from ..contracts.optimization_contracts import StoreSchemaNewerError
from ..io import sqlite_probe
from ..io.sqlite_connection import sqlite_error_details
from ..io.sqlite_scope import sqlite_connection_scope
from .recovery_guard import (
    RuntimeDatabaseDeferred,
    close_runtime_connection,
    guarded_runtime_connect,
    mark_runtime_connection_idle,
    retire_idle_runtime_connections,
    runtime_db_generation_guard,
    runtime_db_recovery_pending,
)
from .repository import RuntimeRepository
from .schema import SALVAGE_REQUEST_SUFFIX, require_runtime_schema_generation, runtime_db_identity


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


_RECOVERY_SUFFIXES = ("", "-wal", "-shm", "-journal")


class _RecoveryStopped(Exception):
    """让 EX 的异常退出保留恢复意图；结构化结果在释放门后返回。"""
    def __init__(self, report):
        self.report = report


def _marker(db):
    return Path(f"{db}{SALVAGE_REQUEST_SUFFIX}")


def _marker_lock(db):
    return Path(f"{db}.recovery-state")


def _load_state(db):
    try:
        raw = read_bytes_beneath(db.parent, (_marker(db).name,))
    except FileNotFoundError:
        return {}
    if raw is None:
        return {}
    state = json.loads(raw) if raw.strip() else {}  # 接受参考设计的旧空请求标记。
    if not isinstance(state, dict) or state.get("schema", "runtime_db_salvage.v1") != "runtime_db_salvage.v1":
        raise ValueError("runtime_db_salvage_marker_invalid")
    return state


def _save_state(db, state, phase):
    state.update(schema="runtime_db_salvage.v1", phase=phase)
    json_io.write_private_json_file_atomic_unlocked(_marker(db), state)


def _artifact(db, state, key):
    name = state[key]
    prefix = db.name + (".damaged-" if key == "backup" else ".salvage-")
    if not isinstance(name, str) or Path(name).name != name or not name.startswith(prefix):
        raise ValueError("runtime_db_salvage_artifact_invalid")
    return db.with_name(name)


@contextmanager
def _recovery_connection(db, *, read_only=True):
    conn = guarded_runtime_connect(db, lambda: sqlite3.connect(
        db.as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True, timeout=0))
    try:
        yield conn
    finally:
        mark_runtime_connection_idle(conn, True)
        close_runtime_connection(conn)


def _verify_source(db):
    """关闭后重新读取真实 WAL 视图，不复用检测者的快照或英文异常消息。"""
    try:
        with _recovery_connection(db) as conn:
            verdict = _rows(conn, "PRAGMA quick_check(1)")
            if verdict == [("ok",)]:
                return True
    except sqlite3.Error as error:
        if sqlite_error_details(error).get("sqlite_primary_code") not in (11, 26):
            raise
    return False


def _source_facts(db):
    with _recovery_connection(db) as conn:
        require_runtime_schema_generation(conn)
        generation = _rows(conn, "SELECT value FROM metadata WHERE key='schema_generation'")
        watermark = _rows(conn, "SELECT seq FROM sqlite_sequence WHERE name='runtime_events'")
        if len(watermark) != 1 or type(watermark[0][0]) is not int or watermark[0][0] < 0:
            raise ValueError("runtime_db_event_watermark_unverified")
        return {"generation": generation[0][0] if generation else None, "watermark": watermark[0][0]}


def _source_signature(db):
    signature = {}
    for suffix in ("", "-wal", "-journal"):
        try:
            info = os.lstat(f"{db}{suffix}")
            signature[suffix] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        except FileNotFoundError:
            signature[suffix] = None
    return signature


def _copy_backup(db, backup):
    """固定四文件、流式复制并 fsync；不 checkpoint 源，不改动原 WAL 字节。"""
    for suffix in _RECOVERY_SUFFIXES:
        source, target = Path(f"{db}{suffix}"), Path(f"{backup}{suffix}")
        try:
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            if not suffix:
                raise
            continue
        temporary = target.with_name(target.name + ".tmp")
        try:
            with os.fdopen(descriptor, "rb") as reader:
                writer_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(writer_fd, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
            os.replace(temporary, target)
            json_io._sync_parent_directory(target)
        finally:
            temporary.unlink(missing_ok=True)


def _file_digest(db):
    descriptor = os.open(db, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _validate_candidate(candidate, receipt, facts):
    if not receipt["ok"]:
        raise ValueError("runtime_db_core_copy_unverified")
    sqlite_probe.require_plain_sqlite_files(candidate)
    with _recovery_connection(candidate, read_only=False) as conn:
        require_runtime_schema_generation(conn)
        generation = _rows(conn, "SELECT value FROM metadata WHERE key='schema_generation'")
        if (generation[0][0] if generation else None) != facts["generation"]:
            raise ValueError("runtime_db_candidate_generation_changed")
        if set(_tables(conn, "main")) != set(receipt["tables"]):
            raise ValueError("runtime_db_core_table_missing")
        for table, counts in receipt["tables"].items():
            expected = counts["source_rows"]
            if counts["error"] or expected is None or counts["copied"] != expected:
                raise ValueError("runtime_db_core_count_unverified")
            if _count(conn, "main", table) != expected + (table == "metadata"):
                raise ValueError("runtime_db_candidate_count_changed")
        if _rows(conn, "PRAGMA foreign_key_check") or _rows(conn, "PRAGMA integrity_check") != [("ok",)]:
            raise ValueError("runtime_db_candidate_integrity_unverified")
        events, last = _rows(conn, "SELECT count(*),coalesce(max(seq),0) FROM runtime_events")[0]
        watermark = _rows(conn, "SELECT seq FROM sqlite_sequence WHERE name='runtime_events'")
        if (events != receipt["events_copied"] or last != receipt["events_last_seq"]
                or not watermark or watermark[0][0] < facts["watermark"]):
            raise ValueError("runtime_db_candidate_watermark_unverified")
        busy, frames, merged = _rows(conn, "PRAGMA wal_checkpoint(TRUNCATE)")[0]
        if busy or frames != merged:
            raise RuntimeDatabaseDeferred("candidate_wal_busy")
    # SQLite 连接全关后再核 sidecar；最终发布的主文件必须独立可用。
    wal = Path(f"{candidate}-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeDatabaseDeferred("candidate_wal_not_merged")
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    json_io._sync_parent_directory(candidate)


def _complete_recovery(db, state, status, *, preserve_evidence=False):
    report = {**state, "status": status, "path": str(db), "published_identity": runtime_db_identity(db)}
    json_io.write_private_json_file_atomic_unlocked(Path(f"{db}.recovery-result.json"), report)
    if status == "healthy" and not preserve_evidence:
        # 瞬态故障清除时只移除本次 journal 明确列出的产物，不扫描历史归档。
        for key in ("candidate", "backup"):
            if key in state:
                artifact = _artifact(db, state, key)
                for suffix in _RECOVERY_SUFFIXES:
                    Path(f"{artifact}{suffix}").unlink(missing_ok=True)
    _marker(db).unlink(missing_ok=True)
    json_io._sync_parent_directory(db)
    return report


def _publish_candidate(db, state):
    candidate = _artifact(db, state, "candidate")
    current = runtime_db_identity(db)
    candidate_identity = tuple(state["candidate_identity"])
    if current == candidate_identity:
        # replace 已完成但阶段写/进程退出落在之后，绝不把旧候选覆盖回来。
        return _complete_recovery(db, state, "recovered")
    if current != tuple(state["identity"]):
        return _complete_recovery(db, state, "superseded")
    sqlite_probe.require_plain_sqlite_files(candidate)
    if runtime_db_identity(candidate) != candidate_identity or _file_digest(candidate) != state["candidate_digest"]:
        raise ValueError("runtime_db_candidate_identity_changed")
    # 候选门也须 EX：失败关闭的候选句柄不能在主文件换名后继续执行旧 WAL。
    with runtime_db_generation_guard(candidate, exclusive=True):
        with sqlite_probe.sqlite_connections_locked_out(db):
            if runtime_db_identity(db) != current:
                raise RuntimeDatabaseDeferred("source_identity_changed")
            signature = _source_signature(db)
            expected = state["source_signature"]
            if (signature[""] != expected[""] or any(
                    signature[suffix] is not None and signature[suffix] != expected[suffix]
                    for suffix in ("-wal", "-journal"))):
                raise RuntimeDatabaseDeferred("source_contents_changed")
            # 此阶段一旦持久化，崩溃重入只能完成候选发布，不能再读删掉 WAL 的旧库作健康判断。
            _save_state(db, state, "publishing")
            for suffix in _RECOVERY_SUFFIXES[1:]:
                Path(f"{db}{suffix}").unlink(missing_ok=True)
            os.replace(candidate, db)
            json_io._sync_parent_directory(db)
    _save_state(db, state, "published")
    return _complete_recovery(db, state, "recovered")


def _recover_locked(db, state, expected_identity, batch_rows):
    current = runtime_db_identity(db)
    if state.get("phase") in {"publishing", "published"}:
        return _publish_candidate(db, state)
    if "candidate" in state:
        # 上次候选关闭失败时仍持有它的 SH；先让创建者退役并证明排空，不能另建候选遗忘旧句柄。
        with runtime_db_generation_guard(_artifact(db, state, "candidate"), exclusive=True):
            pass
    if current is None:
        raise ValueError("runtime_db_source_missing")
    if current != expected_identity:
        return _complete_recovery(db, state, "superseded")
    sqlite_probe.require_plain_sqlite_files(db)
    # 排除非托管 SQLite 执行体；此后校验连接全关，发表时还会重新取此锁。
    with sqlite_probe.sqlite_connections_locked_out(db):
        pass
    if _verify_source(db):
        return _complete_recovery(db, state, "healthy")
    if tuple(state.get("identity", ())) != current:
        state.clear()
        state["identity"] = list(current)
    if state.get("phase") == "requires_recovery":
        raise _RecoveryStopped({**state, "status": "requires_recovery", "path": str(db)})
    if state.get("phase") != "candidate_ready":
        token = uuid.uuid4().hex
        state.update(backup=db.name + ".damaged-" + token,
                     candidate=db.name + ".salvage-" + token, source_signature=_source_signature(db))
        _save_state(db, state, "backing_up")
        _copy_backup(db, _artifact(db, state, "backup"))
        _save_state(db, state, "backed_up")
        _save_state(db, state, "building_candidate")
        receipt = salvage_runtime_db(db, _artifact(db, state, "candidate"), batch_rows=batch_rows)
        state["receipt"] = receipt.to_dict()
        state["facts"] = _source_facts(db)
        state["events_loss"] = bool(receipt.events_error or receipt.events_last_seq < state["facts"]["watermark"])
    candidate = _artifact(db, state, "candidate")
    if state.get("phase") == "candidate_ready" and (
            runtime_db_identity(candidate) != tuple(state["candidate_identity"])
            or _file_digest(candidate) != state["candidate_digest"]):
        raise ValueError("runtime_db_candidate_identity_changed")
    _validate_candidate(candidate, state["receipt"], state["facts"])
    state.update(candidate_identity=list(runtime_db_identity(candidate)), candidate_digest=_file_digest(candidate))
    _save_state(db, state, "candidate_ready")
    # 最后一个校验连接关闭后再独立复核：瞬态 WAL 不一致清除时不作无谓换库。
    if _verify_source(db):
        return _complete_recovery(db, state, "healthy")
    if _source_signature(db) != state["source_signature"]:
        raise RuntimeDatabaseDeferred("source_contents_changed")
    return _publish_candidate(db, state)


def _recovery_failure(db, state, error):
    details = sqlite_error_details(error)
    report = {**state, "path": str(db), "status": "deferred", "reason": getattr(error, "reason", type(error).__name__),
              "error": details or {"exception_type": type(error).__name__, "errno": getattr(error, "errno", None)}}
    if isinstance(error, (ValueError, KeyError, TypeError, StoreSchemaNewerError)) or details.get("sqlite_primary_code") in (11, 26):
        report.update(status="requires_recovery", reason=str(error))
    return report


def recover_runtime_database(path: Path, *, expected_identity: tuple[int, int], batch_rows: int | None = None) -> dict[str, object]:
    """非阻塞 EX 下恢复下次准入；一次失败只返回事实，不重放原请求/工具正文。"""
    db = Path(path).absolute()
    state = {}
    try:
        state = _load_state(db)
        current = runtime_db_identity(db)
        if state.get("phase") == "requires_recovery" and tuple(state.get("identity", ())) == current:
            if "candidate" in state:
                retire_idle_runtime_connections(_artifact(db, state, "candidate"))
            return {**state, "status": "requires_recovery", "path": str(db)}
        if not state and current is not None and current != expected_identity:
            return {"status": "already_recovered", "path": str(db), "published_identity": current}
        with runtime_db_generation_guard(db, exclusive=True):
            with json_io.locked_json_path(_marker_lock(db), blocking=False):
                state = _load_state(db)
                try:
                    return _recover_locked(db, state, expected_identity, batch_rows)
                except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, StoreSchemaNewerError) as error:
                    report = _recovery_failure(db, state, error)
                    if report["status"] == "requires_recovery":
                        # 持 EX 和 marker 锁记录失败源，不能在解锁后覆写另一恢复者的阶段。
                        _save_state(db, report, "requires_recovery")
                    raise _RecoveryStopped(report) from error
    except _RecoveryStopped as stopped:
        return stopped.report
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, StoreSchemaNewerError) as error:
        return _recovery_failure(db, state, error)


def heal_runtime_db_at_startup(db_path: Path) -> RuntimeSalvageReceipt | None:
    """无标记只有一次 exists；有标记才复核并保全/抢救，失败保留源与候选。"""
    db = Path(db_path).absolute()
    if not os.path.lexists(_marker(db)) and not runtime_db_recovery_pending(db):
        return None
    result = recover_runtime_database(db, expected_identity=runtime_db_identity(db))
    if result["status"] == "recovered":
        return RuntimeSalvageReceipt(**result["receipt"])
    if result["status"] in {"healthy", "already_recovered", "superseded"}:
        return None
    error = RuntimeDatabaseDeferred(str(result.get("reason", result["status"])))
    error.runtime_db_recovery = result
    raise error
