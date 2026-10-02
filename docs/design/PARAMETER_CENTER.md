# 参数中心：一个参数一个权威位置，my-agent 可自助修改非安全参数

状态：阶段 0 已实现（分支 `claude/param-center-phase0`，2026-09-27）；阶段 1、2、2b 已完成，阶段 3 部分完成（A/B/D/E/C 组已合入 main，剩余少量内部键分派中），按下文顺序推进。

## 2026-10-01 模块级数值常数只读目录与守卫（P10，分支 `worker/ds1-constants-catalog`，已实现，待集成）

- **设计定案（常数不迁入登记表）**：常数留在读取它的地方（那里是唯一权威，符合“一个概念一个权威位置”；不搬进中央常数模块，
  也不变成用户配置项——用户已嫌参数太多）。目录是投影：`scripts/build_constants_catalog.py` 用 ast 静态扫描 agent_py_agent
  （不 import 产品代码），生成随包 JSON `agent_py_agent/config/constants_catalog.json`（**799 项**、328 个文件），
  供统一查找与查看；改常数就是改那一行源码。
- **目录条目**：名字、文件、计算值（数字字面量与简单算术）、单位（名字后缀推导 _SECONDS/_MS/_CHARS/_BYTES/_TOKENS/
  _COUNT/_PERCENT 等，推不出记空）、类别（上限预算/超时/比例阈值/重试/其它，按名字关键字顺序命中）、说明（紧挨着上方的
  中文注释行）；协议类不收（名字含 VERSION/STATUS/EVENT/SCHEMA/PROTOCOL/CODE 或以 _TYPE 结尾）。
  **目录不存行号**（2026-10-01 修订，3a 审查意见）：目录唯一失效时机是常数增删、改名、改值、改说明或改单位/类别；
  源码里插入空行/换行这类行号漂移不会让目录过期，避免“每个开发分支都得顺带改 JSON、天天冲突”。
- **守卫**（`test_constants_catalog.py`）：目录必须与源码一致（`--check`，不比较行号）；新增常数必须有单位后缀和上方中文说明；
  现有不合规常数全部进待整改白名单 `tests/fixtures/constants_catalog_pending_fixes.json`（685 个名字，历史遗留快照），
  白名单只短不长（已合规或已删除必须删条目）；协议类排除、单位/类别、扫描行为样例钉住；只在常数上方插入空行（行号变了、
  常数没变）时 --check 必须仍通过。
- **查看入口（只读）**：`/settings internal <关键词>`（TUI 与 IM 共用）与 user_config action=search 都能按名字/说明/文件搜到
  常数，显示文件:行、值、单位、类别、说明，并标明“代码常数，只读，改动需改代码”。行号不在目录里，由运行时只读打开那一个
  文件按名字定位（`settings/constants_catalog.locate_line`/`entry_with_line`），定位不到就只显示文件。
- **后续按模块分批整改**（改名、合并、补单位与说明）时以目录为准，每批完成后重新生成目录并删掉对应的白名单条目。

## 2026-10-01 登记表增加来源维度：三份配置纳入参数中心（P17，分支 `worker/ds2-registry-sources`，已实现，待集成）

> 2026-10-01 修订（3a 评审 5a51735c1 后）：capability 写入目标与运行值改走运行时实际读取的路径；runtime_guard 改为只读来源。
> 2026-10-01 删除 log_analysis 来源（分支 `worker/ds2-del-log-analysis`）：log_analysis_config.yaml 是死配置——产品代码无任何读取点，
> agent/log_analysis/ 包早已移除，仓库仅根目录 mutation_testing.py、mutation_tester.py 两个开发辅助脚本还引用旧模块（本轮不动）；
> 按“确定不用的旧字段、旧目录要删；配置必须真的生效”铁律删除。当前三来源 agent/capability/runtime_guard，合计 277 键。

- **来源维度**：`ParameterSpec` 增加 `source` 字段，三来源 `agent`（agent_config.yaml，224 键）/`capability`（capability_config.yaml，31 键）/
  `runtime_guard`（runtime_guard_config.yaml，22 键），合计 277 键；说明、类型、默认值
  从各随包 YAML 与 dataclass 取，规则与主配置一致（说明取 YAML 注释、无注释用键名兜底并计数）。
- **统一入口**：`/settings` 与 user_config 的 search/view/history/revert 三来源全覆盖；`_show` 加「来源：<source>_config.yaml」行。
  写入口仍只有 `parameter_changes` 一套，按来源选目标文件与正式加载器回读校验，写后回读不一致恢复原文件（或删除新建文件）并记
  settings-changes 账本，不另写一套写入：
  - **capability**：写入运行时实际读取的那份文件——`capability_config_for_agent(agent)` 用的路径（`agent.capability_config_path` 或
    `default_capability_config_path(agent.root)`，对 Gateway owner 即 `<owner home>/config/capability_config.yaml`），由
    `runtime_config_reload.capability_config_path_for` 解析；文件不存在时新建、只写被改的键（与主配置“只写非默认项”一致），
    用同一个 `load_capability_config` 回读，不一致恢复原文件/删除新建文件并报 PARAMETER_NOT_EFFECTIVE；回执如实写生效时机
    （capability 改动要重启 Gateway 才生效）。账本记在 agent 用户配置旁（settings-changes.jsonl）。
  - **runtime_guard**：运行时没有用户覆盖层（固定读随包 `DEFAULT_RUNTIME_GUARD_CONFIG_PATH`），参数中心里只读——
    可查看、搜索、看说明，修改一律拒绝并给结构化原因
    `PARAMETER_SOURCE_READ_ONLY`（“只能查看和搜索”），不新造运行时读不到的文件。
- **安全等级**：默认按边界（授权、权限、路径、执行权威链、运行时门、审批、白名单、执行开关、audit 一律模型不可改，用户经宿主入口改）；
  capability 的 17 个能力数值上限键为显式 free 名单（`_EXTRA_FREE_KEYS`，逐键带理由：模型/tool 上下文窗口、skill 检索上限、
  subagent 数量上限等纯数值容量类，不含任何授权语义）；runtime_guard 全部边界（且只读）。
- **运行值口径**：`running_value` 对 capability 按运行时路径读（owner 有覆盖时显示覆盖值，未传路径回落随包默认）；
  只读来源显示随包默认并标「随包默认、不可覆盖」（`/settings` 与 user_config view 的 source_readonly 字段）。
- **守卫**：`test_parameter_sources.py` 16 项（三来源 search/view、running_value 真实读文件与覆盖值、capability 写运行时文件且
  `capability_config_for_agent` 读到新值、文件缺失时新建只写被改键、set/reset/revert 链且运行时入口读回默认、只读来源结构化拒绝
  文件不动、回读失败恢复/删除、边界拒绝文件不动、user_config 工具 set/history/revert/deny 链路、view 标 source_readonly、
  随包 config 目录不再有 log_analysis_config.yaml）；
  变异验证 3 个（capability 写目标改回旧路径→运行时入口读不到新值、去掉只读拒绝→结构化码变 PARAMETER_BOUNDARY、running_value
  capability 忽略运行时路径→覆盖值不显示）均被杀红。
- **前端**：P17 给两份 YAML 补行尾说明注释后，`backend-config-catalog.json` 里 capability 组占位说明已同步为后端注释
  （用 `_descriptions_from_lines` 逐条替换）；2026-10-01 删除 log_analysis 来源后由生成器重新生成目录（270→250 项），
  `--check` 通过；`test_backend_config_catalog.py`/`test_frontend_settings_labels.py` 已跑通。

## 2026-10-01 Curator 固定档案引用（P12，本地已实现，待集成/部署）

- `memory_curator_model_profile`：字符串档案编号，默认空；空值沿用 owner 当前选中模型，不改成当前线程聊天模型。
  非空复用 `/model` 唯一解析器，要求模型与服务商存在、启用且支持 agentic，使用该连接自己的凭据、端点、协议、请求头及模型选项。
  `default` 是部署配置而非已保存档案，不能当固定编号；失效明确报缺配置，不会静默换模型。
- 安全等级是 boundary，登记表 `writable=False`：模型 `user_config` 的 set/reset/revert 都拒绝。
  仅已认证管理员的用户 `/settings` 调用内允许修改此键，授权不是 `actor=chat` 标签，退出清理；其余边界仍拒绝。
  配置依旧重启 Gateway 后生效，记在原参数修改账本，没有第二套配置或确认账。
- 用户操作：先在 `/model` 列表取得**档案编号**（不是临时列表序号），再发
  `/settings set memory_curator_model_profile <档案编号>`；查看用 `/settings show memory_curator_model_profile`，
  回到沿用用 `/settings reset memory_curator_model_profile`。TUI 与 IM 的详情都能看到运行编号/型号，未重启时另列保存编号/型号。
