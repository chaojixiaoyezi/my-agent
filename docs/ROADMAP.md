# ROADMAP

状态标记：

```text
设计中       已形成方向，还没写代码
部分落地     已有基础结构，但还没完整接入运行链路
待验证       已写代码，但还需要真实场景验证
暂停         暂时不做，但保留背景
```

每条记录必须包含"解决问题"，说明它解决哪个用户痛点、系统风险或交付缺口。

更新时机：开工前从这里领取任务，收工后更新状态或移到 COMPLETED.md。

---

## 下一版优先级

来自 STATUS.md，当前最急迫的四项：

### 1. Workflow Router/Compiler 集成

状态：部分落地

解决问题：用户必须手写 goal 和 acceptance，workflow 集成后可以从模板自动生成 worker spec 和验收条件，减少人工配置，缓解"派错工、漏验收、机械 PASS、fake done"。

当前进展：
- `AgentConfig` 已有 `subagent_workflow_mode: auto | manual | off`
- `agent_py_agent/agent/subagent_workflows/` 已有模板加载和校验
- `QualityContract`、`ContextManifest`、`context_packs` 已在 `SubAgentTask`/`SubAgentExecutionContext` 中
- `create_run()` 已有 `workflow_mode` 参数，但存在 `task.raw_json` bug（SubAgentTask 没有该字段）
- 没有调用方传 `workflow_mode != "off"`，功能处于休眠状态

待做：
- 修复 `task.raw_json` bug
- 至少一个调用方传 `workflow_mode`
- Workflow Router：自然语言任务 → 匹配 workflow 模板
- Workflow Compiler：模板 → worker spec + acceptance checklist
- Parent Gate：派工前生成可执行计划和父级验收闸门

设计文档：[docs/design/subagent-quality-contract.md](design/subagent-quality-contract.md)
设计台账：DESIGN_LEDGER.md "Subagent 质量契约与用户少说派工"

### 2. Patch 自动应用

状态：设计中

解决问题：runner 输出的 patches 目前只是计划/状态记录，必须由父代理或集成器验收后手动处理，无法自动应用和集成验证。

**注意**：当前设计原则明确"patch 审核器不自动应用 diff"、"patch apply 需要独立审核链路"。如需打破此原则，必须先更新设计文档，增加 allowlist、沙箱、回滚等约束。

当前进展：
- `PatchReviewRecord` / `PatchReviewReport` 已有
- `subagents-patches` 可以审核 runner 声明的 patch 状态
- 验收器会检查 `patches_reviewed` 和 `patch_status_valid`

待做：
- 设计独立 patch apply 审核链路
- 命令 allowlist 和超时审计
- diff 审计和写入边界
- 集成验收（apply 后跑测试）

设计台账：DESIGN_LEDGER.md "真实 API E2E、测试隔离和 patch 审核链"、"Subagent Runner Entry"

### 3. Lessons 自动生成自学习草稿

状态：设计中

解决问题：runner 输出的 lessons 只写到 output.json 和 DEBRIEF.md，无法聚合、无法被后续任务引用、无法沉淀成 skill。

设计原则：必须走"生成候选草稿 → 用户确认 → 进入 skill"路径，不能自动提升。`enable_self_learning` 配置开关已存在但代码未实现。

当前进展：
- runner 结构化输出中 `lessons` / `next_actions` 已写进 `output.json` 和 `DEBRIEF.md`
- `enable_self_learning` 配置开关存在，默认关闭
- `MEMORY_BACKLOG.md` 明确"自学习先不开发，只预留接口和沉淀队列位置"

待做：
- `agent_py_agent/data/learning_drafts/` 保存学习候选草稿
- 从 runner output.json 提取 lessons → 生成 LearningCandidate 结构
- `/learn` 查看候选、`/learn accept <id>` 确认写入 skill、`/learn reject <id>` 拒绝
- 与 capability_gap 关联：未解决或反复出现的问题自动生成候选

设计文档：[docs/design/subagent-quality-contract.md](design/subagent-quality-contract.md)
设计台账：DESIGN_LEDGER.md "自学习"

### 4. 并行 Worker Pool

状态：部分落地

解决问题：当前 `threading.ThreadPoolExecutor` 默认 concurrency=1，多任务串行执行，效率低。`runner_concurrency` 和 `runner_start_rate` 配置已存在但未接到后台 worker pool。

当前进展：
- `runner_concurrency` 配置已存在，默认 `auto -> 1`
- `runner_start_rate` 配置已存在
- gateway 请求队列已有保守 worker pool（默认 1 个 worker）
- `subagents-dispatch --apply --execute-runners` 已能执行 runner

待做：
- 把 `runner_concurrency` 接到后台 worker pool
- 进程级隔离、session 复用
- 超时硬中断和跨进程锁
- 自适应并发和 per-model 限流
- API 预算、启动速率策略

设计台账：DESIGN_LEDGER.md "Gateway 参数分层"、"本地恢复、诊断、worker 和 adapter 第一版"

---

## 设计中（未开工）

### Memory 第一批痛点归档

状态：设计中

解决问题：记忆系统的层级、召回、任务状态、flush、lesson 抽象和未来 skill 沉淀之间没有稳定同步。

待做：memory 最小闭环、分层和写入验证规则。

设计台账：DESIGN_LEDGER.md "Memory 第一批痛点归档"

