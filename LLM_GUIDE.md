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
8. **参考成熟项目先于自己发明**：底座以 会话运行时 为第一参考；会话、active turn、Compact、Skill、工具、计划、子代理、停止和引导只要 会话运行时 有明确实现，就适配本项目既有 owner/thread/task 事实源，不再并行造一套。长期助手 只补 会话运行时 覆盖较少的长期 Memory、Persona、多用户持久调度和被动验证；通道运行时 只补 IM adapter、通道健康和投递边界。

---

## 2026-08-21 当前运行基线

- 普通主代理和子代理共用 `turn_end.reason`：`completed` / `blocked` /
  `max-tokens` / `aborted` / `error` / `interrupted`。它只表示一轮为什么结束，
  不从模型正文、验收清单、产物数量或测试描述反推完成。
- 递归协作只有一个创建入口 `create_subagents`，创建成功后宿主立即自动启动。
  模型不再看到 `dispatch_subagents` 或 `schedule_child_subagents`；内部 dispatcher
  只是启动、恢复与有界重试引擎，不是人工推动工具。
- 子代理生命周期事件直接唤醒父级；不再为每批 child 登记周期性 LLM 巡场或 `wait` 推进。
  `send_guidance` 只接受一个直接下级 `target` 和一段 `message`，不支持广播、跨层催办或验收。
- 父代理对下级的日常控制面只剩创建、给一个直属下级发补充消息、打断/取消直属下级，以及处理
  直属下级的结构化 capability 请求。模型没有查树、等待、巡场或“再推动一次”工具；`/status`、TUI
  和恢复逻辑仍读取宿主内部代理树投影。模型的取消合同也不暴露 dry-run、进程参数或整树回执，
  防止把打断工具变成另一个巡检入口。
- 普通 child 自动继承直接父级的结构化工作区上界，孙代理逐层继承同一上界；不要求模型重复声明父级
  本来就能写的目录。`output_files` 记录用户明确交付目标和冲突锁，批量时由负责写入的 item 分别声明；
  它不能把父级工作区外的自然语言路径变成权限。命名 Audit/exact-scope worker 继续只用精确授权。
- 历史 `acceptance_checks` / `verification_status` 字段仅为旧账本可读兼容，不进入当前
  TaskEnvelope、runner 模型摘要、父级 wake 或树摘要，也不参与启动/完成判定。

---

## 项目结构速览

```
my-agent/                          ← 项目根目录
├── LLM_GUIDE.md                   ← 【你正在读的文件】LLM 入口
├── DESIGN_LEDGER.md               ← 设计台账（所有设计决策的来源）
├── STATUS.md                      ← 当前状态、测试基线、推荐下一步
├── GATEWAY_DESIGN.md              ← Gateway 架构设计
├── MEMORY_BACKLOG.md              ← 记忆系统痛点和设计原则
├── DISCUSSION_BACKLOG.md          ← 系统问题讨论
├── docs/
│   ├── ROADMAP.md                 ← 【必读】待做/进行中功能清单
│   ├── COMPLETED.md               ← 【必读】已落地功能清单
│   ├── design/                    ← 模块设计文档
│   │   ├── README.md              ← 模块设计文档索引
│   │   └── subagent-quality-contract.md
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
- 读 `SUBAGENT_RUNBOOK.md` 了解运行协议

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
| `GATEWAY_DESIGN.md` | Gateway 专项设计 | Gateway 架构变更时 |
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
6. **默认 dry-run**：所有调度/执行命令默认 dry-run，只有显式开关才改状态。
7. **不删文件**：action apply 不删除文件，不覆盖已有内容。
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
5. 跑 `python3 -m pytest -q` 确认全量通过。
6. 更新 `docs/ROADMAP.md`（改状态）和 `docs/COMPLETED.md`（新增记录）。
7. 如果涉及设计决策，更新 `DESIGN_LEDGER.md`。

### 我要修一个 bug

1. 读相关代码和测试。
2. 修 bug、补测试。
3. 跑 `python3 -m pytest -q` 确认全量通过。
4. 如果 bug 揭示了设计问题，更新 `DESIGN_LEDGER.md`。

### 我要重构代码

1. 读 `agent/DIRECTORY_GUIDE.md`，确认边界。
2. 读 `DESIGN_LEDGER.md` "代码体检与后续拆分计划"条目。
3. 只移动一个低耦合区域，同步迁移调用点，不新增旧入口转发壳。
4. 跑完整测试后再继续。
5. 不在同一轮同时改行为和大移动文件。

### 我不确定该不该改

1. 读 `docs/ROADMAP.md`，看优先级。
2. 读 `DESIGN_LEDGER.md`，看设计原则。
3. 读 `STATUS.md` "当前主要限制"，看是否在限制列表里。
4. 如果都不确定，在 `DISCUSSION_BACKLOG.md` 里记录问题，等确认后再动。
