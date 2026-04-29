# STATUS

更新时间：2026-04-29

当前阶段：`v0.3.x / 本地可恢复工作台底座`

总体状态：核心骨架已可运行，真实 API 全流程已通过；当前重点已经从“能跑”进入“可常驻、可观察、可恢复、可审计”。

最近已推送提交：
- `22efdce feat: add status and timeline views`
- `519492b feat: index gateway and subagent logs`
- `f30cc08 feat: add local sqlite store`

## 当前可用能力

### 安装和入口

本地开发安装：

```powershell
python -m pip install -e .
```

安装后可以直接运行：

```powershell
my-agent
my-agent --help
my-agent status
my-agent timeline --limit 20
```

`my-agent` 不带子命令时会自动确保后台 gateway 存活，然后进入 `chat --gateway` 客户端模式。

### 普通对话和工具调用

已落地：
- `run` 单轮请求。
- `chat` 前台交互。
- `chat --gateway` 作为后台 gateway 客户端。
- 工具目录和推荐工具注入。
- 标准 `[TOOL_CALL]...JSON...[/TOOL_CALL]` 工具调用。
- 兼容 Qwen/OpenClaw 常见 XML-ish 工具调用方言。
- 半截工具调用会转成可恢复的 `__parse_error__`，避免整轮崩溃。

内置工具：
- `list_files`
- `read_file`
- `search_text`
- `write_file`
- `append_file`
- `replace_in_file`
- `fetch_url`
- `http_request`

### 记忆和本地事实源

已落地：
- `memory.jsonl` 继续作为原始记忆流水。
- `LocalStore` 第一版：SQLite + FTS5 + 文件系统 + JSONL。
- 新记忆会双写：JSONL 保存原始记录，SQLite/FTS5 做检索索引。
- 旧记忆可用 `local-index-memory` 补建索引。
- `local-search` 可按关键词和 `source_type` 搜索。
- `timeline` 可按 `source_type` / `event_type` 查看最近事件。

已接入 LocalStore 的主要来源：
- `memory`
- `gateway_request`
- `gateway_event`
- `subagent_run`
- `subagent_work_log`
- `subagent_runner_result`
- `subagent_execution_context`
- `subagent_acceptance_review`
- `subagent_patch_review`
- `subagent_dispatch`
- `subagent_dispatch_report`
- `subagent_dispatch_watch`
- `parent_planner`
- `subagent_capability_route`
- `subagent_action_apply`
- `subagent_channel_probe`

### Gateway 常驻

已落地：
- `my-agent gateway start/status/stop/restart/logs`
- 后台 Python 进程常驻。
- pid/state/heartbeat/stop request/log 文件控制面。
- 本地文件队列：`pending -> processing -> done + responses`。
- `gateway ask` 可同步等待结果。
- `gateway ask --no-wait` 可异步投递，后续用 `gateway result <request_id>` 取结果。
- gateway 重启时会把遗留 `processing` 请求退回 `pending`。
- `my-agent` 默认自动启动 gateway 并进入 gateway chat。
- gateway request 和生命周期事件会写入 LocalStore。

当前 gateway 形态：单机本地后台进程，不是 HTTP/WebSocket 服务。

### Subagent / 多代理工作流

已落地：
- 子代理工单创建和标准目录。
- 父子关系、root_id、depth。
- 子代理红绿灯看板。
- due-check 风险巡检。
- channel probe 通道健康检查。
- action plan / action apply。
- takeover / reassign 基础记录。
- capability request / grant / gap。
- skill/tool 统一能力路由。
- execution context 最小上下文包。
- subagent runner dry-run / execute。
- runner 结构化输出 `[SUBAGENT_RESULT]` 解析。
- runner 坏结构化输出修复回合。
- runner 临时失败有限重试。
- patch review。
- acceptance review 独立验收。
- fake done / 伪造 artifact 防护。
- dispatch 一轮调度。
- dispatch watch 循环。
- parent planner gate：有活跃/待处理/卡住事项时，不允许空心 `HEARTBEAT_OK`。

