# agent_py_agent

## 安装入口

仓库根目录提供 `pyproject.toml`，本地开发安装后会生成 `my-agent` 命令：

```bash
python -m pip install -e .
my-agent
my-agent --help
```

`my-agent` 当前等价于：

```bash
python3 -m agent_py_agent
```

不带子命令时，`my-agent` 会自动确保 gateway 后台进程正在运行，然后进入 `chat --gateway`。

例如：

```bash
my-agent
my-agent chat
my-agent gateway start
my-agent chat --gateway
my-agent gateway ask "你好"
my-agent scenario-test
```

完整参数手册见仓库根目录的 [CLI_REFERENCE.md](../CLI_REFERENCE.md)。

这是 `my-agent` 的 Python 包目录，负责 CLI 入口、模型后端、prompt 拼装、工具循环、记忆、skill/tool 能力路由和 subagent 工单系统。

项目目前坚持一个原则：主链路尽量只依赖 Python3 标准库，先把结构、边界和可审计性做稳。

## CLI 总览

```bash
python3 -m agent_py_agent --help
```

主要命令：

```text
run                         运行一次智能体对话
status                      查看 gateway、LocalStore、subagent 和最近事件总览
timeline                    查看本地事实源最近事件
chat                        启动交互循环
remember                    手动写入记忆
memory-list                 列出最近记忆
memory-search               搜索记忆
local-store-status          查看 SQLite/FTS5 本地事实源状态
local-search                搜索本地事实源
local-index-memory          把旧 JSONL 记忆补建到本地事实源
local-doctor                诊断 LocalStore / gateway / subagent 文件账本一致性
local-rebuild               从文件事实源重建 LocalStore
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
scenario-test               隔离跑 gateway/派工/runner/验收全流程
real-e2e                    跑主代理基础验收矩阵，必要时生成真实任务批量测试计划
gateway                     管理后台 gateway，并向 gateway 投递请求
adapter                     外部聊天工具 / TUI 文件适配器
subagent-context            生成单个子代理执行上下文
subagent-run                按执行上下文运行子代理 runner
```

## 隔离全流程观察

```bash
python3 -m agent_py_agent scenario-test
```

这条命令会创建临时 fixture 项目，把 `workspace_root` 指过去，然后走：

```text
gateway ask -> 主代理 create_subagents 创建并后台启动 -> tree/refs -> RUNNING 时等待后台 runner -> 必要时 dispatch 恢复/重跑 -> 主代理收口
```

