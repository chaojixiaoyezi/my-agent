# LLM: SQLite 只作定位；新 ack 的权威是独立私有追加日志，损坏投影可按持久字节游标重放。
# 模块用途: 按 ID 保存确认并以固定切片迁移 legacy 和观察账，不在热路读取或改写整图。
from __future__ import annotations

import codecs
import itertools
import json
import math
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..common.cache_freshness import CACHE_TRUST_AGE_SECONDS
from ..common.json_io import check_io_deadline, locked_json_path
from ..common.nofollow_fs import ensure_private_dir
from ..user_space.home_indexes import is_sqlite_busy
from .models import ObservationEvent

_INSTALLED: set[tuple[str, int, int]] = set()
_INSTALLED_LOCK = threading.Lock()
# 单次同步确认追加日志的最大读取字节数
_ACK_READ_BYTES = 65536
# 单次目录增量扫描最多发现的观察文件条目数
_DISCOVER_ENTRIES_COUNT = 1024


def _directory_stamp(path):
    """只看目录代次，不遍历已完成的历史来源。"""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


# LLM: 迁移/损坏不能伪装成空集合，原 list 接口没有 coverage 字段时通过异常明确暴露。
# 类用途: 表示观察索引或权威确认源无法可靠读取。
class ObservationIndexError(ValueError):
    pass


# LLM: unknown 与 pending 是不同状态，调用者重试时继续同一持久切片。
# 类用途: 暴露索引尚未追平，避免未迁移 ack 再次上报。
class ObservationIndexIncomplete(ObservationIndexError):
    pass


def _stat_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: 所有来源拒绝链接及特殊文件，missing 仅代表真实不存在。
# 函数用途: 返回包含 ctime 的文件身份。
def _identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ObservationIndexError("observation source must be a single regular file")
    return _stat_identity(info)


# LLM: 不依赖普通 open 的跟随链接行为，打开后再次核对同一 inode。
# 函数用途: 打开只读来源并关闭异常出口的 FD。
@contextmanager
def _reader(path):
    expected = _identity(path)
    if expected is None:
        raise FileNotFoundError(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        actual = _stat_identity(info)
        # 追加及权限刷新会改变时间戳；同一私有 inode 未缩短即可继续读实际打开的文件。
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or actual[:2] != expected[:2] or actual[2] < expected[2]):
            raise ObservationIndexError("observation source changed while opening")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(fd)


# LLM: 只解析一段顶层 flat map，游标位于键值对后；超大单项不放宽全局 1MiB 限制。
# 函数用途: 在 64KiB/128 项上限内读取 legacy JSON 对象的下一切片。
def _legacy_slice(path, offset):
    with _reader(path) as handle:
        handle.seek(offset)
        raw = handle.read(65536)
        eof = handle.tell() == os.fstat(handle.fileno()).st_size
    text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=eof)
    decoder = json.JSONDecoder()
    pos, pairs = 0, []
    if offset == 0:
        pos = len(text) - len(text.lstrip())
        if text[pos:pos + 1] != "{":
            raise ObservationIndexError("legacy observation handled map is not an object")
        pos += 1
    committed = pos
    for _ in range(128):
        start = pos
        pos = len(text) - len(text[pos:].lstrip())
        if text[pos:pos + 1] == "}":
            if text[pos + 1:].strip() or not eof:
                raise ObservationIndexError("legacy observation handled map has trailing data")
            return pairs, offset + len(raw), True, len(raw)
        if offset or pairs:
            if text[pos:pos + 1] != ",":
                if not eof and pairs:
                    break
                raise ObservationIndexError("legacy observation handled separator is invalid")
            pos += 1
            pos = len(text) - len(text[pos:].lstrip())
        try:
            key, pos = decoder.raw_decode(text, pos)
            if not isinstance(key, str):
                raise ObservationIndexError("legacy observation handled ID is invalid")
            pos = len(text) - len(text[pos:].lstrip())
            if text[pos:pos + 1] != ":":
                raise ObservationIndexError("legacy observation handled value is invalid")
            pos += 1
            pos = len(text) - len(text[pos:].lstrip())
            value, pos = decoder.raw_decode(text, pos)
            # 读取块恰好在数字中间时不能提前接受其前缀。
            if pos == len(text) and not eof:
                raise json.JSONDecodeError("incomplete value", text, pos)
        except (ValueError, IndexError) as exc:
            if not eof and pairs:
                pos = start
                break
            raise ObservationIndexError("legacy observation handled map is unreadable or an entry exceeds 64KiB") from exc
        pairs.append((key, value))
        committed = pos
    return pairs, offset + len(text[:committed].encode("utf-8")), False, len(raw)


