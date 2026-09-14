# LLM_GUIDE

这份文档是给 LLM/AI 开发者读的项目入口指南。

**开工前必须读，收工后必须改。**

---

## 铁律

1. **开工前**：读 `docs/ROADMAP.md`，确认你要做的事在列表里，了解它的"解决问题"和当前状态。
2. **收工后**：更新 `docs/ROADMAP.md`（改状态或删除已完成条目），把已落地的功能写入 `docs/COMPLETED.md`。
3. **每个功能必须写"解决问题"**：不能只罗列模块名和 workflow 名，必须说明它解决哪个用户痛点、系统风险或交付缺口。
4. **不改设计文档就不能改设计**：如果实现方向与 DESIGN_LEDGER.md 有冲突，先更新设计文档再写代码。
5. **每次汇报必须带"建议下一步"**：父代理、子代理、其他线程和交接文档都要说明推荐后续动作、并行边界和风险守门点。
6. **自然语言不当机器事实源**：用户 prompt、模型 summary、报告正文、guidance、角色描述只能做沟通和软引导；状态、权限、验收、恢复、派工和产物归属必须来自结构化字段、refs、工具结果、显式配置或文件系统事实。
7. **主链路优先，兜底最后**：主链路没跑顺前不要加 fallback、旧路径兼容、影子入口、只转发 facade 或“还能跑”的旁路；确定不用的旧兼容要删。
8. **复用已有底层协议**：会话、active turn、Compact、Skill、工具、计划、子代理、停止和引导统一使用本项目既有 owner/thread/task 事实源；长期记忆、人格、多用户调度和 IM 适配各守模块边界，不为同一概念再建一套状态机。

---

## 当前运行边界（验收状态看 STATUS）

SSE delta 原样保留，OpenAI 工具参数生成有独立进度；
派工不会永久禁止主代理本地工作，用户明确限制仍保留；复制按最新代次和实际通道结果反馈。

普通 assistant 的原生思考也须跨轮回放，
请求拒绝不等同密钥错误；前台/背景展示交接只按宿主 request ID，不按相同文本删除消息。
长选区的 OSC 52 长度预算不影响 native/tmux stdin；未知上游 400 不以轮换 session header 自动重试。
内部 runtime_fact 同样区分请求拒绝与配置错误；typed 不可重试事实不能被错误正文覆盖。全选与复制绑定当前视口。

本入口只保留现行规则与导航；当前设计、已实现内容和开放问题分别见 `DESIGN_LEDGER.md`、
`docs/COMPLETED.md` 和 `STATUS.md`。旧轮次的测试结果不替代当前发布验收。

- 每台机器一个 Gateway，多个 TUI 是独立客户端/会话。wheel 使用 non-editable 独立 runtime，
  Gateway 与默认 TUI 入口必须同版；检查 executable、module.__file__、安装位置和实际配置，保留回滚。
- 常规真实模型验收默认官网 MiniMax-M2.7；检查实际 provider/端点，不按同名模型推断。
  用户明确指定的其它协议/模型单独验证；私有凭据不进仓库、不输出，不静默切用户日常模型。
- 产品名与发布库是 my-agent；品牌清理不搬迁检出、owner home、真实 tmux 或任务数据。
- `/model` 管理 owner 私有 provider/model v2；获取目录、短测必须显式操作。保存/编辑不调用模型。
  新工作片冻结模型/端点/密钥/容量/请求头整组配置，子孙继承创建时引用，显式 model 只解析本 owner 配置。
  Auth 仍预留；Embedding 只管理目录用途，不能选作主子模型。详见 `docs/design/TUI_MODEL_PROFILES.md`。
- `/permissions` / F4 使用 owner tool_policy.json 的 ask、auto、full-access；Full Access 仅可信管理员。
  工具执行线程继承工作片 Context；角色、工具可用性、Full Access 都不增加用户未要求的工作目标。
  capability grant 与具体工具批准是不同事实；SOUL 专用确认、owner 墙、灾难保护仍有效。
- 普通主子代理默认在 canonical owner home 工作；tasks/output/work 只作整理建议，不形成目录锁。
  执行 cwd 与内部 owner_runs_dir 分离，后台恢复不能继承 daemon 的 cwd。用户明确外部 cwd 仍经权限门。
  文件整理/命名共用 home_context_enabled 与 workspace_task_path_template 软提示，不靠标题猜运行身份。