所有 memory、subagent、gateway 和文件工具写入都在临时目录里，不会碰当前开发仓库。
`create_subagents` 的自动启动会对本批 run_id 跑一轮精确 `subagents-dispatch --apply --start-runners`，不复用 watch 循环，避免和 gateway/daemon 的观察锁互相抢占。

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
python3 -m agent_py_agent
python3 -m agent_py_agent chat
python3 -m agent_py_agent gateway start
python3 -m agent_py_agent chat --gateway
```

不带子命令的 `python3 -m agent_py_agent` 会自动启动 gateway 并进入 gateway chat。显式 `chat` 仍在当前前台进程里调用模型。`chat --gateway` 会把普通消息投递给后台 gateway，chat 自己只负责接收输入和显示响应。

常用命令：

```text
/help                  查看帮助
/sessions              只读列出当前用户最近会话及精确 resume 命令；不切换正在运行的窗口
/status                立即查看当前任务状态，并列出命名 Audit/Goal 的名称、运行时长和状态
/btw <内容>            仅纠偏当前运行任务一次；不会带到下一任务
/stop                  像停止按钮一样打断当前窗口正在执行的一轮及其子代理；命名 Audit/Goal 不受影响
/goal [目标]           查看或开始当前会话的持续目标
/goal <时长> <名称> <任务> 开始一个可与其他命名 Goal 并存的持续目标
/goal <名称> clear     只停止这个命名 Goal
/goal pause|resume     暂停或恢复同一持续目标
/goal edit <目标>      修改目标；保留原任务和工作区
/goal clear            清除持续目标；不删除普通聊天和已有成果
/verbose [off|on|full] 查看或设置当前会话的过程显示档位
/audit help            查看当前真正支持的 Audit 命令
/audit <名称> prepare <内容> 在准确 Audit 工作区中讨论、读样例、试接口或准备修改
/audit <时长> <名称> <任务> 启动一个可与其他命名 Audit 并存的审计
/audit <名称> status   查看这个 Audit 的生效要求、待处理内容、时长和来源概况
/audit <名称> clear    只停止这个命名 Audit
/memory [关键词]       搜索记忆；不带关键词显示最近记忆
/remember <内容>       手动写入记忆
/prompt-file <路径>    增加动态 prompt 文件
/show-prompt <问题>    显示最终 prompt 并回答
/exit                 退出
/logout               退出
```

模型响应期间可以继续输入，新的请求会进入后台队列。

在 `--gateway` 模式下，`/memory`、`/remember` 等个人命令仍由当前 CLI 本地处理；普通自然语言消息会通过 gateway request/response 通道交给后台 gateway。所有位于消息开头的 `/XXXX` 都先由程序解析，不支持的命令直接拒绝，不会进入模型。`/status`、`/btw <内容>`、`/stop`、`/goal ...`、`/audit ...`、`/verbose ...` 与 Feishu 共用 Gateway 会话控制入口。`/audit help|status|clear` 由程序确定执行；`/audit <名称> prepare <内容>` 仍走一次普通模型 turn，但只临时绑定准确 Audit 的资料和工作目录。提问、试验和比较只更新待处理内容；只有当前 prepare turn 中的结构化发布工具可以改变生效要求。`/btw` 不提供列表模式，`/btw-clear` 已移除。用户不用 `/subagents` 指定数量；主代理根据真实可并行工作自主拆分，数量和数量上限由结构化调度合同校验。

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
~/.my-agent/owners/local/main/memory/long_term/memory.jsonl
```

本地事实源默认保存在：

```text
~/.my-agent/owners/local/main/data/workspaces/<workspace-scope>/local_store/
```

它包含 SQLite 数据库、正文文件目录和追加式审计 JSONL。fresh install 下，新记忆、会话、协作和子代理账本都写当前 owner home；repo 内 `data/*` 不再作为普通运行事实源。配置里显式改成非默认运行路径时会按配置落盘。常用维护命令：

```bash
python3 -m agent_py_agent local-store-status
python3 -m agent_py_agent local-index-memory
python3 -m agent_py_agent local-doctor
python3 -m agent_py_agent local-rebuild
python3 -m agent_py_agent local-search "表格" --source-type memory
python3 -m agent_py_agent local-search "gateway 日志" --source-type gateway_request
python3 -m agent_py_agent local-search "子代理目标" --source-type subagent_run
python3 -m agent_py_agent status
python3 -m agent_py_agent timeline --limit 20
```

gateway request、gateway 生命周期事件、subagent 工单、runner 结果、验收、patch 审核、dispatch、watch、planner、能力路由和通道探测也会写入本地事实源。
`status` 看当前总览；`timeline` 看最近事件。
`local-doctor` 查本地账本是否一致；`local-rebuild` 从 memory、gateway、subagent 文件事实源补建 LocalStore。

这个目录默认被 Git 忽略。

## 工具系统

当前内置工具：

```text
list_files
find_files
read_file
read_artifact
search_text
write_file
apply_patch
list_tools
web_search
web_fetch
run_command
```

旧的 `append_file`、`replace_in_file`、`file_write_session`、`write_structured_json`、
`data_to_workbook`、`markdown_to_pdf` 不再作为模型可见工具。复杂文件由模型使用
`write_file`、`apply_patch` 或授权的 `run_command` 生成，系统统一做路径边界、artifact registry
和结构化副作用门；真实命令/文件结果从共用工具出口进入被动验证事实，不经过另一套目录验收器。

工具系统有两层 prompt：
- Tool Catalog：中等详细度工具目录。
- Recommended Tools：当前任务最相关的少量工具详情。