- 失败诊断仍放原 Curator run v2 的 `failure_diagnostic` warning JSON：`profile_id` 与 `profile_reason`，不含连接秘密或记忆正文。
  原因包括 `profile_not_found`、`capability_mismatch`、`profile_disabled`、`provider_disabled`、`credential_missing`、
  `catalog_invalid`、`catalog_unreadable`；不从说明文字判定。失败码仍为 `CURATOR_MODEL_NOT_CONFIGURED`，一小时退避不变。
- `memory_curator_provider`、`memory_curator_model` 已删除：不保留别名，不自动转换，旧配置仅按未知键告警。
  下文 P3 等旧推进记录保留原时点，不代表两键仍可用。
- 多 owner 时私有编号只在对应 owner 目录有效；需要全局共享连接时须采用已授权的 `shared:<编号>`，仍由原共享解析器核权。
  生产指向 deepseek-v4-flash 的实际编号由 3a 部署后设置，本线未读/改生产档案或配置。

## 2026-10-01 参数登记表元数据（P8，分支 `worker/ds2-registry-metadata`，已实现，待集成）

- 设计目标 1 落地：登记表每条参数补「单位、范围、归属模块、读取方」四项元数据，全部由 `agent/settings/parameter_metadata.py` 自动推导、
  不手工抄：
  - **单位**：按键名结尾后缀（_seconds→秒、_ms→毫秒、_chars→字符、_bytes→字节、_tokens→tokens、_percent→%、_days→天、
    _hour→小时、_turns→轮、_files→个文件、_requests→次），长后缀优先，推不出留空不猜；
  - **范围**：从现有规范化/校验规格取——`_memory_coercion._FIELDS`（含 compact_trigger/recovery_percent 的 50-100/25-80）、
    `runtime_tool_field_specs.TOOL_INT_FIELDS`、`services/_normalize` 的 GatewayFieldsService 整数/浮点规格与各枚举
    （model_backend、reasoning_effort/control、structured_output、log_level、access_mode、tool_catalog_mode、path_access_mode）、
    `backends/sampling.validate_top_p`（0-1），没有校验的留空；
  - **读取方与归属模块**：与 `test_config_field_readers` 同一套属性访问/字符串键引用扫描产品代码（排除 config.py、登记表自身），
    归属模块取主要读取方相对 agent_py_agent 的顶层（agent 包内细分到第二段）；`decision_*`/`memory_decision_*` 按
    `decision_config_fields()` 映射补读取方（decision_settings_defaults），与读取方守卫同一判据。
- 展示：`/settings show` 对有值的参数补「单位/范围/归属模块/读取方」四行（`settings_control_service._metadata_line`）；
  user_config 的 view/search 同口径只在有值时带这四个字段（`user_config_tool._spec_view`）。
- 守卫：`test_parameter_metadata.py` 7 项（推导规则各几例、真实字段挂接、范围表无空项、219 个用户可见参数都有读取方与归属模块、
  归属模块不含路径分隔符、两处展示只在有值时出现）；变异验证 3 个（删 _seconds 后缀、删 request_timeout 范围、field_readers
  跳过全部文件）均被杀红。

## 2026-10-01 前端设置页与前端数据清理（P4/P5）

- 设置页删掉 **29 个**对不上任何当前配置键的表单项（不在三份随包 YAML、也不在 `backend-config-catalog.json`）：Dispatch Loop 三项
  （max_consecutive_rounds/max_runners/limit）、scheduler_mode、daemon_apply/daemon_execute_runners、timeout、memory_limit/retention_days/
  enable_archive、enable_watchdog、feishu_webhook_url、qq_*（4 项）、max_input_length/max_path_length/forbid_dangerous_chars/
  path_whitelist_only、notification_store_path/notification_channel_timeout_seconds、subagent_builtin_workflows/subagent_user_workflow_dirs/
  subagent_workflow_review_rounds/task_max_grandchildren、reviewer_mode/auto_accept、workflow_mode。
- `settingsStore.ts` 与 `frontend-runtime-config.json` 同步清理：dispatch/memory/acceptance/security/notification/watchdog 整组删除；
  daemon（保留 runner_instruction）、runner（保留 concurrency/start_rate/timeout_seconds/dynamic_timeout_min/max）、subagent（保留
  task_max_subagents）、workflow（保留 enable_self_learning）、memoryAdvanced/tools 各死字段删除；model 删 timeout/anthropic_version/
  auto_bench_model_on_first_use。外部引用一并处理：Tools.tsx 预算窗口卡片（引用已删的 window_seconds）、authStore.ts 敏感字段清单。
- 保留项：audit/localStore/session 组、log_level、daemon_runner_instruction（在权威集合）；SettingsTools 的“检索限制”是前端本地设置
  （label 明确“不对应后端配置键”），不属于后端键，保留。
- 守卫：新增 `agent_py_agent/tests/test_frontend_settings_labels.py`（不依赖 node）：设置页所有 `label="键（` 必须都在权威键集合，
  settingsStore 引用的运行时配置组必须存在；YAML/目录键变化后设置页未同步会立刻失败。
- **验证限制**：`bun run build`（tsc -b && vite build）因本机 `frontend/node_modules` 缺失（tsc: command not found）跑不了，未联网安装；
  删除正确性由守卫与逐键核对（dead=0）支撑，未做真实 tsc/构建验证。

## 2026-09-30 新增参数

- `goal_continuation_idle_limit`：连续多少个持续目标自动续跑片没有工具调用或 Goal/任务结构化状态变化时自动暂停；默认 3，设为 0 表示不限。
  定义在 `AgentConfig`，随包 `agent_config.yaml` 的中文说明由参数登记表自动读取；按普通非安全整数参数登记，可从参数中心查看/修改，沿配置默认的 Gateway 重启生效语义。

## 2026-10-01 前端参数目录重新生成与守卫

- `frontend/config/backend-config-catalog.json` 按当前随包 YAML 重新生成为 **270 项**（agent_config 219、capability_config 31、
  log_analysis_config 20），`node frontend/scripts/sync-backend-config.mjs --check` 通过。
- 生成器 `frontend/scripts/sync-backend-config.mjs` 的注释归属规则与后端 `parameter_registry._descriptions_from_lines` 统一：
  空行或任何非注释行中断注释块（不再跨空行累积、不再截断到 8 行），无上方注释时取行尾注释（对齐 `config_io.yaml_trailing_comment`）；
  删掉 13 个已不在随包 YAML 里的映射条目；`restartRequired` 与后端 effect 语义一致（每个参数修改都要重启 Gateway 才生效），不再按键名猜。
- 新增 pytest 守卫 `agent_py_agent/tests/test_backend_config_catalog.py`（不依赖 node）：随包 YAML 键集合必须等于目录键集合（各文件分别比，
  失败列出差异），目录说明与后端逐键一致，`restartRequired` 全 true；同事改动 YAML 注释或键后目录未重新生成时会立刻失败。

## 1. 来源

2026-09-27 用户提出两件事：

- “代码里很多参数或者常数比较混乱，要一个个对比然后统一，最好有个专门的地方统一修改，而不是每个代码里改不同的参数还要猜参数是干啥的。”
- “希望 my-agent 的权限能多点，自己能改很多东西。”

同一天的真机实例：

- `max_tokens`：常量 `DEFAULT_MODEL_MAX_TOKENS`、随包 YAML、`AgentConfig` 默认值三处各写 16314；“输出上限 ≤ 窗口 ÷ 4”只在模型档案路径夹取，默认模型不夹；my-agent 找不到能改的位置（自助白名单只有 9 项）。
- `vision_*` 六项配置没有任何读取方（`analyze_image` 早已移除），my-agent 据此报告“看图额度只有 1024”。
- 同名不同义：`max_tokens` 同时表示模型单次输出上限、compact 地标预算（`LandmarkOptions.max_tokens`）和记忆归档的 token 预算。
- my-agent 查代码时看的是仓库旧提交，不是正在运行的安装包；它还以为“没有用户级配置”，实际用户配置由 `MY_AGENT_CONFIG` 指定（本机 `~/.my-agent/config/desktop.yaml`）。

## 2. 目标