- 每个 owner/thread 只有一份 canonical transcript；task link、child wake、展示投影不得建立第二份模型历史。
  新消息只追加，普通轮不改旧缓存前缀。任务切换不删除历史/记忆，真正摘要替换只在 Compact。
  详见 `docs/design/CONVERSATION_CONTEXT_DESIGN.md`。
- main/child/grandchild 各自用 ConversationThread 的 checkpoint + generation CAS 提交 Compact。
  失败/停止不推进代次，不把普通窗口化计成压缩。源历史/精确工具账保留；已退休前缀只能随已提交摘要回收。
  空摘要、失败熔断、重放和取消以当前实现与该模块文档为准，不新增旁路摘要或独立计数。
- Context 是本线程模型 preflight 压力，不是累计计费。压缩开始的实际窗口由压缩模块提供，
  先保存同代数值再通知 TUI；成功提交清旧数字。显示遥测、计数和进度不得进入模型输入/缓存前缀。
  ModelCallLedger 是成本唯一权威；缓存命中以 provider usage 为准，不能用估算或比例推测账单。
- 大窗口切小窗口用有预算的连续分段摘要；覆盖所有来源后才提交，typed overflow 可缩小请求，
  网络/认证错误不伪装成超窗。observed_tool_paths 仅是有界查找提示，不是权限或文件存在证据。
  能容纳的单次请求保留原缓存面；分段采用无执行工具的摘要角色。原文锚点在固定预算内优先保留用户原话，
  助手长结论不能挤掉短用户要求；超预算仍有明确省略，原 transcript 不删除，不承诺摘要无损。
- 原生请求保持稳定 system/tools、已提交摘要/历史、当前 user、append-only native IR、动态事实尾部顺序。
  一个 user 只出现一次；模型/工具快照必须与真实请求一致。Provider system 是授权/验证软指导的唯一正文，
  不能逐工具重复长规则。实际 native Schema 必须与冻结工具快照相同。
- 连接/首事件/流间隔超时分开，健康慢流没有隐式总墙钟；停止贯穿请求和退避。
  明确 HTTP 400 不因缺少解释自动重试；429、5xx、网络瞬断仍走 typed 有界恢复。
- 采样 top_p 是可选配置：普通端点默认省略，模型级覆盖与连接同快照、同缓存键。
  已核对的精确官方/工具运行时 V4 Flash Chat 采用 0.95；用户显式温度不被删除。
  不按任意模型名或代理猜默认，不以调整采样代替 400 根因诊断，详见 `docs/design/TUI_MODEL_PROFILES.md`。
- 工具操作身份、owner、run/task/parent/root 和副作用结果均读取结构化事实。
  明确零副作用失败可交模型修参；部分写入/执行效果未知仍保持 UNKNOWN，不自动重放。
  Shell/controlled_exec 是非交互批处理（stdin=DEVNULL），交互用独立 PTY；后台句柄不是 PTY 句柄。
- 普通子代理直接创建并自动运行；角色/权限快照决定是否可递归，不能解析 goal 扩权。
  正常进度靠直属生命周期事件；list_agents 只供按需查看，不轮询或手工推动。
  用户插话/停止/查看走同一 owner-scoped 结构化控制协议。详见 `docs/modules/subagent/SUBAGENT_RUNBOOK.md`。
- 派工以显式 covers 绑定原 Todo，output_files 可选且不授予额外权限；共享项目目录不代表冲突。
  同一 operation 重放回原回执，普通不同创建不能因 IO 路径相同被合并。依赖/独占写集由模型合理分工，
  不能用自然语言猜依赖、自动生成目录锁或自动派整批 QA/repair。
- Todo 是软计划，不是完成验收；普通 final 不因未勾完而被挡或暗中续跑。只有显式 /goal 和既有
  child 等待、审批、UNKNOWN 保留各自生命周期。UI 清单读 display_plan 的 exact generation/revision；
  历史页只恢复正文，不能用旧 Todo 覆盖当前计划。
- 模型不再请求工具就结束本轮；turn_end.reason 不证明项目完成。失败原因、未验证范围需模型如实说明，
  编译/旧版本成功不能代替新版启动和业务验证；不能拿无响应、空日志猜成沙箱限制。
- 用户可见 commentary、thinking、tool、final 以 typed block/message/request ID 同步主子页面与 canonical
  display 账；display 不进入模型/Compact/Memory。分页和实时游标分开，Ctrl+O 不切换代理身份。
  Working 只由真实活动驱动，Todo 未勾完不能维持动画；流关闭不能判断任务/工具成功。