重要边界：
- `subagent-run` 默认 dry-run。
- 只有显式 `--execute` 才调用真实模型 runner。
- `subagents-dispatch` 默认 dry-run。
- 只有 `--apply --execute-runners` 才会推进真实 runner。
- runner 不直接把任务标为 DONE，只进入等待验收，再由 acceptance 收口。

### 观察入口

已落地：

```powershell
my-agent status
my-agent status --json
my-agent timeline --limit 20
my-agent timeline --source-type gateway_request
my-agent timeline --event-type gateway_request_completed --details
```

`status` 汇总：
- gateway 是否存活、pid、heartbeat、队列数量。
- LocalStore 记录数、事件数、FTS5 状态。
- subagent summary、红灯任务、最近任务。
- 最近 timeline 事件。

`timeline` 展示：
- LocalStore 最近事件。
- 支持按来源和事件类型过滤。
- 可输出 JSON，方便后续 TUI/聊天工具复用。

## 测试状态

最近完整验证：2026-04-29

已通过：
- `py_compile`
- `CLI_REFERENCE` 命令/参数覆盖测试
- LocalStore 定向测试
- 自动发现测试：`68` 个测试通过
- 标准完整冒烟：`ALL_TESTS_PASS`
- 真实 API gateway ask
- 真实 API scenario-test happy path
- 坏天气场景：
  - `verification`
  - `gateway-restart`
  - `structured-repair`
  - `runner-retry`
- 真实 API subagent-run execute
- 父代理验收闭环
- `git diff --check`

当前测试入口：

```powershell
python agent_py_agent/tests/run_tests.py
```

注意：完整冒烟按当前约定会调用真实 API。

## 当前主要限制

还没做完：
- 真正并行 worker pool / session pool。
- 多层父子代理自动上抛和自动下发的完整闭环。
- 子代理和孙代理的真实进程级并发调度。
- patch 自动应用和集成验收。
- lessons 自动生成自学习草稿。
- 长期本地数据 compact / rebuild / backup 命令。
- 远端同步、跨机器 gateway 协作、本体迁移、本体备份。
- HTTP/WebSocket gateway 服务。
- 外部聊天工具 adapter。
- TUI 观察面板。
- ACP / adapter / 外部 session 接入。

当前设计取向：
- 先把单机本地第一事实源做稳。
- 所有关键动作先落文件和 LocalStore。
- 远端同步和组织级多 gateway 后续在这个底座上叠加。

## 推荐下一步

优先级建议：

1. `local-rebuild` / `local-doctor`
   - 从现有 `memory.jsonl`、gateway 文件队列、subagent 目录重建 LocalStore。
   - 检查 SQLite、JSONL、文件系统之间是否不一致。

2. `status` 增强
   - 增加 blocked / awaiting_acceptance / stale gateway 的更明确提示。
   - 增加“建议下一步动作”。

3. gateway 恢复增强
   - 对超时 processing 请求做自动标记。
   - 增加请求级重试和失败归档策略。

4. worker 并发模型设计
   - 明确 runner 并发、启动速率、超时和自适应策略。
   - 从当前 `daemon_*` 过渡到更正式 gateway scheduler。

5. 外部聊天工具 adapter
   - 复用 `gateway ask/result` 和 LocalStore timeline。
   - 让完成结果自动回到聊天工具。

## 常用命令速查

```powershell
my-agent
my-agent status
my-agent timeline --limit 20
my-agent local-search "关键词"
my-agent local-search "任务目标" --source-type subagent_run
my-agent local-search "gateway" --source-type gateway_request
my-agent gateway status
my-agent gateway ask "继续推进当前任务"
my-agent subagents
my-agent subagents-dispatch --watch --planner --apply --execute-runners
my-agent scenario-test
```

## 当前判断

这个版本已经具备一个个人本地 agent 的第一层核心能力：
- 能安装后直接运行。
- 能常驻后台。
- 能通过 gateway 接收任务。
- 能派工和验收子代理。
- 能真实调用 API 跑完整流程。
- 能把关键过程落盘、搜索、审计和观察。

下一阶段应继续围绕“恢复、诊断、并发、外部接入”推进。
