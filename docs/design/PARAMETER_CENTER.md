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

给 my-agent 一个专用开发工作树（独立分支），经 `additional_write_roots` 授权写入：它可以改代码、跑测试、提交到自己的分支；由集成者审核、合并、部署。推送主线与双机部署仍只由集成者负责，避免三方同时写主线。

## 6. 阶段

- **阶段 0（已实现）**：
  - 输出上限统一 65536，常量、随包 YAML、`AgentConfig` 同值，由 `test_model_output_cap.py` 核对。
  - 夹取规则只在 `settings.defaults.effective_max_output_tokens` 一处，后端工厂对默认模型与模型档案统一使用；窗口已明确时取 min(配置, 窗口 ÷ 4)，未明确时按配置原值。
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
- **阶段 2（基础设施）**：
  - 权威登记表 `settings/parameters.py`，以及 YAML/dataclass 一致性测试。
  - `user_config` 按安全等级开放，新增 `/settings` 的 TUI/IM 入口、修改账本与回滚。
  - my-agent 开发工作树授权。
- **阶段 3（迁移）**：按模块分批迁移常数并改名。每批开工前在协作文件贴出文件清单，避开 Codex 正在改的文件。

## 7. 验收

- 每个迁入的参数：改用户配置后读取方确实拿到新值（测试证明，不只看配置文件）。
- 扫描测试：`AgentConfig` 字段都有读取方，没有死配置。
- my-agent 能用工具修改 `free` 参数并回滚；`boundary`/`secret` 被结构性拒绝。
- TUI 和飞书都能完成查看、修改、回滚（用户几乎不用命令行）。