- 思考结束默认折叠，正文/final 完整显示；空思考占位可撤下，完整思考保持原顺序，不能在 final 后重放。
  每个主子页面独立滚动锚点，手动上翻不追尾，回到底部或发送消息才恢复跟随；滚轮每格一行。
- 长期记忆只在本 owner 维护，USER/AGENTS 与普通 memory 可自主更新；SOUL 修改仍需用户本人确认，
  不允许文件工具绕过。Skill 逐轮冻结索引、按需读正文；自学习默认关，仅候选可自动生成，正式 Skill 要确认。
- 生产包不得带开发验收 harness、tests、运行数据或废弃源码。包边界、import 边界与定向测试都要过；
  不通过增加 baseline、skip/xfail 或删除真实失败证据“清绿”。完整发布门见 AGENTS.md。

---

## 项目结构速览

```
my-agent/                          ← 项目根目录
├── LLM_GUIDE.md                   ← 【你正在读的文件】LLM 入口
├── DESIGN_LEDGER.md               ← 设计台账（所有设计决策的来源）
├── STATUS.md                      ← 当前状态、测试基线、推荐下一步
├── docs/design/GATEWAY_DESIGN.md  ← Gateway 架构设计
├── MEMORY_BACKLOG.md              ← 记忆系统痛点和设计原则
├── DISCUSSION_BACKLOG.md          ← 系统问题讨论
├── docs/
│   ├── ROADMAP.md                 ← 【必读】待做/进行中功能清单
│   ├── COMPLETED.md               ← 【必读】已落地功能清单
│   ├── design/                    ← 模块设计文档
│   │   ├── README.md              ← 模块设计文档索引
│   │   └── CONVERSATION_CONTEXT_DESIGN.md
│   └── modules/                   ← 模块四件套（discussion/progress/purpose/structure）
│       ├── README.md
│       ├── subagent/
│       ├── memory/
│       ├── gateway/
│       └── live-lab/
├── agent_py_agent/                ← Python 包源码
│   ├── README.md                  ← 包级使用说明
│   ├── __main__.py                ← CLI 入口
│   ├── agent/                     ← 核心 agent 代码
│   │   ├── DIRECTORY_GUIDE.md     ← 【必读】目录职责地图
│   │   ├── agent_core/            ← 主代理编排
│   │   ├── backends/              ← 模型后端适配
│   │   ├── capability/            ← 能力路由
│   │   ├── gateway_parts/         ← Gateway 文件协议
│   │   ├── io/                    ← 底层 I/O 原语
│   │   ├── local_storage/         ← SQLite 本地事实源
│   │   ├── memory_store/          ← 记忆存储
│   │   ├── memory_archive/        ← 记忆冷归档
│   │   ├── memory_routing/        ← 长期规则路由
│   │   ├── prompting_parts/       ← Prompt 构造
│   │   ├── settings/              ← 配置
│   │   ├── subagents/             ← 子代理领域
│   │   ├── subagent_workflows/    ← Workflow 模板
│   │   └── tooling/               ← 工具实现
│   ├── cli/                       ← CLI 子命令
│   ├── config/                    ← YAML 配置文件
│   ├── data/                      ← 运行时数据和 prompt 模板
│   ├── tests/                     ← 测试
│   └── prompts/                   ← Prompt 模板
└── scripts/                       ← 工具脚本
```

---

## 开工前：必读清单

按顺序读，每个文件解决一个问题：

| 顺序 | 文件 | 解决什么问题 |
|------|------|-------------|
| 1 | `LLM_GUIDE.md` | 你在哪、该读什么、怎么改 |
| 2 | `docs/ROADMAP.md` | 要做的事在不在列表里、当前状态 |
| 3 | `DESIGN_LEDGER.md` | 设计决策来源、是否有冲突的设计原则 |
| 4 | `agent/DIRECTORY_GUIDE.md` | 代码放哪里、边界是什么 |
| 5 | `docs/design/` 对应模块 | 模块级详细设计（如果改动涉及特定模块） |
| 6 | `docs/modules/<module>/02-progress.md` | 模块最近推进了什么 |
| 7 | `docs/modules/<module>/04-structure.md` | 模块结构和核心文件 |

如果改动涉及测试：
- 读 `TESTS.md` 了解测试策略
- 读 `TEST_CHECKLIST.md` 了解收口检查项

