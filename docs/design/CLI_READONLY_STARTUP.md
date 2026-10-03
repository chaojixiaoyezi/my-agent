# 只读 CLI 的只读启动（CLI_READONLY_STARTUP）

- **状态**：设计稿，未实施（2026-10-03，sol3，分支 `worker/sol3-cli-ro`，基于 be 的 H3 分支 `claude/be-host-config-guard` `d160a2c6d`）。
- **复审**：be（H3 作者）。
- **范围**：只读调研 + 设计，不改产品代码。本文只回答“怎么做”，不包含实现。
- **关联**：`docs/design/HOST_CONFIG_WRITE_GUARD.md` 第 3 节第 8 条（模型在命令里跑 CLI 会失败）、第 7 节“必须改 2”的后续项（只读子命令走只读启动，记台账待做）。

## 0. 结论摘要

1. 8 条命令失败的直接原因不在命令本身，而在每条命令都要先走 `cli/common.py::make_agent` 构造完整 `SimpleAgent`；构造过程中至少有 6 处“启动就写”，其中 `LocalStore._init_schema`（`local_storage/schema.py`）是必然命中的第一个硬写点：建目录、`sqlite3.connect` 建库、`PRAGMA journal_mode=WAL`、五组 `CREATE TABLE`、FTS 虚拟表、迁移与回填。
2. 这 8 条命令里，7 条“命令本身”只有 SELECT/文件读取；`subagents` 例外——它调 `board.write_board()`，本身要写两个文件（`subagent_board.json`、`SUBAGENT_BOARD.md`）。`runtime-stale-attempts --settle` 也是显式写，但默认只读。只读启动方案必须把“命令本身要写”与“启动就写”分开处理。
3. 方案核心：命令注册处用**结构化声明**（`set_defaults` 上的只读字段）标记只读命令；`make_agent` 读到声明后构造“只读启动”的 `SimpleAgent`：`LocalStore` 以 `mode=ro` 打开、`_init_schema` 推迟到真要写时才跑、跳过 home 目录/种子文件的 ensure、跳过一切迁移和维护；版本或迁移对不上时报结构化错误，不在只读路径里悄悄升级。
4. 拆成 5 个可实施小块，估 5 人日（详见第 3 节）。

## 1. 现状：8 条命令的写入点清单

命令入口统一是 `cli/main_entry.py::main`（解析后 `args.func(args)`），每条命令的第一行都是 `agent = make_agent(args)`（`cli/common.py:45-59`），再走 `SimpleAgent.__init__`（`agent/core.py:488-598`）。因此“启动就写”对所有命令相同，逐条差异只在“命令本身要写”。

### 1.1 共同启动链（所有 8 条命令都走）

按 `SimpleAgent.__init__` 实际执行顺序列出；“写什么”一列是文件/数据库层面的效果。行号对应当前分支（基于 H3 `d160a2c6d`）。

