# 产品持久数据的保留策略（盘点与方案，未落地）

状态：**未落地**。本文只做只读盘点和方案方向，不改产品代码。盘点基于 main `54880f8e9` 的源码，测量对象是本机生产 home
（`~/.my-agent`，2026-09-28 下午数据盘写满后）。

## 1. 范围与方法

- **代码侧**：从源码逐处找持久写入点，每处给出规范路径、写入方（`文件:行号`，相对 `agent_py_agent/agent/`）、增长驱动、现有清理。
- **测量侧**：只用 `du -sk`、`du -k -d 2` 和 `find -type f` 计数，每条命令都带超时；只读元数据，**不打开任何文件，不读会话或记忆正文**。
  目录名里的 owner 编号在本文一律写成模式。
- **路径简写**：
  - `O/` 表示 owner home，即 `owners/local/main/` 或 `owners/providers/<渠道>/{users,groups}/<id>/`（`user_space/owner_resolver.py:196`）；
  - `WS/` 表示 `O/workspace/runtime/workspaces/<cwd>-<sha12>/`，每个客户端工作目录一份（`user_space/runtime_paths.py:72`）；
  - `GW/` 表示 `owners/local/main/workspace/runtime/services/gateway/`，是全部 owner 共用的 Gateway 请求队列。
- **不在本文范围**：`releases/`（24.12 GB，发布备份，另行处理，源码里没有写入方）。测试方写入的证据目录只列出大小，
  不是产品数据：`testbox-backups/` 0.25 GB、`scale_evidence/` 0.13 GB、`test-evidence/` 0.10 GB、`decision-evidence/` 0.09 GB。

## 2. 总览：生产 home 里产品自己写的数据约 20 GB

| 位置 | 大小 | 文件数 | 说明 |
| --- | --- | --- | --- |
| `owners/local/main/`（管理员 home） | 约 17.1 GB | 约 145 万 | 大头都在这里，见第 4 节 |
| `owners/providers/*/users/*`（28 个 owner） | 0.44 GB | — | 多数是历史验收建在生产 home 里的测试 owner（`release-validation`、`tui-*` 等） |
| `global_index/` | 0.91 GB | 11 | `active_tasks/agents/runs.jsonl`、`owners.jsonl` 四个只追加索引 |
| 根目录 `memory_archive/snapshots/` | 0.87 GB | 11,738 | 旧布局遗留，只在 owner home 缺失时回退写入 |
| `system/backups/` | 0.08 GB | 31,356 | 记忆迁移备份 |
| 根目录 `workspace/tasks/` | 0.08 GB | 25,412 | 旧布局遗留 |

## 3. 现有保留机制

- **owner 保留服务**：`MemoryRetentionService`（`memory_store/retention_scan.py`、`retention_apply.py`），策略在 `O/retention.json`。
  - 触发方：只在 Gateway 运行时由维护线程触发（`cli/gateway_loops.py:1188`），每 `owner_maintenance_scan_interval_seconds`（默认 60，
    0 表示关闭）扫一轮，每个 owner 再按 `maintenance_interval_seconds`（默认 86400）节流（`user_space/owner_maintenance.py:33-97`）；
    也可以手动跑 `home-retention --apply` 或 `memory retention apply`。
  - 做法：按文件 mtime 或任务、线程的 `updated_at` 判断年龄，目录先移到 `O/trash/retention/`，再按 `trash_days` 删除。
  - 键与默认值（`memory_store/retention_models.py:32-48`，0 表示该类关闭）：

    | 键 | 默认 | 键 | 默认 |
    | --- | --- | --- | --- |
    | `conversation_days` | 365 | `completed_task_days` | 365 |
    | `audit_days` | 180 | `subagent_scratch_days` | 30 |
    | `daily_days` | 365 | `tool_output_days_after_terminal` | 30 |
    | `compact_days` | 365 | `rejected_candidate_days` | 30 |
    | `curator_run_days` | 90 | `cache_days` / `tmp_days` | 30 / 7 |
    | `trash_days` | 30 | `legal_hold` / `maintenance_enabled` | false / true |

