# agent_py_agent

这是 `my-agent` 的 Python 包目录，负责 CLI 入口、模型后端、prompt 拼装、工具循环、记忆、skill/tool 能力路由和 subagent 工单系统。

项目目前坚持一个原则：主链路尽量只依赖 Python3 标准库，先把结构、边界和可审计性做稳。

## CLI 总览

```bash
python3 -m agent_py_agent --help
```

主要命令：

```text
run                         运行一次智能体对话
chat                        启动交互循环
remember                    手动写入记忆
memory-list                 列出最近记忆
memory-search               搜索记忆
spawn-subagents             创建子代理工单
subagents                   查看子代理红绿灯看板
subagent                    查看单个子代理详情
subagents-due-check         巡检子代理问题
subagents-probe             检查子代理通道健康
subagents-plan-actions      根据 due-check 生成动作计划
subagents-apply-actions     dry-run 或 apply 低风险动作
subagents-route-capabilities 路由 open capability request
subagents-acceptance        验收等待验收的子代理
subagents-patches           审核 runner 输出里的 patch 记录
subagents-dispatch          执行一轮父代理调度
subagent-context            生成单个子代理执行上下文
subagent-run                按执行上下文运行子代理 runner
```

## 普通运行

```bash
python3 -m agent_py_agent run "总结这个项目现在有什么能力" --no-save
```

显示最终 prompt：

```bash
python3 -m agent_py_agent run "解释工具系统" --show-prompt --no-save
```

动态注入 prompt：

```bash
python3 -m agent_py_agent run "写一个简短计划" --inject "回答要短" --no-save
```

加载额外 prompt 文件：

```bash
python3 -m agent_py_agent run "按额外规则回答" --prompt-file prompts/default.md --no-save
```

## Chat 模式

```bash
python3 -m agent_py_agent chat
```

常用命令：

```text
/help                  查看帮助
/status                查看后台任务状态
/btw                  查看当前运行时 prompt 注入
/btw <内容>            增加运行时 prompt 注入
/btw-clear            清空运行时 prompt 注入
/memory [关键词]       搜索记忆；不带关键词显示最近记忆
/remember <内容>       手动写入记忆
/prompt-file <路径>    增加动态 prompt 文件
/subagents <数量> <目标> 生成子代理工单
/show-prompt <问题>    显示最终 prompt 并回答
/exit                 退出
/logout               退出
```

模型响应期间可以继续输入，新的请求会进入后台队列。

## 记忆

手动写入：

```bash
python3 -m agent_py_agent remember "我喜欢清晰的表格" --kind preference
```

列出最近记忆：

```bash
python3 -m agent_py_agent memory-list --limit 20
```

搜索记忆：

```bash
python3 -m agent_py_agent memory-search "表格" --limit 5
```

记忆默认保存在：

```text
agent_py_agent/data/memory.jsonl
```

这个目录默认被 Git 忽略。

## 工具系统

当前内置工具：

```text
list_files
read_file
search_text
write_file
append_file
replace_in_file
fetch_url
http_request
```

工具系统有两层 prompt：
- Tool Catalog：中等详细度工具目录。
- Recommended Tools：当前任务最相关的少量工具详情。

subagent runner 会使用 `allowed_tools` 白名单：
- prompt 里只展示授权工具。
- 模型尝试调用未授权工具时会被拒绝。

## Subagent 常用命令

创建工单：

```bash
python3 -m agent_py_agent spawn-subagents "实现一个功能并验收" --count 2
```

查看看板：

```bash
python3 -m agent_py_agent subagents
python3 -m agent_py_agent subagents --all
python3 -m agent_py_agent subagents --status BLOCKED
```

查看单个 run：

```bash
python3 -m agent_py_agent subagent <run_id>
```

巡检：

```bash
python3 -m agent_py_agent subagents-due-check
```

通道探测：

```bash
python3 -m agent_py_agent subagents-probe <run_id>
```

动作计划：

```bash
python3 -m agent_py_agent subagents-plan-actions
```

低风险动作 dry-run：

```bash
python3 -m agent_py_agent subagents-apply-actions --dry-run
```

低风险动作 apply：

```bash
python3 -m agent_py_agent subagents-apply-actions --apply --action reopen_for_evidence --run-id <run_id>
```

能力路由 dry-run：

```bash
python3 -m agent_py_agent subagents-route-capabilities --dry-run
```

能力路由 apply：

```bash
python3 -m agent_py_agent subagents-route-capabilities --apply
```

验收 dry-run：

```bash
python3 -m agent_py_agent subagents-acceptance --dry-run
```

验收 apply：

```bash
python3 -m agent_py_agent subagents-acceptance --apply --run-id <run_id>
```

patch 审核 dry-run：

```bash
python3 -m agent_py_agent subagents-patches --dry-run
```

patch 审核 apply：

```bash
python3 -m agent_py_agent subagents-patches --apply --run-id <run_id>
```

父代理调度 dry-run：

```bash
python3 -m agent_py_agent subagents-dispatch --dry-run
```

父代理调度 apply：

```bash
python3 -m agent_py_agent subagents-dispatch --apply
```

真正调用 runner 模型：

```bash
python3 -m agent_py_agent subagents-dispatch --apply --execute-runners
```

生成执行上下文：

```bash
python3 -m agent_py_agent subagent-context <run_id>
```

runner dry-run：

```bash
python3 -m agent_py_agent subagent-run <run_id>
```

runner 真执行：

```bash
python3 -m agent_py_agent subagent-run <run_id> --execute
```

## Subagent Runner 输出协议

runner prompt 会要求模型最后输出：

```text
[SUBAGENT_RESULT]
{
  "status": "AWAITING_ACCEPTANCE",
  "summary": "本轮完成或卡住的摘要",
  "used_tools": [],
  "used_skills": [],
  "evidence": [],
  "capability_requests": [],
  "artifacts": [],
  "tests": [],
  "patches": [],
  "lessons": [],
  "next_actions": [],
  "blocked_reason": "",
  "failure_type": ""
}
[/SUBAGENT_RESULT]
```

系统会自动解析这个 JSON 块：
- `evidence` 写入验收证据。
- `capability_requests` 写成 open `CapabilityRequest`。
- `artifacts`、`tests`、`patches`、`lessons`、`next_actions` 写入 `output.json`。
- `lessons` 和 `next_actions` 也会追加到 `DEBRIEF.md`。
- 未授权 `used_tools` / `used_skills` 会被忽略并审计。

详细说明见仓库根目录的 [SUBAGENT_RUNBOOK.md](../SUBAGENT_RUNBOOK.md)。

## 配置文件

主配置：

```text
agent_py_agent/config/agent_config.yaml
```

能力配置：

```text
agent_py_agent/config/capability_config.yaml
```

API key 推荐从环境变量读取：

```bash
export AGENT_API_KEY="你的 key"
```

## 测试

安全的定向检查：

```bash
python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py
python3 -m agent_py_agent --help
python3 -m agent_py_agent subagent-run --help
python3 -m agent_py_agent subagents-patches --help
python3 -m agent_py_agent subagents-dispatch --help
```

注意：完整 `agent_py_agent/tests/run_tests.py` 是当前标准收口冒烟，会使用真实 API，并用临时配置隔离 memory 和 subagent 测试数据。
