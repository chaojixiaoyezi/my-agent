"""可重建恢复投影的有界位置与写前责任；不授予业务执行、结算或文件删除权。"""
from __future__ import annotations

import base64
import ctypes
import errno
import json
import logging
import os
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import check_io_deadline, locked_json_path

_logger = logging.getLogger(__name__)


# LLM: 投影数据库必须在私有、无链接目录内；连接前后核同一普通文件，侧车不能沿链接产生副作用。
# 函数用途: 拒绝链接、硬链接和特殊文件，取得已存在文件身份。
def _regular_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"projection requires a single-link regular file: {path}")
    return info.st_dev, info.st_ino


# LLM: 构造与复核共用同一信任门；仅以已打开FD收紧当前用户的.my-agent子树，不改普通私有组祖先。
# 函数用途: 拒绝非可信属主和非sticky公共可写祖先，兼容当前用户的私有组目录。
def _ensure_trusted_ancestor(descriptor, current_path: Path, parent_stat):
    owner = os.geteuid()
    if parent_stat.st_uid not in {0, owner}:
        raise PermissionError("projection ancestor is not a trusted directory")
    if parent_stat.st_mode & stat.S_ISVTX:
        return parent_stat
    if parent_stat.st_mode & 0o002:
        raise PermissionError("projection ancestor is not a trusted directory")
    if parent_stat.st_mode & 0o020:
        if parent_stat.st_uid == owner and ".my-agent" in current_path.parts:
            os.fchmod(descriptor, parent_stat.st_mode & ~0o077)
            return os.fstat(descriptor)
        if parent_stat.st_uid != owner or parent_stat.st_gid != os.getegid():
            raise PermissionError("projection ancestor is not a trusted directory")
    return parent_stat