- **写入时自带上限**（与保留服务无关）：
  - `O/data/decision/outcomes.jsonl`：1000 行（`conversation/decision_outcome_log.py:25`）；
  - `O/data/decision/reach_counts.json`：按小时分桶，保留 7 天；
  - `O/memory/ops.jsonl`：4096 行；
  - 长期记忆：超过 2000 条时削到 1200 条；
  - `data/verification/evidence.sqlite3`：每个任务根 100 条、30 天、总共 1 万条；
  - skill 学习账本：2000 条，最多 5 个版本；
  - 设置变更记录：500 条；
  - 渠道投递记录：7 天；
  - 后台进程会话：保留最近 128 个。
- **其它**：`O/quota.json` 的 `max_disk_mb` 默认 0，即不设磁盘上限；`log_analysis_config.yaml` 的 `source_retention_days: 30`
  没有任何 Python 代码读取。

## 4. 按大小排序（管理员 home 与全局，产品写入）

“只增不减”指代码里找不到任何删除、裁剪或轮转；“机制过松”指有保留机制，但在当前数据上基本回收不到东西。

| # | 规范路径 | 大小 | 写入方与增长驱动 | 现有清理 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 1 | `O/tasks/<date>/<slug>/`（旧版任务工作区，含模型按提示创建的文件） | 8.30 GB，约 70 万文件，56 个日期目录 | `user_space/run_workspace.py:445` 与文件工具；每个任务 | `completed_task_days` 365，只认带已完成 `work/state.json` 的目录；子代理草稿 30 天；终态后工具输出 30 天 | 机制过松；最大的几个目录是用户任务里克隆或下载的项目（单个 3.0 GB、1.3 GB、1.1 GB），属于用户交付物 |
| 2 | `WS/local_store/`（`local.db` 1.12 GB、`events.jsonl` 0.71 GB、`files/` 0.08 GB） | 1.92 GB | `local_storage/records.py:268`、`events.py:99`、`gateway_parts/logging.py:65`；每个 Gateway 请求和事件 | 有 `reset()` 但没有调用方 | 只增不减 |
| 3 | `O/memory_archive/snapshots/context_bundles/<date>/<request>.{json,md}` | 1.90 GB | `user_space/context_bundle.py:205`；每轮一份（`auto_save_memory` 开启时） | 无 | 只增不减 |
| 4 | `O/agents/<run>/`（子代理目录） | 1.30 GB，约 49 万文件，13.3 万个条目（13.17 万个超过 90 天） | `subagents/services/persistence/service.py:589`；每次子代理保存 | 无 | 只增不减 |
| 5 | `global_index/{active_tasks,active_agents,active_runs,owners}.jsonl` | 0.93 GB（338/271/269/51 MB） | `user_space/home_indexes.py:200`；每次引用变化追加一行 | 只有手动 `home-index-rebuild --apply` 会压缩 | 只增不减 |
| 6 | 根目录 `memory_archive/snapshots/` | 0.87 GB | `user_space/context_bundle.py:232` 的回退路径；只在 owner home 缺失时写 | 启动时只删空的旧目录（`user_space/home_layout.py:245-272`） | 旧布局遗留，只增不减 |
| 7 | `O/data/workspaces/<cwd>-<hash>/`（2631 个，结构同 `WS/`；1252 个以测试函数名 `test_*` 命名，另有 `workspace`、`fixture_project` 等，是早期测试未隔离 home 时写进来的） | 0.80 GB | 当前源码没有写入方（运行时工作区已改到 `workspace/runtime/workspaces`） | 无 | 旧布局遗留 |
| 8 | `WS/conversations/messages/<thread>.jsonl` | 0.44 GB | `conversation/store_messages.py:253`；每个用户轮和助手输出片段 | `conversation_days` 365，整条线程不活跃才移走 | 机制过松 |
| 9 | `O/runs/<date>/<sha24>/`（新版运行与任务工作区） | 0.41 GB，44,452 文件 | `agent_core/run_task_workspace_writer.py:539`、`user_space/run_workspace.py:95`；每个晋升为任务的轮次 | 只删没动过的空脚手架（`run_workspace.py:104`）；保留扫描只看 `O/tasks/`（`retention_scan.py:280/342/405`） | 只增不减 |
| 10 | `WS/subagents/daily/<date>/events.jsonl` 等 | 0.26 GB | `memory_archive/daily_ledger.py:55`；每次子代理派发 | 无 | 只增不减 |
| 11 | `O/blobs/tool_outputs/{*.json,index.jsonl}`（`index.jsonl` 0.18 GB） | 0.23 GB | `memory_archive/tool_output_externalizer.py:237,314`；每次工具调用一行索引，大输出一个文件 | 无 | 只增不减 |
| 12 | `WS/conversations/display_archives/` | 0.18 GB | `conversation/display_archive.py:36`；每次展示归档 | 无；线程被移走时也不跟着走 | 只增不减 |
| 13 | `O/runtime.db` | 0.14 GB | `runtime_db/`；每个运行、每次工具调用写事件、操作和尝试 | 只删锁和唤醒队列两类行，不做 VACUUM | 只增不减 |
| 14 | `GW/requests/{done,failed,terminal}/`、`responses/`、`gateway_requests.jsonl`、`input_receipts/` 等 | 0.13 GB，11,331 文件 | `gateway_parts/recovery.py:938-1010`、`io.py:477,528,594`；每个请求 | 无；`gateway.log` 也不轮转（`common/rotating_log.py` 只有测试在用） | 只增不减 |
| 15 | `WS/conversations/agent_transcript_events/<run>.jsonl` | 0.12 GB | `conversation/agent_transcript.py:34`；每个子代理事件 | 运行结束时单个文件裁到最后 1024 行，文件本身不删 | 只增不减（文件数） |