| # | 位置（文件:行，函数） | 写什么 | 备注 |
| --- | --- | --- | --- |
| S1 | `agent/core.py:508` `self.root.mkdir(parents=True, exist_ok=True)` | 建工作区根目录（不存在时） | Full Access 下 root 可能是用户项目目录；隔离 owner 下是 owner home，沙箱只读时 EPERM/EROFS |
| S2 | `agent/core.py:860-865` `_resolve_home_paths` → `ensure_my_agent_home` + `ensure_owner_home` | 建数据根/owner home 的全部目录 | `user_space/owner_resolver.py:111-120`：逐个 `mkdir` + 写种子文件 + 写种子 JSON（permissions/quota 等） |
| S3 | `agent/core.py:874-878` `_register_owner_ref_if_possible` → `register_owner_ref` | 写 owner 注册引用（失败吞 `OSError`） | 只捕 `OSError`；其它异常仍会冒泡 |
| S4 | `agent/core.py:533` → `agent/core.py:310-344` `_wire_memory_authorities` → `agent/local_storage/store.py:34-49` `LocalStore.__init__` → `local_storage/schema.py:201-229` `_init_schema` | **本地库全量启动写**（本项主犯） | 见下表 S4a-S4h |
| S5 | `agent/core.py:556` `ConversationStore(paths["conversation_workspace"])` | 会话工作区（`runtime/workspaces/<scope>/conversations`）构造副作用：建目录/建库 | 构造内部待实施时逐函数核对，标注 [待核对] |
| S6 | `agent/core.py:559` `_wire_memory_curator` | `DailyMemoryStore`、`MemoryCuratorStateStore`、`CuratorRunLog`、`MemoryMigrationService` 构造；curator 运行目录 | 构造是否写盘待逐函数核对 [待核对]；迁移服务只在 curator 持 lease 时应用，不在启动跑 |
| S7 | `agent/core.py:560` `CollaborationStore(...)` | 协作账本工作区（`runtime/.../collaboration`）构造副作用 | [待核对] |
| S8 | `agent/core.py:561-572` `SchedulerRepository(...)` + `SchedulerDueIndex(...)` | `global_index_dir/scheduler_due.sqlite3` 等 | 构造可能建库/建索引 [待核对] |
| S9 | `agent/core.py:580` `_build_subagent_manager` → `agent/subagents/manager.py:83+` `SubAgentManager(...)` | 打开 owner `runtime.db`（A 类受保护文件） | `agent.subagents.runtime_db` 是权威运行库；打开方式（是否写模式、是否建表）实施时确认 [待核对] |
| S10 | `agent/core.py:593` `_build_tool_registry`；`:594` `load_extension_registry`；`:595` `extensions.activate_agent`；`:596` `_register_orchestration_tools` | 插件注册/激活副作用；编排工具注册 | 默认无插件时基本无写 [待核对] |

**S4 展开（`LocalStore._init_schema`，`local_storage/schema.py:201-229`）**：

| # | 行号 | 写什么 |
| --- | --- | --- |
| S4a | `schema.py:202-204` | `self.root.mkdir`、`files_dir.mkdir`、`events_path.parent.mkdir`（即 `workspace/runtime/workspaces/<scope>/local_store/` 及其 `files/`） |
| S4b | `schema.py:295-305` `_connect` | `sqlite3.connect(db_path)`——库文件不存在时**直接建库**；`PRAGMA busy_timeout=30000`；`PRAGMA journal_mode=WAL`（写 WAL 模式，落 `local.db-wal`/`-shm`）；`PRAGMA foreign_keys=ON` |
| S4c | `schema.py:210` `_execute_schema(_BASE_SCHEMA_SQL)` | 建基础表/索引（`CREATE TABLE/INDEX IF NOT EXISTS`，含 `records`、`events`、`metadata`） |
| S4d | `schema.py:211` `_migrate_legacy_agent_runs_rename`（231-245） | 旧库 `ALTER TABLE agent_runs RENAME TO legacy_agent_runs` |
| S4e | `schema.py:212-215` | `_TASK_REGISTRY_SQL`、`_CONTROL_PLANE_SQL`、`_RUNTIME_GATE_LEDGER_SQL`、`_TOOL_OPERATIONS_SQL` 四组建表 |
| S4f | `schema.py:216-217`、`251-263` `_init_fts_schema` | FTS：`_migrate_fts_schema` 版本不符时 `DROP TABLE records_fts`；`CREATE VIRTUAL TABLE records_fts`；`INSERT ... metadata` 写 FTS 版本 |
| S4g | `schema.py:218` `conn.commit()` | 提交以上所有 schema 变更 |
| S4h | `schema.py:222-228` `rebuild_fts()` | FTS 重建回填：读 records 内容文件并写索引（维护动作） |

补充：`local_store_path` 的默认位置是 `runtime_root / "local_store" / "local.db"`，其中 `runtime_root = <owner_workspace>/runtime/workspaces/<workspace_scope_id>`（`agent/user_space/runtime_paths.py:69-80、112-126`）；`subagent_workspace` 在 `runtime_root/subagents`，`conversation_workspace` 在 `runtime_root/conversations`。这些都在 H3 的 A 类保护面内（`workspace/runtime/`）。

### 1.2 逐条命令：命令本身的写入点

判定口径：读 `cmd_*` 函数体及其直接调用的服务函数；“启动就写”指 1.1 节共同链，“命令本身要写”指执行阶段额外的写。凡未能逐函数核对的标 [待核对]，实施前必须复核。