1. 每个可调参数只有一个权威定义：键名、默认值、类型与范围、单位、中文说明、归属模块、安全等级、生效时机、读取方。
2. 随包 YAML、`AgentConfig` 默认值和文档都与权威定义一致，由测试核对，不再多处各写一份。
3. 用户和 my-agent 都能通过结构化入口（`user_config` 工具、TUI 与 IM 的 `/settings`）查看和修改非安全参数；每次修改记账、可回滚。
4. 模块里散落的“可调常数”（超时、上限、预算、阈值）迁入；协议常量（状态码、事件名、schema 版本）不迁。
5. 同名不同义的参数改名区分；没有读取方的配置删除（“配置必须真的生效”）。
   - 2026-10-01 落地（分支 `worker/ds1-mask-rename`）：压缩摘要“原话备份”段的 `LandmarkOptions.max_tokens` 改名
     `landmark_budget_tokens`（它是整段备份的 token 预算，不是模型输出上限，与 `max_tokens` 同名不同义）；数值不变，
     调用方与测试全量同步；只被测试调用的 `memory_archive.tokens.check_token_budget`（连同 `TokenBudgetResult` 与阈值表）删除。

## 3. 安全等级

| 等级 | 谁能改 | 例子 |
|---|---|---|
| `free` | 用户与 my-agent | 输出上限、超时、compact 百分比、展示与读取预算 |
| `boundary` | 只有用户经宿主入口 | 权限档位、路径模式、危险根、`additional_write_roots`、凭据、owner 身份、宿主数据根 |
| `secret` | 同 boundary，回显脱敏 | 各类 app secret、token、key |

`boundary` 从现有 `BOUNDARY_KEYS` 起步，是结构性拒绝，不是“暂时没开放”。硬门只守这一层（安全与客观事实）；其余参数只做类型与范围校验。

## 4. 修改、生效与回滚

- 写入用户配置文件（`MY_AGENT_CONFIG`），随版本升级保留；随包 YAML 只提供默认值。用户配置只写与默认不同的项，不整份复制随包 YAML（测试机曾整份复制，把新默认值全部遮住）。
- 生效时机如实报告：下一轮、下一次新会话或重启 Gateway（沿用 `user_config_capability` 的 effect 语义）。
- 每次修改追加一条账本记录（键、原值、新值、操作者、原因、时间）；`/settings history` 查看，`/settings revert <记录编号>` 回滚一次修改（编号见 history，允许至少 6 位十六进制前缀，不是键名；`control_commands.py` 的 `_SETTINGS_USAGE`）。
- 不设确认队列（用户没时间审批）：靠范围校验、账本和一键回滚兜底。

## 5. my-agent 改代码的权限（阶段 2）

- **写权限不用新做**：按 owner 的管理员授权早已存在，就是 F4 的 Full Access（存于该 owner 的 `tool_policy.json`，只有结构化
  local/main 能选；飞书私聊用 `/admin` 绑定后也按 local/main 运行）。它解除 owner 墙，子代理仍降回 WorkspaceOnly。
  `additional_write_roots` 是全局配置，对所有 owner 生效，不用于这件事。
- **缺的是“去哪改、改完交给谁”**：my-agent 曾把检出目录当成自己运行的代码，诊断连错三次。新配置 `self_dev_worktree`（边界项，
  模型不能改）指向一个独立分支的 git 工作树；本机管理员处于 Full Access 且该目录确是 git 工作树时，Owner Scope 提示词追加
  “my-agent 自身代码”一段：正在运行的代码位置（包目录，只读参考，部署整体替换）、开发工作树位置、改完跑相关测试并提交到当前分支、
  告诉用户分支和提交号，由集成者审核合并部署，不推送远端、不切分支、不改其他检出目录；调参数用 `user_config`。这段只是工作约定，
  不放宽也不收紧写权限（`prompting_parts/builder._self_development_guide`）。
- 推送主线与双机部署仍只由集成者负责，避免三方同时写主线。
- **运行中安装的写保护（2026-09-27，用户担心“my-agent 自己改自己代码会不会出事”）**：新边界开关 `protect_running_runtime`
  （默认开）让正在运行的安装目录对所有工具只读，Full Access 也不例外；my-agent 改代码只能落在开发工作树，改坏了不影响运行中的系统。
  核对时顺带确认：Full Access 主会话本来就没有 `allowed_write_roots`（不是目录白名单），文件工具可以直接写开发工作树，不需要额外授权。

## 6. 阶段

- **阶段 0（已实现）**：
  - 输出上限统一 65536，常量、随包 YAML、`AgentConfig` 同值，由 `test_model_output_cap.py` 核对。
  - 夹取公式只在 `settings.defaults.output_cap_for_window` 一处：已知窗口（模型档案或配置写了窗口，是否显式都算）时取 min(配置, 窗口 ÷ 4)，未知时按配置值。
    后端工厂构造时用它算出 `backend.max_tokens`，这就是实际发送值，请求体与输出预留直接读它；没有 max_tokens 的后端（测试替身、echo）
    由 `call_runtime.max_output_tokens` 按同一公式估算。第一版（9207d54e5）只夹显式窗口，且替身估算回退到未夹取的 65536，
    “窗口×0.8−输出上限”的 compact 预算在 8 万以下窗口变成 1，8 个 compact 用例回归（dsh-9b 的 CI 监视发现），已按上述规则修正。
  - 模型短测（`_probe`）改用正式上限，能发现供应商不接受该上限的情况。
  - 删除没有读取方的 `vision_*` 配置；用户配置残留时只告警。
  - 验证命令被管道、`;`、`||` 或后台掩盖返回码时，给出结构化“未计入”事实（见 `docs/modules/verification/02-progress.md`）。
- **阶段 1（盘点，2026-09-27 首轮已做）**：只读脚本扫描产品代码（不含测试），结果：
  - `AgentConfig` 391 个字段，其中 11 个没有任何读取方：阶段 0 删了 `vision_*` 六项，这一轮再删 `lsp_servers`（LSP 工具已移除）、
    `scheduler_mode`、`extensions_dir`（扩展改为按 `extension_plugins` 列表加载）、`continuation_reminder_seconds`（9-15 目标改为事件驱动后
    定时续跑已删）、`task_max_grandchildren`（从未实现）。另有 36 个 Jev 决策字段由 `decision_config_fields()` 按名字映射读取，不算死配置。
    新增 `test_config_field_readers.py`：每个字段都必须有读取方，以后不会再攒下死配置。
  - 模块级数值常数 699 个，分布在 288 个文件：上限/预算类 455、超时类 152、比例/阈值 21、重试 11、其它 60；32 个常数名在多个文件各定义一份。
  - 同名不同义：函数参数 `limit` 267 处、`timeout` 72 处、`max_chars` 69 处、`budget` 38 处、`max_tokens` 10 处，含义随模块变化。
  - 迁移批次按“用户最常问、最常调”的顺序排：模型请求（输出上限、超时、重试）→ compact 与上下文预算 → 工具输出与读取预算 →
    子代理与调度 → 记忆与检索 → 其余。每批先把常数收进登记表并证明读取方真的读到，再删原常数。
- **阶段 2（基础设施，2026-09-27 已实现，开发工作树除外）**：
  - 登记表 `settings/parameter_registry.py`：每个 `AgentConfig` 字段一条，说明取自随包 YAML 该键正上方的注释（没有再取行尾注释），类型取默认值的真实类型；
    安全等级按显式名单与键名记号判定（凭据、权限、身份、路径、外部地址、端口、服务/插件、提示词、审计、锁、执行权威链、内部元数据
    等为边界），`TUNABLE_KEYS` 可显式放行（飞书凭据，回显脱敏）。首轮结果：386 项中 286 项模型可改，其余为边界。
  - 写入口 `settings/parameter_changes.py`：只写当前进程实际加载的用户配置（`user_config_path(config)`；原来只看 Gateway 从不设置的
    `MY_AGENT_CONFIG`，模型因此误报“没有用户级配置”）；布尔、数字不加引号，写后用正式 `load_config` 回读，类型或值不一致就恢复原文件；
    列表/映射、边界项、随包 YAML 拒绝；每次写入记入用户配置旁的 `settings-changes.jsonl`（最近 500 条），可按编号前缀回滚、可连续回滚；
    凭据类只记脱敏值、不可回滚。生效时机如实为“重启 Gateway 后”（压缩百分比与读文件上限原来误报“下一次会话”），管理员可发 `/restart`。
  - 模型档案字段（2026-09-27，分支 `claude/be-effort-probe`）：同一写入口新增 `set_profile_field` / `revert_profile_change`，
    只改白名单 `PROFILE_FIELDS`（目前只有 `reasoning_control`，供智能程度自动检测写入），只改当前用户自己的私有档案；在模型档案
    文件锁内读改写整行、经 `validate_model` 校验，由 `model_profiles._save_profiles` 原子保存（与 `/model` 编辑同一入口）。记在该用户
    档案旁的 `.changes.jsonl`：与配置账本同一格式、同一脱敏与回滚规则，另带 `target`（档案编号与模型名）；按编号前缀撤销只查本人账本。
    下一轮对话起生效（每轮都会重新读取模型档案）。详见[智能程度](REASONING_EFFORT.md)第 8 节。
  - my-agent 的 `user_config` 工具新增 search/reset/history/revert，set 走同一写入口；聊天 `/settings`（TUI 与 IM，仅管理员）
    提供总览、搜索、详情、修改、恢复默认、记录与回滚。
