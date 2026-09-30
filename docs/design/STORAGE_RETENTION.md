# 产品持久数据的保留策略（盘点与方案，部分落地）

状态：**部分落地**（2026-09-29 按 R4 上线后的代码与生产首跑核对，见第 8 节；第 1–7 节保留 09-28 的原始盘点，不逐段改写）。本文最初只做只读盘点和方案方向，不改产品代码。盘点基于 main `54880f8e9` 的源码，测量对象是本机生产 home
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
- **审计与日志**：`O/logs/audit/audit.jsonl`、`O/audit_log.jsonl`（见缺口 7）。
- **审计类运行工作区**：`O/audits/<audit_id>/`，与 `O/runs/` 同为运行工作区根，不是日志（见缺口 1）。
- **个人与身份数据**：`O/media/input/`、`O/persona/{versions.jsonl,backups/}`、`identity/**`、`O/sessions/`。
- **调度、监听与协作**：`O/data/scheduler/history.jsonl`、`O/watch_state/` 的归档（按设计永不删除）、
  `O/{capability_requests,temporary_grants}/`（只标过期不删）、`WS/collaboration/**`。
- **其他清理留下的尾巴**：会话 `.ledger_archive/`、长期记忆的 `*.archive.jsonl`、插件更新后被替换的旧包。
- **旧布局遗留的空会话目录**：`O/conversations/`、`O/data/conversations/`，两处都是空目录，没有保留机制，随旧布局一次性迁移处理（第 6 节第 5 条）。
  - 当前源码没有产品写入方。会话存储只建在两个规范位置：`O/workspace/runtime/workspaces/<scope>/conversations`
    （`user_space/runtime_paths.py:128-133`）和 `O/compact/conversations`（`user_space/home_layout_v2.py:75`）。
  - `O/data/conversations/` 建于 06-01，与当时的默认会话目录吻合：`conversation_workspace` 默认值 `data/conversations` 由 `4685bf62e`
    在 05-26 加入，`ba3b1537e` 在 06-04 删除。
  - `O/conversations/` 建于 09-24 20:03，只有 16 个空子目录，形态与 `ConversationStore` 新建时一致（只建空子目录、不写文件）。当前源码里
    只有验收脚本 `agent_py_agent/scripts/b_acceptance/curator_failure_evidence.py:85,123` 会在所给 home 下这样建，按设计应传隔离 home。
  - 唤醒发现仍把这两处当作历史形态扫描（`owner_wake_discovery.py:63-68`），迁移时一并确认是否还需要。

## 5. 现有机制的缺口

1. **保留扫描只看旧布局**：`completed_task_days`、`tool_output_days_after_terminal`、`subagent_scratch_days` 只扫 `O/tasks/`，
   实际只清旧布局。新的运行工作区有两个根，走哪个由 `conversation/workspace_paths.py:16-18` 的 `durable_work_root` 按工作类型决定：
   - 审计类工作写 `O/audits/<audit_id>/`（同文件 `:21-25`），由 `conversation/audit_lifecycle.py:293` 激活，同样带 `work/state.json`；
   - 其余运行写 `O/runs/<date>/<sha24>/`（`agent_core/run_task_workspace_writer.py:551`，这里直接取 `owner_runs_dir`，与上面是同一个根）。

   同文件的 `validated_durable_work_path`（`:30-55`）已经列出全部持久工作根：`O/runs/`、旧的 `O/tasks/` 和 `O/audits/`。保留扫描的根列表
   应当从这个权威推导，不在扫描里另写目录。2026-09-28 裁定：`O/runs/` 与 `O/audits/` 都纳入 `completed_task_days`（365 天）；
   `tool_output_days_after_terminal` 两个根都暂不扩，实现另行提交。
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
5. **旧布局遗留一次性处理**。根目录 `memory_archive/`、`workspace/`、`O/data/workspaces/`、`system/backups/`，以及空的
   `O/conversations/`、`O/data/conversations/`：由迁移命令先确认不再被引用，再进回收站；不自动静默删除。
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

