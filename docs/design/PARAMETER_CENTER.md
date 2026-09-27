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
  - 登记表 `settings/parameter_registry.py`：每个 `AgentConfig` 字段一条，说明取自随包 YAML 该键正上方的注释，类型取默认值的真实类型；
    安全等级按显式名单与键名记号判定（凭据、权限、身份、路径、外部地址、端口、服务/插件、提示词、审计、锁、执行权威链、内部元数据
    等为边界），`TUNABLE_KEYS` 可显式放行（飞书凭据，回显脱敏）。首轮结果：386 项中 286 项模型可改，其余为边界。
  - 写入口 `settings/parameter_changes.py`：只写当前进程实际加载的用户配置（`user_config_path(config)`；原来只看 Gateway 从不设置的
    `MY_AGENT_CONFIG`，模型因此误报“没有用户级配置”）；布尔、数字不加引号，写后用正式 `load_config` 回读，类型或值不一致就恢复原文件；
    列表/映射、边界项、随包 YAML 拒绝；每次写入记入用户配置旁的 `settings-changes.jsonl`（最近 500 条），可按编号前缀回滚、可连续回滚；
    凭据类只记脱敏值、不可回滚。生效时机如实为“重启 Gateway 后”（压缩百分比与读文件上限原来误报“下一次会话”），管理员可发 `/restart`。
  - my-agent 的 `user_config` 工具新增 search/reset/history/revert，set 走同一写入口；聊天 `/settings`（TUI 与 IM，仅管理员）
    提供总览、搜索、详情、修改、恢复默认、记录与回滚。
- **阶段 2b（2026-09-27 已实现）**：开发工作树约定，见 §5。原计划“先做只给管理员的写入授权”核对后发现多余：F4 Full Access 就是
  按 owner、仅管理员的结构化授权，本机管理员从 9-21 起一直开着。只新增 `self_dev_worktree` 配置与提示词一段；工作树由集成者建在
  `~/my-agent-worktrees/my-agent-self`（分支 `my-agent/self-dev`，基于 main），本机用户配置填上该路径。
- **查看补充（2026-09-27，来自 my-agent 在开发交流板的提问）**：
  - 配置值与实际使用值不同的参数，在登记表 `_APPLIED_RULES` 登记派生函数（目前只有 max_tokens → `effective_max_output_tokens`，
    公式仍只在原处）。`applied_value(key, config)` 给查看入口用：`user_config` 的 view/search 按本片会话模型给
    `applied_value`/`applied_rule`，`/settings show` 按默认模型多一行“实际使用值”，并注明 /model 切换过的会话可能不同。
  - 删除 `read_config_fact` 里旧白名单遗留的 `tunable` 字段：模型同时看到 writable=true 与 tunable=false，误以为 max_tokens 改不了；
    能否修改只看登记表的 `writable`。
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
  - **方向调整（用户 2026-09-27）**：“几百个参数是不是太多了，有些可以合并，有些可能没用了”。本机用户配置只改过 11 项
    （测试机 298 项是当年整份复制随包 YAML）。下一步先把测试机配置收成“只写与默认不同的项”，再对 386 个配置项逐项分类：
    删除（无用）、合并（总一起调）、降级（只对程序内部有意义，改回代码常数并移出配置）、常用（`/settings` 默认只列二三十个）、
    高级（可搜索但不刷屏）。目标约 100 项；每批不改任何生效值。

## 7. 验收

- 每个迁入的参数：改用户配置后读取方确实拿到新值（测试证明，不只看配置文件）。
- 扫描测试：`AgentConfig` 字段都有读取方，没有死配置。
- my-agent 能用工具修改 `free` 参数并回滚；`boundary`/`secret` 被结构性拒绝。
- TUI 和飞书都能完成查看、修改、回滚（用户几乎不用命令行）。
