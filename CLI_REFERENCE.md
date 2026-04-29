# CLI Reference

这份文档是 `my-agent` 的命令行参数手册。日常快速确认可以用：

```powershell
my-agent --help
my-agent <command> --help
```

## 安装与入口

在仓库根目录安装本地开发版本：

```powershell
cd C:\Users\41542\Desktop\my_agent\simple-python-agent-v0.3
python -m pip install -e .
```

安装后直接运行：

```powershell
my-agent
my-agent --help
```

如果还没有安装，也可以用模块入口：

```powershell
python -m agent_py_agent --help
```

## 顶层参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--config <path>` | `agent_py_agent/config/agent_config.yaml` | 指定主配置文件，控制模型后端、API base、API key 环境变量名、记忆路径、工具开关和 subagent workspace。 |
| `-h`, `--help` | - | 显示帮助。 |

## 运行形态

| 形态 | 命令 | 是否常驻 | 是否调用真实 API |
| --- | --- | --- | --- |
| 默认入口 | `my-agent` | 前台 chat + 后台 gateway | 是，由后台 gateway 调用 |
| 查看帮助 | `my-agent --help` | 否 | 否 |
| 单轮对话 | `my-agent run "任务"` | 否 | 是，取决于配置的模型后端 |
| 交互聊天 | `my-agent chat` | 前台交互 | 是，用户发送消息时调用 |
| gateway 客户端聊天 | `my-agent chat --gateway` | 前台客户端，后台 gateway 执行 | 是，由后台 gateway 调用 |
| 一轮父代理调度 | `my-agent subagents-dispatch` | 否 | 否，默认 dry-run |
| 持续父代理调度 | `my-agent subagents-dispatch --watch --interval 30` | 前台常驻 | 否，除非加 `--apply --execute-runners` |
| 父代理 LLM planner 调度 | `my-agent subagents-dispatch --watch --planner --interval 30` | 前台常驻 | 是，有待处理事项时调用父代理 planner |
| 配置驱动前台 daemon | `my-agent daemon` | 前台常驻 | 取决于 `daemon_*` 配置 |
| 后台 gateway | `my-agent gateway start` | 后台常驻 | 取决于 `daemon_*` 配置 |
| gateway 客户端请求 | `my-agent gateway ask "任务"` | 否，投递到后台 gateway | 是，由后台 gateway 调用 |
| 真实 runner 调度 | `my-agent subagents-dispatch --apply --execute-runners` | 否 | 是 |
| 隔离全流程测试 | `my-agent scenario-test` | 临时启动并停止 gateway | 是，除非加 `--dry-run` |
| 子代理单次执行 | `my-agent subagent-run <run_id> --execute` | 否 | 是 |

当前 gateway 第一版已经实现为本地后台进程控制面：它管理 pid、state、heartbeat、stop request、日志和本地请求队列，并在内部复用 daemon/watch 调度。`my-agent` 不带子命令时会自动确保 gateway 存活，然后进入 `chat --gateway`。常驻形态和外部方案对比见 [GATEWAY_DESIGN.md](GATEWAY_DESIGN.md)。

## 常用命令

安全检查一轮 watch：

```powershell
my-agent subagents-dispatch --watch --max-cycles 1 --interval 0 --max-runners 0
```

持续巡检但不写回、不调用 runner：

```powershell
my-agent subagents-dispatch --watch --interval 30
```

持续巡检，并在有待处理事项时唤醒父代理 LLM planner：

```powershell
my-agent subagents-dispatch --watch --planner --interval 30
```

按配置启动前台 daemon：

```powershell
my-agent daemon
```

默认启动体验：

```powershell
my-agent
```

等价于：自动启动后台 gateway，然后进入 `my-agent chat --gateway`。退出 chat 不会关闭 gateway，后续还可以继续 `my-agent` 或 `my-agent chat --gateway` 接回去。

手动管理后台 gateway：

```powershell
my-agent gateway start
my-agent gateway status
my-agent gateway ask "继续推进当前任务"
my-agent gateway stop
```

持续巡检并允许真实推进 runner：

```powershell
my-agent subagents-dispatch --watch --planner --apply --execute-runners --interval 30 --max-runners 1
```

跑一轮可观察的隔离全流程测试：

```powershell
my-agent scenario-test
```

它会新建临时 fixture 工作区，经过 `gateway ask` 让主代理派工，再执行真实 runner 和父代理验收。所有 memory、subagent、gateway 和写文件工具都被 `workspace_root` 关在临时目录里。