| 命令 | 注册/入口 | 执行阶段读什么 | 命令本身要写 |
| --- | --- | --- | --- |
| `status` | `cli/local_commands.py:66-118` `cmd_status` | `local_store.stats()`、`subagents.board.build_board()`、`local_store.timeline()`、Gateway `state.json`/`heartbeat.json`、`gateway_request_counts`、`detect_active_work` | 未见显式写。`build_board`（`subagents/services/board/service.py:74-113`）只读遍历；`detect_active_work` 需实施时复核 [待核对]。注意每个 `local_store` 查询都会走 `_connect`（S4b 的 PRAGMA WAL 写） |
| `home-status` | `cli/home_runtime_commands.py:123-127` `cmd_home_status` | `home_runtime_status(agent.home_paths)`（`user_space/home_runtime_query.py:75`）读入口文件/目录/计数 | 无 |
| `subagents` | `cli/_board.py:56-92` `cmd_subagents` | `subagents.board.write_board()` + `shared_progress_for_board` | **有**：`write_board` 写 `subagent_board.json` 和 `SUBAGENT_BOARD.md` 到 `agent.subagents.workspace`（`subagents/services/board/service.py:115-131`，即 `runtime_root/subagents/`，A 类）。命令末尾还打印“已写入” |
| `memory-list` | `cli/local_commands.py:318-325` `cmd_memory_list` | `agent.memory.all()`（长期记忆 JSONL 读取） | 未见显式写；`JsonlMemory` 构造副作用在启动链 [待核对] |
| `local-store-status` | `cli/local_commands.py:337-341` `cmd_local_store_status` | `agent.local_store.stats()`（`local_storage/maintenance.py:100-113`，两条 SELECT） | 无（除 `_connect` 的 PRAGMA） |
| `timeline` | `cli/local_commands.py:164-192` `cmd_timeline` | `agent.local_store.timeline()`（`local_storage/events.py:22`） | 未见显式写 [待核对 events.timeline 内部是否触碰维护] |
| `task-workspace-list` | `cli/home_runtime_commands.py:104-120` `cmd_task_workspace_list` | `list_task_workspaces`（`user_space/home_runtime_query.py:28-38`）读任务 `state` 文件 | 无 |
| `runtime-stale-attempts` | `cli/home_runtime_commands.py:155-176` `cmd_runtime_stale_attempts` | `repo.unidentified_stale_attempts()`（`runtime_db/repository.py:731+`，纯 SELECT） | 默认无；`--settle` 时调 `repo.settle_unidentified_attempts(...)` 写 `runtime.db`（显式结清，不是只读路径） |

### 1.3 汇总：启动写 vs 命令写

- **启动就写（8 条全中，必改）**：S1-S10，其中 S4（LocalStore schema）是硬写，S2（owner home ensure）在隔离 owner 下同样是硬写。H3 二审实测的失败路径就是这里。
- **命令本身要写（需单独裁决）**：
  - `subagents`：写 board 文件。若把它声明为只读命令，则只读启动下要么改写输出（不落盘、只打印），要么把它排除出只读名单（保持现状失败），由 3a/be 裁决；本设计建议：**保留写盘语义但把它排除出“只读启动”名单**，或在沙箱里自动降级为“只打印、跳过写盘”并打印明确提示（推荐后者，见 2.7）。
  - `runtime-stale-attempts --settle`：显式写。建议：只读启动只对“不带 `--settle`”的调用生效；带 `--settle` 走普通启动，沙箱里失败并给 `CLI_HOST_STATE_READ_ONLY`（现状语义不变）。
- **只读命令名单（首批 8 条，按声明判定，不按命令名猜）**：`status`、`home-status`、`subagents`（见上）、`memory-list`、`local-store-status`、`timeline`、`task-workspace-list`、`runtime-stale-attempts`。

## 2. 方案：只读命令走只读启动

### 2.1 “只读命令”怎么判定：命令注册处的结构化声明

