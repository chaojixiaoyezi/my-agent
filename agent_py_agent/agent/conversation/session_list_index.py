"""可丢弃的列表定位投影。只保存文件引用/排序元数据，不保存任务正文、状态或执行许可。

旧目录按有界批次回填，未追平明确partial；常规写者提交canonical后发布索引修订。
每次返回仍读原文件，跨进程原位改写以文件身份复核；不缓存可变Session。
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import ensure_private_dir
from ..io.sqlite_scope import connect_projection_sqlite, scoped_connection
from .observation_index import _close_failed_factory

_REBUILD_BATCH = 128


def _stamp(path: Path) -> str:
    try:
        item = path.lstat()
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(item.st_mode) or (not stat.S_ISREG(item.st_mode) and not stat.S_ISDIR(item.st_mode)):
        raise ValueError("list projection source is not a regular file/directory")
    return f"{item.st_dev}:{item.st_ino}:{item.st_size}:{item.st_mtime_ns}:{item.st_ctime_ns}"


class SessionListIndex:
    """每次查询内存与正文读取有界，SQLite只作检索；删除索引不会删除或改变业务记录。"""

    def __init__(self, root: Path, *, kind: str, reader):
        if kind not in {"session", "session_task", "thread"}:
            raise ValueError("invalid list projection kind")
        self.root, self.kind, self.reader = Path(root), kind, reader
        self.path = self.root.parent / f".{self.root.name}-{kind}-list.sqlite3"
        self.scope = hashlib.sha256(f"{self.root.resolve()}:{kind}".encode()).hexdigest()[:24]

    @contextmanager
    def _connection(self):
        ensure_private_dir(self.path.parent)
        with locked_json_path(self.path):
            if not self.path.exists():
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
            _stamp(self.path)
            info = self.path.lstat()
            if info.st_nlink != 1:
                raise ValueError("list projection hardlink rejected")
            with scoped_connection(self.path, False, self._open_projection,
                                   rebuild_projection=self._projection_rebuild_proof) as conn:
                try:
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise

    def _projection_rebuild_proof(self):
        return _stamp(self.root) if self.root.is_dir() else None

    # LLM: 两个排序索引分别服务过滤和无过滤 keyset 查询，按原建库路径幂等升级。
    # 函数用途: 打开私有列表投影并补齐有界分页所需索引。
    def _open_projection(self):
        conn = connect_projection_sqlite(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-1024")
            conn.execute("CREATE TABLE IF NOT EXISTS entries (path TEXT PRIMARY KEY, sort_at REAL, filter_key TEXT, stamp TEXT, generation INTEGER)")
            conn.execute("CREATE INDEX IF NOT EXISTS list_order ON entries(filter_key, sort_at DESC, path DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS list_order_all ON entries(sort_at DESC, path DESC)")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('revision', ?)", (str(time.time_ns()),))
            if self.kind == 'session_task':
                conn.execute('CREATE TABLE IF NOT EXISTS artifact_refs (artifact_id TEXT, task_path TEXT, PRIMARY KEY(artifact_id,task_path))')
                conn.execute('CREATE INDEX IF NOT EXISTS artifact_refs_task ON artifact_refs(task_path)')
                conn.execute('CREATE TABLE IF NOT EXISTS artifact_errors (task_path TEXT PRIMARY KEY)')
                conn.execute('CREATE TRIGGER IF NOT EXISTS artifact_entry_deleted AFTER DELETE ON entries BEGIN '
                             'DELETE FROM artifact_refs WHERE task_path=OLD.path; '
                             'DELETE FROM artifact_errors WHERE task_path=OLD.path; END')
            return conn
        except BaseException as error:
            _close_failed_factory(conn, error)
            raise

    def _meta(self, conn, key, default=""):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _put_meta(self, conn, key, value):
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def _record(self, conn, path: Path, generation: int):
        relative = str(path.relative_to(self.root))
        if not path.exists():
            conn.execute("DELETE FROM entries WHERE path=?", (relative,))
            return
        version = _stamp(path)
        payload, error = self.reader(path)
        if error is not None:
            # 坏记录也保留定位引用，页面读原文件时返回load_errors，不能当不存在。
            sort_at, filter_key = 0.0, ""
        else:
            sort_at = float(payload.get("created_at" if self.kind == "session_task" else "updated_at") or 0)
            filter_key = str(payload.get(self._filter_field()) or "")
        conn.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?)",
                     (relative, sort_at, filter_key, version, generation))
        if self.kind == 'session_task':
            conn.execute('DELETE FROM artifact_refs WHERE task_path=?', (relative,))
            conn.execute('DELETE FROM artifact_errors WHERE task_path=?', (relative,))
            metadata = payload.get('metadata', {}) if error is None else {}
            refs = metadata.get('result_artifacts', []) if isinstance(metadata, dict) else None
            invalid = error is not None or not isinstance(refs, list)
            for item in refs if isinstance(refs, list) else []:
                identifier = item.get('artifact_id') if isinstance(item, dict) else None
                if (not isinstance(identifier, str) or not identifier or len(identifier.encode()) > 128
                        or '/' in identifier or '\\' in identifier):
                    invalid = True
                    continue
                conn.execute('INSERT OR IGNORE INTO artifact_refs VALUES(?,?)', (identifier, relative))
            if invalid:
                conn.execute('INSERT OR IGNORE INTO artifact_errors VALUES(?)', (relative,))

    def publish(self, path: Path) -> None:
        """只在原成功提交后调用；索引写失败不回滚已提交的canonical。"""
        with self._connection() as conn:
            self._record(conn, path, int(self._meta(conn, "generation", "0")))
            self._put_meta(conn, "revision", int(self._meta(conn, "revision", "0")) + 1)
            # 不用目录mtime认证内容；它只发现新旧写者的新增/删除，原位更新仍读文件身份。

    def _filter_field(self):
        return {"session": "user_id", "session_task": "target_thread_id", "thread": "owner_id"}[self.kind]

    # LLM: 重建和游标版本在同一连接事务提交；每批成员变化都废弃前一批的游标。
    # 函数用途: 有界补齐定位记录，防止后续批次插入到已越过的排序区间。
    def _refresh(self, conn) -> bool:
        current = _stamp(self.root)
        if self._meta(conn, "directory") != current:
            self._put_meta(conn, "directory", current)
            self._put_meta(conn, "offset", 0)
            self._put_meta(conn, "complete", 0)
            self._put_meta(conn, "generation", int(self._meta(conn, "generation", "0")) + 1)
            self._put_meta(conn, "revision", int(self._meta(conn, "revision", "0")) + 1)
        if self._meta(conn, "complete", "0") == "1":
            return not self._probe_file_identities(conn)
        offset = int(self._meta(conn, "offset", "0"))
        generation = int(self._meta(conn, "generation", "0"))
        selected = []
        before = conn.total_changes
        if self.root.exists():
            with os.scandir(self.root) as entries:
                selected = list(itertools.islice(entries, offset, offset + _REBUILD_BATCH + 1))
            for entry in selected[:_REBUILD_BATCH]:
                path = Path(entry.path) / "session.json" if self.kind == "session" else Path(entry.path)
                if self.kind == "session" and not entry.is_dir(follow_symlinks=False):
                    continue
                if self.kind != "session" and (not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False)):
                    continue
                self._record(conn, path, generation)
        changed = conn.total_changes > before
        complete = len(selected) <= _REBUILD_BATCH and current == _stamp(self.root)
        self._put_meta(conn, "offset", offset + min(len(selected), _REBUILD_BATCH))
        self._put_meta(conn, "complete", int(complete))
        if complete:
            removed = conn.execute("DELETE FROM entries WHERE generation<>?", (generation,)).rowcount
            changed = changed or removed > 0
        # entries 的写入或最终清理与版本一起提交，不能签发跨批仍有效的旧游标。
        if changed:
            self._put_meta(conn, "revision", int(self._meta(conn, "revision", "0")) + 1)
        return complete

    def _probe_file_identities(self, conn):
        # 老写者可原位改正文而不改变目录mtime。每页只stat两个引用，循环游标落原投影；
        # 发现变化才读正文修位置。稳定页不全目录遍历、不把未抽中的可变对象当权限依据。
        after = self._meta(conn, "probe_after")
        rows = conn.execute("SELECT path,stamp FROM entries WHERE path>? ORDER BY path LIMIT 2", (after,)).fetchall()
        if not rows:
            rows = conn.execute("SELECT path,stamp FROM entries ORDER BY path LIMIT 2").fetchall()
        changed = False
        for row in rows:
            path = self.root / row["path"]
            if _stamp(path) != row["stamp"]:
                self._record(conn, path, int(self._meta(conn, "generation", "0")))
                changed = True
        self._put_meta(conn, "probe_after", rows[-1]["path"] if rows else "")
        if changed:
            self._put_meta(conn, "revision", int(self._meta(conn, "revision")) + 1)
        return changed

    def page(self, *, limit: int = 50, cursor: str | None = None, filter_key: str | None = None) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("list limit must be 1..100")
        with self._connection() as conn:
            complete = self._refresh(conn)
            revision = self._meta(conn, "revision", "0")
            after = None
            if cursor:
                try:
                    value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    if value[:3] != [self.scope, revision, filter_key]:
                        return {"items": [], "next_cursor": None, "coverage": "expired", "load_errors": []}
                    after = (float(value[3]), str(value[4]))
                except (ValueError, TypeError, IndexError):
                    raise ValueError("invalid list cursor") from None
            where, args = [], []
            if filter_key is not None:
                where.append("(filter_key=? OR filter_key='')")
                args.append(filter_key)
            if after is not None:
                where.append("(sort_at,path)<(?,?)")
                args.extend(after)
            sql = "SELECT * FROM entries" + (" WHERE " + " AND ".join(where) if where else "")
            rows = conn.execute(sql + " ORDER BY sort_at DESC,path DESC LIMIT ?", (*args, limit + 1)).fetchall()
            result, errors = [], []
            changed = False
            for row in rows[:limit]:
                path = self.root / row["path"]
                if _stamp(path) != row["stamp"]:
                    self._record(conn, path, int(self._meta(conn, "generation", "0")))
                    changed = True
                if not path.exists():
                    continue
                payload, error = self.reader(path)
                if error:
                    errors.append(error)
                elif filter_key is None or str(payload.get(self._filter_field()) or "") == filter_key:
                    result.append(payload)
            if changed:
                self._put_meta(conn, "revision", int(revision) + 1)
                complete = False
            last = rows[min(limit, len(rows)) - 1] if rows else None
            next_cursor = base64.urlsafe_b64encode(json.dumps([
                self.scope, revision, filter_key, last["sort_at"], last["path"]], separators=(",", ":")).encode()).decode() if last and len(rows) > limit and not changed else None
            return {"items": result, "next_cursor": next_cursor,
                    "coverage": "complete" if complete else "partial", "load_errors": errors}


    def artifact_tasks(self, artifact_id: str, *, limit: int = 2) -> dict:
        """只在原session_task索引定位候选；旧行每次32条回填，返回前重读原记录。"""
        if self.kind != 'session_task' or type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('invalid artifact task lookup')
        with self._connection() as conn:
            complete = self._refresh(conn)
            if self._meta(conn, 'artifact_backfill_complete') != '1':
                after = self._meta(conn, 'artifact_backfill_after')
                rows = conn.execute('SELECT path FROM entries WHERE path>? ORDER BY path LIMIT 33', (after,)).fetchall()
                for row in rows[:32]:
                    self._record(conn, self.root / row['path'], int(self._meta(conn, 'generation', '0')))
                if rows:
                    self._put_meta(conn, 'artifact_backfill_after', rows[min(32, len(rows)) - 1]['path'])
                if len(rows) <= 32 and complete:
                    self._put_meta(conn, 'artifact_backfill_complete', 1)
            complete = complete and self._meta(conn, 'artifact_backfill_complete') == '1'
            candidates = conn.execute('SELECT e.path,e.stamp FROM artifact_refs a JOIN entries e ON e.path=a.task_path '
                'WHERE a.artifact_id=? ORDER BY e.path LIMIT ?', (artifact_id, limit + 1)).fetchall()
            if len(candidates) > limit:
                complete = False
            items, errors = [], []
            for row in candidates[:limit]:
                path = self.root / row['path']
                payload, error = self.reader(path)
                if error is not None:
                    errors.append('ARTIFACT_TASK_SOURCE_UNREADABLE')
                    complete = False
                    continue
                # 索引只定位，当前原记录必须仍宣称同一ID；失效行通过原发布路径修复。
                refs = payload.get('metadata', {}).get('result_artifacts', [])
                if not any(isinstance(item, dict) and item.get('artifact_id') == artifact_id for item in refs):
                    self._record(conn, path, int(self._meta(conn, 'generation', '0')))
                    complete = False
                    continue
                items.append(payload)
            if conn.execute('SELECT 1 FROM artifact_errors LIMIT 1').fetchone():
                complete = False
            return {'items': items, 'coverage': 'complete' if complete else 'partial', 'load_errors': errors,
                    'truncated': len(candidates) > limit}