停止前台 watch：

```text
Ctrl+C
```

## 命令总览

| 命令 | 用途 | 写文件 | 真实 API |
| --- | --- | --- | --- |
| `run` | 运行一次智能体对话 | 默认写记忆，可用 `--no-save` 关闭 | 是，除非配置 echo 后端 |
| `remember` | 手动写入一条记忆 | 是 | 否 |
| `memory-list` | 列出最近记忆 | 否 | 否 |
| `memory-search` | 搜索记忆 | 否 | 否 |
| `local-store-status` | 查看本地事实源状态 | 否 | 否 |
| `local-search` | 搜索 SQLite/FTS5 本地事实源 | 否 | 否 |
| `local-index-memory` | 把旧 JSONL 记忆补建到本地事实源 | 是 | 否 |
| `chat` | 启动交互循环 | 默认写记忆，可用 `--no-save` 关闭 | 是；加 `--gateway` 时由后台 gateway 调用 |
| `spawn-subagents` | 拆分并创建 subagent 工单 | 是 | 否 |
| `subagents` | 查看 subagent 看板 | 否 | 否 |
| `subagents-due-check` | 巡检 subagent 风险 | 写全局 due-check 报告 | 否 |
| `subagents-probe` | 检查 subagent 通道健康 | 写 probe 报告和单任务记录 | 否 |
| `subagents-plan-actions` | 根据 due-check 生成动作计划 | 写 action plan 报告 | 否 |
| `subagents-apply-actions` | dry-run 或执行低风险动作 | `--apply` 时写回任务和审计日志 | 否 |
| `subagents-route-capabilities` | 路由 capability request | `--apply` 时写 grant/gap | 否 |
| `subagents-acceptance` | 验收等待验收的 subagent | `--apply` 时写回状态和审计日志 | 否 |
| `subagents-patches` | 审核 runner 输出的 patch 记录 | `--apply` 时写 patch 审核状态和日志 | 否 |
| `subagents-dispatch` | 执行父代理调度 | dry-run 写报告；`--apply` 写回 | 只有 `--apply --execute-runners` 会调用 |
| `daemon` | 按 `agent_config.yaml` 的 `daemon_*` 配置启动前台常驻调度 | 取决于配置 | 取决于配置 |
| `scenario-test` | 跑一轮隔离的 gateway/chat/subagent/runner/验收全流程 | 写临时 fixture 和报告 | 默认调用真实 API，可用 `--dry-run` 跳过 runner |
| `gateway` | 管理后台 gateway 进程 | 写 gateway pid/state/heartbeat/log | 取决于配置 |
| `subagent-context` | 生成单个 subagent 执行上下文 | 是 | 否 |
| `subagent-run` | 按执行上下文运行一个 subagent | 是 | 只有 `--execute` 会调用 |
| `subagent` | 查看单个 subagent 详情 | 否 | 否 |

## `run`

```powershell
my-agent run "总结这个项目" --no-save
```

| 参数 | 说明 |
| --- | --- |
| `prompt` | 必填，用户任务或问题。 |
| `--inject <text>` | 动态注入 prompt，可多次传入。 |
| `--prompt-file <path>` | 加载额外动态 prompt 文件，可多次传入。 |
| `--save` | 保存本次对话到记忆。 |
| `--no-save` | 不保存本次对话到记忆。 |
| `--show-prompt` | 打印最终拼装后的 prompt。 |

## `remember`

```powershell
my-agent remember "我喜欢清晰的表格" --kind preference
```

| 参数 | 说明 |
| --- | --- |
| `content` | 必填，记忆内容。 |
| `--kind <kind>` | 记忆类型，默认 `note`，常用值如 `note`、`preference`、`fact`。 |

## `memory-list`

```powershell
my-agent memory-list --limit 20
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--limit <n>` | `20` | 最多显示条数。 |

## `memory-search`