- **阶段 2b（2026-09-27 已实现）**：开发工作树约定，见 §5。原计划“先做只给管理员的写入授权”核对后发现多余：F4 Full Access 就是
  按 owner、仅管理员的结构化授权，本机管理员从 9-21 起一直开着。只新增 `self_dev_worktree` 配置与提示词一段；工作树由集成者建在
  `~/my-agent-worktrees/my-agent-self`（分支 `my-agent/self-dev`，基于 main），本机用户配置填上该路径。
- **查看补充（2026-09-27，来自 my-agent 在开发交流板的提问）**：
  - 配置值与实际使用值不同的参数，在登记表 `_APPLIED_RULES` 登记派生函数（max_tokens → `effective_max_output_tokens`；
    推理强度 `model_reasoning_effort` 见下一条），公式仍只在原处。`applied_value(key, config)` 给查看入口用：`user_config` 的 view/search 按本片会话模型给
    `applied_value`/`applied_rule`，`/settings show` 按默认模型多一行“实际效果”（原叫“实际使用值”，既显示数字也显示一句话，
    2026-09-27 改名），并注明 /model 切换过的会话可能不同。
  - 删除 `read_config_fact` 里旧白名单遗留的 `tunable` 字段：模型同时看到 writable=true 与 tunable=false，误以为 max_tokens 改不了；
    能否修改只看登记表的 `writable`。
- **说明与实际效果补充（2026-09-27，集成者派活，分支 `claude/be-param-descriptions`）**：
  - 说明也读行尾注释：键正上方的注释优先，没有再取 `key: 值  # 说明` 的行尾部分；判断“什么是注释”复用加载器同一条引号规则
    （`config_io.yaml_trailing_comment` 与 `_strip_yaml_comment` 同源），引号里的 `#` 不算。389 个字段里空说明从 216 降到 177，
    其中 39 个是原来没读到的行尾注释。
  - 守卫：说明为空的字段只允许出现在 `agent_py_agent/tests/fixtures/parameter_description_baseline.json`（按原因分组）。名单外新增空说明、
    或名单里的字段已有说明或已删除，`test_parameter_registry` 都失败，名单只会越来越短。
- **P3 补齐保留键说明（2026-10-01，分支 `worker/ds2-param-parity`）**：此前基线第二组 8 个保留 advanced 键无说明（dynamic_timeout_min、
  home_lesson_stale_caveat_days、local_store_fts_enabled、memory_curator_daily_finalize_hour、memory_curator_model、memory_curator_provider、
  memory_hot_min_occurrences、memory_lesson_min_occurrences）。已把它们的中文说明写进随包 agent_config.yaml 各键正上方注释；后台
  Memory Curator 原整段挂在 `memory_curator_enabled` 上的注释块拆开归到各自的键，删掉已删除键（batch_message_limit、max_retries）的说明。
  基线现在只剩 5 个加载器写入的运行时元数据键（config_layers / config_path / config_sources / config_warnings / memory_config_warnings），
  `test_parameter_registry.py` 通过。
- **P7 全量默认值一致性测试（2026-10-01）**：新增 `test_config_defaults_parity.py`——用正式加载器读随包 agent_config.yaml，
  对 `AgentConfig` 全部字段逐个断言加载值等于 dataclass 默认值（219 个 YAML 键当前全部一致，无值级白名单项）；
  5 个运行时元数据键跳过值比较但断言仍存在（键被删即失败）；值级白名单为空、结构保留（放进去的键必须写明原因且仍不一致，
  变一致即失败，防止白名单烂掉）。api_key 由 `api_key_env` 指向的环境变量注入，测试里先清掉再加载，避免本机环境污染。
  实现了目标第 2 条“随包 YAML、`AgentConfig` 默认值和文档都与权威定义一致，由测试核对”的全量落点。
  - 推理强度如实生效值：`model_reasoning_effort` 登记派生规则，读取函数与 `/effort` 回执同一组合——控制方式只由
    `resolved_reasoning_control(model_reasoning_control, api_base, model_backend)` 决定，说明只由 `describe_reasoning_effect` 生成，
    不另写支持判断。用户当前用的 opencode.ai 不在已确认名单里，查看时如实显示“当前模型不支持调节……本设置暂不改变请求”。
  - 修改回执：`applied_value_with(key, 新值, config)` 用只读视图把新值套进同一派生规则。`user_config` 的 set 回执多给
    `applied_value`/`applied_rule`/`applied_basis`（按本片会话模型），聊天 `/settings set` 多一句“按新值在默认模型上的实际效果”；
    只对 `_APPLIED_RULES` 里的键生效，没有专项分支。
- **脱敏与边界收紧、说明纠正（2026-09-27，同一分支第二个提交，分类子代理发现、集成者派活）**：
  - 凭据判定只有一处：`user_config_capability.is_credential_key`，键名等于凭据名或以 `_api_key/_secret/_password/_token/_cookie(s)/
    _credential(s)/_encrypt_key` 结尾才算（完整片段，不按子串）。登记表 `is_masked`、`mask_value`（/settings、user_config 回显）与
    命令行 `config-get` 都用它。复核时发现比误伤更严重的问题：原 `mask_value` 只认 3 个飞书键，`api_key`、`gateway_auth_token`、
    `qq_app_secret`、两个 `*_embedding_api_key`（现已合并为 `embedding_api_key`）在 `/settings show` 与 user_config 查看里是明文；`config-get api_key` 也明文打印。
    `input_media_token_reserve`（token 是计数单位）不再算凭据：原来它被判为边界，修改记录还标成凭据、不能回滚。
  - 边界记号分两类：凭据、权限审批、管理员、访问与信任名单、网络与外部地址、端口、请求头回调、环境变量、锁仍对所有参数生效；
    指向类记号（路径与目录、身份、服务与插件、提示词、审计、令牌与键）只对文本、列表、开关类参数生效——整数和小数只是上限、
    超时、间隔或数量，不会指向任何东西。凭据键不论类型都是边界。`cli_audit_cleanup_days`（审计记录保留期，缩短会提前删掉审计证据）
    显式列入边界名单；飞书私聊闲置锁定时长靠“锁”记号保持为边界。
  - 前后差异：边界 107 → 93，移出 14 个数字旋钮（`background_owner_wake_rescan_seconds`、`background_owner_workers`、
    `background_pending_wake_prompt_limit`、`background_threads_per_owner`、`cli_audit_limit`、`decision_model_selection_prompt_max_chars`、
    `gateway_service_command_timeout_seconds`、`home_lesson_auto_read_limit`、`home_lesson_stale_caveat_days`、`input_media_token_reserve`、
    `memory_resume_recommended_read_paths_limit`、`owner_agent_idle_seconds`、`owner_agent_pool_max_agents`、
    `owner_maintenance_scan_interval_seconds`），没有新增；脱敏 9 → 8（只移出 `input_media_token_reserve`）；模型可改 287 → 301。
    路径、写入范围、飞书/QQ 凭据、owner 身份、访问锁与审计保留期全部仍是边界，由测试逐项钉住。
  - 说明纠正：“额外动态 prompt 文件”注释原先压在 `additional_write_roots` 上方，它拿到了 prompt_files 的说明、prompt_files 反而没有；
    两个键各写对说明。`lease_stale_without_heartbeat_seconds` 原说明写成 Gateway 租约，实际管审计来源采集 worker：单次模型尝试
    上限（夹在 60～900 秒）与 worker 租约有效期（至少 30 秒），按实际改写。空说明基线 177 → 175。
  - 恢复默认与回滚的回执也附“按新值的实际效果”（`parameter_changes.applied_after_change`：回到默认时按登记默认值算），
    与修改同一口径；`user_config` 的 reset/revert 回执同样多给 `applied_value/applied_rule/applied_basis`。
