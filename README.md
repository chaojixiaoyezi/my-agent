# my-agent

## 安装后直接执行

本地开发安装：

```bash
python -m pip install -e .
```

安装后可以直接运行：

```bash
my-agent
my-agent --help
my-agent chat
my-agent gateway start
my-agent chat --gateway
my-agent gateway status
my-agent gateway ask "你好，检查一下当前任务"
my-agent scenario-test
```

完整参数手册见 [CLI_REFERENCE.md](CLI_REFERENCE.md)。

项目名先固定为 `my-agent`。后续如果确定正式名字，只需要改 `pyproject.toml` 里的 `project.name` 和 `project.scripts`，代码入口可以继续复用 `agent_py_agent.__main__:main`。

当前推荐的常驻方式是第一版后台 gateway：

```bash
my-agent
my-agent gateway start
my-agent chat --gateway
my-agent gateway status
my-agent gateway ask "继续推进当前任务"
my-agent gateway stop
```

`my-agent` 不带子命令时会自动启动后台 gateway，然后进入 `chat --gateway`。退出 chat 不会关闭 gateway；gateway 会继续在后台值班。

`gateway` 第一版会启动后台 Python 进程，在内部复用现有 daemon/watch 调度，并写 pid、state、heartbeat、stop request、日志和本地请求队列。`gateway ask` 会把聊天/任务投递给后台 gateway，由常驻进程调用模型并把结果写回 `data/gateway/responses`。

大白话说：`gateway start` 是“把主代理放到后台值班”，`gateway ask` 是“给后台主代理发一句话”，`gateway result` 是“拿之前异步任务的结果”。以后接入聊天工具后，普通用户不需要手动敲 `ask/result`，聊天工具会自动投递请求并把结果回给你；这两个命令会保留为开发和排错入口。

`my-agent chat --gateway` 已经可以把普通聊天消息接到后台 gateway：chat 只是前台客户端，真正调用模型的是 gateway。`my-agent` 默认会走这条路径。为了稳妥，显式执行 `my-agent chat` 仍然保留原来的前台模型调用。

主代理现在也能在自然语言对话里真正派工，而不只是“建议使用 subagent”。工具目录里新增了三个主代理专属工具：
- `create_subagents`：把聊天里的拆分/派工意图落成子代理工单。
- `subagent_board`：让主代理读取当前子代理看板和风险状态。
- `dispatch_subagents`：让主代理触发一轮调度；默认 dry-run，只有明确 `apply=true` 且 `execute_runners=true` 才会调用真实 runner。

做真实任务测试时，可以直接跑：

```bash
my-agent scenario-test
```

它会新建临时 fixture，通过 gateway ask 让主代理派工，再执行真实 runner 和父代理验收，并把 `memory_path`、`subagent_workspace`、`gateway_workspace` 和文件工具都关进 `workspace_root` 指向的临时目录，避免污染当前开发仓库。

坏天气场景也走同一个入口：

```bash
my-agent scenario-test --case verification
my-agent scenario-test --case gateway-restart
my-agent scenario-test --case structured-repair
my-agent scenario-test --case runner-retry
my-agent scenario-test --case all --count 1
```

`verification` 会确认伪造 artifact 不会通过验收；`gateway-restart` 会确认旧 gateway 崩溃遗留的 processing 请求能退回 pending；`structured-repair` 会确认坏结构化输出能被修复回合补齐；`runner-retry` 会确认 runner 临时失败后下一轮 dispatch 能有限重试并完成验收。

`daemon` 仍保留为前台调试入口。开启 planner 后，如果 gate 发现仍有 active/pending/stalled/needs-intervention 事项，会触发完整父代理 LLM turn；如果模型只回 `HEARTBEAT_OK`，会被记录为失败。

配置分两层：`task_max_subagents=0` / `task_max_grandchildren=0` 表示用户层任务规模不设硬上限；`runner_concurrency: "auto"` 等调度项留给未来 gateway 自适应。`runner_failure_policy: "auto"` 当前表示临时 runner 失败最多尝试 2 次，`"off"` 表示不自动重试，数字字符串如 `"3"` 表示最多尝试 3 次。当前 `daemon_*` 是前台调度器的高级参数：`daemon_max_runners: "auto"` 会先映射成保守值 1，`daemon_max_cycles=0` 表示持续运行，`daemon_limit=0` 表示不限制记录条数，`daemon_max_cards=0` 表示不限制能力卡数量，`daemon_interval=0` 通常只用于测试或单轮验证。

一个用 Python3 标准库搭起来的个人通用智能体骨架。

当前项目重点不是“做一个简单聊天脚本”，而是在逐步搭一个可审计、可扩展、能承载多层子代理工作的基础设施：
- 普通 chat / run，以及 `chat --gateway` 客户端模式。
- 长期记忆。
- 本地事实源：SQLite 结构化账本、FTS5 全文索引、正文文件和 JSONL 审计流水。
- 工具目录和工具调用循环，兼容标准 `[TOOL_CALL]` JSON 以及 Qwen/通道运行时 常见的 XML-ish 工具调用方言。
- skill / tool 统一能力路由。
- subagent 工单、看板、due-check、通道探测。
- capability request / grant / gap。
- subagent execution context。
- subagent runner dry-run / execute。
- runner 结构化输出回写。
- gateway 后台进程与本地 inbox / response 消息入口。
- `scenario-test` 隔离全流程观察入口。