```powershell
my-agent memory-search "表格" --limit 5
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | - | 必填，搜索关键词。 |
| `--limit <n>` | `5` | 最多显示条数。 |

## `local-store-status`

```powershell
my-agent local-store-status
```

显示本地事实源当前状态，包括 SQLite 数据库路径、正文文件目录、审计 JSONL 路径、FTS5 是否可用、记录数和事件数。

这条命令不会调用模型，也不会写业务记录；它只会在启动 agent 时确保本地事实源结构存在。

## `local-search`

```powershell
my-agent local-search "表格" --source-type memory --limit 5
my-agent local-search "gateway 日志" --source-type gateway_request
my-agent local-search "某个子任务目标" --source-type subagent_run
```

搜索本地事实源。它会优先走 SQLite FTS5；如果当前 Python/SQLite 不支持 FTS5，或某次查询被 FTS5 语法拒绝，会自动退回普通 LIKE 检索。

当前已经写入本地事实源的主要来源：

| source_type | 内容 |
| --- | --- |
| `memory` | 用户/助手记忆 |
| `gateway_request` | gateway ask 的提交、处理、完成/失败摘要 |
| `gateway_event` | gateway 启动、停止、失败、清理等生命周期事件 |
| `subagent_run` | 子代理工单当前状态、目标、证据和能力请求摘要 |
| `subagent_work_log` | 单个子代理 `WORK_LOG.md` 追加项 |
| `subagent_runner_result` | runner dry-run / execute 结果 |
| `subagent_execution_context` | 子代理执行上下文生成记录 |
| `subagent_acceptance_review` | 父代理验收记录 |
| `subagent_patch_review` | patch 审核记录 |
| `subagent_dispatch` | 父代理调度单条动作 |
| `subagent_dispatch_report` | 最近一次 dispatch 汇总报告 |
| `subagent_dispatch_watch` | watch 循环记录 |
| `parent_planner` | 父代理 planner 决策记录 |
| `subagent_capability_route` | 能力请求路由记录 |
| `subagent_action_apply` | action apply 记录 |
| `subagent_channel_probe` | 子代理通道健康检查记录 |

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | - | 必填，搜索关键词。 |
| `--limit <n>` | `5` | 最多显示条数。 |
| `--source-type <type>` | - | 只搜索某类来源，例如 `memory`、`gateway_request`、`subagent_run`。 |
| `--visibility <value>` | - | 只搜索某种可见性，默认不过滤。第一版常见值是 `private`。 |
| `--preview-chars <n>` | `500` | 每条命中最多打印多少正文字符；`-1` 表示完整打印。 |

## `local-index-memory`

```powershell
my-agent local-index-memory
```

把已有 `memory_path` 里的 JSONL 记忆补建到 SQLite/FTS5 本地事实源。升级到这版之后可以先跑一次，后续新记忆会自动双写：JSONL 保留原始流水，SQLite/FTS5 负责检索。

## `chat`

```powershell
my-agent chat
my-agent gateway start
my-agent chat --gateway
```

默认 `my-agent chat` 会在当前前台进程里调用模型。`my-agent chat --gateway` 则只把普通消息投递给已经启动的后台 gateway，当前 chat 变成客户端。这样退出 chat 后，gateway 仍可继续常驻；后续 TUI/聊天工具也会走同一条通道。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--inject <text>` | - | 启动时注入 prompt，可多次传入。 |
| `--prompt-file <path>` | - | 启动时加载额外 prompt 文件，可多次传入。 |
| `--memory-limit <n>` | `5` | 交互中 `/memory` 默认显示条数。 |
| `--no-save` | `false` | 交互对话不自动保存到记忆。 |
| `--gateway` | `false` | 普通聊天消息投递给后台 gateway；如果 gateway 没启动，会提示先执行 `my-agent gateway start`。 |
| `--gateway-timeout <seconds>` | `gateway_request_timeout` | gateway 模式等待单条响应的秒数。 |

chat 内部命令仍在本地处理，例如 `/memory`、`/remember`、`/subagents`。普通自然语言消息才会进入模型；在 `--gateway` 模式下，这些普通消息会走 gateway request/response。

普通自然语言进入模型后，主代理可以调用三个编排工具：

| 工具 | 作用 | 风险边界 |
| --- | --- | --- |
| `create_subagents` | 创建一个或多个子代理工单 | 默认只授予 read-only 工具；`tool_preset="coding"` 才授予文件读写工具 |
| `subagent_board` | 读取当前子代理看板 | 只读 |
| `dispatch_subagents` | 执行一轮父代理调度 | 默认 dry-run；必须同时 `apply=true` 和 `execute_runners=true` 才会真实调用 runner API |

因此你可以在 chat 里说“拆给两个子代理做，并先 dry-run 看看调度计划”。如果要做真实 runner 测试，建议明确说明“使用隔离 fixture 目录、允许真实 API、最多 N 个 runner”。

