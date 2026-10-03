# 只读 CLI：现状与不做的理由（CLI_READONLY_STARTUP）

- **状态：结论：不做（3a 10-03 定乙）**。
- **复审**：be，2026-10-03；基线为 H3 `d160a2c6d`。
- **范围**：只修订文档、源内注释和测试说明；不改产品逻辑或行为。沙箱中的 CLI 继续失败并返回结构化错误 `CLI_HOST_STATE_READ_ONLY`，提示改用内置工具。
- **关联**：[H3 宿主托管文件写保护](HOST_CONFIG_WRITE_GUARD.md) 第 3 节第 8 条、[设计台账](../../DESIGN_LEDGER.md)。

## 裁决

不在模型沙箱中支持 CLI 只读启动，保持 H3 的宿主状态拒写边界。理由是常态下 SQLite 库并不能在拒写的沙箱中以 `mode=ro` 打开；绕过该问题的“宿主始终保持 SQLite 长连接”方案会新添运行不变量，不为 CLI 便利引入。改用已有内置工具；各命令可替代范围及尚存缺口见下表。

## 现状：八条命令为什么失败

下列八条命令都先经 `cli/main_entry.py::main` 调用命令处理函数，再由 `cli/common.py::make_agent` 构造完整 `SimpleAgent`。在已有 home 的常见情形中，最先必然命中的硬写点是 `LocalStore` 建表初始化，而不是 home 初始化或 owner 登记。

| 启动点 | 位置与效果 | 对既有 home 的实际情况 |
| --- | --- | --- |
| S1 | `agent/core.py:508`，确保工作区根目录存在 | 只在目录缺失时创建。 |
| S2 | `agent/core.py:860-865` → `_resolve_home_paths`、`ensure_my_agent_home`、`ensure_owner_home`；`agent/user_space/owner_resolver.py:111-120` | 目录及种子文件/JSON 均缺失才创建；已有 home 上不写。 |
| S3 | `agent/core.py:874-878` → `_register_owner_ref_if_possible`、`register_owner_ref` | owner 登记仅缺失时补写；已有登记不写。 |
| S4 | `agent/core.py:533` → `_wire_memory_authorities` → `agent/local_storage/store.py:34-49` → `agent/local_storage/schema.py:201-229` `_init_schema` | **第一个必然命中的硬写点**：初始化 LocalStore 时创建目录/数据库、设置 WAL、建表/索引并执行 schema/FTS 初始化。H3 拒绝对 `workspace/runtime` 状态的写入后，启动失败并由 CLI 守卫返回 `CLI_HOST_STATE_READ_ONLY`。 |

原稿 S5–S10 不再核查，**因结论为不做，未核**：S5 `ConversationStore`；S6 Curator/迁移相关存储；S7 `CollaborationStore`；S8 `SchedulerRepository`/索引；S9 子代理 `runtime.db`；S10 插件扩展注册/激活。`detect_active_work` 与 `events.timeline` 的内部副作用也未核。此结论不依赖这些点的副作用。

受影响命令及可用替代工具：

| CLI 命令（原代码入口） | 已有内置工具 | 覆盖判断与缺口 |
| --- | --- | --- |
| `status`（`cli/local_commands.py:66-118`） | `gateway_status`；`list_agents`；需要请求明细时可查 `audit_records(topic="requests")` | **部分覆盖**：分别可查看 Gateway 权威状态、当前可见代理树及 Gateway 请求记录；不等于 CLI 汇总，LocalStore 统计、LocalStore 事件时间线和 `detect_active_work` 仍是缺口。 |
| `home-status`（`cli/home_runtime_commands.py:123-127`） | — | **缺口**：没有已核实的模型内置工具能返回 CLI 的 owner home 初始化/布局状态。 |
| `subagents`（`cli/_board.py:56-92`） | `list_agents` | **部分覆盖**：只读返回当前可见代理树状态和产物引用，不生成 CLI 的 `subagent_board.json` / `SUBAGENT_BOARD.md`，也不是同一份 board 展示。旧名 `inspect_agent_tree` 已从模型可用控制工具中退役；当前工具名是 `list_agents`。 |
| `memory-list`（`cli/local_commands.py:318-325`） | `remember(action="list")`；按关键词查找可用 `memory_search` | **覆盖正式长期记忆列表**：`remember(action="list")` 读取正式长期条目；不包含候选、日记等其它存储。`memory_search` 是查询检索，不是全量列表，且受 `enable_memory_search_tool` 开关控制。 |
| `local-store-status`（`cli/local_commands.py:337-341`） | — | **缺口**：没有已核实的模型内置工具提供 LocalStore 的 `stats()` 状态。 |
| `timeline`（`cli/local_commands.py:164-192`） | `session_search` | **部分相关但不等价**：可翻查本地对话/历史记录；CLI 读的是 LocalStore 事件时间线，两者数据源与语义不同，LocalStore timeline 仍是缺口。 |
| `task-workspace-list`（`cli/home_runtime_commands.py:104-120`） | `session_search`；`list_agents` | **缺口**：历史命中可能带已结束 Gateway 任务的 `task_ref`，`list_agents` 可给可见代理产物引用；两者都不枚举 owner 下完整的 task workspace。 |
| `runtime-stale-attempts`（`cli/home_runtime_commands.py:155-176`） | `gateway_status` | **部分信息，详细能力缺口**：Gateway 状态仅投影启动时写入 `state.json` 的未识别 stale attempt 计数（有正数时显示）；不查询 `runtime.db` 的完整记录，也不提供 CLI `--settle`。 |