## 当前状态

已能运行的主链路：
1. 普通请求进入 `SimpleAgent.run()`。
2. 根据任务注入工具目录和推荐工具。
3. 模型如输出 `[TOOL_CALL]`，或输出 Qwen/通道运行时 常见的 `<tool_call><function=...><parameter=...>` 形式，工具系统会解析、执行并回填结果；如果工具调用半截损坏，会变成可恢复的 `__parse_error__`。
4. 子代理可以创建工单，记录父子关系、能力边界、验收要求和证据。
5. 子代理缺能力时记录 `CapabilityRequest`。
6. 父代理可用 Capability Router 把 request 路由成 grant 或 gap。
7. grant 可生成 `execution_context.json`。
8. `subagent-run` 读取 execution context，默认 dry-run，显式 `--execute` 才调用模型。
9. runner 输出 `[SUBAGENT_RESULT]` JSON 后，系统会把 evidence、capability request、artifacts、tests、patches、lessons、next_actions 写回工单。

还没做完的主链路：
- 真正的并行 worker / process / session 调度。
- 多层父子代理自动上抛和下发。
- patch 自动应用与集成验收。
- lessons 自动生成自学习草稿。
- 完整 ACP / adapter / 外部 session 接入。

## 快速开始

查看命令：

```bash
python3 -m agent_py_agent --help
```

运行一次普通请求：

```bash
python3 -m agent_py_agent run "你好，介绍一下当前项目" --no-save
```

进入 chat：

```bash
python3 -m agent_py_agent chat
```

创建子代理工单：

```bash
python3 -m agent_py_agent spawn-subagents "开发一个可验收的功能" --count 2
```

查看子代理看板：

```bash
python3 -m agent_py_agent subagents
```

查看本地事实源状态，并把旧 JSONL 记忆补建到 SQLite/FTS5：

```bash
python3 -m agent_py_agent local-store-status
python3 -m agent_py_agent local-index-memory
python3 -m agent_py_agent local-search "表格" --source-type memory
```

生成单个子代理执行上下文：

```bash
python3 -m agent_py_agent subagent-context <run_id>
```

dry-run 一个子代理 runner，不调用模型：

```bash
python3 -m agent_py_agent subagent-run <run_id>
```

真正执行一个子代理 runner，可能调用模型 API：

```bash
python3 -m agent_py_agent subagent-run <run_id> --execute
```

能力请求路由 dry-run：

```bash
python3 -m agent_py_agent subagents-route-capabilities --dry-run
```

能力请求路由 apply：

```bash
python3 -m agent_py_agent subagents-route-capabilities --apply
```

更多 subagent / capability 细节见 [SUBAGENT_RUNBOOK.md](SUBAGENT_RUNBOOK.md)。

## 配置

主配置文件：

```text
agent_py_agent/config/agent_config.yaml
```

负责：
- 模型后端。
- API 地址和 key 环境变量名。
- 记忆路径。
- 本地事实源路径：`local_store_path`、`local_store_files_dir`、`local_store_events_path`。
- 工具开关和工具返回长度。
- chat / run 通用行为。
- 自学习总开关预留项。

能力路由配置：

```text
agent_py_agent/config/capability_config.yaml
```

负责：
- capability request 最大描述长度。
- 上抛最大层数。
- 候选 skill/tool 数量。
- grant 最大 skill/tool 数量。
- subagent heartbeat / timeout / due-check 参数。
- evidence 最小要求。

约定：能力路由配置里的数字限制项，`0` 表示不限制。

## API Key

不要把真实 key 写进仓库。

推荐在 shell 里配置：

```bash
export AGENT_API_KEY="你的 key"
```

配置文件里只保留环境变量名：

```yaml
api_key: ""
api_key_env: "AGENT_API_KEY"
```

## 关键文档

- [agent_py_agent/README.md](agent_py_agent/README.md)：包内 CLI 使用说明。
- [SUBAGENT_RUNBOOK.md](SUBAGENT_RUNBOOK.md)：subagent、capability 和 runner 详细手册。
- [AGENTS.md](AGENTS.md)：后续 AI 开发者必须遵守的开发规范。
- [CODEBASE_TREE.md](CODEBASE_TREE.md)：目录树和关键文件职责。
- [DESIGN_LEDGER.md](DESIGN_LEDGER.md)：设计想法、落地状态和后续方向。
- [TESTS.md](TESTS.md)：测试说明。

## 安全边界

- `subagent-run` 默认 dry-run，不调用模型。
- `--execute` 才会调用模型 API。
- runner 执行前默认做 channel probe，BROKEN 时不会继续模型调用。
- 子代理只能看到 `execution_context.json` 里的 allowed tools / skills。
- 模型尝试调用未授权工具时，工具层会拒绝。
- runner 不会直接把任务标成 DONE，只会进入 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`，等待独立验收。
- `patches` 当前只记录补丁意图和状态，不会自动 apply。
- `lessons` 当前只写入 debrief，不会自动写正式 skill。

## 本地验证

推荐先跑定向测试，避免误触真实模型 API：

```bash
python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py
python3 -m agent_py_agent --help
python3 -m agent_py_agent subagent-run --help
```

当前完整 `agent_py_agent/tests/run_tests.py` 里包含 `agent_py_agent run`，如果默认配置是远端模型，可能会请求真实 API。需要完整跑之前，建议先把测试配置切到 echo 或确认 API 调用成本。