## `spawn-subagents`

```powershell
my-agent spawn-subagents "开发一个可验收的功能" --count 2
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `goal` | - | 必填，要拆分的目标。 |
| `--count <n>` | `3` | 子代理数量。 |

## `subagents`

```powershell
my-agent subagents --limit 20
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--all` | `false` | 显示全部记录，而不是默认红灯/最近记录。 |
| `--status <status>` | - | 按状态过滤，如 `BLOCKED`、`DONE`、`TAKEN_OVER`。 |
| `--owner <owner>` | - | 按 `owner`、`supervisor` 或 `final_owner` 过滤。 |
| `--root-id <id>` | - | 按根任务 ID 过滤。 |
| `--limit <n>` | `20` | 最多显示多少条。 |

## `subagents-due-check`

```powershell
my-agent subagents-due-check --limit 20
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--all` | `false` | 显示全部问题，而不是按 limit 截断。 |
| `--limit <n>` | `20` | 最多显示多少条问题。 |

## `subagents-probe`

```powershell
my-agent subagents-probe --limit 20
my-agent subagents-probe <run_id>
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `run_id` | - | 可选，指定子代理运行 ID；不传则检查最近记录。 |
| `--limit <n>` | `20` | 不指定 `run_id` 时最多检查多少条。 |

## `subagents-plan-actions`

```powershell
my-agent subagents-plan-actions --limit 20
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--all` | `false` | 显示全部动作，而不是按 limit 截断。 |
| `--limit <n>` | `20` | 最多显示多少条动作。 |

## `subagents-apply-actions`

```powershell
my-agent subagents-apply-actions --dry-run
my-agent subagents-apply-actions --apply --action reopen_for_evidence
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--dry-run` | 默认模式 | 只预览动作，不修改记录。 |
| `--apply` | `false` | 真正执行低风险动作。 |
| `--action <name>` | - | 只处理指定动作，如 `reopen_for_evidence`。 |
| `--run-id <id>` | - | 只处理指定子代理运行 ID，可多次传入。 |
| `--limit <n>` | `20` | 最多处理多少条动作。 |
| `--take-over-by <name>` | - | 接管动作的接管者，apply takeover 时必填。 |
| `--locked-file <path>` | - | 接管时锁定的文件，可多次传入。 |

## `subagents-route-capabilities`

```powershell
my-agent subagents-route-capabilities --dry-run
my-agent subagents-route-capabilities --apply --skill-dir .\skills
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--dry-run` | 默认模式 | 只预览路由，不生成 grant/gap。 |
| `--apply` | `false` | 真正生成 capability grant 或 gap。 |
| `--run-id <id>` | - | 只处理指定子代理运行 ID，可多次传入。 |
| `--skill-dir <path>` | - | 额外 skill 目录，可多次传入。 |
| `--limit <n>` | `20` | 最多处理多少条 request。 |

## `subagents-acceptance`

```powershell
my-agent subagents-acceptance --dry-run
my-agent subagents-acceptance --apply --reviewer parent
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--dry-run` | 默认模式 | 只生成验收报告，不修改记录。 |
| `--apply` | `false` | 验收通过时标记 `DONE/VERIFIED`，失败时标记 `BLOCKED/FAILED`。 |
| `--run-id <id>` | - | 只验收指定子代理运行 ID，可多次传入。 |
| `--limit <n>` | `20` | 最多处理多少条记录。 |
| `--reviewer <name>` | `parent` | 验收者标识。 |
| `--note <text>` | - | 写入验收记录的备注。 |

## `subagents-patches`

```powershell
my-agent subagents-patches --dry-run
my-agent subagents-patches --apply --reviewer parent
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--dry-run` | 默认模式 | 只生成 patch 审核报告，不修改记录。 |
| `--apply` | `false` | 写回 patch 审核状态。 |
| `--run-id <id>` | - | 只审核指定子代理运行 ID，可多次传入。 |
| `--limit <n>` | `20` | 最多处理多少条记录。 |
| `--reviewer <name>` | `parent` | 审核者标识。 |
| `--note <text>` | - | 写入 patch 审核记录的备注。 |

## `subagents-dispatch`