### Memory 全量归档等级与压缩前 Hook

状态：设计中

解决问题：多轮上下文压缩后会遗忘，压缩前必须有 hook 保存结构化恢复快照。

待做：`memory_archive_level` 配置项、compression snapshot 存储格式、脱敏策略。

设计台账：DESIGN_LEDGER.md "Memory 全量归档等级与压缩前 Hook"

### Memory 长期规则索引化与强制路由

状态：设计中

解决问题：长期规则不能一条条塞进常驻 memory，需要确定性 memory router 在命中时才注入。

待做：`MemoryRoute` 数据结构、`memory-route` 最小版本、接入 chat/gateway/任务恢复。

设计台账：DESIGN_LEDGER.md "Memory 长期规则索引化与强制路由"

### Memory 压缩方式调研

状态：设计中

解决问题：直接截断太危险，纯摘要会漂移，纯 RAG 不可靠。需要组合策略。

待做：compression snapshot JSON schema、摘要 prompt、token budget 估算器。

设计台账：DESIGN_LEDGER.md "Memory 压缩方式调研"

### 用户真实痛点：Subagent 假完成与失控

状态：设计中

解决问题：Fake Done、父会话放养、子代理静默/卡死、输出不落盘、缺独立验收层等系统性问题。

待做：自动周期 due-check、自动心跳、真实入口验收、URL 来源核查、截图/日志证据自动执行器。

设计台账：DESIGN_LEDGER.md "用户真实痛点：Subagent 假完成与失控"

### 注释重构规范

状态：设计已记录，待分阶段落地

解决问题：大量函数 docstring 是一句话说明，缺少 LLM/人类双层说明，新接手的 AI 或开发者难以理解副作用和边界。

待做：按模块分阶段补齐双层注释（config → tools → core → CLI → subagent）。

设计台账：DESIGN_LEDGER.md "注释重构规范"

---

## 部分落地（有骨架但未完整接通）

### Subagent Channel Probe

状态：部分落地

解决问题：通道故障和任务失败必须分开判断，接管前先确认 workdir、机器 JSON、写入现场是否正常。

已有：本地工单现场 probe、channel_status 状态机、CLI 命令。

待做：模型接口 probe、ACP/adapter/session probe、派工前自动 probe 和失败阻断。

### Grant 注入执行上下文

状态：部分落地

解决问题：capability grant 不能只停留在运行记录里，必须能变成子代理实际可读取的执行包。

已有：`SubAgentExecutionContext`、`build_execution_context()`、`write_execution_context()`、CLI 命令。

待做：grant 后自动重新唤醒子代理、按 token 预算裁剪、子代理写 capability request 统一入口。

### 层级能力上抛

状态：部分落地

解决问题：子代理遇到问题不应自己全局搜索 skill/tool，应描述能力缺口，由父代理发现和下发。

已有：`CapabilityRequest`/`CapabilityGrant`/`CapabilityGap`、单个子代理 runner 入口、capability request 路由。

待做：跨层级自动上抛、根据 route 结果自动重新唤醒子代理、tool fallback group 和 failure mode 参与排序。

### 本地恢复、诊断、worker 和 adapter 第一版

状态：部分落地

解决问题：LocalStore 一致性诊断、gateway 请求崩溃恢复、adapter 文件协议。

已有：`local-doctor`、`local-rebuild`、gateway failed 归档、processing lease、保守 worker pool、`adapter file`。

待做：LocalStore compact/backup/export、gateway 请求取消/优先级/租约续期、runner 进程级隔离、adapter HTTP/WebSocket 版。

### 可见真实环境测试台 Live Lab

状态：部分落地

解决问题：开发 agent 不能只靠单元测试，需要能在用户看得见的终端里跑真实 runtime。

已有：`live_agent_lab.py`、隔离配置、命令执行、transcript 和 summary、`open_live_lab.sh`。

待做：memory 长任务、tools 边界任务、问题任务和多轮恢复任务场景。

### 并行开发 Workstream 工作台

状态：部分落地

解决问题：多个 AI 可以并行，但必须先把目录、分支、职责边界和交接格式定清楚。

已有：`WORKSTREAMS.md`、`HANDOFF_TEMPLATE.md`、worktree 创建/状态/打开脚本。

待做：`workstream_sync.sh`、`workstream_handoff_check.sh`、主线集成固定 checklist。

### 外部痛点文档：DISPATCH_PAIN_POINTS / PAIN_POINTS

状态：已记录，部分约束已落地

解决问题：workdir 不可靠、子代理启动后不建工单、completion notification 不等于完成、BLOCKED 不能当终点等系统性痛点。

已落地：标准工单目录模板、`ACTION_RECEIPTS.md`/`TEST_CHECKLIST.md`/`BUGS.md`/`SKILL_USAGE.md`/`HANDOFF.md`。

未落地：顶层任务现场模板、SPEC→TEST_CHECKLIST 追踪链、Tool fallback log、Browser/PWA/Game 自动验收器、数据规模断言器、安全任务隔离策略。

---

## 待验证

### chat 交互体验：中文输入和后台输出

状态：待验证

解决问题：`input()` 和后台线程同时输出时破坏输入行，中文删除有视觉残留。

待做：确认 `prompt_toolkit` + `patch_stdout()` 在各终端下稳定。
