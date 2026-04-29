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
| 查看帮助 | `my-agent --help` | 否 | 否 |
| 单轮对话 | `my-agent run "任务"` | 否 | 是，取决于配置的模型后端 |
| 交互聊天 | `my-agent chat` | 前台交互 | 是，用户发送消息时调用 |
| 一轮父代理调度 | `my-agent subagents-dispatch` | 否 | 否，默认 dry-run |
| 持续父代理调度 | `my-agent subagents-dispatch --watch --interval 30` | 前台常驻 | 否，除非加 `--apply --execute-runners` |
| 父代理 LLM planner 调度 | `my-agent subagents-dispatch --watch --planner --interval 30` | 前台常驻 | 是，有待处理事项时调用父代理 planner |
| 配置驱动前台 daemon | `my-agent daemon` | 前台常驻 | 取决于 `daemon_*` 配置 |
| 真实 runner 调度 | `my-agent subagents-dispatch --apply --execute-runners` | 否 | 是 |
| 子代理单次执行 | `my-agent subagent-run <run_id> --execute` | 否 | 是 |

当前 gateway 尚未实现。现在的常驻方式是前台 watch 进程，后续可以在它外面增加 `my-agent gateway start/status/stop`。常驻形态和外部方案对比见 [GATEWAY_DESIGN.md](GATEWAY_DESIGN.md)。

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

持续巡检并允许真实推进 runner：

```powershell
my-agent subagents-dispatch --watch --planner --apply --execute-runners --interval 30 --max-runners 1
```

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
| `chat` | 启动交互循环 | 默认写记忆，可用 `--no-save` 关闭 | 是，用户发消息时调用 |
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

## `chat`

```powershell
my-agent chat
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--inject <text>` | - | 启动时注入 prompt，可多次传入。 |
| `--prompt-file <path>` | - | 启动时加载额外 prompt 文件，可多次传入。 |
| `--memory-limit <n>` | `5` | 交互中 `/memory` 默认显示条数。 |
| `--no-save` | `false` | 交互对话不自动保存到记忆。 |

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
