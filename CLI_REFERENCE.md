# CLI Reference

当前说明对应 e905 的**开发分支**，不代表本机运行包已升级。默认内置解析树为 24 个根；第三方扩展仍可登记不冲突的独立根，但不能通过别名占用内置根或历史路由。完整 v1 手册保存在 [历史手册](docs/history/CLI_REFERENCE_V1.md)，不是新调用的依据。

```sh
my-agent --help
my-agent <规范根> --help
python -m agent_py_agent --help
```

## 人工规则与记忆候选控制（组合候选，未发布）

以下为 F4/F5 组合候选的显式控制语法。终端 direct、Gateway 和 IM 共用原服务，控制本身不调用模型；部署版本须另核，不代表真实飞书租户已验收。普通用户操作本人 owner，群用户保留当前 HostActor/会话边界，不新增全机管理员门或监控 grant。

| 命令 | 行为与边界 |
|---|---|
| `/agents show`、`/agents refresh` | 查看规则来源/状态；沿原刷新点处理，不凭配置宣称正文实时新鲜 |
| `/agents source local <绝对路径>` | 预览切换本人有权读取的本地来源，配置不增加路径权限 |
| `/agents source url <https地址>` | 预览只读远端来源；网络来源默认关闭，开启仍受原网络权限，真实网络未验证 |
| `/agents reset` | 预览恢复本人默认来源 |
| `/agents write "完整规则正文\n下一行"` | JSON 字符串完整预览拟写内容，保留换行及尾空白；模型不能代填确认权 |
| `/agents confirm <预览编号>`、`/agents receipt <预览编号>` | 原预览本人在当前会话确认一次或只读查回执；来源、正文、权限变化需重核，unknown 不重放 |
| `/agents owner <规范owner> <上述操作>` | 显式跨 owner，仅原完整本机管理员且 Full Access 可用；目标不改变 actor |
| `/memory candidates`、`/memory candidates list [起始序号]` | 本人唯一候选库元信息，每页 20 条；不等于正式保存 |
| `/memory candidates show <候选ID>` | 查看可安全完整显示的正文、范围、动作和目标版本 |
| `/memory candidates preview <候选ID>` | 创建 600 秒精确预览，返回可复制的 confirm/reject/cancel/receipt 命令 |
| `/memory candidates confirm <预览编号>` | 确认完整版本，沿原 review/promote(false)、目标 CAS 与证据门提交 |
| `/memory candidates reject <预览编号>` | 原候选状态机拒绝，不提交正式内容 |
| `/memory candidates cancel <预览编号>` | 取消预览，候选状态不变 |
| `/memory candidates receipt <预览编号>` | 只读原结果；未知不能借新消息重复执行 |

先查看候选，复制候选 ID 做 preview，核对完整正文、范围和目标，再复制回执的完整确认命令，不编造编号。确认绑定 owner、actor、channel、conversation/thread、完整候选 SHA 与目标版本；换人、换话题、过期或候选变化须重核。超过 12 KiB 或会被脱敏/用户投影改变的候选不提供 IM 确认，须受控本机完整核验，不能用摘要批准另一正文。