- **字典与列表参数的结构脱敏（2026-09-27，分支 `claude/be-structured-masking`，基于 `f824b6c10`）**：
  - 问题：`mask_value` 只看顶层键名，再把整个值 `str()`。`model_custom_headers`（可能放 Authorization、x-api-key）、`mcp_servers`
    （每个服务器的 env 与 args 里可能有令牌）在 `/settings show`、user_config view/search 里整段明文，模型经 user_config 就能看到。
    `model_auth_ref` 核对过：装的是引用（服务商编号、登录方式、代次、配置哈希与目录文件路径），不是凭据本身。
  - 规则（`user_config_capability.masked_structure`，`mask_value` 是它的文本版，全部出口共用）：顶层键是凭据就整值遮住；
    否则按结构递归——请求头与环境变量容器（按容器名最后一段是 headers/header/env/environ/environment 认，不按请求头名写死名单）
    的值一律遮住、只留键名；嵌套映射里键名命中 `is_credential_key` 的遮值；列表里 `--凭据名=值` 遮值、`--凭据名 值` 遮下一项，
    名字是凭据的 `名字=值` 遮值（docker 的 `-e GITHUB_TOKEN=…`），`--header`/`--env` 这类请求头/环境变量开关的值
    （`名字: 值`、`名字=值`）只留名字（mcp-remote 的 `--header "Authorization: Bearer …"`），`--开关=网址` 的网址照样处理；
    网址里的密码与名字是凭据的查询参数遮值（修改记录里带 YAML 引号的文本先剥引号）。返回同形副本，普通字典与列表照常显示。
  - 当时的已知边界（单字母开关不认、`-e DB_PASS=…` 不遮、libpq 关键字连接串不遮、顶层文本不拆 `名字=值`）已由下一条补齐。
  - 出口清单：登记表查看入口（user_config view 的 parameter/fact、search 的摘要）、聊天 `/settings` 的总览/查看/搜索/历史、
    set/reset/revert 回执（`previous`/`saved` 取自已脱敏的记录，`effective` 经 `mask_value`）、`parameter_changes` 记账
    （所有键都脱敏后才写；脱敏改动了值的记录标 `masked`，回滚拒绝，避免把 `***` 写回配置）、user_config 与 `/settings` 的
    历史回显（`displayed_change` 读出后再遮一次，旧记录也不漏；回滚内部仍读原记录）、命令行 `config-get`（改为传原始值）。
- **脱敏边界补齐（2026-09-27，分支 `claude/be-masking-edges`，基于 `946a26783`）**：集成者定的方向是宁可多遮——显示上多遮一点
  只损失一点可读性，漏遮就是泄露。
  - 任意开关（含单字母 `-H`、`-e`）后面的一项，形状是“请求头行”（`名字: 值`，名字由 HTTP token 字符组成）或 `名字=值` 时只留名字；
    形状是结构事实，不需要知道开关在各个程序里的含义。`:` 后紧跟 `//` 的是网址，交给网址规则。代价是 `-e LOG_LEVEL=debug`、
    `--addr 127.0.0.1:8080`、`--config C:\…` 这类普通值也会被遮。
  - 凭据名补常见写法 `pass`、`passwd`、`pwd`、`private_key`、`secret_key`、`access_key`、`auth`，仍按完整末尾片段认（`DB_PASS`、
    `SSH_PRIVATE_KEY`、`BASIC_AUTH`）；名字里的 `.` 也当分隔符（`spring.datasource.password`）。`is_credential_key` 同时决定边界分类，
    逐个核对登记表 389 项：没有新命中（`auth_enabled`、`access_mode` 不以它们结尾），脱敏和边界的数量都不变。
  - 顶层文本、映射里的文本和列表项都拆空白分隔的 `名字=值`，名字是凭据的遮值：libpq 的 `host=… password=…` 会被遮住，
    引号括起的值整段遮，引号里的连接串也拆开检查；文字中间夹的网址也逐个找出来遮密码。文本参数因此会被标 `masked`、不能回滚，
    这是对的：本来就不该把 `***` 写回配置。
  - 一项自身是 `名字=值` 且名字有角色时，整项都算值（`--password=带空格的值`、`--header=名字: 值`、`--env=名字=值`）。
  - 凭据名下面是映射时（`auth: {type, token}`），保留键名、值逐个遮住，结构仍看得见。
  - 分两项写法与密钥形态的值（2026-10-01 补齐，分支 `worker/ds1-mask-rename`）：请求头/环境变量容器开关（`--headers`、`--env`）后
    “名字 值”分成两项时，名字和值两项都遮（名字可能是 Authorization/x-api-key 这类敏感请求头名；`--api-key VALUE` 仍是单值，只遮值）；
    不紧跟开关、名字又不像凭据的单独一项 `名字=值` 里，值形如密钥（常见 token 前缀如 sk-/ghp_/eyJ，或 >=24 字符的字母数字混用长串）
    也遮值，规则只看结构不写死服务商，普通值（主机名、纯数字、网址、路径）不遮。
- **回显值文字统一（2026-09-27，分支 `claude/9a-mask-value-display`，基于 `eb7c639a1`）**：`mask_value` 是所有回显出口唯一的
  值文字口径——非凭据的布尔按配置文件写法显示 true/false，数字照实显示（False、0 不是空值），None、空串、空列表、空映射显示空串，
  凭据键整值遮住；聊天 `/settings` 把空串显示成“（空）”，这是唯一的界面差异，给模型的 JSON 仍是空串。
  - 问题：原 `mask_value` 是 `str(x or "")`，简易 YAML 又把 `false`、`0` 解析成 False、0，于是 user_config 的运行值与默认值、
    查看报告（`read_config_fact` 的 effective/user_value/packaged_default）、set/reset/revert 回执的 `effective`、命令行 `config get`
    都把 False、0 给成空串（模型读到“没配”），True 显示成 Python 写法 `True`；`/settings` 另写了一份布尔、数字特判绕开它。
  - 做法：一处根修 `mask_value`，删掉 `/settings` 的特判（同一概念只留一个实现）。修改记录记的是 YAML 原文字符串，`masked` 判定与
    回滚不受影响。
  - 守卫 `test_value_display_parity`：False、0、True、None、空串、空列表、空映射、凭据 8 种值 × user_config view 的 fact 与
    parameter、search、改参回执、`/settings show` 的运行值与用户配置值、`config get` 7 个出口，全部走真实配置文件与 `load_config`；
    走不到的 5 格（YAML 写不出 None；列表、映射不在聊天里改；凭据不能写）由测试核对确实走不到。
