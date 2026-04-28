# my-agent

一个用 Python3 标准库搭起来的个人通用智能体骨架。

当前项目重点不是“做一个简单聊天脚本”，而是在逐步搭一个可审计、可扩展、能承载多层子代理工作的基础设施：
- 普通 chat / run。
- 长期记忆。
- 工具目录和工具调用循环。
- skill / tool 统一能力路由。
- subagent 工单、看板、due-check、通道探测。
- capability request / grant / gap。
- subagent execution context。
- subagent runner dry-run / execute。
- runner 结构化输出回写。

## 当前状态

已能运行的主链路：
1. 普通请求进入 `SimpleAgent.run()`。
2. 根据任务注入工具目录和推荐工具。
3. 模型如输出 `[TOOL_CALL]`，工具系统执行并回填结果。
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