- 现状：命令注册散在 `add_*_subcommands` 里，每个子命令用 `parser.set_defaults(func=cmd_x)` 挂处理函数（例：`home_runtime_commands.py:28`、`:68`、`:74`；`local_commands` 对应注册在 `subcommands_basic.py`；`subagents` 在 `cli/subagents.py`）。
- 方案：在同一个 `set_defaults` 调用里加一个结构化字段，例如 `set_defaults(func=cmd_x, cli_readonly=True)`。只读意图属于**命令声明**，与处理函数一起登记、一起评审；`main_entry.main` 只读 `getattr(args, "cli_readonly", False)`。
- 禁止用命令名集合判断（例如 `args.command in {...}`）：命令名是字符串，不是结构化声明；新增命令时容易漏登记且不可评审。8 条命令各自在自己的注册点声明，改动 diff 与声明同处一文件。
- 传递：`cli/common.py::make_agent(args)` 读取 `args.cli_readonly`，把 `readonly_startup=bool(...)` 传给 `SimpleAgent`（新关键字参数，默认 `False`，普通命令零行为变化）。**不放进 AgentConfig**：这是单次命令启动的运行态，不是用户配置，避免写进 YAML/dataclass 与“配置必须真的生效”的负担。
- 扩展命令（插件注册的 CLI 命令）：默认没有该字段 → 非只读，保持现状；插件以后要用需自己声明。

### 2.2 只读启动路径（`SimpleAgent(readonly_startup=True)`）

只读启动必须满足“不写一个字节”，逐点对应 1.1 节：

1. **S1 跳过** `self.root.mkdir`；root 不存在时不创建，直接按“目录缺失”报结构化错误（见 2.6）。
2. **S2 跳过 ensure**：新增只读解析路径——`resolve_owner_home` 只解析不建目录、不写种子文件。目录/文件缺失时**不补**，让命令按现有只读逻辑报告缺失（`home-status` 的 exists=false 本来就是它的输出语义）。
3. **S3 跳过** `register_owner_ref`（写注册引用）。
4. **S4**：`LocalStore(..., readonly=True)`——见 2.3；`_init_schema` 不在构造里跑，改由“首次写操作”触发（写路径保持原样，只读路径永不触发）。
5. **S5/S6/S7/S8**：只读启动时不构造会写盘的次级服务（会话/协作/调度/curator 运行目录），或构造“延迟/只读”变体。实施时先逐个核对构造副作用（1.1 节的 [待核对]），能延迟的一律延迟到命令真正需要时；`status`/`memory-list` 等命令实测只用 `local_store`、`memory`、`subagents`，不需要 curator/调度写盘。
6. **S9**：`SubAgentManager` 的 `runtime_db` 以只读打开（`mode=ro`）；`runtime-stale-attempts` 默认路径只 SELECT；`--settle` 不是只读路径（见 2.7）。
7. **S10**：只读启动跳过 `extensions.activate_agent` 里会写盘的激活副作用（若有）；插件默认关闭，实施时核对。

### 2.3 本地库 `mode=ro` 打开、建表推迟

- `LocalStore.__init__(db_path, ..., readonly: bool = False)`：
  - `readonly=True` 时**不做** S4a 的三个 `mkdir`、**不做** `_init_schema`；
  - 连接统一走一个只读 `_connect`：`sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)`；跳过 `PRAGMA journal_mode=WAL`（在只读库上无意义且可能报错）；保留 `busy_timeout`、`foreign_keys`；
  - 任何写方法（`record`、`index`、`timeline` 之外的写 API、`rebuild_fts`、`maintenance` 的清理等）在 `readonly=True` 实例上先抛结构化错误（如 `LOCAL_STORE_READONLY`），而不是等 SQLite 报 `SQLITE_READONLY`。
- **建表推迟**：把 `_init_schema` 从 `__init__` 挪到 `_require_writable()`；普通（非只读）实例的首次写操作前调用，行为与今天等价（今天也是在构造时建）；只读实例永不调用。命令里真需要建表的写路径（`local-rebuild`、`run`、`remember` 等）自然保持今天的行为。
- 只读命令查询时如果表还不存在（全新 home 从未写过库）：不要建表；给结构化错误/空结果说明（见 2.6 的 `CLI_STORE_NOT_INITIALIZED`）。
- `stats()`/`timeline()` 等只读方法在只读实例上保持可用（它们本来就是 SELECT）。

