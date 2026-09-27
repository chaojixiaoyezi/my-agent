# 参数中心：一个参数一个权威位置，my-agent 可自助修改非安全参数

状态：阶段 0 已实现（分支 `claude/param-center-phase0`，2026-09-27）；阶段 1—3 规划中，按下文顺序推进。

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
- 每次修改追加一条账本记录（键、原值、新值、操作者、原因、时间）；`/settings history` 查看，`/settings revert <键>` 回滚。
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
  - 守卫：说明为空的字段只允许出现在 `agent_py_agent/tests/fixtures/parameter_description_baseline.json`（按原因分组：5 个加载器元数据；
    172 个等配置减量分类，多数将删除、合并或降级）。名单外新增空说明、或名单里的字段已有说明或已删除，`test_parameter_registry` 都失败，
    名单只会越来越短。这一轮不补写那 172 个说明。
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
    `qq_app_secret`、两个 `*_embedding_api_key` 在 `/settings show` 与 user_config 查看里是明文；`config-get api_key` 也明文打印。
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
  - 仍未覆盖：`--headers 名字 值` 这种名字和值分成两项的写法（如 mcp-proxy），第二项不遮；不紧跟开关、名字又不像凭据的
    单独一项 `名字=值`（如 `DB_HOST=…`）照常显示。
- **阶段 3（迁移）**：按模块分批迁移常数并改名。每批开工前在协作文件贴出文件清单，避开 Codex 正在改的文件。
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
  - **常用层级（2026-09-27，集成者派活，分支 `claude/9a-settings-common-view`）**：用户说“把用户当傻瓜”，99% 的人不会手动调参数，
    但命令要保留。`parameter_registry.COMMON_KEYS` 登记配置分类里标 common 的 21 个键（封闭的产品决策名单，不是开放世界的类型识别），
    登记表对这些键给 `ParameterSpec.common = True`，只用于展示与推荐，不参与放行。`/settings` 不带参数只列常用参数（当前运行值、
    是否改过、说明第一句；改了没重启注明“发 /restart 后生效”），并用一句话说一共多少项、看全部发 `/settings all`；
    `/settings all` 是原总览（总数、可改范围、用户配置位置、改过的个数、最近修改）加按分类的全部参数清单。search、show 不变。
    `user_config` 的 search/view 结果多一个 `common` 字段，工具说明提示模型优先从常用参数里推荐。守卫（`test_parameter_registry`）：
    名单里的键必须存在、不能是安全边界项；与空说明基线的交集只允许过渡项 `memory_compact_auto_trigger_percent`（my-agent 补齐说明后
    收紧为空集）。`/settings` 回执里的布尔与数字不再经 `mask_value`（它把 False、0 当空，原来显示成“（空）”），布尔按 true/false 显示。

## 7. 验收

- 每个迁入的参数：改用户配置后读取方确实拿到新值（测试证明，不只看配置文件）。
- 扫描测试：`AgentConfig` 字段都有读取方，没有死配置。
- my-agent 能用工具修改 `free` 参数并回滚；`boundary`/`secret` 被结构性拒绝。
- TUI 和飞书都能完成查看、修改、回滚（用户几乎不用命令行）。
