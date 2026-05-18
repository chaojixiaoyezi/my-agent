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
my-agent --app
my-agent --plain
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
| `--app` | `false` | 兼容参数；默认入口已经使用应用内聊天界面。 |
| `--plain` | `false` | 使用普通终端聊天模式，不进入应用内滚动历史界面。 |
| `-h`, `--help` | - | 显示帮助。 |

## 运行形态

| 形态 | 命令 | 是否常驻 | 是否调用真实 API |
| --- | --- | --- | --- |
| 默认入口 / 应用内聊天 | `my-agent` | 前台应用界面 + 后台 gateway | 是，由后台 gateway 调用 |
| 兼容应用入口 | `my-agent --app` | 前台应用界面 + 后台 gateway | 是，由后台 gateway 调用 |
| 普通终端聊天 | `my-agent --plain` | 前台 chat + 后台 gateway | 是，由后台 gateway 调用 |
| 全局状态 | `my-agent status` | 否 | 否 |
| 最近事件 | `my-agent timeline` | 否 | 否 |
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
| 主代理基础 E2E | `my-agent real-e2e --workspace ./.e2e --json` | 否 | 否；真实模型用例会明确跳过，产物可用 `--artifact` 验收 |
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

查看全局状态和最近事件：

```powershell
my-agent status
my-agent timeline --limit 20
my-agent timeline --source-type gateway_request --details
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
| `status` | 查看 gateway、LocalStore、subagent 和最近事件总览 | 否 | 否 |
| `timeline` | 查看本地事实源最近事件 | 否 | 否 |
| `run` | 运行一次智能体对话 | 默认写记忆和轻量 recovery hook，可用 `--no-save` 关闭普通会话保存 | 是，除非配置 echo 后端 |
| `remember` | 手动写入一条记忆 | 是 | 否 |
| `memory-list` | 列出最近记忆 | 否 | 否 |
| `memory-search` | 搜索记忆 | 否 | 否 |
| `home-status` | 查看 `~/.my-agent` 入口文件、关键目录和轻量计数 | 否 | 否 |
| `memory-daily-list` | 直接查看 home daily memory 按天流水 | 否 | 否 |
| `memory-route` | 按长期规则索引预览 memory 路由命中 | 否 | 否 |
| `memory-doctor` | 诊断 memory 配置、路由索引和归档目录 | 否 | 否 |
| `memory-archive-list` | 列出 raw/hook 归档记录 | 否 | 否 |
| `memory-archive-search` | 按字段搜索 raw/hook 归档 | 否 | 否 |
| `memory-resume` | 从归档、LocalStore 和任务目录生成恢复线索 | 否 | 否 |
| `task-workspace-list` | 查看 home workspace/tasks 任务工作区 | 否 | 否 |
| `context-bundle` | 查看最新主代理上下文包、scope、自检和工具/运行合同 | 否 | 否 |
| `real-e2e` | 运行主代理基础 E2E 矩阵，并可验收真实产物 | 写报告 JSON | 否；当前不会自动调用模型 |
| `memory-artifact-read` | 显式读取已登记 tool-output artifact 正文 | 否 | 否 |
| `memory-fact-write` | 写入用户确认的 compact resume 补全事实源 | 是 | 否 |
| `memory-compact` | 预演 memory compact 计划；`--apply` 生成非破坏性恢复产物 | `--apply` 时写 | 否 |
| `local-store-status` | 查看本地事实源状态 | 否 | 否 |
| `local-search` | 搜索 SQLite/FTS5 本地事实源 | 否 | 否 |
| `local-index-memory` | 把旧 JSONL 记忆补建到本地事实源 | 是 | 否 |
| `local-doctor` | 诊断 LocalStore、gateway 队列和 subagent 文件事实源 | 可选 `--repair` | 否 |
| `local-rebuild` | 从 memory/gateway/subagent 文件事实源重建 LocalStore | 是 | 否 |
| `logs` | log analysis 状态、文件导入和安全查询入口 | status/query 只读；ingest 写 log analysis 数据目录 | 否 |
| `chat` | 启动交互循环 | 默认写记忆，可用 `--no-save` 关闭 | 是；加 `--gateway` 时由后台 gateway 调用 |
| `learn` | 查看和确认自动生成的 learning draft 候选 | 读取或更新 `data/learning_drafts/*.json` | 否 |
| `spawn-subagents` | 拆分并创建 subagent 工单 | 是 | 否 |
| `subagents` | 查看 subagent 看板 | 否 | 否 |
| `subagents-due-check` | 巡检 subagent 风险 | 写全局 due-check 报告 | 否 |
| `subagents-budget` | 汇总 subagent runner 调用、工具轮数和 token 粗估 | 写预算 JSON/Markdown 报告 | 否 |
| `subagents-probe` | 检查 subagent 通道健康 | 写 probe 报告和单任务记录 | 否 |
| `subagents-plan-actions` | 根据 due-check 生成动作计划 | 写 action plan 报告 | 否 |
| `subagents-apply-actions` | dry-run 或执行低风险动作 | `--apply` 时写回任务和审计日志 | 否 |
| `subagents-workflow-plan` | 预览目标会命中哪个内置 subagent workflow | 否 | 否 |
| `subagents-leadership-recovery-plan` | 预览批量 coordinator 挂掉后的 leader 分摊接管计划 | 写 dry-run 计划报告 | 否 |
| `subagents-leadership-recovery-apply` | 按显式 child 子集重挂到新 leader | `--apply` 时只移动指定 child 子树 | 否 |
| `subagents-hierarchy` | 预览或显式创建 child/grandchild subagent run | `--apply` 时创建下一层任务 | 否 |
| `subagents-recovery-tree` | 查询 root subagent 的多层恢复交接包 | 否，只输出 refs-only 恢复线索 | 否 |
| `subagents-route-capabilities` | 路由 capability request | `--apply` 时写 grant/gap | 否 |
| `subagents-acceptance` | 验收等待验收的 subagent | `--apply` 时写回状态和审计日志 | 否 |
| `subagents-acceptance-plan` | 查看、审计或显式应用单个 subagent 的父级验收决策 | `--write` 写 dry-run 决策；`--apply` 只允许 `inspect_only` 进入普通验收 apply；`--next-action` 给上级动作建议；`--auto-policy` 写策略 dry-run 审计；`--execute-auto-tests` 只在 `--auto-execution` 下手动确认跑 tests | 否 |
| `subagents-tests` | 查看或显式重跑单个 subagent 的真实测试执行记录 | `--re-run` 时写 `test_execution.json/md` | 否 |
| `subagents-patches` | 审核或 apply runner 输出的 patch 记录 | 默认 review dry-run；`--review-apply` 只写审核状态；`--apply` 真正落文件 | 否 |
| `subagents-memory-gate` | 查看或写回子代理 memory/skill 候选 review decision | 传 `--candidate-id` 时写 `memory_gate/decisions.jsonl` 和 gate 状态 | 否 |
| `subagents-dispatch` | 执行父代理调度 | dry-run 写报告；`--apply` 写回；`--execute-acceptance-tests` 只跑父级验收 tests | 只有 `--apply --execute-runners` 会调用模型；`--execute-acceptance-tests` 会执行本地验收 tests |
| `daemon` | 按 `agent_config.yaml` 的 `daemon_*` 配置启动前台常驻调度 | 取决于配置 | 取决于配置 |
| `scenario-test` | 跑一轮隔离的 gateway/chat/subagent/runner/验收全流程 | 写临时 fixture 和报告 | 默认调用真实 API，可用 `--dry-run` 跳过 runner |
| `real-e2e` | 跑主代理基础确定性矩阵，并把指定产物交给 Artifact Acceptance 验收 | 写 refs-first 报告 | 否；真实模型产物由外部真实 run 生成后用 `--artifact` 接入 |
| `gateway` | 管理后台 gateway 进程 | 写 gateway pid/state/heartbeat/log | 取决于配置 |
| `adapter` | 外部聊天工具 / TUI 适配器入口 | 写 adapter inbox/outbox | 由后台 gateway 调用 |
| `subagent-context` | 生成单个 subagent 执行上下文 | 是 | 否 |
| `subagent-run` | 按执行上下文运行一个 subagent | 是 | 只有 `--execute` 会调用 |
| `subagent` | 查看单个 subagent 详情 | 否 | 否 |
| `audit-log` | 查询审计日志 | `--cleanup` 时写回 | 否 |

## `status`

```powershell
my-agent status
my-agent status --recent --limit 10
my-agent status --json
```

显示当前本地工作台总览：gateway 存活状态、gateway 队列数量、LocalStore 记录/事件数量、subagent summary、红灯任务、Shared Progress、Takeover View、Acceptance Plan、Acceptance Next Action、最近事件和建议下一步动作。它只读现有账本，不调用模型。

`Takeover View` 会列出可接管 run、failure handoff ref、takeover readiness ref 和 recommended read order；它只读取恢复索引，不展开 artifact 正文。若 run 携带隔离元数据，还会显示 principal、conversation、memory namespace 和 config scope 摘要；这些字段只是审计线索，不代表员工长期记忆已启用，也不代表允许写全局配置。

`Acceptance Plan` 会展示待验收、失败或阻塞 run 的父级验收 dry-run 决策，例如 `execute_tests`、`inspect_only`、`request_human` 或 `rescue`。它只显示摘要和 refs，不执行 tests、不写任务状态、不读取 artifact 正文。

`Acceptance Next Action` 会展示同一批可见 run 的父级下一动作建议，例如 `run_tests`、`request_human_confirmation`、`plan_rescue` 或 `apply_acceptance`，并列出建议命令、原因、refs 和 `mutates_task_state`。它只显示建议，不执行命令、不写任务状态、不读取 artifact 正文。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--limit <n>` | `5` | 最多显示多少条 hot/recent/timeline 项。 |
| `--recent` | `false` | 额外显示最近子代理列表。 |
| `--json` | `false` | 输出机器可读 JSON，方便后续 TUI/聊天工具复用。 |

显示进行中任务摘要：
- gateway 是否存活
- 活跃子代理任务数
- 遗留的 processing 请求数
- 最近 3 个任务的 ID、目标、状态

此功能由配置项 `auto_detect_work_on_startup` 控制，默认为 true。

## `timeline`

```powershell
my-agent timeline --limit 20
my-agent timeline --source-type gateway_request
my-agent timeline --event-type gateway_request_completed --details
```

显示 LocalStore 最近事件。它更像“发生了什么”的时间线，而 `local-search` 更像“按关键词找资料”。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--limit <n>` | `20` | 最多显示多少条事件。 |
| `--source-type <type>` | - | 按来源过滤，例如 `gateway_request`、`subagent_run`、`subagent_runner_result`。 |
| `--event-type <type>` | - | 按事件类型过滤，例如 `gateway_request_completed`、`subagent_run_saved`。 |
| `--details` | `false` | 显示事件 payload 摘要。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `run`

```powershell
my-agent run "总结这个项目" --no-save
```

默认保存时，`run` 会同时写入普通 JSONL memory、raw archive 和一条轻量 `memory/hooks/YYYY-MM-DD.jsonl` recovery snapshot。`--no-save` 会关闭普通单轮会话保存；任务类 runner 仍会通过自己的事实源写 run_id 级恢复锚点。

如果配置打开 `memory_resume_auto_context_enabled: true`，`run/chat/gateway` 会在“继续、刚刚、恢复、run_id/request_id”等恢复场景里尝试读取归档和任务事实源，并把一段短小 `Recovery Brief` 注入本轮 prompt。默认关闭，避免普通请求被恢复检索拖慢。

`run` 结束状态行会显示粗略 token 估算，例如 `prompt_tokens≈...`、`inject_tokens≈...`、`resume_tokens≈...`。这是保守估算，不是模型厂商 tokenizer 的精确计费值。

| 参数 | 说明 |
| --- | --- |
| `prompt` | 必填，用户任务或问题。 |
| `--inject <text>` | 动态注入 prompt，可多次传入。 |
| `--prompt-file <path>` | 加载额外动态 prompt 文件，可多次传入。 |
| `--save` | 保存本次对话到记忆。 |
| `--no-save` | 不保存本次对话到记忆。 |
| `--show-prompt` | 打印最终拼装后的 prompt。 |
| `--resume-context` | 本次请求临时启用恢复上下文注入，不用改配置文件。 |
| `--no-resume-context` | 本次请求临时关闭恢复上下文注入。 |

## `remember`

```powershell
my-agent remember "我喜欢清晰的表格" --kind preference
```

| 参数 | 说明 |
| --- | --- |
| `content` | 必填，记忆内容。 |
| `--kind <kind>` | 记忆类型，默认 `note`，常用值如 `note`、`preference`、`fact`。 |

## `learn`

```powershell
my-agent learn list
my-agent learn accept <candidate_id>
my-agent learn reject <candidate_id>
my-agent learn stats --json
```

当 `agent_config.yaml` 里打开 `enable_self_learning: true` 后，runner 结构化输出中的 `lessons` 会自动沉淀为 `data/learning_drafts/*.json` 候选草稿。`learn` 命令只管理这些候选，不会自动改正式 skill。

| 子命令 | 说明 |
| --- | --- |
| `learn list` | 列出当前 learning draft，显示状态、置信度、出现次数和证据数。 |
| `learn accept <candidate_id>` | 把一个候选标记为 `accepted`，表示这条 lesson 值得后续人工提升。 |
| `learn reject <candidate_id>` | 把一个候选标记为 `rejected`。 |
| `learn stats` | 输出 draft/accepted/rejected 汇总；可加 `--json`。 |

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

## `home-status`

```powershell
my-agent home-status
my-agent home-status --json
```

查看当前配置解析出的 `~/.my-agent` 家目录状态，包括 `SOUL.md`、`USER.md`、`AGENTS.md`、`memory.md` 是否存在，`memory/daily`、`workspace/tasks`、`scripts`、`role_templates`、`workflows` 等关键目录是否存在，以及 daily 文件数和 task workspace 数量。它只读目录结构，不调用模型、不扫描产物正文。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--json` | `false` | 输出机器可读 JSON。 |

## `memory-daily-list`

```powershell
my-agent memory-daily-list "表格" --date 2026-05-13
my-agent memory-daily-list --date 2026-05-13 --role user --kind preference --json
```

直接查看 home daily memory：也就是 `~/.my-agent/memory/daily/YYYY-MM-DD.jsonl` 里的按天记忆流水。这个命令适合调试“今天到底存了什么记忆”，不会读取旧 `memory_path`，也不会调用模型。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | 空 | 可选搜索关键词；为空时列出匹配日期、角色和类型的记录。 |
| `--date <YYYY-MM-DD>` | - | 只查看某一天的 daily memory 文件。 |
| `--role <role>` | 空 | 按 role 精确过滤，例如 `user`、`assistant`、`tool`。 |
| `--kind <kind>` | 空 | 按 kind 精确过滤，例如 `dialogue`、`preference`、`note`。 |
| `--limit <n>` | `50` | 最多显示多少条记录；未传时读 `cli_task_list_limit`。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `memory-route`

```powershell
my-agent memory-route "上下文压缩前要不要读长期规则"
my-agent memory-route "memory index" --index memory/routing/INDEX.md --mode strict --limit 5 --auto-read-limit 2
my-agent memory-route --validate --json
my-agent memory-route "任务恢复规则" --json
```

按长期规则路由索引预览查询会命中哪些 authority 文件。默认索引是工作区下的 `memory/routing/INDEX.md`；索引不存在时不会崩溃，会输出清楚的诊断信息。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | - | 可选，要路由的查询或用户任务；配合 `--validate` 时可以省略。 |
| `--index <path>` | `memory/routing/INDEX.md` | 指定路由索引文件；相对路径按 agent workspace root 解析。 |
| `--mode <mode>` | 配置 `memory_rule_routing_mode` | 路由模式，可选 `off`、`soft`、`strict`。 |
| `--validate` | `false` | 只校验 route index 的重复关键词、冲突关键词、死链和非法 `inject_mode`。 |
| `--limit <n>` | `5` | 最多显示多少条命中 route；`0` 表示不截断。 |
| `--auto-read-limit <n>` | 配置 `memory_rule_auto_read_limit` | 最多升级多少条规则路径到 required/candidate。 |
| `--json` | `false` | 输出机器可读 JSON，包含 `matches`、`required_read_paths`、`candidate_paths` 和诊断信息。 |

## `memory-doctor`

```powershell
my-agent memory-doctor
my-agent memory-doctor --index memory/routing/INDEX.md
my-agent memory-doctor --json
```

诊断 memory 配置和文件骨架：显示 memory 配置的 effective values、配置回退 warnings、home runtime 入口文件/目录状态、路由索引是否存在、routes 加载和校验结果，以及 `memory/hooks`、`memory/raw` 目录的文件数、最近文件和 hook retention 配置。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--index <path>` | `memory/routing/INDEX.md` | 指定路由索引文件；相对路径按 agent workspace root 解析。 |
| `--json` | `false` | 输出机器可读 JSON，包含 `home`、`warnings`、`routing.routes` 和 archive 目录状态。 |

## `memory-archive-list`

```powershell
my-agent memory-archive-list
my-agent memory-archive-list --layer raw --limit 20
my-agent memory-archive-list --layer raw --level 3 --json
my-agent memory-archive-list --date 2026-04-30 --json
```

列出 `memory/raw` 和 `memory/hooks` 里的最近归档记录。它只读 JSONL，不调用模型；输出会标明 layer、记录 ID、run/request/session、文件路径和行号。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--layer <layer>` | `all` | 查看哪一层归档，可选 `all`、`raw`、`hook`。 |
| `--date <YYYY-MM-DD>` | - | 只查看某一天的归档文件。 |
| `--level <0|1|2|3>` | - | 只查看指定 `archive_level` 的记录。 |
| `--limit <n>` | `20` | 最多显示多少条记录。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `memory-archive-search`

```powershell
my-agent memory-archive-search "README" --speaker tool
my-agent memory-archive-search --run-id subagent-xxx --status ok --json
my-agent memory-archive-search "继续" --since 2026-04-30 --layer raw
```

按关键词和结构化字段搜索 `memory/raw` / `memory/hooks`。这层是恢复线索，不是任务最终事实源；查到线索后仍应继续读任务目录、LocalStore 记录或 authority 文件。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | 空 | 搜索关键词，可不传，只用字段过滤。 |
| `--layer <layer>` | `all` | 搜索哪一层归档，可选 `all`、`raw`、`hook`。 |
| `--date <YYYY-MM-DD>` | - | 只搜索某一天。 |
| `--since <time>` | - | 只看此时间之后的记录，支持 ISO 时间或日期。 |
| `--until <time>` | - | 只看此时间之前的记录，支持 ISO 时间或日期。 |
| `--session-id <id>` | - | 按 session_id 精确过滤。 |
| `--request-id <id>` | - | 按 request_id 精确过滤。 |
| `--run-id <id>` | - | 按 run_id 精确过滤。 |
| `--task-id <id>` | - | 按 task_id 精确过滤。 |
| `--speaker <name>` | - | 按 speaker 精确过滤，如 `user`、`assistant`、`tool`。 |
| `--target <name>` | - | 按 target 精确过滤。 |
| `--action <name>` | - | 按 action 精确过滤，如 `message`、`response`、`tool_call`。 |
| `--status <status>` | - | 按 status 精确过滤，如 `ok`、`failed`。 |
| `--tool-name <name>` | - | 按工具名精确过滤。 |
| `--source <source>` | - | 按来源精确过滤，如 `run`、`gateway`、`subagent`。 |
| `--limit <n>` | `20` | 最多显示多少条记录。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `memory-resume`

```powershell
my-agent memory-resume "继续 README 那个任务"
my-agent memory-resume --run-id subagent-xxx --json
my-agent memory-resume --request-id gwreq-xxx --limit 10
my-agent memory-resume "继续" --context-only
my-agent memory-resume --from-compact apply-xxx --context-only
```

从归档线索、LocalStore 检索结果、旧 subagent 任务目录和 home task workspace 中生成恢复简报。它会列出 archive clues、LocalStore clues、任务事实源路径和下一步建议，提醒你先读 `STATUS.md`、`WORK_LOG.md`、`HANDOFF.md`、`TEST_CHECKLIST.md`，或 `~/.my-agent/workspace/tasks/<date>/<task>/state.json` / `timeline.jsonl` 等权威文件后再继续。

`--context-only` 只打印稳定格式的 `Recovery Brief` 文本块，不打印外层说明。这个输出适合复制给真实环境测试、人工 handoff，后续也可以作为自动上下文注入的复用入口。

`--from-compact <apply_id>` 会改走 compact apply 恢复路径，只读读取 `memory-compact --apply` 生成的 metadata、apply bundle、restore refs、work state snapshot、compact context 和 self-check，输出 `Compact Resume Context`、consistency report、action guard、continue packet、推荐读取路径和下一步动作。它不会自动执行工具，也不会修改任务或子代理文件；`--compact-owner-type/--compact-owner-id` 只是给未来子代理会话压缩预留 owner 字段。

compact 恢复输出还会包含 `compact_resume_handoff` 和 `compact_continue_packet`：handoff 稳定展示目标、当前阶段、下一步、验收条件、约束、最近测试、推荐读取路径和 action guard 状态；continue packet 则给手动/半自动/未来自动流程一个机器可读的继续契约。`--context-only` 打印的 `Compact Resume Context` 也会分节包含这些内容，适合复制给新会话或其他 agent 接手。

`--compact-resume-mode auto` 会启用更严格的 Action Guard：缺 acceptance、constraints、latest tests、refs 或 self-check 失败时会返回非 0，防止无人值守状态继续偏航。字段齐全、refs 存在且 self-check 通过时会返回 `allow_automated_continue` / `allowed_to_continue=true`，continue packet 会标记 `ready_to_continue=true`，但仍标记 `automatic_tool_execution=none`，表示只允许后续策略接着判断，不会由 resume 命令直接跑工具。默认 `manual` 只生成恢复材料和人工确认提示。

缺 work state 字段时，compact 恢复输出会包含 `completion_prompt`。这会给出可复制的“验收条件/约束/测试”模板和建议命令；模板本身不会自动写文件。用户确认这些事实后，可以用 `memory-fact-write` 写入 scope 内的 `runtime_facts/<fact_id>/task.json`，再重新按同一个 request/session/task/run scope 执行 `memory-compact --apply`。

普通 `run` 在上下文风险达到阈值时会额外打印 `compact_suggestion` 和 `compact_auto`。`compact_suggestion` 给出 `memory-compact --dry-run`、`memory-compact --apply` 和 `memory-resume --from-compact` 的建议命令；`compact_auto` 显示自动协调器当前停在 `needs_user_confirmation`、`blocked_after_action_guard`、`ready_after_action_guard` 等哪一步，并显示 `apply_id` / `continue_ready`。默认只是 plan-only 提醒；只有显式配置 `memory_compact_auto_allow_apply: true` 才会自动做非破坏性 apply + auto resume + guard/packet 停车，仍不会继续执行工具。

JSON 输出里会额外包含 `brief`：

| 字段 | 说明 |
| --- | --- |
| `latest_user_intent` | 最近能恢复出的用户意图。 |
| `latest_assistant_action` | 最近能恢复出的助手动作。 |
| `related_ids` | 关联的 session/request/run/task ID，方便下一轮精确恢复。 |
| `likely_task_statuses` | 从任务目录读出的状态摘要；没有任务目录时不会凭 archive 猜。 |
| `recommended_read_paths` | 必读事实源路径。 |
| `authority_note` | 提醒 archive/local 只是恢复线索，任务文件才是当前事实源。 |
| `context_block` | 稳定格式的恢复文本块，后续可用于人工 handoff 或自动注入。 |

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | 空 | 恢复关键词，可不传，只用 request/run/session 等字段过滤。 |
| `--layer <layer>` | `all` | 从哪一层归档找线索，可选 `all`、`raw`、`hook`。 |
| `--date <YYYY-MM-DD>` | - | 只看某一天。 |
| `--since <time>` | - | 只看此时间之后的归档线索。 |
| `--until <time>` | - | 只看此时间之前的归档线索。 |
| `--session-id <id>` | - | 按 session_id 精确过滤。 |
| `--request-id <id>` | - | 按 request_id 精确过滤。 |
| `--run-id <id>` | - | 按 run_id 精确过滤。 |
| `--task-id <id>` | - | 按 task_id 精确过滤。 |
| `--speaker <name>` | - | 按 speaker 精确过滤。 |
| `--target <name>` | - | 按 target 精确过滤。 |
| `--action <name>` | - | 按 action 精确过滤。 |
| `--status <status>` | - | 按 status 精确过滤。 |
| `--tool-name <name>` | - | 按工具名精确过滤。 |
| `--source <source>` | - | 按来源精确过滤。 |
| `--limit <n>` | `20` | 最多显示多少条线索。 |
| `--from-compact <apply_id-or-path>` | - | 从某次非破坏性 compact apply 恢复上下文，可传 apply_id 或产物路径。 |
| `--compact-resume-mode <manual|auto>` | `manual` | compact 恢复守门模式；`auto` 字段缺失会阻断，字段齐全时只返回允许继续的机器信号。 |
| `--compact-owner-type <type>` | `main_agent` | compact owner 类型；`subagent_run` / `subagent_session` 会只读解析 task-local run workspace refs。 |
| `--compact-owner-id <id>` | 空 | compact owner 标识；子代理 owner 通常传 run_id，不会写主 memory 或自动执行工具。 |
| `--context-only` | `false` | 只输出可交接/注入的恢复上下文块。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `task-workspace-list`

```powershell
my-agent task-workspace-list --date 2026-05-13
my-agent task-workspace-list "购物网站" --json
```

列出 `~/.my-agent/workspace/tasks/{date}/{task_slug}/` 下的主代理任务工作区。它会展示 `state.json`、`timeline.jsonl`、`task.yaml`、`outputs/`、`runtime/`、`agents/` 等引用，帮助恢复和前端调试；不会读取产物正文，也不会调用模型。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | 空 | 可选关键词，可匹配 task_id、task_name、run_id、request_id 或目录 slug。 |
| `--date <YYYY-MM-DD>` | - | 只查看某一天的任务目录。 |
| `--limit <n>` | `50` | 最多显示多少个任务；未传时读 `cli_task_list_limit`。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `memory-artifact-read`

```powershell
my-agent memory-artifact-read C:\repo\memory_archive\artifacts\tool_outputs\read_file-call-abc.json
my-agent memory-artifact-read <sha256> --offset 4000 --max-chars 2000 --json
my-agent memory-artifact-read <call_id> --max-chars 0
```

显式读取已经外置的 tool-output artifact 正文。这个命令只信任 `memory_archive/artifacts/tool_outputs/index.jsonl` 里的登记记录；`artifact_ref` 可以是登记过的 path、sha256 或 call_id。普通文件路径即使存在，也不会被当成 artifact 读取。

默认只读前 4000 个字符；`--offset` 可以从正文中间继续读，`--max-chars 0` 表示读取完整正文。JSON 输出会包含 `content_hash_verified=true`、`reads_artifact_body=true`、`truncated`、`content_offset` 和 `content_max_chars`，方便接管者确认这次确实是显式读取。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `artifact_ref` | 必填 | 来自恢复包、manifest 或 tool output index 的 artifact path/hash/call_id。 |
| `--offset` | `0` | 从正文第几个字符开始读取。 |
| `--max-chars` | `4000` | 最多读取多少字符；`0` 表示读取全部。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `memory-fact-write`

```powershell
my-agent memory-fact-write --fact-id request-xxx --acceptance "pytest passes" --constraint "do not touch user config" --latest-test "python3 -m pytest -q"
my-agent memory-fact-write --from-compact apply-xxx --acceptance "..." --constraint "..." --latest-test "..." --json
```

把用户已经确认的 compact resume 补全事实写入 `memory_archive/runtime_facts/<fact_id>/task.json`。它用于半自动 compact 闭环：`memory-resume --from-compact --compact-resume-mode auto` 如果因为缺 `acceptance`、`constraints`、`latest_tests` 阻断，先让用户补齐事实，再写入这个事实源，然后重新执行同一 scope 的 `memory-compact --apply`。

这个命令只保存显式传入的事实，不解析模型回复，不猜验收条件。`--from-compact` 只用于读取目标、下一步和 scope；如果没有传 `--fact-id`，命令会优先从 compact scope 中选择 `request_id`、`session_id`、`task_id`、`run_id` 作为事实源目录名。为了让后续 apply 能读到它，重新 compact 时要使用同一个 scope，例如同一个 `--request-id`。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--fact-id <id>` | - | 事实源目录名；通常使用 request_id/session_id/task_id/run_id。 |
| `--from-compact <apply_id>` | - | 可选：从 compact apply 的 handoff 读取目标、下一步和 scope。 |
| `--goal <text>` | - | 可选：当前任务目标；未传时尝试从 compact handoff 读取。 |
| `--next-action <text>` | 可重复 | 恢复后的下一步动作。 |
| `--acceptance <text>` | 可重复 | 用户确认的验收条件。 |
| `--constraint <text>` | 可重复 | 用户确认的约束或禁止事项。 |
| `--latest-test <text>` | 可重复 | 最近已跑或必须跑的测试状态。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `context-bundle`

```powershell
my-agent context-bundle latest
my-agent context-bundle latest --json
```

只读查看最新 `Main Agent Context Bundle v1`。它不会调用模型，也不会写文件。这个命令用于确认最近一次主代理保存型 run 的任务卡是否存在、scope 是否正确、自检是否通过，以及 RunScope、ToolManifest、Acceptance Contract 和 prompt budget 当前是什么。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `latest` | - | 查看最新主代理上下文包。 |
| `--json` | `false` | 输出机器可读 JSON，方便前端或调试脚本读取。 |

## `real-e2e`

```powershell
my-agent real-e2e --workspace .\.real-e2e --json
my-agent real-e2e --workspace .\.real-e2e --artifact .\outputs\index.html --json
my-agent real-e2e --workspace .\.real-e2e --real-task-suite --real-task-max-workers 4 --json
my-agent real-e2e --workspace .\.real-e2e --report .\reports\real-e2e.json
```

运行主代理基础验收矩阵。当前命令默认只跑不调用模型的确定性用例：工具失败分类、大输出 artifact refs、确定性 E2E matrix；需要真实模型的用例会明确标记 `SKIPPED`，不会把“没跑”说成通过。

如果已经用真实模型生成了产物，可以把文件路径传给 `--artifact`。命令会调用 Artifact Acceptance（产物验收）统一检查 HTML、JSON、CSV、XLSX、PDF 和未知格式的基础质量，并把 findings（问题清单）写进报告。它不展开大文件正文，只写路径、类型和结构化问题。

如果要准备多主代理真实任务测试，可以加 `--real-task-suite`。命令会生成家具 HTML、购物网站、GitHub 升星 XLSX、DeepSeek 论文翻译 PDF 等任务的 `prompt.md`、`acceptance.json`、`expected_artifacts.json` 和 `suite_report.json`，并给每个任务分配 worker slot（工位）和 timeout（超时）。默认仍然只生成计划，不自动调用模型。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--workspace <path>` | 当前目录 `.my-agent-real-e2e` | E2E 工作区；命令会把报告和确定性测试证据写到这里。 |
| `--report <path>` | `<workspace>/real_e2e_report.json` | 报告输出路径。 |
| `--artifact <path>` | 可重复 | 额外验收真实任务产物。适合先让模型生成文件，再用机器验收确认。 |
| `--include-real-model` | `false` | 预留真实模型用例标记；当前不会自动发起模型调用。 |
| `--real-task-suite` | `false` | 生成主代理真实任务批量测试计划。报告只放 refs，不内联任务 prompt 或大产物正文。 |
| `--real-task-max-workers <n>` | `4` | 真实任务计划的最大并发工位；当前用于计划分配，不直接启动进程。 |
| `--real-task-timeout <seconds>` | `480` | 真实任务计划的单任务超时秒数；后续受控 runner 会读取同一字段。 |
| `--json` | `false` | 输出完整机器可读 JSON。 |

## `memory-compact`

```powershell
my-agent memory-compact
my-agent memory-compact --session-id sess-xxx --json
my-agent memory-compact --request-id gwreq-xxx --limit 0
my-agent memory-compact --apply
my-agent memory-compact --apply --main-context-bundle-ref ~/.my-agent/memory_archive/snapshots/context_bundles/2026-05-17/example.json
```

预演上下文压缩计划。默认 dry-run 会扫描 `memory/raw`、`memory/hooks`、`memory_archive/snapshots` 和 `memory_archive/tokens`，汇总可压缩线索、权威 snapshot、token ledger、风险提示和下一步建议，不删除、不覆盖、不重写任何归档文件。

`--apply` 当前是非破坏性 apply：会生成 compact context、metadata、apply bundle、restore refs、work state snapshot、self-check、失败报告和 append-only ledger。它只建立恢复入口，不删除、不重写、不裁剪 raw/hook/snapshot/token/task/run 文件。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--dry-run` | `true` | 只生成计划，不修改文件；当前唯一支持模式。 |
| `--apply` | `false` | 生成非破坏性 compact apply 产物；失败时返回非 0 并写 self-check failed 报告。 |
| `--layer <layer>` | `all` | 扫描哪一层归档，可选 `all`、`raw`、`hook`。 |
| `--date <YYYY-MM-DD>` | - | 只扫描某一天。 |
| `--since <time>` | - | 只看此时间之后的归档线索。 |
| `--until <time>` | - | 只看此时间之前的归档线索。 |
| `--session-id <id>` | - | 按 session_id 精确过滤 archive、snapshot 和 token ledger。 |
| `--request-id <id>` | - | 按 request_id 精确过滤 archive 和 snapshot。 |
| `--run-id <id>` | - | 按 run_id 精确过滤 archive 和 snapshot。 |
| `--task-id <id>` | - | 按 task_id 精确过滤 archive 和 snapshot。 |
| `--main-context-bundle-ref <path>` | 自动读取 latest | 显式指定主代理 context bundle；scope 不匹配时仍保留引用但记录 warning。未显式指定时会自动读取 latest，并在 scope 不匹配时跳过绑定，避免串任务。 |
| `--level <0|1|2|3>` | - | 只扫描指定 `archive_level` 的 raw/hook 记录。 |
| `--limit <n>` | `50` | 最多纳入多少条归档记录；`0` 表示不限。 |
| `--json` | `false` | 输出机器可读 JSON。 |

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

## `local-doctor`

```powershell
my-agent local-doctor
my-agent local-doctor --json
my-agent local-doctor --repair
```

诊断本地事实源和文件账本是否一致：SQLite 是否可打开、记忆 JSONL 是否已索引、LocalStore 正文文件是否缺失、gateway processing 是否超时、subagent 工单是否缺关键文件。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--json` | `false` | 输出机器可读 JSON。 |
| `--repair` | `false` | 处理超过 `gateway_processing_timeout_seconds` 的 processing 请求：未超尝试次数则退回 pending，超过则写 failed response 并归档。 |
| `--limit <n>` | `20` | 每类问题最多显示多少条。 |

## `local-rebuild`

```powershell
my-agent local-rebuild
my-agent local-rebuild --source memory
my-agent local-rebuild --source gateway --source subagent
my-agent local-rebuild --reset
```

从磁盘事实源重建 LocalStore 索引。默认重建 `memory`、`gateway`、`subagent` 和 `fts`。`--reset` 会先清空 LocalStore 的 records/events/FTS，再从 JSONL、gateway 队列/响应、subagent 工单目录重新索引；它不会删除原始 memory、gateway、subagent 文件。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--source <name>` | `all` | 只重建指定来源，可多次传入；可选 `all`、`memory`、`gateway`、`subagent`、`fts`。 |
| `--reset` | `false` | 先清空 LocalStore records/events/FTS 再重建。 |

## `logs`

```powershell
my-agent logs status
my-agent logs status --json
my-agent logs ingest validation/security_fixtures/security_alert_v1.jsonl --source-id fixture --format jsonl
my-agent logs query --start-time 2026-04-30T00:00:00Z --end-time 2026-04-30T23:59:59Z --attacker-ip 198.51.100.23
my-agent logs hunt-ip 198.51.100.23 --start-time 2026-04-30T00:00:00Z --end-time 2026-04-30T23:59:59Z
my-agent logs trace-case case-1 --start-time 2026-04-30T00:00:00Z --end-time 2026-04-30T23:59:59Z
```

`logs` 是 log analysis 的最小 CLI 面，适合本地轻量第一响应 replay 和受控查询，不是生产 SIEM。最小闭环是：先 `status` 确认配置和 data dir，再 `ingest` 导入 SecurityAlertV1 JSONL/CSV，接着用 `query` 查时间窗内事件，用 `hunt-ip` 围绕 IP 扩展，用 `trace-case` 从 case seed 生成 evidence。

`status` 只读取 `agent_py_agent/config/log_analysis_config.yaml` 并显示 disabled/enabled、capability level、data dir 和 warnings，不启动 worker。`ingest` 写入配置里的 `data_dir`，除非传 `--root`。`query`、`hunt-ip`、`trace-case` 读取同一目录；`--limit` 未传时使用 `query_default_limit`，超过 `query_max_limit` 时会截断并在输出里显示 warning。

自然语言路径也可用：在 `run`、`chat` 或 gateway 里提出明显安全日志任务，例如“分析这批 security logs”“追一下 WAF 日志里的攻击 IP”“帮我看安全日志里的可疑登录”。普通聊天和普通开发任务不会暴露安全工具；只有显式 grant `logs/security`，或运行时识别到 security log / audit log / firewall log / WAF log / 安全日志 / 审计日志等明确任务时，才会自动授权 `security_query`、`security_hunt_ip`、`security_trace_case`。

离线 Live Lab replay：

```powershell
python scripts\live_agent_lab.py --suite log-analysis
python scripts\live_lab\log_analysis_replay.py --output-root %TEMP%\通道运行时-log-replay
```

这条 replay 不调用真实 LLM 或网络；新输出目录下预期摘要包含 `ok=true`、`dry_run=true`、`total_events=3`、`stored_events=3`、`case_count=1`，并打印 `case_path`、`route_path`、`report_path`、`evidence_paths`。

| 子命令 | 参数 | 说明 |
| --- | --- | --- |
| `status` | `--json` | 输出机器可读 JSON。 |
| `ingest` | `file` | 必填，待导入的本地日志文件。 |
| `ingest` | `--root <path>` | 覆盖 log analysis 数据目录；未传时使用配置 `data_dir`。 |
| `ingest` | `--source-id <id>` | 导入源 ID，用于 checkpoint、manifest 和去重。 |
| `ingest` | `--format <jsonl|json|csv|log>` | 覆盖输入格式；未传时按文件后缀推断。 |
| `ingest` | `--json` | 输出机器可读 JSON。 |
| `query` | `--root <path>` | 覆盖 log analysis 数据目录。 |
| `query` | `--start-time <iso>` | 查询起始时间；受控安全查询建议始终传时间窗。 |
| `query` | `--end-time <iso>` | 查询结束时间。 |
| `query` | `--attacker-ip <ip>` | 按攻击方或源 IP 过滤。 |
| `query` | `--victim-ip <ip>` | 按受害方或目的 IP 过滤。 |
| `query` | `--domain <name>` | 按 domain/host/SNI/DNS query 过滤。 |
| `query` | `--uri <path>` | 按 URI/URL/path/API 过滤。 |
| `query` | `--alert-type <type>` | 按告警类型过滤。 |
| `query` | `--limit <n>` | 覆盖查询返回上限。 |
| `query` | `--json` | 输出机器可读 JSON。 |
| `hunt-ip` | `ip` | 必填，围绕一个 IP 同时查 attacker/victim 角色。 |
| `hunt-ip` | `--role <any|attacker|victim>` | 限制 IP 角色；默认 `any`。 |
| `hunt-ip` | `--root`, `--start-time`, `--end-time`, `--limit`, `--json` | 与 `query` 同义。 |
| `trace-case` | `case_id` | 必填，从 case seed 扩展查询。 |
| `trace-case` | `--root`, `--start-time`, `--end-time`, `--limit`, `--json` | 与 `query` 同义。 |

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
| `--resume-context` | 配置值 | 本次 chat 会话临时启用恢复上下文注入。 |
| `--no-resume-context` | 配置值 | 本次 chat 会话临时关闭恢复上下文注入。 |

chat 内部命令仍在本地处理，例如 `/memory`、`/remember`、`/subagents`。普通自然语言消息才会进入模型；在 `--gateway` 模式下，这些普通消息会走 gateway request/response。

chat 和 gateway 都复用 `SimpleAgent.run()` 的恢复上下文能力。也就是说，只有当主配置显式打开 `memory_resume_auto_context_enabled` 时，普通消息才会在恢复触发词场景里自动注入 `Recovery Brief`；默认不查、不注入。

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
my-agent spawn-subagents "真实层级 E2E 主节点任务" --count 1 --role coordinator --agent-name root-coordinator
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `goal` | - | 必填，要拆分的目标。 |
| `--count <n>` | `3` | 子代理数量。 |
| `--role <role>` | `worker` | 显式创建角色。普通拆分保持 `worker`；真实层级 E2E 可用 `coordinator` 创建只负责调度下层的 root。 |
| `--agent-name <name>` | `""` | 显式 agent 名称，常用于 root/coordinator E2E，例如 `root-coordinator`。 |

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

输出会包含 `Shared Progress`、`Takeover View`、`Acceptance Plan` 和 `Acceptance Next Action`：前者显示 root task 聚合计数，接管视图显示恢复入口 refs 和推荐读取顺序，验收计划显示父级 dry-run 决策，下一动作区块显示建议命令、原因、refs 和 `mutates_task_state`。完整 artifact 正文不会自动进入看板，也不会因为展示验收计划或下一动作建议而执行 tests、apply 或 rescue。

## `subagents-due-check`

```powershell
my-agent subagents-due-check --limit 20
my-agent subagents-due-check --root-id <root_run_id> --all
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--root-id <root_run_id>` | 空 | 只巡检指定 root subagent 任务树；适合真实 E2E 多棵树共用一个 workspace 时降噪。 |
| `--all` | `false` | 显示全部问题，而不是按 limit 截断。 |
| `--limit <n>` | `20` | 最多显示多少条问题。 |

## `subagents-budget`

```powershell
my-agent subagents-budget
my-agent subagents-budget --root-id <root_run_id> --max-model-calls 80 --max-tool-rounds 200
my-agent subagents-budget --root-id <root_run_id> --max-prompt-response-tokens 500000 --include-dry-runs --json
```

这个命令只读取 runner 结果引用和 prompt/response 文件大小，用来估算一棵真实 E2E 树的模型调用、工具轮数和粗略 token 预算。它不会展开 artifact 正文，也不会改变任务状态；适合在 3、5、10、1/4/16/48 这类多代理压测后快速确认调用没有失控。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--root-id <root_run_id>` | 空 | 只统计指定 root subagent 任务树；为空时统计当前 workspace 里所有可读 runner 记录。 |
| `--max-model-calls <n>` | `0` | 模型调用数阈值；`0` 表示不检查。超过时写入 `exceeded`，但不阻断执行。 |
| `--max-tool-rounds <n>` | `0` | 工具轮数阈值；`0` 表示不检查。 |
| `--max-prompt-response-tokens <n>` | `0` | prompt+response 粗略 token 阈值；按文件字节数估算，`0` 表示不检查。 |
| `--include-dry-runs` | `false` | 把 dry-run prompt 也计入预算记录；默认只看真实 runner 结果。 |
| `--json` | `false` | 输出机器可读 JSON。 |

输出位置：

```text
agent_py_agent/data/subagents/subagent_run_budget.json
agent_py_agent/data/subagents/SUBAGENT_RUN_BUDGET.md
```

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
my-agent subagents-plan-actions --root-id <root_run_id> --all
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--all` | `false` | 显示全部动作，而不是按 limit 截断。 |
| `--limit <n>` | `20` | 最多显示多少条动作。 |
| `--root-id <id>` | - | 只为指定 root subagent 任务树生成动作计划，适合多棵真实 E2E 树共用 workspace 时降噪。 |

## `subagents-apply-actions`

```powershell
my-agent subagents-apply-actions --dry-run
my-agent subagents-apply-actions --apply --action reopen_for_evidence
my-agent subagents-apply-actions --apply --action recover_coordinator_leadership --run-id <stale_root> --take-over-by <leader_run_id>
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--dry-run` | 默认模式 | 只预览动作，不修改记录。 |
| `--apply` | `false` | 真正执行低风险动作。 |
| `--action <name>` | - | 只处理指定动作，如 `reopen_for_evidence`。 |
| `--run-id <id>` | - | 只处理指定子代理运行 ID，可多次传入。 |
| `--limit <n>` | `20` | 最多处理多少条动作。 |
| `--take-over-by <name>` | - | 接管/领导权恢复动作的接管者；`recover_coordinator_leadership` 要求这里是现有 leader run ID，并会把旧 coordinator 的直接子任务重挂到该 leader。 |
| `--locked-file <path>` | - | 接管时锁定的文件，可多次传入。 |

## `subagents-workflow-plan`

```powershell
my-agent subagents-workflow-plan "开发一个可验收的功能"
my-agent subagents-workflow-plan "开发一个可验收的功能" --template-id single_worker_verified --output-dir .agent/workflow-previews --json
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `goal` | - | 必填，待路由的父任务目标。 |
| `--template-id <id>` | - | 强制使用指定 workflow 模板做预览。 |
| `--task-type <id>` | - | 结构化任务类型，例如 `code_or_bugfix` 或 `quality_deliverable`；用于模板路由，不从 goal 自然语言猜。 |
| `--risk-tags <tags>` | - | 逗号分隔的结构化风险标签，写入 dry-run 预览供审计。 |
| `--output-dir <path>` | - | 显式写出 JSON / Markdown dry-run 预览，不创建 subagent。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `subagents-leadership-recovery-plan`

```powershell
my-agent subagents-leadership-recovery-plan --root-id <root_run_id> --leader <leader_run_id>
my-agent subagents-leadership-recovery-plan --root-id <root_run_id> --leader <leader_a> --leader <leader_b> --max-children-per-leader 3 --json
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定 heartbeat timeout 等巡检阈值。 |
| `--root-id <id>` | - | 必填，只规划这一棵 root subagent 任务树。 |
| `--leader <id>` | - | 必填，候选新 leader run ID；可多次传入。 |
| `--max-children-per-leader <n>` | `3` | 每个 leader 最多接多少个直接孩子；`0` 表示不限制。 |
| `--json` | `false` | 输出机器可读 JSON。 |

该命令只做 dry-run：它会读取 due-check 里的 `coordinator_heartbeat_stale` 问题，把失联 coordinator 的直接孩子按容量分给候选 leader，并写出 `subagent_leadership_recovery_plan.json` / `SUBAGENT_LEADERSHIP_RECOVERY_PLAN.md`。当前不会改 `parent_id`、不会标记旧 coordinator，也不会自动执行 future command；真正分批 apply 入口是后续阶段。

## `subagents-leadership-recovery-apply`

```powershell
my-agent subagents-leadership-recovery-apply --coordinator <old_coord> --leader <new_leader> --child-run-id <child>
my-agent subagents-leadership-recovery-apply --apply --root-id <root_run_id> --coordinator <old_coord> --leader <new_leader> --child-run-id <child_a> --child-run-id <child_b>
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--dry-run` | 默认模式 | 只预览，不修改任务树。 |
| `--apply` | `false` | 真正重挂指定 child 子树。 |
| `--root-id <id>` | - | 可选，限制 coordinator / leader / child 必须属于同一 root。 |
| `--coordinator <id>` | - | 必填，旧 coordinator run ID。 |
| `--leader <id>` | - | 必填，新 leader run ID，必须存在且不能是失败/超时/断通道状态。 |
| `--child-run-id <id>` | - | 必填，要移动的直接 child run ID；可多次传入。 |
| `--max-children-per-leader <n>` | `0` | 可选容量上限；非 0 时会阻断超过 leader 直接 child 容量的 apply。 |
| `--json` | `false` | 输出机器可读 JSON。 |

安全边界：只移动 `--child-run-id` 明确列出的直接孩子；如果 child 已经不在旧 coordinator 下、root 不匹配、leader 不健康或容量超限，整次 apply 会被阻断，不会产生部分移动。移动成功后会递归刷新后代 depth；旧 coordinator 没有剩余孩子时才标记为 `TAKEN_OVER`。

## `subagents-hierarchy`

```powershell
my-agent subagents-hierarchy <run_id> --child reporter:reporter-a:"write report"
my-agent subagents-hierarchy <run_id> --child checker:checker-a:"check report" --apply --json
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `run_id` | - | 必填，父级 subagent 运行 ID。 |
| `--child <role:agent:goal>` | - | 待创建子任务，格式 `ROLE:AGENT_NAME:GOAL`；可多次传入。 |
| `--apply` | `false` | 真正创建 child runs；默认只预览。 |
| `--requested-by <name>` | `parent` | 调度请求来源，用于审计摘要。 |
| `--max-children <n>` | `0` | 父级最多 child 数；0 表示不限制。 |
| `--max-depth <n>` | `2` | 允许创建的最大层级深度。 |
| `--json` | `false` | 输出机器可读 JSON。 |

## `subagents-recovery-tree`

```powershell
my-agent subagents-recovery-tree <run_id>
my-agent subagents-recovery-tree <run_id> --hide-healthy --json
my-agent subagents-recovery-tree <run_id> --capability-config config/capability_config.yaml
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `run_id` | - | 必填，根 subagent 运行 ID。 |
| `--capability-config <path>` | `config/capability_config.yaml` | 提供 heartbeat/run timeout 阈值，用于把 stale `RUNNING` 后代列入恢复候选。 |
| `--hide-healthy` | `false` | 只展示 root 和需要恢复的节点，减少上下文体积。 |
| `--requested-by <name>` | `parent` | 查询请求来源，用于审计摘要。 |
| `--max-nodes <n>` | `200` | 最多扫描多少个子树节点。 |
| `--json` | `false` | 输出机器可读 JSON。 |

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
my-agent subagents-acceptance --execute-tests --test-timeout 120
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--dry-run` | 默认模式 | 只生成验收报告，不修改记录。 |
| `--apply` | `false` | 验收通过时标记 `DONE/VERIFIED`，失败时标记 `BLOCKED/FAILED`。 |
| `--run-id <id>` | - | 只验收指定子代理运行 ID，可多次传入。 |
| `--limit <n>` | `20` | 最多处理多少条记录。 |
| `--reviewer <name>` | `parent` | 验收者标识。 |
| `--note <text>` | - | 写入验收记录的备注。 |
| `--execute-tests` | 配置值 | 本次验收显式执行 `output.json.tests`，覆盖 `acceptance_execute_tests`。 |
| `--no-execute-tests` | 配置值 | 本次验收显式不执行 tests，覆盖配置默认值。 |
| `--test-timeout <seconds>` | `acceptance_test_timeout_seconds` | 本次真实执行 tests 的单条测试超时秒数。 |

## `subagents-acceptance-plan`

```powershell
my-agent subagents-acceptance-plan <run_id>
my-agent subagents-acceptance-plan <run_id> --json
my-agent subagents-acceptance-plan <run_id> --write
my-agent subagents-acceptance-plan <run_id> --apply
my-agent subagents-acceptance-plan <run_id> --next-action
my-agent subagents-acceptance-plan <run_id> --auto-policy
my-agent subagents-acceptance-plan <run_id> --auto-execution
my-agent subagents-acceptance-plan <run_id> --auto-execution --execute-auto-tests
my-agent subagents-acceptance-plan <run_id> --followup
my-agent subagents-acceptance-plan <run_id> --apply-followup
```

只读取该 run 的 `output.json`、`reports/test_execution.json` 和 handoff/readiness refs，展示父级下一步 dry-run 决策。输出可能是 `execute_tests`、`inspect_only`、`request_human` 或 `rescue`；默认不会执行 tests、不会读取 artifact 正文、不会写回 task 状态。显式传 `--write` 时会写入 `reports/parent_acceptance_decision.json` 审计文件，但这仍然不是 apply。

显式传 `--apply` 时会先写入 `parent_acceptance_decision.json`，再写入 `parent_acceptance_apply.json`。当前第一版只允许 `inspect_only` 进入既有 `acceptance_review` apply 路径；`execute_tests`、`request_human` 和 `rescue` 会被拦截为未应用，并在 apply 审计文件里记录下一步需要显式执行测试、人工确认或救援接管。`--apply` 不会自动跑 tests，也不会自动 rescue。

显式传 `--next-action` 时只生成父/上级代理可读的下一步建议，例如 `run_tests`、`request_human_confirmation`、`plan_rescue` 或 `apply_acceptance`。它会展示建议命令和 `parent_acceptance_decision.json` / `parent_acceptance_apply.json` refs，但不会执行建议命令、不会写 task 状态。

显式传 `--auto-policy` 时会读取 next-action，写入 `reports/parent_acceptance_auto_policy.json`，并展示策略判断。第一版固定 dry-run：`run_tests` 可被标记为 `allow` / `would_execute=true`，但 `executed=false`；`request_human_confirmation`、`plan_rescue`、`apply_acceptance` 等不会自动执行。半自动计划会额外展示 `execution_mode=manual_only`、`automatic_execution_allowed=false`、`recommended_command` 和 `preflight_status`，意思是“这条命令可以给人或后续受控调度器参考，但当前代码不会自己运行”。`ready_for_automatic_execution` 第一版固定 false。

显式传 `--auto-execution` 时会读取 auto-policy，写入 `reports/parent_acceptance_auto_execution.json`，并展示自动执行 dry-run facade。默认固定 `execution_allowed=false`、`guard_status=blocked`、`executed=false`，只展示 recommended command、blockers 和 hard guard，不启动命令、不修改 task 状态。只有同时显式传 `--execute-auto-tests` 时，才会把 auto-policy 的 `run_tests` 建议转换为一次手动确认的测试执行，写入 `reports/test_execution.json/md`；随后会写 `reports/parent_acceptance_auto_followup.json`，把测试后的下一步归类为人工 apply、人工 rescue、人工确认或继续补测试。follow-up 仍只是审计和建议，不 apply、不 rescue、不修改 task 状态。

显式传 `--followup` 时只读取 `parent_acceptance_auto_followup.json` 并展示受控下一步命令。显式传 `--apply-followup` 时才进入人工确认入口：`ready_for_manual_apply` 会复用父级 `inspect_only` apply 桥接；`needs_manual_rescue` 必须提供 `--take-over-by`，并复用 `takeover_or_reassign` action handler 的通道检查、接管审计和 readiness refs。坏 JSON、run_id 不匹配、测试报告引用不一致或过期测试报告都会被阻断。它不会因为 follow-up 存在就自动执行。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `run_id` | - | 子代理运行 ID。 |
| `--json` | `false` | 输出机器可读 JSON，仍保持 refs-only。 |
| `--write` | `false` | 写入 refs-only 父级验收决策审计文件，不执行决策。 |
| `--apply` | `false` | 显式应用低风险 `inspect_only` 决策；其它决策只写入拦截审计，不改 task。 |
| `--next-action` | `false` | 查看父/上级代理下一步显式动作建议，不执行动作。 |
| `--auto-policy` | `false` | 查看并写入父级自动策略 dry-run 审计，不执行动作。 |
| `--auto-execution` | `false` | 查看并写入父级自动执行 dry-run facade 审计，不执行动作。 |
| `--execute-auto-tests` | `false` | 只能配合 `--auto-execution` 使用；显式确认执行 auto-policy 允许的 `run_tests`，写测试报告但不 apply。 |
| `--followup` | `false` | 查看测试后的 follow-up 下一步建议，不执行动作。 |
| `--apply-followup` | `false` | 显式处理 follow-up：测试通过时 apply，测试失败时走受控接管入口。 |
| `--take-over-by <name>` | `""` | `--apply-followup` 处理 rescue 时必填；复用 action apply 的 takeover 门。 |
| `--locked-file <path>` | - | follow-up rescue 接管时锁定的文件，可多次传入。 |
| `--reviewer <name>` | `parent` | `--apply` 进入普通验收路径时写入的 reviewer。 |
| `--note <text>` | `""` | `--apply` 进入普通验收路径时写入的备注。 |

## `subagents-tests`

```powershell
my-agent subagents-tests <run_id>
my-agent subagents-tests <run_id> --re-run --timeout 120
```

默认只读取并展示该 run 的 `reports/test_execution.json` 摘要，不会重新执行测试，也不会读取任何 artifact 正文。显式传 `--re-run` 后，命令会按 `output.json` 里的 `tests` 执行 allowlist 内验证，并写回 `test_execution.json` 和 `test_execution.md`。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `run_id` | - | 子代理运行 ID。 |
| `--re-run` | `false` | 显式重新执行 `output.json.tests` 并写入真实执行报告。 |
| `--timeout <seconds>` | `120` | 重跑时单条测试的超时秒数。 |

## `subagents-patches`

```powershell
my-agent subagents-patches --dry-run
my-agent subagents-patches --review-apply --reviewer parent
my-agent subagents-patches --apply-dry-run --run-id <run_id>
my-agent subagents-patches --apply --run-id <run_id> --reviewer parent
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--dry-run` | 默认模式 | 只生成 patch 审核报告，不修改记录。 |
| `--review-apply` | `false` | 写回 patch 审核状态，但不真正 apply 文件。 |
| `--apply-dry-run` | `false` | 展示将要 apply 的 diff，不真正写文件。 |
| `--apply` | `false` | 真正 apply `write_file` patch、跑 allowlist 测试并记录审计日志。 |
| `--run-id <id>` | - | 只审核指定子代理运行 ID，可多次传入。 |
| `--limit <n>` | `20` | 最多处理多少条记录。 |
| `--reviewer <name>` | `parent` | 审核者标识。 |
| `--note <text>` | - | 写入 patch 审核记录的备注。 |

## `subagents-memory-gate`

```powershell
my-agent subagents-memory-gate <run_id>
my-agent subagents-memory-gate <run_id> --candidate-id <candidate_id> --decision approve_memory --reviewer parent
my-agent subagents-memory-gate <run_id> --export-memory --candidate-id <candidate_id>
my-agent subagents-memory-gate <run_id> --export-skill --candidate-id <candidate_id>
my-agent subagents-memory-gate <run_id> --retention-dry-run
my-agent subagents-memory-gate <run_id> --retention-apply
my-agent subagents-memory-gate <run_id> --verify
```

这个命令默认只处理 `tasks/<root_id>/agents/<run_id>/memory_gate/` 里的候选 review 状态。显式传 `--export-memory` 才会把 `approve_memory` 候选写入主代理长期 memory JSONL；显式传 `--export-skill` 才会把 `approve_skill` 候选写成本地 skill draft。`--retention-apply` 只压缩 active review queue，不删除候选、decision 或 export 审计日志。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `run_id` | - | 子代理运行 ID。 |
| `--retention-dry-run` | `false` | 只生成 retention 清理计划和 `retention_report.json`。 |
| `--retention-apply` | `false` | 应用 retention：从 active review queue 移除 closed 候选，但保留审计日志。 |
| `--export-memory` | `false` | 把已 `approve_memory` 的候选显式导出到主 memory JSONL。 |
| `--export-skill` | `false` | 把已 `approve_skill` 的候选显式导出为 skill draft，不安装正式 skill。 |
| `--verify` | `false` | 写 `verifier_report.json`，检查 gate/export 没有自动提升或边界破坏。 |
| `--candidate-id <id>` | - | 要写回 review decision 的候选 ID；不传时只列出候选。 |
| `--decision <value>` | `needs_evidence` | 可选 `approve_memory`、`approve_skill`、`reject`、`needs_evidence`。 |
| `--reviewer <name>` | `parent` | review 者标识。 |
| `--note <text>` | - | 写入 `decisions.jsonl` 的备注。 |
| `--memory-path <path>` | 当前 agent memory | `--export-memory` 的目标 JSONL。 |
| `--skill-output-dir <path>` | run-local `memory_gate/skill_drafts` | `--export-skill` 的草稿输出目录。 |
| `--limit <n>` | `20` | 列表模式最多显示多少条候选。 |

## `subagents-dispatch`

```powershell
my-agent subagents-dispatch
my-agent subagents-dispatch --apply
my-agent subagents-dispatch --apply --execute-runners
my-agent subagents-dispatch --execute-acceptance-tests --max-runners 0
my-agent subagents-dispatch --watch --planner --interval 30
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--capability-config <path>` | `agent_py_agent/config/capability_config.yaml` | 指定能力路由配置。 |
| `--dry-run` | 默认模式 | 只生成调度报告，不修改记录。 |
| `--apply` | `false` | 执行低风险调度动作并写审计日志。 |
| `--execute-runners` | `false` | 配合 `--apply` 调用真实模型执行 runner；不能单独使用。 |
| `--execute-acceptance-tests` | `false` | 显式执行父级验收 auto-policy 允许的 `run_tests`，写 `test_execution.json/md`，但不 apply、不 rescue、不修改 task 状态。 |
| `--planner` | `false` | 有 active/pending/stalled/needs-intervention 事项时调用父代理 LLM planner；如果模型只回 `HEARTBEAT_OK`，会被 gate 标记为失败。 |
| `--workflow-mode <off\|plan\|auto>` | `off` | dispatch 前对父任务执行 workflow 规划；`plan` 只写计划，`auto` 还会自动派出 workflow worker 子工单。 |
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

dispatch 输出位置：

```text
agent_py_agent/data/subagents/subagent_dispatch_report.json
agent_py_agent/data/subagents/SUBAGENT_DISPATCH.md
```

当本轮 dispatch 处理 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE` 的 run 时，acceptance 记录会带 parent acceptance auto-policy 的 refs-only 摘要：`parent_acceptance_policy_ref`、decision、action、would_execute 和 executed。默认只写 `reports/parent_acceptance_auto_policy.json` / `parent_acceptance_auto_execution.json` 审计并展示引用，不执行 tests、不 apply、不 rescue、不修改 task 状态。只有显式传 `--execute-acceptance-tests` 时，dispatch/watch 才会执行 auto-policy 允许的 `run_tests` 并写测试报告和 follow-up；任务仍停留在等待验收状态，后续必须另走 `subagents-acceptance-plan <run_id> --apply-followup` 或带 `--take-over-by` 的 rescue 入口。

watch 输出位置：

```text
agent_py_agent/data/subagents/subagent_dispatch_watch_heartbeat.json
agent_py_agent/data/subagents/subagent_dispatch_watch_report.json
agent_py_agent/data/subagents/SUBAGENT_DISPATCH_WATCH.md
agent_py_agent/data/subagents/subagent_dispatch_watch_log.jsonl
agent_py_agent/data/subagents/DISPATCH_WATCH_LOG.md
```

watch 模式不会在 watch 层重新运行 auto-policy；watch record 的 evidence 只指向本轮 dispatch JSON/Markdown，后续沿 dispatch record 里的 ref 查看单个 run 的 policy audit。

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
# 子代理默认像“不同记忆/权限的主代理”一样工作。
# 默认只保留少量用户能理解的入口，细节由系统和 LLM 判断。
enable_subagents: true
subagent_mode: "trusted_local_hardening"
max_subagents: 1000
subagent_workspace: "data/subagents"
subagent_role_template_dirs: []
subagent_debug_trace_level: 0

# 父级验收默认不直接执行命令；需要真实跑测试时再显式打开。
acceptance_execute_tests: false
acceptance_test_timeout_seconds: 120

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
my-agent scenario-test --case gateway-cross-day-resume
my-agent scenario-test --case gateway-delayed-response
my-agent scenario-test --case gateway-multi-worker
my-agent scenario-test --case gateway-stale-lease
my-agent scenario-test --case gateway-processing-stop
my-agent scenario-test --case parent-subagent-cross-day-resume
my-agent scenario-test --case real-model-recovery
my-agent scenario-test --case real-model-recovery-multi-round
my-agent scenario-test --case structured-repair
my-agent scenario-test --case runner-retry
my-agent scenario-test --case all --count 1
my-agent scenario-test --direct
my-agent scenario-test --dry-run
my-agent scenario-test --direct --count 10 --max-runners 10 --runner-concurrency 5 --runner-start-rate 10
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
| `gateway-cross-day-resume` | 启动真实后台 gateway，投递一次 ask，再模拟跨天线索，确认 `memory-resume` 能读回 request/response JSON | 取决于配置；echo 后端不调用 |
| `gateway-delayed-response` | 模拟 response 已经落盘但 pending 请求副本迟到，确认 worker 只归档请求、不重复调用模型 | 否 |
| `gateway-multi-worker` | 启动两个 request worker 并发抢占多条 pending 请求，确认每条只完成一次、不重复归档 | 否 |
| `gateway-stale-lease` | 模拟 worker 中断留下的旧 `processing` lease，确认恢复会重排并由 worker 完成请求 | 否 |
| `gateway-processing-stop` | 模拟 worker 已领任务并正在调模型时 gateway stop/restart，确认重启后不卡死、不丢请求、不留半截 JSON | 否 |
| `parent-subagent-cross-day-resume` | 跑一次真实 subagent runner 工具回合，模拟跨天线索，确认 `memory-resume` 能回到 task fact sources | 否 |
| `real-model-recovery` | 用真实 API 跑一次 subagent，验证 `memory-resume` 能找回真实模型响应内容 | 是 |
| `real-model-recovery-multi-round` | 用真实 API 跑多轮工具调用（read_file + search_text），验证 `memory-resume` 能找回每轮 evidence 和 output.json | 是 |
| `structured-repair` | 模拟 runner 输出损坏的 `[SUBAGENT_RESULT]`，确认修复回合补齐 JSON 并通过验收 | 否 |
| `runner-retry` | 模拟 runner 第一次模型调用失败，确认下一轮 dispatch 会有限重试并完成验收 | 否 |
| `all` | 依次跑 `verification`、`gateway-restart`、`gateway-cross-day-resume`、`gateway-delayed-response`、`gateway-multi-worker`、`gateway-stale-lease`、`gateway-processing-stop`、`parent-subagent-cross-day-resume`、`real-model-recovery-multi-round`、`structured-repair`、`runner-retry`、`happy` | `happy` 和 multi-round 会调用 |

| 参数 | 说明 |
| --- | --- |
| `--capability-config <path>` | 能力路由配置文件路径，默认使用 `config/capability_config.yaml`。 |
| `--case <name>` | 场景类型，默认 `happy`。 |
| `--workspace <path>` | 保存场景测试结果的父目录；不传则使用系统临时目录。 |
| `--count <n>` | 本场景创建多少个子代理，默认 `2`。 |
| `--max-runners <n>` | 每轮最多推进多少个 runner，默认 `2`。 |
| `--max-cycles <n>` | 最多执行多少轮 dispatch，默认 `3`。 |
| `--timeout <seconds>` | gateway ask 等待响应的秒数，默认 `300`。 |
| `--runner-concurrency <value>` | 仅本次场景测试覆盖 `runner_concurrency`，用于真实并发压测；例如 `5`。不传则走真实配置。 |
| `--runner-start-rate <value>` | 仅本次场景测试覆盖 `runner_start_rate`，用于限制本轮最多启动多少 runner；例如 `10`。不传则走真实配置。 |
| `--model-request-timeout <seconds>` | 仅本次场景测试覆盖模型 `request_timeout`，用于压测慢模型或快速暴露超时恢复。 |
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

`gateway` 第一版是本地后台控制面。它会启动一个后台 Python 进程，在内部按配置运行现有 daemon/watch 调度，并把 pid、state、heartbeat、stop request、请求队列、响应和日志写到 `gateway_workspace`。当前 request worker pool 已有保守第一版，默认 1 个 worker；runner 并发也只在显式配置 `runner_concurrency` 为数字时启用。它还不是多机器组织 gateway。

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
| `supervisor-start` | 启动看门狗进程，自动监控并重启崩溃的 gateway。 |
| `supervisor-stop` | 停止看门狗进程。 |
| `supervisor-status` | 查看 supervisor 和 gateway 状态。 |
| `start-all` | 一键启动 gateway（带 supervisor）+ 所有适配器。 |
| `install` | 安装系统服务（Linux systemd 或 macOS launchd），开机自启。 |
| `uninstall` | 卸载系统服务。 |

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
| `ask` | `--resume-context` | 本次 gateway 请求临时启用恢复上下文注入。 |
| `ask` | `--no-resume-context` | 本次 gateway 请求临时关闭恢复上下文注入。 |
| `result` | `--show-prompt` | 打印响应 JSON 中保存的最终 prompt。 |
| `result` | `--json` | 输出完整响应 JSON。 |
| `run` | daemon 同名参数 | 内部调试用，支持 `--max-cycles 1 --interval 0 --no-planner` 这类安全验证。 |

### 请求文件流转

当前实现以文件队列为主事实源，方便跨平台排查和恢复。gateway 运行时另有可选本机 HTTP 控制服务骨架；它不替代 `requests/*` 和 `responses/*` 文件事实源，也还不是完整 WebSocket / 多租户远端 gateway。

```text
ask 写入 pending -> gateway worker 加 lease 并移到 processing -> 模型处理 -> 写 responses -> 原请求移到 done/failed
```

如果 gateway 意外退出，重启时会把 `processing` 里没处理完且未超尝试次数的请求退回 `pending`。运行中如果 processing 超过 `gateway_processing_timeout_seconds`，也会按 `gateway_request_max_attempts` 自动重排或归档到 `failed`。

gateway 控制面配置：

```yaml
gateway_workspace: "data/gateway"
gateway_heartbeat_interval: 5
gateway_stale_seconds: 120
gateway_stop_timeout: 20
gateway_request_timeout: 300
gateway_request_poll_interval: 1
gateway_request_workers: 1
gateway_processing_timeout_seconds: 900
gateway_request_max_attempts: 2
```

恢复上下文自动注入配置：

```yaml
memory_resume_auto_context_enabled: false
memory_resume_auto_context_mode: "trigger"  # off / trigger / always
memory_resume_auto_context_limit: 5         # 1-50
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
agent_py_agent/data/gateway/requests/failed/<request_id>.json
agent_py_agent/data/gateway/responses/<request_id>.json
agent_py_agent/data/gateway/gateway_requests.jsonl
```

## `adapter`

当前提供两类入口：文件协议 adapter，以及 QQ/飞书通道 adapter 的启动、状态和停止命令。文件协议仍是最容易接入外部工具或 TUI 的稳定最小协议。

```powershell
my-agent adapter file
my-agent adapter file --watch
my-agent adapter file --root /tmp/my-agent-adapter
my-agent adapter start --channel feishu --daemon
my-agent adapter start --channel qq
my-agent adapter status
my-agent adapter stop
```

外部聊天工具或 TUI 可以把消息 JSON 写进 adapter inbox，adapter 会投递到 gateway，再把响应写到 outbox。

输入 JSON 支持这些字段：

```json
{
  "id": "msg-1",
  "conversation_id": "conv-1",
  "user": "user-1",
  "text": "帮我看一下当前任务状态",
  "no_save": false
}
```

默认目录：

```text
agent_py_agent/data/adapters/file/inbox/<message_id>.json
agent_py_agent/data/adapters/file/processing/<message_id>.json
agent_py_agent/data/adapters/file/done/<message_id>.json
agent_py_agent/data/adapters/file/failed/<message_id>.json
agent_py_agent/data/adapters/file/outbox/<message_id>.json
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--root <path>` | `adapter_workspace` | 适配器根目录。 |
| `--inbox <path>` | `<root>/inbox` | 覆盖 inbox 目录。 |
| `--outbox <path>` | `<root>/outbox` | 覆盖 outbox 目录。 |
| `--watch` | `false` | 持续轮询 inbox。 |
| `--once` | `false` | 只处理当前已有消息后退出。 |
| `--poll-interval <seconds>` | `1.0` | watch 模式轮询间隔。 |
| `--limit <n>` | `20` | 每轮最多处理多少条消息，`0` 表示不限制。 |
| `--timeout <seconds>` | `gateway_request_timeout` | 等待 gateway 响应的秒数。 |
| `--no-start-gateway` | `false` | 不自动启动 gateway，未运行时直接失败。 |

通道 adapter 子命令：

| 子命令 | 说明 |
| --- | --- |
| `start --channel <name>` | 启动指定通道适配器，支持 `feishu` / `qq` / `all`。 |
| `start --daemon` | 后台守护进程模式运行，写入 PID 文件。 |
| `start --pid-file <path>` | 指定 PID 文件路径。 |
| `status` | 查看通道适配器状态。 |
| `stop` | 停止所有通道适配器。 |

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

## `bench-model`

```powershell
my-agent bench-model
my-agent bench-model --show
my-agent bench-model --profile /path/to/profile.json
```

运行模型速度基准测试或查看已有速度模型。速度模型用于动态计算 runner 超时时间。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--show` | `false` | 只查看已有速度模型，不运行测试。 |
| `--profile <path>` | `data/model_speed_profile.json` | 速度模型文件路径。 |

示例输出：

```
Backend: anthropic_compatible
Model: claude-sonnet-4-20250514
Tested at: 2026-05-02T10:00:00Z
Interpolation: log_linear

Samples:
  Input  1000,  Output  500:  10.0s (150 tok/s)
  Input  5000,  Output  500:  20.0s (275 tok/s)
  Input 10000,  Output  500:  35.0s (300 tok/s)
  Input 50000,  Output  500:  82.0s (616 tok/s)
  Input 100000,  Output  500: 153.0s (676 tok/s)

Speed model saved to: data/model_speed_profile.json
```

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

## `task-lookup`

查询单个任务详情，跨会话可用。

```powershell
my-agent task-lookup <task_id>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `task_id` | 是 | 任务 ID |

## `task-list`

列出任务列表，支持按用户和状态过滤。

```powershell
my-agent task-list
my-agent task-list --user-id admin --status RUNNING --limit 20
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--user-id` | 当前用户 | 按用户 ID 过滤 |
| `--status` | 全部 | 按状态过滤，如 PLANNING/RUNNING/DONE |
| `--limit` | 50 | 最多显示多少条 |

## `task-show`

显示单个任务详情。

```powershell
my-agent task-show <task_id>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `task_id` | 是 | 任务 ID |

## `task-abandon`

标记任务为 ABANDONED，Dispatch 不再调度。

```powershell
my-agent task-abandon <task_id>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `task_id` | 是 | 任务 ID |

## `task-pause`

暂停任务，可用 `task-resume` 恢复。

```powershell
my-agent task-pause <task_id>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `task_id` | 是 | 任务 ID |

## `task-resume`

恢复已暂停的任务。

```powershell
my-agent task-resume <task_id>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `task_id` | 是 | 任务 ID |

## `task-search`

搜索任务，支持模糊描述。

```powershell
my-agent task-search "gateway"
my-agent task-search --query "修复"
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--query` | 是 | 搜索关键词或模糊描述 |

## `notifications`

查看任务完成通知。

```powershell
my-agent notifications
my-agent notifications --all
my-agent notifications --flush
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--all` | 否 | 列出所有通知（含已读） |
| `--flush` | 否 | 推送所有离线存储的通知 |
| `--limit` | 20 | 最多显示多少条 |

## `adapter`

外部通道适配器（飞书/QQ/文件）。

```powershell
my-agent adapter start --channel feishu
my-agent adapter start --channel qq
my-agent adapter start --channel all
my-agent adapter start --daemon
my-agent adapter status
my-agent adapter stop
```

子命令：

| 子命令 | 说明 |
|--------|------|
| `start --channel <name>` | 启动指定通道适配器（feishu/qq/all） |
| `start --daemon` | 后台守护进程模式运行，写入 PID 文件 |
| `start --pid-file <path>` | 指定 PID 文件路径 |
| `status` | 查看通道适配器状态 |
| `status --pid-file <path>` | 指定 PID 文件路径 |
| `stop` | 停止所有通道适配器 |
| `stop --pid-file <path>` | 指定 PID 文件路径 |
| `stop --timeout <seconds>` | 等待优雅停止的超时秒数 |
| `file` | 文件协议适配器（inbox JSON → gateway → outbox JSON） |

## `audit-log`

查询审计日志，支持按用户、动作、目标过滤，显示最近活跃用户和摘要统计。

```powershell
my-agent audit-log
my-agent audit-log --user admin --action create_task --limit 50
my-agent audit-log --target-type task --offset 10
my-agent audit-log --recent-users --limit 10
my-agent audit-log --summary
my-agent audit-log --cleanup --days 90
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--user <id>` | 全部 | 按用户 ID 过滤 |
| `--action <name>` | 全部 | 按动作类型过滤，如 `create_task`、`access_denied` |
| `--target <id>` | 全部 | 按目标 ID 过滤 |
| `--target-type <type>` | 全部 | 按目标类型过滤，如 `task` |
| `--status <status>` | 全部 | 按状态过滤，如 `success`、`denied` |
| `--limit <n>` | `100` | 最多显示条数 |
| `--offset <n>` | `0` | 分页偏移 |
| `--recent-users` | `false` | 显示最近活跃用户列表 |
| `--summary` | `false` | 显示审计统计摘要 |
| `--cleanup` | `false` | 清理旧审计条目 |
| `--days <n>` | `90` | cleanup 时清理多少天前的记录 |

## 安全约定

- 默认调度命令都是 dry-run，先写报告，不修改任务。
- `subagent-run --execute` 会调用真实 API。
- `subagents-dispatch --apply --execute-runners` 会调用真实 API。
- `subagents-dispatch --planner` 在 gate 发现有待处理事项时会调用父代理 LLM。
- `--execute-runners` 必须和 `--apply` 一起使用。
- `--watch` 是前台常驻，终端关闭或 `Ctrl+C` 后进程停止。
- `--force-lock` 只用于确认旧 watch 进程异常退出后的残留 lock。