- **阶段 3（迁移）**：按模块分批整改常数并改名。每批开工前在协作文件贴出文件清单，避开 Codex 正在改的文件。
  - **方向调整（2026-10-01，P10 基础设施，分支 `worker/ds1-constants-catalog`）**：常数**不再收进参数登记表**。
    理由：登记表是“用户可调参数”的权威（含可改性、安全等级、生效时机），而模块级数值常数大多只对程序内部有意义，
    迁进去只会让用户看到几百个“不能改”的条目，违背“用户嫌参数太多”的方向；搬进中央常数模块又会制造第二权威。
    新设计：常数留在读取点（唯一权威），由只读目录投影（见本文顶部 P10 节），用户和 my-agent 用
    `/settings internal` 或 user_config search 查“这个常数在哪个文件哪一行、值多少、单位是什么、干什么用”，
    要改就去那一行改；每批整改（改名、合并、补单位与说明）后重新生成目录并删掉待整改白名单里对应条目。
    目录不存行号（行号由查看入口运行时按名字定位，行号漂移不会让目录过期，避免各分支天天在目录上冲突），
    唯一失效时机是常数增删、改名、改值、改说明或改单位/类别。
    与旧计划“每批先把常数收进登记表并证明读取方真的读到，再删原常数”的差别：不再搬进登记表，也不删原常数，
    目录只增不改动源码；改名/合并仍按原规则做（同名不同义改名、同一概念收成一处、删死常数）。
  - **第一批：同名常数（2026-09-27）**。盘点时 32 个常数名在多个文件各定义一份。
    - 删掉没有读取方的 `CONTEXT_WINDOW`（TUI 两份）与 `RECENT_ARCHIVE_FILE_LIMIT`（记忆诊断两份，实际读配置）。
    - 同一概念收成一处：聊天历史轮数/预览字数、折叠预览字数、后备扫描上限（`tooling/_filesystem_helpers`）、响应预览下限
      （`web_http_helpers`）、源抓取时限（`ingestion/puller.SOURCE_FETCH_TIMEOUT_SECONDS`）、适配器领取时限（ingress 直接用
      `GatewayClaimLeaseConfig` 默认）、策略失败退役次数（`conversation/store_progress`）、压缩失败熔断阈值与冷却
      （`memory_archive/compact_circuit_breaker`，原 context_compactor 另有同值副本和不同名的冷却常数）、流式分块读取上限
      （`gateway_parts/io.STREAM_CHUNK_READ_MAX_BYTES`）。
    - 名不副实的改名：网页抓取里按字节用的 `_MAX_BODY_CHARS` 改为 `_MAX_BODY_BYTES`。
    - 剩下 19 个同名常数确属不同含义或有意独立（如两套密码散列的 scrypt 参数、各持久格式版本号），列入
      `test_constant_names_unique.py` 的白名单并写原因；以后新增同名数值常数即测试失败，白名单里的名字不再重复也失败。
    - 不改任何数值，行为不变。
  - **第二批：决策点请求字符上限（2026-09-27，my-agent 在分支 `my-agent/self-dev` 完成，集成者审核合并）**：三个决策点位共用的当前请求字符上限
    `decision_request_max_chars`（集成时按用户要求改为：默认 2000，超出取首尾节选并标注、不再整点跳过，`0` 表示不截取；
    一般用户不用改）。原 `_MAX_REQUEST_CHARS` 三份常量
    （`decision_planning.py`、`tool_context/decision_delivery_quality.py`、`tool_context/decision_action_candidate.py`）删除，
    统一由 `settings/defaults.py::decision_request_max_chars(config)` 读取；非整数、负数或缺字段回落默认值。
  - **方向调整（用户 2026-09-27）**：“几百个参数是不是太多了，有些可以合并，有些可能没用了”。本机用户配置只改过 11 项
    （测试机 298 项是当年整份复制随包 YAML）。下一步先把测试机配置收成“只写与默认不同的项”，再对 386 个配置项逐项分类：
    删除（无用）、合并（总一起调）、降级（只对程序内部有意义，改回代码常数并移出配置）、常用（`/settings` 默认只列二三十个）、
    高级（可搜索但不刷屏）。目标约 100 项；每批不改任何生效值。
  - **减量收口（2026-10-01，三线收尾 goal P11，集成者裁定）**：“约 100 项”的目标更正为实际下限。随包 YAML 现有 219 个键，
    `/settings` 列表与搜索显示 218 个（`model_auth_ref` 由 /model 登录写入、请勿手填，归入 `MANAGED_ELSEWHERE_KEYS` 只隐藏不删，
    `/settings show` 仍可查看）。复核余下候选（`orphan_supervision_interval_seconds`、`owner_maintenance_scan_interval_seconds`、
    `skill_guard_max_files/_size_kb`、`lease_stale_without_heartbeat_seconds` 等）：分类时都有明确保留理由（出事时置 0 的急停开关、
    外部 skill 安装扫描上限、租约时长），生产配置也没有覆盖它们，降级为常数只会拿走排障手段；常用层 21 个、其余可搜索不刷屏，
    用户日常不受参数数量打扰。之后不再按数量目标减量，只在“没有读取方”（`test_config_field_readers.py` 守卫）或“同一概念多个旋钮”时删并。
  - **参数减量第 2 批：11 组重复参数合并（2026-09-27，集成者派活，分支 `claude/9a-merge-config`）**：同一个概念只留一个旋钮。
    被吸收的键从 AgentConfig、随包 YAML、规范化、说明基线与测试里删除，不留别名、不自动转值；写在用户配置里只告警
    “unknown config key”并忽略。默认行为全部保持；只有显式写过被吸收键、或写了原来不生效的值的配置会变（见各条）。
    - `runner_concurrency` ← `runner_auto_concurrency`：auto/空 = 8，数字 = 上限，`0` = 不限制（原来 `"0"` 实际被夹成 1）。
    - `background_claim_ttl_seconds` ← `background_claim_heartbeat_interval_seconds`：续约心跳始终按 TTL 推导（约 1/3）；
      子代理 runner 会话心跳固定 5 秒（原来借用同一个键）。
    - `max_parallel_tool_calls` ← `max_tool_calls_per_round`（配置）：空 = 8，`0` = 不限制，数字 = 上限。修掉两处不生效：
      线程池写死 `min(8, …)`（大于 8 的设置和 0 都只跑 8 个），规范化最小值卡在 8（0 与 1–7 写不进去）。后台工作片仍按
      守卫文件 `background_max_tool_calls_per_round` 写任务属性 `max_tool_calls_per_round`，与并发上限取小。
    - `tool_agent_budget_max_calls` ← `tool_agent_budget_window_seconds`：窗口固定 600 秒；空/0 = 关闭，默认关闭（与原来空值
      遮蔽守卫文件后的实际效果一致）。守卫文件里从来读不到的 `tool_agent_budget_*`、`max_tool_rounds` 副本删除。
    - `tool_artifact_read_budget_max_chars` ← 窗口参数：窗口固定 600 秒，`0` = 不限制。
    - `memory_archive_level` ← `memory_hook_archive_level`；`memory_rule_routing_mode` ← `memory_rule_routing_enabled`（false = off）；
      `memory_resume_auto_context_mode` ← `memory_resume_auto_context_enabled`，默认改为 off，单次显式开启仍强制 always。
    - `model_context_window_tokens` ← `model_context_window_explicit`：默认改为空。空 = 服务商元数据/探测，都没有按 128000
      （`settings/defaults.DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS`；原解析兜底 200000 在默认配置下走不到）；数字 = 显式容量。
      构造期输出上限、Skill 索引预算、决策输入里的“当前窗口”在空时按 128000，与原默认值一致。
    - `temperature` ← `model_temperature_explicit`：默认改为空；空 = 不发送，数字 = 发送；`/model` 档案带温度照旧发送。
    - `runner_failure_retry_limit`（AgentConfig，唯一的家）← `runner_failure_policy` 与守卫文件的 `runner_failure_retry_limit`、
      `same_run_redispatch_limit`：普通失败后最多重跑几次，默认 1（等于原来的实际效果：守卫 2 次被同 run 上限 1 压成 1 次），
      `0` = 不重跑；临时供应失败仍按 `provider_transient_redispatch_limit` 取大。放 AgentConfig 而不是守卫文件：守卫文件只从
      随包路径读、部署会覆盖、`/settings` 与 `user_config` 看不到。原来 `1` 被当成“不重跑”、`off`/`0` 实际还会重跑一次，现统一。
    - YAML 里 `key:` 留空会读成 `[]`：整数旋钮与温度都按“没填”处理，不再每次启动告警。
  - **参数减量：嵌入服务合并与改名（2026-09-27，集成者派活，分支 `claude/9a-embedding-merge`）**：记忆语义召回与工具语义检索
    共用一个嵌入服务。`tool_embedding_model/api_base/api_key/api_key_env` 删除；`memory_embedding_*` 改名为 `embedding_*`
    （模型、端点、key、key 的环境变量名），参数中心归到“模型请求”，安全等级不变（模型可在聊天里改，端点、key、key 环境变量名是边界）。
    两个功能开关保留、各管各的：`memory_semantic_recall` 只管记忆召回，`tool_vector_search_enabled`（默认开）只管工具检索。
    旧键只告警并忽略，不留别名、不自动转值；错误码 `MEMORY_EMBEDDING_MODEL_MISSING/INIT_FAILED` 不改名（描述的是语义记忆功能的状态）。
    - 读取链：`core._embedding_client` 是唯一构建入口（`embedding_model` 空 = 不建客户端、不发请求；`embedding_api_base` 空沿用
      `api_base`；key 链 `embedding_api_key` > `embedding_api_key_env` 指向的环境变量 > 聊天 key）；`_build_memory_embedder` 与
      `_build_tool_embedder` 只各自判断开关。原 `tool_embedding_*` 是逐字段覆盖、空则回落 `memory_*`，所以没配过它们的部署行为不变。
    - 行为变化只有两种：配过 `tool_embedding_*` 且与记忆那组不同的，工具改用共享服务（工具向量只在进程内存里，重启即重建，无数据迁移）；
      迁移时如果 `memory_semantic_recall` 已开而记忆模型为空（当前降级为关键词），把工具的模型搬进 `embedding_model` 后记忆召回会从降级
      变成可用，并开始写 `memory_vectors.json`（集成者确认接受：用户本来就开了语义召回）。
    - 部署迁移规则（每个实际加载的用户配置与运行时配置层文件）：`memory_embedding_X` 改名为 `embedding_X`、值原样搬；记忆召回在用
      （开关开且模型非空）时以记忆的值为准，直接删 `tool_embedding_*`，不能把工具的模型搬进共享键——`memory_vectors.json` 不记录生成
      模型、只有维度守卫，同维度换模型会静默混用向量空间；记忆召回没在用时，`tool_embedding_X` 非空就逐字段覆盖共享键再删。
      2026-09-27 核查（只看计数）：本机与测试机在用配置都没设这 10 个键，本次部署无需迁移。
    - 同批清理：`retrieval/embedding.build_embedder(dict)`、`_resolve_api_key` 没有产品调用方，删除；`LocalHashingEmbedder` 只被测试
      当确定性替身，原样搬到 `tests/_hashing_embedder.py`。回归见 `test_embedding_service.py`（此前 `_build_tool_embedder` 没有测试）。
    - 未落地方向：`/model` 目录已有 `embedding` 用途但没有运行时消费者，长期可改为引用档案（复用服务商凭据，4 个平铺键收成 1 个引用）；
      向量库记录生成模型、不匹配的向量视为不存在并提供重新嵌入，方案另写。