### 2.4 Gateway 在跑 / 不在跑（WAL 两种状态）

`local.db` 是 WAL 模式。只读打开时按 WAL 状态分三种：

1. **Gateway 在跑**（`-wal` 与 `-shm` 都在，写连接持有）：`mode=ro` 直读即可，SQLite 会以只读方式使用现有 `-shm`，能读到已提交的最新数据；沿用 `busy_timeout=30000` 处理并发 checkpoint 的短暂锁。
2. **Gateway 不在跑、已正常 checkpoint**（无 `-wal` 或 `-wal` 为空）：`mode=ro` 直读主库，行为最干净。
3. **Gateway 不在跑、`-wal` 有残留但 `-shm` 不存在**：SQLite 打开只读库时可能需要创建 `-shm` 才能读 WAL；在只读挂载/沙箱里创建会失败（`SQLITE_READONLY_CANTINIT`/`SQLITE_CANTOPEN`）。此时**不允许**悄悄用 `immutable=1` 读主库——那会漏掉未 checkpoint 的事务，读到旧数据。正确行为：报结构化错误 `CLI_STORE_WAL_NEEDS_HOST`（说明“本地库有未合并的 WAL，需要宿主先打开一次库/由宿主侧完成 checkpoint 后再读”），退出码 1；只读命令不写 `-shm`、不 checkpoint。

- 判定“Gateway 在不在跑”不能靠猜：沿用现有结构化事实（`gateway_running(paths)` 读 pid/state，`gateway_parts`）；命令输出里区分“库可读（ro）”和“WAL 需宿主”两种情况。
- 若 `-shm` 可写但库只读（例如普通用户对目录有写权限、只对 db 只读）：允许 SQLite 建 `-shm` 读取（这是 SQLite 的正常只读 WAL 行为），但**本项目标是沙箱全只读**，仍按“沙箱里 `-shm` 建不出来 → 走情况 3”设计。

### 2.5 版本或迁移对不上时怎么报

- 现状没有统一的“store schema 版本”键；只有 FTS 有 `_FTS_SCHEMA_VERSION`（`schema.py:265-293`）。实施时补一个 `metadata` 键（如 `store_schema_version`），在**写路径**的 `_init_schema` 末尾写入；只读路径只读不写。
- 只读启动时的裁决（只读 `metadata`，不跑任何迁移）：
  - 版本一致 → 正常服务；
  - 库版本旧于代码（需要迁移）→ `CLI_STORE_MIGRATION_REQUIRED`（带当前/期望版本号），提示“需要先由宿主/Gateway 启动完成迁移”；
  - 库版本新于代码（库来自更新版本）→ `CLI_STORE_VERSION_AHEAD`，命令能读到的字段照常读，缺列时按该错误报；
  - 库完全没有版本键（更老的库）→ 按“需要迁移”处理，不猜。
- FTS：版本不符时只读路径**不 DROP、不 rebuild**，把 `_fts_available` 视为 False（关键词查询退回 LIKE 语义或明确提示索引未就绪），因为 timeline/stats 不依赖 FTS。
- 错误码走既有结构化风格（`error_code=...` 一行 + 退出码 1），与 `CLI_HOST_STATE_READ_ONLY`（`cli/host_state_guard.py`）并列；后者继续作为沙箱真权限失败的兜底码，两者不互相替代。

### 2.6 只读启动的错误码（新增，建议）

| 码 | 触发 | 提示方向 |
| --- | --- | --- |
| `CLI_STORE_NOT_INITIALIZED` | 只读启动发现本地库/关键目录从未初始化 | 先让宿主完成首次初始化（正常跑一次 Gateway 或对应命令） |
| `CLI_STORE_MIGRATION_REQUIRED` | store 版本旧，需要迁移 | 宿主/Gateway 启动完成迁移后再试 |
| `CLI_STORE_VERSION_AHEAD` | 库版本新于当前代码 | 升级 my-agent 后再试；缺列字段明确列出 |
| `CLI_STORE_WAL_NEEDS_HOST` | 有 WAL 残留但 `-shm` 建不出来 | 宿主先打开一次库/完成 checkpoint |
| `CLI_HOST_STATE_READ_ONLY` | 只读启动后仍有未覆盖的宿主状态写（或非只读命令） | 保持 H3 现状：改用内置工具 |

