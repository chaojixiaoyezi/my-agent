# LLM: schema仍是唯一建表权威；结构摘要涵盖SQL/迁移/FTS，显式同线程批次复用连接但不合并各操作事务。
# LLM: coordinator_io只选择更严格短IO，实际scope每次借用绑定且不留source；旧默认guard/迁移/连接缓存保持。
# 模块用途: 管理原SQLite结构与连接，C独立零等待并消费当前原提交门。
from __future__ import annotations

"""owns SQLite connection setup, schema creation, and transactional context helpers.

这个文件只负责 LocalStore 的数据库地基。
建表、打开连接、启用 WAL/外键、处理 FTS5 是否可用，都在这里集中处理。
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from ..common.global_write_commit import execute_global_write
from ..common.json_io import append_private_jsonl_records_unlocked, locked_json_path
from ..common.nofollow_fs import ensure_private_dir
from ..io.sqlite_schema import schema_fingerprint, schema_is_current, stamp_schema
from ..io.sqlite_scope import scoped_connection, sqlite_connection_factory

# FTS5 records 索引的 schema 版本。中文检索失灵根因:默认 unicode61 把一整段汉字
# 当作 1 个 token(实测 sqlite 3.53 下 '记忆推送模式...' 整串=单 token),子串/词
# 永远 MATCH 不中,FTS5 对中文形同虚设(只能靠 LIKE 兜底,零排序、只扫 preview)。
# v2 改用 fts5 自带的 'trigram' 分词器(逐字三元组,子串可命中,零外部依赖)。
# 版本号变了就 DROP+重建 records_fts 并由 maintenance.rebuild_fts 回填正文。
_FTS_SCHEMA_VERSION = "2"
_FTS_SCHEMA_VERSION_KEY = "records_fts_schema_version"
# trigram:子串级中文检索的关键。代价:① 查询 token 需 >=3 字符才可能命中
# (1-2 字查询由 search() 的 LIKE 兜底,已有逻辑);② 索引体积略增。
_FTS_TOKENIZE = "trigram"

_BASE_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        title TEXT NOT NULL,
        content_path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content_preview TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        visibility TEXT NOT NULL DEFAULT 'private',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_records_source
    ON records(source_type, source_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_records_updated
    ON records(updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        record_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    )
    """,
)

