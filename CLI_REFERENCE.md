# CLI Reference

当前说明对应 e905 的**开发分支**，不代表本机运行包已升级。默认内置解析树为 24 个根；第三方扩展仍可登记不冲突的独立根，但不能通过别名占用内置根或历史路由。完整 v1 手册保存在 [历史手册](docs/history/CLI_REFERENCE_V1.md)，不是新调用的依据。

```sh
my-agent --help
my-agent <规范根> --help
python -m agent_py_agent --help
```

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
- `all`：输出 `runtime`、`memory` 两个独立 facet；配置、路由、归档和警告只放在 `common` 一处。验证根或读取范围不同的公共结果以对应 facet 命名，不伪装成同一事实。整体健康为两个原条件的合取。
- v1 平铺输入仅在根边界转成 runtime 兼容投影，保留原字段及**固定退出 0**，即使健康字段为 false 也不改旧外部合同。它不是第二份诊断实现，也不是规范诊断的退出语义。

唯一服务为 `agent_py_agent/agent/user_space/home_doctor.py::DoctorService`。CLI 只注入现有只读 planner 工厂并格式化结果；两份旧 CLI Doctor 模块及旧 reexport 已移除。请求缓存区分 owner、索引、验证根和范围，不跨 owner 或范围复用健康结果。

## Slash 退出与管理员撤销

- 规范退出为 `/exit`；原明确退出词仍保留。
- `logout`、`/logout` 被拒绝并提示选择明确语义：不能退出界面，也不能撤销管理员认证。
- `/admin logout` 沿原身份校验撤销本人绑定，不等同退出；本机/群与 IM 私聊的既有边界不变。
- 发布 N 的 `/user`、`/v`、`/quit` 只在输入边界转换为 `/users`、`/verbose`、`/exit`，规范目录没有别名成员。
- MCP 标准协议的 `list_tools` 保留，不能和产品工具入口优化混淆。

## 配置 CLI 与错误口径

`config set` 保留原五键白名单：`feishu_app_id`、`feishu_app_secret`、`feishu_verification_token`、`feishu_encrypt_key`、`feishu_callback_port`。CLI 委托现有 typed 参数写服务与全局写门，原验证、正式回读和脱敏账本生效；没有新增直接 YAML 写入或 Slash 身份旁路。读取使用统一配置事实并脱敏。实际账号秘密优先使用宿主安全卡入口，不在普通聊天或示例命令中填写。

`STORE_SCHEMA_NEWER`、`TOOL_SCHEMA_INVALID_MODE_COMBINATION`、`MEMORY_COMMIT_OBLIGATION_QUARANTINED` 在真实 `agent/contracts/error_taxonomy.py` 统一分类；本分支只追加分类，不声称其他席位的触发点已接线，也不因此自动重试、迁移或解除隔离。

## 永久冻结的外部守护合同

按真实基线 `4bea324e3209ef9b9ecc39b6c2378bacc078b374` 扫描并冻结：`gateway supervisor`、`gateway run`、`adapter start`、`gateway maintenance-lock`、`gateway maintenance-unlock`，以及实际持久化的 `python -m agent_py_agent` 模块入口。参数、默认值与原 handler 不随人工命令整理而改变。没有新增不存在的 `agent_py_agent.runtime.*` 入口。

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
