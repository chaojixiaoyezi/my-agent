# STATUS

## 2026-05-02 最新恢复入口

如果下次换电脑、换会话、换 IDE，先看这一段和 [HANDOFF_current-state.md](HANDOFF_current-state.md)。

当前远端已同步到：

- branch: `main`
- remote: `origin/main`
- latest pushed recovery checkpoint: parent/subagent recovery scenario + gateway stale lease scenario

最新可恢复能力：

- `memory-resume` 已能把 gateway request/response JSON 当成恢复事实源。
- `scenario-test --case gateway-cross-day-resume` 已能启动真实后台 gateway、投递真实 `gateway ask`、模拟跨天线索，并验证恢复能回到 request/response JSON。
- `scenario-test --case gateway-delayed-response` 已能验证 response 已落盘时，迟到 pending 请求副本只归档、不重复调用模型。
- `scenario-test --case gateway-multi-worker` 已能用两个 request worker 并发处理多条 pending 请求，验证不重复响应或归档。
- `scenario-test --case gateway-stale-lease` 已能模拟 worker 中断留下旧 processing lease，验证恢复会重排并完成请求。
- `scenario-test --case gateway-processing-stop` 已能模拟 worker 正在处理请求时 gateway stop/restart，验证重启后不卡死、不丢请求、不留半截 JSON。
- `scenario-test --case parent-subagent-cross-day-resume` 已能创建真实子代理任务、执行 runner 工具回合、模拟跨天线索，并验证恢复能回到任务事实源。
- `scenario-test --case real-model-recovery-multi-round` 已能用真实 API 跑多轮工具调用（read_file + search_text），验证 memory-resume 能找回每轮 evidence 和 output.json。
- gateway worker 在 processing lease 写失败时会继续处理请求，不会因为观测文件失败把用户请求卡死。
- 如果 LocalStore 记录了临时 `requests/processing/<id>.json`，恢复时会优先纠偏到现存的 `requests/done` 或 `requests/failed`。
- 日志分析模块第一版已落地：SecurityCase → LogWorkOrder → SubAgentTask 转换链 + bounded_query 受控查询工具。

最新验收：

- `python3 -m pytest -q` -> `749 passed`
- `python3 scripts/check_doc_sync.py` -> `DOC_SYNC_PASS`
- `git diff --check` -> passed
- focused gateway/memory/doc tests -> `16 passed`
- wide memory/gateway focused tests -> `76 passed`
- parent/subagent runner recovery focused tests -> `2 passed`
- gateway stale lease focused tests -> `3 passed`
- gateway multi-worker focused tests -> `4 passed`
- gateway delayed-response focused tests -> `5 passed`
- gateway/scenario/doc focused tests -> `11 passed`
- CLI reference focused test -> `1 passed`
- log analysis first loop tests -> `26 passed`

下一版优先级：

1. ~~补 gateway 坏天气场景：processing 中 stop/restart。~~ ✅ 已完成 `gateway-processing-stop`
2. ~~把 subagent workflow router/compiler/parent gate 接到真实 task creation，默认 dry-run 或 manual-confirm。~~ ✅ 已完成
3. ~~推进 LOG work-order 到真实 SubAgentTask 执行桥，并补 bounded evidence reader。~~ ✅ 已完成第一版
4. ~~用真实外部模型补跑 parent/subagent runner 跨天恢复冒烟。~~ ✅ 已完成 `real-model-recovery-multi-round`

详细交接见 [HANDOFF_current-state.md](HANDOFF_current-state.md)，模块细节见 `docs/modules/memory/02-progress.md`、`docs/modules/subagent/02-progress.md` 和 `docs/modules/gateway/02-progress.md`。

更新时间：2026-05-01

当前阶段：`v0.4-dev / memory resume and auto recovery context`

总体状态：核心骨架已可运行，真实 API 全流程已通过；当前重点已经从“能跑”进入“可常驻、可观察、可恢复、可审计”。