### 2.7 命令本身的写怎么处理

- `subagents`（`write_board`）：建议只读启动下**自动降级**——`write_board` 检测到只读实例时只 `build_board` + 打印，跳过两个文件写入，并在输出里加一行“沙箱只读：未写入 subagent_board.json”。这样命令可用、语义明确；或由 be/3a 裁决改为排除出只读名单（本设计倾向前者）。
- `runtime-stale-attempts --settle`：带 `--settle` 时 `cli_readonly` 声明不生效（注册处按 `--settle` 是否存在是运行态判断，不是命令名判断）——实现上在 `cmd_runtime_stale_attempts` 里检测到 `settle` 就报“该模式需要可写宿主”结构化错误；默认（不带 `--settle`）走只读启动。
- 其它 6 条命令命令本身无写，只读启动即可。

## 3. 实施拆分（每块：改哪些文件、接口、用例、工时）

| 块 | 内容 | 改哪些文件 | 接口 | 用例（新增/改） | 估时 |
| --- | --- | --- | --- | --- | --- |
| B1 | CLI 只读结构化声明与传递 | `cli/parser.py`（不动逻辑，只核对）；8 条命令各自注册文件（`cli/home_runtime_commands.py`、`cli/subcommands_basic.py`、`cli/subagents.py`、`cli/_board.py` 相关注册点）；`cli/main_entry.py`；`cli/common.py` | `set_defaults(func=..., cli_readonly=True)`；`make_agent(args)` 读 `getattr(args,"cli_readonly",False)`；`SimpleAgent(..., readonly_startup=bool)` | 遍历 `build_parser()` 的 subparser：8 条命令 `cli_readonly=True`、随机抽非只读命令为 False；`make_agent` 传递字段的 fake agent 单测 | 0.5 人日 |
| B2 | LocalStore 只读打开 + 延迟建表 | `local_storage/store.py`、`local_storage/schema.py`、写方法所在 mixin（`records/events/maintenance/tool_operations/control_plane` 等）加 `_require_writable` | `LocalStore(db_path, ..., readonly=False)`；只读连接 URI `file:...?mode=ro`；`_init_schema` 移入写路径 | 只读实例上 `stats()/timeline()` 成功且目录/库/`-wal` 零新增；只读实例调写 API 抛 `LOCAL_STORE_READONLY`；全新目录只读打开报 `CLI_STORE_NOT_INITIALIZED` | 1 人日 |
| B3 | SimpleAgent 只读启动路径 | `agent/core.py`（`__init__`、`_resolve_home_paths`、`_wire_memory_authorities`、`_wire_memory_curator`、`_build_subagent_manager`）；`agent/user_space/owner_resolver.py`（只解析不 ensure 的入口） | `SimpleAgent(..., readonly_startup=False)`；`resolve_owner_home` 只读解析路径；`SubAgentManager` runtime.db 只读打开 | 临时只读目录里构造只读 agent：跑通、且目录 mtime/文件清单与构造前一致（零写入断言）；`ensure_owner_home` 不被调用（fake/spy） | 1.5 人日 |
| B4 | 版本/迁移门 + WAL 三态 + 错误码 | `local_storage/schema.py`（`store_schema_version` 读写）、`cli/host_state_guard.py`（新码常量与判定）、`cli/local_commands.py`/`home_runtime_commands.py`（错误输出） | 新增 2.6 表里的 4 个码；只读路径只读版本不迁移 | 版本旧/新/缺失三态各一条；WAL 残留无 `-shm`（模拟：`-wal` 手工造、删 `-shm`）报 `CLI_STORE_WAL_NEEDS_HOST`；Gateway 在跑（真 `-wal`/`-shm`）直读成功 | 1 人日 |
| B5 | 命令级收口与端到端 | `cli/_board.py`（write_board 只读降级）、`cli/home_runtime_commands.py`（`--settle` 显式报错）；`TESTS.md`、`DESIGN_LEDGER.md` | `write_board` 只读分支；`--settle` 拒绝分支 | 8 条命令在“全只读 home 快照”上逐条跑（隔离副本 + chmod/只读挂载）；对照 8 条命令在正常环境行为不变；`subagents` 降级输出断言 | 1 人日 |
|  |  |  |  | **合计** | **5 人日**（不含评审与真实沙箱复核） |