- **减量第一批（2026-09-27，分支 `claude/38-delete-dead-config`，已合入 main `8f73a512c`，双机 step13s）**：按分类结论逐项复核后删除 43 个没有产品读取方的配置项
  （只在 `settings/config.py`、随包 YAML、归一化表或字段规格表里出现，或只被孤儿模块/测试/离线验收入口读取）。同批处理：
  - 孤儿模块及其专属测试一起删：`agent_core/watchdog.py`、`concurrency/task_lock.py`（含 `LockAcquisitionError`）、`external_knowledge/`。
  - `contracts/real_run_review.py` 与 `contracts/small_real_acceptance_gate.py` 保留，配置读取换成模块常量（5000000 / 1000000 / 900，数值不变）。
  - `tool_protocol` 字段删除：运行时协议只由 `agent_core/native_tool_protocol._NATIVE_PROTOCOL` 决定，归一化器不再放行 `text`。
  - `subagent_memory_retention_policy` / `subagent_memory_delete_after_days` / `subagent_destroy_summary_required` 删除后，任务记录
    `attributes.memory_scope` 升为 `subagent_memory_scope.v2`（只写 namespace 与 auto_promote）；没有摘要、哈希或签名覆盖旧记录，残留键无读取方。
  - `DispatchRuntimePolicy` 去掉只在 snapshot 回显的 `active_interval` / `idle_interval`，snapshot 升 `dispatch_runtime_policy.v2`。
  - `cli/models.py::SubagentsAcceptanceOptions.execute_tests` 一并删除。加载器对旧配置里残留的键只告警（unknown config key），不阻断启动。
  - 空说明基线 `parameter_description_baseline.json` 从 177 缩到 143：34 项删除，另有 2 项（`conversation_pending_wake_limit`、
    `lease_stale_without_heartbeat_seconds`）因上方注释块重新归属而有了说明。前端 `frontend/config/backend-config-catalog.json` 是由随包 YAML
    生成的目录，在本批之前已经过期，本批未重新生成；前端设置页仍有 12 个已删键的表单项，留给前端单独清理。
    （2026-09-27 由分支 `claude/9a-batch3-e` 处理：目录按当前随包 YAML 重新生成为 313 项，`npm run check:config` 通过；设置页共清掉
    24 个已删键的表单项——本批 12 个、第 2 批被吸收的 6 个、第 3 批 A/E 组降为常量的 6 个，看门狗整段一并删除。store 与
    `frontend-runtime-config.json` 里这些字段的默认值暂留，等能跑 tsc 类型检查时再清，见 ROADMAP。**（2026-10-01 由分支 `worker/ds1-frontend-settings` 清掉 store 与 JSON 里的残留字段，ROADMAP 对应条目已关闭。）**）
  - **决策点位的期限与模型引用只留覆盖层（2026-09-27，集成者，分支 `claude/decision-point-fields`）**：12 个点位的
    `timeout_seconds`/`profile_id` 原来在 agent/memory/capability 三份配置里各有一个字段（24 个），与用户长期设置、会话设置里
    按点位覆盖是同一概念的两个家。现只保留覆盖层：配置里只有通用 `decision_timeout_seconds`、`decision_background_timeout_seconds`、
    `decision_profile_id` 与各点位 `*_mode`，点位没有覆盖时继承通用值。默认值全是空（继承），两台机器在用配置均未设置，行为不变。
  - **常用层级（2026-09-27，集成者派活，分支 `claude/9a-settings-common-view`）**：用户说“把用户当傻瓜”，99% 的人不会手动调参数，
    但命令要保留。`parameter_registry.COMMON_KEYS` 登记配置分类里标 common 的 21 个键（封闭的产品决策名单，不是开放世界的类型识别），
    登记表对这些键给 `ParameterSpec.common = True`，只用于展示与推荐，不参与放行。`/settings` 不带参数只列常用参数（当前运行值、
    是否改过、说明第一句；改了没重启注明“发 /restart 后生效”），并用一句话说一共多少项、看全部发 `/settings all`；
    `/settings all` 是原总览（总数、可改范围、用户配置位置、改过的个数、最近修改）加按分类的全部参数清单。search、show 不变。
    `user_config` 的 search/view 结果多一个 `common` 字段，工具说明提示模型优先从常用参数里推荐。守卫（`test_parameter_registry`）：
    名单里的键必须存在、不能是安全边界项；常用参数必须都有说明、不能留在空说明基线里（原过渡项 `memory_compact_auto_trigger_percent`
    已由下条补齐，过渡名单已删除）。`/settings` 回执里的值文字只由 `mask_value` 给出（布尔 true/false、数字照实，见上文“回显值文字统一”）。
  - **补齐 50 个键的说明（2026-09-27，my-agent 在 `my-agent/self-dev` 完成，集成者审核后移植到新 main）**：随包 YAML 里 50 个空说明键
    各加一段中文注释，按“做什么 / 什么时候改 / 特殊值含义”写，不写默认数字（参数中心自己显示）；安全边界键统一以“只能由用户在配置文件里
    改，模型不能改”结尾。每条“0 表示……”都追到代码里比较的那一行，途中发现 `cli_audit_cleanup_days` 的 0 会删光审计，已另修为永久保留。
    空说明基线相应删去这 50 个键；`test_parameter_registry` 的过渡名单随之删除，改为“常用参数必须都有说明”。