# LLM: 逐层dir_fd/no-follow保留父链；按共用信任门收紧.my-agent祖先，投影父目录始终要求本人私有。
# 函数用途: 在SQLite按路径打开之前建立私有父目录，并提供同一目录链的身份复核。
@contextmanager
def _projection_directory(path: Path):
    if os.open not in os.supports_dir_fd:
        raise OSError(errno.ENOTSUP, "projection requires descriptor-relative directory access")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    chain = []
    current = Path(path.anchor)
    descriptor = os.open(current, flags)
    chain.append((current, descriptor))
    try:
        for part in path.parent.parts[1:]:
            _ensure_trusted_ancestor(descriptor, current, os.fstat(descriptor))
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            current = current / part
            descriptor = child
            chain.append((current, descriptor))
        parent = _ensure_trusted_ancestor(descriptor, current, os.fstat(descriptor))
        if parent.st_uid != os.geteuid():
            raise PermissionError("projection directory must belong to the current identity")
        if parent.st_mode & 0o077:
            os.fchmod(descriptor, 0o700)

        # LLM: 复核所有已打开父节点及相同信任门；不以旧路径stat替代持有FD上的权限事实。
        # 函数用途: 在原锁等待后及SQLite连接前后拒绝父链换代，并自愈允许收紧的组写权限。
        def verify():
            for directory, held in chain:
                observed, opened = directory.lstat(), os.fstat(held)
                if (not stat.S_ISDIR(observed.st_mode)
                        or (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise ValueError("projection parent identity or trust changed")
                _ensure_trusted_ancestor(held, directory, opened)
            if os.fstat(descriptor).st_mode & 0o077:
                raise PermissionError("projection directory is no longer private")

        verify()
        yield descriptor, verify
    finally:
        for _directory, held in reversed(chain):
            os.close(held)


# LLM: 仅供可重建投影使用；父链已私有且稳定，不吞损坏、不截断文件、不改变canonical；连接/描述符逐次关闭。
# 函数用途: 在原路径跨进程锁内安全连接私有SQLite并原子提交单次投影事务。
@contextmanager
def private_projection_connection(path: Path, *, blocking: bool = True, deadline: float | None = None):
    check_io_deadline(deadline)
    path = Path(path).absolute()
    with _projection_directory(path) as (parent_fd, verify), locked_json_path(path, blocking=blocking):
        check_io_deadline(deadline)
        verify()
        _regular_identity(path)
        descriptor = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        db = None
        try:
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _regular_identity(path) != identity:
                raise ValueError("projection identity changed before connect")
            for suffix in ("-journal", "-wal", "-shm"):
                _regular_identity(path.with_name(path.name + suffix))
            os.fchmod(descriptor, 0o600)
            verify()
            timeout = max(0.0, deadline - time.monotonic()) if deadline is not None else 5.0
            db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=timeout if blocking else 0.0)
            verify()
            if _regular_identity(path) != identity:
                raise ValueError("projection identity changed during connect")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA cache_size=-256")
            with db:
                yield db
                check_io_deadline(deadline)
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise BlockingIOError(errno.EBUSY, "projection is busy", str(path)) from exc
            raise
        finally:
            if db is not None:
                db.close()
            os.close(descriptor)


# LLM: cookie保存内核块位置与至多32KiB未解码尾部；每页最多读取一个块、解码128项并关闭FD。
#   尾部也随cookie落盘，目录插入锁侧车时不按旧字节位置重读已经变化的块，重启不丢进度。
#   只支持已明确实现dirent ABI的Darwin/Linux，未知平台保守报错，绝不悄悄全量枚举或跳过N个前缀。
# 函数用途: 按原生目录位置领取固定数量真实目录项，返回下一内核位置和EOF。
def directory_page(directory: Path, cookie: str = "") -> tuple[tuple[str, ...], str, bool]:
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise OSError(errno.ENOTSUP, "bounded directory discovery requires a supported native cursor")
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        state = json.loads(cookie) if cookie else {}
        position = int(state.get("offset", 0))
        data = base64.b64decode(state.get("tail", ""), validate=True)
        if len(data) > 32768:
            raise ValueError("native directory continuation exceeds bounded block")
        count = len(data)
        if not data:
            os.lseek(descriptor, position, os.SEEK_SET)
            library = ctypes.CDLL(None, use_errno=True)
            buffer = ctypes.create_string_buffer(32768)
            if sys.platform == "darwin":
                read_dir = library.__getdirentries64
                read_dir.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int64)]
                read_dir.restype = ctypes.c_ssize_t
                base = ctypes.c_int64()
                count = read_dir(descriptor, buffer, len(buffer), ctypes.byref(base))
            else:
                read_dir = library.getdents64
                read_dir.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
                read_dir.restype = ctypes.c_ssize_t
                count = read_dir(descriptor, buffer, len(buffer))
            if count < 0:
                raise OSError(ctypes.get_errno(), "cannot read bounded directory cursor", str(directory))
            position = os.lseek(descriptor, 0, os.SEEK_CUR)
            data = buffer.raw[:count]
        names = []
        consumed = 0
        offset = 0
        while offset < len(data) and consumed < 128:
            length = int.from_bytes(data[offset + 16:offset + 18], sys.byteorder)
            name_offset = 21 if sys.platform == "darwin" else 19
            if length < 24 or offset + length > len(data):
                raise ValueError("invalid native directory entry length")
            name = os.fsdecode(data[offset + name_offset:offset + length].split(b"\0", 1)[0])
            if name not in {".", ".."}:
                names.append(name)
            offset += length
            consumed += 1
        next_cookie = json.dumps({"offset": position, "tail": base64.b64encode(data[offset:]).decode("ascii")}, separators=(",", ":"))
        return tuple(names), next_cookie, count == 0
    finally:
        os.close(descriptor)


