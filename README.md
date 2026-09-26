# my-agent

一个可以长期值守的本地个人智能体：一台机器一个常驻 Gateway，终端 TUI 和飞书等通道都接到它上面；
主代理能拆出多层子代理协作，工具在系统沙箱里执行，插件可以用任何语言写，每一步都有可核对的结构化账。

[![Test](https://github.com/chaojixiaoyezi/my-agent/actions/workflows/test.yml/badge.svg)](https://github.com/chaojixiaoyezi/my-agent/actions/workflows/test.yml)
[![Lint](https://github.com/chaojixiaoyezi/my-agent/actions/workflows/lint.yml/badge.svg)](https://github.com/chaojixiaoyezi/my-agent/actions/workflows/lint.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

- 主链路只用 Python 标准库，Python 3.10+，macOS / Linux / Windows 都能装。
- 模型、密钥、权限都在 TUI 里配置，或者直接对代理说一句让它自己配；软件不预选任何模型。
- 面向“把任务交给它、过一会儿回来看结果”的使用方式，而不是一问一答的聊天窗口。

当前哪些能力稳定、哪些仍有边界，以 [产品能力与边界](docs/PRODUCT_FACTS.md) 为唯一权威；发布风险见 [STATUS](STATUS.md)。

## 它解决什么问题

普通聊天助手每次对话都从零开始，任务一长就丢上下文，跑命令没有隔离，做了什么也无法回溯。
my-agent 把这些当成底座问题来解：

| 你遇到的情况 | my-agent 的做法 |
| --- | --- |
| 任务要跑几十分钟，中途还要插话、改方向、停下来 | 常驻 Gateway 持有会话；TUI 可随时插话、停止、恢复，工作片冻结配置，后台续跑有唤醒链 |
| 一个人干不完，想拆给几个“同事”并行 | 主代理创建子代理、子代理再拆孙代理；每个运行都有身份、状态、终态和通知，父级按结构化事实调度 |
| 让模型跑命令、装依赖、开服务，怕它碰到不该碰的 | 命令走 bwrap（Linux）或 Seatbelt（macOS）沙箱，沙箱不可用就拒绝执行；后台服务默认只监听回环；owner 家目录墙 |
| 上下文越聊越大，图片、长文件、大输出把窗口撞爆 | 自动 Compact、含图历史的视觉摘要、大工具输出外置、按余量提前外置；主子孙代理各有自己的压缩协议 |
| 想扩展能力，又不想被 Python 绑死 | 插件是独立进程：Python、Go、Node 或任意可执行文件都行；装、配、启、停、卸、更新全在 TUI 里完成 |
| 事后想知道“它到底做了什么、为什么失败” | SQLite/FTS5 本地事实源、运行时账本、工具操作幂等与未知副作用对账、事件时间线、`my-agent status` / `timeline` / `local-doctor` |

## 能力一览

### 常驻 Gateway 与多客户端

- 每台机器一个 Gateway，服务多个用户和多个客户端；TUI、IM 通道、HTTP 入口都是它的客户端。
- 每个用户有自己的 canonical home（`~/.my-agent/owners/<provider>/<user>`），会话、记忆、模型目录、插件互相隔离。
- 停机时在途模型调用会被结清并留下结构化事实；启动时对遗留运行做对账，不把坏账当成“没有任务”。

### 终端 TUI

- 流式回答、工具记录、思考折叠、历史分页、拖选复制、粘贴多行。
- 插话、停止、恢复、目标模式与任务清单；子代理有独立子页面，可以进去看、插话、停止。
- `/model` 配模型、`/permissions`（或 F4）选权限模式、`/plugins` 管插件；插件面板显示在输入区上方。

### 模型接入

- 三种生成协议：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages；MiniMax、Qwen 以及任何兼容这三种协议的服务都能接。
- 账号登录：ChatGPT 订阅设备码授权与通用 OAuth 设备码流程，见 [模型账号登录](docs/design/MODEL_OAUTH.md)。
- 模型目录按用户私有保存；“选择已有模型”只改当前会话，“新会话默认模型”只改以后新开的会话；管理员可逐个共享模型，密钥留在服务端。
- 代理可以自己管目录：对它说“帮我加个模型”“这个会话换成 X”“测一下这个模型通不通”，它通过 `manage_models` 工具完成；删服务商需要你确认一次，密钥只在你那句话里出现一次，回执和归档都不回显。
- 可选的决策模型（TypeSafe 决策接口）：在有来源的候选之间做选择、分类、评分；它不是权限门、不是记忆事实源，也不裁判任务完成。

### 多层子代理

- 主代理按需创建子代理，子代理可递归协作；创建、进入、插话、停止、结果通知全部按结构化父子身份执行。
- 每次运行有 run / task / attempt 身份和账本；终态、通知、恢复都可去重、可重放。
- 子代理继承或显式指定本用户可用的模型，工具与文件范围只能收窄、不能越出父级。

### 工具与安全

- 文件读写、搜索、结构化补丁、大输出引用；批处理命令、交互 PTY、受管后台进程三类句柄分开管理。
- 命令沙箱：Linux 用系统或随包的 bubblewrap，macOS 用 Seatbelt；自检失败返回 `SANDBOX_UNAVAILABLE`，不会悄悄退回无隔离执行。
- 后台服务默认只监听 127.0.0.1；要对局域网开放，由用户长期授权一次，宿主每两秒复核监听范围，越界即结束。
- 三档权限模式：默认确认 / 自主工作 / Full Access（仅本机管理员）；审批可原地续跑，禁用工具、灾难命令保护和人格文件确认不受自主模式影响。
- 网络请求带私网、`file://` 等 URL 门；MCP 服务器和技能按当前工具快照加载。

### 插件系统

- 插件是独立 MCP 进程，通过本地包安装：Python 包用隔离 venv 运行；任意语言（Go、Node、脚本、可执行文件）用 v6 包格式，启用前给出确认码，解释器路径与哈希被钉住，被替换即拒绝启动。
- 逐次下发工作区读写范围，插件拿不到宿主其他目录；可选的进程级 OS 沙箱试点（`plugin_process_sandbox`，默认关）。
- 插件可以带 Skill、显示面板、调用宿主只读 API；停用、卸载、换代后下一次快照自然生效。
- 自带 13 个插件：`workspace-peek`（工作区预览）、`genui-lite`（表格与图表）、`browser-lite`（受控无头浏览器）、`image-text`（本地 OCR）、
  `savepoint-lite`（文件快照）、`design-lite`（HTML 设计稿）、`desktop-lite`（系统通知/剪贴板）、`harness-console`（工作台网页）、
  `context-inspector`、`activity-line`、`status-pet`、`worktable-lite`、`web-board`；另有 `hello-go`、`hello-node` 两个跨语言示例。
- 插件 SDK 与一致性测试套件见 [plugins/sdk](plugins/sdk/)，打包与生命周期见 [插件包](docs/design/PLUGIN_PACKAGES.md)、[任意语言插件](docs/design/PLUGIN_ANY_LANGUAGE.md)。

### 记忆、人格与技能

- 长期记忆走“候选 → 晋升”链，代理用 `remember` 保存“需要时才想起”的具体事实。
- 人格三件套 SOUL / USER / AGENTS 每轮注入：用户画像和长期工作约定由代理自主维护，改 SOUL 必须用户确认。
- Skill 三级加载：索引 → 正文 → 引用资源，优先级 workspace > user > builtin。
- 自学习默认关闭；开启后主代理完成的多轮工具任务会在后台自动总结成 owner 的 `skills/learned/` Skill，子代理经验提案也自动安装；不需要逐条确认，发布前经自动闸门（重名、安全扫描、脱敏、只改自己生成且你没改过的 Skill），每次写入都留账，可用 `my-agent skills learned` 回滚或删除。

### 上下文与压缩

- 同一用户同一会话只有一份 canonical transcript；展示分页、子代理通知、后台恢复都不另造模型历史。
- 达到阈值自动 Compact；含图片的历史用独立的视觉摘要请求处理；单条工具输出超过距压缩点的余量时立刻外置，模型只看预览和恢复锚点。
- Context、累计 token、缓存读写、输出速率是分开的指标，TUI 状态行逐项显示。

## 架构一览

```text
  终端 TUI        飞书等 IM 通道        HTTP / ASGI 入口        CLI (my-agent run/gateway ask)
      \                 |                     |                        /
       +----------------+----------+----------+-----------------------+
                                   |
                        Gateway（每台机器一个，常驻）
            会话与回合 · 审批 · 控制事件 · Compact · 唤醒/续跑 · 停机结清与启动对账
                                   |
        +--------------------------+---------------------------+
        |                          |                           |
   工具执行层                   子代理 runner                 插件 MCP 进程
   文件 / 命令 / PTY /          run·task·attempt 账本          Python venv 或
   后台进程宿主 / 网络 / MCP     递归协作 · 结构化通知          任意语言可执行文件
   bwrap / Seatbelt 沙箱                                       逐次工作区权限
                                   |
                     模型后端（OpenAI Chat / Responses / Anthropic / 决策模型）
                                   |
              ~/.my-agent：每个用户的 canonical home · runtime_db(SQLite/FTS5)
                         · 记忆 · 模型目录 · 插件环境 · 发布与回滚副本
```

## 快速开始

### 安装

推荐一键容器安装（Linux，或已装 Docker/Podman 的 macOS/WSL）：

```bash
curl -fsSL https://raw.githubusercontent.com/chaojixiaoyezi/my-agent/main/install.sh | bash
```

安装器构建受限容器，在容器内完成 bwrap 自检，再装一个透明的 `my-agent` 包装器：`~/.my-agent` 持久挂载，
当前目录作为唯一工作区挂入，不挂 Docker socket；自检失败就不会装出一个降级运行的版本。

宿主开发安装（macOS / Linux）：

```bash
cd /path/to/my-agent
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .        # 开发者用 -e ".[dev]"
my-agent --help
```

Windows PowerShell：

```powershell
cd C:\你的路径\my-agent
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1      # 提示不允许执行脚本时先运行 Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -U pip
python -m pip install -e .
my-agent --help
```

宿主 venv 只推荐开发使用：没有 bwrap 的 Linux 上，owner-scoped 命令会安全拒绝而不是无隔离执行。

### 第一次运行

```bash
my-agent
# 默认入口：确保后台 Gateway 存活，然后进入 TUI。
```

1. 在 TUI 输入 `/model`，新增一个服务商和模型：选择协议（Chat / Responses / Messages），填模型名、接口地址、密钥和上下文窗口，保存后选中。
   也可以直接对代理说“帮我加一个模型，地址是……，密钥是……”，它会自己写进目录并切换。
2. 输入 `/permissions` 或按 F4 选权限模式；默认是“默认确认”，敏感操作会问你。
3. 说一句任务试试，例如“看看当前目录有什么，写个 README 草稿”。

软件不预填任何模型或地址；未配置时可以打开 TUI 和设置，但不会调用任何模型，也不会退回到别的模型。

### 部署式配置（可选）

无人值守部署可以在 `agent_py_agent/config/agent_config.yaml` 里给出显式连接，密钥只放环境变量：

```yaml
model_backend: "anthropic_compatible"      # 或 openai_compatible / openai_responses
api_base: "https://api.example.com/anthropic"
model_name: "your-model"
model_context_window_tokens: 200000
api_key: ""
api_key_env: "AGENT_API_KEY"               # 只写“去哪个环境变量读”
```

```bash
read -rsp "AGENT_API_KEY: " AGENT_API_KEY; echo; export AGENT_API_KEY   # macOS / Linux，当前终端有效
```

```powershell
[Environment]::SetEnvironmentVariable("AGENT_API_KEY", "你的 key", "User")  # Windows，新窗口生效
```

显式部署连接会出现在 `/model` 的可选列表里，但用户自己保存的模型不受它影响。`.env` 不会被自动读取。

## 权限模式

输入 `/permissions`，或按 **F4** 打开菜单；审批框出现时也能按 F4。

| 模式 | 含义 |
| --- | --- |
| 默认确认 | 在自己家目录内工作，敏感工具操作询问用户 |
| 自主工作 | 在自己家目录内自动执行，主代理和子代理不逐次询问普通工具操作 |
| Full Access | 仅本机管理员可选，可访问家目录外，不逐次询问普通工具操作 |

选择保存到当前用户的工具策略，适用于该用户的会话与子代理；已挂起的审批可原地继续，路径权限在下一工作片生效。
`/permissions ask`、`/permissions auto`、`/permissions full-access` 可直接切换。

## 常用命令速查

完整参数见 [CLI_REFERENCE.md](CLI_REFERENCE.md)。下面每条后面标了是否会调用模型。

```bash
my-agent                                  # 默认入口：确保 Gateway 存活并进入 TUI
my-agent status                           # 全局总览：gateway、本地事实源、子代理、最近事件；不调用模型
my-agent status --json                    # 机器可读输出；不调用模型
my-agent timeline --limit 20              # 最近 20 条本地审计事件；不调用模型
my-agent run "总结当前项目状态" --no-save   # 脚本入口，前台跑一次请求；调用模型
my-agent gateway start|status|stop        # 后台 Gateway 启停与状态
my-agent gateway ask "继续推进当前任务"     # 给后台 Gateway 发同步请求；调用模型
my-agent gateway ask "跑个长任务" --no-wait # 异步投递，返回 request_id
my-agent gateway result <request_id>      # 读异步结果；不调用模型
my-agent gateway logs --lines 80          # 最近日志；不调用模型
my-agent feishu connect --scan            # 扫码接入飞书，自动建应用
my-agent memory-list --limit 20           # 最近长期记忆；不调用模型
my-agent memory-search "关键词"            # 搜索长期记忆；不调用模型
my-agent local-doctor                     # 诊断记忆、gateway、子代理、本地事实源是否一致；不调用模型
my-agent subagents                        # 子代理红绿灯看板；不调用模型
my-agent runtime-stale-attempts           # 列出无身份的悬挂运行轮，--settle 显式结清；不调用模型
my-agent skills learned list              # 查看自动总结的 Skill、待处理请求和今日调用次数；不调用模型
my-agent skills proposals list            # 查看子代理经验 Skill 提案；不调用模型
```

默认的长期数据都在当前用户的 owner home 下：

```text
~/.my-agent/owners/local/main/memory/long_term/memory.jsonl
~/.my-agent/owners/local/main/workspace/runtime/workspaces/<workspace-scope>/local_store/
~/.my-agent/owners/local/main/tasks/<yyyy-mm-dd>/<task-slug>/{output,work}/
```

无论从哪个 shell 目录启动，相对路径都从自己的 owner home 或任务目录解析，不会把源码树或 `/root` 自动变成工作区；
只有本机 `local/main` 显式选择 Full Access 才能访问外部目录，且子代理仍保持 owner/task 范围。

## 配置文件

- `agent_py_agent/config/agent_config.yaml`：主配置。模型连接、owner home 与记忆路径、Gateway 与 runner 参数、工具开关、
  Compact 与外置阈值、后台进程监听范围、决策模型开关、`enable_self_learning`（默认关）、`enable_model_profile_tool`（默认开）等，每项都有中文注释。
- `agent_py_agent/config/capability_config.yaml`：子代理能力路由、上抛层数、grant 上限、心跳与超时；数字限制项 `0` 表示不限制。
- 用户级配置：`~/.my-agent/config/`，模型目录在 `config/model-profiles/`（0700 目录、0600 文件），权限模式在各用户的 `tool_policy.json`。

## 安全边界

- 密钥不进仓库：模型密钥只在用户私有目录或环境变量里；工具回执、归档和日志按字段名脱敏。
- 命令必须隔离：多用户或 owner-scoped 的前后台命令必须经过 bwrap / Seatbelt；自检失败统一 `SANDBOX_UNAVAILABLE`，不允许无隔离回退。
- 后台服务默认回环：`background_listen_scope` 默认 loopback，开放局域网需用户一次性长期授权，宿主持续复核。
- 默认容器安装只挂当前工作目录和 `~/.my-agent`，不挂 Docker socket。
- 子代理只看得到执行上下文里授权的工具与技能；未授权工具在工具层被拒绝；子代理不能越出父级范围。
- 自学习只写自学目录（`skills/learned/`、`skills/lesson-*`），不改内置、共享、插件或你手写的 Skill；每次写入都留账、可回滚；人格文件 SOUL 的修改仍必须用户确认。
- 未知副作用不冒充成功：工具操作、进程终止、插件停用都要有可核对的回执，拿不到就记为未知并保留对账入口。
- 更多边界见 [产品能力与边界](docs/PRODUCT_FACTS.md)。

## 项目状态

- 十步重构 / 插件 Goal 的第 1—7、10 步已按真实 TUI 验收收口，第 8、9 步（模型与工具循环拆分、TUI 拆分）仍在进行，见 [执行 Goal](docs/tasks/REFACTOR_PLUGIN_GOAL.md)。
- 每次发布都要先过本地严格 gate（focused pytest、Ruff、文档同步、代码尺寸、diff、打包洁净度），再推送并双机同版部署；线上 CI 只作参考，不是验收来源。
- 真实验收方法、历史证据和已知问题见 [TESTS.md](TESTS.md) 与 [STATUS.md](STATUS.md)。

## 测试

```bash
python3 -m pytest -q -m "not slow and not e2e" --tb=short --timeout=60   # fast suite，CI 同款，托管 runner 上约 40 分钟
python3 -m pytest agent_py_agent/tests/test_plugin_*.py -q                 # 某个模块
python3 -m pytest -m slow -q                                              # 压力测试
python3 -m pytest agent_py_agent/tests/test_architecture_guardrails.py -q # 架构护栏，每次提交前都跑
```

- 1,100 多个测试文件、两万余项用例，覆盖合同单测、fake tool / fake LLM、回放和少量真实环境验收；真实 TUI 验收单独记录在 [TESTS.md](TESTS.md)。
- CI 在 Python 3.10 / 3.11 / 3.12 上运行 fast suite，`run_command` 用例在 runner 上真实使用 bubblewrap 沙箱；Full Tests 工作流每日定时运行，也可手动触发。
- 测试分层与规范见 [TESTING_POLICY.md](TESTING_POLICY.md)，提交前的严格 gate 见 [开发规则](docs/development/DEVELOPMENT_RULES.md)。

## 参与开发

- 新开发者（人或 AI）从 [ONBOARDING.md](ONBOARDING.md) 开始，规范在 [AGENTS.md](AGENTS.md) 和 [LLM_GUIDE.md](LLM_GUIDE.md)。
- 提交前必须通过本地严格 gate；PR 合并到 `main` 需要维护者审阅，首次贡献者的工作流需要维护者批准后才会运行。
- 并行开发线用独立 worktree，见 [WORKSTREAMS.md](docs/WORKSTREAMS.md) 和 [交接模板](docs/tasks/HANDOFF_TEMPLATE.md)。
- 发现安全问题请使用仓库的私密漏洞报告入口，不要开公开 issue。

## 文档导航

| 文档 | 说明 |
| --- | --- |
| [docs/PRODUCT_FACTS.md](docs/PRODUCT_FACTS.md) | 当前产品能力与边界（唯一权威） |
| [CLI_REFERENCE.md](CLI_REFERENCE.md) | 全部命令、TUI 斜杠命令与参数 |
| [docs/design/TUI_DESIGN.md](docs/design/TUI_DESIGN.md) | TUI 交互规范 |
| [docs/design/TUI_MODEL_PROFILES.md](docs/design/TUI_MODEL_PROFILES.md) | 模型目录、会话选择与代理自助管理 |
| [docs/design/GATEWAY_DESIGN.md](docs/design/GATEWAY_DESIGN.md) | Gateway 设计 |
| [docs/modules/subagent/SUBAGENT_RUNBOOK.md](docs/modules/subagent/SUBAGENT_RUNBOOK.md) | 子代理、能力路由与 runner 手册 |
| [docs/design/PLUGIN_PACKAGES.md](docs/design/PLUGIN_PACKAGES.md) · [PLUGIN_ANY_LANGUAGE.md](docs/design/PLUGIN_ANY_LANGUAGE.md) | 插件包、任意语言插件与沙箱 |
| [docs/architecture/MY_AGENT_HOME_LAYOUT.md](docs/architecture/MY_AGENT_HOME_LAYOUT.md) | `~/.my-agent` 家目录布局 |
| [DESIGN_LEDGER.md](DESIGN_LEDGER.md) | 设计想法、落地状态与后续方向 |
| [CODEBASE_TREE.md](CODEBASE_TREE.md) | 目录树逐文件说明 |
| [docs/ROADMAP.md](docs/ROADMAP.md) · [docs/COMPLETED.md](docs/COMPLETED.md) | 待做与已完成清单 |
| [docs/decisions/](docs/decisions/) | 架构决策记录 |

## 许可

Copyright 2026 超级小叶子 (chaojixiaoyezi)。代码按 [Apache License 2.0](LICENSE) 发布。随包提供的 bubblewrap 沙箱二进制保留其自身许可、源码归档与来源说明，见 [NOTICE](NOTICE) 与 `agent_py_agent/vendor/bubblewrap/`。