```powershell
my-agent subagents-dispatch
my-agent subagents-dispatch --apply
my-agent subagents-dispatch --apply --execute-runners
my-agent subagents-dispatch --watch --planner --interval 30
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--dry-run` | 默认模式 | 只生成调度报告，不修改记录。 |
| `--apply` | `false` | 执行低风险调度动作并写审计日志。 |
| `--execute-runners` | `false` | 配合 `--apply` 调用真实模型执行 runner；不能单独使用。 |
| `--planner` | `false` | 有 active/pending/stalled/needs-intervention 事项时调用父代理 LLM planner；如果模型只回 `HEARTBEAT_OK`，会被 gate 标记为失败。 |
| `--max-runners <n>` | `1` | 本轮最多推进多少个 runner；`0` 表示不执行 runner。 |
| `--limit <n>` | `20` | 每个阶段最多处理多少条记录；`0` 表示不限制。 |
| `--watch` | `false` | 持续循环执行 dispatch。 |
| `--interval <seconds>` | `30.0` | watch 模式每轮间隔秒数；`0` 表示不等待，通常只用于测试或单轮验证。 |
| `--max-cycles <n>` | `0` | watch 模式最多循环次数，`0` 表示持续运行。 |
| `--force-lock` | `false` | 强制覆盖已有 watch lock；只应在确认旧进程已退出后使用。 |
| `--reviewer <name>` | `parent-dispatch` | patch/acceptance 审核者标识。 |
| `--note <text>` | - | 写入调度关联审核记录的备注。 |
| `--instruction <text>` | - | 给本轮 runner 的额外指令。 |
| `--max-cards <n>` | `0` | runner 最多注入多少张能力卡，`0` 表示不限制。 |
| `--no-probe` | `false` | 执行 runner 前不做通道健康检查。 |
| `--take-over-by <name>` | - | 接管动作的接管者，apply takeover 时必填。 |
| `--locked-file <path>` | - | 接管时锁定的文件，可多次传入。 |
| `--skill-dir <path>` | - | 额外 skill 目录，可多次传入。 |

watch 输出位置：

```text
agent_py_agent/data/subagents/subagent_dispatch_watch_heartbeat.json
agent_py_agent/data/subagents/subagent_dispatch_watch_report.json
agent_py_agent/data/subagents/SUBAGENT_DISPATCH_WATCH.md
agent_py_agent/data/subagents/subagent_dispatch_watch_log.jsonl
agent_py_agent/data/subagents/DISPATCH_WATCH_LOG.md
```

planner 输出位置：

```text
agent_py_agent/data/subagents/parent_planner_report.json
agent_py_agent/data/subagents/PARENT_PLANNER.md
agent_py_agent/data/subagents/parent_planner_prompt.md
agent_py_agent/data/subagents/parent_planner_response.md
agent_py_agent/data/subagents/parent_planner_log.jsonl
agent_py_agent/data/subagents/PARENT_PLANNER_LOG.md
```

## `daemon`

```powershell
my-agent daemon
```

`daemon` 是配置驱动的前台常驻入口，等价于按 `agent_config.yaml` 的 `daemon_*` 配置调用 `subagents-dispatch --watch ...`。它不是后台 service，终端关闭或 `Ctrl+C` 后会停止。

常用覆盖：

```powershell
my-agent daemon --apply --execute-runners
my-agent daemon --max-cycles 1 --interval 0 --no-planner
```

| 参数 | 默认值来源 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--dry-run` | 覆盖 `daemon_apply` | 只生成报告，不写回。 |
| `--apply` | 覆盖 `daemon_apply` | 写回低风险动作和审计日志。 |
| `--execute-runners` | 覆盖 `daemon_execute_runners` | 配合 apply 调用真实模型执行 runner。 |
| `--no-execute-runners` | 覆盖 `daemon_execute_runners` | 不调用真实模型执行 runner。 |
| `--planner` | 覆盖 `daemon_planner` | 启用父代理 LLM planner。 |
| `--no-planner` | 覆盖 `daemon_planner` | 关闭父代理 LLM planner。 |
| `--interval <seconds>` | `daemon_interval` | 每轮调度结束后的等待秒数；`0` 表示不等待，通常只用于测试或单轮验证。 |
| `--max-runners <n|auto>` | `daemon_max_runners` | 每轮最多推进多少个 runner；`auto` 当前映射为保守值 1，未来 gateway 会自适应；`0` 表示不执行 runner。 |
| `--limit <n>` | `daemon_limit` | 每个阶段最多处理多少条记录；`0` 表示不限制。 |
| `--max-cycles <n>` | `daemon_max_cycles` | 最多循环次数，`0` 表示持续运行。 |
| `--force-lock` | - | 强制覆盖已有 watch lock。 |
| `--reviewer <name>` | `daemon_reviewer` | patch/acceptance 审核者标识。 |
| `--note <text>` | - | 写入调度关联审核记录的备注。 |
| `--instruction <text>` | `daemon_runner_instruction` | 给 runner 的额外指令。 |
| `--max-cards <n>` | `daemon_max_cards` | runner 最多注入多少张能力卡；`0` 表示不限制。 |
| `--no-probe` | 覆盖 `daemon_probe` | 执行 runner 前不做通道健康检查。 |
| `--take-over-by <name>` | - | 接管动作的接管者，apply takeover 时必填。 |
| `--locked-file <path>` | - | 接管时锁定的文件，可多次传入。 |
| `--skill-dir <path>` | - | 额外 skill 目录，可多次传入。 |

默认配置位置：

```yaml
# 用户层任务规模：0 表示不设硬上限，让主代理按任务复杂度决定
task_max_subagents: 0
task_max_grandchildren: 0