**IM 只开放 `/memory candidates` 子树。** 裸 `/memory [查询内容]` 仅终端搜索正式记忆；本机 `my-agent memory candidates review/promote` 不是普通 IM 执行入口。AGENTS 始终走 `/agents`，Skill 沿原 `/skills` 用户确认与 CAS。`consumed_unknown` 只表示运输已受理、业务结果未确认，应查原回执及正式内容而非重新提交。普通 Memory 的宿主自动审核与 AGENTS/SOUL 的用户确认边界以 [开发约定](AGENTS.md#自学习功能约束) 为准。

`self_map` 是按权限发现的只读工具，不是 `/self_map` 斜杠命令。模型先读无参总览，再沿领域、准确条目和 cursor 读取原公开读口；unknown/partial 不能当空，不新增权限、执行或记忆副本。详见[自我目录](docs/design/SELF_MAP.md)及[规则归属](docs/design/PERSONA_AGENTS_OWNERSHIP.md)。`/help memory`、`/help agents` 从同一 command_catalog 展示语法，文档不签发权限。

## 全局参数与输入边界

- `--config <path>`：显式配置路径；默认沿原环境/随包配置选择，不改变部署配置。
- `--plain`：普通终端聊天模式。
- `--dir <path>`、`--workspace <path>`、`--workspace-root <path>`：原工作目录选择参数。
- `-h`、`--help`：查看当前节点帮助。

全局参数放在根命令前。兼容器只定位真正的根命令，不扫描配置路径、目录、业务正文或 `--` 后的文本寻找旧名字；未知参数交给同一解析树拒绝。根选项当前只有零值或单值形式，新增多值形式必须先扩展明确的解码合同，不能猜值的边界。未带命令时保持原交互入口，不把合成分组误当成启动聊天。

## 24 个规范根

| 根 | 当前入口 | 用途 |
|---|---|---|
| `adapter` | `my-agent adapter` | 通道适配器运行与状态 |
| `admin` | `my-agent admin` | 本机管理员密码和绑定身份管理 |
| `audit` | `my-agent audit` | 审计日志 |
| `background` | `my-agent background` | 原后台主代理管理 |
| `chat` | `my-agent chat` | 交互会话 |
| `collaboration` | `my-agent collaboration` | 原协作控制 |
| `config` | `my-agent config` | 受限配置读取与设置 |
| `context` | `my-agent context` | 上下文包 |
| `dev` | `my-agent dev` | 开发探针、合同与隔离场景 |
| `feishu` | `my-agent feishu` | 飞书接入 |
| `gateway` | `my-agent gateway` | Gateway 与监督进程控制 |
| `guidance` | `my-agent guidance` | 给原子代理补充要求 |
| `home` | `my-agent home` | 家目录状态、保留计划和索引 |
| `local-store` | `my-agent local-store` | 本地检索存储管理 |
| `memory` | `my-agent memory` | 正式记忆、候选、路由、归档和诊断 |
| `resume` | `my-agent resume` | 显式恢复已有会话 |
| `run` | `my-agent run` | 单轮任务 |
| `runtime` | `my-agent runtime` | 原运行欠账查看与显式结清 |
| `skills` | `my-agent skills` | 技能与自学提议 |
| `status` | `my-agent status` | 当前状态 |
| `subagents` | `my-agent subagents` | 子代理列表、调度、恢复和执行 |
| `task` | `my-agent task` | 任务查询与原状态控制 |
| `timeline` | `my-agent timeline` | 最近事件 |
| `update` | `my-agent update` | 原升级入口 |

## 常用规范调用

```sh
my-agent home status --json
my-agent home index rebuild --help
my-agent memory route --help
my-agent memory archive resume --help
my-agent memory compact --help
my-agent task list --help
my-agent subagents list --help
my-agent subagents run --help
my-agent skills learned proposals --help
my-agent config get feishu_app_id
```

`subagents --help` 和 `skills learned proposals --help` 展示分组，不被旧默认列表兼容抢走。新生产代码、恢复建议、脚本和帮助都应生成规范路径，不再生成 v1 输入。

## 统一只读 Doctor

```sh
my-agent memory doctor --json
my-agent memory doctor --scope runtime --json
my-agent memory doctor --scope memory --json
my-agent memory doctor --scope all --json
```

- 默认 `memory`：保留原 `memory doctor` 的机器字段、候选 stats、curator status、migration/retention plan，健康不成立退出 1，成立退出 0。不运行 apply、正式晋升、curator run 或模型。
- `runtime`：只取原运行/Home 诊断，不初始化 memory 维护组件；规范输出用 `runtime` facet，健康仍按原路由加载条件。
- `all`：输出 `runtime`、`memory` 两个独立 facet。`common` 中各公共字段始终保留 `runtime` / `memory` 子键，不能因两侧内容相同就折叠为单对象；验证根或读取范围不同仍分别表达，不伪装成同一事实。整体健康为两个原条件的合取。这里约定稳定结构，不预设字段值；修复代码与回归由集成者核验。
- v1 平铺输入仅在根边界转成 runtime 兼容投影，保留原字段及**固定退出 0**，即使健康字段为 false 也不改旧外部合同。它不是第二份诊断实现，也不是规范诊断的退出语义。

唯一服务为 `agent_py_agent/agent/user_space/home_doctor.py::DoctorService`。CLI 只注入现有只读 planner 工厂并格式化结果；两份旧 CLI Doctor 模块及旧 reexport 已移除。请求缓存区分 owner、索引、验证根和范围，不跨 owner 或范围复用健康结果。

## Slash 退出与管理员撤销

- 规范退出为 `/exit`；原明确退出词仍保留。
- `logout`、`/logout` 被拒绝并提示选择明确语义：不能退出界面，也不能撤销管理员认证。
- `/admin logout` 沿原身份校验撤销本人绑定，不等同退出；本机/群与 IM 私聊的既有边界不变。
- 发布 N 的 `/user`、`/v`、`/quit` 只在输入边界转换为 `/users`、`/verbose`、`/exit`，规范目录没有别名成员。
- MCP 标准协议的 `list_tools` 保留，不能和产品工具入口优化混淆。

## 会话控制：`/goal` 与 `/btw`

- `/goal [目标]` 查看或开始当前会话持续目标；`/goal pause|resume` 暂停/恢复，`/goal edit <目标>` 修改目标但保留原任务和工作区，`/goal clear` 清除目标而不删除聊天及成果。
- `/goal <时长> <名称> <任务>` 创建可并存的命名 Goal；`/goal <名称> clear` 只停止准确命名项。Goal 是当前会话的持续目标层，不是新 Agent；多个 Goal 不让普通聊天自动猜一个“当前目标”。
- `/btw <内容>` 只纠偏当前运行任务一次，不永久追加 prompt、不传入下一任务；无列表模式，`/btw-clear` 已移除。`/status` 查看当前状态；普通 `/stop` 与 Gateway 服务停止不同，命名 Audit/Goal 须按准确范围停止。具体控制语法以当前 `/help` 为准。

## 插件命令入口

公共入口为 `/plugins [管理动作]`、`/plugins@<插件ID> [动作] [参数]`；能力包入口为 `/plugins#[包名] [查看|启用|停用|删除|退回 [版本摘要]|安装 <单号>]`，与 `/plugins@` 不混用。管理 install/configure/enable/disable/remove/update/status 只对管理员开放，原身份和审批门不因目录可见而放宽。

`/plugins configure <插件ID> --file ./settings.json` 读取授权路径的完整 JSON，按声明验证、私有保存并保持停用，不是增量补丁。`/plugins update <插件ID> <新包路径>` 要求先停用且同 ID；配置结构不变保留配置，否则须重新 configure/enable。安装/更新相对包路径沿当前会话工作区根解析，不扩大路径权限。`/plugins status <原请求编号>` 查原结果，超时不自动重送；自制包 `/plugins confirm <单号>` 单次确认安装，`/plugins revert <包名>` 先预览再按准确摘要确认。

`/help`、补全和执行共用声明；未知后缀不转普通聊天、Shell 或停止操作。源码入口不代表已部署，真实 TUI 装卸未验证。完整说明见[插件与安全开关](docs/guides/PLUGIN_GUIDE.md)及[插件设计](docs/design/PLUGIN_LIFECYCLE.md)。

## 模型选择、账号与 OAuth

TUI `/model` 打开新增模型、选择模型（对话/决策/向量）、管理已有模型、连接测试、默认模型与共享菜单。新增模型支持 OpenAI Chat、Anthropic、OpenAI Responses、登录账号和 Jev 决策；接口、密钥与高级请求头沿安全界面配置，不在普通聊天填写秘密。飞书无参 `/model` 提供选择卡，可向原操作者私发新增/编辑表单；私人表单仍配置原群的模型，秘密不进入聊天或模型，旧卡及异值重放拒绝。

`/model <编号>` 为当前会话选择，`/model default <编号>` 设置新会话默认，不倒改已打开会话或已有 child。可选项只来自本人保存、显式部署配置及管理员开放的共享模型；TUI 无参仍打开菜单。主会话也可经 `manage_models` 使用同一服务，受 `enable_model_profile_tool` 控制，删除服务商沿原审批。详见[模型菜单设计](docs/design/TUI_MODEL_PROFILES.md)。

「登录账号」中的 ChatGPT 订阅打开浏览器登录，SSH 等环境退回设备码；通用登录要求服务商支持设备码流程，并配置 Client ID、端点、Scope、Audience 及可选 Client Secret。不自动选型号，不借其他应用登录；取消登录不等于退出账号，OAuth 账号不跨用户共享。OAuth 真实服务商与浏览器登录本轮未验证，详见[模型账号登录](docs/design/MODEL_OAUTH.md)。

## IM 管理员身份

本机先用 `my-agent admin password set` 设置管理员密码。启用 `admin_channel_identity_enabled` 后，原 IM 一对一私聊 `/admin <密码>` 按渠道与用户 ID 绑定管理员；群聊不匹配。10 分钟内错 5 次锁定该身份 10 分钟。`/approve <管理员密码>` 只批准本会话唯一待决操作一次，`/deny` 拒绝；没有或多个待决则拒绝，不扩大为长期授权。请勿把真实密码写进普通模型对话。

适配器不将这些敏感控制写进持久入站队列，Gateway 仅存脱敏回执；渠道自身可能保留原消息，应撤回。终端 TUI/plain 已是本机管理员，会本地拒绝这类 IM 认证命令。`/admin logout` 撤销本人绑定，不退出界面；本机管理用 `my-agent admin identities list` / `my-agent admin identities remove`。边界与开关见[IM 管理员身份](docs/design/ADMIN_CHANNEL_IDENTITY.md)，实际部署开关值另核。

## 配置 CLI 与错误口径

`config set` 保留原五键白名单：`feishu_app_id`、`feishu_app_secret`、`feishu_verification_token`、`feishu_encrypt_key`、`feishu_callback_port`。CLI 委托现有 typed 参数写服务与全局写门，原验证、正式回读和脱敏账本生效；没有新增直接 YAML 写入或 Slash 身份旁路。读取使用统一配置事实并脱敏。实际账号秘密优先使用宿主安全卡入口，不在普通聊天或示例命令中填写。

`STORE_SCHEMA_NEWER`、`TOOL_SCHEMA_INVALID_MODE_COMBINATION`、`MEMORY_COMMIT_OBLIGATION_QUARANTINED` 在真实 `agent/contracts/error_taxonomy.py` 统一分类；本分支只追加分类，不声称其他席位的触发点已接线，也不因此自动重试、迁移或解除隔离。

## 永久冻结的外部守护合同

按真实基线 `4bea324e3209ef9b9ecc39b6c2378bacc078b374` 扫描并冻结：`gateway supervisor`、`gateway run`、`adapter start`、`gateway maintenance-lock`、`gateway maintenance-unlock`，以及实际持久化的 `python -m agent_py_agent` 模块入口。参数、默认值与原 handler 不随人工命令整理而改变。R1 明确修复 supervisor 未给目录时用 None 覆盖顶层 `--dir` 的缺陷：该参数默认改为 SUPPRESS，显式目录在规范/旧输入均保留；快照同步记录这一项修正，其余合同不变。没有新增不存在的 `agent_py_agent.runtime.*` 入口。

另须永久守护安装器跨版本升级/回滚实际使用的 `gateway supervisor-start`、`gateway supervisor-stop`、`gateway stop --drain --stop-background --background-timeout 30`、`gateway start --ready-timeout 60` 与 `adapter stop`。后台派工 `background/dispatch.py` 和孤儿识别 `startup_recovery.py` 仍消费 `subagents-dispatch`；在这些消费者完成迁移并验证前，不得随 v1 转换器移除。冻结的是持久 argv、参数和 handler 合同，不是把旧平铺名重新推荐给用户；新调用使用 `gateway supervisor start/stop`、`subagents dispatch`。冻结清单及代码测试由集成者同步核验，不能只按 N→N+1 注释退役。

帮助和解析测试不证明 launchd/systemd 安装、真实服务启停或另一台机器可达；这些目标观察点未验证。

## 删除门与性能 Probe

```sh
python3 scripts/verify_optimization_removal.py --worker sol-9 --json
python3 scripts/verify_optimization_removal.py --worker all --json
python3 scripts/audit_code_removal.py --worker sol-9 --json
python3 scripts/performance_review_probe.py --iterations 3 --warmup 1 --json
python3 scripts/benchmark_config_policy.py --config <隔离配置路径> --iterations 3 --warmup 1 --json
```

删除门读取随仓 manifest，保留全部 383 项与本任务 185 项映射；跨线项只标实现责任，不擅自修改其他席位业务。`retained_contract_correction` 的两项测试保留；60 个额外候选未取得独占写集内的 AST、字面和动态零消费者证据，均为 `deferred_dynamic_check`，没有盲删。持久化 `request_id` 的 `subagent-run:` 前缀不是 CLI 输入，只在准确文件和字段的 AST 上下文保留。

结果区分 `pass`、`fail`、`pending_semantic_check`、`requires_behavioral_test`、`cross_worker_dependency`。单席结构通过不代表全量删除或行为通过；全量门在其他领域补齐精确合同与集成前可以返回非零，不能删除欠账来凑绿。显式 `--output` 的仓内报告只允许写入 `.pytest_cache/`，不覆盖产品 JSON 或清单。旧 audit 文件名只转交同一个实现。

Probe 输出每次样本、时间、Python 峰值内存与列明文件的源码摘要；配置 Probe 另标输入摘要且不回显值。它只观察当前进程内的真实 parser 构建或正式配置加载，不是完整依赖快照、真实用户链路、服务启动或跨机性能结论。

## 当前注册路径与参数索引

以下索引由本分支真实 `build_parser()` 生成，只列参数名，不读取配置值、不执行 handler。具体位置参数、默认值和权限以该节点 `--help` 与原业务门为准。

<!-- e905-canonical-index -->

| 当前路径 | 本节点参数 |
|---|---|
| `my-agent` | `--config`, `--dir`, `--help`, `--plain`, `--workspace`, `--workspace-root`, `-h` |
| `my-agent status` | `--help`, `--json`, `--limit`, `--recent`, `-h` |
| `my-agent timeline` | `--details`, `--event-type`, `--help`, `--json`, `--limit`, `--source-type`, `-h` |
| `my-agent run` | `--delivery-contract-file`, `--dir`, `--help`, `--inject`, `--no-resume-context`, `--no-save`, `--prompt-file`, `--resume`, `--resume-context`, `--save`, `--show-prompt`, `--workspace`, `--workspace-root`, `-h` |
| `my-agent memory` | `--help`, `-h` |
| `my-agent memory remember` | `--help`, `--kind`, `-h` |
| `my-agent memory list` | `--help`, `--limit`, `-h` |
| `my-agent memory search` | `--help`, `--limit`, `-h` |
| `my-agent memory candidates` | `--help`, `-h` |
| `my-agent memory candidates list` | `--help`, `--json`, `--limit`, `--status`, `-h` |
| `my-agent memory candidates review` | `--decision`, `--help`, `--json`, `--note`, `--promotion-target`, `--proposed-action`, `--reviewer`, `--target-entry-id`, `-h` |
| `my-agent memory candidates promote` | `--automatic`, `--confirmed`, `--help`, `--json`, `--reviewer`, `-h` |
| `my-agent memory agents` | `--help`, `-h` |
| `my-agent memory agents cleanup` | `--apply`, `--conversation`, `--help`, `--json`, `--migration-id`, `--plan-digest`, `-h` |
| `my-agent memory curator` | `--help`, `-h` |
| `my-agent memory curator status` | `--help`, `--json`, `-h` |
| `my-agent memory curator run` | `--force`, `--help`, `--json`, `-h` |
| `my-agent memory curator recover` | `--help`, `--json`, `-h` |
| `my-agent memory retention` | `--help`, `-h` |
| `my-agent memory retention plan` | `--help`, `--json`, `-h` |
| `my-agent memory retention apply` | `--help`, `--json`, `-h` |
| `my-agent memory doctor` | `--help`, `--index`, `--json`, `--scope`, `-h` |
| `my-agent memory migrate` | `--apply`, `--help`, `--json`, `-h` |
| `my-agent memory vectors` | `--help`, `-h` |
| `my-agent memory vectors status` | `--help`, `--json`, `-h` |
| `my-agent memory vectors rebuild` | `--confirmed`, `--help`, `--json`, `-h` |
| `my-agent memory daily` | `--help`, `-h` |
| `my-agent memory daily list` | `--actor`, `--date`, `--event-type`, `--help`, `--json`, `--limit`, `-h` |
| `my-agent memory route` | `--auto-read-limit`, `--help`, `--index`, `--json`, `--limit`, `--mode`, `--validate`, `-h` |
| `my-agent memory archive` | `--help`, `-h` |
| `my-agent memory archive list` | `--date`, `--help`, `--json`, `--layer`, `--level`, `--limit`, `-h` |
| `my-agent memory archive search` | `--action`, `--date`, `--help`, `--json`, `--layer`, `--limit`, `--request-id`, `--run-id`, `--session-id`, `--since`, `--source`, `--speaker`, `--status`, `--target`, `--task-id`, `--tool-name`, `--until`, `-h` |
| `my-agent memory archive resume` | `--action`, `--compact-owner-id`, `--compact-owner-type`, `--compact-resume-mode`, `--context-only`, `--date`, `--from-compact`, `--help`, `--json`, `--layer`, `--limit`, `--request-id`, `--run-id`, `--session-id`, `--since`, `--source`, `--speaker`, `--status`, `--target`, `--task-id`, `--tool-name`, `--until`, `-h` |
| `my-agent memory artifact` | `--help`, `-h` |
| `my-agent memory artifact read` | `--help`, `--json`, `--max-chars`, `--mode`, `--offset`, `--query`, `-h` |
| `my-agent memory fact` | `--help`, `-h` |
| `my-agent memory fact write` | `--acceptance`, `--compact-owner-id`, `--compact-owner-type`, `--constraint`, `--fact-id`, `--from-compact`, `--goal`, `--help`, `--json`, `--latest-test`, `--next-action`, `-h` |
| `my-agent memory compact` | `--apply`, `--date`, `--dry-run`, `--help`, `--json`, `--layer`, `--level`, `--limit`, `--main-context-bundle-ref`, `--request-id`, `--run-id`, `--session-id`, `--since`, `--task-id`, `--until`, `-h` |
| `my-agent chat` | `--dir`, `--direct`, `--gateway`, `--gateway-timeout`, `--help`, `--inject`, `--memory-limit`, `--no-resume-context`, `--no-save`, `--prompt-file`, `--resume-context`, `--session-id`, `--workspace`, `--workspace-root`, `-h` |
| `my-agent resume` | `--dir`, `--direct`, `--gateway`, `--gateway-timeout`, `--help`, `--inject`, `--memory-limit`, `--no-resume-context`, `--no-save`, `--prompt-file`, `--resume-context`, `--workspace`, `--workspace-root`, `-h` |
| `my-agent context` | `--help`, `-h` |
| `my-agent context bundle` | `--help`, `-h` |
| `my-agent context bundle latest` | `--help`, `--json`, `-h` |
| `my-agent dev` | `--help`, `-h` |
| `my-agent dev bench-model` | `--help`, `--profile`, `--show`, `-h` |
| `my-agent dev contracts` | `--help`, `-h` |
| `my-agent dev contracts status` | `--help`, `--json`, `--limit`, `--max-files`, `--root`, `-h` |
| `my-agent dev scenario-test` | `--capability-config`, `--case`, `--count`, `--direct`, `--dry-run`, `--help`, `--max-cycles`, `--max-runners`, `--model-request-timeout`, `--planner`, `--runner-concurrency`, `--runner-start-rate`, `--skill-dir`, `--timeout`, `--workspace`, `-h` |
| `my-agent home` | `--help`, `-h` |
| `my-agent home status` | `--help`, `--json`, `-h` |
| `my-agent home retention` | `--apply`, `--help`, `--json`, `-h` |
| `my-agent home index` | `--help`, `-h` |
| `my-agent home index rebuild` | `--apply`, `--help`, `--json`, `-h` |
| `my-agent task` | `--help`, `-h` |
| `my-agent task workspace` | `--help`, `-h` |
| `my-agent task workspace list` | `--date`, `--help`, `--json`, `--limit`, `-h` |
| `my-agent task show` | `--help`, `-h` |
| `my-agent task list` | `--help`, `--limit`, `--status`, `--user-id`, `-h` |
| `my-agent task abandon` | `--help`, `-h` |
| `my-agent task pause` | `--help`, `-h` |
| `my-agent task resume` | `--help`, `-h` |
| `my-agent task search` | `--help`, `-h` |
| `my-agent runtime` | `--help`, `-h` |
| `my-agent runtime stale-attempts` | `--help`, `--json`, `--older-than-days`, `--settle`, `-h` |
| `my-agent skills` | `--help`, `-h` |
| `my-agent skills learned` | `--help`, `-h` |
| `my-agent skills learned list` | `--help`, `--json`, `-h` |
| `my-agent skills learned show` | `--help`, `--json`, `-h` |
| `my-agent skills learned revert` | `--help`, `--json`, `-h` |
| `my-agent skills learned remove` | `--help`, `--json`, `-h` |
| `my-agent skills learned proposals` | `--help`, `-h` |
| `my-agent skills learned proposals list` | `--help`, `--json`, `-h` |
| `my-agent skills learned proposals show` | `--help`, `--json`, `--revision`, `-h` |
| `my-agent skills learned proposals confirm` | `--help`, `--json`, `--revision`, `-h` |
| `my-agent skills learned proposals reject` | `--help`, `--json`, `--revision`, `-h` |
| `my-agent skills proposals` | `--help`, `-h` |
| `my-agent skills proposals list` | `--help`, `--json`, `--status`, `-h` |
| `my-agent skills proposals show` | `--help`, `--json`, `-h` |
| `my-agent skills proposals confirm` | `--expected-revision`, `--help`, `--json`, `-h` |
| `my-agent skills proposals reject` | `--expected-revision`, `--help`, `--json`, `-h` |
| `my-agent local-store` | `--help`, `-h` |
| `my-agent local-store status` | `--help`, `-h` |
| `my-agent local-store search` | `--help`, `--limit`, `--preview-chars`, `--source-type`, `--visibility`, `-h` |
| `my-agent local-store index-memory` | `--help`, `-h` |
| `my-agent local-store doctor` | `--help`, `--json`, `--limit`, `--repair`, `--workspace-root`, `-h` |
| `my-agent local-store rebuild` | `--help`, `--reset`, `--source`, `--workspace-root`, `-h` |
| `my-agent guidance` | `--help`, `-h` |
| `my-agent guidance send` | `--case-id`, `--delivery`, `--help`, `--json`, `--priority`, `--run-id`, `--sender`, `--target-id`, `--target-type`, `--task-id`, `--thread-id`, `-h` |
| `my-agent subagents` | `--help`, `-h` |
| `my-agent subagents create` | `--agent-name`, `--count`, `--help`, `--role`, `-h` |
| `my-agent subagents list` | `--all`, `--help`, `--limit`, `--owner`, `--root-id`, `--status`, `-h` |
| `my-agent subagents leadership` | `--help`, `-h` |
| `my-agent subagents leadership plan` | `--capability-config`, `--help`, `--json`, `--leader`, `--max-children-per-leader`, `--root-id`, `-h` |
| `my-agent subagents leadership apply` | `--apply`, `--child-run-id`, `--coordinator`, `--dry-run`, `--help`, `--json`, `--leader`, `--max-children-per-leader`, `--root-id`, `-h` |
| `my-agent subagents hierarchy` | `--apply`, `--child`, `--help`, `--json`, `--max-children`, `--max-depth`, `--requested-by`, `-h` |
| `my-agent subagents recovery-tree` | `--capability-config`, `--help`, `--hide-healthy`, `--json`, `--max-nodes`, `--requested-by`, `-h` |
| `my-agent subagents due-check` | `--all`, `--capability-config`, `--help`, `--limit`, `--root-id`, `-h` |
| `my-agent subagents budget` | `--help`, `--include-dry-runs`, `--json`, `--max-model-calls`, `--max-prompt-response-tokens`, `--max-tool-rounds`, `--root-id`, `-h` |
| `my-agent subagents probe` | `--help`, `--limit`, `-h` |
| `my-agent subagents actions` | `--help`, `-h` |
| `my-agent subagents actions plan` | `--all`, `--capability-config`, `--help`, `--limit`, `--root-id`, `-h` |
| `my-agent subagents actions apply` | `--action`, `--apply`, `--capability-config`, `--dry-run`, `--help`, `--limit`, `--locked-file`, `--run-id`, `--take-over-by`, `-h` |
| `my-agent subagents route-capabilities` | `--apply`, `--capability-config`, `--dry-run`, `--help`, `--limit`, `--run-id`, `--skill-dir`, `-h` |
| `my-agent subagents tests` | `--help`, `--re-run`, `--timeout`, `-h` |
| `my-agent subagents patches` | `--apply`, `--apply-dry-run`, `--dry-run`, `--help`, `--limit`, `--note`, `--review-apply`, `--reviewer`, `--run-id`, `-h` |
| `my-agent subagents dispatch` | `--advance`, `--apply`, `--background-launch-id`, `--capability-config`, `--dry-run`, `--expected-attempt`, `--force-lock`, `--help`, `--instruction`, `--interval`, `--limit`, `--locked-file`, `--max-cards`, `--max-cycles`, `--max-runners`, `--no-probe`, `--note`, `--planner`, `--reviewer`, `--run-id`, `--skill-dir`, `--start-runners`, `--take-over-by`, `--watch`, `--workspace-root`, `-h` |
| `my-agent subagents context` | `--help`, `--max-cards`, `-h` |
| `my-agent subagents run` | `--dry-run`, `--execute`, `--help`, `--instruction`, `--max-cards`, `--no-probe`, `-h` |
| `my-agent subagents show` | `--help`, `-h` |
| `my-agent subagents daemon` | `--apply`, `--capability-config`, `--dry-run`, `--force-lock`, `--help`, `--instruction`, `--interval`, `--limit`, `--locked-file`, `--max-cards`, `--max-cycles`, `--max-runners`, `--no-planner`, `--no-probe`, `--no-start-runners`, `--note`, `--planner`, `--reviewer`, `--skill-dir`, `--start-runners`, `--take-over-by`, `-h` |
| `my-agent gateway` | `--help`, `-h` |
| `my-agent gateway start` | `--force`, `--help`, `--ready-timeout`, `-h` |
| `my-agent gateway stop` | `--background-timeout`, `--drain`, `--help`, `--kill`, `--reason`, `--stop-background`, `--timeout`, `-h` |
| `my-agent gateway restart` | `--force`, `--help`, `--ready-timeout`, `--timeout`, `-h` |
| `my-agent gateway logs` | `--help`, `--lines`, `-h` |
| `my-agent gateway run` | `--after-pid`, `--help`, `--workspace-root`, `-h` |
| `my-agent gateway status` | `--help`, `-h` |
| `my-agent gateway ask` | `--help`, `--inject`, `--json`, `--no-resume-context`, `--no-save`, `--no-wait`, `--prompt-file`, `--resume-context`, `--show-prompt`, `--timeout`, `-h` |
| `my-agent gateway result` | `--help`, `--json`, `--show-prompt`, `-h` |
| `my-agent gateway supervisor` | `--check-interval`, `--heartbeat-timeout`, `--help`, `--max-restart-attempts`, `--restart-cooldown`, `--workspace-root`, `-h` |
| `my-agent gateway supervisor install-service` | `--help`, `--no-start`, `--unit`, `-h` |
| `my-agent gateway supervisor uninstall-service` | `--help`, `--unit`, `-h` |
| `my-agent gateway supervisor start` | `--help`, `-h` |
| `my-agent gateway supervisor stop` | `--help`, `--timeout`, `-h` |
| `my-agent gateway supervisor status` | `--help`, `-h` |
| `my-agent gateway maintenance-lock` | `--help`, `--reason`, `--ttl`, `-h` |
| `my-agent gateway maintenance-unlock` | `--help`, `-h` |
| `my-agent gateway start-all` | `--adapter`, `--help`, `-h` |
| `my-agent gateway install` | `--force`, `--help`, `-h` |
| `my-agent gateway uninstall` | `--help`, `-h` |
| `my-agent adapter` | `--help`, `-h` |
| `my-agent adapter file` | `--help`, `--inbox`, `--limit`, `--no-start-gateway`, `--once`, `--outbox`, `--poll-interval`, `--root`, `--timeout`, `--watch`, `-h` |
| `my-agent adapter start` | `--channel`, `--daemon`, `--help`, `--pid-file`, `-h` |
| `my-agent adapter status` | `--help`, `--pid-file`, `-h` |
| `my-agent adapter stop` | `--help`, `--pid-file`, `--timeout`, `-h` |
| `my-agent audit` | `--help`, `-h` |
| `my-agent audit log` | `--action`, `--cleanup`, `--days`, `--help`, `--limit`, `--offset`, `--recent-users`, `--status`, `--summary`, `--target`, `--target-type`, `--user`, `-h` |
| `my-agent background` | `--help`, `-h` |
| `my-agent background message` | `--canonical-user-id`, `--channel`, `--content`, `--conversation-id`, `--help`, `--json`, `--no-run-background`, `--now`, `--run-background`, `--user-id`, `-h` |
| `my-agent background bind-task` | `--goal`, `--help`, `--json`, `--now`, `--progress-interval-seconds`, `--route-channel`, `--route-target`, `--task-id`, `--thread-id`, `-h` |
| `my-agent background observe` | `--dedupe-key`, `--event-type`, `--evidence-ref`, `--help`, `--json`, `--now`, `--parent-agent-id`, `--requires-llm-report`, `--requires-main-agent`, `--root-task-id`, `--severity`, `--source-agent-id`, `--summary`, `--task-id`, `--thread-id`, `--urgency`, `--wake`, `-h` |
| `my-agent background status` | `--help`, `--json`, `-h` |
| `my-agent background tick` | `--help`, `--json`, `--now`, `-h` |
| `my-agent background service` | `--help`, `--interval`, `--json`, `--max-cycles`, `--stop-file`, `-h` |
| `my-agent collaboration` | `--help`, `-h` |
| `my-agent collaboration overview` | `--help`, `--json`, `-h` |
| `my-agent collaboration list` | `--help`, `--json`, `--limit`, `--status`, `-h` |
| `my-agent collaboration status` | `--case-id`, `--help`, `--json`, `-h` |
| `my-agent collaboration update-request` | `--actor-agent-id`, `--case-id`, `--help`, `--json`, `--request-id`, `--status`, `--summary`, `-h` |
| `my-agent collaboration update-status` | `--actor-agent-id`, `--case-id`, `--decision-type`, `--help`, `--json`, `--status`, `--summary`, `-h` |
| `my-agent update` | `--check`, `--help`, `--package`, `-h` |
| `my-agent config` | `--help`, `-h` |
| `my-agent config set` | `--help`, `-h` |
| `my-agent config get` | `--help`, `-h` |
| `my-agent feishu` | `--help`, `-h` |
| `my-agent feishu connect` | `--app-id`, `--app-secret`, `--domain`, `--help`, `--restart`, `--scan`, `--secret-file`, `-h` |
| `my-agent admin` | `--help`, `-h` |
| `my-agent admin password` | `--help`, `-h` |
| `my-agent admin password set` | `--help`, `-h` |
| `my-agent admin password status` | `--help`, `--json`, `-h` |
| `my-agent admin password clear` | `--help`, `-h` |
| `my-agent admin identities` | `--help`, `-h` |
| `my-agent admin identities list` | `--help`, `--json`, `-h` |
| `my-agent admin identities remove` | `--help`, `-h` |