排序：B1 → B2 → B3 → B4 → B5；B2 与 B3 可并行（接口先定死）。每块完成后跑相关测试文件 + guards9（按 3a 工作规则）。

## 4. 风险与边界

### 4.1 并发写

- WAL 允许一写多读，但只读连接**必须能读 `-shm`**；Gateway 长跑时 `-shm` 一直在，风险低；Gateway 停机后 `-wal` 残留 + 无 `-shm` 是主要坑（2.4 情况 3）。
- 只读连接不要执行 `PRAGMA journal_mode=WAL`、不要 checkpoint、不要建表；这些都会升级写锁或写文件。
- 与 Gateway 的 checkpoint 竞争：保留 `busy_timeout=30000`；只读命令是短查询，可接受。
- 同一 owner 的 `runtime.db` 也可能被别的运行版本写（`unidentified_stale_attempts` 的注释已说明）；只读打开下读到的是提交点快照，命令语义就是“看当前事实”，不承诺跨库一致。

### 4.2 Linux 与 macOS 的差别

| 维度 | macOS（Seatbelt） | Linux（bwrap） | 影响 |
| --- | --- | --- | --- |
| 写拒绝的 errno | `EPERM` | `EROFS` | 只读打开失败的判定不能只看一个 errno；以 sqlite 错误码（PERM/READONLY/CANTOPEN）为主，errno 为辅（H3 已这么做） |
| `-shm` 创建 | 受保护路径上 EPERM | 只读挂载上 EROFS | 情况 3 两平台都失败，同一错误码 |
| 不存在的路径 | Seatbelt 可对不存在路径下拒绝规则 | bwrap 只能挂已存在路径，库不存在时只读启动直接读不到 | Linux 上“库不存在”的报错更早出现；错误文案要覆盖“未初始化” |
| 大小写 | 默认大小写不敏感 | 敏感 | 只读判定按 `Path.resolve` 后的真实路径（与 H3 一致） |

### 4.3 其它边界

- **只读启动不等于命令整体只读**：`subagents`/`--settle` 这类命令本身要写，必须按 2.7 单独处理，否则沙箱里仍会被 H3 拦成 `CLI_HOST_STATE_READ_ONLY`。
- **首次运行/全新 home**：没有任何库时只读命令应给“未初始化”错误，不能假装成功，也不能偷偷建库。
- **`ensure_owner_home` 的种子文件**：只读启动跳过写入；如果用户 home 缺 `permissions.json` 等，命令照常按缺失读，不补写。
- **错误码兼容**：`CLI_HOST_STATE_READ_ONLY` 的行为与用例（H3 的 `test_host_files_access.py`）不能改；新码只加在只读启动路径。
- **插件扩展命令**：默认非只读，不享受只读启动；要用的插件自行声明。
- **未验证项**：本文所有“待核对”的构造副作用（S5-S10、`detect_active_work`、`events.timeline` 内部）在实施 B3 前必须逐函数核对；本文行号基于 `d160a2c6d`，实施时按当时 HEAD 复核。

## 5. 未决问题（给 be 复审）

1. `subagents` 命令：只读降级（跳过写盘）还是排除出只读名单？本设计倾向降级。
2. 只读启动是否需要在命令输出里显式标注“只读模式”（例如 stderr 一行），避免用户以为写过了？
3. `store_schema_version` 的键名与写入时机（写路径 `_init_schema` 末尾）是否与 H3 的后续计划冲突？
4. 只读启动要不要复用 `MY_AGENT_HOST_STATE_READ_ONLY=1` 环境标记来“自动启用”（沙箱里自动只读启动），还是只认命令声明？本设计建议只认声明 + 沙箱失败兜底，避免两套判定。
