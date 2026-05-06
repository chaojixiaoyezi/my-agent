# Subagent 专属开发路线

## 核心判断

我们要做的 subagent 不是“多开几个聊天分身”，而是一个可管理的任务树系统。

每个 subagent 都应该是一个可查询、可恢复、可验收、可上抛、可被父级观察的任务节点。主 agent 不靠聊天上下文记全局状态，而是从任务注册表、事件流、证据包和验收报告里实时读取事实。

这份路线只描述开发顺序和交付边界，不改动既有需求、痛点和设计动机。

## 目标形态

```text
用户
  -> Commander / 父会话
  -> Task Tree / AgentRun Registry
  -> Scheduler / Dispatch
  -> Coordinator / Worker / Verifier / Rescue
  -> Evidence Packet / Artifact / Finding
  -> Acceptance Report / Status Report
```

核心原则：

- subagent 是任务节点，不是一次性工具调用。
- 原始事实放在文件、LocalStore、artifact store 或日志数据面，不塞进模型上下文。
- 子代理输出默认是待审核材料，不是最终结论。
- 每个关键结论必须能追溯到 evidence 或 artifact。
- 父级要能随时回答“谁在干什么、卡在哪里、证据是什么、下一步是什么”。

## 当前已有基础

- `SubAgentTask` 已有 `id`、`parent_id`、`root_id`、`depth`、状态和任务目录。
- `SubAgentTask` 已补 `StatusReport`、progress/current step/latest summary、blockers、artifact/evidence refs、evidence packets、findings、checkpoint refs。
- runner 已能 dry-run / execute，并写回 `RUNNER_RESULT.md`、`reports/runner_result.json`、`output.json`。
- runner 结构化输出已能写回最小 `evidence_packets` / `findings`，任务目录会生成 `reports/status_report.json`。
- workflow planner 已能生成计划；`auto` apply 已能物化 worker 子工单。
- due-check、action plan、capability route、acceptance review、patch review/apply service 已有第一版。
- LocalStore、timeline、memory-resume 已能索引 subagent 事实源。
- learning draft 已能在 `enable_self_learning=true` 时从 runner lessons 生成候选。

## 开发顺序

### 1. Task Tree Control Plane v1

状态：第一片已落地；还需要继续补父级查询和更完整的 CLI/status 展示。

先把 subagent 从“任务目录集合”升级成可观察任务树。

要做：

- 定义 `StatusReport` 写回结构，包含 `run_id`、`version`、`state`、`progress`、`current_step`、`budget_used`、`summary_delta`、`artifact_refs`、`evidence_refs`、`blockers`、`checkpoint_ref`。
- 在任务目录和 LocalStore 中保存最近一次 status report。
- 给父级和 CLI 提供 child visibility：列子任务、看子任务状态、看 blocked / running / awaiting acceptance。
- 把 `workflow_child_run_ids`、依赖关系和父子状态汇总进 `subagents` 看板。

退出标准：

- 父任务可以不读聊天上下文，只读事实源回答当前进度。
- `subagents` / `status` 能看见 root task 的 children、阻塞项和最新摘要。
- 一个子任务卡住时，父级能定位到具体 run、原因和证据路径。

### 2. Evidence Packet / Finding 合同

状态：最小写回和阻断已落地；还需要继续补 acceptance report 分层和 verifier 使用。

第二步做证据合同，直接压住 fake done。

要做：

- 定义 `EvidencePacket`：`claim`、`checked_scope`、`evidence_refs`、`artifact_refs`、`counter_evidence_refs`、`confidence`、`unresolved_risks`。
- 定义 `Finding`：面向父级归并的结构化结论，必须引用 evidence packet。
- runner structured output 支持写回 evidence packets / findings。
- acceptance 读取 evidence packets，而不是只看 summary、文件存在或测试文本。

退出标准：

- 子代理不能只用“已完成”通过验收。
- 父级验收能列出每条关键结论对应的 evidence refs。
- 缺 evidence chain 的 DONE / PASS 会被阻断。

### 3. Acceptance 强化 + Verifier

状态：第一片已落地；acceptance report 已分层展示，确定性 verifier checks 已能阻断 unresolved evidence risk。还需要继续补自动验收项生成和独立 verifier / critic worker。

第三步把验收从“检查完成材料”升级成“检查结论是否可靠”。

要做：

- 从 `output.json.tests`、`artifacts`、`evidence_packets` 自动生成验收检查项。
- acceptance report 分层展示：worker 自述、证据事实、父级结论。
- 新增 verifier 角色约定：只读证据、找反例、检查 claim 是否过度推断。
- 高风险 workflow 默认 producer -> verifier / critic -> parent acceptance。

退出标准：

- 缺测试、缺 artifact、缺 evidence、patch 未验、capability gap 未处理时不能 DONE。
- verifier FAIL 不会被 producer 覆盖。
- acceptance report 可以作为 scheduler 后续动作的事实源。

### 4. Rescue / Escalation

状态：第一片已落地；action plan 已能带 rescue/escalation 元数据并写入 apply 记录。还需要继续补 rescue packet 文件、重复失败去重、重试上限和真正上抛记录。

