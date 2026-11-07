"""按行能力登记及协作待回应读投影；case/请求/证据原文件仍是权威。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from sqlite3 import OperationalError

from ..common import nofollow_fs
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    open_private_lock_beneath,
    open_readonly_file_beneath,
)
from .identity import agent_identity_keys
from .models import AGENT_CAPABILITY_STATUS_AVAILABLE, AgentCapability
from .request_status import is_blocked_request_status, is_declined_request_status

logger = logging.getLogger(__name__)


# LLM: 新能力登记只在此按 agent_id 原子 upsert；历史身份永久保留，活跃投影及 pending 路由独立维护。
# 类用途: 提供私有 SQLite 行存储、稳定迁移和单连接按目标分页查询。
class CollaborationHistoryIndex:
    PAGE_SIZE = 64
    MAX_LEGACY_ITEM_CHARS = 1024 * 1024

    # LLM: 构造不读整份旧名册或 case；旧 case 只做常数采样，库存由生产后台接线处理。
    # 函数用途: 建立私有库及显式覆盖元数据，不改变旧账本原件。
    def __init__(self, root):
        self.root = Path(root)
        ensure_private_dir(self.root)
        self.path = self.root / ".history-index.sqlite3"
        with locked_json_path(self.root / ".history-init"):
            with self.connect() as db:
                self._install(db)

    # LLM: 原case和名册不动，缺cases目录视为无旧case；尚有来源时保持partial并沿原后台回填。
    # 函数用途: 在当前连接安装原投影表和覆盖标记。
    def _install(self, db):
        db.executescript("""
                    CREATE TABLE IF NOT EXISTS agents (
                        agent_id TEXT PRIMARY KEY, payload TEXT NOT NULL, source TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS active_agents (
                        agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
                        load REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS active_agent_order ON active_agents(load,agent_id);
                    CREATE TABLE IF NOT EXISTS agent_features (
                        agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
                        feature TEXT NOT NULL, PRIMARY KEY(agent_id,feature)
                    );
                    CREATE INDEX IF NOT EXISTS agent_feature_lookup ON agent_features(feature,agent_id);
                    CREATE TABLE IF NOT EXISTS cases (
                        case_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS cases_status ON cases(status,case_id);
                    CREATE TABLE IF NOT EXISTS requests (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, request_id TEXT NOT NULL,
                        payload TEXT NOT NULL, eligible INTEGER NOT NULL, priority TEXT NOT NULL, created REAL NOT NULL,
                        UNIQUE(case_id,request_id)
                    );
                    CREATE TABLE IF NOT EXISTS targets (
                        case_id TEXT NOT NULL, request_id TEXT NOT NULL, identity TEXT NOT NULL,
                        PRIMARY KEY(case_id,request_id,identity)
                    );
                    CREATE TABLE IF NOT EXISTS responses (
                        case_id TEXT NOT NULL, request_id TEXT NOT NULL, identity TEXT NOT NULL,
                        PRIMARY KEY(case_id,request_id,identity)
                    );
                    CREATE TABLE IF NOT EXISTS pending (
                        identity TEXT NOT NULL, case_id TEXT NOT NULL, request_id TEXT NOT NULL,
                        priority TEXT NOT NULL, created REAL NOT NULL, case_created REAL NOT NULL,
                        request_seq INTEGER NOT NULL, PRIMARY KEY(identity,case_id,request_id)
                    );
                    CREATE INDEX IF NOT EXISTS pending_target_order
                        ON pending(identity,priority,created,case_created,case_id,request_seq);
                    CREATE INDEX IF NOT EXISTS pending_case_request ON pending(case_id,request_id);
                    CREATE TABLE IF NOT EXISTS dirty (case_id TEXT PRIMARY KEY, published INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS case_files (case_id TEXT PRIMARY KEY, signature TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS case_errors (case_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS inventory (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL UNIQUE, done INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS remaining_inventory ON inventory(done,seq);
                    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS roster_seen (agent_id TEXT PRIMARY KEY);
        """)
        if not db.execute("SELECT 1 FROM meta WHERE key='cases_complete'").fetchone():
            try:
                with os.scandir(self.root / "cases") as entries:
                    legacy = next(entries, None) is not None
            except FileNotFoundError:
                legacy = False
            db.executemany("INSERT INTO meta VALUES (?,?)", [("cases_complete", str(int(not legacy))),
                           ("inventory_ready", str(int(not legacy)))])

    # LLM: 每连接只点查覆盖header；缺表或种子未完成时重用原安装器，其他SQLite错误原样拒绝。
    # 函数用途: 修复未初始化的schema，供调用者复核身份并在交给业务前提交。
    def _ensure_schema(self, db):
        try:
            if db.execute("SELECT 1 FROM meta WHERE key='cases_complete'").fetchone():
                return False
        except OperationalError as exc:
            if str(exc) != "no such table: meta":
                raise
        self._install(db)
        return True

    # LLM: 原no-follow及访问锁不变；重装种子先提交，随后业务BEGIN和回滚不影响初始化。
    # 函数用途: 提供一次原子事务，拒绝索引、sidecar 或父目录身份被替换。
    @contextmanager
    def connect(self):
        with locked_json_path(self.root / ".history-access"):
            if not nofollow_fs._supports_dir_fd():
                with self._connect_portable() as db:
                    yield db
                return
            with ExitStack() as resources:
                root_fd = open_directory_beneath(self.root, (), require_dir_fd=True)
                resources.callback(os.close, root_fd)
                pinned = []
                for suffix in ("-journal", "-wal", "-shm", ""):
                    path = Path(str(self.path) + suffix)
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        if suffix:
                            continue
                    descriptor = open_private_lock_beneath(self.root, (path.name,))
                    resources.callback(os.close, descriptor)
                    os.fchmod(descriptor, 0o600)
                    pinned.append((path, descriptor))
                _verify_files(self.root, root_fd, pinned)
                db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=5)
                try:
                    db.execute("PRAGMA foreign_keys=ON")
                    _verify_files(self.root, root_fd, pinned)
                    with db:
                        if self._ensure_schema(db):
                            _verify_files(self.root, root_fd, pinned)
                            db.commit()
                            logger.warning("SQLITE_SCHEMA_REINSTALLED store=collaboration_history path=%s", self.path)
                        yield db
                        _verify_files(self.root, root_fd, pinned)
                finally:
                    db.close()

    # LLM: 便携路径仅POSIX收紧权限并先关闭种子FD；重装先提交，避免业务BEGIN嵌套和回滚撤销种子。
    # 函数用途: 为无dir_fd平台保留外键、提交、回滚与连接关闭合同。
    @contextmanager
    def _connect_portable(self):
        root_info = nofollow_fs._portable_directory(self.root, (), create=False).lstat()
        pinned = []
        for suffix in ("-journal", "-wal", "-shm", ""):
            path = Path(str(self.path) + suffix)
            try:
                path.lstat()
            except FileNotFoundError:
                if suffix:
                    continue
            descriptor = open_private_lock_beneath(self.root, (path.name,))
            try:
                if os.name != "nt" and hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                pinned.append((path, os.fstat(descriptor)))
            finally:
                os.close(descriptor)
        _verify_files(self.root, root_info, pinned)
        db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            _verify_files(self.root, root_info, pinned)
            with db:
                if self._ensure_schema(db):
                    _verify_files(self.root, root_info, pinned)
                    db.commit()
                    logger.warning("SQLITE_SCHEMA_REINSTALLED store=collaboration_history path=%s", self.path)
                yield db
                _verify_files(self.root, root_info, pinned)
        finally:
            db.close()

    # LLM: 原子更新一个身份及活跃投影；legacy 可恢复 retired 身份，但不能覆盖 native 的后来登记。
    # 函数用途: 按精确 agent_id 登记能力，并同步独立活跃投影。
    def upsert_agent(self, capability, *, source="native", connection=None):
        if connection is None:
            with self.connect() as db:
                self.upsert_agent(capability, source=source, connection=db)
            return
        db = connection
        if source == "legacy":
            existing = db.execute("SELECT source FROM agents WHERE agent_id=?", (capability.agent_id,)).fetchone()
            if existing and existing[0] not in {"legacy", "retired"}:
                return
        db.execute("INSERT INTO agents VALUES (?,?,?) ON CONFLICT(agent_id) DO UPDATE SET "
                   "payload=excluded.payload,source=excluded.source",
                   (capability.agent_id, _encode(capability.to_dict()), source))
        db.execute("DELETE FROM active_agents WHERE agent_id=?", (capability.agent_id,))
        db.execute("DELETE FROM agent_features WHERE agent_id=?", (capability.agent_id,))
        if capability.status == AGENT_CAPABILITY_STATUS_AVAILABLE:
            db.execute("INSERT INTO active_agents VALUES (?,?)", (capability.agent_id, capability.load))
            db.executemany("INSERT OR IGNORE INTO agent_features VALUES (?,?)",
                           [(capability.agent_id, item) for item in capability.capabilities])

    # LLM: 固定页迁移旧来源；已知来源整文件消失也完成一次 retire，空签名作耐久完成标记，保留身份和 open 责任。
    # 函数用途: 可重启迁移任意大小旧名册，原件不改，重复身份遵守最后旧行及 native 优先规则。
    def migrate_roster(self):
        path = self.root / "agent_capabilities.json"
        with locked_json_path(self.root / ".history-roster-migration"):
            signature = file_signature(path)
            with self.connect() as db:
                meta = dict(db.execute("SELECT key,value FROM meta WHERE key LIKE 'roster_%'"))
                if meta.get("roster_complete") == signature:
                    return
                if not signature and "roster_complete" not in meta and not meta.get("roster_work"):
                    return
                offset = int(meta.get("roster_offset", "0")) if meta.get("roster_work") == signature else 0
                if meta.get("roster_work") != signature:
                    db.execute("DELETE FROM roster_seen")
                    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [("roster_work", signature), ("roster_offset", "0")])
            if signature:
                descriptor = open_readonly_file_beneath(self.root, (path.name,))
                page, count = [], 0
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    for count, item in enumerate(_legacy_values(stream, self.MAX_LEGACY_ITEM_CHARS), 1):
                        if count <= offset:
                            continue
                        page.append(item)
                        if len(page) == self.PAGE_SIZE:
                            self._roster_page(page, count)
                            page.clear()
                    if page:
                        self._roster_page(page, count)
            if file_signature(path) != signature:
                raise ValueError("legacy capability roster changed during migration")
            with self.connect() as db:
                retired = "SELECT agent_id FROM agents WHERE source='legacy' AND agent_id NOT IN (SELECT agent_id FROM roster_seen)"
                db.execute(f"DELETE FROM active_agents WHERE agent_id IN ({retired})")
                db.execute(f"DELETE FROM agent_features WHERE agent_id IN ({retired})")
                db.execute("UPDATE agents SET source='retired' WHERE source='legacy' AND agent_id NOT IN (SELECT agent_id FROM roster_seen)")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('roster_complete',?)", (signature,))

    # LLM: checkpoint 与本页能力行在同一事务，失败回滚整页；非对象旧值按原读取语义忽略但仍推进源序号。
    # 函数用途: 提交最多一页旧身份及耐久偏移。
    def _roster_page(self, page, offset):
        with self.connect() as db:
            for item in page:
                if not isinstance(item, dict):
                    continue
                capability = AgentCapability.from_dict(item)
                self.upsert_agent(capability, source="legacy", connection=db)
                db.execute("INSERT OR IGNORE INTO roster_seen VALUES (?)", (capability.agent_id,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('roster_offset',?)", (str(offset),))

    # LLM: 全集读取保留公开报告语义；候选查询另走活跃行，不能让历史身份丢失。
    # 函数用途: 返回所有已登记身份的独立能力模型。
    def agents(self):
        self.migrate_roster()
        with self.connect() as db:
            return [AgentCapability.from_dict(json.loads(row[0])) for row in db.execute("SELECT payload FROM agents ORDER BY rowid")]

    # LLM: 候选只查活跃投影；历史身份不删除，能力条件按原精确集合语义。
    # 函数用途: 分页挑选可用代理，不加载整张历史名册。
    def matching_agents(self, required, exclude, limit):
        self.migrate_roster()
        sql = "SELECT a.payload FROM active_agents v JOIN agents a ON a.agent_id=v.agent_id WHERE a.agent_id<>?"
        values = [exclude]
        for feature in sorted(required):
            sql += " AND EXISTS(SELECT 1 FROM agent_features f WHERE f.agent_id=a.agent_id AND f.feature=?)"
            values.append(feature)
        sql += " ORDER BY v.load,a.agent_id"
        if limit > 0:
            sql += " LIMIT ?"
            values.append(limit)
        with self.connect() as db:
            cursor = db.execute(sql, values)
            rows = []
            for page in iter(lambda: cursor.fetchmany(self.PAGE_SIZE), []):
                rows.extend(AgentCapability.from_dict(json.loads(item[0])) for item in page)
            return rows

    # LLM: 精确指纹读不遍历历史；写者在原 case 锁内以此核验自己将要改写的规范版本。
    # 函数用途: 读取一个已发表 case 的来源指纹。
    def case_signature(self, case_id):
        with self.connect() as db:
            row = db.execute("SELECT signature FROM case_files WHERE case_id=?", (case_id,)).fetchone()
            return row[0] if row else None

    # LLM: 索引未结标记先于 canonical 写；旧已发表事实只能增强，首次写失败不可释放已知旧 case。
    # 函数用途: 耐久标记一个 case 的投影正在更新。
    def mark_dirty(self, case_id, *, published):
        with self.connect() as db:
            published = published or bool(db.execute("SELECT 1 FROM cases WHERE case_id=?", (case_id,)).fetchone())
            db.execute("INSERT INTO dirty VALUES (?,?) ON CONFLICT(case_id) DO UPDATE SET "
                       "published=max(published,excluded.published)", (case_id, int(published)))

    # LLM: 物理账本 ID 是 SQL 写入边界，混入其它 case 的模型必须在任何事务修改前拒绝。
    # 函数用途: 防御性核对一个事件或恢复批次的归属。
    @staticmethod
    def _assert_case_rows(case_id, rows):
        for row in rows:
            if row is not None and row.case_id != case_id:
                raise ValueError("collaboration case identity mismatch")

    # LLM: 原 case 锁内写入；先核归属，事件/路由/指纹/清 dirty 在同一事务提交，不接受跨 case 事件。
    # 函数用途: 在原账本成功写入后增量发表对应投影，不重读其它 case 或历史事件。
    def commit_event(self, case_id, signature, *, case=None, request=None, evidence=None):
        self._assert_case_rows(case_id, (case, request, evidence))
        with self.connect() as db:
            if case is not None:
                self._put_case(db, case)
            if request is not None:
                self._put_request(db, request)
            if evidence is not None:
                self._put_evidence(db, evidence)
            db.execute("INSERT OR REPLACE INTO case_files VALUES (?,?)", (case_id, signature))
            db.execute("DELETE FROM dirty WHERE case_id=?", (case_id,))

    # LLM: case 原状态仍按字面 open 匹配；关闭时清热路由，重开时按保留的请求/回应恢复，不删除历史身份。
    # 函数用途: 更新一个 case 的规范投影及其路由状态。
    def _put_case(self, db, case):
        db.execute("INSERT INTO cases VALUES (?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET "
                   "payload=excluded.payload,status=excluded.status,created=excluded.created",
                   (case.case_id, _encode(case.to_dict()), case.status, case.created_at))
        self._refresh_routes(db, case.case_id)

    # LLM: completed 没有证据时仍按原 pending 语义等待；只有 declined/blocked 排除，精确目标不扩名/角色。
    # 函数用途: 保存一条请求的最后快照、目标集合和独立热路由。
    def _put_request(self, db, request):
        eligible = int(not (is_declined_request_status(request.status) or is_blocked_request_status(request.status)))
        db.execute("INSERT INTO requests(case_id,request_id,payload,eligible,priority,created) VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(case_id,request_id) DO UPDATE SET payload=excluded.payload,eligible=excluded.eligible,"
                   "priority=excluded.priority,created=excluded.created",
                   (request.case_id, request.request_id, _encode(request.to_dict()), eligible, request.priority, request.created_at))
        db.execute("DELETE FROM targets WHERE case_id=? AND request_id=?", (request.case_id, request.request_id))
        db.executemany("INSERT INTO targets VALUES (?,?,?)", [(request.case_id, request.request_id, identity)
                       for identity in agent_identity_keys(request.target_agent_ids)])
        self._refresh_routes(db, request.case_id, request.request_id)

    # LLM: 任意有效回应都停止同一 source 的等待，matched 不改变该合同；回应历史永久保留供改派/重开复核。
    # 函数用途: 登记请求的精确回应身份，移除已回答的热路由。
    def _put_evidence(self, db, evidence):
        if evidence.request_id and evidence.source_agent_id:
            db.executemany("INSERT OR IGNORE INTO responses VALUES (?,?,?)", [
                (evidence.case_id, evidence.request_id, identity) for identity in agent_identity_keys(evidence.source_agent_id)])
            self._refresh_routes(db, evidence.case_id, evidence.request_id)

    # LLM: 所有删除和重建均带 case/request 索引键；热表不包含关闭 case、拒绝/阻塞及已回应的目标。
    # 函数用途: 只重算被修改的 case 或请求的派发候选。
    def _refresh_routes(self, db, case_id, request_id=None):
        where, values = "case_id=?", [case_id]
        selected = "t.case_id=?"
        if request_id is not None:
            where += " AND request_id=?"
            selected += " AND t.request_id=?"
            values.append(request_id)
        db.execute("DELETE FROM pending WHERE " + where, values)
        db.execute("INSERT OR IGNORE INTO pending SELECT t.identity,t.case_id,t.request_id,r.priority,r.created,c.created,r.seq "
                   "FROM targets t JOIN requests r ON r.case_id=t.case_id AND r.request_id=t.request_id "
                   "JOIN cases c ON c.case_id=t.case_id WHERE " + selected + " AND c.status='open' AND r.eligible=1 "
                   "AND NOT EXISTS(SELECT 1 FROM responses e WHERE e.case_id=t.case_id "
                   "AND e.request_id=t.request_id AND e.identity=t.identity)", values)

    # LLM: 仅迁移或精确 dirty 恢复走全 case 重建；规范文件错误保留在独立错误表，不冒充空账。
    # 函数用途: 在原 case 锁内原子替换一个 case 的读投影并清理耐久 pending。
    def sync_case(self, case, requests, evidence, errors, signature):
        self._assert_case_rows(case.case_id, requests)
        self._assert_case_rows(case.case_id, evidence)
        with self.connect() as db:
            for table in ("pending", "targets", "responses", "requests"):
                db.execute(f"DELETE FROM {table} WHERE case_id=?", (case.case_id,))
            self._put_case(db, case)
            for request in requests:
                self._put_request(db, request)
            for item in evidence:
                self._put_evidence(db, item)
            db.execute("DELETE FROM case_errors WHERE case_id=?", (case.case_id,))
            if errors:
                db.execute("INSERT INTO case_errors VALUES (?,?)", (case.case_id, _encode(errors)))
            db.execute("INSERT OR REPLACE INTO case_files VALUES (?,?)", (case.case_id, signature))
            db.execute("DELETE FROM dirty WHERE case_id=?", (case.case_id,))

    # LLM: 首写尚未发表需耐久 published=0 且 canonical 缺失共同证明；旧已发表缺失一直保持未知。
    # 函数用途: 精确清除没有产生任何 case 的失败首写占位。
    def discard_unpublished(self, case_id):
        with self.connect() as db:
            row = db.execute("SELECT published FROM dirty WHERE case_id=?", (case_id,)).fetchone()
            if row == (0,) and not db.execute("SELECT 1 FROM cases WHERE case_id=?", (case_id,)).fetchone():
                db.execute("DELETE FROM dirty WHERE case_id=?", (case_id,))
                return True
            return False

    # LLM: 只选固定一页已知 ID；调用方在原 case 锁内重读，不用目录发现恢复 pending。
    # 函数用途: 为生产恢复返回一页未结 case。
    def dirty_cases(self):
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT case_id FROM dirty ORDER BY case_id LIMIT ?", (self.PAGE_SIZE,))]

    # LLM: 覆盖观察不枚举目录；耐久 dirty 也是未收敛工作，不能因旧迁移完成而忽略精确恢复通知。
    # 函数用途: 返回迁移/精确修复是否完成及剩余维护工作量。
    def coverage(self):
        with self.connect() as db:
            complete = db.execute("SELECT value FROM meta WHERE key='cases_complete'").fetchone()[0] == "1"
            remaining = db.execute("SELECT count(*) FROM inventory WHERE done=0").fetchone()[0]
            remaining += db.execute("SELECT count(*) FROM dirty").fetchone()[0]
            return {"complete": complete and remaining == 0, "remaining": remaining}

    # LLM: 后台一次发现后使用稳定 SQL seq；包括已知 SQL case，防止缺 canonical 的旧身份被静默丢弃。
    # 函数用途: 建立可重启的 case 库存，每次内存及 SQL 参数为固定页。
    def inventory_batch(self):
        with locked_json_path(self.root / ".history-inventory"):
            with self.connect() as db:
                ready = db.execute("SELECT value FROM meta WHERE key='inventory_ready'").fetchone()[0] == "1"
                if not ready:
                    cursor = db.execute("SELECT case_id FROM cases ORDER BY case_id")
                    for page in iter(lambda: cursor.fetchmany(self.PAGE_SIZE), []):
                        db.executemany("INSERT OR IGNORE INTO inventory(case_id) VALUES (?)", page)
            if not ready:
                page = []
                with os.scandir(self.root / "cases") as entries:
                    for entry in entries:
                        if entry.name.endswith(".json"):
                            page.append((entry.name[:-5],))
                        if len(page) == self.PAGE_SIZE:
                            with self.connect() as db:
                                db.executemany("INSERT OR IGNORE INTO inventory(case_id) VALUES (?)", page)
                            page.clear()
                with self.connect() as db:
                    db.executemany("INSERT OR IGNORE INTO inventory(case_id) VALUES (?)", page)
                    db.execute("UPDATE meta SET value='1' WHERE key='inventory_ready'")
            with self.connect() as db:
                return list(db.execute("SELECT seq,case_id FROM inventory WHERE done=0 ORDER BY seq LIMIT ?", (self.PAGE_SIZE,)))

    # LLM: 只有原文件复核成功的精确 case 才完成；失败库存不跳过，后续维护可在原 seq 重试。
    # 函数用途: 标记一条迁移库存完成并按真实剩余数发布覆盖。
    def complete_inventory(self, seq):
        with self.connect() as db:
            if seq is not None:
                db.execute("UPDATE inventory SET done=1 WHERE seq=?", (seq,))
            remaining = db.execute("SELECT 1 FROM inventory WHERE done=0 LIMIT 1").fetchone()
            ready = db.execute("SELECT value FROM meta WHERE key='inventory_ready'").fetchone()[0] == "1"
            db.execute("UPDATE meta SET value=? WHERE key='cases_complete'", (str(int(ready and not remaining)),))

    # LLM: 一个读事务按精确身份点查；错误查询固定稀疏错误表为外层，不让优化器反向遍历所有 open case。
    # 函数用途: 获取有限目标的待回应请求及覆盖/坏账事实，关闭及已回应历史不进入候选。
    def pending_rows(self, identities, limit):
        identities = sorted(identities)
        selected, errors = {}, []
        slots = ",".join("?" for _ in identities)
        with self.connect() as db:
            db.execute("BEGIN")
            for identity in identities:
                sql = ("SELECT p.case_id,p.request_id,c.payload,r.payload,f.signature,p.priority,p.created,p.case_created,p.request_seq "
                       "FROM pending p INDEXED BY pending_target_order JOIN cases c ON c.case_id=p.case_id "
                       "JOIN requests r ON r.case_id=p.case_id AND r.request_id=p.request_id "
                       "LEFT JOIN case_files f ON f.case_id=p.case_id WHERE p.identity=? "
                       "AND NOT EXISTS(SELECT 1 FROM dirty d WHERE d.case_id=p.case_id) "
                       "AND NOT EXISTS(SELECT 1 FROM responses e WHERE e.case_id=p.case_id AND e.request_id=p.request_id "
                       f"AND e.identity IN ({slots})) ORDER BY p.priority,p.created,p.case_created,p.case_id,p.request_seq")
                values = [identity, *identities]
                if limit > 0:
                    sql += " LIMIT ?"
                    values.append(limit)
                cursor = db.execute(sql, values)
                for page in iter(lambda: cursor.fetchmany(self.PAGE_SIZE), []):
                    for row in page:
                        selected[(row[0], row[1])] = row
            if db.execute("SELECT value FROM meta WHERE key='cases_complete'").fetchone()[0] != "1":
                errors.append({"context": "collaboration.pending.index", "error_type": "PartialCoverage", "message": "legacy case migration is partial"})
            dirty = list(db.execute("SELECT case_id FROM dirty ORDER BY case_id LIMIT ?", (self.PAGE_SIZE + 1,)))
            errors.extend({"context": "collaboration.pending.index", "error_type": "PendingCanonicalCommit", "case_id": item[0]} for item in dirty)
            for row in db.execute("SELECT e.payload FROM case_errors e CROSS JOIN cases c ON c.case_id=e.case_id "
                                  "WHERE c.status='open' LIMIT ?", (self.PAGE_SIZE + 1,)):
                errors.extend(json.loads(row[0]))
        rows = sorted(selected.values(), key=lambda row: (row[5], row[6], row[7], row[0], row[8]))
        return (rows if limit <= 0 else rows[:limit]), errors


# LLM: 读写均使用同一稳定序列化，拒绝 NaN 等不能作为稳定能力/路由事实的值。
# 函数用途: 编码私有 SQL 行及指纹，不生成解释性字段。
def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


# LLM: 每次只 stat 精确路径；链接及非普通文件不是健康缺失，缺失用空标记供写者证明首次未发表。
# 函数用途: 生成目录内文件的稳定身份/版本指纹。
def file_signature(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("collaboration source must be a regular single-link file")
    return _encode([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])


# LLM: SQLite 路径需匹配 no-follow 预检 FD；缺 sidecar 允许正常提交删除，出现时仍须私有普通单链接。
# 函数用途: 复核目录、数据库和 journal/WAL 的身份与私有权限。
def _verify_files(root, root_fd, pinned):
    if not nofollow_fs._supports_dir_fd():
        _verify_portable_files(root, root_fd, pinned)
        return
    current, opened = root.lstat(), os.fstat(root_fd)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise OSError("collaboration SQLite directory identity changed")
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
            raise OSError("collaboration SQLite must be a private regular single-link file")
    for path, descriptor in pinned:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if path != main:
                continue
            raise
        opened = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("collaboration SQLite file identity changed")


# LLM: 便携路径保留普通单链接、reparse及目录/文件身份检查；仅POSIX且有fchmod时核权限位。
# 函数用途: 不持外部SQLite文件FD地复核主库和正常增删的sidecar。
def _verify_portable_files(root, root_info, pinned):
    current = nofollow_fs._portable_directory(root, (), create=False).lstat()
    if ((current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino)
            or getattr(current, "st_file_attributes", 0) & 0x400):
        raise OSError("collaboration SQLite directory identity changed")
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
                or os.name != "nt" and hasattr(os, "fchmod") and stat.S_IMODE(info.st_mode) & 0o077):
            raise OSError("collaboration SQLite must be a private regular single-link file")
        opened = identities.get(path)
        if opened is not None and (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("collaboration SQLite file identity changed")


# LLM: 旧名册是一个 JSON 对象，不能按行拆或把全文件交 json.load；单值上界与固定读块避免累计整份历史。
# 类用途: 为旧 map 提供保留 JSON 语法错误及重复键顺序的流式读取。
class _MapReader:
    # LLM: 缓冲只保存尚未消费的一个值及下一固定块；不缓存已经提交的旧记录。
    # 函数用途: 初始化流式 JSON 解码器。
    def __init__(self, stream, max_chars):
        self.stream, self.max_chars = stream, max_chars
        self.buffer, self.eof = "", False
        self.decoder = json.JSONDecoder()

    # LLM: 只在当前值未完整时继续固定块读取；EOF 是来源事实，不按空数据猜测成功。
    # 函数用途: 补充一个源块。
    def _read(self):
        chunk = self.stream.read(65536)
        self.eof = not chunk
        self.buffer += chunk

    # LLM: 大片空白逐块消费，不使空白前缀累积成整份文件。
    # 函数用途: 返回下一个 JSON 符号或显式 EOF。
    def peek(self):
        self.buffer = self.buffer.lstrip()
        while not self.buffer and not self.eof:
            self._read()
            self.buffer = self.buffer.lstrip()
        return self.buffer[:1]

    # LLM: 严格匹配对象分隔符，不接受逗号缺失、尾逗号或把截断来源当作完整对象。
    # 函数用途: 消费一个预期符号。
    def expect(self, symbol):
        if self.peek() != symbol:
            raise ValueError("invalid legacy capability roster object syntax")
        self.buffer = self.buffer[1:]

    # LLM: 数字恰好结束在读块边界也需读下一块确认；错误和超大单值显式返回，不静默跳行。
    # 函数用途: 解码一个有界 JSON 值。
    # LLM: raw_decode 可只识别数字前缀；必须看到完整 token 的分隔符或 EOF 才提交消费位置。
    # 函数用途: 跨固定读取块完整解出一个旧 JSON 值。
    def value(self):
        self.peek()
        while True:
            if len(self.buffer) > self.max_chars:
                raise ValueError("legacy capability roster item exceeds bounded migration limit")
            try:
                value, end = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                if self.eof:
                    raise
                self._read()
                continue
            if end == len(self.buffer) and not self.eof:
                self._read()
                continue
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and end < len(self.buffer) and self.buffer[end] not in " \t\r\n,}]"):
                if self.eof:
                    raise ValueError("invalid legacy capability roster number token")
                self._read()
                continue
            self.buffer = self.buffer[end:]
            return value


# LLM: 按原 map 顺序产出所有值，调用方按固定页提交且保留源序号；尾部垃圾绝不标记迁移完成。
# 函数用途: 流式迁移旧名册，保留非对象值的原忽略口径及错误边界。
def _legacy_values(stream, max_chars):
    reader = _MapReader(stream, max_chars)
    reader.expect("{")
    if reader.peek() != "}":
        while True:
            if not isinstance(reader.value(), str):
                raise ValueError("legacy capability roster key must be a string")
            reader.expect(":")
            yield reader.value()
            if reader.peek() == "}":
                break
            reader.expect(",")
    reader.expect("}")
    if reader.peek():
        raise ValueError("trailing data in legacy capability roster")
