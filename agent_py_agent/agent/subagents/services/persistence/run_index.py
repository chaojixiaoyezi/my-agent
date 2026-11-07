"""子代理容量及请求谱系的增量查找投影；规范状态仍由原文件写者负责。"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat
from contextlib import ExitStack, contextmanager
from itertools import islice
from pathlib import Path
from sqlite3 import OperationalError
from types import SimpleNamespace

from ....common import nofollow_fs
from ....common.json_io import locked_json_path
from ....common.nofollow_fs import (
    NoFollowPathError,
    open_directory_beneath,
    open_private_lock_beneath,
    open_readonly_file_beneath,
)
from ....common.opaque_id import validate_opaque_id
from ....common.value_parsing import text_value
from ....conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from ...models import (
    SUBAGENT_ENDED_STATUSES,
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    SUBAGENT_REUSABLE_STATUSES,
    task_status_in,
)
from ..agent_run_state import canonical_state_path_from_payload
from ..contract_identity import (
    idempotency_contract_identity_from_context_packs,
    repair_contract_identity_from_context_packs,
)

logger = logging.getLogger(__name__)


# LLM: 新索引只登记规范写者提交的标量；pending 先于文件发表耐久提交，未知覆盖不得当作零占用。
# 函数用途: 为容量、派工身份和请求定位保存按行索引，不在热查询中枚举历史文件。
class SubagentRunIndex:
    # LLM: 初始化仅采样八项；旧投影版本必须重核 canonical，库存完成前所有热查询 fail closed。
    # 函数用途: 初始化私有查找库及迁移状态；每次连接均在词法作用域内关闭。
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.path = self.workspace / ".run-index.sqlite3"
        with locked_json_path(self.workspace / ".run-index-init.guard"):
            with self.connect() as db:
                self._install(db)

    # LLM: 只复用原DDL和八项旧源采样，原始运行目录保留且partial不得冒充完整覆盖。
    # 函数用途: 在当前连接重装查找表与原覆盖元数据，不同步扫描历史。
    def _install(self, db):
        db.executescript("""
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY, active INTEGER NOT NULL,
                        audit_epoch INTEGER, updated REAL NOT NULL, pending INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS runs_active ON runs(active, run_id);
                    CREATE INDEX IF NOT EXISTS runs_pending ON runs(pending, run_id);
                    CREATE TABLE IF NOT EXISTS membership (
                        scope_id TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT NOT NULL,
                        PRIMARY KEY(scope_id, kind, run_id)
                    );
                    CREATE INDEX IF NOT EXISTS membership_run ON membership(run_id);
                    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS run_lookup (
                        run_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, cover_open INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS lookup_parent_cover ON run_lookup(parent_id,cover_open,run_id);
                    CREATE TABLE IF NOT EXISTS system_names (
                        run_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, depth INTEGER NOT NULL,
                        ordinal TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS system_parent_max
                        ON system_names(parent_id,depth,length(ordinal) DESC,ordinal DESC);
                    CREATE TABLE IF NOT EXISTS reusable (
                        kind TEXT NOT NULL, identity TEXT NOT NULL, parent_id TEXT NOT NULL,
                        root_id TEXT NOT NULL, role TEXT NOT NULL, write_roots TEXT NOT NULL,
                        display_identity TEXT NOT NULL, run_id TEXT NOT NULL, updated REAL NOT NULL,
                        PRIMARY KEY(kind,run_id)
                    );
                    CREATE INDEX IF NOT EXISTS reuse_exact
                        ON reusable(kind,identity,parent_id,write_roots,root_id,role,display_identity,updated,run_id);
                    CREATE INDEX IF NOT EXISTS reuse_order
                        ON reusable(kind,identity,parent_id,write_roots,updated ASC,run_id DESC);
                    CREATE TABLE IF NOT EXISTS inventory (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE,
                        done INTEGER NOT NULL DEFAULT 0, error_type TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS inventory_remaining ON inventory(done,seq);
                    CREATE INDEX IF NOT EXISTS inventory_skipped ON inventory(seq) WHERE done=1 AND error_type!='';
                    CREATE TABLE IF NOT EXISTS pending_commits (
                        run_id TEXT PRIMARY KEY, recovery TEXT NOT NULL, was_published INTEGER NOT NULL
                    );
        """)
        if db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() != (2,):
            with os.scandir(self.workspace) as entries:
                sampled = list(islice(entries, 8))
            legacy = (bool(db.execute("SELECT 1 FROM runs LIMIT 1").fetchone())
                      or len(sampled) == 8 or any(entry.is_dir(follow_symlinks=False) or entry.is_symlink() for entry in sampled))
            db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                           [("complete", int(not legacy)), ("offset", 0),
                            ("inventory_ready", int(not legacy)), ("schema", 2)])

    # LLM: 每连接只点查schema键；缺表或未完成种子复用原安装器，异常不能伪装成空库存。
    # 函数用途: 补未初始化的表及元数据，返回是否实际重装，由调用者复核身份后提交。
    def _ensure_schema(self, db):
        try:
            if db.execute("SELECT 1 FROM meta WHERE key='schema'").fetchone():
                return False
        except OperationalError as exc:
            if str(exc) != "no such table: meta":
                raise
        self._install(db)
        return True

    # LLM: no-follow种子和访问锁保持；重装提交先于业务BEGIN，业务回滚不能撤销覆盖种子。
    # 函数用途: 为一次查询或固定批次提供私有短连接，先关 SQLite 再关预检 FD，异常完整回滚。
    @contextmanager
    def connect(self):
        with locked_json_path(self.workspace / ".run-index-access.guard"):
            if not nofollow_fs._supports_dir_fd():
                with self._connect_portable() as db:
                    yield db
                return
            with ExitStack() as resources:
                root_fd = open_directory_beneath(self.workspace, (), require_dir_fd=True)
                resources.callback(os.close, root_fd)
                pinned = []
                for suffix in ("-journal", "-wal", "-shm", ""):
                    path = Path(str(self.path) + suffix)
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        if suffix:
                            continue
                    descriptor = open_private_lock_beneath(self.workspace, (path.name,))
                    resources.callback(os.close, descriptor)
                    if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
                        os.fchmod(descriptor, 0o600)
                    pinned.append((path, descriptor))
                _verify_sqlite_files(self.workspace, root_fd, pinned)
                db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=5)
                try:
                    _verify_sqlite_files(self.workspace, root_fd, pinned)
                    with db:
                        if self._ensure_schema(db):
                            _verify_sqlite_files(self.workspace, root_fd, pinned)
                            db.commit()
                            logger.warning("SQLITE_SCHEMA_REINSTALLED store=subagent_run_index path=%s", self.path)
                        yield db
                        _verify_sqlite_files(self.workspace, root_fd, pinned)
                finally:
                    db.close()

    # LLM: 便携分支沿原访问锁和路径预检；初始化先提交，种子FD先关再开SQLite，不外推POSIX竞态强度。
    # 函数用途: 在无dir_fd平台提供真实提交、回滚及确定性关闭的短连接。
    @contextmanager
    def _connect_portable(self):
        root_info = nofollow_fs._portable_directory(self.workspace, (), create=False).lstat()
        pinned = []
        for suffix in ("-journal", "-wal", "-shm", ""):
            path = Path(str(self.path) + suffix)
            try:
                path.lstat()
            except FileNotFoundError:
                if suffix:
                    continue
            descriptor = open_private_lock_beneath(self.workspace, (path.name,))
            try:
                if hasattr(os, "fchmod") and stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
                    os.fchmod(descriptor, 0o600)
                pinned.append((path, os.fstat(descriptor)))
            finally:
                os.close(descriptor)
        _verify_sqlite_files(self.workspace, root_info, pinned)
        db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            _verify_sqlite_files(self.workspace, root_info, pinned)
            with db:
                if self._ensure_schema(db):
                    _verify_sqlite_files(self.workspace, root_info, pinned)
                    db.commit()
                    logger.warning("SQLITE_SCHEMA_REINSTALLED store=subagent_run_index path=%s", self.path)
                yield db
                _verify_sqlite_files(self.workspace, root_info, pinned)
        finally:
            db.close()

    # LLM: 每次规范提交先登记 pending；旧 pending 无恢复凭据仍视为可能已发表，不能重试后降格为未发表。
    # 函数用途: 在规范文件写入前留下精确 run 未结记录及首存没有 locator 时的恢复定位字段。
    def mark_pending(self, run_id: str, *, task=None) -> None:
        run_id = validate_opaque_id(run_id, kind="run_id")
        recovery, published = None, False
        if task is not None:
            if task.id != run_id:
                raise ValueError("pending canonical identity mismatch")
            recovery = {"id": run_id, "created_at": task.created_at, "task_dir": task.task_dir,
                        "task_workspace_dir": task.task_workspace_dir,
                        "agent_run_workspace_dir": task.agent_run_workspace_dir}
            canonical = canonical_state_path_from_payload(recovery)
            if canonical is None:
                raise ValueError("pending canonical path unavailable")
            published = canonical.exists() or (self.workspace / run_id / "task.json").exists()
        with self.connect() as db:
            if recovery is not None:
                # 旧 committed 或没有恢复凭据的未知 pending 都不可因文件缺失而删除占位。
                published = published or bool(db.execute(
                    "SELECT 1 FROM runs r WHERE r.run_id=? AND (r.pending=0 OR EXISTS "
                    "(SELECT 1 FROM run_lookup l WHERE l.run_id=r.run_id) OR NOT EXISTS "
                    "(SELECT 1 FROM pending_commits p WHERE p.run_id=r.run_id))", (run_id,)).fetchone())
                db.execute("INSERT INTO pending_commits VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                           "recovery=excluded.recovery,was_published=max(was_published,excluded.was_published)",
                           (run_id, _identity_key(recovery), int(published)))
            db.execute("INSERT INTO runs(run_id,active,audit_epoch,updated,pending) VALUES (?,1,NULL,0,1) "
                       "ON CONFLICT(run_id) DO UPDATE SET pending=1", (run_id,))

    # LLM: 调用方持有原 canonical guard；可复用迁移批次连接，但不接受模型提供的投影。
    # 函数用途: 规范状态发表后原子更新计数、谱系、系统展示编号及三种精确复用身份。
    def upsert(self, task, *, connection=None) -> None:
        if connection is None:
            with self.connect() as db:
                self.upsert(task, connection=db)
            return
        run_id = validate_opaque_id(task.id, kind="run_id")
        attrs = task.attributes if isinstance(task.attributes, dict) else {}
        try:
            epoch = max(0, int(attrs.get("audit_run_epoch") or 0))
        except (TypeError, ValueError):
            epoch = None
        active = int(not task_status_in(task.status, SUBAGENT_ENDED_STATUSES))
        connection.execute("INSERT INTO runs VALUES (?,?,?,?,0) ON CONFLICT(run_id) DO UPDATE SET "
                           "active=excluded.active,audit_epoch=excluded.audit_epoch,updated=excluded.updated,pending=0",
                           (run_id, active, epoch, task.updated_at or task.created_at or 0))
        connection.execute("DELETE FROM membership WHERE run_id=?", (run_id,))
        lineage = {run_id, str(task.root_id or "").strip(), str(task.parent_id or "").strip(),
                   str(attrs.get("conversation_task_id") or "").strip()}
        rows = [(key, "lineage", run_id) for key in lineage if key]
        request = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
        if request:
            rows.append((request, "request", run_id))
        connection.executemany("INSERT INTO membership VALUES (?,?,?)", rows)
        parent = text_value(task.parent_id)
        cover_open = int(not task_status_in(task.status, SUBAGENT_RECOVERY_CLOSED_STATUSES))
        connection.execute("INSERT INTO run_lookup VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                           "parent_id=excluded.parent_id,cover_open=excluded.cover_open", (run_id, parent, cover_open))
        connection.execute("DELETE FROM system_names WHERE run_id=?", (run_id,))
        match = re.fullmatch(r"agent-d(0|[1-9][0-9]*)-.+-([1-9][0-9]*)", text_value(task.agent_name))
        if match:
            connection.execute("INSERT INTO system_names VALUES (?,?,?,?)",
                               (run_id, parent, int(match[1]), match[2]))
        connection.execute("DELETE FROM reusable WHERE run_id=?", (run_id,))
        if task_status_in(task.status, SUBAGENT_REUSABLE_STATUSES):
            identities = {"work": text_value(attrs.get("work_scope_key")),
                          "repair": repair_contract_identity_from_context_packs(task.context_packs),
                          "idempotency": idempotency_contract_identity_from_context_packs(task.context_packs)}
            roots = _write_roots_key(task.allowed_write_roots, exclude=task.task_dir)
            for kind, identity in identities.items():
                if identity:
                    connection.execute("INSERT INTO reusable VALUES (?,?,?,?,?,?,?,?,?)",
                                       (kind, _identity_key(identity), parent, text_value(task.root_id),
                                        text_value(task.role), roots, _display_identity(task.agent_name),
                                        run_id, task.updated_at or task.created_at or 0))
        connection.execute("DELETE FROM pending_commits WHERE run_id=?", (run_id,))

    # LLM: 生产 prepare/init 接线负责调度维护；当前读只核覆盖，不同步库存或列表回退。
    # 函数用途: 阻止部分迁移或中断提交被当作健康容量。
    def _require_complete(self, db) -> None:
        partial = not db.execute("SELECT value FROM meta WHERE key='complete'").fetchone()[0]
        pending = bool(db.execute("SELECT 1 FROM runs WHERE pending=1 LIMIT 1").fetchone())
        if partial or pending:
            reason = "coverage is partial" if partial else "has pending canonical commits"
            raise RuntimeError(f"subagent run index {reason}")

    # LLM: 同一连接读取覆盖状态，不为观察触发迁移；pending 只提供数量不打印任务正文。
    # 函数用途: 返回显式迁移覆盖及未结提交事实。
    def coverage(self) -> dict[str, object]:
        with self.connect() as db:
            meta = dict(db.execute("SELECT key,value FROM meta"))
            pending = db.execute("SELECT count(*) FROM runs WHERE pending=1").fetchone()[0]
            remaining = db.execute("SELECT count(*) FROM inventory WHERE done=0").fetchone()[0]
            skipped = db.execute("SELECT count(*) FROM inventory WHERE done=1 AND error_type!=''").fetchone()[0]
        return {"complete": bool(meta["complete"]), "offset": meta["offset"], "pending": pending,
                "inventory_ready": bool(meta["inventory_ready"]), "remaining": remaining, "skipped": skipped}

    # LLM: 参数全来自宿主结构化身份；UNION 消除多个谱系键命中的同一 run。
    # 函数用途: 生成同时供请求列表与容量计数使用的唯一谱系选择器。
    def _selection(self, scope_ids, request_key, epoch):
        scopes = sorted(scope_ids)
        placeholders = ",".join("?" for _ in scopes)
        sql = ("SELECT r.run_id FROM runs r JOIN (SELECT run_id FROM membership WHERE kind='lineage' "
               f"AND scope_id IN ({placeholders}) UNION SELECT run_id FROM membership "
               "WHERE kind='request' AND scope_id=?) m ON m.run_id=r.run_id")
        params = [*scopes, request_key]
        if epoch is not None:
            sql += " WHERE r.audit_epoch=?"
            params.append(epoch)
        return sql, params

    # LLM: 容量从活跃索引出发，对每个活跃 run 点查成员关系；不能先 UNION 同请求的全部终态历史。
    # 函数用途: 在一个读事务中返回 owner 与当前请求树的活跃数量。
    def active_counts(self, scope_ids=(), request_key="", epoch=None) -> tuple[int, int]:
        with self.connect() as db:
            db.execute("BEGIN")
            self._require_complete(db)
            total = db.execute("SELECT count(*) FROM runs WHERE active=1").fetchone()[0]
            if not scope_ids:
                return total, 0
            scopes = sorted(scope_ids)
            placeholders = ",".join("?" for _ in scopes)
            sql = ("SELECT count(*) FROM runs r INDEXED BY runs_active WHERE r.active=1 AND EXISTS "
                   "(SELECT 1 FROM membership m WHERE m.run_id=r.run_id AND "
                   f"((m.kind='lineage' AND m.scope_id IN ({placeholders})) "
                   "OR (m.kind='request' AND m.scope_id=?)))")
            params = [*scopes, request_key]
            if epoch is not None:
                sql += " AND r.audit_epoch=?"
                params.append(epoch)
            selected = db.execute(sql, params).fetchone()[0]
            return total, selected

    # LLM: 历史仅选精确 id；调用方仍逐 id 读 canonical 并应用原末次身份校验。
    # 函数用途: 返回指定请求或根任务的候选 id，不枚举其它树的历史。
    def request_ids(self, scope_ids, request_key, epoch) -> list[str]:
        with self.connect() as db:
            db.execute("BEGIN")
            self._require_complete(db)
            sql, params = self._selection(scope_ids, request_key, epoch)
            return [row[0] for row in db.execute(sql + " ORDER BY r.updated DESC,r.run_id", params)]

    # LLM: 编号来自展示前缀而非 task.depth，含所有状态；按十进制长度排序避免 SQLite 整数溢出。
    # 函数用途: 查询同一父级指定展示深度的历史最大系统编号加一。
    def next_system_index(self, parent_id: str, depth: int) -> int:
        with self.connect() as db:
            db.execute("BEGIN")
            self._require_complete(db)
            row = db.execute("SELECT ordinal FROM system_names WHERE parent_id=? AND depth=? "
                             "ORDER BY length(ordinal) DESC,ordinal DESC LIMIT 1",
                             (text_value(parent_id), int(depth))).fetchone()
            return int(row[0]) + 1 if row else 1

    # LLM: 存在性只针对调用方明确给定的 ID；分块参数不是全历史 SELECT，不为 miss 读取 task 文件。
    # 函数用途: 返回给定候选中已登记的精确 run_id 集合。
    def known_ids(self, candidate_ids) -> set[str]:
        candidates = list(dict.fromkeys(text_value(item) for item in candidate_ids if text_value(item)))
        with self.connect() as db:
            db.execute("BEGIN")
            self._require_complete(db)
            known = set()
            for start in range(0, len(candidates), 256):
                batch = candidates[start:start + 256]
                slots = ",".join("?" for _ in batch)
                known.update(row[0] for row in db.execute(f"SELECT run_id FROM runs WHERE run_id IN ({slots})", batch))
            return known

    # LLM: covers 的 closed 唯一集合是 DONE 与 handled terminal；FAILED/BLOCKED 等仍占原 Todo，不能套容量 ended。
    # 函数用途: 按原列表顺序挑出同一父级仍未收口的直属孩子供 canonical 复核。
    def active_child_ids(self, parent_id: str) -> list[str]:
        with self.connect() as db:
            db.execute("BEGIN")
            self._require_complete(db)
            return [row[0] for row in db.execute(
                "SELECT l.run_id FROM run_lookup l JOIN runs r ON r.run_id=l.run_id "
                "WHERE l.parent_id=? AND l.cover_open=1 ORDER BY r.updated DESC,l.run_id",
                (text_value(parent_id),))]

    # LLM: 这里只比较持久化身份等值，不计算或扩大权限；合约解析复用原 helper，最终 _same_* 仍由调用方复核。
    # 函数用途: 精确检索三种复用候选，保留原 reversed(list_runs) 顺序及 root/role wildcard 语义。
    def reusable_ids(self, params, kind, identity) -> list[str]:
        if kind not in {"work", "repair", "idempotency"}:
            raise ValueError("unsupported reusable index kind")
        sql = "SELECT run_id FROM reusable WHERE kind=? AND identity=? AND parent_id=? AND write_roots=?"
        values = [kind, _identity_key(identity), text_value(params.parent_id), _write_roots_key(params.extra_write_roots)]
        if text_value(params.root_id):
            sql += " AND root_id=?"
            values.append(text_value(params.root_id))
        if text_value(params.role):
            sql += " AND role IN ('',?)"
            values.append(text_value(params.role))
        if kind == "idempotency":
            sql += " AND display_identity=?"
            values.append(_display_identity(params.agent_name))
        sql += " ORDER BY updated ASC,run_id DESC LIMIT 65"
        with self.connect() as db:
            db.execute("BEGIN")
            self._require_complete(db)
            candidates = [row[0] for row in db.execute(sql, values)]
        if len(candidates) > 64:
            raise RuntimeError("subagent reusable candidate budget exceeded")
        return candidates

    # LLM: 库存取旧 SQL 身份与目录发现并集；缺 locator 的已知身份不能消失。UNIQUE 保留中断前 seq。
    # 函数用途: 在后台维护车道建立耐久 SQL 库存，内存和每次 SQL 写入均为固定页。
    def _inventory(self) -> None:
        if self.coverage()["inventory_ready"]:
            return
        with self.connect() as db:
            # SQL 游标固定源快照，每次只取一页；只写 inventory，不改游标所读的 runs。
            existing = db.execute("SELECT run_id FROM runs ORDER BY run_id")
            for batch in iter(lambda: existing.fetchmany(64), []):
                db.executemany("INSERT OR IGNORE INTO inventory(run_id) VALUES (?)", batch)
        page = []
        with os.scandir(self.workspace) as entries:
            for entry in entries:
                if entry.is_symlink() or (entry.is_dir(follow_symlinks=False) and (Path(entry.path) / "task.json").is_file()):
                    page.append((entry.name,))
                if len(page) == 64:
                    with self.connect() as db:
                        db.executemany("INSERT OR IGNORE INTO inventory(run_id) VALUES (?)", page)
                    page.clear()
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO inventory(run_id) VALUES (?)", page)
            db.execute("UPDATE meta SET value=1 WHERE key='inventory_ready'")

    # LLM: 库存一次枚举与固定 canonical 批次明确分开；服务只在显式维护/后台调用，不由热 miss 同步执行。
    # 函数用途: 通过稳定 SQL seq 续迁移，逐 ID 在原 canonical guard 内读取；确定缺失的规范记录结项排除，其余坏行保留 partial。
    def backfill(self, loader, *, batch_size=64) -> dict[str, object]:
        from .service import _canonical_state_guard_path

        size = min(256, max(1, int(batch_size)))
        with locked_json_path(self.workspace / ".run-index-maintenance.guard"):
            state = self.coverage()
            if state["complete"]:
                return {**state, "loaded": 0, "errors": []}
            self._inventory()
            with self.connect() as db:
                batch = list(db.execute("SELECT seq,run_id FROM inventory WHERE done=0 ORDER BY seq LIMIT ?", (size,)))
            loaded, errors = 0, []
            for seq, run_id in batch:
                try:
                    validate_opaque_id(run_id, kind="run_id")
                    if not nofollow_fs._supports_dir_fd():
                        self._backfill_portable(loader, run_id, seq)
                        loaded += 1
                        continue
                    # 拒绝旧 run/locator 链接先于创建 guard；完整 loader 仍复用原 canonical reader。
                    directory_fd = open_directory_beneath(self.workspace, (run_id,), require_dir_fd=True)
                    try:
                        with locked_json_path(_canonical_state_guard_path(self.workspace, SimpleNamespace(id=run_id))):
                            os.close(open_readonly_file_beneath(self.workspace, (run_id, "task.json")))
                            task = loader(run_id)
                            directory = (self.workspace / run_id).lstat()
                            opened = os.fstat(directory_fd)
                            if (not stat.S_ISDIR(directory.st_mode)
                                    or (directory.st_dev, directory.st_ino) != (opened.st_dev, opened.st_ino)):
                                raise InventoryCanonicalMismatch("inventory canonical directory changed")
                            if task.id != run_id:
                                raise InventoryCanonicalMismatch("inventory canonical identity mismatch")
                            with self.connect() as db:
                                self.upsert(task, connection=db)
                                db.execute("UPDATE inventory SET done=1,error_type='' WHERE seq=?", (seq,))
                    finally:
                        os.close(directory_fd)
                    loaded += 1
                except Exception as exc:  # noqa: BLE001 确定的规范缺失结项排除；其余错误行保留 partial 待下一轮
                    # 同锚点 list_runs 跳过坏行：不登记 runs 行、不计容量，库存结项并保留错误类型；瞬态打开竞态、权限、I/O 与解析错误仍重试。
                    skipped = (isinstance(exc, (FileNotFoundError, NotADirectoryError, InventoryCanonicalMismatch))
                               or isinstance(exc, NoFollowPathError) and not exc.transient_open_race)
                    if not skipped:
                        errors.append({"run_id": run_id, "error_type": type(exc).__name__})
                    with self.connect() as db:
                        db.execute("UPDATE inventory SET done=?,error_type=? WHERE seq=?",
                                   (int(skipped), type(exc).__name__, seq))
            with self.connect() as db:
                first_pending = db.execute("SELECT min(seq) FROM inventory WHERE done=0").fetchone()[0]
                offset = (first_pending - 1 if first_pending is not None
                          else db.execute("SELECT coalesce(max(seq),0) FROM inventory").fetchone()[0])
                db.execute("UPDATE meta SET value=? WHERE key='offset'", (offset,))
                db.execute("UPDATE meta SET value=? WHERE key='complete'", (int(first_pending is None),))
            return {**self.coverage(), "loaded": loaded, "errors": errors}

    # LLM: 只在原canonical guard内加载指定库存行；根及run目录重检，不因缺目录FD跳过身份或类型核验。
    # 函数用途: 让便携平台以同一事务和完成标记迁移一条真实规范记录。
    def _backfill_portable(self, loader, run_id, seq):
        from .service import _canonical_state_guard_path

        directory = nofollow_fs._portable_directory(self.workspace, (run_id,), create=False)
        before = directory.lstat()
        with locked_json_path(_canonical_state_guard_path(self.workspace, SimpleNamespace(id=run_id))):
            source = (directory / "task.json").lstat()
            if (not stat.S_ISREG(source.st_mode) or source.st_nlink != 1
                    or getattr(source, "st_file_attributes", 0) & 0x400):
                raise NoFollowPathError("inventory canonical file is not regular")
            task = loader(run_id)
            current = nofollow_fs._portable_directory(self.workspace, (run_id,), create=False).lstat()
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise InventoryCanonicalMismatch("inventory canonical directory changed")
            if task.id != run_id:
                raise InventoryCanonicalMismatch("inventory canonical identity mismatch")
            with self.connect() as db:
                self.upsert(task, connection=db)
                db.execute("UPDATE inventory SET done=1,error_type='' WHERE seq=?", (seq,))

    # LLM: 恢复库存只按 pending 索引定位，大小恒有界；JSON 中只有宿主先写的定位字段，不是权威业务状态。
    # 函数用途: 读取一批精确 pending 记录供服务在 canonical guard 内修复。
    def pending_rows(self, *, batch_size=64):
        with self.connect() as db:
            return list(db.execute("SELECT r.run_id,p.recovery,p.was_published FROM runs r "
                                   "LEFT JOIN pending_commits p ON p.run_id=r.run_id WHERE r.pending=1 "
                                   "ORDER BY r.run_id LIMIT ?", (min(256, max(1, int(batch_size))),)))

    # LLM: 批次选择后可能已有规范写者完成；canonical 锁内按同一 ID 再核当前 pending，不重放旧快照。
    # 函数用途: 精确读取一个仍未结的提交定位记录。
    def pending_record(self, run_id):
        with self.connect() as db:
            return db.execute("SELECT r.run_id,p.recovery,p.was_published FROM runs r "
                              "LEFT JOIN pending_commits p ON p.run_id=r.run_id "
                              "WHERE r.pending=1 AND r.run_id=?", (run_id,)).fetchone()

    # LLM: 仅服务在 canonical 锁内确认首存未发表后可调用；已有提交投影绝不因缺文件被删除。
    # 函数用途: 清除一个已证明没有发表过的 pending 首存占位。
    def discard_unpublished(self, run_id: str) -> None:
        with self.connect() as db:
            if db.execute("SELECT 1 FROM run_lookup WHERE run_id=?", (run_id,)).fetchone():
                raise ValueError("published run cannot be discarded")
            db.execute("DELETE FROM membership WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM runs WHERE run_id=? AND pending=1", (run_id,))
            db.execute("DELETE FROM pending_commits WHERE run_id=?", (run_id,))


# LLM: 仅迁移循环在 canonical guard 内确认目录或身份已不对应库存行时抛出；按类型归为确定不可用，不凭文案判断。
# 类用途: 标记一条库存行的规范记录已不属于该 run 身份。
class InventoryCanonicalMismatch(ValueError):
    pass


# LLM: SQLite 连接前后复核原 no-follow FD 对应的路径；不存在的 sidecar 也必须在出现时保持普通私有单链接。
# 函数用途: 拒绝索引文件、目录或 sidecar 被换成链接/其它身份，不放宽已有目录权限。
def _verify_sqlite_files(workspace, root_fd, pinned):
    if not nofollow_fs._supports_dir_fd():
        _verify_portable_sqlite_files(workspace, root_fd, pinned)
        return
    current = workspace.lstat()
    opened = os.fstat(root_fd)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise OSError("private SQLite directory identity changed")
    main = pinned[-1][0]
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = Path(str(main) + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if suffix:
                continue
            raise
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise OSError("SQLite index must be a private regular single-link file")
    for path, descriptor in pinned:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if path != main:
                continue  # SQLite 正常提交可删除已预检 journal；不复用其 FD。
            raise
        opened = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("private SQLite file identity changed")


# LLM: 无FD平台只比较同源路径身份，仍拒绝目录/链接/reparse/多链接；没有fchmod时不套POSIX权限位。
# 函数用途: 在SQLite连接和提交前复核便携路径，不持有阻碍Windows删除journal的文件句柄。
def _verify_portable_sqlite_files(workspace, root_info, pinned):
    current = nofollow_fs._portable_directory(workspace, (), create=False).lstat()
    if ((current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino)
            or getattr(current, "st_file_attributes", 0) & 0x400):
        raise OSError("private SQLite directory identity changed")
    main, identities = pinned[-1][0], dict(pinned)
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = Path(str(main) + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if suffix:
                continue
            raise
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
                or hasattr(os, "fchmod") and stat.S_IMODE(info.st_mode) & 0o077):
            raise OSError("SQLite index must be a private regular single-link file")
        opened = identities.get(path)
        if opened is not None and (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("private SQLite file identity changed")


# LLM: 身份序列化不计算权限，仅使共享 helper 产生的元组和路径等值可被 SQL 精确比较。
# 函数用途: 为结构化查找键生成无歧义的稳定标量。
def _identity_key(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


# LLM: 只复刻原 _same_* 的字符串等值格式，不展开路径、不推导父根、不生成或继承授权。
# 函数用途: 规范化原允许写根列表并排除任务自身内部目录，供索引比较身份。
def _write_roots_key(values, *, exclude="") -> str:
    internal = text_value(exclude)
    internal = internal if internal == "/" else internal.rstrip("/")
    roots = set()
    for value in values or []:
        text = text_value(value)
        root = text if text == "/" else text.rstrip("/")
        if root and root != internal:
            roots.add(root)
    return _identity_key(sorted(roots))


# LLM: 原幂等展示语义只把编号通用角色归同一类；用户自定义名仍按 casefold 精确等值。
# 函数用途: 为幂等查找生成不混淆自定义名的带类型展示身份。
def _display_identity(value) -> str:
    name = text_value(value).casefold()
    match = re.fullmatch(r"agent-d\d+-([a-z0-9_-]+)-\d+", name)
    generic_roles = {"worker", "general", "researcher", "writer", "tester", "bug-finder", "coordinator", "leaf-worker"}
    return _identity_key(["system"] if match and match[1] in generic_roles else ["name", name])


# LLM: 唯一宿主请求范围解析器，保留原 request/task/run 关联和原 Audit 纪元读口，不复制权限逻辑。
# 函数用途: 为容量与请求谱系查询构造同一范围。
def request_selection(agent: object, request_id: str):
    from ....core import _active_audit_run_epoch

    key = str(request_id or "").strip()
    scope_ids = {key}
    current = getattr(agent, "_current_run_params", None)
    if str(getattr(current, "request_id", "") or "").strip() == key:
        attrs = getattr(current, "task_attributes", None)
        if isinstance(attrs, dict):
            scope_ids.add(str(attrs.get("conversation_task_id") or "").strip())
        scope_ids.add(str(getattr(current, "run_id", "") or "").strip())
        scope_ids.add(str(getattr(current, "task_id", "") or "").strip())
    scope_ids.discard("")
    return scope_ids, key, _active_audit_run_epoch(agent, key)