工具能力核对依据：`gateway_status` 只读当前唯一 Gateway，面向主代理注册（`agent/tooling/gateway_status.py:29-95`）；`list_agents` 只读当前代理树（`agent/agent_core/orchestration/tool_specs.py:265-298`、`agent/agent_core/orchestration/tools/list_agents.py:33-72`）；`remember(action="list")` 读取正式长期记忆（`agent/capability/memory_tool.py:42-82, 227-280, 884-895`）；`memory_search` 只做有界查询检索且开关默认关闭（`agent/capability/memory_search_tool.py:40-87`）；`session_search` 查阅本地历史记录而非 LocalStore 事件（`agent/capability/session_search_tool.py:95-144`）；`audit_records` 仅有 `decision`、`requests` 两个主题（`agent/tooling/audit_records_tool.py:30-33`）。这些工具只覆盖其自身声明的范围，不应据名称外推成 CLI 的完整等价物。

`subagents` 命令执行阶段会写 board 文件；`runtime-stale-attempts --settle` 是显式写入路径。这两者也不能仅凭“查询类命令”名称视为只读。

## be 复审探针证据

be 在 macOS scratch 临时目录，用 Python 3.12.13 / SQLite 3.50.4，以 Seatbelt `(deny file-write* (subpath <库目录>))` 对照沙箱拒写（复审材料 `README.md` 第 5–27 行、`probe.py`、`report.json`）：

| SQLite 状态 | 库目录内容 | 沙箱 `mode=ro` 结果 |
| --- | --- | --- |
| 写连接仍开着，WAL 有已提交记录 | `db`、`-wal`、`-shm` | 可读到最新 2 行。 |
| 最后连接正常关闭 | 只有 `db` | `SQLITE_CANTOPEN`（SQLite code 14）。 |
| 写进程被杀且 WAL、SHM 都保留 | `db`、`-wal`、`-shm` | 可读到最新 2 行。 |
| WAL 残留但 SHM 不在 | `db`、`-wal` | `SQLITE_CANTOPEN`。 |

宿主在“正常关闭”与“WAL 无 SHM”两种情况下虽能 `mode=ro` 读取，却会新建 `-wal` / `-shm`；宿主侧 `mode=ro` 不是零写入（`report.json` 第 30–52、82–106 行）。

真实仓储探针分别对 `LocalStore.stats()`、`RuntimeRepository.unidentified_stale_attempts()` 做一次正常操作后关闭连接：目录只剩 `local.db`（另有 `files/`）和 `runtime.db`，没有持久连接；Seatbelt 下两库 `mode=ro` 均返回 `SQLITE_CANTOPEN`（`real_store_probe.py`、`real_store_report.json` 第 1–19 行）。因此即使 Gateway 进程还在、当前空闲，仓储连接也已关闭，处于上述“正常关闭”情形；原稿把此情形判断为可直接只读打开是错误的。

## 为什么不做

1. **探针已推翻关键前提**：普通空闲状态下，宿主仓储操作开连接、用完即关；只读沙箱不能补建 SQLite 所需 sidecar，无法可靠打开本地库。
2. **不采用甲方案**：让宿主保持长连接以留住 WAL/SHM，会把“宿主连接持续存活且 sidecar 状态适合只读”的新运行不变量带入连接生命周期、关闭、异常退出与恢复路径。仅为支持沙箱 CLI 引入它不划算，也未有现成合同保证。
3. **符合安全方向**：H3 的目标就是让模型沙箱碰不到宿主状态。保留拒写边界和 `CLI_HOST_STATE_READ_ONLY`，用明确可用的内置工具读取其已授权范围；工具覆盖不到的功能如实列为缺口，不为补齐 CLI 体验扩大宿主状态访问面。

## 依据

- be 复审材料：SQLite WAL 探针（`probe.py` → `report.json`）与真实 LocalStore/RuntimeRepository 探针（`real_store_probe.py` → `real_store_report.json`）；均只在 scratch 临时目录操作。
- H3 现状：`docs/design/HOST_CONFIG_WRITE_GUARD.md` 第 3 节第 8 条；结构化 CLI 错误守卫在 `agent_py_agent/cli/host_state_guard.py`。
- 本文保留的 CLI / 启动链位置均基于 H3 基线 `d160a2c6d`。