## 已知设计残留（2026-09-29 记录，本次不改逻辑）

- **subagent_scratch 不看父任务是否已结束**：判定只看子代理自身的终态与时间。父任务仍在运行时，
  它下面早已结束（超过 `subagent_scratch_days`，默认 30 天）的子代理，其
  `inbox` / `outbox` / `compactions` / `artifacts/tool_outputs` 仍会被移入回收站
  （`final_report` 不动，回收站内 30 天内可恢复）。这是本次保留的既有行为，只在此写明。
- **所有恢复材料扫描都按规范根与规范深度**：`runs/<date>/<key>` 与 `tasks/<date>/<slug>` 第二层、
  `audits/<audit_id>` 第一层，由 `workspace_paths.canonical_task_root` 统一判定；非规范深度的
  「像任务」目录一律不算任务根（2026-09-29 起 tool_output 与 subagent_scratch 两处也套用）。

## 8. 落地状态（2026-09-29 核对：代码按 main `89af6b07a`，生产按 step16i 首跑）

核对只看源码和生产结构化字段（`O/data/maintenance.json`、`O/audit_log.jsonl` 里的 `owner_retention_applied`），不读正文。
「待定」表示没有排期、没有裁定，新想法只记在这里和台账，不开工。

**已落地**（R4 owner 保留重做，step16h/16i 上线；global_index 压缩与向量缓存回收同批）：

- 维护入口 `user_space/owner_maintenance.run_owner_retention_if_due`：Gateway 的 owner 维护线程每 60 秒扫一遍
  （`owner_maintenance_scan_interval_seconds`），每个 owner 到期（`maintenance_interval_seconds`，默认 86400）才跑。一次维护依次做：
  保留 apply、文本向量缓存回收（`text_vector_cache_reclaimed`）、global_index 压缩。结果写 `O/data/maintenance.json`
  （`owner-maintenance.v1`），每次 apply（包括拒绝）在 `O/audit_log.jsonl` 追加一条 `owner_retention_applied`。
- 缺口 1 的一半：`completed_task_days`（365 天）扫描 `O/runs`、`O/tasks`、`O/audits` 三个根，按
  `workspace_paths.canonical_task_root` 的规范深度认任务根（`retention_scan._recovery_roots` / `_iter_task_states`）。
  结构化终态加天数后整棵移入回收站。
- 错误隔离：只有策略级错误（`POLICY_INVALID`、`POLICY_UNREADABLE`、`CANDIDATES_UNREADABLE`）或法律保留才整次拒绝执行；
  `TASK_STATE_INVALID` 这类路径级错误只剔除与出错子树重叠的动作（`retention._without_errored_subtrees`），其余照常执行。
- 缺口 3：`audit-log --cleanup` 按 owner home 解析到写入端同一个 `O/logs/audit/audit.jsonl`。
- 缺口 7 的一半：原来扫错目标的 `audit` 类别（`O/audit/*.jsonl` 是记忆归档原始事件，Curator 要读）已停用，`audit_days` 暂不生效，
  不再误删。
- 方向 2 的一部分：global_index 的 active_tasks / active_runs / active_agents / owners 四个只追加索引，在维护里按 key 重写压缩
  （`home_index_compact`）：文件不小于 64 MiB、且不小于上次压缩后大小的 2 倍、距上次压缩满 6 小时才动。
  生产首次压缩（09-29 03:57，owner `5324eb9ef8b6` 的维护）：三份索引 355/282/284 MB → 41/56/55 MB，读取侧逐 key 核对 0 差异，派发未受影响
  （证据 `~/.my-agent/releases/step16i-df0114f2/first-maintenance/`）。
- 首跑积压的处理方式：`tmp`（7 天）、`cache`（30 天）、`curator_run`（90 天）、`daily`（365 天）、`compact`（365 天）按 mtime 直接删除；
  子代理 scratch（30 天）在父任务状态可读、未被保留、子代理自身结构化终态时移入回收站；回收站按 tombstone 的 `moved_at` 满 30 天清除。