# LLM: 同 owner 的投影与 ack 日志用相同锁序；派生表可丢弃，独立确认日志绝不截断。
# 类用途: 提供精确确认、只读迁移和 pending 候选索引。
class ObservationIndex:
    # LLM: 延迟 IO 保持只读构造语义；路径遵循 SessionListIndex 的同级私有命名约定。
    # 函数用途: 绑定唯一观察根和恢复源。
    def __init__(self, storage):
        self.storage = storage
        self.path = storage.root.parent / f".{storage.root.name}-observations.sqlite3"
        self.journal_path = storage.root.parent / f".{storage.root.name}-observation-acks.jsonl"
        self.last_work = {}
        self._probe_after = ""
        self._quiet = None

    # LLM: busy/磁盘写错不是损坏，只允许 CORRUPT/NOTADB 重建投影，且保留 ack 日志。
    # 函数用途: 在原路径锁内打开、提交和关闭连接。
    @contextmanager
    def _connection(self):
        ensure_private_dir(self.path.parent)
        with locked_json_path(self.path):
            if not self.path.exists():
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
            info = _identity(self.path)
            os.chmod(self.path, 0o600, follow_symlinks=False)
            for suffix in ("-wal", "-shm"):
                _identity(Path(str(self.path) + suffix))
            try:
                conn = self._open()
            except sqlite3.DatabaseError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 255
                if is_sqlite_busy(exc) or code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                    raise
                with _INSTALLED_LOCK:
                    _INSTALLED.discard((str(self.path), *info[:2]))
                fd = os.open(self.path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    current = os.fstat(fd)
                    if (current.st_dev, current.st_ino, current.st_nlink) != (info[0], info[1], 1):
                        raise ObservationIndexError("observation projection changed during repair")
                    os.ftruncate(fd, 0)
                finally:
                    os.close(fd)
                conn = self._open()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    # LLM: ack 与 pending 同事务更新；全局及 thread 两种访问均有匹配的排序索引。
    # 函数用途: 初始化私有 WAL 数据库。
    def _open(self):
        identity = _identity(self.path)
        key = (str(self.path), *identity[:2])
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode=rw", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA cache_size=-1024")
            with _INSTALLED_LOCK:
                if key not in _INSTALLED or not identity[2]:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                    conn.execute("CREATE TABLE IF NOT EXISTS acks (id TEXT PRIMARY KEY, at REAL NOT NULL)")
                    conn.execute("CREATE TABLE IF NOT EXISTS sources (path TEXT PRIMARY KEY, stamp TEXT, offset INTEGER, complete INTEGER)")
                    conn.execute("CREATE TABLE IF NOT EXISTS fragments (path TEXT PRIMARY KEY, start INTEGER, raw BLOB)")
                    conn.execute("CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, thread TEXT, needs_main INTEGER, status TEXT, due REAL, path TEXT, offset INTEGER, length INTEGER, wake TEXT)")
                    conn.execute("CREATE INDEX IF NOT EXISTS pending_thread ON pending(thread,needs_main,status,due,id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS pending_all ON pending(needs_main,status,due,id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS pending_source ON pending(path)")
                    conn.execute("CREATE INDEX IF NOT EXISTS pending_wake ON pending(wake,status,due,id)")
                    conn.execute("CREATE INDEX IF NOT EXISTS source_work ON sources(complete,path)")
                    conn.commit()
                    _INSTALLED.add(key)
            return conn
        except BaseException:
            conn.close()
            raise

    @contextmanager
    def _read_connection(self, *, blocking=True, deadline=None):
        """已安装且 WAL 设施齐备时只读；返回 None 由调用方在退出后进入原写路径。"""
        check_io_deadline(deadline)
        identity = _identity(self.path)
        key = (str(self.path), *identity[:2]) if identity else None
        with _INSTALLED_LOCK:
            installed = key in _INSTALLED
        if not installed or not identity[2] or any(
            _identity(Path(str(self.path) + suffix)) is None for suffix in ("-wal", "-shm")
        ):
            yield None
            return
        seconds = 5 if blocking else 0
        if deadline is not None:
            seconds = min(seconds, max(0, deadline - time.monotonic()))
        conn = None
        try:
            conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=seconds)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(seconds * 1000)}")
            conn.execute("BEGIN")
            conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
            current = _identity(self.path)
            if current is None or current[:2] != identity[:2]:
                conn.close()
                conn = None
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            if getattr(exc, "sqlite_errorcode", 0) & 255 not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise
            with _INSTALLED_LOCK:
                _INSTALLED.discard(key)
            conn = None
        except BaseException:
            if conn is not None:
                conn.close()
            raise
        try:
            yield conn
            check_io_deadline(deadline)
        finally:
            if conn is not None:
                conn.close()

    def _journal_current(self, conn):
        """只比较持久游标与权威字节身份，不在查询事务中回放或改写。"""
        identity = _identity(self.journal_path)
        offset = int(self._get(conn, "journal_offset"))
        if identity is None:
            return offset == 0
        return (offset == identity[2]
                and self._get(conn, "journal_identity", "") == json.dumps(identity[:2])
                and self._get(conn, "journal_stamp", "") == json.dumps(identity))

    def _legacy_current(self, conn, path):
        return (self._get(conn, "legacy_complete") == "1"
                and self._get(conn, "legacy_stamp", "") == json.dumps(_identity(path)))

    def _quiet_signature(self, scope=None):
        return (_identity(self.journal_path), _identity(self.storage.observation_handled_path),
                _directory_stamp(self.storage.observations_dir))

    # LLM: 同一回合轮流询问多个 scope（插话的 thread/request/agent_run/task 目标、观察的不同 wake）；单槽空结果
    #   会被下一个 scope 顶掉，每次询问都重开 SQLite 写事务。按 scope 各留一份，来源签名与可信窗口规则不变；
    #   投影库只比 dev/inode：任一 scope 的空刷新都会改库的 ctime/mtime，重建换 inode 仍失效，内容变化由来源签名覆盖。
    #   只整份替换、从不原地改，出窗口即丢，规模随近期 scope 数有界。
    def _quiet_hit(self, scope, signature):
        current = self._quiet
        quiet = current.get(scope) if isinstance(current, dict) else None
        return bool(quiet and quiet[0] == (signature, (_identity(self.path) or ())[:2])
                    and 0 <= time.monotonic() - quiet[2] < CACHE_TRUST_AGE_SECONDS
                    and time.time() < quiet[1])

    def _remember_quiet(self, scope, before, earliest_due):
        # 不能把查询结束后新到的来源签名配给旧的空结果。
        after = self._quiet_signature(scope)
        now, current = time.monotonic(), self._quiet
        quiet = {key: value for key, value in current.items()
                 if key != scope and 0 <= now - value[2] < CACHE_TRUST_AGE_SECONDS} if isinstance(current, dict) else {}
        if before == after:
            quiet[scope] = ((after, (_identity(self.path) or ())[:2]), earliest_due, now)
        self._quiet = quiet

    # LLM: 标量元数据不复制整个历史。
    # 函数用途: 精确读取一个持久游标。
    def _get(self, conn, key, default="0"):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # LLM: 游标更新与该批数据同事务提交。
    # 函数用途: 保存一个恢复位置。
    def _put(self, conn, key, value):
        if self._get(conn, key, None) == str(value):
            return
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    # LLM: 首次正处理时间单调保留，零值仍可由之后真正的确认提升。
    # 函数用途: 幂等安装一批 ack 并从候选表清理对应 ID。
    def _install(self, conn, pairs):
        conn.executemany("INSERT INTO acks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET at=excluded.at WHERE acks.at<=0", pairs)
        conn.executemany("DELETE FROM pending WHERE id=? AND ? > 0", [(key, value) for key, value in pairs])

    # LLM: 新权威日志只能追加，成功 fsync 后才提交派生表，失败重试由日志游标幂等恢复。
    # 函数用途: 持久保存当前精确 ack 批次。
    def _append(self, pairs):
        raw = b"".join((json.dumps({"id": key, "at": value}, ensure_ascii=False) + "\n").encode() for key, value in pairs)
        expected = _identity(self.journal_path)
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (expected and (info.st_dev, info.st_ino) != expected[:2]):
                raise ObservationIndexError("observation ack journal identity changed")
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("observation ack journal short write")
                view = view[written:]
            os.fsync(fd)
            end = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if expected is None and os.name == "posix":
            parent = os.open(self.journal_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        return end, len(raw)

    # LLM: 恢复一次最多128行，达到64KiB后在完整行边界停止；单行仍受64KiB限制。
    # 函数用途: 将权威 ack 日志的下一批安装到投影。
    def _sync(self, conn, work):
        offset = int(self._get(conn, "journal_offset"))
        identity = _identity(self.journal_path)
        if identity is None:
            if offset:
                raise ObservationIndexError("observation ack journal is missing")
            return True
        previous = self._get(conn, "journal_identity", "")
        if previous and json.loads(previous) != list(identity[:2]):
            raise ObservationIndexError("observation ack journal was replaced")
        if offset > identity[2]:
            raise ObservationIndexError("observation ack journal was truncated")
        if offset == identity[2]:
            stamp = self._get(conn, "journal_stamp", "")
            if stamp and json.loads(stamp)[:4] != list(identity[:4]):
                raise ObservationIndexError("observation ack journal changed without append")
            # fchmod 仅刷新 ctime，不能把权限维护误判为等长正文改写。
            self._put(conn, "journal_stamp", json.dumps(identity))
            return True
        pairs, consumed = [], 0
        with _reader(self.journal_path) as handle:
            opened = _stat_identity(os.fstat(handle.fileno()))
            if opened[:2] != identity[:2] or opened[2] < identity[2]:
                raise ObservationIndexError("observation ack journal changed while opening")
            identity = opened
            handle.seek(offset)
            for _ in range(128):
                raw = handle.readline(_ACK_READ_BYTES + 1)
                if not raw:
                    break
                work["bytes_read"] += len(raw)
                if len(raw) > _ACK_READ_BYTES:
                    raise ObservationIndexError("observation ack journal has an incomplete or oversized record")
                if not raw.endswith(b"\n"):
                    current = _stat_identity(os.fstat(handle.fileno()))
                    if current != identity and current[2] >= identity[2]:
                        # 写者仍在追加尾行，本拍只提交此前完整行，下一拍从该行开头重读。
                        break
                    raise ObservationIndexError("observation ack journal has an incomplete or oversized record")
                try:
                    payload = json.loads(raw)
                    key, value = payload["id"], float(payload["at"])
                    if not isinstance(key, str) or not key or not math.isfinite(value):
                        raise ValueError("invalid ack")
                except (ValueError, KeyError, TypeError) as exc:
                    raise ObservationIndexError("observation ack journal is unreadable") from exc
                pairs.append((key, value))
                consumed += len(raw)
                if consumed >= _ACK_READ_BYTES:
                    break
            info = os.fstat(handle.fileno())
            if info.st_nlink != 1 or info.st_size < max(identity[2], offset + consumed):
                raise ObservationIndexError("observation ack journal changed while reading")
            # 游标与实际 FD 的代次一起提交，不使用打开前可能已过时的大小和时间戳。
            identity = _stat_identity(info)
        self._install(conn, pairs)
        self._put(conn, "journal_offset", offset + consumed)
        self._put(conn, "journal_identity", json.dumps(identity[:2]))
        self._put(conn, "journal_stamp", json.dumps(identity))
        work["json_decodes"] += len(pairs)
        return offset + consumed == identity[2]

    # LLM: legacy 永远只读；有效确认追加到独立日志后才移动游标，重放不会撤销或重写既有确认。
    # 函数用途: 迁移下一批旧 flat map，返回是否已完整覆盖。
    def _legacy(self, conn, work):
        path = self.storage.observation_handled_path
        identity = _identity(path)
        stamp = json.dumps(identity)
        previous = self._get(conn, "legacy_stamp", "")
        offset = int(self._get(conn, "legacy_offset"))
        complete = self._get(conn, "legacy_complete") == "1"
        if identity is None:
            if previous and offset and not complete:
                raise ObservationIndexError("unfinished legacy observation map is missing")
            self._put(conn, "legacy_stamp", stamp)
            self._put(conn, "legacy_complete", 1)
            return True
        if previous != stamp:
            offset, complete = 0, False
        if complete:
            return True
        pairs, after, complete, consumed = _legacy_slice(path, offset)
        if _identity(path) != identity:
            raise ObservationIndexIncomplete("legacy observation map changed during migration")
        valid = []
        for key, raw in pairs:
            try:
                value = float(raw or 0)
            except (ValueError, TypeError):
                continue
            if math.isfinite(value) and value > 0:
                existing = conn.execute("SELECT at FROM acks WHERE id=?", (key,)).fetchone()
                if not existing or existing[0] <= 0:
                    valid.append((key, value))
        if valid:
            end, written = self._append(valid)
            self._install(conn, valid)
            self._put(conn, "journal_offset", end)
            journal = _identity(self.journal_path)
            self._put(conn, "journal_identity", json.dumps(journal[:2]))
            self._put(conn, "journal_stamp", json.dumps(journal))
            work["bytes_written"] += written
        self._put(conn, "legacy_stamp", stamp)
        self._put(conn, "legacy_offset", after)
        self._put(conn, "legacy_complete", int(complete))
        work["bytes_read"] += consumed
        work["json_decodes"] += len(pairs) * 2
        return complete

    # LLM: 对有限 ID 列表逐主键查询；不创建与历史等大的 dict 或 IN 参数列表。
    # 函数用途: 查询请求涉及的 ack 时间。
    def _lookup(self, conn, ids):
        result = {}
        for key in ids:
            row = conn.execute("SELECT at FROM acks WHERE id=?", (key,)).fetchone()
            if row:
                result[key] = row[0]
        return result

    # LLM: ack 不依赖 legacy 迁移，超过旧 cap 也能立即提交精确新确认；重复确认不追加日志。
    # 函数用途: 保存一批 ID，保持首次正处理时间不变。
    def ack(self, ids, current):
        current = float(current)
        if not math.isfinite(current):
            raise ValueError("observation handled time must be finite")
        work = {"bytes_read": 0, "bytes_written": 0, "json_decodes": 0}
        complete = False
        with self._connection() as conn:
            complete = self._sync(conn, work)
            if complete:
                known = self._lookup(conn, ids)
                pairs = [(key, current) for key in dict.fromkeys(ids) if known.get(key, 0) <= 0]
                if pairs:
                    end, written = self._append(pairs)
                    self._install(conn, pairs)
                    self._put(conn, "journal_offset", end)
                    identity = _identity(self.journal_path)
                    self._put(conn, "journal_identity", json.dumps(identity[:2]))
                    self._put(conn, "journal_stamp", json.dumps(identity))
                    work["bytes_written"] += written
        self.last_work = work
        if not complete:
            raise ObservationIndexIncomplete("observation ack recovery is partial; retry continues its cursor")

    # LLM: 已持久确认的 ID 不受旧 map 缺失/损坏影响；unknown 必须先完成 bounded 迁移才能当作未处理。
    # 函数用途: 为当前候选读取精确处理时间。
    def handled(self, ids, *, include_legacy=True):
        ids = list(dict.fromkeys(ids))
        work = {"bytes_read": 0, "bytes_written": 0, "json_decodes": 0}
        complete, result = False, {}
        with self._read_connection() as conn:
            if conn is not None and self._journal_current(conn):
                result = self._lookup(conn, ids)
                if (not include_legacy or all(result.get(key, 0) > 0 for key in ids)
                        or self._legacy_current(conn, self.storage.observation_handled_path)):
                    self.last_work = work
                    return result
        with self._connection() as conn:
            synced = self._sync(conn, work)
            result = self._lookup(conn, ids)
            complete = synced and (not include_legacy or all(result.get(key, 0) > 0 for key in ids) or self._legacy(conn, work))
            if complete:
                result = self._lookup(conn, ids)
        self.last_work = work
        if not complete:
            raise ObservationIndexIncomplete("legacy observation ack coverage is partial; retry continues its cursor")
        return result

    # LLM: 小 legacy 快照可做本轮精确查找，迁移仍走相同固定批次与持久游标，不整图改写。
    # 函数用途: 仅推进 ack 迁移，不读取观察历史。
    def migrate_legacy(self):
        work = {"bytes_read": 0, "bytes_written": 0, "json_decodes": 0}
        with self._connection() as conn:
            complete = self._sync(conn, work) and self._legacy(conn, work)
        self.last_work = work
        return complete

    # LLM: 文件目录只发现来源引用，正文下一阶段按独立持久字节游标读取。
    # 函数用途: 每轮最多发现1024个目录项并抽查两个已知来源身份，常见目录变化可一拍覆盖。
    def _discover(self, conn):
        directory = self.storage.observations_dir
        try:
            info = directory.stat()
            stamp = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}"
        except FileNotFoundError:
            stamp = "missing"
        if self._get(conn, "directory", "") != stamp:
            self._put(conn, "directory", stamp)
            self._put(conn, "directory_offset", 0)
            self._put(conn, "directory_complete", 0)
        if self._get(conn, "directory_complete") != "1":
            offset = int(self._get(conn, "directory_offset"))
            selected = []
            if stamp != "missing":
                with os.scandir(directory) as iterator:
                    selected = list(itertools.islice(iterator, offset, offset + _DISCOVER_ENTRIES_COUNT + 1))
                for entry in selected[:_DISCOVER_ENTRIES_COUNT]:
                    if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                        self._source(conn, Path(entry.path))
            self._put(conn, "directory_offset", offset + min(len(selected), _DISCOVER_ENTRIES_COUNT))
            self._put(conn, "directory_complete", int(len(selected) <= _DISCOVER_ENTRIES_COUNT))
        after = self._probe_after
        rows = conn.execute("SELECT path FROM sources WHERE path>? ORDER BY path LIMIT 2", (after,)).fetchall()
        if not rows:
            rows = conn.execute("SELECT path FROM sources ORDER BY path LIMIT 2").fetchall()
        for row in rows:
            self._source(conn, directory / row[0])
        self._probe_after = rows[-1][0] if rows else ""

    # LLM: 观察账按追加协议增长；替换/缩短/等长改写丢弃引用及未完成行，删除源也清理片段。
    # 函数用途: 更新一个来源的身份与待回填位置。
    def _source(self, conn, path):
        name = path.name
        identity = _identity(path)
        row = conn.execute("SELECT * FROM sources WHERE path=?", (name,)).fetchone()
        if identity is None:
            conn.execute("DELETE FROM pending WHERE path=?", (name,))
            conn.execute("DELETE FROM fragments WHERE path=?", (name,))
            conn.execute("DELETE FROM sources WHERE path=?", (name,))
            return
        stamp = json.dumps(identity)
        if row and row["stamp"] == stamp:
            return
        previous = json.loads(row["stamp"]) if row else None
        offset = row["offset"] if row and previous[:2] == list(identity[:2]) and identity[2] > previous[2] else 0
        if not offset:
            conn.execute("DELETE FROM pending WHERE path=?", (name,))
            conn.execute("DELETE FROM fragments WHERE path=?", (name,))
        conn.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,0)", (name, stamp, offset))

    # LLM: 所有源合计128行/64KiB；未完成行与字节游标同事务持久化，删除竞态不回滚探测进度。
    # 函数用途: 建立下一批 canonical 行引用，不复制整条历史账。
    def _refresh(self, conn, work):
        self._discover(conn)
        sources = conn.execute("SELECT * FROM sources WHERE complete=0 ORDER BY path LIMIT 4").fetchall()
        remaining, lines = 65536, 128
        for source in sources:
            if remaining <= 0 or lines <= 0:
                break
            path = self.storage.observations_dir / source["path"]
            try:
                remaining, lines = self._advance_source(conn, source, work, remaining, lines)
            except FileNotFoundError:
                self._source(conn, path)
        return self._get(conn, "directory_complete") == "1" and conn.execute("SELECT 1 FROM sources WHERE complete=0 LIMIT 1").fetchone() is None

    # LLM: 大于单轮预算的合法行分批暂存；片段只用于重建且可丢弃，返回事件仍点读canonical。
    # 函数用途: 用剩余预算推进一个来源，记录跨轮行片段而不收紧原合法行长度。
    def _advance_source(self, conn, source, work, remaining, lines):
        name = source["path"]
        path = self.storage.observations_dir / name
        identity = _identity(path)
        if identity is None:
            self._source(conn, path)
            return remaining, lines
        fragment = conn.execute("SELECT start,raw FROM fragments WHERE path=?", (name,)).fetchone()
        offset = source["offset"]
        with _reader(path) as handle:
            handle.seek(offset)
            while remaining > 0 and lines > 0:
                start = fragment["start"] if fragment else handle.tell()
                raw = handle.readline(remaining)
                if not raw:
                    break
                remaining -= len(raw)
                work["bytes_read"] += len(raw)
                offset = handle.tell()
                data = bytes(fragment["raw"]) + raw if fragment else raw
                if not raw.endswith(b"\n") and offset != identity[2]:
                    conn.execute("INSERT OR REPLACE INTO fragments VALUES (?,?,?)", (name, start, data))
                    break
                fragment = None
                conn.execute("DELETE FROM fragments WHERE path=?", (name,))
                lines -= 1
                if not data.strip():
                    continue
                try:
                    work["json_decodes"] += 1
                    event = ObservationEvent.from_dict(json.loads(data))
                except (ValueError, TypeError, KeyError) as exc:
                    work.setdefault("row_errors", []).append({"path": str(path), "offset": start, "message": str(exc)})
                    continue
                handled = conn.execute("SELECT at FROM acks WHERE id=?", (event.observation_id,)).fetchone()
                if event.handled_at > 0 or (handled and handled[0] > 0):
                    continue
                conn.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,'pending',?,?,?,?,?)",
                    (event.observation_id, event.thread_id, int(event.requires_main_agent or event.requires_llm_report),
                     event.observed_at, name, start, len(data), event.wake_signal_id))
        if _identity(path) != identity:
            self._source(conn, path)
            raise ObservationIndexIncomplete("observation source changed during indexing")
        conn.execute("UPDATE sources SET offset=?,complete=? WHERE path=?", (offset, int(offset == identity[2]), name))
        return remaining, lines

    # LLM: 显式维护与普通查询使用相同固定预算；恢复进度在返回 partial 前已经提交。
    # 函数用途: 推进一次恢复并返回真实覆盖与 IO 计数。
    def maintenance(self):
        work = {"bytes_read": 0, "bytes_written": 0, "json_decodes": 0, "load_errors": []}
        complete = False
        with self._connection() as conn:
            try:
                synced = self._sync(conn, work)
                legacy = self._legacy(conn, work) if synced else False
                sources = self._refresh(conn, work) if synced else False
                complete = synced and legacy and sources
            except ObservationIndexError as exc:
                work["load_errors"].append({"message": str(exc), "type": type(exc).__name__})
            work["legacy_offset"] = int(self._get(conn, "legacy_offset"))
        work["coverage"] = "complete" if complete else "partial"
        self.last_work = work
        return work

    # LLM: 每次 SQL 最多 128 个活跃候选；limit<=0 的旧调用合同只放开活跃结果量，不触碰 handled 历史。
    # 函数用途: 通过 keyset 分页取请求涉及的 pending 引用。
    def _candidates(self, conn, limit, wake=None):
        rows, after = [], None
        while limit <= 0 or len(rows) < limit:
            size = min(128, limit - len(rows)) if limit > 0 else 128
            clause = " AND (due,id)>(?,?)" if after else ""
            args = (*after, size) if after else (size,)
            where = "needs_main=1" if wake is None else "wake=?"
            args = args if wake is None else (wake, *args)
            page = conn.execute("SELECT * FROM pending WHERE " + where + " AND status='pending'" + clause + " ORDER BY due,id LIMIT ?", args).fetchall()
            rows.extend(page)
            if len(page) < size:
                break
            after = (page[-1]["due"], page[-1]["id"])
        return rows

    # LLM: LIMIT 下推到 pending 索引，仅读候选 canonical 字节；未知 legacy 覆盖明确拒绝，不用空表推断未处理。
    # 函数用途: 返回指定数量的活跃待处理观察，不扫描历史；旧 limit<=0 返回全部活跃项。
    def pending(self, limit, *, wake=None):
        work = {"bytes_read": 0, "bytes_written": 0, "json_decodes": 0}
        scope = (limit, wake)
        signature = self._quiet_signature(scope)
        if self._quiet_hit(scope, signature):
            self.last_work = {**work, "candidate_rows": 0}
            return []
        result, complete = [], False
        with self._connection() as conn:
            synced = self._sync(conn, work)
            sources = self._refresh(conn, work) if synced else False
            rows = self._candidates(conn, 1, wake)
            # 没有候选也必须推进 migration；现代 ack 已知完整时才可忽略坏旧图。
            legacy = self._legacy(conn, work) if synced and rows else True
            complete = synced and sources and legacy
            if complete:
                # 本批 legacy 可能刚移除前页 handled 引用，重取同一个有界页面，不能误报空待处理。
                rows = self._candidates(conn, limit, wake)
                for row in rows:
                    ack = conn.execute("SELECT at FROM acks WHERE id=?", (row["id"],)).fetchone()
                    if ack and ack[0] > 0:
                        continue
                    path = self.storage.observations_dir / row["path"]
                    try:
                        with _reader(path) as handle:
                            handle.seek(row["offset"])
                            raw = handle.read(row["length"])
                        work["bytes_read"] += len(raw)
                        work["json_decodes"] += 1
                        event = ObservationEvent.from_dict(json.loads(raw))
                    except FileNotFoundError:
                        self._source(conn, path)
                        complete = False
                        continue
                    if event.observation_id != row["id"]:
                        conn.execute("UPDATE sources SET offset=0,complete=0 WHERE path=?", (row["path"],))
                        complete = False
                    elif event.handled_at <= 0 and ((wake is not None and event.wake_signal_id == wake) or
                                                   (wake is None and (event.requires_main_agent or event.requires_llm_report))):
                        result.append(event)
                    else:
                        conn.execute("DELETE FROM pending WHERE id=?", (row["id"],))
        work["candidate_rows"] = len(rows)
        self.last_work = work
        if not complete:
            raise ObservationIndexIncomplete("observation pending coverage is partial; retry continues its cursor")
        if not result:
            self._remember_quiet(scope, signature, min((row["due"] for row in rows), default=math.inf))
        else:
            self._quiet = None
        return result