每次 run 会先固定一份工具快照：`注册工具 ∩ owner policy ∩ allowed_tools ∩ availability`。
文本目录、推荐区、原生工具 Schema、`list_tools`、`tool_search` 和最终执行都复用它；
工具搜索只能在快照内做减法，不能获得新权限。可选后端未配置或既有进程掉线时不会继续向模型
宣称可用，执行前还会复检；可用性检查本身不启动浏览器/LSP/MCP、不发网络请求也不写业务数据。

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
python3 -m agent_py_agent subagents --root-id <root_run_id>
```

`--root-id`、`--status`、`--owner` 会在生成看板时直接收窄范围；终端输出、
`subagent_board.json` 和 `SUBAGENT_BOARD.md` 都反映同一个范围，便于排查单棵代理树。

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

后台 gateway：

```bash
python3 -m agent_py_agent gateway start
python3 -m agent_py_agent gateway status
python3 -m agent_py_agent gateway ask "继续推进当前任务"
python3 -m agent_py_agent gateway stop
```

`gateway` 第一版会启动后台 Python 进程，并写 `gateway.pid`、`gateway_state.json`、`gateway_heartbeat.json`、`gateway_stop.request`、`gateway.log`、本地请求队列和响应文件。`gateway ask` 会把聊天/任务写入 `requests/pending`，后台 gateway 处理后把结果写入 `responses`，后续 chat/TUI 会接到这条客户端通道上。

这里的本地请求队列可以这样理解：

```text
requests/pending      新请求，等待 gateway 处理
requests/processing   正在处理；gateway 崩溃或 processing 超时后会退回 pending
requests/done         已处理请求的原始记录
requests/failed       超过重试次数或处理失败的请求归档
responses             每个 request_id 对应的模型响应
gateway_requests.jsonl 审计日志
```

`gateway ask/result` 不是最终用户必须记住的日常入口。以后接聊天工具时，聊天工具会把用户消息写入同一条队列，再把 response 自动发回给用户；CLI 命令主要用于开发、调试和排查外部适配器问题。

第一版文件适配器：

```bash
python3 -m agent_py_agent adapter file --watch
```

外部工具把 JSON 消息写到 `adapter_workspace/inbox`，adapter 投递给 gateway，再把响应写到 `outbox`。

`chat --gateway` 是这条路的第一步：它已经不在前台 chat 里直接调用模型，而是把普通消息交给后台 gateway。后续 TUI、微信、飞书、Telegram 等适配器会继续复用同一条消息通道。

飞书普通用户不需要任务触发词：聊天、让 Agent 做文件工作、派工或设置定时，都从同一条自然语言
对话进入。会话由“当前 owner + 飞书 chat/topic”确定；它会持续累计原始 transcript（对话记录），达到阈值后自动 compact（压缩摘要），然后在同一会话继续累计；任务只是挂在会话上的持久工作现场，不会把用户换到另一个聊天。不同 owner 或不同 chat/topic 不共享历史。同一会话消息严格按顺序落账。当前模型轮仍在执行时，新普通消息会作为真实 UserTurn 在下一个安全点进入该轮，不新建第二份 history，也不靠文字判断“聊天还是任务”；没有 live turn 时则开始正常下一轮。旧的 active task 不会自动污染新闲聊；只有实际任务工具、结构化续接选择或 `/audit`、`/goal` 特殊模式才建立任务关联。

`/goal` 是同一会话上的持续目标层，不是新会话或新 Agent。旧的未命名 `/goal <目标>` 仍保持单目标兼容语义；显式 `/goal <时长> <名称> <任务>` 可以在同一 thread 中并存，每个 Goal 都有独立 task/goal ID、计时和续跑事件。Audit 只支持命名语法；名称是不含空格、最多 64 个字符、区分大小写的用户句柄，真正执行权来自当前 owner/thread 内稳定的 `audit_id`。每个 Audit 的资料位于 owner 自己的 `audits/<audit_id>/`，不会与普通 `tasks/` 或其他 Audit 混放。`prepare` 只把这一轮临时带入该目录，不建立第二份 transcript、Memory、Compact 或黏住的 Audit 模式；草稿与生效要求分别保存，明确发布后才切换，运行中的完整批次仍用旧要求完成。`/status` 只概览名称、已运行时长和状态；`/goal <名称> clear`、`/audit <名称> clear` 或主代理的结构化停止工具只影响精确同名项。告诉主代理名称即可停止唯一匹配项；若同名 Audit 和 Goal 同时存在，程序会拒绝猜测并要求指明类型。普通 `/stop` 仍只中断当前窗口，不删除 transcript、任务工作区或记忆，也不会停止命名 Audit/Goal。多个 Goal 同时存在时，普通聊天不会猜测其中某个是“当前目标”，精确后台续跑轮只读取自己的 Goal，因此不会把后台目标菜单污染进普通对话。只有消息开头的显式 `/audit` 命令能建立 Audit 范围；程序不从普通正文判断是否属于 Audit 或是否已经确认修改。

默认安装使用飞书长连接并开启私聊密码卡；首次设置卡不会吞掉用户的第一条消息。`USER.md` 中的称呼、画像和稳定偏好，以及
`AGENTS.md` 中的长期工作约定，都由当前 owner 的 Agent 通过 `update_persona` 自主维护；单项保持一个单行事实，
USER 多项可用一次原子 `operations` 批量提交。可选 `source_quote` 只进入版本审计，不参与自然语言授权。
只有 `SOUL.md` 的性格、语气和核心人设变更必须走确认链，飞书用户点击卡片前不会写入。基础文件、补丁或
shell 直接碰三份人格文件都会在任务晋升前被拒，并明确报告没有发生变更。
默认产品规则来自随包发布的 `builtin:prompts/default.md`，不依赖启动目录。

前台 watch 调试入口：

```bash
python3 -m agent_py_agent daemon
```

`daemon` 默认读取 `agent_config.yaml` 里的 `daemon_*` 配置。模型根据可独立的工作项自主决定本批子代理数量；当前 root 可用槽位、可选单次限制、每任务、每 owner 和全局并发上限依次取严格最小值。请求超限时整批拒绝，不静默截断或创建一半。默认 `max_subagents=8`、`subagent_hierarchy_max_children_per_tool_call=0`，即只用八个 root 会话槽位而不再额外限制单批为四个；`task_max_subagents=0` / `task_max_grandchildren=0` 也表示对应维度不额外收紧，不取消 owner/并发安全上限。runner 并发、超时和启动速率默认走 `auto`；`daemon_max_cycles=0` 表示持续运行，`daemon_limit=0` 表示不限制记录条数。

宿主内部恢复/观察循环（不是模型工具）：

```bash
python3 -m agent_py_agent subagents-dispatch --watch --planner --interval 30
```

安全跑一轮 watch：

```bash
python3 -m agent_py_agent subagents-dispatch --watch --max-cycles 1 --interval 0
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