- **减量第三批（2026-09-27，分支 `claude/38-internal-constants`，A/B/D/E/C 组均已合入 main）**：102 个只对程序内部有意义的字段降级为读取点旁边的
  具名常量（值不变），从 `AgentConfig`、随包 YAML、归一化表、字段规格表、说明基线和测试里删掉字段。规则：每个概念只定义一次
  （多读取点从定义处 import，遵守 `test_constant_names_unique` 与 import 边界），命令行显式 flag 仍优先于常量。
  - 已完成 A 组（24 项，命令与展示默认值）：`cli_*` 12 项、`subagent_cli_default_limit`、`subagent_probe_default_limit`、`subagent_board_limit`、
    `subagent_hierarchy_default_max_depth`、`subagent_hierarchy_recovery_max_nodes`、`subagent_spawn_default_count`、`chat_collapse_preview_lines`、
    `chat_collapse_preview_chars`、`chat_history_assistant_preview_chars`、`chat_transcript_scroll_lines`、`memory_doctor_recent_archive_file_limit`、
    `tool_catalog_show_truncated_notice`。六份重复的 `_subagent_config_int`/`_config_int` 命令默认值 helper 收成 `cli/common.int_arg_or_default`。
  - 已完成 B 组前半（7 项，分支 `claude/38-internal-constants-bd`，gateway/后台节奏）：`gateway_heartbeat_interval`（`gateway_parts/lease_service.GATEWAY_HEARTBEAT_INTERVAL_SECONDS`，心跳文件循环与后台主循环从这里 import）、`gateway_stale_seconds`（`gateway_parts/status_rendering.GATEWAY_STALE_SECONDS`，`cli/local_commands` import）、`gateway_request_poll_interval`、`background_main_error_backoff_seconds`、`background_owner_wake_rescan_seconds`（`cli/gateway_loops` 三个常量；后台主循环 tick 仍按
    轮询与心跳取小再夹到 1～5 秒推导）、`gateway_service_command_timeout_seconds`（`cli/chat_parts/control_runtime`，
    原“最小 10 秒”地板随配置项一起删除）、`conversation_unhandled_observation_limit`（`conversation/runtime`）。场景测试、Live Lab 与
    `tests/run_tests.py` 生成的配置不再写 `gateway_request_poll_interval: 1`（这些测试 Gateway 改按默认 0.2 秒轮询）。
  - 已完成 B 组后半（12 项，同一分支，daemon/dispatch 默认值）：`daemon_planner/interval/max_runners/limit/max_cycles/max_cards/probe/reviewer`
    → `cli/daemon.py` 的 `DAEMON_*` 常量（`"auto"` 映射为 `DAEMON_DEFAULT_MAX_RUNNERS`=1），命令行 flag 仍优先，错误文案只提 flag；
    `daemon_mutate_state` / `daemon_start_runners` / `daemon_runner_instruction` 仍是配置项。`dispatch_max_consecutive_rounds` /
    `dispatch_default_max_runners` / `dispatch_default_limit` / `dispatch_default_watch_interval` → `DispatchRuntimePolicy` 的字段默认值
    （`from_config` 与两个 `_non_negative_*_attr` 删除，loop/watch/CLI 三个调用方直接 `DispatchRuntimePolicy()`；snapshot 仍是 v2，
    `source` 固定 `code-defaults`）。场景测试、Live Lab、`tests/run_tests.py` 模板不再写 daemon 三行（它们不启动 daemon）。
  - 已完成 D 组（10 项，同一分支）：`dynamic_timeout_safety_margin` → `agent_core/dynamic_timeout.DYNAMIC_TIMEOUT_SAFETY_MARGIN`（2.0，
    请求总超时与首包预算共用；原首包预算在缺字段时的 1.5 兜底随之消失）；`estimated_prefill_tokens_per_second`、`probe_min_samples`、
    `probe_window_samples`、`probe_outlier_trim` → `model/call_monitor.FirstTokenTimeoutOptions` 的字段默认值（200/2/5/True），
    `call_runtime.first_token_timeout_options` 不再传入（`bool_config` 随之删除）；`anthropic_version` → `backends/anthropic.AnthropicCompatibleBackend`
    构造参数默认值（2023-06-01），工厂与 OAuth 构造不再传入，`model_scope` 缓存键去掉该字段；`input_media_token_reserve`、
    `compact_vision_digest_max_requests` → `conversation/compact_media_policy.INPUT_MEDIA_TOKEN_RESERVE`（1600）/
    `COMPACT_VISION_DIGEST_MAX_REQUESTS`（4），`media_token_reserve()` / `vision_digest_max_requests()` 不再收 agent；
    `tool_context_microcompact_min_chars` → 已有的 `tool_context/microcompact.DEFAULT_MICROCOMPACT_MIN_CHARS`（1500）；`max_protocol_repairs`
    → `agent_core/_runtime_params.MAX_PROTOCOL_REPAIRS`（2，`ToolLoopExecuteParams` 字段默认值同源）。`/settings` 与 `user_config`
    测试里原来拿 `max_protocol_repairs` 当“非常用参数”样例，改用 `dynamic_timeout_min`。
  - 用户可能真会调、保留为 advanced 不降级（集成者已同意）：`memory_curator_daily_finalize_hour`、`home_lesson_stale_caveat_days`、
    `memory_lesson_min_occurrences`、`memory_hot_min_occurrences`、`chat_history_max_turns`、`estimated_output_tokens_per_second`、
    `dynamic_timeout_min`、`local_store_fts_enabled`。
  - 已完成 E 组（14 项，分支 `claude/9a-batch3-e`，集成者派活）：
    - 后台上下文预算：`background_context_max_string_chars/list_items/dict_items/max_depth` 固定用
      `conversation/context_budget.BackgroundContextBudget` 的默认值（1200/20/80/6），`background_context_max_total_tokens` 仍可配置；
      `conversation_context_recent_limit`、`background_pending_wake_prompt_limit` 成为 `background_context` 的
      `CONVERSATION_CONTEXT_RECENT_LIMIT`、`BACKGROUND_PENDING_WAKE_PROMPT_LIMIT`（都是 20），runtime 的三处唤醒合批选择从这里 import。
    - 终态工具折叠：`conversation_terminal_tool_fold_max_chars` 成为 `tool_context_window._TERMINAL_TOOL_FOLD_MAX_CHARS`（6000），
      开关 `conversation_terminal_tool_fold_enabled` 与热期秒数不变。
    - 工具目录/详情上限：`core.TOOL_CATALOG_LIMIT`（80）、`core.TOOL_DETAIL_MAX_CHARS`（4000）；目录单条截断与分页起点直接用
      `ToolRegistryParams` 的默认值（700、0）。给模型的目录分页提示原来让它“调大 tool_catalog_limit 或 tool_catalog_offset”，
      改为“其余工具可用 list_tools 查看完整清单”；截断标记由“已按 tool_detail_max_chars 截断”改为“已按 工具详情字数上限 截断”，
      不再指向已删的配置键。这是降级带来的唯一模型可见文字变化。
    - 合同状态扫描预算：`contract_status` 的 `CONTRACT_STATUS_RECENT_FINDINGS_LIMIT/MAX_SCAN_FILES/MAX_REPORT_BYTES`
      （20/1000/2000000）；`ContractStatusScanRequest.config` 只为取这三个默认值而存在，一并删除；`contracts status` 的
      `--limit/--max-files` 默认值引用同一常量，显式 flag 仍优先。
    - 加载器元数据 `config_path/config_sources/config_layers/config_warnings/memory_config_warnings` 从 /settings 的列表、计数与
      参数搜索里隐藏（`parameter_registry.LOADER_METADATA_KEYS` + `listed_parameters()`，user_config 的可改数量同口径）；字段与
      登记表项不删，`/settings show` 仍可查看、仍是安全边界。
    - 回归见 `test_param_reduction_e_group.py` 与 `test_settings_chat_control.py`；原来通过配置对象改这些值的测试改为 patch 常量。
  - 已合入 main：C 组（memory 归档预览/语义摘要/恢复/策展批次，第 1–3 批随 `624367a69` 等提交）、决策选模字数（`3bba03e22`，选模型点位两个上限降为常量并同批重生成前端目录）。
  - 前端目录（C 组第 2、3 批与杂项批合入后，main `3d76ac687`，分支 `claude/9b-frontend-catalog-c`）：
    `frontend/config/backend-config-catalog.json` 按当前随包 YAML 重新生成为 264 项（agent_config 222、capability_config 22、
    log_analysis_config 20），`node` 与 `bun` 跑同一脚本输出逐字相同，`--check` 两种都通过。相对上次生成（278 项）只有：
    删掉 C 组的 14 个键、其后字段的全局 `order` 与组内位置顺移。杂项批删掉 `conversation_pending_wake_limit` 后，原来挂在它上面
    的分组标题注释与 `background_context_max_total_tokens` 自己的注释连成一段，前后端解析器都把整段并进说明；随后按集成者决定
    删掉这段已无对象的分组标题，前后端说明都回到该键自己那句（说明基线是空说明名单，不含此键，无需改）。设置页清掉
    `memory_resume_auto_context_limit` 表单项；store 默认值暂留（沿 3af7c94df 的做法）。**（2026-10-01 已由 `worker/ds1-frontend-settings` 清理 store 与 JSON 中包括该键在内的全部死字段。）**
  - `tool_write_inline_max_chars`（写文件指引的软建议，集成者追加，分支 `claude/38-internal-constants-bd`）：常量统一到
    `tooling/content_transport_policy.MAX_INLINE_WRITE_CONTENT_CHARS`（12000），`settings/defaults` 里重复的
    `DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS` 删除；`core.py` 不再传入，`tool_model_generation` 两处直接用常量；`ToolRegistryParams`
    仍保留同名字段（默认即常量，只有测试显式传）。设置页表单项与 DEVELOPMENT_RULES 的说明同步。
  - E 组范围外、分类为 internal 但未归入任何一组的：`conversation_pending_wake_limit`、`memory_artifact_default_read_chars`，
    留给集成者分派（`conversation_unhandled_observation_limit` 已随 B 组前半降级）。
  - **后续合入 main（2026-10-01 核对）**：
    - 杂项批 `35259e86c`：compact 语义摘要与唤醒窗口 6 键降为常量（值不变）。
    - C 组第 2 批 `624367a69`：恢复/归档 6 键降为常量（值不变）；C 组第 1、3 批同批合入。
    - `3bba03e22`：选模型点位两个上限降为常量并同批重生成前端目录。
    - `6dc18342e`：7 个内部实现参数降为读取点旁的具名常量。
    - 09-28 后新增的 5 个参数随各自功能合入 main：`memory_compact_auto_trigger_max_tokens`、`memory_context_calibration_carry_enabled`、
      `decision_observe_sampling_enabled`、`decision_observe_nonblocking_enabled`、`model_reasoning_levels`。

## 7. 验收

- 每个迁入的参数：改用户配置后读取方确实拿到新值（测试证明，不只看配置文件）。
- 扫描测试：`AgentConfig` 字段都有读取方，没有死配置。
- my-agent 能用工具修改 `free` 参数并回滚；`boundary`/`secret` 被结构性拒绝。
- TUI 和飞书都能完成查看、修改、回滚（用户几乎不用命令行）。