如果改动涉及 subagent/capability/runner：
- 读 `docs/modules/subagent/SUBAGENT_RUNBOOK.md` 了解运行协议

---

## 收工后：必改清单

### 1. 更新 `docs/ROADMAP.md`

- 如果你完成了某个条目：把状态改为"已落地"，或直接删除条目（已移入 COMPLETED.md）。
- 如果你在做某个条目但没做完：更新"当前进展"和"待做"。
- 如果你发现了新问题或新需求：在 ROADMAP.md 末尾新增条目，必须写"解决问题"。
- 如果你发现设计冲突：先更新 DESIGN_LEDGER.md，再更新 ROADMAP.md。

### 2. 更新 `docs/COMPLETED.md`

- 新增一条记录，包含：解决问题、落地内容、验证方式。
- 不要写太长，每条 10-20 行足够。

### 3. 更新 `DESIGN_LEDGER.md`（如果涉及设计决策）

- 新增条目写清楚：日期、状态、摘要、已落地、后续方向。
- 超过 100 行的细节拆到 `docs/design/<module>.md`。

### 4. 更新 `STATUS.md`（如果改动影响测试基线或可用能力）

- 更新测试状态（通过数量）。
- 更新"当前可用能力"或"当前主要限制"。
- 更新"最新恢复入口"（如果改动影响恢复链路）。

### 5. 更新模块四件套（如果改动涉及特定模块）

- `docs/modules/<module>/02-progress.md`：记录本次推进。
- `docs/modules/<module>/04-structure.md`：如果结构变了。

### 6. 写清本轮汇报的下一步

- 最终回复、handoff、子代理报告都要包含 `建议下一步`。
- 建议要能直接指导下一位开发者行动：先做什么、能不能并行、不要碰哪些用户本地改动、需要跑哪些验证。
- 如果已经完成到可暂停，也要明确建议是等待评审、合并、观察 CI，还是进入下一阶段。

---

## 编码规范

### 注释格式（两层注释）

```python
# LLM: technical summary of the contract, side effects and invariants.
# 函数用途: 人能看懂的用途、调用时机和修改注意事项。
def example(...):
    ...
```

- 每个功能代码里的 module / class / function / method 都要有这类双层注释。
- module 使用 `模块用途:`，class 使用 `类用途:`，function 和 method 使用 `函数用途:`。
- class / def 有装饰器时，注释放在第一行装饰器上方。
- `LLM:` 给后续模型读，写清 contract / invariant / side effects / caller expectations。
- `模块用途:`、`函数用途:` 或 `类用途:` 给人读，用大白话说明用途、调用入口、修改时要检查什么。
- 有副作用的函数必须写清：会不会写文件、调用 API、启动进程、改状态。
- 测试函数不强求长注释。
- 批量补注释后必须跑 `compileall`、`ruff` 和 `scripts/check_code_size.py`；注释不计入实现行数，但语法位置和装饰器位置必须正确。

### 测试要求

- 新增测试用 `test_*.py` 或 `test_` 前缀，`run_tests.py` 会自动发现。
- 完整冒烟默认调用真实 API，echo/fake backend 只用于纯函数定位。
- 冒烟测试必须隔离 workspace，不污染开发仓库。
- 测试通过后更新 `STATUS.md` 的测试数量。

### 代码放置决策

按这个顺序问：

1. 外部协议客户端？→ `backends/` 或 `clients/`
2. 主代理流程编排？→ `agent_core/`
3. 子代理领域规则？→ `subagents/`
4. 工具实现或安全边界？→ `tooling/`
5. Gateway 文件协议？→ `gateway_parts/`
6. 本地事实源持久化？→ `local_storage/`
7. 记忆存储？→ `memory_store/`
8. Prompt 上下文构造？→ `prompting_parts/`
9. 配置 schema？→ `settings/`
10. 底层无业务含义的 I/O？→ `io/`

都不是？先写清楚变化原因，再决定是否需要新目录。不要塞进 `utils/common/shared`。

---

## 文档体系

### 设计层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `DESIGN_LEDGER.md` | 设计决策主台账，只放摘要和导航 | 每次设计决策变更 |
| `docs/design/*.md` | 模块级详细设计 | 模块设计变更时 |
| `docs/design/GATEWAY_DESIGN.md` | Gateway 专项设计 | Gateway 架构变更时 |
| `*_BACKLOG.md` | 痛点收集和设计原则 | 发现新痛点时 |

