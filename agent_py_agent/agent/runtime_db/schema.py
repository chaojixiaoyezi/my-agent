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

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
        status TEXT NOT NULL DEFAULT 'active',
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
        current_contract_id TEXT NOT NULL DEFAULT '',
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
    # ------------------------------------------------- continuation_handoffs
    # CLI 自动续跑显式移交(2026-08-14 长任务首要约束, owner seq1856 + steward
    # seq1857): CLI 预算耗尽/正常收口时写待消费移交单, gateway 调度器扫描
    # 接管续跑——owner/task 级共享权威(runtime.db, 不依赖进程 cwd 或入口
    # 私有 conversation store)。consumed_at=0 且 lease 未过期 = 待接管;
    # consume 是 CAS(consumed_at=0 条件更新)保证不双消费。
    # 原子性语义(双席复核硬门1 seq1906): CLI 收口路径在同一进程内顺序执行
    # ——budget_exhausted 事件落账 → 移交单写入 → CLI 退出; 单线程无并发
    # 窗口, 三者顺序原子(任一步失败, 任务保留非终态, gateway 凭 runtime.db
    # 的 created 状态 + 已落账事件可重建移交——移交单幂等创建兜底)。
    # claim 释放: CLI 正常退出 = 前台 claim lease 自然过期; gateway 领取
    # 时同一 policy 锁内 CAS 双向检查(cli_claim_at/gateway_claim_at lease),
    # 不会与存活 claim 并发执行。
    "CREATE TABLE IF NOT EXISTS continuation_handoffs ("
    " handoff_id TEXT PRIMARY KEY,"
    " task_run_id TEXT NOT NULL,"
    " agent_run_id TEXT NOT NULL,"
    " attempt_id TEXT NOT NULL,"
    " root_run_id TEXT NOT NULL DEFAULT '',"
    " root_request_id TEXT NOT NULL DEFAULT '',"
    " root_thread_id TEXT NOT NULL DEFAULT '',"
    " root_task_id TEXT NOT NULL DEFAULT '',"
    " user_prompt TEXT NOT NULL DEFAULT '',"
    " continuation_seq INTEGER NOT NULL DEFAULT 0,"
    " reason TEXT NOT NULL DEFAULT '',"
    " created_at REAL NOT NULL,"
    " consumed_at REAL NOT NULL DEFAULT 0,"
    " consumed_by TEXT NOT NULL DEFAULT ''"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_handoffs_pending "
    " ON continuation_handoffs(consumed_at, created_at)",
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
    # unknown/unscoped 写置 DIRTY → reconcile 前阻止发布/验收/交付（G.14）。
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
    # ---------------------------------------------------------------- R3（I 节）
    # AcceptanceContract（I.2/I.6）：dispatch 前由框架编译、校验、冻结，
    # 不可变；current_contract_id 在 task_runs 上 CAS 防分叉。模型只能
    # propose assertions（I.1）；command/cwd/working_dir/裸程序结构性
    # 不进契约（I.3），只作 inert evidence（I.4，读取不触发 subprocess）。
    """
    CREATE TABLE IF NOT EXISTS acceptance_contracts (
        contract_id TEXT PRIMARY KEY,
        task_run_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'FROZEN',
        compiled_json TEXT NOT NULL DEFAULT '{}',
        digest TEXT NOT NULL,
        inert_legacy_json TEXT NOT NULL DEFAULT '{}',
        frozen_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_contracts_task_run ON acceptance_contracts(task_run_id)",
    # ValidatorOperation（A.8/I.8/I.9）：每次 validator 执行追到具体
    # attempt；记录 code digest/argv/env/artifact digests/stdout/stderr。
    # status：VERIFIED/FAILED/UNAVAILABLE/BLOCKED（sandbox 不可用 fail
    # closed，不降级为 advisory）。
    # G2 补尾：assertion_key 为断言稳定标识（validator_ref::artifact_kind，
    # 编译时生成），行级绑定具体断言——同契约同 ref 不同 kind 的多断言
    # 可区分（3.txt「不能只记 validator_ref」）；空 = 升级前的旧行（按
    # ref 兜底匹配，见 closeout 判定）。
    """
    CREATE TABLE IF NOT EXISTS validator_operations (
        operation_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL,
        agent_run_id TEXT NOT NULL,
        contract_id TEXT NOT NULL DEFAULT '',
        assertion_key TEXT NOT NULL DEFAULT '',
        validator_ref TEXT NOT NULL,
        validator_kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        code_digest TEXT NOT NULL DEFAULT '',
        argv_json TEXT NOT NULL DEFAULT '[]',
        env_json TEXT NOT NULL DEFAULT '{}',
        artifact_digests_json TEXT NOT NULL DEFAULT '[]',
        stdout_text TEXT NOT NULL DEFAULT '',
        stderr_text TEXT NOT NULL DEFAULT '',
        exit_code INTEGER NOT NULL DEFAULT -1,
        started_at REAL NOT NULL DEFAULT 0,
        settled_at REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_validator_ops_attempt ON validator_operations(attempt_id)",
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
    # ------------------------------------------------ wake_intents（#233）
    # 叫醒意图单一权威状态机（规格 docs/design/WAKE_INTENT_SCHEDULING_SPEC.md）：
    # 唤醒只认信号源（入站/cron/heartbeat/sleep/子代理完成/中断恢复），来源统一
    # 写 intent，dispatcher 唯一消费。字段/状态机/CAS/dedup 按规格 §1-§3。
    # intent 只存受控 ref，不存 prompt/正文/密钥/大 payload（§8）。
    """
    CREATE TABLE IF NOT EXISTS wake_intents (
        intent_id TEXT PRIMARY KEY,
        dedup_key TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        parent_run_id TEXT NOT NULL DEFAULT '',
        root_run_id TEXT NOT NULL DEFAULT '',
        attempt_id TEXT NOT NULL DEFAULT '',
        owner_id TEXT NOT NULL,
        execution_mode TEXT NOT NULL DEFAULT 'interactive',
        source TEXT NOT NULL,
        wake_reason TEXT NOT NULL DEFAULT '',
        source_event_id TEXT NOT NULL DEFAULT '',
        retry_event_id TEXT NOT NULL DEFAULT '',
        provenance_ref TEXT NOT NULL DEFAULT '',
        provider_scope_ref TEXT NOT NULL DEFAULT '',
        continuation_policy TEXT NOT NULL DEFAULT '',
        policy_generation INTEGER NOT NULL DEFAULT 0,
        payload_schema_version TEXT NOT NULL DEFAULT 'v1',
        priority INTEGER NOT NULL DEFAULT 0,
        not_before REAL NOT NULL DEFAULT 0,
        next_wake_at REAL NOT NULL,
        due_window TEXT NOT NULL DEFAULT '',
        retry_after REAL NOT NULL DEFAULT 0,
        expires_at REAL,
        status TEXT NOT NULL DEFAULT 'pending',
        claim_generation INTEGER NOT NULL DEFAULT 0,
        claim_token TEXT NOT NULL DEFAULT '',
        lease_owner TEXT NOT NULL DEFAULT '',
        lease_until REAL NOT NULL DEFAULT 0,
        claimed_at REAL NOT NULL DEFAULT 0,
        handed_off_at REAL NOT NULL DEFAULT 0,
        finished_at REAL NOT NULL DEFAULT 0,
        handoff_id TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL DEFAULT '',
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error_ref TEXT NOT NULL DEFAULT '',
        cancelled_reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wake_intents_due ON wake_intents(status, next_wake_at)",
    "CREATE INDEX IF NOT EXISTS idx_wake_intents_owner ON wake_intents(owner_id, status)",
)


#: 存量库幂等迁移（CREATE TABLE IF NOT EXISTS 不更新既有表结构）。
#: 每项为 (检查列, 表, ALTER SQL)；列已存在则跳过，多次启动安全。
#: 并发安全见 _apply_runtime_migrations（duplicate column 视为已达成）。
_RUNTIME_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "current_contract_id",
        "task_runs",
        "ALTER TABLE task_runs ADD COLUMN current_contract_id TEXT NOT NULL DEFAULT ''",
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
    # G2 补尾：validator operation 绑定断言标识（旧库补列，新库 CREATE 已带）。
    (
        "assertion_key",
        "validator_operations",
        "ALTER TABLE validator_operations ADD COLUMN assertion_key TEXT NOT NULL DEFAULT ''",
    ),
)


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(
            row["name"] == column
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
    except sqlite3.OperationalError:
        return False


class RuntimeSchemaMixin:
    """连接与建表。模式对齐 local_storage.schema（WAL+外键+busy_timeout）。"""

    def _init_runtime_schema(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._runtime_connection() as conn:
            self._execute_runtime_schema(conn, _BASE_RUNTIME_SQL)
            self._apply_runtime_migrations(conn)
            conn.commit()

    @staticmethod
    def _apply_runtime_migrations(conn: sqlite3.Connection) -> None:
        """幂等迁移存量库（R1/R2 建的库无 current_contract_id）。

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

    @staticmethod
    def _execute_runtime_schema(conn: sqlite3.Connection, statements: tuple[str, ...]) -> None:
        for statement in statements:
            conn.execute(statement)

    def _runtime_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _runtime_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._runtime_connect()
        try:
            yield conn
        finally:
            conn.close()
