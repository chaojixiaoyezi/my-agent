# my-agent

一个用 Python 标准库搭起来的个人通用智能体底座。

后续版本统一以 **my-agent** 发布，正式仓库为 [chaojixiaoyezi/my-agent](https://github.com/chaojixiaoyezi/my-agent)。
安装命令、产品说明和发布名称统一为 `my-agent`，已有 `~/.my-agent` 用户数据不因改名迁移。
历史说明使用中性路径与会话占位，不假装旧目录已更名；原始定位仍可按提交和运行编号追溯。

它现在不是单纯聊天脚本，而是在逐步变成一个可常驻、可审计、可恢复、能跑多层子代理任务的本地工作台。

当前哪些能力稳定、部分可用、实验性或仅设计，以
[当前产品事实](docs/PRODUCT_FACTS.md) 为唯一权威；路线图和历史完成记录不代表已发布能力。

### 在 TUI 中配置模型

输入 `/model`，选择“新增模型”或“选择已有模型”。新增时选择 OpenAI 风格或 Anthropic 风格接口，
填写模型名称、接口基础地址、密钥和总上下文窗口（tokens）；按 Tab 切换，选择“保存”，或返回/退出不保存。
Auth 认证暂时只有预留入口。保存后在“选择已有模型”中启用，可随时选回“部署默认”。

配置仅影响当前用户的后续工作片；正在执行的工作片和已有子代理不会因菜单切换被中断。
密钥掩码输入、私有保存，不进入聊天记录。保存成功不代表接口已经验证可用；窗口请按供应商实际容量填写。

### 在 TUI 中选择权限与审批模式

输入 `/permissions`，或按 **F4** 打开菜单；审批框出现时也能按 F4，不必逐个确认子代理。

| 模式 | 含义 |
| --- | --- |
| 默认确认 | 自己家目录内工作，敏感工具操作询问用户。 |
| 自主工作 | 自己家目录内自动执行，主代理和所属子代理不逐次询问普通工具操作。 |
| Full Access | 仅本机管理员可选，主代理可访问家目录外，不逐次询问普通工具操作。 |

↑↓ 选择、Enter 保存、Esc 取消。Full Access 有二次确认；本机 TUI 默认是管理员身份，但不默认开启全盘权限。
也可明确输入 `/permissions ask`、`/permissions auto` 或 `/permissions full-access`。
选择保存到当前用户的工具策略，适用于该用户的会话与子代理，不改变其他用户。
审批选择保存后生效，已挂起的主/子代理普通工具审批可原地继续；路径权限在下一工作片生效，当前片不热改。
子代理仍限定该用户家目录；禁用工具、灾难命令保护和 SOUL 等本人确认不会因为自主模式被取消。

## 从零开始

下面命令按“刚拿到项目，想跑起来”的顺序写。每条命令后面都写了它是干什么的。

### 推荐：一键容器安装

Linux，或已经装好 Docker/Podman 的 macOS/WSL：

```bash
curl -fsSL https://raw.githubusercontent.com/chaojixiaoyezi/my-agent/main/install.sh | bash
```

安装器默认构建受限容器，在容器内完成 bwrap namespace/mount 真自检，然后安装一个透明
`my-agent` 包装器。普通用户直接运行 `my-agent`；脚本仍可运行 `my-agent run ...`，都不需要手动 `docker exec`。

- `~/.my-agent` 持久挂到容器的 `/my-agent-home`。
- 当前工作目录是唯一挂入的项目工作区 `/workspace`。
- owner-scoped 前后台命令必须经过 bwrap；自检失败则安装/worker 启动失败，不会降级宿主执行。
- 干净 Linux 没有 Docker/Podman 时，安装器会尝试安装 rootless Podman。

本地源码验证容器安装：

```bash
MYAGENT_SRC="$PWD" bash install.sh --container
```

### macOS / Linux 宿主开发安装

宿主 venv 只推荐开发使用；没有 Linux bwrap 时，owner-scoped `run_command` 会安全拒绝。

```bash
cd /path/to/my-agent
# 进入项目根目录；后面的命令都默认在这里执行。

python3 --version
# 确认 Python 已安装；项目要求 Python 3.10 或更高。

python3 -m venv .venv
# 创建一个本项目专用虚拟环境，避免污染系统 Python。

source .venv/bin/activate
# 启用虚拟环境；看到命令行前面有 (.venv) 就对了。

python -m pip install -U pip
# 升级 pip，减少安装包版本问题。

python -m pip install -e .
# 以可编辑模式安装本项目；改代码后不用重复安装。

my-agent --help
# 验证安装成功，并查看所有可用命令。
```

也可以复用安装器显式选择宿主开发模式：

```bash
bash install.sh --host
```

### Windows PowerShell 安装

```powershell
cd C:\你的路径\my-agent
# 进入项目根目录；把路径换成你本机实际位置。

py -3 --version
# 确认 Python 已安装；项目要求 Python 3.10 或更高。

py -3 -m venv .venv
# 创建一个本项目专用虚拟环境。

.\.venv\Scripts\Activate.ps1
# 启用虚拟环境；看到命令行前面有 (.venv) 就对了。

python -m pip install -U pip
# 升级 pip。

python -m pip install -e .
# 以可编辑模式安装本项目。

my-agent --help
# 验证安装成功，并查看所有可用命令。
```

如果 PowerShell 提示不允许执行脚本，先运行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# 允许当前 Windows 用户执行本机脚本；这是为了能激活 .venv。
```

## API Key

当前默认配置使用 MiniMax 的 Anthropic 兼容接口：

```yaml
model_backend: "anthropic_compatible"
api_base: "https://api.minimaxi.com/anthropic"
model_name: "MiniMax-M2.7"
api_key_env: "AGENT_API_KEY"
```

### 去哪里拿 Key

截至 2026-04-29，MiniMax 官方文档写法是：

- 打开 [MiniMax API Platform](https://platform.minimax.io/docs/guides/quickstart)，注册或登录账号。
- 进入 [API Overview](https://platform.minimax.io/docs/api-reference/api-overview) 里说的 `API Keys`。
- 普通按量付费 key：选择 `Create new secret key`。
- Token Plan key：选择 `Create Token Plan Key`。

### Key 存在哪里

不要把真实 key 写进 Git 仓库，也不要提交到 README、配置文件或聊天记录里。

本项目推荐只把 key 放到环境变量 `AGENT_API_KEY`，配置文件里只保留“去哪个环境变量读”：

```yaml
api_key: ""
api_key_env: "AGENT_API_KEY"
```

### macOS / Linux 临时设置 Key

```bash
read -rsp "AGENT_API_KEY: " AGENT_API_KEY; echo
# 安全输入 key；不会把 key 明文显示在终端。

export AGENT_API_KEY
# 把刚输入的 key 放进当前终端环境变量。

echo ${AGENT_API_KEY:+AGENT_API_KEY_OK}
# 只验证 key 是否存在，不打印 key 内容。
```

这个方式只对当前终端有效。关掉终端后需要重新设置。

### macOS / Linux 长期设置 Key

```bash
nano ~/.zshrc
# 打开 zsh 配置文件；如果你用 bash，就改成 nano ~/.bashrc。
```

在文件末尾手动加一行：

```bash
export AGENT_API_KEY="你的真实 key"
# 每次打开新终端时自动设置 AGENT_API_KEY。
```

保存后运行：

```bash
source ~/.zshrc
# 让刚改的配置立刻在当前终端生效。

echo ${AGENT_API_KEY:+AGENT_API_KEY_OK}
# 验证 key 已经读到，但不打印 key。
```

### Windows PowerShell 临时设置 Key

```powershell
$env:AGENT_API_KEY="你的真实 key"
# 只给当前 PowerShell 窗口设置 key。

if ($env:AGENT_API_KEY) { "AGENT_API_KEY_OK" }
# 只验证 key 是否存在，不打印 key 内容。
```

这个方式只对当前 PowerShell 窗口有效。

### Windows PowerShell 长期设置 Key

```powershell
[Environment]::SetEnvironmentVariable("AGENT_API_KEY", "你的真实 key", "User")
# 把 key 存到当前 Windows 用户的环境变量里。

# 关掉当前 PowerShell，再重新打开一个新的 PowerShell。
# Windows 用户环境变量通常要新窗口才会生效。

if ($env:AGENT_API_KEY) { "AGENT_API_KEY_OK" }
# 验证新窗口已经读到 key，但不打印 key。
```

### 关于 `.env`

`.env` 文件已经被 `.gitignore` 忽略，但当前代码不会自动读取 `.env`。

也就是说，写 `.env` 只是“本地备忘”，程序不会自己加载。最稳的方式仍然是使用系统环境变量 `AGENT_API_KEY`。

## 第一次验证

先跑不会调用真实模型的检查：

```bash
python3 scripts/live_agent_lab.py --suite smoke
# 跑 Live Lab 冒烟测试；默认临时改成 echo 后端，不消耗真实 API。
```

确认 key 和真实模型可用：

```bash
my-agent run "你好，用一句话介绍你自己" --no-save
# 直接调用一次真实模型；--no-save 表示不要把这轮写入长期记忆。

my-agent run "继续刚才的 README 任务" --resume-context --show-prompt
# 临时打开恢复上下文注入，并打印最终 prompt，方便检查它到底注入了哪些恢复线索。
```

如果你想看后台 gateway 路径：

```bash
my-agent gateway start
# 启动后台 gateway，相当于让主代理在后台值班。

my-agent gateway ask "你好，检查一下 gateway 是否能工作" --no-save
# 给后台 gateway 发一条任务；这会调用真实模型。

my-agent gateway status
# 查看 gateway 是否还活着、队列里有没有请求。

my-agent gateway stop --kill
# 停掉后台 gateway；--kill 表示正常停止失败时强制结束。
```

## 常用命令速查

### 基础命令

```bash
my-agent --help
# 查看所有命令和参数；不调用模型。

my-agent status
# 查看当前总览：gateway、LocalStore、subagent、最近事件；不调用模型。

my-agent status --json
# 输出机器可读 JSON，方便脚本或后续 TUI 使用；不调用模型。

my-agent timeline --limit 20
# 查看最近 20 条本地审计事件；不调用模型。

my-agent timeline --source-type gateway_request
# 只看 gateway 请求相关事件；不调用模型。
```

### 普通对话

```bash
my-agent run "帮我总结当前项目状态" --no-save
# 脚本/调试入口：运行一次前台请求；会调用真实模型；--no-save 表示不写长期记忆。

my-agent chat
# 进入 gateway 客户端聊天；真正调用模型的是后台 gateway。

my-agent chat --direct
# 开发调试：显式绕过 gateway，在当前前台进程调用模型；不是正式默认 runtime。

my-agent
# 默认入口：自动确保 gateway 存活，然后进入 chat --gateway。
```

### Gateway 后台值班

```bash
my-agent gateway start
# 启动后台 gateway；启动本身不调用模型。

my-agent chat --gateway
# 与默认 `my-agent chat` 相同；保留显式写法便于脚本表达意图。

my-agent gateway ask "继续推进当前任务"
# 给后台 gateway 发一条同步请求；会调用真实模型，并等待结果。

my-agent gateway ask "继续推进当前任务" --resume-context --show-prompt
# 临时打开恢复上下文注入，并在响应里打印最终 prompt。

my-agent gateway ask "跑一个长任务" --no-wait
# 异步投递请求，不等模型完成；会返回 request_id。

my-agent gateway result <request_id>
# 读取异步请求结果；不会发起新的模型调用。

my-agent gateway logs --lines 80
# 查看 gateway 最近 80 行日志；不调用模型。

my-agent gateway restart --force
# 重启 gateway；旧进程停不掉时允许强制终止。

my-agent gateway stop --kill
# 停止 gateway；超时后强制结束。
```

### 本地记忆和事实源

fresh install 下，长期记忆和本地事实源默认属于当前 owner：

```text
~/.my-agent/owners/local/main/memory/long_term/memory.jsonl
~/.my-agent/owners/local/main/workspace/runtime/workspaces/<workspace-scope>/local_store/
~/.my-agent/owners/local/main/tasks/<yyyy-mm-dd>/<task-slug>/{output,work}/
```

repo 内 `data/*` 只保留测试 fixture 或显式指定路径用途，不再是普通运行事实源。
如果配置里显式把 `local_store_path`、`subagent_workspace`、`gateway_workspace`
等改成非默认路径，这些显式路径仍会生效；默认新任务、记忆、gateway 和子代理状态都归当前 owner home。

本地 TUI 的结构化身份默认为 `local/main`，但默认权限仍是 WorkspaceOnly：无论从哪个 shell 目录启动，
相对路径都从自己的 owner home（或任务建立后的项目目录）解析，不会把 `/root`、源码树等启动目录自动变成
工作区。普通/远程用户只能使用自己的 owner home；文件权限不限制正常外网访问。只有本机 `local/main`
显式配置 `access_mode: full-access` 后才可访问外部目录，且子代理仍保持 owner/task 范围。Full Access 下，
模型只有在用户明确指定外部路径或要求系统排障时才离开自己的 home；涉及其他 owner 目录还需用户明确点名，
默认先只读并尽量少改。这里的自然语言规则只是行为约束，真正权限只认结构化身份和配置。

```bash
my-agent memory-list --limit 20
# 查看最近 20 条长期记忆；不调用模型。

my-agent memory-search "关键词" --limit 5
# 搜索长期记忆 JSONL；不调用模型。

my-agent local-store-status
# 查看 SQLite/FTS5/文件事实源状态；不调用模型。

my-agent local-index-memory
# 把旧 memory.jsonl 补建到 LocalStore 索引；不调用模型。

my-agent local-doctor
# 诊断 memory、gateway、subagent、LocalStore 是否一致；不调用模型。

my-agent local-rebuild --source fts
# 重建全文检索索引；不调用模型。

my-agent local-search "gateway" --source-type gateway_request
# 在 LocalStore 里搜索 gateway 请求记录；不调用模型。
```

### Subagent 多代理工作流

```bash
my-agent spawn-subagents "开发一个可验收的功能" --count 2
# 手动创建 2 个子代理工单；不直接执行真实 runner。

my-agent subagents
# 查看子代理红绿灯看板；不调用模型。

my-agent subagents-due-check
# 巡检卡住、超时、需要验收的子代理；不调用模型。

my-agent subagents-route-capabilities --dry-run
# 预览能力请求如何路由成 skill/tool grant；不写回。

my-agent subagents-route-capabilities --apply
# 真正写入 capability grant/gap；不一定调用模型。

my-agent subagent-context <run_id>
# 生成某个子代理的执行上下文包；不调用模型。

my-agent subagent-run <run_id>
# dry-run 一个子代理 runner，只生成 prompt 和报告；不调用模型。

my-agent subagent-run <run_id> --execute
# 真正调用模型执行这个子代理；会消耗 API。

my-agent subagents-dispatch --dry-run
# 预览一轮父代理调度；不写回，不执行 runner。

my-agent subagents-dispatch --apply --start-runners --max-runners 1
# 真正调度并执行最多 1 个 runner；会调用真实模型。
```

### 场景测试

```bash
my-agent scenario-test
# 隔离跑一轮真实全流程：gateway ask -> 派工 -> runner -> 验收；会调用真实模型。

my-agent scenario-test --case verification
# 验证伪造 artifact 不会通过验收；通常不调用真实模型。

my-agent scenario-test --case gateway-restart
# 验证 gateway 崩溃遗留 processing 请求能恢复；不调用真实模型。

my-agent scenario-test --case gateway-cross-day-resume
# 验证真实后台 gateway 请求跨天恢复到 request/response JSON；echo 后端不调用真实模型。

my-agent scenario-test --case gateway-delayed-response
# 验证响应已落盘但 pending 请求副本迟到时不会重复调用模型；不调用真实模型。

my-agent scenario-test --case gateway-multi-worker
# 验证两个 request worker 并发抢占多条 pending 请求；不调用真实模型。

my-agent scenario-test --case gateway-stale-lease
# 验证 worker 中断留下的旧 processing lease 会重排并完成；不调用真实模型。

my-agent scenario-test --case parent-subagent-cross-day-resume
# 验证真实 subagent runner 写回后跨天恢复到任务事实源；使用测试后端，不调用真实模型。

my-agent scenario-test --case structured-repair
# 验证坏 SUBAGENT_RESULT 能触发修复回合；使用测试后端，不调用真实模型。

my-agent scenario-test --case runner-retry
# 验证 runner 临时失败会有限重试；使用测试后端，不调用真实模型。

my-agent scenario-test --case all --count 1
# 连续跑所有场景；最后的 happy path 会调用真实模型。
```

### Live Lab 可见测试台

```bash
python3 scripts/live_agent_lab.py --suite smoke
# 当前终端跑冒烟测试；默认不调用真实模型。

python3 scripts/live_agent_lab.py --suite compact-stress --real-llm --timeout 2400
# 当前终端跑真实 compact 长输出压测；默认生成约 10MB 现场记录，至少要求 20 次 compact。

python3 scripts/live_agent_lab.py --suite real --real-llm --timeout 300 --count 1 --max-cycles 2
# 当前终端跑真实 LLM + gateway + 长链路测试；会调用真实模型。

scripts/open_live_lab.sh --suite real --real-llm --timeout 300 --count 1 --max-cycles 2
# macOS 新开一个可见 Terminal 跑真实测试；你能看到 prompt、命令、响应和证据路径。
```

### 并行开发 Workstream

```bash
scripts/workstream_create.sh memory
# 创建 memory 开发线的 git worktree 和 workstream/memory 分支。

scripts/workstream_create.sh memory --dry-run
# 只预览会创建什么，不真的创建目录或分支。

scripts/workstream_status.sh
# 查看主仓库和所有 worktree 的分支、HEAD、未提交文件。

scripts/open_workstream.sh memory
# macOS 新开一个可见 Terminal，进入 memory 开发线。
```

每条线的职责边界和交接格式见 [WORKSTREAMS.md](docs/WORKSTREAMS.md) 和 [HANDOFF_TEMPLATE.md](docs/tasks/HANDOFF_TEMPLATE.md)。

## 配置文件

主配置文件：

```text
agent_py_agent/config/agent_config.yaml
```

它负责：

- 模型后端、API 地址、模型名、key 环境变量名。
- owner home、记忆路径和本地事实源路径。
- memory raw/hook 归档、长期规则路由和可选恢复上下文注入开关。
- gateway 工作区和请求超时。
- daemon / runner / 调度策略。
- 工具开关和工具返回长度。
- 自学习总开关预留项。

能力路由配置：

```text
agent_py_agent/config/capability_config.yaml
```

它负责：

- capability request 最大描述长度。
- 上抛最大层数。
- 候选 skill/tool 数量。
- grant 最大 skill/tool 数量。
- subagent heartbeat / timeout / due-check 参数。
- evidence 最小要求。

约定：能力路由配置里的数字限制项，`0` 表示不限制。

## 当前能力

已能运行的主链路：

1. 普通请求进入 `SimpleAgent.run()`。
2. 根据任务注入工具目录和推荐工具。
3. 模型输出 `[TOOL_CALL]` 或 Qwen/通道运行时 常见 XML-ish 工具调用时，工具系统会解析、执行并回填结果。
4. 工具调用半截损坏时，会变成可恢复的 `__parse_error__`，避免整轮崩掉。
5. 子代理可以创建工单，记录父子关系、能力边界、交付要求和证据。
6. 父代理可用 Capability Router 把 request 路由成 grant 或 gap。
7. `subagent-run` 默认 dry-run，显式 `--execute` 才调用真实模型。
8. runner 输出 `[SUBAGENT_RESULT]` JSON 后，系统会把 evidence、artifacts、tests、patches、lessons、next_actions 写回工单。
9. workflow plan 已能进入真实任务创建：父任务保存 `workflow_plan`，`auto` apply 可物化 worker 子工单。
10. `enable_self_learning=true` 时，成功 runner 的 lessons 会生成 learning draft 候选，由 `my-agent learn` 管理。
11. CLI 构造 agent 时支持 `MY_AGENT_RUNTIME_CONFIG` / `MY_AGENT_RUNTIME_CONFIG_LAYERS` 注入 owner/task/run/runtime scoped 配置 overlay，并保留 `config_sources` / `config_layers` 来源链。
12. 后台 dispatch 启动标记和启动恢复摘要会暴露结构化读取/保存错误，避免把坏账本误判成“没有任务”。

还没做完的主链路：

- 真正的进程级 worker pool / session pool 和长期心跳治理。
- 多层父子代理自动上抛和下发，以及 capability grant 后的自动唤醒闭环。
- patch 自动集成后的更强 owner / 权限策略、批量验证和失败恢复编排。
- 运行时错误报告已经覆盖更多恢复入口，但仍需继续清理 gateway/http/lease/audit 等剩余 `except Exception` best-effort 路径。
- 配置层已支持 CLI runtime overlay，后续还要把 agent-local/task-local overlay ref 接到子代理创建、接管和远端 session 入口。
- accepted learning draft 到正式 skill / rule / profile 的人工确认提升流程。
- 完整 ACP / 外部 agent session / 远端执行器接入。

## 安全边界

- 不要把真实 API Key 写进仓库。
- `subagent-run` 默认 dry-run，不调用模型。
- `--execute` 才会调用模型 API。
- runner 执行前默认做 channel probe，BROKEN 时不会继续模型调用。
- 子代理只能看到 `execution_context.json` 里的 allowed tools / skills。
- 模型尝试调用未授权工具时，工具层会拒绝。
- 多用户/owner-scoped 的前台与后台 shell 必须通过 bwrap 隔离；缺失、动态依赖错误、namespace 或 mount 自检失败统一返回 `SANDBOX_UNAVAILABLE`，不允许未隔离回退。
- 默认容器安装只挂当前工作目录和 `~/.my-agent`，不挂 Docker socket，也不暴露宿主其他目录。
- runner 写出结构化结果后直接进入 `DONE/VERIFIED` 或明确失败态；父级只读取代理树和产物 refs 继续调度或汇总，不再有单独最终收口阶段。
- `patches` 当前只记录补丁意图和状态，不会自动 apply。
- `lessons` 会写入 `output.json` / `DEBRIEF.md`；打开 `enable_self_learning=true` 后还会生成 learning draft 候选，但不会自动写正式 skill。

## 关键文档

- [LLM_GUIDE.md](LLM_GUIDE.md)：**LLM/AI 开发者总入口**，含开工前/收工后清单、编码规范和设计原则。
- [docs/ROADMAP.md](docs/ROADMAP.md)：待做/进行中功能清单，开工前必读。
- [docs/COMPLETED.md](docs/COMPLETED.md)：已落地功能清单，收工后必改。
- [CLI_REFERENCE.md](CLI_REFERENCE.md)：完整命令和参数手册。
- [SUBAGENT_RUNBOOK.md](docs/modules/subagent/SUBAGENT_RUNBOOK.md)：subagent、capability 和 runner 详细手册。
- [WORKSTREAMS.md](docs/WORKSTREAMS.md)：并行开发线和 worktree 规则。
- [AGENTS.md](AGENTS.md)：后续 AI 开发者必须遵守的开发规范。
- [CODEBASE_TREE.md](CODEBASE_TREE.md)：目录树和关键文件职责。
- [DESIGN_LEDGER.md](DESIGN_LEDGER.md)：设计想法、落地状态和后续方向的主导航。
- [docs/design/](docs/design/)：模块级长篇设计文档。
- [TESTS.md](TESTS.md)：测试说明和覆盖率报告。

## 测试

### 运行测试

```bash
# 跑全部测试（约 5000+ 个，耗时 ~40 秒）
python3 -m pytest -q

# 跑某个模块的测试
python3 -m pytest agent_py_agent/tests/test_subagent_*.py -q

# 跑压力测试（标记为 slow 的测试）
python3 -m pytest -m slow -q

# 查看覆盖率（需要先安装 pytest-cov）
pip install pytest-cov
python3 -m pytest --cov=agent_py_agent/agent --cov-report=term-missing -q
```

### 测试结构

```text
agent_py_agent/tests/
├── conftest.py                  # 共享 fixture（mock 对象、临时文件等）
├── test_subagent_*.py           # 子代理模块测试
├── test_manager_*.py            # 子代理管理器测试
├── test_gateway_*.py            # gateway 模块测试
├── test_session_*.py            # session 模块测试
├── test_memory_*.py             # 记忆模块测试
├── test_archive_*.py            # 记忆归档测试
├── test_capability_*.py         # 能力路由测试
├── test_auth_*.py               # 认证模块测试
├── test_notification_*.py       # 通知模块测试
├── test_adapter_*.py            # 通道适配器测试
├── test_cli_*.py / test_subcommands_*.py  # CLI 命令测试
├── test_e2e_*.py                # 端到端流程测试
├── test_scenario_*.py           # 场景测试
├── test_stress_*.py             # 压力测试
└── test_regression_fixes.py     # 已知 bug 回归测试
```

### 更新测试文档

当以下情况发生时，必须同步更新 [TESTS.md](TESTS.md)：

1. **新增测试文件** — 在 TESTS.md 的"测试文件清单"中添加条目
2. **删除或重命名测试文件** — 更新对应条目
3. **新增压力测试或回归测试** — 在对应章节添加说明
4. **覆盖率发生变化** — 更新覆盖率数据
5. **测试策略变更** — 更新"测试策略"章节

更新命令：

```bash
# 查看当前测试统计
python3 -m pytest -q --co | tail -1

# 查看测试文件列表
ls agent_py_agent/tests/test_*.py | wc -l
```

CI/CD 会在每次 push 时自动运行测试并检查 TESTS.md 是否与实际测试文件同步。

### CI/CD 状态

**查看 CI 运行状态**：
- 打开 GitHub 仓库页面 → Actions 选项卡
- 或直接访问：`https://github.com/<owner>/<repo>/actions`

**Badge 状态徽章**（添加到仓库 README 顶部）：

```markdown
[![Test](https://github.com/<owner>/<repo>/actions/workflows/test.yml/badge.svg)](https://github.com/<owner>/<repo>/actions/workflows/test.yml)
[![Lint](https://github.com/<owner>/<repo>/actions/workflows/lint.yml/badge.svg)](https://github.com/<owner>/<repo>/actions/workflows/lint.yml)
```

将 `<owner>` 和 `<repo>` 替换为实际的用户名和仓库名。

**PR 合并要求**：Push 到 main 或打开 PR 时，两个 workflow（Test 和 Lint）必须全部通过才能合并。

### 本地复现 CI 失败

如果 CI 失败，先在本地复现：

```bash
# 1. 拉取最新代码
git pull

# 2. 安装依赖
python3 -m pip install -e .

# 3. 运行完整测试（与 CI 同款）
python3 -m pytest -q --tb=short

# 4. 运行 lint 检查（与 CI 同款）
python3 scripts/check_doc_sync.py
python3 -m py_compile agent_py_agent/**/*.py
python3 -m py_compile scripts/*.py
```

如果本地测试通过但 CI 失败，常见原因：
- **Python 版本差异**：CI 使用 3.10/3.11/3.12，本地确认版本 `python3 --version`
- **依赖版本差异**：删除 `.venv` 重建，或 `pip install -e .` 重新安装
- **并行状态污染**：某些测试依赖串行执行，用 `python3 -m pytest -q --tb=short -p no:randomly` 禁用随机顺序

### 本地验证

```bash
python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py
# 编译核心 Python 文件，检查语法错误。

python3 -m agent_py_agent --help
# 直接用模块入口查看帮助，验证源码入口可用。

python3 scripts/live_agent_lab.py --suite smoke
# 跑不消耗真实 API 的冒烟测试。

python3 agent_py_agent/tests/run_tests.py
# 跑完整测试；当前测试策略可能调用真实 API，跑前确认 key 和成本。
```

---

## 文档导航

| 文档 | 说明 |
|------|------|
| [ONBOARDING.md](ONBOARDING.md) | **新 LLM / 新开发者开工指南** — 先读这个 |
| [AGENTS.md](AGENTS.md) | LLM 开发者行为规范 |
| [CODEBASE_TREE.md](CODEBASE_TREE.md) | 目录树逐文件说明 |
| [CLI_REFERENCE.md](CLI_REFERENCE.md) | CLI 命令和参数手册 |
| [docs/architecture/BOUNDARY_RULES.md](docs/architecture/BOUNDARY_RULES.md) | 分层导入矩阵 |
| [docs/architecture/MODULE_OWNERSHIP.md](docs/architecture/MODULE_OWNERSHIP.md) | 模块职责归属表 |
| [docs/development/DEVELOPMENT_RULES.md](docs/development/DEVELOPMENT_RULES.md) | 编码规则 |
| [TESTING_POLICY.md](TESTING_POLICY.md) | 测试分层和规范 |
| [CODE_SIZE_POLICY.md](CODE_SIZE_POLICY.md) | 代码尺寸限制 |
| [CLEAN_PACKAGE_POLICY.md](CLEAN_PACKAGE_POLICY.md) | 打包洁净度规范 |
| [docs/decisions/](docs/decisions/) | 架构决策记录 (ADR) |