最新推进：
- 已完成完整从头到尾真实链路测试：CLI、memory、LocalStore、gateway、scenario、真实 API runner、父代理验收全部通过。
- 已新增轻量 recovery snapshot 自动写入：普通 run/chat/gateway 默认随保存写 hook，subagent-run 在 runner 结果写回后写 run_id 恢复锚点。
- `memory-resume` 已支持 `--context-only`，可以只输出稳定恢复块，方便人工 handoff、真实环境测试和后续自动注入。
- 已新增可选恢复上下文自动注入：默认关闭；打开后在“继续/恢复/刚刚/run_id”等场景读取归档和事实源，把短 `Recovery Brief` 注入本轮 prompt。
- `run/chat/gateway ask` 已支持 `--resume-context` / `--no-resume-context` 临时开关，并在状态行展示 prompt、injection、resume 的保守 token 估算。
- 修复默认 gateway 入口缺少 chat handler 的回归；`my-agent` 默认入口可自动进入 gateway chat。
- scenario-test 已按当前写入边界核对子代理 `task_dir/scenario_outputs/` 产物，避免旧路径误判。
- 完整冒烟脚本最后统一改用 pytest 正常运行，避免跳过 pytest fixture 机制。
- 新增 workstream 并行开发工作台：用 git worktree 隔离 memory、runtime、tools-boundary、live-lab 等开发线，并提供状态查看、可见终端打开和 handoff 模板。
- 新增 Live Lab 可见真实环境测试台：可以新开 Terminal 观察 prompt、命令、响应和证据路径，并默认使用隔离 workspace。
- 已新增 `local-doctor` / `local-rebuild`，可从 memory、gateway、subagent 文件事实源诊断并重建 LocalStore。
- `status` 已输出 suggested actions，能提示 gateway、LocalStore 和 subagent 的下一步处理动作。
- gateway 请求队列新增 `failed` 归档、processing lease、超时重排/失败归档和保守 request worker pool。
- runner 并发已有第一版配置入口：默认 1；显式设置 `runner_concurrency` 为数字后才并行执行多个 run。
- 已新增 `adapter file` 文件协议，外部聊天工具/TUI 可通过 inbox/outbox 复用 gateway。
- 已新增 Round 5 Gateway 常驻稳定性功能：
  - 长期助手 风格 PID 记录（start_time tracking + scoped locks）
  - Watchdog Supervisor 自动监控并重启崩溃 gateway
  - Adapter 守护进程模式（`--daemon` + PID 文件）
  - `gateway start-all --adapter` 一键启动 gateway + 适配器
  - 系统服务安装（`gateway install` systemd/launchd）

最近已推送提交：
- `45bbd08 feat: Round 5 continued — adapter daemon, PID tracking, one-click start, test fixes`
- `37e046d feat: Round 5 gateway stability — 长期助手 daemon control, supervisor, service install`
- `84813a7 merge: integrate tool boundary hardening`
- `d6e31b2 merge: integrate framework runtime hardening`
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
- 兼容 Qwen/通道运行时 常见 XML-ish 工具调用方言。
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

最近完整验证：2026-04-30

已通过：
- `py_compile`
- `CLI_REFERENCE` 命令/参数覆盖测试
- LocalStore 定向测试
- pytest 全量测试：`131 passed`
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
python3 scripts/live_agent_lab.py --suite smoke
scripts/open_live_lab.sh --suite real --real-llm --timeout 300 --count 1 --max-cycles 2
bash -n scripts/workstream_*.sh scripts/open_workstream.sh
scripts/workstream_status.sh
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

1. memory archive / hook 可观察入口
   - 能列出 `memory/hooks` 和 `memory/raw` 最近记录。
   - 能按 session/request/run/tool/status 等字段搜索。

2. memory resume 恢复线索
   - 用户说“继续”或给出关键词时，能找出相关归档、LocalStore 记录和任务目录引用。
   - 输出恢复摘要、事实源路径和下一步建议，也可以用 `--context-only` 单独输出恢复上下文块。
   - 不把 archive 当最终事实源，正式继续前仍先读任务目录。

3. 压缩前 hook 标准化
   - 把 run/chat/gateway/subagent-run 的结束点统一写 recovery snapshot。
   - 默认按 `memory_hook_archive_level=3` 保存恢复必需字段。

4. worker 并发模型设计
   - 明确 runner 并发、启动速率、超时和自适应策略。
   - 从当前 `daemon_*` 过渡到更正式 gateway scheduler。

5. 外部聊天工具 adapter
   - 复用 `gateway ask/result` 和 LocalStore timeline。
   - 让完成结果自动回到聊天工具。

6. 长期后台日志分析底座
   - 规划见 `LOG_ANALYSIS_BACKLOG.md`。
   - 先做 file/api/syslog/kafka 多来源接入、checkpoint/dedup、parser registry、DuckDB/Parquet 查询和 detector -> case -> analyst subagent 闭环。
   - ML 底座先预留统一 `FeatureExtractor` / `ScoringEngine` / `ModelRegistry` 契约，后续可接本地 sklearn/river、远程 HTTP/gRPC 模型服务、ClickHouse/Spark/Flink/Ray/Kafka stream scorer 等集群后端。

## 常用命令速查

```powershell
my-agent
my-agent status
my-agent timeline --limit 20
my-agent local-doctor
my-agent local-rebuild
my-agent memory-doctor
my-agent memory-route "任务恢复规则"
my-agent local-search "关键词"
my-agent local-search "任务目标" --source-type subagent_run
my-agent local-search "gateway" --source-type gateway_request
my-agent gateway status
my-agent gateway ask "继续推进当前任务"
my-agent adapter file --watch
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