# LLM: 数据库键不授予路径权限；Unicode请求编号可保留，但只允许一个普通文件名片段。
# 函数用途: 在拼规范路径前拒绝投影键中的越目录、空键与跨平台特殊分隔符。
def projection_key(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or any(c in value for c in "/\\\0:"):
        raise ValueError("invalid recovery projection key")
    return value


# LLM: 指纹只是调度提示，不能作为终态或输入结算证明；缺失和非普通文件均不能产生执行许可。
# 函数用途: 无正文读取地记录规范文件的结构版本。
def projection_file_stamp(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return "invalid"
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


# LLM: 只保存定位、版本与待核责任；写前登记先于canonical，确认必须在原路径锁内重读权威并按版本CAS。
# 类用途: 提供终态投影和输入回执的有界keyset队列、目录发现与逐页文件身份审计。
class RecoveryPendingIndex:
    # LLM: 每根独立，连接不跨调用保存；表中绝无请求正文或owner/执行能力。
    # 函数用途: 创建私有恢复投影与仅覆盖pending的查询索引。
    def __init__(self, root: Path, *, blocking: bool = True, deadline: float | None = None) -> None:
        self.root = Path(root)
        self.blocking, self.deadline = blocking, deadline
        self.path = self.root / "projection_state" / "recovery_pending.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    kind TEXT NOT NULL, request_id TEXT NOT NULL, version INTEGER NOT NULL,
                    pending INTEGER NOT NULL, stamp TEXT NOT NULL,
                    PRIMARY KEY(kind, request_id));
                CREATE INDEX IF NOT EXISTS recovery_pending ON entries(kind, request_id)
                    WHERE pending=1;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO meta VALUES ('sequence', '0');
            """)

    # LLM: 原文件锁序为canonical→投影；此连接中的审计只stat，不取canonical锁，不倒置锁序。
    # 函数用途: 返回短事务私有连接。
    def connect(self):
        return private_projection_connection(self.path, blocking=self.blocking, deadline=self.deadline)

    # LLM: 单个元数据值是游标提示，不是执行或结算裁决。
    # 函数用途: 在已持投影事务时读取持久位置。
    def _get(self, db, key: str, default: str = "") -> str:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # LLM: 不重写相同元数据，避免稳态轮询制造无效数据库写入。
    # 函数用途: 单点更新一个游标或维护时间。
    def _set(self, db, key: str, value) -> None:
        db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                   "WHERE meta.value!=excluded.value", (key, str(value)))

    # LLM: 序号在同一跨进程投影事务内增长，旧tick不能确认更晚的canonical变化。
    # 函数用途: 取得一个耐久单调变更序号。
    def _next(self, db) -> int:
        value = int(self._get(db, "sequence", "0")) + 1
        self._set(db, "sequence", value)
        return value

    # LLM: 调用者必须先持原canonical路径锁；登记成功后即使写入/进程失败，也保留可重读责任。
    # 函数用途: 在规范提交之前登记一次可重放变更。
    def begin(self, kind: str, request_id: str) -> int:
        request_id = projection_key(request_id)
        if kind not in {"terminal", "receipt"}:
            raise ValueError("invalid recovery projection kind")
        with self.connect() as db:
            version = self._next(db)
            db.execute("INSERT INTO entries VALUES (?,?,?,1,'') ON CONFLICT(kind,request_id) "
                       "DO UPDATE SET version=excluded.version,pending=1", (kind, request_id, version))
            return version

    # LLM: 审计观察只唤醒变化行；相同stat不能反复制造新版本而饿死原恢复页。
    # 函数用途: 登记一条新发现或结构版本变化的文件。
    def _observe(self, db, kind: str, request_id: str, stamp: str) -> tuple[int, int]:
        row = db.execute("SELECT version,pending,stamp FROM entries WHERE kind=? AND request_id=?",
                         (kind, request_id)).fetchone()
        if row is not None and row[2] == stamp:
            return row[0], row[1]
        version = self._next(db)
        db.execute("INSERT INTO entries VALUES (?,?,?,1,?) ON CONFLICT(kind,request_id) "
                   "DO UPDATE SET version=excluded.version,pending=1,stamp=excluded.stamp",
                   (kind, request_id, version, stamp))
        return version, 1

    # LLM: 新增前缀不重置cookie；每次128项及128个已知键；非法名字保留文件和有界诊断，不回滚合法发现。
    # 函数用途: 推进旧目录发现和已知文件的有界原位变化审计，返回本页审计键。
    def refresh(self, kind: str, directory: Path, *, force: bool = False) -> tuple[tuple[str, int, int], ...]:
        if kind not in {"terminal", "receipt"}:
            raise ValueError("invalid recovery projection kind")
        directory = Path(directory)
        with self.connect() as db:
            audit_key = f"audit_{kind}"
            after = self._get(db, audit_key)
            rows = db.execute("SELECT request_id FROM entries WHERE kind=? AND request_id>? "
                              "ORDER BY request_id LIMIT 128", (kind, after)).fetchall()
            if not rows and after:
                rows = db.execute("SELECT request_id FROM entries WHERE kind=? ORDER BY request_id LIMIT 128", (kind,)).fetchall()
            audited = []
            for (key,) in rows:
                key = projection_key(key)
                version, pending = self._observe(db, kind, key, projection_file_stamp(directory / f"{key}.json"))
                audited.append((key, version, pending))
            self._set(db, audit_key, rows[-1][0] if rows else "")
            try:
                info = directory.lstat()
            except FileNotFoundError:
                return tuple(audited)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("recovery directory must not be a link")
            identity = f"{info.st_dev}:{info.st_ino}"
            stamp = f"{identity}:{info.st_mtime_ns}:{info.st_ctime_ns}"
            cookie = self._get(db, f"cookie_{kind}")
            if self._get(db, f"identity_{kind}") != identity:
                cookie = ""
            due = time.time() >= float(self._get(db, f"scan_after_{kind}", "0"))
            if not cookie and not force and not due and self._get(db, f"directory_{kind}") == stamp:
                return tuple(audited)
            if not cookie:
                self._set(db, f"pass_{kind}", stamp)
            names, next_cookie, complete = directory_page(directory, cookie)
            invalid = []
            for name in names:
                if not name.endswith(".json"):
                    continue
                try:
                    key = projection_key(name[:-5])
                except ValueError:
                    invalid.append(name)
                    continue
                observed = projection_file_stamp(directory / name)
                if observed not in {"missing", "invalid"}:
                    self._observe(db, kind, key, observed)
            if invalid:
                self._set(db, f"invalid_{kind}", json.dumps({"count": len(invalid), "names": invalid[:8]}, ensure_ascii=False))
                _logger.warning("RECOVERY_DISCOVERY_INVALID_NAMES directory=%s names=%r", directory, invalid[:8])
            self._set(db, f"identity_{kind}", identity)
            self._set(db, f"cookie_{kind}", "" if complete else next_cookie)
            if complete:
                self._set(db, f"directory_{kind}", self._get(db, f"pass_{kind}", stamp))
                self._set(db, f"scan_after_{kind}", time.time() + 3600)
            return tuple(audited)

    # LLM: 候选只取pending部分索引并按持久调用方位置轮转；坏行或不断插入的新前缀不能重置旧积压。
    # 函数用途: 取得至多128个待核键及其确切版本。
    def page(self, kind: str, *, after: str = "", limit: int = 128) -> tuple[tuple[str, int], ...]:
        count = max(0, min(128, int(limit)))
        with self.connect() as db:
            rows = db.execute("SELECT request_id,version FROM entries WHERE kind=? AND pending=1 "
                              "AND request_id>? ORDER BY request_id LIMIT ?", (kind, after, count)).fetchall()
            if len(rows) < count and after:
                rows += db.execute("SELECT request_id,version FROM entries WHERE kind=? AND pending=1 "
                                   "AND request_id<=? ORDER BY request_id LIMIT ?",
                                   (kind, after, count-len(rows))).fetchall()
            return tuple((projection_key(key), version) for key, version in rows)

    # LLM: 调用方在canonical原锁内已复核当前权威；只确认领取版本，不能清掉tick期间更晚的写前登记。
    # 函数用途: CAS确认单条恢复投影，保留并发提交的新责任。
    def acknowledge(self, kind: str, request_id: str, version: int, stamp: str) -> bool:
        with self.connect() as db:
            result = db.execute("UPDATE entries SET pending=0,stamp=? WHERE kind=? AND request_id=? AND version=?",
                                (stamp, kind, projection_key(request_id), version))
            return result.rowcount == 1

    # LLM: 仅用于原路径锁内已确认不存在的松散规范文件，不删除任何历史或封存账；新版本CAS不匹配则保留。
    # 函数用途: 去掉已经不存在文件的调度引用。
    def forget(self, kind: str, request_id: str, version: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM entries WHERE kind=? AND request_id=? AND version=?",
                       (kind, projection_key(request_id), version))

    # LLM: 标记/答复损坏只唤醒已知键；不构造终态、不发业务IO、不清其它待办。
    # 函数用途: 将已知完成投影重新排入有界恢复队列。
    def wake(self, kind: str, request_id: str, version: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE entries SET pending=1 WHERE kind=? AND request_id=? AND version=?",
                       (kind, projection_key(request_id), version))

    # LLM: 追平只表示当前已知pending为空且发现页已到EOF，不是业务全部成功的证明。
    # 函数用途: 返回本根本类发现与恢复队列的当前排空提示。
    def caught_up(self, kind: str) -> bool:
        with self.connect() as db:
            return (not self._get(db, f"cookie_{kind}")
                    and db.execute("SELECT 1 FROM entries WHERE kind=? AND pending=1 LIMIT 1", (kind,)).fetchone() is None)


# LLM: 只认识Gateway既有两种canonical布局；不追逐marker/答复/state等派生JSON，也不扩大原写者权限。
# 函数用途: 为原JSON writer登记一次可恢复的写前变更。
def record_canonical_change(path: Path, *, blocking: bool = True, deadline: float | None = None) -> None:
    path = Path(path)
    if path.suffix != ".json":
        return
    if path.parent.name == "input_receipts":
        root, kind = path.parent.parent, "receipt"
    elif path.parent.name == "terminal" and path.parent.parent.name == "requests":
        root, kind = path.parents[2], "terminal"
    else:
        return
    RecoveryPendingIndex(root, blocking=blocking, deadline=deadline).begin(kind, path.stem)