另外还有一批量小、但同样只增不减的位置：

- **记忆与运行事实**：`O/memory_archive/{runtime_facts,tokens,task_progress,compact_applies}`、`O/memory/hooks/`（`storage.py:160` 的
  `enforce_retention` 没有调用方）。
- **审计与日志**：`O/logs/audit/audit.jsonl`、`O/audit_log.jsonl`、`O/audits/`。
- **个人与身份数据**：`O/media/input/`、`O/persona/{versions.jsonl,backups/}`、`identity/**`、`O/sessions/`。
- **调度、监听与协作**：`O/data/scheduler/history.jsonl`、`O/watch_state/` 的归档（按设计永不删除）、
  `O/{capability_requests,temporary_grants}/`（只标过期不删）、`WS/collaboration/**`。
- **其他清理留下的尾巴**：会话 `.ledger_archive/`、长期记忆的 `*.archive.jsonl`、插件更新后被替换的旧包。

## 5. 现有机制的缺口

1. **保留扫描只看旧布局**：`completed_task_days`、`tool_output_days_after_terminal`、`subagent_scratch_days` 只扫 `O/tasks/`，
   新的运行工作区都写在 `O/runs/`（`conversation/workspace_paths.py:1-4`、`run_task_workspace_writer.py:551`），实际只清旧布局。
2. **没有 Gateway 就不清理**：保留只在 Gateway 维护线程里跑，纯 CLI 使用或 Gateway 长期不开时不会自动执行。
3. **审计清理命令指错文件**：`audit-log --cleanup` 按裸配置组路径（`cli/audit_log_cmd.py:108-131`），解析成当前目录下相对的
   `data/audit/audit.jsonl`（`audit/paths.py:16`），而不是 `O/logs/audit/audit.jsonl`。
4. **`compact_days` 可能删掉仍在用的检查点链**：按 mtime 删除，一个仍活跃、但 365 天没压缩过的线程会丢掉检查点链，
   之后读取失败（`conversation/compact_checkpoint.py:226`）。
5. **移走线程会留下孤儿**：移走时跳过 `model_usage`、引导索引、`display_archives`、压缩检查点和锁文件。
6. **生产 home 里积累了测试数据**：28 个 provider owner 中多数来自历史验收；`O/data/workspaces/` 里 1252 个工作区以测试函数名命名，
   是早期测试没有隔离 home 时写进来的。owner 生命周期没有“移除测试 owner”的入口。