## Subagent Runner 结束协议

子代理像主代理一样自然输出最终回复，不需要生成 `[SUBAGENT_RESULT]` 包装或自行声明
`DONE/VERIFIED`。宿主根据真实模型流、工具结果和控制事件写入统一 `turn_end`：
`completed`、`aborted`、`blocked`、`error`、`max_tokens`、`interrupted`。

模型可见的递归控制面只有 `create_subagents`（创建后自动启动）、`send_guidance`（给直属
child 插入普通消息）、`cancel_subagents`（打断并结束直属 child）和 `resolve_capability_requests`
（处理直属 child 的权限请求）。内部 dispatcher/worker pool 负责启动、并发、恢复、活动投影和
终态通知，不暴露查询、等待、巡逻、手动推进或自报进度工具。真实产物、工具记录、blockers、
capability requests 和 refs 由宿主持久化并自动递给直接父级；子代理创建孙代理时仍是同一套关系。
自动重试资格不能否决直接父级的显式打断；同批 child 也不会互相注入完整 goal 名册。

Gateway/会话任务及其递归 child 的普通相对路径都从用户项目 cwd 解析。内部 task root 是状态目录，
不是项目目录；只有显式写成 `work/...` 或 `output/...` 时才进入宿主管理的任务命名空间。

详细说明见仓库根目录的 [SUBAGENT_RUNBOOK.md](../SUBAGENT_RUNBOOK.md)。

## Gateway 与工作目录

本地 TUI/Gateway 会话进入后台任务后仍使用启动时的项目目录作为 cwd；隐藏任务目录只保存状态和显式
`work/`、`output/` 内容，不会让同一条相对路径出现“前台可写、后台不可写”。远程用户和子代理仍服从各自
更窄的 owner/task 写边界。

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