### 状态层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `STATUS.md` | 当前状态、测试基线、推荐下一步 | 每次测试基线或能力变更 |
| `docs/ROADMAP.md` | 待做/进行中功能清单 | 开工前读后、收工后改 |
| `docs/COMPLETED.md` | 已落地功能清单 | 功能完成时 |

### 运行层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `SUBAGENT_RUNBOOK.md` | subagent/capability/runner 运行协议 | 主链路变更时 |
| `TESTS.md` | 测试策略 | 测试策略变更时 |
| `TEST_CHECKLIST.md` | 收口检查项 | 新增检查项时 |
| `CLI_REFERENCE.md` | CLI 命令参考 | 新增/变更命令时 |
| `CODEBASE_TREE.md` | 代码树说明 | 结构变更时 |

### 模块四件套（docs/modules/<module>/）

| 文件 | 用途 |
|------|------|
| `01-discussion.md` | 灵感和讨论 |
| `02-progress.md` | 推进、解决的问题和测试 |
| `03-purpose.md` | 初心和设计想法 |
| `04-structure.md` | 结构树、核心文件、数据流 |

同步门：`scripts/check_doc_sync.py` 会检查 covered module 的文档是否与代码同步。

---

## 设计原则

从 DESIGN_LEDGER.md 提炼的核心约束：

1. **递归协作者模型**：主代理、子代理和孙代理共用同一轮模型/工具循环；差异只来自结构化父子身份、权限上界和工作区。
2. **自然完成**：模型不再请求工具时结束本轮；宿主只记录 `turn_end.reason`，不在回复后追加机器质量验收。
3. **事实与质量分开**：路径、权限、工具终态和产物存在性仍是结构化事实；“做得够不够好”由模型/用户继续复核，不设通用硬门。
4. **patch 不自动应用**：patch 审核器只审核状态，不自动应用 diff（除非有独立审核链路）。
5. **自学习必须用户确认**：学习候选必须经过用户确认才能变成正式 skill。
6. **执行模式以入口合同为准**：TUI 正常委派会实际执行；显式 dry-run 的管理入口只做预览，不能把旧管理命令默认值推广到所有任务。
7. **删除与覆盖受权限和副作用合同控制**：不能对用户宣称全产品“不删文件”；高风险目标需明确授权和精确路径，失败不伪装成功。
8. **文件第一事实源**：机器判断必须读 JSON/JSONL，Markdown 台账不是事实源。
9. **每个功能写"解决问题"**：不能只罗列模块名。
10. **通道故障 ≠ 任务失败**：runtime/session/adapter 故障不等于任务本身失败。

---

## 常见场景

### 我要加一个新功能

1. 读 `docs/ROADMAP.md`，确认是否已有相关条目。
2. 读 `DESIGN_LEDGER.md`，确认没有设计冲突。
3. 读 `agent/DIRECTORY_GUIDE.md`，确认代码放哪里。
4. 写代码、写测试。
5. 先跑相关定向测试，再做真实 TUI 验收；全仓 pytest 频率遵守 AGENTS.md，不因新功能机械重复全仓。
6. 更新 `docs/ROADMAP.md`（改状态）和 `docs/COMPLETED.md`（新增记录）。
7. 如果涉及设计决策，更新 `DESIGN_LEDGER.md`。

### 我要修一个 bug

1. 读相关代码和测试。
2. 修 bug、补测试。
3. 跑相关定向测试并复验原真实 TUI 失败样本；不能把定向通过写成端到端通过。
4. 如果 bug 揭示了设计问题，更新 `DESIGN_LEDGER.md`。

### 我要重构代码

1. 读 `agent/DIRECTORY_GUIDE.md`，确认边界。
2. 读 `DESIGN_LEDGER.md` "代码体检与后续拆分计划"条目。
3. 只移动一个低耦合区域，同步迁移调用点，不新增旧入口转发壳。
4. 跑直接调用方和相邻模块定向测试，远端提交前执行 AGENTS.md 全部严格 gate。
5. 不在同一轮同时改行为和大移动文件。

### 我不确定该不该改

1. 读 `docs/ROADMAP.md`，看优先级。
2. 读 `DESIGN_LEDGER.md`，看设计原则。
3. 读 `STATUS.md` "当前主要限制"，看是否在限制列表里。
4. 如果都不确定，在 `DISCUSSION_BACKLOG.md` 里记录问题，等确认后再动。