7. **审计保留扫的不是审计日志**：`audit_days`（默认 180）只扫 `O/audit/*.jsonl`（`memory_store/retention_scan.py:168`，
   `owner_audit_dir` 见 `user_space/home_layout_v2.py:264`）。这个目录放的是主代理每轮的记忆归档原始事件
   （`agent_core/_finalization_service.py:212-217` 调 `memory_archive/storage.py:48-50,112`），也是 Curator 的输入
   （`memory_store/curator_inputs.py:433`）。审计日志本身不在其中：
   - `AuditLogger` 写 `O/logs/audit/audit.jsonl`：`audit_log_path` 默认是空串（`agent_py_agent/config/agent_config.yaml:652`），
     启动时由 `user_space/runtime_paths.py:97-102` 注入 `owner_logs_dir/audit`，写入在 `audit/logger.py:70`；
     每条还复制一份到 LocalStore 事件（`audit/logger.py:122-128`），随第 4 节第 2 行的 `local_store` 一起只增不减；
   - owner 审计账本 `O/audit_log.jsonl`（`user_space/home_layout_v2.py:281`），保留服务自己的执行记录也写在这里
     （`memory_store/retention_apply.py:445`）。

   这几处都没有自动清理，`audit_days` 对它们不生效。唯一的入口是手动 `audit-log --cleanup`（`cli_audit_cleanup_days` 默认 90，
   `settings/config.py:529`），而它也指不到 `O/logs/audit/audit.jsonl`：见缺口 3；2026-09-28 的修复改成按 owner home 解析，
   但默认值仍落在 `O/data/audit/`。方向：审计类数据按写入端的规范路径登记进第 6 节第 1 条的登记表，扫描不再另写一份目录。

## 6. 通用保留方案方向（不写专项分支）

以下是方向，不是实现承诺；落地前各项另行评审。原则沿用开发铁律：一个概念一个权威位置，判断只看结构化事实（路径登记、
终态、引用、mtime），不按文件内容或自然语言判断；默认先移入回收站，再过期删除。

1. **一张存储登记表，一个保留权威**。
   - 每类持久数据在一处声明：根路径模式、年龄来源（mtime、终态时间或 `updated_at`）、终态判定、引用保护、动作（进回收站、
     删除、压缩或轮转）、对应的 `retention.json` 键。
   - `MemoryRetentionService` 按登记表通用执行，不再为每种目录写一段扫描代码。第 5 节缺口 1 的根因就是扫描写死了一个目录。
2. **只追加的日志与索引，按段轮转或整理压缩**。
   - 适用于 `global_index/*.jsonl`、`local_store/events.jsonl`、`gateway_requests.jsonl`、`blobs/tool_outputs/index.jsonl`、审计日志。
   - 做法：写满或过期就切段；索引类定期按“当前有效行”重写。`home-index-rebuild` 已有的压缩逻辑可以挪进维护线程，
     `common/rotating_log.py` 可以接到 `gateway.log`。
3. **每请求、每运行的快照与记录，终态后按天数保留**。
   - 适用于上下文快照、`runtime_facts`、Gateway 的 `done/terminal/failed/responses`、`agents/`、`runs/`、子代理 `daily/`。
   - 规则：终态后过 N 天清理，N 走配置，并且只清没有被结构化引用的项（被任务、线程、pin 或 refs 引用的保留），
     避免拆断恢复链；缺口 4 也按“是否仍被引用”判断，不按 mtime。
4. **SQLite 库：行级保留加定期整理**。`runtime.db`、`local.db` 对终态行按天数删除，维护时做增量整理（VACUUM），
   避开正在持有的租约和锁。
5. **旧布局遗留一次性处理**。根目录 `memory_archive/`、`workspace/`、`O/data/workspaces/`、`system/backups/`：由迁移命令先确认
   不再被引用，再进回收站；不自动静默删除。
6. **用户交付物不自动删，改为可见和提示**。`O/tasks`、`O/runs` 里的项目文件属于用户数据：
   - 默认不按时间删；
   - 在设置页、TUI 和 IM 提供“存储占用”视图和一键移入回收站；
   - `max_disk_mb` 给出有意义的默认提醒值，只提醒、不拦截。
7. **不依赖 Gateway 运行**。维护入口同时挂到 CLI 启动或轻量调度上（有节流），缺口 2 随之消除。
8. **磁盘压力兜底**。可用空间低于阈值时，先触发一次维护，再在 TUI 和 IM 醒目提示；不因磁盘压力删除用户交付物。
9. **测试数据与生产 home 分开**。验收与测试一律使用隔离 home；为已经混入的测试 owner 提供带确认的移除命令。

## 7. 待决问题

- 各类数据的默认天数（快照、Gateway 记录、子代理目录）取多少，需要结合恢复与审计需求决定。
- 用户交付物的“存储占用”视图放在哪个入口（设置页、`/status`、IM 命令），以及回收站的恢复方式。
- 登记表放在 `settings` 还是 `user_space`，以及它与参数中心（`PARAMETER_CENTER.md`）如何分工。