第四步处理卡住、失败和能力缺口。

要做：

- 定义 rescue 触发条件：timeout、repeated failure、blocked、capability gap、stale heartbeat。
- rescue agent 输入包包含目标任务状态、事件、artifact、evidence、父级摘要和失败记录。
- rescue 输出只能是修复建议、补充证据、重新排队、上抛或放弃建议。
- escalation 规则：本层无法解决时上抛父级；父级无能力时继续上抛更高层。

退出标准：

- 一个 blocked task 能自动生成可审计的 rescue / escalate 动作建议。
- rescue 不能越权写文件或绕过 acceptance。
- 重复失败不会无限重试。

### 5. Compact / Checkpoint 协议

这部分可以和前几步并行开发，但必须保持边界清楚。

要做：

- 每个 agent 维护三份摘要：`local_working_summary`、`parent_rollup_summary`、`children_rollup_summary`。
- 每个长任务定期写结构化 handoff：`decision_ledger.json`、`progress.md`、`failing_tests.json`、`next_actions.json`。
- checkpoint 只保存恢复必要字段，不保存完整聊天历史。
- memory-resume 优先回到任务目录、status report、evidence packet 和 artifact refs。

退出标准：

- compact 后主 agent 上下文仍小，但能恢复关键事实。
- 跨天恢复时能回到任务树节点和 evidence chain。
- compact agent 不修改 subagent 状态机、dispatch、acceptance 逻辑。

### 6. Worker / Session Pool

等状态、证据、验收稳定后，再扩大并发。

要做：

- 在现有保守 runner 线程池基础上，引入明确 queue / lease / heartbeat 模型。
- 增加进程级 worker pool，隔离 runner 执行。
- 增加 session pool、启动速率限制、自适应 timeout 和预算控制。
- 所有 worker 状态写入 LocalStore / event stream。

退出标准：

- 并发 runner 不重复抢同一个任务。
- worker 崩溃后 lease 可恢复。
- `status` 能看到 worker pool、队列长度、运行中任务和卡住任务。

### 7. Patch 集成验收闭环

patch 能力要在强验收和 worker pool 后做，避免自动破坏用户文件。

要做：

- patch apply 必须检查 owner、allowed write roots、review status 和 patch 状态。
- apply 后必须跑指定测试，或记录未跑原因并进入 acceptance 风险。
- patch 集成结果写入 evidence / artifact / acceptance report。
- 未知 patch 状态、planned、blocked、未审核 applied patch 都阻断 DONE。

退出标准：

- patch 自动集成可审计、可解释、可阻断。
- apply 后没有测试证据时不能静默通过。
- 用户文件边界不被越权修改。

### 8. Learning Draft 提升链路

当前已有 draft 生成，下一步是人工确认提升。

要做：

- accepted draft 生成候选 rule / quality profile / skill patch。
- 每个候选必须带来源 run、evidence、适用范围和反例风险。
- 人工确认后才进入正式 skill、rule 或 profile。

退出标准：

- lessons 不会自动污染正式能力。
- 成功经验和失败样本能逐步变成可回归资产。

### 9. ACP / 外部 Agent Session / 远端执行器

最后再接远端执行通道。

要做：

- 定义外部 Agent Card / capability card。
- 将当前 task spec、status report、artifact refs 映射到外部协议。
- 接入远端执行器前必须复用本地权限、evidence、acceptance、checkpoint 规则。

退出标准：

- 外部 session 是任务树的一个节点，不是不可观察黑盒。
- 远端结果必须写 evidence / artifact / status report。
- 远端 agent 不能绕过本地 acceptance。

## 和 compact 并行开发的边界

compact 可以并行，但建议只负责：

- memory archive compact / rebuild / backup。
- recovery snapshot 压缩。
- LocalStore compact / export。
- `memory-resume` 恢复摘要体积控制。
- checkpoint / handoff artifact 的读取和压缩。

compact 不应直接修改：

- `agent_py_agent/agent/subagents/` 状态机。
- `agent_py_agent/agent/subagent_workflows/` worker 物化逻辑。
- `agent_py_agent/agent/agent_core/*dispatch*` 调度逻辑。
- acceptance 阻断规则。

如果 compact 需要改 LocalStore schema 或 source type，必须先同步 subagent status report、runner result、acceptance report 的索引兼容策略。

## 第一轮建议落地范围

第一轮只做 Task Tree Control Plane v1 和 Evidence Packet 最小闭环。当前已完成第一片：

- `StatusReport` 数据结构和落盘。
- `subagents` 看板展示 children / progress / blockers / latest summary。
- runner output 支持最小 `evidence_packets`。
- acceptance 阻断缺 evidence chain 的完成。
- focused tests 覆盖旧 task 兼容、runner 写回、LocalStore 索引、acceptance 阻断。

第一轮收尾已推进：

- 把 status report 最近状态纳入更完整的父级查询/索引展示。
- acceptance report 已明确列出 worker 自述、证据事实、父级结论三层，并增加 verifier checks。
- 基于 evidence packets 自动生成更细的验收项。

这样做的好处是先把“看得见、能恢复、可验收”做稳，再去做更强并发和远端执行。