# 未来 gateway 调度策略：auto 表示由主代理/调度器自适应
scheduler_mode: "auto"
runner_concurrency: "auto"
runner_start_rate: "auto"
runner_timeout_seconds: "auto"
runner_failure_policy: "auto"

# 前台 daemon 过渡期参数：0 是显式策略值，不表示“未设置”
daemon_planner: true
daemon_apply: false
daemon_execute_runners: false
daemon_interval: 30
daemon_max_runners: "auto"
daemon_limit: 0
daemon_max_cycles: 0
daemon_max_cards: 0
daemon_probe: true
daemon_reviewer: "parent-daemon"
daemon_runner_instruction: ""
```

`runner_failure_policy: "auto"` 当前表示 runner 临时失败后最多尝试 2 次。可以写 `"off"` 关闭自动重试，也可以写 `"3"` 这类数字字符串表示总尝试次数。自动重试只覆盖 runner 自身错误、结构化输出解析失败、工具结果丢失这类可恢复问题。

## `scenario-test`

```powershell
my-agent scenario-test
my-agent scenario-test --workspace .\tmp-scenarios --count 2 --max-runners 2
my-agent scenario-test --case verification
my-agent scenario-test --case gateway-restart
my-agent scenario-test --case structured-repair
my-agent scenario-test --case runner-retry
my-agent scenario-test --case all --count 1
my-agent scenario-test --direct
my-agent scenario-test --dry-run
```

`scenario-test` 是专门给我们观察全流程用的隔离测试入口。默认流程是：

```text
新建临时 fixture -> 写隔离配置 workspace_root -> 启动临时 gateway
-> gateway ask 触发主代理创建子代理 -> dispatch 执行真实 runner
-> 父代理验收 -> 输出看板和 SCENARIO_SUMMARY
```

它每次都会在父目录下新建一个 `scenario-*` 子目录，不会复用当前开发仓库的数据目录。默认 `--case happy` 会调用真实 API：主代理派工走一次模型，runner 也会走真实模型。只想看调度计划时用 `--dry-run`。

`--case` 可以切换坏天气场景：

| case | 说明 | 是否调用真实 API |
| --- | --- | --- |
| `happy` | gateway ask -> 主代理派工 -> runner 写文件 -> 父代理验收 | 是 |
| `verification` | 构造“模型声称写了 artifact 但文件不存在”的伪完成记录，确认验收必须拒绝 | 否 |
| `gateway-restart` | 模拟旧 gateway 崩溃时遗留的 `processing` 请求，确认重启恢复会退回 `pending` | 否 |
| `structured-repair` | 模拟 runner 输出损坏的 `[SUBAGENT_RESULT]`，确认修复回合补齐 JSON 并通过验收 | 否 |
| `runner-retry` | 模拟 runner 第一次模型调用失败，确认下一轮 dispatch 会有限重试并完成验收 | 否 |
| `all` | 依次跑 `verification`、`gateway-restart`、`structured-repair`、`runner-retry`、`happy` | `happy` 会调用 |

| 参数 | 说明 |
| --- | --- |
| `--capability-config <path>` | 能力路由配置文件路径，默认使用 `config/capability_config.yaml`。 |
| `--case <name>` | 场景类型，默认 `happy`。 |
| `--workspace <path>` | 保存场景测试结果的父目录；不传则使用系统临时目录。 |
| `--count <n>` | 本场景创建多少个子代理，默认 `2`。 |
| `--max-runners <n>` | 每轮最多推进多少个 runner，默认 `2`。 |
| `--max-cycles <n>` | 最多执行多少轮 dispatch，默认 `3`。 |
| `--timeout <seconds>` | gateway ask 等待响应的秒数，默认 `300`。 |
| `--dry-run` | 只调度不执行真实 runner API；主代理/gateway 派工仍可能调用模型。 |
| `--planner` | dispatch 时启用父代理 planner。 |
| `--direct` | 不经过 gateway，直接用当前进程跑主代理派工；排查 gateway 时不要加。 |
| `--skill-dir <path>` | 额外 skill 目录，可多次传入。 |

关键输出：

| 输出 | 说明 |
| --- | --- |
| `run_root` | 本次测试的总目录。 |
| `fixture_root` | 被测隔离项目目录，文件工具只能在这里读写。 |
| `scenario_outputs/*.md` | runner 写出的证据报告。 |
| `scenario_summary.json` / `SCENARIO_SUMMARY.md` | 本次测试摘要。 |

## `gateway`

```powershell
my-agent gateway start
my-agent gateway status
my-agent gateway stop
my-agent gateway restart
my-agent gateway logs
my-agent gateway ask "帮我检查当前任务状态"
my-agent gateway result <request_id>
```

`gateway` 第一版是本地后台控制面。它会启动一个后台 Python 进程，在内部按配置运行现有 daemon/watch 调度，并把 pid、state、heartbeat、stop request、请求队列、响应和日志写到 `gateway_workspace`。它还不是多机器组织 gateway，也还没有 worker pool；这些会在后续接入同一命令面。

先把它理解成三层：

```text
gateway start/stop/status/logs  管后台进程活不活
gateway ask/result              给后台进程发消息、拿结果
gateway run                     内部调试入口，平时不用直接敲
```

`ask/result` 是保留给开发和调试的本地入口。以后接微信、飞书、Telegram、Web TUI 时，聊天工具会替你调用同一套 gateway 请求协议；普通用户只需要在聊天工具里发消息，gateway 做完后自动回复。

| 子命令 | 说明 |
| --- | --- |
| `start` | 启动后台 gateway；已运行时默认不重复启动。 |
| `status` | 读取 pid、state 和 heartbeat，显示是否存活。 |
| `stop` | 写 stop request，等待后台 gateway 在调度轮次之间正常退出。 |
| `restart` | 先 stop 再 start。 |
| `logs` | 显示 gateway 日志尾部。 |
| `ask` | 把一条聊天/任务请求写入本地 inbox，由后台 gateway 调用模型处理。 |
| `result` | 根据 request id 读取 `ask --no-wait` 留下的响应。 |
| `run` | 内部/调试命令，前台运行 gateway 循环；通常由 `start` 调用。 |

### `gateway ask` 的两种用法

同步用法：发消息，然后当前终端一直等到结果回来。

```powershell
my-agent gateway ask "总结一下当前项目状态"
```

适合短任务、确认 gateway 能否正常调用模型、或者临时让后台主代理回答一句话。

异步用法：只发任务，不等结果。

```powershell
my-agent gateway ask "跑一轮完整检查" --no-wait
```

它会返回类似：

```text
queued request_id=gwreq-1777442684-0b7ac8cb
```

之后再查：

```powershell
my-agent gateway result gwreq-1777442684-0b7ac8cb
```

适合比较长的任务。这个设计也是未来聊天工具“先回复已收到，做完再回你”的基础。

常用参数：

| 子命令 | 参数 | 说明 |
| --- | --- | --- |
| `start` | `--force` | 如果已有 gateway 在跑，先尝试停止再启动。 |
| `start` | `--force-lock` | 传给内部 daemon，强制覆盖已有 dispatch watch lock。 |
| `stop` | `--timeout <seconds>` | 等待正常停止的秒数，默认使用 `gateway_stop_timeout`。 |
| `stop` | `--kill` | 超时后强制终止进程。 |
| `stop` | `--reason <text>` | 写入 stop request 的原因。 |
| `restart` | `--timeout <seconds>` | 等待正常停止的秒数。 |
| `restart` | `--force` | 停止超时后强制终止旧进程。 |
| `restart` | `--force-lock` | 重启后传给内部 daemon。 |
| `logs` | `--lines <n>` | 显示最后多少行日志，`0` 表示全部。 |
| `ask` | `--inject <text>` | 给本次 gateway 请求动态注入 prompt，可多次传入。 |
| `ask` | `--prompt-file <path>` | 给本次请求追加 prompt 文件，可多次传入。 |
| `ask` | `--no-save` | 不把本次 gateway 对话保存进记忆。 |
| `ask` | `--show-prompt` | 响应返回时打印最终 prompt。 |
| `ask` | `--timeout <seconds>` | 等待后台响应的秒数，默认使用 `gateway_request_timeout`。 |
| `ask` | `--no-wait` | 只投递请求并立即返回 request id。 |
| `ask` | `--json` | 输出完整响应 JSON。 |
| `result` | `--show-prompt` | 打印响应 JSON 中保存的最终 prompt。 |
| `result` | `--json` | 输出完整响应 JSON。 |
| `run` | daemon 同名参数 | 内部调试用，支持 `--max-cycles 1 --interval 0 --no-planner` 这类安全验证。 |

### 请求文件流转

当前实现先用文件队列，不用 HTTP server。好处是跨平台、容易查问题，也方便后续替换成 SQLite 或 WebSocket。

```text
ask 写入 pending -> gateway 移到 processing -> 模型处理 -> 写 responses -> 原请求移到 done
```

如果 gateway 意外退出，重启时会把 `processing` 里没处理完的请求退回 `pending`。这表示“上次正在办但没办完，重新排队”。

gateway 控制面配置：

```yaml
gateway_workspace: "data/gateway"
gateway_heartbeat_interval: 5
gateway_stale_seconds: 120
gateway_stop_timeout: 20
gateway_request_timeout: 300
gateway_request_poll_interval: 1
```

默认文件：

```text
agent_py_agent/data/gateway/gateway.pid
agent_py_agent/data/gateway/gateway_state.json
agent_py_agent/data/gateway/gateway_heartbeat.json
agent_py_agent/data/gateway/gateway_stop.request
agent_py_agent/data/gateway/gateway.log
agent_py_agent/data/gateway/requests/pending/<request_id>.json
agent_py_agent/data/gateway/requests/processing/<request_id>.json
agent_py_agent/data/gateway/requests/done/<request_id>.json
agent_py_agent/data/gateway/responses/<request_id>.json
agent_py_agent/data/gateway/gateway_requests.jsonl
```

## `subagent-context`

```powershell
my-agent subagent-context <run_id>
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `run_id` | - | 必填，子代理运行 ID。 |
| `--max-cards <n>` | `0` | 最多注入多少张能力卡，`0` 表示不限制。 |

## `subagent-run`

```powershell
my-agent subagent-run <run_id>
my-agent subagent-run <run_id> --execute
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `run_id` | - | 必填，子代理运行 ID。 |
| `--dry-run` | 默认模式 | 只生成 prompt 和报告，不调用模型。 |
| `--execute` | `false` | 真正调用模型执行，可能消耗 API。 |
| `--instruction <text>` | - | 给本次 runner 的额外指令。 |
| `--max-cards <n>` | `0` | 最多注入多少张能力卡，`0` 表示不限制。 |
| `--no-probe` | `false` | 执行前不做通道健康检查。 |

## `subagent`

```powershell
my-agent subagent <run_id>
```

| 参数 | 说明 |
| --- | --- |
| `run_id` | 必填，子代理运行 ID。 |

## 配置文件

主配置：

```text
agent_py_agent/config/agent_config.yaml
```

能力路由配置：

```text
agent_py_agent/config/capability_config.yaml
```

API key 默认从 `AGENT_API_KEY` 环境变量读取。PowerShell 示例：

```powershell
$env:AGENT_API_KEY="你的 key"
```

## 安全约定

- 默认调度命令都是 dry-run，先写报告，不修改任务。
- `subagent-run --execute` 会调用真实 API。
- `subagents-dispatch --apply --execute-runners` 会调用真实 API。
- `subagents-dispatch --planner` 在 gate 发现有待处理事项时会调用父代理 LLM。
- `--execute-runners` 必须和 `--apply` 一起使用。
- `--watch` 是前台常驻，终端关闭或 `Ctrl+C` 后进程停止。
- `--force-lock` 只用于确认旧 watch 进程异常退出后的残留 lock。