**部分落地、剩余待定**：

- 缺口 1 的另一半：`tool_output_days_after_terminal` 与 `subagent_scratch_days` 仍只扫旧版 `O/tasks`（09-28 裁定「工具输出暂不扩」）。
- 缺口 7 的另一半：真正的审计日志 `O/logs/audit/audit.jsonl` 只能手动 `audit-log --cleanup`（`cli_audit_cleanup_days` 默认 90），
  `O/audit_log.jsonl` 与 LocalStore 里的审计事件副本没有任何清理。
- 方向 3：只有 `O/runs`、`O/audits` 经 `completed_task_days` 覆盖；上下文快照、`runtime_facts`、Gateway 的 done/terminal/failed/responses、
  `O/agents`、子代理 `daily/` 仍没有保留规则。另外 `completed_task_days` 是连用户交付物一起整棵移入回收站，与方向 6「交付物不自动删」
  不一致（09-28 的裁定如此，365 天后才触发），待定。

**未做（待定）**：缺口 2（维护仍只由 Gateway 触发；手动入口 `home-retention [--apply]`、`memory retention plan|apply` 只跑保留，
不做向量缓存回收、索引压缩，也不写 `maintenance.json`）、缺口 4（`compact` 仍按 mtime 删，没有引用保护，活跃线程的检查点链仍可能被拆断）、
缺口 5（移走线程的孤儿文件）、缺口 6（没有移除测试 owner 的入口）；方向 1（存储登记表）、方向 2 的其余文件（`local_store/events.jsonl`、
`blobs/tool_outputs/index.jsonl`、审计日志、`gateway.log` 轮转）、方向 4（SQLite 行级保留与 VACUUM）、方向 5（旧布局迁移）、方向 6（占用视图，
`max_disk_mb` 种子仍是 0）、方向 7（不依赖 Gateway）、方向 8（磁盘压力触发维护）、方向 9 的移除命令。

**已知残留（2026-09-29 核对时发现，待定，不改代码）**：

- `owner_maintenance._maintenance_status` 只要 `load_errors` 非空就记 `policy_unavailable`，哪怕路径级错误已被隔离、其余动作都执行了；
  `last_success_at` 也因此不前进。状态名会让人误以为整次被拒（生产 local/main 带 355 条 `TASK_STATE_INVALID` 时就是这样）。
  `partial_failure` 实际到不了：失败动作同时会记一条错误。
- `retention_scan._tool_output_actions` 的「函数用途」注释写「含旧 tasks 与新版 runs 两个根」，实际只扫 `O/tasks`；
  `_recovery_roots` 的注释写「两个规范根」，实际返回三个。
- `AuditLogQuery.cleanup_old_entries` 先读、写临时文件再替换，全程不持追加锁（`io.jsonl.append_line_locked` 用的锁），
  手动清理期间新追加的审计行可能丢失。
- 回收站清除注释里说的「散落文件按 mtime 清除」没有实现，只清 tombstone 容器。

**生产首跑积压（09-29 21:39:50，证据 `~/.my-agent/releases/step16i-df0114f2/first-maintenance/` 第二节）**：
- local/main 审计事件 `applied=true`、`ok=true`，共 11742 个动作，执行失败 0，与 my-agent-4 的 plan 逐类对上：
  - tmp 直接删除 3581（约 77 MB）；
  - subagent_scratch 进回收站 8090（约 73 MB）；
  - tool_output 进回收站 25，另有 46 条执行时目标已不在（missing）。
- 355 条 `TASK_STATE_INVALID` 的子树被隔离、没有动，要不要修复或迁移待定。
- `maintenance.json` 仍记 `policy_unavailable`，这正是上面「已知残留」第一条的实例。
- 另一个 owner（`86462b8c9517`）执行 146 个动作。
- 两个 owner（`93b8c3ffffb8`、`ebd40e6fc3ec`）的保留策略本身无效（`POLICY_INVALID`），每天整次拒绝执行，需要有人看它们的
  `retention.json`（待定）。
