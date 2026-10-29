# LLM: C existing_only不切WAL、不建库且独立零等待连接；普通/read_only/F1连接预算和schema唯一实现保持。
# 模块用途: 在原SQLite连接入口区分现存库短提交，不增加schema或迁移分支。
"""Owner runtime.db 权威 schema（3.txt A 节主链）。

表关系（A.4 主链 Task→TaskRun→AgentRun tree→AgentAttempt）：
    tasks 1─N task_runs 1─N agent_runs（parent_agent_run_id 成树，''=root）
                              │  1─N agent_attempts（current pointer 在 agent_runs）
                              ├─1 delegations（child→parent 的 immutable 委托链，A.7）
                              └─1 workspace_bindings（D 节，可读写根集合）
    root_claims：同一 owner 库内唯一物理写根声明（D.5）
    runtime_events：append-only 权威事件流（A.3），agent_events 投影由其重建

与 local_storage（records/agent_runs 投影库）物理隔离：本库是权威，
投影库任何字段不得反向决定权威状态（A.2）。
"""

# LLM: schema 只为 Task 身份和真实执行/工具/投递状态建表；只读连接不得初始化或切 WAL，退役表列不再创建或读取。
#   持久结构摘要由 SQL 与迁移清单自动生成，只跳过相同结构的初始化；显式请求作用域不改变 transaction 的独立提交。
# 模块用途: 创建并幂等迁移 owner 的运行事实表，为只读查询提供禁止写入的连接。

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..contracts.optimization_contracts import require_schema_generation
from ..io.sqlite_schema import schema_fingerprint, schema_is_current, stamp_schema
from ..io.sqlite_scope import scoped_connection, sqlite_connection_factory

#: 每个 owner 的权威库文件名，位于 owner home 根（A.1 唯一 runtime.db）。
RUNTIME_DB_FILENAME = "runtime.db"


def runtime_db_path(home_root: Path) -> Path:
    """owner home 根 → 权威库路径。"""
    return Path(home_root) / RUNTIME_DB_FILENAME