_TASK_REGISTRY_SQL = (
    """
    CREATE TABLE IF NOT EXISTS task_registry (
        task_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL,
        goal TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_registry_session ON task_registry(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_registry_user ON task_registry(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_registry_status ON task_registry(status)",
    "CREATE INDEX IF NOT EXISTS idx_task_registry_updated ON task_registry(updated_at)",
    """
    INSERT INTO metadata(key, value)
    VALUES('task_registry_enabled', 'true')
    ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """,
)

_CONTROL_PLANE_SQL = (
    # R1：旧 agent_runs 投影改名 legacy_agent_runs（3.txt R1 迁移顺序：新旧不得
    # 同名双权威——权威 agent_runs 现属 owner runtime.db）。新库直接建新名，
    # 旧库经 _migrate_legacy_agent_runs_rename 数据保留改名。
    """
    CREATE TABLE IF NOT EXISTS legacy_agent_runs (
        run_id TEXT PRIMARY KEY,
        root_task_id TEXT NOT NULL,
        parent_run_id TEXT NOT NULL DEFAULT '',
        depth INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT '',
        agent_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        progress REAL NOT NULL DEFAULT 0,
        current_step TEXT NOT NULL DEFAULT '',
        latest_summary TEXT NOT NULL DEFAULT '',
        workspace_path TEXT NOT NULL DEFAULT '',
        checkpoint_ref TEXT NOT NULL DEFAULT '',
        latest_compact_ref TEXT NOT NULL DEFAULT '',
        compact_count INTEGER NOT NULL DEFAULT 0,
        heartbeat_at REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_root ON legacy_agent_runs(root_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON legacy_agent_runs(parent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON legacy_agent_runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_updated ON legacy_agent_runs(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS task_rollups (
        task_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT '',
        progress REAL NOT NULL DEFAULT 0,
        running_agents INTEGER NOT NULL DEFAULT 0,
        blocked_agents INTEGER NOT NULL DEFAULT 0,
        completed_agents INTEGER NOT NULL DEFAULT 0,
        failed_agents INTEGER NOT NULL DEFAULT 0,
        latest_summary TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_rollups_updated ON task_rollups(updated_at)",
)

_RUNTIME_GATE_LEDGER_SQL = (
    """
    CREATE TABLE IF NOT EXISTS runtime_gate_ledger (
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        operation_id TEXT NOT NULL,
        tool TEXT NOT NULL DEFAULT '',
        parameters_json TEXT NOT NULL DEFAULT '{}',
        runtime_gate_json TEXT NOT NULL DEFAULT '{}',
        idempotency_key TEXT NOT NULL DEFAULT '',
        args_hash TEXT NOT NULL DEFAULT '',
        approval_id TEXT NOT NULL DEFAULT '',
        result_ref TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY(run_id, operation_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runtime_gate_ledger_run ON runtime_gate_ledger(run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_gate_ledger_task ON runtime_gate_ledger(task_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_gate_ledger_idem ON runtime_gate_ledger(run_id, idempotency_key)",
)

_TOOL_OPERATIONS_SQL = (
    """
    CREATE TABLE IF NOT EXISTS tool_operations (
        owner_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        operation_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        args_hash TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        idempotency_scope TEXT NOT NULL,
        idempotency_namespace TEXT NOT NULL,
        status TEXT NOT NULL,
        holder_id TEXT NOT NULL,
        holder_host TEXT NOT NULL DEFAULT '',
        holder_pid INTEGER NOT NULL DEFAULT 0,
        holder_process_start_token TEXT NOT NULL DEFAULT '',
        generation INTEGER NOT NULL DEFAULT 1,
        lease_expires_at REAL NOT NULL,
        result_json TEXT NOT NULL DEFAULT '{}',
        error_code TEXT NOT NULL DEFAULT '',
        unknown_reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        completed_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(owner_id, run_id, operation_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_operations_business_key
    ON tool_operations(owner_id, idempotency_namespace, idempotency_key)
    WHERE idempotency_scope = 'business' AND idempotency_key <> ''
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_operations_run ON tool_operations(owner_id, run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tool_operations_status ON tool_operations(owner_id, status, updated_at)",
)


_LOCAL_MIGRATION_SQL = ("ALTER TABLE agent_runs RENAME TO legacy_agent_runs",)
_FTS_TABLE_SQL = "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED, title, content, tokenize='{tokenizer}')"
_FTS_DROP_SQL = "DROP TABLE IF EXISTS records_fts"
_FTS_REBUILD_PENDING_KEY = "records_fts_rebuild_pending"
_LOCAL_STATE_SQL = "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"


# LLM: 只读取目标库的独立状态键，结构摘要不能代替 FTS 可用性或回填完成状态。
# 函数用途: 读取一个结构化初始化状态，不缓存用户正文。
def _read_local_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row is not None else None


# LLM: 状态写入必须在调用方事务中提交；SQL 文本也纳入结构指纹。
# 函数用途: 保存可恢复的 FTS 版本或回填状态。
def _write_local_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(_LOCAL_STATE_SQL, (key, value))


# LLM: 数字版本只能向前迁移；遇到旧程序不认识的非数字版本也保留，不能为了指纹失配降级投影。
# 函数用途: 判断已有 FTS 是否超出当前程序可安全迁移的版本。
def _newer_fts_version(stored: str | None) -> bool:
    if stored is None or stored == _FTS_SCHEMA_VERSION:
        return False
    if not stored.isdecimal():
        return True
    return _FTS_SCHEMA_VERSION.isdecimal() and int(stored) > int(_FTS_SCHEMA_VERSION)


# LLM: 摘要包含全部建表、迁移及 FTS 声明，开关/分词器变化不沿用旧结构；只计算，不访问用户数据。
# 函数用途: 自动计算 LocalStore 当前结构版本，不需手工递增版本号。
def _local_schema_fingerprint(enable_fts: bool) -> str:
    return schema_fingerprint((
        _BASE_SCHEMA_SQL, _TASK_REGISTRY_SQL, _CONTROL_PLANE_SQL,
        _RUNTIME_GATE_LEDGER_SQL, _TOOL_OPERATIONS_SQL, _LOCAL_MIGRATION_SQL,
        enable_fts, _FTS_TABLE_SQL, _FTS_DROP_SQL, _FTS_SCHEMA_VERSION, _FTS_TOKENIZE,
        sqlite3.sqlite_version, _LOCAL_STATE_SQL, _FTS_REBUILD_PENDING_KEY, _FTS_SCHEMA_VERSION_KEY,
    ))


# LLM: 结构摘要不替代实例 FTS 状态；只读实际虚表验证本 SQLite 能使用它，无 FTS 时保持 LIKE 兜底。
# 函数用途: 跳过建表时仍恢复正确的全文索引可用性。
def _existing_fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT id FROM records_fts LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


# LLM: 初始化按库内摘要跳过且恢复FTS实例状态；完整迁移与盖章同事务，有界批次只优化连接生命周期。
# LLM: C仅固定初始化/事务叶复用原global scope，所有source/callback只在当前借用栈，不新增上下文或状态权威。
# 类用途: 管理原schema与连接，确保旧库迁移和C每次末写来源都准确。
class LocalStoreSchemaMixin:

    # LLM: 建目录/建表会写盘，摘要命中时只读取 FTS 状态；原正文回填仍在 schema 事务外执行。
    # LLM: C分支只保存IO策略，实际出生/DDL依当前原scope；默认分支及完整schema guard不变。
    # 函数用途: 首次或变化时按原schema迁移，C的扫描回填另在事务外准备。
    def _init_schema(self) -> None:
        if self.coordinator_io:
            return self._init_coordinator_schema()
        ensure_private_dir(self.root)
        ensure_private_dir(self.files_dir)
        ensure_private_dir(self.events_path.parent)
        # 结构指纹只证明 DDL 完成；回填标记独立持久化，失败重开仍会重试。
        self._fts_needs_rebuild = False
        fingerprint = _local_schema_fingerprint(self.enable_fts)
        with self._connection() as conn:
            self._ensure_schema(conn, fingerprint)
        # 索引刚被迁移重建(旧 unicode61 表被丢弃),用现有 records 回填新 trigram 索引。
        # rebuild_fts 由 maintenance mixin 提供;最小 schema-only store 没有它时跳过
        # 回填(它本就没有 records 内容文件,新空索引即正确)。
        rebuild = getattr(self, "rebuild_fts", None)
        if self._fts_needs_rebuild and self._fts_available and callable(rebuild):
            try:
                rebuild()
                self._fts_needs_rebuild = False
            except sqlite3.OperationalError:
                # 回填失败不能让 store 起不来:LIKE 兜底仍可用,下次 rebuild_fts 再补。
                pass

    # LLM: 未命中时持写锁再次核对避免并发重复迁移；异常靠连接出口回滚，不盖失败的结构摘要。
    # 函数用途: 在同一事务完成需要的建表与迁移，或者只恢复 FTS 实例状态。
    def _ensure_schema(self, conn: sqlite3.Connection, fingerprint: str) -> None:
        if schema_is_current(conn, fingerprint) and self._restore_fts_state(conn):
            return
        conn.execute("BEGIN IMMEDIATE")
        if schema_is_current(conn, fingerprint) and self._restore_fts_state(conn):
            return
        self._execute_schema(conn, _BASE_SCHEMA_SQL)
        self._migrate_legacy_agent_runs_rename(conn)
        self._execute_schema(conn, _TASK_REGISTRY_SQL)
        self._execute_schema(conn, _CONTROL_PLANE_SQL)
        self._execute_schema(conn, _RUNTIME_GATE_LEDGER_SQL)
        self._execute_schema(conn, _TOOL_OPERATIONS_SQL)
        fts_initialized = not self.enable_fts
        if self.enable_fts:
            fts_initialized = self._init_fts_schema(conn)
        else:
            self._fts_available = False
            _write_local_state(conn, _FTS_REBUILD_PENDING_KEY, "true")
        self._restore_fts_state(conn)
        if fts_initialized:
            stamp_schema(conn, fingerprint)
        conn.commit()

    # LLM: 即使 DDL 已完成也独立读取虚表及待回填键，避免吞掉上次初始化后的回填失败。
    # 函数用途: 恢复三个独立事实：配置开启、索引可读、正文待回填。
    def _restore_fts_state(self, conn: sqlite3.Connection) -> bool:
        self._fts_available = self.enable_fts and _existing_fts_available(conn)
        self._fts_needs_rebuild = _read_local_state(conn, _FTS_REBUILD_PENDING_KEY) == "true"
        return not self.enable_fts or self._fts_available

    # LLM: 只能与实际回填在同一事务内清标记，失败/中断 rollback 后重开仍待回填。
    # 函数用途: 由维护器在成功回填提交前记下完成状态。
    def _record_fts_rebuild_complete(self, conn: sqlite3.Connection) -> None:
        _write_local_state(conn, _FTS_REBUILD_PENDING_KEY, "false")

    # LLM: 本迁移仅保留数据改名，SQL 属结构摘要；调用方持写事务，不能同时建立新旧同名权威。
    # 函数用途: 把旧投影表改名，与 runtime.db 的真实运行表区分。
    def _migrate_legacy_agent_runs_rename(self, conn: sqlite3.Connection) -> None:
        """数据保留迁移：旧投影表 agent_runs → legacy_agent_runs。

        权威 agent_runs 现属 owner runtime.db，投影库不得再用同名表（R1 新旧
        不得同名双权威）。已有库执行一次 ALTER TABLE RENAME（索引随表保留），
        新库无旧表则跳过，随后 _CONTROL_PLANE_SQL 的 IF NOT EXISTS 建新名。
        """
        has_old = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs'"
        ).fetchone()
        has_new = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_agent_runs'"
        ).fetchone()
        if has_old and not has_new:
            self._execute_schema(conn, _LOCAL_MIGRATION_SQL)

    # LLM: 仅执行传入的声明 SQL，调用方持写事务；指纹必须包含这份清单。
    # 函数用途: 按顺序建表和迁移，不自行提交。
    def _execute_schema(self, conn: sqlite3.Connection, statements: tuple[str, ...]) -> None:
        for statement in statements:
            conn.execute(statement)

    # LLM: FTS SQL/分词器/版本都进入结构摘要；失败保留 LIKE 兜底，DDL 与版本写入在原初始化事务内。
    # 函数用途: 创建或升级可重建的全文索引，返回语句是否成功以决定能否盖完成指纹。
    def _init_fts_schema(self, conn: sqlite3.Connection) -> bool:
        try:
            self._migrate_fts_schema(conn)
            conn.execute(_FTS_TABLE_SQL.format(tokenizer=_FTS_TOKENIZE))
            self._fts_available = True
            self._record_fts_schema_version(conn)
            return True
        except sqlite3.OperationalError:
            self._fts_available = False
            return False

    # LLM: 只丢弃纯 FTS 投影，不动正文事实；迁移 SQL 包含在摘要，调用方在事务外按旧语义回填。
    # 函数用途: 分词器版本变化时重建索引，并告诉初始化器需要回填。
    def _migrate_fts_schema(self, conn: sqlite3.Connection) -> None:
        """旧 FTS schema(无 trigram)在版本不匹配时丢弃并标记回填。

        records_fts 是纯索引,正文事实源在 records 表/内容文件,DROP 后 rebuild_fts
        可无损重建。仅当 records_fts 已存在且记录的 schema 版本与当前不一致时才丢弃,
        首建(无表)不触发回填。"""
        has_fts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='records_fts'"
        ).fetchone()
        if not has_fts:
            needs_rebuild = conn.execute("SELECT 1 FROM records LIMIT 1").fetchone() is not None
            _write_local_state(conn, _FTS_REBUILD_PENDING_KEY, "true" if needs_rebuild else "false")
            return
        stored_version = _read_local_state(conn, _FTS_SCHEMA_VERSION_KEY)
        if stored_version == _FTS_SCHEMA_VERSION or _newer_fts_version(stored_version):
            return
        conn.execute(_FTS_DROP_SQL)
        _write_local_state(conn, _FTS_REBUILD_PENDING_KEY, "true")

    # LLM: 新程序可记录已建好的版本，旧程序不能覆盖新库的版本事实。
    # 函数用途: 保存 FTS 声明版本但不执行降级。
    def _record_fts_schema_version(self, conn: sqlite3.Connection) -> None:
        if not _newer_fts_version(_read_local_state(conn, _FTS_SCHEMA_VERSION_KEY)):
            _write_local_state(conn, _FTS_SCHEMA_VERSION_KEY, _FTS_SCHEMA_VERSION)

    # LLM: 新连接才设 PRAGMA；仅显式作用域用游标跟踪子类，作用域外保持标准库短连接。
    # LLM: 默认仍30秒/原缓存工厂；C只用当前scope内独立timeout0短连接，不混入F1连接池。
    # 函数用途: 按实例IO策略打开原库，C出生与WAL均经实际末门。
    def _connect(self) -> sqlite3.Connection:
        if self.coordinator_io:
            return self._local_write(self._connect_coordinator)
        conn = sqlite3.connect(self.db_path, timeout=30.0, factory=sqlite_connection_factory())
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 另一个 gateway / CLI 进程短暂持锁时，状态查询不能因为切 WAL 失败而崩。
            pass
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # LLM: 显式批次只复用同store、同线程连接；嵌套不关闭外层，各写API原有commit/rollback边界不变，异常也释放。
    # LLM: C分支仅保上下文形状、不登记或复用连接，各操作维持独立短事务，默认batch不变。
    # 函数用途: 默认复用原线程批次连接，C不借默认30秒连接。
    @contextmanager
    def connection_batch(self):
        if self.coordinator_io:
            yield
            return
        state = self._connection_local
        if getattr(state, 'connection', None) is not None:
            yield
            return
        with self._connection() as conn:
            state.connection = conn
            try:
                yield
            finally:
                del state.connection

    # LLM: 批次借用连接仍丢弃未提交事务；非批次操作复用当前显式SQLite作用域，否则保持默认短连接。
    # LLM: C仅在活scope内出生短读取连接，门外读取后精确原FD rollback/close不因撤权泄漏；不授权其它mixin新DML。
    # 函数用途: 取得原操作连接，C读取与固定写事务使用不同明确入口。
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self.coordinator_io:
            with self._coordinator_read_connection() as conn:
                yield conn
            return
        state = getattr(self, '_connection_local', None)
        existing = getattr(state, 'connection', None)
        if existing is not None:
            try:
                yield existing
            finally:
                existing.rollback()
            return
        with scoped_connection(self.db_path, False, self._connect) as conn:
            yield conn


    # LLM: 只消费原global scope/window；不保存callback，不给普通W/Count0无条件增加global guard。
    # 函数用途: 将C的固定本地末writer交给当次真实来源，缺源沿原typed拒绝。
    def _local_write(self, operation):
        return execute_global_write(operation, require_scope=True)

    # LLM: 原目录及完整schema算法照用；指纹纯计算在门外，回填仍独立事务且失败保原pending。
    # 函数用途: 为C实例逐固定出生/DDL门初始化原库，不缓存来源或跳过旧库迁移。
    def _init_coordinator_schema(self):
        self._local_write(self._prepare_coordinator_directories)
        self._fts_needs_rebuild = False
        fingerprint = _local_schema_fingerprint(self.enable_fts)
        self._local_write(lambda: self._initialize_coordinator_schema(fingerprint))
        rebuild = getattr(self, "rebuild_fts", None)
        if self._fts_needs_rebuild and self._fts_available and callable(rebuild):
            try:
                rebuild()
                self._fts_needs_rebuild = False
            except sqlite3.OperationalError:
                pass

    # LLM: 只准备原root/files/events父目录，路径来自同Store；不接任意目录或做扫描。
    # 函数用途: 在固定初始化末门内执行原三处目录确保。
    def _prepare_coordinator_directories(self):
        ensure_private_dir(self.root)
        ensure_private_dir(self.files_dir)
        ensure_private_dir(self.events_path.parent)

    # LLM: 此叶只由原短window调用；完整_ensure_schema及原guard/迁移/stamp不复制，清理不覆盖主异常。
    # 函数用途: 用独立零等待连接执行原schema事务并回收原FD。
    def _initialize_coordinator_schema(self, fingerprint):
        conn = self._connect_coordinator()
        primary = None
        try:
            self._ensure_schema(conn, fingerprint)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _retire_coordinator_connection(conn, primary)

    # LLM: 调用方已在原末window；无F1 factory/cache/batch，只有原WAL/foreign_keys和零等待参数。
    # 函数用途: 创建C的一条独立短连接，设置失败也关闭本次已打开FD。
    def _connect_coordinator(self):
        conn = sqlite3.connect(self.db_path, timeout=0.0)
        primary = None
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=0")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except BaseException as exc:
            primary = exc
            _retire_coordinator_connection(conn, primary)
            raise

    # LLM: 出生由当前源核，读取在门外；退出只回收已打开同一FD，不新连/补账，撤权仍cleanup且保主异常。
    # 函数用途: 为C门外查询或快照准备取得短读取连接。
    @contextmanager
    def _coordinator_read_connection(self):
        conn = self._connect()
        primary = None
        try:
            yield conn
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _retire_coordinator_connection(conn, primary)

    # LLM: operation是固定原SQL算法，所有参数/正文已准备；BEGIN/DML/event/commit/cleanup同末门，不能提交门外DML。
    # 函数用途: 对C执行完整原单库本地事务，SQLite锁忙零等待并保真实partial。
    def _coordinator_transaction(self, operation, *, secure_delete=False):
        # LLM: 仅原短window执行本库固定事务，不运输连接到门外；异常先保存再原FD清理。
        # 函数用途: 完成一次BEGIN到commit的本地事务及同连接关闭。
        def commit():
            conn = self._connect_coordinator()
            primary = None
            try:
                if secure_delete:
                    conn.execute("PRAGMA secure_delete=ON")
                conn.execute("BEGIN IMMEDIATE")
                result = operation(conn)
                conn.commit()
                return result
            except BaseException as exc:
                primary = exc
                raise
            finally:
                _retire_coordinator_connection(conn, primary)
        return self._local_write(commit)

    # LLM: 原events锁先在门外取得；仅其FD准确出生入末门，flock/身份重试/close仍门外。
    # 函数用途: 为原SQL事件追加预先持有相同JSONL双层锁。
    def _coordinator_events_lock(self):
        return locked_json_path(self.events_path, birth_commit=self._local_write)

    # LLM: 只接受本Store固定events_path且调用方已持原锁；嵌套复用原canonical window，不重取锁或另选来源。
    # 函数用途: 在原SQL INSERT之后按同一事件编码追加已持锁JSONL。
    def _append_coordinator_event_records(self, path, records, *, sort_keys=True):
        if path != self.events_path:
            raise ValueError("local_store_event_path_changed")
        return self._local_write(lambda: append_private_jsonl_records_unlocked(
            self.events_path, records, sort_keys=sort_keys))


# LLM: 只清理原已打开连接，不新连接、不新增业务DML；rollback/close全部尝试，有主异常时不覆盖其对象。
# 函数用途: 正常或撤权退出时精确归还本次短连接，避免清理中断泄漏FD。
def _retire_coordinator_connection(conn, primary=None):
    cleanup_error = None
    try:
        if conn.in_transaction:
            conn.rollback()
    except BaseException as exc:
        cleanup_error = exc
    try:
        conn.close()
    except BaseException as exc:
        cleanup_error = cleanup_error if cleanup_error is not None else exc
    if primary is None and cleanup_error is not None:
        raise cleanup_error
