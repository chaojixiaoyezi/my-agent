"""按请求号寻址的历史投影；canonical terminal 仍是完成权威。

旧 JSONL 只分批迁入。新记录写一行 SQLite 并按天追加导出，导出未完成意图
先提交，可在下一次调用重放；不在完成路径物化全部历史。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from ..common.json_io import append_private_jsonl_records, write_private_text_file_atomic
from ..common.nofollow_fs import (
    open_directory_beneath,
    open_readonly_file_beneath,
    unlink_file_beneath,
)
from ..memory_archive._storage_dates import _date_key as _day_key
from .recovery_pending_index import private_projection_connection


# LLM: 单根投影只负责历史检索与导出，不产生请求完成事实；调用方仍核canonical。
# 类用途: 用唯一请求键维护历史，分批接收旧JSONL并恢复未完成导出。
class GatewayHistoryIndex:
    # LLM: 路径只从原Gateway根和history字段派生，不读取其它owner。
    # 函数用途: 定位历史数据库、共享转换锁与导出段目录。
    def __init__(self, paths):
        self.paths = paths
        self.path = paths.root / "history_index.sqlite3"
        self.transition = paths.root / "history_transitions" / "history.transition"

    # LLM: 保留原history→投影锁序；共享私有父链/叶/侧车拒门，连接逐次关闭，数据库不是权威。
    # 函数用途: 打开一个私有、事务化的历史投影连接。
    @contextmanager
    def connection(self):
        from .io import locked_file_transition
        with locked_file_transition(self.transition), private_projection_connection(self.path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("CREATE TABLE IF NOT EXISTS history (request_id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL, origin TEXT NOT NULL, conflict INTEGER NOT NULL DEFAULT 0, export_pending INTEGER NOT NULL DEFAULT 0, export_path TEXT NOT NULL DEFAULT '', export_offset INTEGER NOT NULL DEFAULT 0, export_mode TEXT NOT NULL DEFAULT '')")
                conn.execute("CREATE INDEX IF NOT EXISTS history_export_pending ON history(export_pending,request_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS history_export_path ON history(export_path)")
                conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS migration_errors (line_number INTEGER PRIMARY KEY, context TEXT NOT NULL, reason TEXT NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS migration_fragment (singleton INTEGER PRIMARY KEY, body BLOB NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS audit_keys (request_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, thread_id TEXT NOT NULL, recorded_at REAL NOT NULL, status TEXT NOT NULL, error_code TEXT NOT NULL, channel TEXT NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS audit_owner ON audit_keys(owner_id,request_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS audit_time_seek ON audit_keys(recorded_at DESC,request_id DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS audit_error_seek ON audit_keys(error_code,recorded_at DESC,request_id DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS audit_channel_seek ON audit_keys(channel,recorded_at DESC,request_id DESC)")
                conn.execute("CREATE TABLE IF NOT EXISTS history_publish (request_id TEXT PRIMARY KEY, publish_seq INTEGER NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS history_publish_seq ON history_publish(publish_seq,request_id)")
                if self._meta(conn, "initialized") != "1":
                    self._initialize(conn)
                if not self._meta(conn, "append_seq"):
                    self._initialize_publish(conn)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    # LLM: 只取一个元数据键，默认值不代表记录已迁完。
    # 函数用途: 点读持久游标字段。
    def _meta(self, conn, key, default=""):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # LLM: 游标与同批投影在同一个SQLite事务内发表。
    # 函数用途: 更新单个迁移或导出元数据键。
    def _set_meta(self, conn, key, value):
        conn.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE meta.value != excluded.value", (key, str(value)))

    # LLM: 首次只stat旧来源，不等待全文回填；新根的小热段上限固定而非历史快照。
    # 函数用途: 固定旧来源身份并初始化字节游标和小段导出状态。
    def _initialize(self, conn):
        identity, size, stamp = self._legacy_source()
        for key, value in {"initialized": 1, "legacy_identity": identity, "legacy_limit": size,
                           "cursor": 0, "line": 0, "discard": 0,
                           "compat": int(size == 0), "compat_count": 0, "audit_complete": int(size == 0),
                           "legacy_observed_stamp": f"{size}:{stamp}"}.items():
            self._set_meta(conn, key, value)

    def _initialize_publish(self, conn):
        # 为升级前行预留序号。新追加可立即入库，旧行由既有有界维护补齐。
        upper = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM history").fetchone()[0]
        for key, value in (("append_seq", upper), ("source_generation", 0), ("publish_legacy_upper", upper),
                           ("publish_backfill_complete", int(upper == 0)), ("publish_cursor", 0),
                           ("publish_rebuilt_at", time.time())):
            self._set_meta(conn, key, value)
        # 旧版位置写入仍有效；有缺项时唤醒原回填，包括 SQLite 复用尾部 rowid 的情况。
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS history_publish_legacy_insert AFTER INSERT ON history
            WHEN NOT EXISTS (SELECT 1 FROM history_publish WHERE request_id=NEW.request_id)
            BEGIN
                UPDATE meta SET value=CASE
                    WHEN key='publish_cursor' AND
                        (SELECT value FROM meta WHERE key='publish_backfill_complete')='1' THEN NEW.rowid-1
                    ELSE MIN(CAST(value AS INTEGER),NEW.rowid-1) END
                WHERE key IN ('audit_cursor','publish_cursor');
                UPDATE meta SET value='0' WHERE key IN ('publish_backfill_complete','audit_complete');
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS history_publish_remove AFTER DELETE ON history
            BEGIN
                DELETE FROM history_publish WHERE request_id=OLD.request_id;
                UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='source_generation';
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS history_publish_key_repair BEFORE INSERT ON audit_keys
            WHEN EXISTS (
                SELECT 1 FROM audit_keys WHERE request_id=NEW.request_id
                AND (owner_id,thread_id,recorded_at,status,error_code,channel)
                    != (NEW.owner_id,NEW.thread_id,NEW.recorded_at,NEW.status,NEW.error_code,NEW.channel)
            )
            BEGIN
                UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='source_generation';
            END
        """)

    def _publish(self, conn, request_id, *, existing_rowid=None, upgrade_upper=0):
        if conn.execute("SELECT 1 FROM history_publish WHERE request_id=?", (request_id,)).fetchone():
            return
        if existing_rowid is not None and 0 < existing_rowid <= upgrade_upper:
            sequence = existing_rowid
        else:
            sequence = int(self._meta(conn, "append_seq")) + 1
            self._set_meta(conn, "append_seq", sequence)
        conn.execute("INSERT INTO history_publish(request_id,publish_seq) VALUES (?,?)", (request_id, sequence))

    def _upgrade_upper(self, conn):
        return int(self._meta(conn, "publish_legacy_upper")) if self._meta(conn, "publish_upgrade_done") != "1" else 0

    # LLM: 每次append在同一历史锁内查一键；旧回填最多128行/256KiB，新写优先于旧行。
    # 函数用途: 持久更新一个请求并重放它的有界导出，完全相同重放不写。
    def append(self, payload: dict) -> bool:
        request_id = str(payload.get("id") or payload.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("gateway history projection requires request_id")
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self.connection() as conn:
            self._flush_pending(conn)
            row = conn.execute("SELECT rowid,* FROM history WHERE request_id=?", (request_id,)).fetchone()
            if row is not None and row["origin"] == "live" and row["digest"] == digest and not row["conflict"]:
                return False
            self._migrate_batch(conn)
            row = conn.execute("SELECT rowid,* FROM history WHERE request_id=?", (request_id,)).fetchone()
            if row is not None and row["digest"] == digest and not row["conflict"]:
                return False
            export_path, offset, mode = self._export_location(conn, row, body)
            self._publish(conn, request_id, existing_rowid=row["rowid"] if row is not None else None,
                          upgrade_upper=self._upgrade_upper(conn))
            conn.execute("INSERT INTO history VALUES (?,?,?,'live',0,1,?,?,?) ON CONFLICT(request_id) DO UPDATE SET body=excluded.body,digest=excluded.digest,origin='live',conflict=0,export_pending=1,export_path=excluded.export_path,export_offset=excluded.export_offset,export_mode=excluded.export_mode",
                         (request_id, body, digest, export_path, offset, mode))
            self._audit_key(conn, request_id, payload)
            # 意图先耐久；导出失败不能让已提交canonical责任消失，也不靠异常猜是否写成。
            conn.commit()
            self._flush_pending(conn)
            return True

    # 小热段规则保持不变；之后只stat当天日段，不随历史长度读取或改写旧正文。
    # 函数用途: 在原历史锁内为本次追加固定日期和准确字节偏移。
    def _export_location(self, conn, row, body):
        size = len(body.encode("utf-8")) + 1
        if row is not None and row["export_path"] == "@history" and self._meta(conn, "compat") == "1":
            current = self.paths.history.stat().st_size if self.paths.history.exists() else 0
            if current + size <= 256 * 1024:
                return "@history", 0, "replace"
        count = int(self._meta(conn, "compat_count", "0"))
        if row is None and self._meta(conn, "compat") == "1" and count < 128:
            current = self.paths.history.stat().st_size if self.paths.history.exists() else 0
            if current + size <= 256 * 1024:
                self._set_meta(conn, "compat_count", count + 1)
                return "@history", current, "append"
        export_path = f"history_segments/{_day_key(time.time())}.jsonl"
        try:
            offset = (self.paths.root / export_path).stat().st_size
        except FileNotFoundError:
            offset = 0
        return export_path, offset, "append"

    def _append_daily_export(self, row, payload):
        """只重放原意图拥有的日段尾部；已提交的前缀缺失时保留意图并报错。"""
        parts = Path(row["export_path"]).parts
        data = (row["body"] + "\n").encode("utf-8")
        offset = row["export_offset"]
        try:
            descriptor = open_readonly_file_beneath(self.paths.root, parts)
        except FileNotFoundError:
            info, existing = None, b""
        else:
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                handle.seek(offset)
                existing = handle.read(len(data))
        if existing == data:
            return
        current_size = info.st_size if info is not None else 0
        if current_size < offset:
            raise ValueError("daily history export prefix is missing")
        if current_size != offset:
            parent = open_directory_beneath(self.paths.root, parts[:-1], require_dir_fd=True)
            try:
                descriptor = os.open(parts[-1], os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
                try:
                    current = os.fstat(descriptor)
                    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                            != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
                        raise ValueError("daily history export changed during replay")
                    os.ftruncate(descriptor, offset)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent)
        append_private_jsonl_records(self.paths.root / row["export_path"], [payload], sort_keys=True)

    # LLM: 单次最多重放一个意图；它成功前不会接纳下一个导出，避免未决队列失控。
    # 函数用途: 根据已耐久的准确正文恢复一次私有JSONL导出。
    def _flush_pending(self, conn):
        row = conn.execute("SELECT * FROM history WHERE export_pending=1 ORDER BY request_id LIMIT 1").fetchone()
        if row is None:
            return
        payload = json.loads(row["body"])
        if row["export_path"] != "@history":
            target = self.paths.root / row["export_path"]
            if row["export_mode"] == "append":
                self._append_daily_export(row, payload)
            else:
                write_private_text_file_atomic(target, row["body"] + "\n")
        elif row["export_mode"] == "append":
            data = (row["body"] + "\n").encode("utf-8")
            try:
                with self.paths.history.open("rb") as handle:
                    handle.seek(row["export_offset"])
                    existing = handle.read(len(data))
            except FileNotFoundError:
                existing = b""
            if existing != data:
                current_size = self.paths.history.stat().st_size if self.paths.history.exists() else 0
                if existing and data.startswith(existing) and current_size == row["export_offset"] + len(existing):
                    # 原意图只拥有它自己的准确尾部前缀；不截断别的记录或任意损坏历史。
                    descriptor = os.open(self.paths.history, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        os.ftruncate(descriptor, row["export_offset"])
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    current_size = row["export_offset"]
                if current_size != row["export_offset"]:
                    target = self.paths.root / "history_segments" / (hashlib.sha256(row["request_id"].encode()).hexdigest() + ".jsonl")
                    write_private_text_file_atomic(target, row["body"] + "\n")
                    self._set_meta(conn, "compat", 0)
                    conn.execute("UPDATE history SET export_path=?, export_pending=0 WHERE request_id=? AND digest=?", (str(target.relative_to(self.paths.root)), row["request_id"], row["digest"]))
                    conn.commit()
                    return
                append_private_jsonl_records(self.paths.history, [payload], sort_keys=True)
        else:
            with self.paths.history.open("rb") as handle:
                raw = handle.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("history export segment exceeds bounded repair")
            retained = []
            for line in raw.decode("utf-8").split("\n"):
                if not line:
                    continue
                item = json.loads(line)
                item_id = str(item.get("id") or item.get("request_id") or "").strip()
                if item_id != row["request_id"]:
                    retained.append(line)
            retained.append(row["body"])
            write_private_text_file_atomic(self.paths.history, "\n".join(retained) + "\n")
        if row["export_path"] == "@history":
            identity, size, stamp = self._legacy_source()
            # 自己完整发表的小段已在唯一键表中；别让下一次no-op重放再迁自己。
            self._set_meta(conn, "legacy_identity", identity)
            self._set_meta(conn, "legacy_limit", size)
            self._set_meta(conn, "legacy_observed_stamp", f"{size}:{stamp}")
            self._set_meta(conn, "cursor", size)
        conn.execute("UPDATE history SET export_pending=0 WHERE request_id=? AND digest=?", (row["request_id"], row["digest"]))
        conn.commit()

    # LLM: 固定预算持久化游标，重开对象/进程仍继续；只迁移原文件，不阻塞新请求。
    # 函数用途: 对旧JSONL执行一次可恢复的有界迁移。
    def migrate_batch(self) -> dict:
        with self.connection() as conn:
            self._flush_pending(conn)
            return self._migrate_batch(conn)

    # LLM: 每次来源读取最多128行/256KiB；跨拍大行片段与游标同事务保存，到完整行才解析，不凭预算判坏。
    # 函数用途: 推进旧文件字节游标并处理源换代，不覆盖新写者正文。
    def _legacy_source(self):
        try:
            info = self.paths.history.lstat()
        except FileNotFoundError:
            return "", 0, 0
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("legacy history must be a regular file")
        return f"{info.st_dev}:{info.st_ino}", info.st_size, info.st_mtime_ns

    def _legacy_source_changed(self, conn, *, identity, size, stamp, offset, previous):
        observed = self._meta(conn, "legacy_observed_stamp")
        return (identity != previous or size < offset
                or observed.startswith(f"{size}:") and observed != f"{size}:{stamp}")

    def _migrate_batch(self, conn):
        offset = int(self._meta(conn, "cursor", "0"))
        limit = int(self._meta(conn, "legacy_limit", "0"))
        line_number = int(self._meta(conn, "line", "0"))
        used, lines = 0, 0
        identity, size, stamp = self._legacy_source()
        previous = self._meta(conn, "legacy_identity")
        if self._legacy_source_changed(conn, identity=identity, size=size, stamp=stamp, offset=offset, previous=previous):
            # 自己的正常导出会一并发表身份。外部轮替及截断撤销旧查询窗口。
            offset, line_number = 0, 0
            conn.execute("DELETE FROM migration_fragment")
            self._set_meta(conn, "legacy_identity", identity)
            self._set_meta(conn, "source_generation", int(self._meta(conn, "source_generation")) + 1)
        if size != limit:
            self._set_meta(conn, "compat", 0)
        limit = size
        self._set_meta(conn, "legacy_limit", limit)
        self._set_meta(conn, "legacy_observed_stamp", f"{size}:{stamp}")
        if offset < limit:
            with self.paths.history.open("rb") as handle:
                handle.seek(offset)
                while offset < limit and used < 256 * 1024 and lines < 128:
                    start = offset
                    raw = handle.readline(min(256 * 1024 - used, limit - offset))
                    if not raw:
                        break
                    used += len(raw)
                    complete = raw.endswith(b"\n") or start + len(raw) == limit
                    partial = conn.execute("SELECT body FROM migration_fragment WHERE singleton=1").fetchone()
                    if not complete and partial is None and lines:
                        break
                    offset += len(raw)
                    if not complete:
                        conn.execute("INSERT INTO migration_fragment VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET body=CAST(body || excluded.body AS BLOB)", (raw,))
                        continue
                    if partial is not None:
                        raw = bytes(partial[0]) + raw
                        conn.execute("DELETE FROM migration_fragment WHERE singleton=1")
                    line_number += 1
                    lines += 1
                    try:
                        payload = json.loads(raw.decode("utf-8")) if raw.strip() else None
                        if payload is not None and not isinstance(payload, dict):
                            raise ValueError("history row must be an object")
                    except (UnicodeDecodeError, ValueError) as error:
                        self._migration_error(conn, line_number, type(error).__name__)
                        continue
                    if isinstance(payload, dict):
                        self._migrate_row(conn, payload)
        for key, value in (("cursor", offset), ("line", line_number)):
            self._set_meta(conn, key, value)
        return {"cursor": offset, "bytes_read": used, "lines_read": lines, "complete": offset >= limit}

    # LLM: 旧重复行只标记需修复；live行在迁移与并发append交错中始终优先。
    # 函数用途: 将一条旧记录迁入唯一键投影。
    def _migrate_row(self, conn, payload):
        request_id = str(payload.get("id") or payload.get("request_id") or "").strip()
        if not request_id:
            return
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        row = conn.execute("SELECT rowid,origin FROM history WHERE request_id=?", (request_id,)).fetchone()
        if row is not None and row["origin"] == "live":
            return
        self._publish(conn, request_id, existing_rowid=row["rowid"] if row is not None else None,
                      upgrade_upper=self._upgrade_upper(conn))
        conn.execute("INSERT INTO history(request_id,body,digest,origin) VALUES (?,?,?,'legacy') ON CONFLICT(request_id) DO UPDATE SET body=excluded.body,digest=excluded.digest,conflict=1 WHERE history.origin='legacy'", (request_id, body, digest))
        self._audit_key(conn, request_id, payload)

    def _audit_key(self, conn, request_id, payload):
        """白名单键与原历史同事务更新；不形成第二终态权威。"""
        from .request_audit_records import _outcome_entry, _record_owner
        entry = _outcome_entry(payload, 0, _record_owner(payload, {}))
        terminal = payload.get("terminal_response") if isinstance(payload.get("terminal_response"), dict) else {}
        stamp = terminal.get("ended_at", payload.get("ended_at", payload.get("created_at", 0)))
        try:
            stamp = stamp if type(stamp) in (int, float) and math.isfinite(stamp) else 0
        except OverflowError:
            stamp = 0
        claim = payload.get("conversation_claim") if isinstance(payload.get("conversation_claim"), dict) else {}
        conn.execute("INSERT OR REPLACE INTO audit_keys VALUES (?,?,?,?,?,?,?)",
                     (request_id, _record_owner(payload, {}), str(claim.get("thread_id") or payload.get("thread_id") or ""),
                      stamp, entry["status"], entry["error_code"], entry["channel"]))

    def backfill_audit_step(self, *, limit=1):
        """原维护写门按持久 rowid 前进；前台只读绝不补索引。"""
        with self.connection() as conn:
            return self._backfill_audit_step(conn, limit=limit)

    def _backfill_audit_step(self, conn, *, limit):
        # 旧版可自行推进 audit_cursor，发布序号覆盖需要独立记账。
        after = min(int(self._meta(conn, key, "0")) for key in ("audit_cursor", "publish_cursor"))
        upgrade_upper = self._upgrade_upper(conn)
        used, visited, complete = 0, 0, False
        deadline = time.monotonic() + .05
        while visited < min(128, max(1, limit)) and used < 1024 * 1024 and time.monotonic() < deadline:
            row = conn.execute("SELECT rowid,request_id,substr(body,1,262145) AS body FROM history WHERE rowid>? ORDER BY rowid LIMIT 1",
                               (after,)).fetchone()
            if row is None:
                complete = True
                break
            visited += 1
            used += len(row["body"].encode())
            self._publish(conn, row["request_id"], existing_rowid=row["rowid"], upgrade_upper=upgrade_upper)
            try:
                if len(row["body"]) >= 262145:
                    raise ValueError("audit_projection_source_exceeds_slice")
                self._audit_key(conn, row["request_id"], json.loads(row["body"]))
            except (ValueError, TypeError, AttributeError, OverflowError):
                self._set_meta(conn, "audit_error_rowid", row["rowid"])
                self._set_meta(conn, "audit_error_code", "audit_projection_row_invalid_or_oversized")
                self._set_meta(conn, "audit_invalid", 1)
            after = row["rowid"]
        self._set_meta(conn, "audit_cursor", after)
        self._set_meta(conn, "publish_cursor", after)
        self._set_meta(conn, "audit_complete", int(complete))
        self._set_meta(conn, "publish_backfill_complete", int(complete))
        if complete:
            self._set_meta(conn, "publish_upgrade_done", 1)
        return {"items": visited, "bytes": used, "complete": complete, "cursor": after}

    def maintenance_step(self, *, cutoff):
        """原lane的一次写门：先补缺失键，再沿原retention游标执行。"""
        with self.connection() as conn:
            if self._meta(conn, "audit_complete") != "1" or self._meta(conn, "publish_backfill_complete") != "1":
                report = self._backfill_audit_step(conn, limit=1)
                return {**report, "complete": False}
            if cutoff is None:
                return {"items": 0, "bytes": 0, "complete": True, "cursor": 0}
            return self._prune_history_step(conn, cutoff=cutoff, max_rows=1)

    def audit_page(self, query, *, window_only=False):
        """有界 keyset 只读；游标绑定来源身份、过滤和投影代次。"""
        from .request_audit_records import _index_audit_page
        return _index_audit_page(self, query, window_only=window_only)

    # LLM: 错误保留位置和类别，不保留可能敏感的坏正文。
    # 函数用途: 为一条坏旧行登记可诊断迁移错误。
    def _migration_error(self, conn, line_number, reason):
        conn.execute("INSERT OR REPLACE INTO migration_errors VALUES (?,'gateway.index.history.read',?)", (line_number, reason))

    # LLM: 诊断读取不推进迁移，不将投影提升为终态权威。
    # 函数用途: 点读一个历史摘要。
    def get(self, request_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT body FROM history WHERE request_id=?", (request_id,)).fetchone()
            return json.loads(row[0]) if row else None

    # LLM: keyset只返回固定批次，不用OFFSET也不物化全历史。
    # 函数用途: 为离线重建按请求号读取有界页。
    def page(self, *, after: str, limit: int = 128) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT body FROM history WHERE request_id>? ORDER BY request_id LIMIT ?", (after, min(128, max(1, limit)))).fetchall()
            return [json.loads(row[0]) for row in rows]

    # LLM: 离线诊断按固定批次取错误，前台append不读全表。
    # 函数用途: 返回迁移错误的一页，保留来源路径与行号。
    def migration_errors(self, *, after: int = 0) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM migration_errors WHERE line_number>? ORDER BY line_number LIMIT 128", (after,)).fetchall()
            return [{"context": row["context"], "line_number": row["line_number"], "message": row["reason"], "path": str(self.paths.history)} for row in rows]

    def prune_history_step(self, *, cutoff: float, max_rows: int) -> dict:
        """按持久rowid游标检查一小批；删除与游标共事务，正文未知或导出未决均保留。"""
        with self.connection() as conn:
            return self._prune_history_step(conn, cutoff=cutoff, max_rows=max_rows)

    def _prune_history_step(self, conn, *, cutoff, max_rows):
        cursor = int(self._meta(conn, "retention_cursor", "0"))
        rows = conn.execute("SELECT rowid,* FROM history WHERE rowid>? ORDER BY rowid LIMIT ?",
                            (cursor, max(0, max_rows))).fetchall()
        paths, removed, size = set(), [], 0
        for row in rows:
            size += len(row["body"].encode("utf-8"))
            if row["export_pending"] or not _expired_history_body(row["body"], cutoff):
                continue
            removed.append((row["rowid"],))
            paths.add(row["export_path"])
        for rowid, in removed:
            conn.execute("DELETE FROM audit_keys WHERE request_id=(SELECT request_id FROM history WHERE rowid=?)", (rowid,))
        conn.executemany("DELETE FROM history WHERE rowid=?", removed)
        today = _day_key(time.time())
        for export_path in paths:
            parts = _retention_export_parts(export_path, today)
            if parts is not None and conn.execute(
                    "SELECT 1 FROM history WHERE export_path=? LIMIT 1", (export_path,)).fetchone() is None:
                # 共用历史锁防止并发append；安全unlink拒绝目录链接，也不触碰JSON锁侧车。
                unlink_file_beneath(self.paths.root, parts)
        if rows:
            cursor = rows[-1]["rowid"]
        complete = conn.execute("SELECT 1 FROM history WHERE rowid>? LIMIT 1", (cursor,)).fetchone() is None
        if complete:
            cursor = 0
        self._set_meta(conn, "retention_cursor", cursor)
        return {"cursor": cursor, "complete": complete, "items": len(rows), "bytes": size}


def _expired_history_body(body: str, cutoff: float) -> bool:
    """历史writer传入response：只认精确终态与有限正数ended_at，不猜别名或创建时间。"""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(payload, dict) or payload.get("status") not in ("done", "failed", "cancelled", "interrupted"):
        return False
    ended = payload.get("ended_at")
    try:
        return type(ended) in (int, float) and math.isfinite(ended) and 0 < ended < cutoff
    except OverflowError:
        return False


def _retention_export_parts(export_path: str, today: str) -> tuple[str, ...] | None:
    """删除范围仅限已知日段及旧请求hash叶；兼容热段和未知路径均保留。"""
    parts = Path(export_path).parts
    if len(parts) != 2 or parts[0] != "history_segments" or export_path != "/".join(parts):
        return None
    name = parts[1]
    if not name.endswith(".jsonl"):
        return None
    stem = name[:-6]
    if len(stem) == 64 and all(char in "0123456789abcdef" for char in stem):
        return parts
    try:
        day = date.fromisoformat(stem).isoformat()
    except ValueError:
        return None
    return parts if day == stem and day != today else None


def maintain_gateway_history_lane(request) -> dict:
    """复用owner路径与原写门，在条数及毫秒预算内逐条推进唯一数据库游标。"""
    from ..common.json_io import read_json_object_report
    from ..memory_store.retention_models import MemoryRetentionPolicy
    from ..user_space.owner_maintenance import _real_owner_directory
    from ..user_space.runtime_paths import runtime_paths_for_agent
    from ..user_space.storage_lanes import (
        OWNER_RETENTION_OVERRIDE_MAX_DAYS,
        STORAGE_LANE_SLICE_BYTES,
        STORAGE_LANE_SLICE_ENTRIES,
        STORAGE_LANE_SLICE_MS,
    )
    from .paths import gateway_paths_from_root

    receipt = {"cursor": dict(request.cursor), "complete": True, "items": 0, "bytes": 0}
    if request.legal_hold:
        return {**receipt, "deferred_reason": "legal_hold"}
    root = runtime_paths_for_agent(None, request.home.owner_home_dir, request.home)["gateway_workspace"]
    if not _real_owner_directory(request.home, root):
        return receipt
    index = GatewayHistoryIndex(gateway_paths_from_root(root))
    if not index.path.exists():
        return receipt
    # 与owner_retention_override读取同一策略；audit_days的0关闭语义不能被正整数覆盖器吞掉。
    policy = read_json_object_report(Path(request.home.owner_retention_json), context="storage_lanes.override")
    days = MemoryRetentionPolicy().audit_days
    override = policy.payload.get("audit_days")
    if policy.load_error is None and type(override) is int and override <= OWNER_RETENTION_OVERRIDE_MAX_DAYS:
        days = override
    budget = request.budget.capped(items=STORAGE_LANE_SLICE_ENTRIES, size=STORAGE_LANE_SLICE_BYTES,
                                   ms=STORAGE_LANE_SLICE_MS)
    receipt["complete"] = False
    while (not request.budget.exhausted(receipt["items"], receipt["bytes"])
           and not budget.exhausted(receipt["items"], receipt["bytes"])):
        report = request.run_write(lambda: index.maintenance_step(cutoff=request.now - days * 86400 if days > 0 else None))
        receipt["cursor"] = {"retention_cursor": report["cursor"]}
        receipt["items"] += report["items"]
        receipt["bytes"] += report["bytes"]
        if report["complete"]:
            receipt["complete"] = True
            break
    return receipt