_BASE_RUNTIME_SQL = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        thread_id TEXT NOT NULL DEFAULT '',
        conversation_task_id TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        goal TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_runs (
        task_run_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        closed_at REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id)",
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        agent_run_id TEXT PRIMARY KEY,
        task_run_id TEXT NOT NULL,
        parent_agent_run_id TEXT NOT NULL DEFAULT '',
        delegation_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        current_attempt_id TEXT NOT NULL DEFAULT '',
        current_attempt_generation INTEGER NOT NULL DEFAULT 0,
        workspace_epoch INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_task_run ON agent_runs(task_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_agent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_run_id ON agent_runs(run_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS agent_attempts (
        attempt_id TEXT PRIMARY KEY,
        agent_run_id TEXT NOT NULL,
        attempt_generation INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT '',
        started_at REAL NOT NULL,
        ended_at REAL NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(agent_run_id, attempt_generation)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_attempts_run ON agent_attempts(agent_run_id)",
    """
    CREATE TABLE IF NOT EXISTS delegations (
        delegation_id TEXT PRIMARY KEY,
        parent_agent_run_id TEXT NOT NULL,
        child_agent_run_id TEXT NOT NULL,
        child_attempt_id TEXT NOT NULL,
        granted_scope_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        UNIQUE(parent_agent_run_id, child_agent_run_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delegations_parent ON delegations(parent_agent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_delegations_child ON delegations(child_agent_run_id)",
    """
    CREATE TABLE IF NOT EXISTS workspace_bindings (
        binding_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        workspace_epoch INTEGER NOT NULL DEFAULT 1,
        root_path TEXT NOT NULL,
        readable_roots_json TEXT NOT NULL DEFAULT '[]',
        writable_roots_json TEXT NOT NULL DEFAULT '[]',
        extra_write_roots_json TEXT NOT NULL DEFAULT '[]',
        roots_digest TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workspace_bindings_run ON workspace_bindings(agent_run_id, status)",
    """
    CREATE TABLE IF NOT EXISTS root_claims (
        claim_id TEXT PRIMARY KEY,
        binding_id TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        root_path TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(root_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_root_claims_binding ON root_claims(binding_id)",
    """
    CREATE TABLE IF NOT EXISTS runtime_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        task_run_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runtime_events_attempt ON runtime_events(attempt_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_events_type ON runtime_events(event_type, created_at)",
    # 恢复页按序号前进；消费与重复事实按精确执行身份查找，不反复扫描全历史。
    "CREATE INDEX IF NOT EXISTS idx_runtime_events_type_seq ON runtime_events(event_type, seq)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_events_type_identity ON runtime_events(event_type, agent_run_id, attempt_id, seq)",
    # ---------------------------------------------------------------- R2（G/H 节）
    # ToolOperation 状态机（G.1）：CLAIMED→EXECUTING(CAS handler_started_at)→
    # SUCCEEDED/FAILED/CANCELLED；EXECUTING 后无法证明零副作用一律 UNKNOWN（G.4）。
    """
    CREATE TABLE IF NOT EXISTS tool_operations (
        operation_id TEXT PRIMARY KEY,
        agent_run_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        attempt_generation INTEGER NOT NULL,
        tool_operation_generation INTEGER NOT NULL,
        operation_type TEXT NOT NULL,
        canonical_scope TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'CLAIMED',
        handler_started_at REAL NOT NULL DEFAULT 0,
        settled_at REAL NOT NULL DEFAULT 0,
        outcome_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_operations_attempt ON tool_operations(attempt_id)",
    "CREATE INDEX IF NOT EXISTS idx_tool_operations_run ON tool_operations(agent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_tool_operations_status ON tool_operations(status)",
    # 资源锁（G.6）：holder instance + PID/start token + attempt generation +
    # workspace_epoch + lease。UNIQUE(canonical_scope) = 同一资源至多一个持有者。
    """
    CREATE TABLE IF NOT EXISTS resource_locks (
        lock_id TEXT PRIMARY KEY,
        canonical_scope TEXT NOT NULL UNIQUE,
        holder_instance TEXT NOT NULL,
        pid INTEGER NOT NULL DEFAULT 0,
        start_token TEXT NOT NULL DEFAULT '',
        attempt_id TEXT NOT NULL,
        attempt_generation INTEGER NOT NULL,
        workspace_epoch INTEGER NOT NULL,
        tool_operation_generation INTEGER NOT NULL DEFAULT 0,
        lease_expires_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_resource_locks_lease ON resource_locks(lease_expires_at)",
    # 资源 mutation 账（G.12）：canonical scope + version + MUTATING/STABLE/DIRTY。
    # unknown/unscoped 写置 DIRTY → reconcile 前阻止发布等不可逆动作（G.14）。
    """
    CREATE TABLE IF NOT EXISTS resource_mutations (
        mutation_id TEXT PRIMARY KEY,
        canonical_scope TEXT NOT NULL UNIQUE,
        version INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'STABLE',
        dirty_reason TEXT NOT NULL DEFAULT '',
        attempt_id TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL
    )
    """,
    # PublishOperation（H.2/H.7）：staging→共享唯一通道；崩溃只允许落
    # COMMITTED 或 DIRTY/UNKNOWN，不能谎称原子成功。
    """
    CREATE TABLE IF NOT EXISTS publish_operations (
        publish_id TEXT PRIMARY KEY,
        binding_id TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        workspace_epoch INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'STAGING',
        manifest_json TEXT NOT NULL DEFAULT '[]',
        preimage_digests_json TEXT NOT NULL DEFAULT '{}',
        postimage_digests_json TEXT NOT NULL DEFAULT '{}',
        committed_at REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_publish_ops_attempt ON publish_operations(attempt_id)",
    # ArtifactRecord（H.8/H.9）：内容寻址 immutable artifact snapshot，发布完成
    # 后才写；validator 只验证这里引用的 digest。主键必须是独立
    # artifact_record_id（G2 补，3.txt:303）：content_digest 只是内容 hash，
    # 两个不同路径即使内容相同也各有一条记录；content_path 指向内容寻址
    # store（publish 时冻结，validator 只读该对象，不读 live workspace）。
    """
    CREATE TABLE IF NOT EXISTS artifact_records (
        artifact_record_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        publish_id TEXT NOT NULL DEFAULT '',
        rel_path TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        content_path TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_artifact_records_attempt ON artifact_records(attempt_id)",
    # ---------------------------------------------------------------- G1 补（3.txt B.3）
    # opaque ID → 框架目录 ID 权威映射：ID 不直接成为物理路径权威（B.3）。
    # 任何把 run_id/task_id/session_id/attempt_id/delegation_id 拼进路径的
    # 公开入口先过 validate_opaque_id（拒绝式），再把映射登记进本表；
    # directory_id 由框架生成（首次登记即固定，重复登记幂等）。目录名
    # 轮换/迁移只改本表，不碰调用方拼接点。
    """
    CREATE TABLE IF NOT EXISTS id_path_mapping (
        opaque_id TEXT PRIMARY KEY,
        id_kind TEXT NOT NULL,
        directory_id TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_path_mapping_kind ON id_path_mapping(id_kind)",
    # ---------------------------------------------------------------- wake_queue
    # 调度唤醒字条(2026-08-17 扫描治理, owner 拍板): "任务自己留的闹钟"。
    # 模型 sleep/任务收口等待/goal tick/子代理等待时写一行"几点醒"; 调度器
    # hot tick 只查 next_due_at<=now 的到期行(索引查询, 不翻任务目录)。
    # 每任务一行待醒字条(upsert 幂等); 事件(用户消息/子代理完成)可提前置醒。
    # woke 后由消费者核任务档案(link/state.json/runtime.db)再拉起——
    # 档案证明已终结 → 清行(僵尸即清, 不累积)。
    """
    CREATE TABLE IF NOT EXISTS wake_queue (
        wake_id TEXT PRIMARY KEY,
        root_task_id TEXT NOT NULL,
        root_run_id TEXT NOT NULL DEFAULT '',
        root_thread_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'sleep',
        next_due_at REAL NOT NULL,
        woke_at REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        -- WK-INT(2026-08-20): wake 持久化状态机升级——一次性闹钟 → 可恢复待办。
        -- claimed 期间带 lease；失败回 pending 并按 retry_after 退避重试；
        -- 429 限流把 retry_after 推到恢复点；进程崩溃后 lease 过期由 reclaim 回收。
        lease_until REAL NOT NULL DEFAULT 0,
        retry_after REAL NOT NULL DEFAULT 0,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wake_queue_due ON wake_queue(status, next_due_at)",
    "CREATE INDEX IF NOT EXISTS idx_wake_queue_task ON wake_queue(root_task_id)",
    # idx_wake_queue_lease 在迁移末尾确保（新列可能由迁移 ALTER 补上，此处建会失败）
    # ---------------------------------------------------------------- R4（K 节）
    # Outbox（K.3/K.4/K.6）：同库状态用本地事务；跨进程/外部副作用走
    # at-least-once 投递 + effect_key 去重，不宣称 exactly-once（K.2）。
    # status：PENDING/IN_FLIGHT/ACKED/FAILED/DEAD_LETTER。attempts 超限
    # 进 DEAD_LETTER（可查询、可人工重放、evidence 保留完整证据）。
    """
    CREATE TABLE IF NOT EXISTS outbox_entries (
        outbox_id TEXT PRIMARY KEY,
        effect_key TEXT NOT NULL UNIQUE,
        owner_id TEXT NOT NULL DEFAULT '',
        task_run_id TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'PENDING',
        attempts INTEGER NOT NULL DEFAULT 0,
        claimed_at REAL NOT NULL DEFAULT 0,
        claimed_by TEXT NOT NULL DEFAULT '',
        next_retry_at REAL NOT NULL DEFAULT 0,
        provider_evidence_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        settled_at REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outbox_status_retry ON outbox_entries(status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_task_run ON outbox_entries(task_run_id)",
    # Inbox（K.4）：跨边界到达消息，effect_key UNIQUE 去重（at-least-once
    # 语义下重复投递只处理一次）。
    """
    CREATE TABLE IF NOT EXISTS inbox_entries (
        inbox_id TEXT PRIMARY KEY,
        effect_key TEXT NOT NULL UNIQUE,
        sender TEXT NOT NULL DEFAULT '',
        task_run_id TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'RECEIVED',
        received_at REAL NOT NULL,
        processed_at REAL NOT NULL DEFAULT 0
    )
    """,
)


#: 存量库幂等迁移（CREATE TABLE IF NOT EXISTS 不更新既有表结构）。
#: 每项为 (检查列, 表, ALTER SQL)；列已存在则跳过，多次启动安全。
#: 并发安全见 _apply_runtime_migrations（duplicate column 视为已达成）。
_RUNTIME_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # WK-INT(2026-08-20): wake_queue 持久化状态机迁移——旧表(一次性闹钟)
    # 无新列, 升级后必须 ALTER 补齐, 否则新代码读写崩溃。
    (
        "lease_until",
        "wake_queue",
        (
            "ALTER TABLE wake_queue ADD COLUMN lease_until REAL NOT NULL DEFAULT 0",
            "ALTER TABLE wake_queue ADD COLUMN retry_after REAL NOT NULL DEFAULT 0",
            "ALTER TABLE wake_queue ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE wake_queue ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
        ),
    ),
    (
        "closed_at",
        "task_runs",
        "ALTER TABLE task_runs ADD COLUMN closed_at REAL NOT NULL DEFAULT 0",
    ),
    # G2 补：artifact_records 主键独立化迁移（3.txt:303）。顺序敏感：
    # 1) 加 content_digest 列 + 同项回填旧行（旧 artifact_id 值即原 digest；
    #    回填绑定在 ADD 的条件上——列刚加的这次启动才执行，之后跳过；
    #    此时列名仍叫 artifact_id，回填引用旧名）→ 2) 加 content_path →
    #    3) 主键列改名 artifact_id→artifact_record_id（RENAME 保留主键
    #    约束，sqlite ≥ 3.25）。全新建库已用新 schema，三项全部跳过。
    (
        "content_digest",
        "artifact_records",
        (
            "ALTER TABLE artifact_records ADD COLUMN content_digest TEXT NOT NULL DEFAULT ''",
            "UPDATE artifact_records SET content_digest = artifact_id "
            "WHERE content_digest = ''",
        ),
    ),
    (
        "content_path",
        "artifact_records",
        "ALTER TABLE artifact_records ADD COLUMN content_path TEXT NOT NULL DEFAULT ''",
    ),
    (
        "artifact_record_id",
        "artifact_records",
        "ALTER TABLE artifact_records RENAME COLUMN artifact_id TO artifact_record_id",
    ),
)


_RUNTIME_MIGRATION_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_wake_queue_lease ON wake_queue(status, lease_until)",
    "CREATE INDEX IF NOT EXISTS idx_task_runs_closed_at ON task_runs(closed_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_run_id ON agent_runs(run_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_current_attempt ON agent_runs(current_attempt_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_attempts_status_ended ON agent_attempts(status, ended_at)",
    "CREATE INDEX IF NOT EXISTS idx_tool_operations_status ON tool_operations(status)",
    "CREATE INDEX IF NOT EXISTS idx_resource_locks_attempt ON resource_locks(attempt_id)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_events_task_run ON runtime_events(task_run_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_resource_mutations_state ON resource_mutations(state)",
)


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(
            row["name"] == column
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
    except sqlite3.OperationalError:
        return False


# LLM: metadata是原runtime.db唯一header；只查询，不建表，不将忙或坏库当代际0。先发R1到所有写者后才允许提升代际。
# 函数用途: 每次借用可写连接前拒绝较新存储，旧库缺header按0处理。
def require_runtime_schema_generation(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone() is None:
        return
    row = conn.execute("SELECT value FROM metadata WHERE key='schema_generation'").fetchone()
    if row is not None:
        value = row[0]
        if not isinstance(value, str) or not value.isdecimal():
            raise ValueError('STORE_SCHEMA_INVALID')
        require_schema_generation({'schema_generation': int(value)})


# LLM: RuntimeSchemaMixin 只向前补当前仍被消费的列；禁止为了清理旧库而
# 自动 DROP，避免升级时破坏用户历史，旧 acceptance 表仅作为惰性遗留数据。
#   完成摘要与初始化同事务，连接工厂只在显式作用域跟踪语句，不改变独立 transaction 调用方。
# 类用途: 初始化 runtime.db、执行安全的幂等迁移并提供 SQLite 连接。
class RuntimeSchemaMixin:
    """连接与建表。模式对齐 local_storage.schema（WAL+外键+busy_timeout）。"""

    # LLM: 先读持久摘要，未命中时用写事务再次核对；迁移和盖章同提交，不引入进程内 owner 缓存。
    # 函数用途: 新库或旧副本才建表迁移，正常重复构造只查询一次结构摘要。
    def _init_runtime_schema(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        fingerprint = schema_fingerprint((_BASE_RUNTIME_SQL, _RUNTIME_MIGRATIONS, _RUNTIME_MIGRATION_SQL))
        with self._runtime_connection() as conn:
            if schema_is_current(conn, fingerprint):
                return
            conn.execute("BEGIN IMMEDIATE")
            if schema_is_current(conn, fingerprint):
                return
            self._execute_runtime_schema(conn, _BASE_RUNTIME_SQL)
            self._apply_runtime_migrations(conn)
            stamp_schema(conn, fingerprint)
            conn.commit()

    # LLM: 迁移只补当前代码仍消费的列；旧功能残表保留为惰性历史，不能在
    # 启动时做破坏性清库；迁移后索引也由声明 SQL 执行并进入完成摘要。
    # 函数用途: 幂等升级存量 runtime.db，并处理并发初始化竞态。
    @staticmethod
    def _apply_runtime_migrations(conn: sqlite3.Connection) -> None:
        """幂等迁移存量库，只补当前运行时仍需要的列。

        并发安全：多个连接可能同时初始化同一库（Gateway/后台 worker 共用
        owner 权威库）。目标态是「列存在」：
        - 检测到列 → 跳过；
        - 未检测到 → ALTER；并发窗口内另一连接已 ALTER（duplicate）→
          视为迁移已达成，不抛错。
        """
        for column, table, alter_sql in _RUNTIME_MIGRATIONS:
            if _table_has_column(conn, table, column):
                continue
            # 迁移项支持单条 SQL 或同条件多语句元组（G2 回填绑定 ADD 条件）。
            statements = alter_sql if isinstance(alter_sql, tuple) else (alter_sql,)
            for statement in statements:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
                    # 并发连接已先完成本列迁移，目标态已达成。
        # WK-INT: 状态机索引在迁移后统一确保（列可能由 ALTER 补上，基础
        # schema 阶段建索引会因列缺失失败；CREATE IF NOT EXISTS 幂等）。
        RuntimeSchemaMixin._execute_runtime_schema(conn, _RUNTIME_MIGRATION_SQL)

    @staticmethod
    def _execute_runtime_schema(conn: sqlite3.Connection, statements: tuple[str, ...]) -> None:
        for statement in statements:
            conn.execute(statement)

    # LLM: ro和C existing_only都禁初始化/WAL切换；后者mode=rw不建缺库且busy_timeout=0，不在原C短门等SQL锁。
    #   每次真实 connect 才设 PRAGMA；显式请求作用域内工厂跟踪游标，其余保持标准库连接。
    # 函数用途: 打开对应访问模式的物理连接，写模式沿既有 WAL 与外键设置。
    def _runtime_connect(self) -> sqlite3.Connection:
        read_only = getattr(self, "read_only", False)
        existing_only = getattr(self, "existing_only", False)
        mode = "ro" if read_only else "rw"
        address = self.db_path.resolve().as_uri() + "?mode=" + mode if read_only or existing_only else str(self.db_path)
        timeout = 0.0 if existing_only else 30.0
        conn = sqlite3.connect(address, timeout=timeout, uri=read_only or existing_only, factory=sqlite_connection_factory())
        conn.row_factory = sqlite3.Row
        if not read_only:
            try:
                require_runtime_schema_generation(conn)
            except BaseException:
                conn.close()
                raise
        conn.execute("PRAGMA busy_timeout=" + ("0" if existing_only else "30000"))
        if not read_only and not existing_only:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # LLM: 原F1只按path/read_only缓存；新增C existing_only须独立短连接，不能继承或污染原30秒rw预算。
    #   Count0/read_only及transaction原生命周期保持，不flush旧缓存，不自动提交。
    # 函数用途: 借用原库连接，C现存库写入结束即关闭，正常请求仍用原F1作用域。
    @contextmanager
    def _runtime_connection(self) -> Iterator[sqlite3.Connection]:
        if getattr(self, "existing_only", False):
            conn = self._runtime_connect()
            try:
                yield conn
            finally:
                conn.close()
            return
        with scoped_connection(self.db_path, getattr(self, "read_only", False), self._runtime_connect) as conn:
            if not getattr(self, 'read_only', False):
                require_runtime_schema_generation(conn)
            yield conn
