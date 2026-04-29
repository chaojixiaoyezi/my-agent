# 设计思路台账

这份文档用来记录我们在交流中形成的新思路，避免后续开发时忘记上下文。

后续 AI 开发者必须先读：
- `AGENTS.md`
- `CODEBASE_TREE.md`
- 本文件

每次出现新的架构想法、命令语义、能力边界或长期方向，都要在这里追加记录，并标明是否已经落地。

## 状态标记

```text
已落地       已经有代码或配置实现
部分落地     已有基础结构，但还没完整接入运行链路
设计中       已形成方向，还没写代码
待验证       已写代码，但还需要真实场景验证
暂停         暂时不做，但保留背景
```

## 2026-04-28 / chat 交互体验

### 后台队列

状态：已落地

思路：
- 用户发出一条消息后，不应该卡死在模型响应上。
- chat 模式应允许继续输入，后续输入进入后台队列。
- 同一时间先保持只跑一个模型请求，避免记忆和工具调用乱序。

落地位置：
- `agent_py_agent/__main__.py`

后续注意：
- 需要继续补 `/queue`、`/cancel`、`/queue-clear`。
- 当前取消只能取消本地等待或未开始任务，不能撤回已经发到服务端的请求。

### 中文输入和后台输出

状态：待验证

思路：
- 普通 `input()` 和后台线程同时输出时，会破坏正在编辑的输入行。
- 中文输入删除时还会出现视觉残留。
- 真实交互终端应优先使用 `prompt_toolkit` 和 `patch_stdout()` 保护输入行。

落地位置：
- `agent_py_agent/__main__.py`

后续注意：
- 不要再用终端标题栏 escape 序列显示读秒；部分 IDE 会把控制码打印成正文。
- 如需读秒，优先使用 `prompt_toolkit` bottom toolbar，或保留 `/status` 主动查询。

## 2026-04-28 / `/btw` 语义

状态：部分落地

思路：
- `/btw` 不是传统意义的 inject 管理命令。
- 它代表“顺便说一下 / 打断补充 / 修正上下文”。
- 用户可以用它补充约束、纠正目标、改变当前任务倾向。

当前行为：
- `/btw` 显示当前运行时补充上下文。
- `/btw <内容>` 追加一条补充上下文。
- `/btw-clear` 清空补充上下文。

后续方向：
- `/btw-once <内容>`：只对下一条任务生效。
- `/btw-pop`：撤回最后一条 btw。
- 任务正在执行时，`/btw` 不能真正修改已经发到服务端的 prompt，但可以影响排队中和后续任务。

## 2026-04-28 / 自学习

状态：部分落地

思路：
- 个人通用助手一定需要自学习能力。
- 自学习不能直接改正式 skill，必须先生成学习候选草稿。
- 用户确认后，草稿才能进入正式 skill。

已落地：
- `enable_self_learning` 配置开关，默认关闭。
- `AGENTS.md` 里写明自学习约束。

落地位置：
- `agent_py_agent/config/agent_config.yaml`
- `agent_py_agent/agent/config.py`
- `AGENTS.md`

后续方向：
- `agent_py_agent/data/learning_drafts/`：保存学习候选草稿。
- `/learn`：查看候选。
- `/learn accept <id>`：确认写入 skill。
- `/learn reject <id>`：拒绝候选。

## 2026-04-28 / Skill 使用方式

状态：部分落地

思路：
- 不采用“全量 skill 常驻 prompt”。
- 不让子代理直接看到全局 skill 宇宙。
- 最佳方向是“检索门控的渐进式 skill 路由”。

目标加载顺序：

```text
L0: Capability Taxonomy
L1: Skill Cards
L2: Candidate Cards
L3: SKILL.md 正文
L4: references/templates/scripts 附件
```

后续原则：
- 常驻内容要少。
- Skill Card 可被本地检索。
- 真正命中后才加载 `SKILL.md`。
- 附件必须按需读取。

已落地底座：
- `agent_py_agent/agent/skills.py`：扫描和解析 `SKILL.md` 成 Skill Card。
- `agent_py_agent/agent/capabilities.py`：把 Skill Card 和 Tool Card 合并进统一 Capability Router。
- `agent_py_agent/tests/test_capabilities.py`：覆盖 skill 解析、tool card 映射和候选检索。

尚未落地：
- 还未完整接入 chat/subagent 主链路。
- 还未实现 capability_request / capability_grant 协议。

## 2026-04-28 / Tool 使用方式

状态：部分落地

思路：
- tools 和 skills 应统一纳入 Capability 系统。
- tool 比 skill 更危险，因为 tool 会产生实际动作。
- 子代理不应直接看到全量工具，只应拿到父代理授权的最小 tool bundle。

关键原则：

```text
工具失败 ≠ 能力失败
当前授权失败 ≠ 系统无能力
能力缺口要上抛，而不是伪装完成
```

典型场景：
- 有 5 个 web 搜索工具。
- 子代理只拿到其中一个，调用失败。
- 子代理不能直接说“不行”或假完成。
- 它应该上抛 capability_request，让父代理寻找替代 tool 或 skill。

后续方向：
- Tool Card 需要记录 `fallback_group`、`failure_modes`、`risk_level`、`side_effects`。
- 父代理可以下发替代工具 bundle。
- 失败经验应反向更新 Tool Card，避免下次继续错误路由。

已落地底座：
- `agent_py_agent/agent/capabilities.py` 已能把现有 `ToolSpec` 映射成 Tool Capability Card。
- 当前已补基础风险分类和副作用分类，例如 `filesystem_write`、`network_request`。

尚未落地：
- `fallback_group` 和 `failure_modes` 还没进入 Tool Card。
- 还没有父代理下发 tool bundle 的运行链路。

## 2026-04-28 / 层级能力上抛

状态：部分落地

思路：
- 子代理遇到问题，不应该自己全局搜索 skill/tool。
- 子代理只描述能力缺口。
- 父代理查自己的 Skill/Tool Cards。
- 父代理找不到就继续向上抛。
- 更高层找到能力后，可以沿原链路下发给真正需要的下级代理。
- 中间层不需要读 skill 全文，只转发 skill/tool card 或 capability grant。

目标流程：

```text
子代理执行任务
  -> 遇到能力缺口
  -> capability_request 给父代理
  -> 父代理查自己的 skill/tool cards
      -> 找到：下发 capability_grant
      -> 没找到：继续向上抛
  -> 更高层找到能力
  -> 沿原链路下发 grant
  -> 子代理按需加载正文或工具说明
  -> 完成任务或记录 capability_gap
```

核心原则：

```text
下级代理只暴露问题，不搜索全局能力；
上级代理负责能力发现、授权和下发；
中间代理可以只转发 capability grant，不必展开 skill/tool 全文。
```

已落地底座：
- `agent_py_agent/agent/subagent.py` 已新增 `CapabilityRequest`、`CapabilityGrant`、`CapabilityGap`。
- `SubAgentTask` 已升级成兼容旧名字的轻量 SubAgentRun，支持 `parent_id`、`root_id`、`depth`、`allowed_skills`、`allowed_tools`。
- `SubAgentManager` 已支持记录能力请求、能力授权和能力缺口。
- `agent_py_agent/tests/test_agent.py` 已覆盖父子关系和能力记录读写。
- `SubAgentManager.route_capability_requests()` 已能把 open request 路由到 skill/tool card。
- `python3 -m agent_py_agent subagents-route-capabilities` 已能 dry-run 或 apply 生成 grant/gap。

尚未落地：
- 已有单个子代理 runner 入口，但还没有并行 worker / process / session 调度。
- capability_request 已能在当前父代理能力范围内路由，但还没有跨层级自动上抛。
- capability_grant 已能写入运行记录、生成执行上下文，并由 `subagent-run` 读取。

## 2026-04-28 / Subagent 框架

状态：部分落地

思路：
- subagent 不只是“拆出 thought/plan”，而是一个可追踪的运行节点。
- 每个运行节点都应该有父子关系、能力边界、状态、结果和能力协商记录。
- 先逻辑隔离，再考虑未来是否物理隔离成独立进程、终端或远程 agent。

已落地底座：
- `SubAgentCard`：角色卡，描述角色、默认工具、能否写文件、能否生成子代理、输出契约。
- `SubAgentTask`：兼容旧调用的运行记录。
- `task.json` 和 `run.json` 双写。
- `thought.md` 增加 Capability Boundary 区块。

后续方向：
- 增加 `/subagents` 查看运行树。
- 增加 `/subagent <id>` 查看单个运行详情。
- 增加子代理状态流转：`PLANNING`、`RUNNING`、`BLOCKED`、`DONE`、`FAILED`。
- 接入 Capability Router，为子代理初始化 allowed skill/tool bundle。
- 接入真正执行循环，让子代理拥有独立上下文。

已落地补充：
- `SubAgentBoardItem` / `SubAgentBoard`。
- `subagent_board.json` 机器事实源。
- `SUBAGENT_BOARD.md` 人类红绿灯摘要。
- `python3 -m agent_py_agent subagents` 查看 summary / hot list / recent。
- `python3 -m agent_py_agent subagent <run_id>` 查看单个运行详情。
- 默认 hot list 会浮出 `BLOCKED`、`FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`DONE` 无证据、open capability request/gap、takeover、工单文件缺失等风险。
- `python3 -m agent_py_agent subagents-due-check` 生成父代理巡检报告。

## 2026-04-29 / Subagent Due-check

状态：部分落地

思路：
- 父代理不能 spawn 后放养，必须周期性查看子代理真实产出。
- due-check 的第一阶段只做“发现问题 + 建议动作”，不自动接管。
- 机器事实源应先落盘，后续自动接管、重派、缩小目标都读这份报告。

已落地：
- `SubAgentManager.due_check()`：扫描所有子代理运行记录。
- `SubAgentManager.write_due_check()`：写出 `subagent_due_check.json` 和 `SUBAGENT_DUE_CHECK.md`。
- `python3 -m agent_py_agent subagents-due-check`：CLI 触发巡检。
- 巡检会标出 P0/P1/P2 问题：
  - 工单关键文件缺失。
  - `DONE` 缺验收证据。
  - `DONE` 但未验证。
  - `FAILED` / `TIMEOUT` / `CHANNEL_ERROR` / `BLOCKED`。
  - 心跳停滞。
  - 运行超时。
  - open capability request。
  - open capability gap。

尚未落地：
- 自动周期触发 due-check。
- 根据 due-check 已能生成 dry-run action plan，但还没有自动 apply。
- channel probe 已有本地工单现场检查，但还没有接入模型 session / ACP adapter / 真实执行器。

## 2026-04-29 / Due-check Action Plan

状态：部分落地

思路：
- due-check 负责发现问题，action plan 负责把问题转成下一步动作。
- 第一阶段必须 dry-run，不自动接管、不自动重派、不自动改任务状态。
- 同一个 run 的重复问题要去重合并，例如 `heartbeat_stale` 和 `run_timeout` 合并成一次 `takeover_or_reassign`。

已落地：
- `ActionPlanItem` / `ActionPlanReport` 数据结构。
- `SubAgentManager.plan_actions()`：把 due-check issue 映射成动作。
- `SubAgentManager.write_action_plan()`：写出 `subagent_action_plan.json` 和 `SUBAGENT_ACTION_PLAN.md`。
- `python3 -m agent_py_agent subagents-plan-actions`：CLI 查看 dry-run 动作计划。

当前动作类型：
- `probe_or_repair_channel`：通道坏或缺 probe 证据时，优先检查 / 修复通道。
- `inspect_channel_probe`：通道降级时，先读 probe 证据。
- `repair_work_order`：工单关键路径缺失时修复现场。
- `reopen_for_evidence`：DONE 但缺证据时重开补验收。
- `run_acceptance`：DONE 但未 VERIFIED 时运行验收。
- `takeover_or_reassign`：超时或心跳停滞时准备接管、重派或缩小目标。
- `inspect_failure`：FAILED 时读取现场日志再决定处理方式。
- `classify_blocker`：BLOCKED 时分类原因。
- `route_capability_request`：处理能力请求并准备下发 skill/tool grant。
- `triage_capability_gap`：能力缺口进入学习或工具建设队列。

尚未落地：
- `--apply` 已有保守执行层，但还没有自动周期执行。
- 自动 takeover 已有受限 apply；自动 reassign / shrink-scope 还没有。
- 自动 route capability request。
- apply 前的交互式用户确认。

## 2026-04-29 / Action Apply v1

状态：部分落地

思路：
- action apply 默认必须 dry-run。
- 只有显式 `--apply` 才能改子代理运行记录。
- 第一版只执行低风险动作，不自动删文件、不覆盖已有人工产物、不自动下发 skill/tool。
- 所有 apply 必须留下机器审计日志和人类审计日志。

已落地：
- `ActionApplyRecord` / `ActionApplyReport` 数据结构。
- `SubAgentManager.apply_actions()`：执行或 dry-run 执行动作计划。
- `SubAgentManager.write_action_apply_report()`：写出 `subagent_action_apply_report.json` 和 `SUBAGENT_ACTION_APPLY.md`。
- `python3 -m agent_py_agent subagents-apply-actions`：默认 dry-run。
- `python3 -m agent_py_agent subagents-apply-actions --apply ...`：显式执行。
- 真正执行时追加：
  - `subagent_action_apply_log.jsonl`
  - `ACTION_APPLY_LOG.md`
  - 子代理自己的 `WORK_LOG.md`

当前允许执行的动作：
- `probe_or_repair_channel` / `inspect_channel_probe`：执行 channel probe 并记录证据。
- `repair_work_order`：补齐标准工单文件，只写缺失文件，不覆盖已有内容。
- `reopen_for_evidence`：把缺证据 DONE 改为 `BLOCKED`，标记 `failure_type=missing_evidence`。
- `run_acceptance`：只标记 `verification_status=NEEDS_ACCEPTANCE`，不自动跑未知命令。
- `takeover_or_reassign`：当前只做 takeover；必须提供 `--take-over-by`，会写 `TAKEOVER.md`。
- `route_capability_request` / `triage_capability_gap` / `inspect_failure` / `classify_blocker`：只写入待人工处理日志，不自动改授权或学习。

安全边界：
- 默认 dry-run。
- `takeover_or_reassign` 必须显式指定接管者。
- takeover 前会先确认 channel 为 `OK`，否则拒绝接管。
- 不删除文件。
- 不覆盖已有 `STATUS.md` / `WORK_LOG.md` / `ACCEPTANCE.md`。

尚未落地：
- 自动 reassign。
- 自动 shrink-scope。
- action apply 尚未直接触发 capability route；需要单独运行 `subagents-route-capabilities`。
- apply 前的交互式二次确认。
- apply 后的统一验收执行器。

## 2026-04-29 / Capability Request Routing

状态：部分落地

思路：
- 子代理不直接看全局 skill/tool 宇宙，只提交 capability request。
- 父代理用自己的 Capability Router 查 skill/tool card。
- 命中则生成 capability grant，未命中则生成 capability gap。
- 第一版默认 dry-run，只有显式 `--apply` 才写 grant/gap。

已落地：
- `CapabilityRouteRecord` / `CapabilityRouteReport` 数据结构。
- `SubAgentManager.route_capability_requests()`：路由 open capability request。
- `SubAgentManager.write_capability_route_report()`：写出 `subagent_capability_route_report.json` 和 `SUBAGENT_CAPABILITY_ROUTE.md`。
- `python3 -m agent_py_agent subagents-route-capabilities`：CLI 路由能力请求。
- 支持 `--skill-dir` 额外加载本地 skill card。
- apply 命中时会调用 `record_capability_grant()`，并把 request 标记为 `GRANTED`。
- apply 未命中时会调用 `record_capability_gap()`，并把 request 标记为 `GAP`。
- 真正 apply 时追加：
  - `subagent_capability_route_log.jsonl`
  - `CAPABILITY_ROUTE_LOG.md`
  - 子代理自己的 `WORK_LOG.md`

安全边界：
- 默认 dry-run。
- 只下发 skill/tool card，不加载完整 `SKILL.md` 正文。
- 不执行工具。
- 不自动学习新 skill。
- grant/gap 都可审计。

尚未落地：
- 跨父/爷/更高层级的自动上抛。
- grant 已能生成执行上下文文件，并可由 `subagent-run` 作为单 run 执行入口读取。
- 根据 route 结果自动重新唤醒子代理。
- tool fallback group 和 failure mode 还没有参与排序。

## 2026-04-29 / Grant 注入执行上下文

状态：部分落地

思路：
- capability grant 不能只停留在运行记录里，必须能变成子代理实际可读取的执行包。
- 执行包只包含父级授权后的能力，不给子代理全局 skill/tool 宇宙。
- 子代理缺能力时继续写 capability request，而不是自己到处搜索或假完成。

已落地：
- `SubAgentExecutionContext` 数据结构。
- `SubAgentManager.build_execution_context()`：生成单个 run 的最小执行上下文。
- `SubAgentManager.write_execution_context()`：写出 `execution_context.json` 和 `EXECUTION_CONTEXT.md`。
- `python3 -m agent_py_agent subagent-context <run_id>`：CLI 生成执行上下文。
- 执行上下文包含：
  - goal / thought / plan。
  - owner / supervisor / final_owner。
  - allowed skills / allowed tools。
  - granted cards。
  - acceptance checks / evidence。
  - allowed_write_roots / forbidden_write_roots / locked_files。
  - open capability request / gap。
  - 子代理执行硬规则。

安全边界：
- 不展开完整 `SKILL.md` 正文。
- 不注入未授权工具。
- 不读取全局 registry。
- runner 执行时只渲染并允许调用 `allowed_tools` 内的工具。
- 默认 dry-run；显式 `--execute` 才会调用模型。

尚未落地：
- grant 后自动重新唤醒或继续子代理。
- 按 token 预算裁剪 card 描述和上下文正文。
- 子代理写 capability request 的统一入口。

## 2026-04-29 / Subagent Runner Entry

状态：部分落地

思路：
- 子代理 runner 第一版先不做真正并发进程池，只打通一条可审计链路：
  execution context -> runner prompt -> 模型执行或 dry-run -> 工单回写。
- runner 默认 dry-run，避免误触真实 API。
- 真执行时必须先经过工具 allowlist，不能看到或调用未授权工具。

已落地：
- `SimpleAgent.run(..., allowed_tools=[...])`：限制工具目录、推荐工具和实际工具调用。
- `SimpleAgent.run_subagent()`：读取并刷新执行上下文，生成 runner prompt。
- `SubAgentManager.record_runner_result()`：把 runner 结果写回工单。
- `parse_subagent_runner_output()`：解析 `[SUBAGENT_RESULT]` 结构化 JSON。
- `python3 -m agent_py_agent subagent-run <run_id>`：默认 dry-run。
- `python3 -m agent_py_agent subagent-run <run_id> --execute`：显式调用模型执行。
- 结构化输出中的 `evidence` 会写入 `VerificationEvidence`。
- 结构化输出中的 `capability_requests` 会写成 open `CapabilityRequest`。
- 结构化输出中的 `artifacts` / `tests` / `patches` 会写进 `output.json`，供验收器和集成器读取。
- 结构化输出中的 `lessons` / `next_actions` 会写进 `output.json`，并追加到 `DEBRIEF.md`。
- 未授权的 `used_tools` / `used_skills` 会被忽略并写入 `output.json` 审计。
- runner 输出文件：
  - `RUNNER_RESULT.md`
  - `reports/runner_result.json`
  - `logs/runner_prompt.md`
  - `logs/runner_response.md`
  - `output.json`

安全边界：
- `--execute` 才会调用模型，默认只生成 prompt 和报告。
- 执行前默认跑 channel probe，BROKEN 时直接标记 `CHANNEL_ERROR`，不继续模型调用。
- 模型即使输出未授权工具调用，也会被 `ToolRegistry.execute_call(..., allowed_tools=...)` 拦住。
- 模型完成后只标记 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`，不直接标记 DONE，避免假完成。

尚未落地：
- 真正的并行 worker / process / session 管理。
- runner 还不会自动应用 patch；patches 目前只是计划/状态记录，必须由父代理或集成器验收后处理。
- runner 还没有把 lessons 自动转成 skill 草稿。
- 多子代理统一调度、超时接管和重派。

## 2026-04-29 / Subagent Channel Probe

状态：部分落地

思路：
- 通道故障和任务失败必须分开判断。
- 在自动接管或重派前，父代理应该先确认 workdir、机器 JSON、写入现场和 probe 证据是否正常。
- 第一阶段先做本地运行现场 probe；后续再接模型 session、ACP adapter、远程 runtime。

已落地：
- `ChannelProbeCheck` / `ChannelProbeResult` / `ChannelProbeReport` 数据结构。
- `SubAgentTask.channel_status`：`UNKNOWN` / `OK` / `DEGRADED` / `BROKEN`。
- `SubAgentTask.last_probe_at` 和 `channel_checks`。
- `SubAgentManager.probe_channel(run_id)`：检查单个子代理运行现场。
- `SubAgentManager.write_channel_probe_report()`：写出 `subagent_channel_probe.json` 和 `SUBAGENT_CHANNEL_PROBE.md`。
- `python3 -m agent_py_agent subagents-probe`：CLI 触发通道健康检查。
- 单个 run 会写 `CHANNEL_PROBE.md` 和 `logs/last_channel_probe.json` 作为证据。
- due-check 会识别 `channel_broken` / `channel_degraded` / `channel_probe_missing`。

当前检查项：
- 标准工单目录和关键文件是否完整。
- `task.json` / `run.json` / `output.json` / `dependencies.json` 是否可读。
- `scratch/` 是否可写。
- probe 证据是否能写入任务目录。

尚未落地：
- 模型接口 probe。
- ACP / adapter / session probe。
- 工具通道 probe。
- 派工前自动 probe 和失败阻断。
- channel probe 失败后的自动修复建议。

## 2026-04-28 / 能力路由配置

状态：已落地

思路：
- 子代理、skill/tool 授权、能力上抛相关参数不放进 `agent_config.yaml`。
- 单独使用能力路由配置，避免主配置变成杂物间。
- 所有数字限制项统一约定：`0` 表示不限制。

已落地：
- `agent_py_agent/config/capability_config.yaml`
- `agent_py_agent/agent/capability_config.py`

默认正常限制：

```yaml
capability_request_max_tokens: 600
capability_escalation_max_hops: 4
capability_candidate_limit: 5
capability_bundle_max_tokens: 3000
capability_fallback_max_attempts: 3
```

其他数字限制项默认 `0`，由用户后续自行收紧。

## 2026-04-28 / Capability Gap

状态：部分落地

思路：
- 如果最终没有任何上级代理能找到 skill/tool 解决问题，不应该只报失败。
- 应记录 capability gap，作为后续自学习或工具建设的输入。

建议字段：

```yaml
missing_capability: 缺少什么能力
source_task: 来源任务
attempted_skills: 已尝试 skill
attempted_tools: 已尝试 tool
why_failed: 为什么失败
needed_outputs: 需要产出什么
suggested_skill: 是否建议沉淀成 skill
suggested_tool: 是否建议开发成 tool
```

后续方向：
- `/gaps` 查看能力缺口。
- `/gaps learn <id>` 生成学习候选。
- `/gaps close <id>` 标记已解决。

## 2026-04-28 / 用户真实痛点：Subagent 假完成与失控

状态：设计中

来源：
- 用户基于真实使用经验总结。

核心痛点：
- Fake Done / 假完成：子代理说“完成了”，但实际文件缺、功能缺、按钮没反应、没有真实验证证据。
- 只测设计路径，不测用户真实入口：内部路由 PASS，但直接打开短路径、刷新、复制链接会 404 或不可用。
- 父会话派完就放养：只 spawn，不持续 due-check，不主动看实际产出。
- 子代理长时间静默 / 卡死 / timeout 不合理：父会话没有及时接管、改派或缩小目标。
- 子代理输出不直接落盘：只在聊天里汇报，压缩或丢会话后成果难恢复、难验收。
- 任务拆得太粗或太散：太粗吃不下，太散难集成、互相覆盖。
- 没有明确 owner / supervisor / final_owner：并行任务没人统一收口，容易双写或漏验收。
- 缺独立验收层：子代理自己说通过就算通过，缺 dev → test → verify / reviewer / smoke test 闭环。
- 验收只看文件存在，不看行为可用：文件存在不等于功能可用，必须跑命令、测流程、看日志或截图。
- 缺真实来源核查：调研类任务不能只凭模型知识，必须访问真实 URL 并记录来源。
- 没有 skill 匹配和使用证据：子代理是否真的读了 skill 不可审计。
- 通道故障和任务失败混在一起：runtime/session/adapter 故障不等于任务本身失败。
- 工具失败后停住：一个工具没结果就卡住，没有 fallback 链继续尝试。
- 上下文断片 / 历史 session 污染判断：旧 session 的“完成”可能误导当前状态。
- 收口不完整：代码做完但 RESULT / EVIDENCE / TEST_CHECKLIST / SKILL_SPARK / lessons 没补。
- 没有把踩坑沉淀成 skill：BLOCKED 修好了但没提炼成长期规则。
- 并行之后缺统一集成验证：子任务各自通过，但合起来可能路由断、样式冲突、接口不通。
- 对“完成”的标准不够硬：必须按“功能全、流程通、入口测、证据齐、验收过”收口。

框架约束：
- 子代理不能只返回自然语言“完成”；必须产出结构化验收证据。
- 子代理运行记录必须落盘，不能只存在聊天上下文。
- 每个任务必须有 owner、supervisor、final_owner。
- 父代理必须做 due-check，不能 spawn 后放养。
- 任务状态要区分 `DONE`、`FAILED`、`BLOCKED`、`CHANNEL_ERROR`、`TIMEOUT`。
- 工具失败要记录 failure mode，并触发 fallback 或 capability_request。
- 完成前必须经过独立 verification 或 final_owner 收口。
- 真实用户入口、刷新、短路径、复制链接等要进入验收 checklist。
- 调研任务必须记录 URL / 来源 / 访问时间。
- 子代理必须记录使用了哪些 skill/tool card。
- 未解决或反复出现的问题要生成 capability_gap 或 learning_draft。

后续优先落地：
- `SubAgentTask` 增加 owner / supervisor / final_owner。
- `SubAgentTask` 增加 verification_status / evidence / acceptance_checks / failure_type。
- 能力配置增加 heartbeat / due-check / timeout / evidence 最小要求。
- 增加 `/subagents` 和 `/subagent <id>` 查看运行树与验收状态。
- 增加 Fake Done 防护：没有 evidence 的任务不能标记为 DONE。

已落地底座：
- `SubAgentTask` 已有 owner / supervisor / final_owner。
- `SubAgentTask` 已有 verification_status / evidence / acceptance_checks / failure_type。
- `CapabilityConfig` 已有 heartbeat / due-check / run timeout / minimum evidence 配置。
- `SubAgentManager.set_status(..., require_evidence=True)` 已禁止无证据 DONE。
- `agent_py_agent/tests/test_agent.py` 已覆盖 Fake Done 防护。

尚未落地：
- 父代理已有手动 due-check 报告，但还没有自动周期巡检和自动接管。
- 子代理还没有自动心跳。
- 真实入口验收、URL 来源核查、截图/日志证据还只是字段结构，没有自动执行器。
- 收口文档 RESULT / EVIDENCE / TEST_CHECKLIST / lessons 还没有自动检查。

## 2026-04-29 / 外部痛点文档：DISPATCH_PAIN_POINTS

状态：已记录，部分约束已落地

来源：
- `/Users/xiaoyezi/Downloads/DISPATCH_PAIN_POINTS.md`
- 文档生成时间：2026-04-28
- 原归属目录：`/Users/xiaoyezi/.hermes/tasks/2026-04-28/dispatch-pain-points/`

定位：
- 这是派工 / 子代理 / 小傻妞痛点全集。
- 内容覆盖 P0 / P1 / P2 三层痛点、派工前 Checklist、标准派工 Prompt 骨架。
- 本文档是后续 subagent、capability routing、自学习、验收闭环的重要输入源。

相对已有台账新增或强调的关键点：
- workdir 参数不可靠，子代理产物可能散落 HOME。
- 子代理启动后必须立刻创建工单目录，并写 `STATUS.md` / `WORK_LOG.md`。
- 派工前必须做通道健康 probe，ACP / runtime / adapter / output path 坏了不能批量派工。
- completion notification 不等于完成，完成后父代理必须读取产物并独立验收。
- takeover 必须写 `TAKEOVER.md`，锁定文件，避免 parent/child 双写。
- `BLOCKED` 不能当终点，必须分类处理：工具/环境、任务太难、缺信息、权限禁止。
- 强依赖任务不能并行猜产物，必须通过 `output.json` / `dependencies.json` 传递。
- Markdown 台账不是事实源，机器判断必须读 JSON / JSONL。
- 中途汇报必须和最终汇报区分，避免用户误以为已经结束。
- 禁止用 sleep / heartbeat 伪装长任务，长任务必须有真实阶段产出。
- 子代理输出必须短摘要 + 证据路径，长日志落盘。
- 任务现场文件名需要统一，例如 `STATUS.md`、`WORK_LOG.md`、`RESULT.md`、`EVIDENCE.md`、`TESTS.md`、`ACCEPTANCE.md`、`DEBRIEF.md`。
- 子代理报告必须有问题分级 P0/P1/P2。
- 派工前要做成本判断：秒级可验证用工具，多步骤判断/迭代/验收才派 subagent。

应转成框架的硬约束：
- Subagent run 必须有 `workdir` / `task_dir` / `artifact_dir`，并默认禁止写 HOME、Desktop、Downloads、`.openclaw`。
- Subagent run 必须有 `status_file`、`work_log_file`、`acceptance_file`、`debrief_file` 等标准产物路径。
- Subagent run 必须支持 `dependencies` 和 `output_json`。
- Subagent run 必须记录 `channel_status` / `failure_type`，区分任务失败和通道失败。
- Subagent run 必须支持 `takeover_by`、`locked_files`、`takeover_reason`。
- Subagent run 必须记录 `model_name` 和实际运行模型。
- Subagent run 必须记录 `skill_paths_read` / `tool_cards_used`，方便审计。
- 父代理必须维护 machine-readable board，而不是只看 Markdown。
- 完成状态必须经过 `ACCEPTANCE PASS` 或 final_owner 收口。

已落地相关底座：
- `SubAgentTask` 已有 owner / supervisor / final_owner。
- `SubAgentTask` 已有 evidence / acceptance_checks / failure_type。
- `CapabilityConfig` 已有 due-check、heartbeat、run timeout、minimum evidence 配置。
- `CapabilityRequest` / `CapabilityGrant` / `CapabilityGap` 已有数据结构。
- `SkillCard` / `CapabilityCard` / `CapabilityRouter` 已有底座。

已落地底座：
- 标准工单目录模板。
- `task_dir` / `data_dir` / `output_dir` / `tests_dir` / `reports_dir` / `logs_dir` / `scratch_dir`。
- `allowed_write_roots` / `forbidden_write_roots`。
- `STATUS.md` / `WORK_LOG.md` / `ACCEPTANCE.md` / `DEBRIEF.md` 自动初始化。
- `TAKEOVER.md` 接管记录。
- `output.json` / `dependencies.json` 自动初始化。
- `validate_work_order(run_id)` 校验关键目录和文件是否存在。
- `record_takeover()` 记录 takeover_by、reason、locked_files，并把任务状态置为 `TAKEN_OVER`。
- `subagents-probe` 可以检查本地工单现场通道健康，并记录 `channel_status`。

尚未落地但优先级高：
- channel probe 还需要接模型接口、ACP / adapter / session 和工具通道。
- P0/P1/P2 问题分级已进入 due-check 报告，但还没接入执行器、用户汇报和自动接管流程。
- 子代理模型字段。
- 派工前工具 vs subagent 成本判断。

## 2026-04-29 / 文档基线更新

状态：已落地

思路：
- 之前 README 仍停留在早期简版，已经跟当前 capability / subagent / runner 体系不匹配。
- 睡前需要把主链路、命令、协议、安全边界和测试策略写清楚，避免后续 AI 接手时只靠上下文记忆。

已落地：
- 重写根目录 `README.md`：项目总览、快速开始、配置、关键文档、安全边界和测试提醒。
- 重写 `agent_py_agent/README.md`：包内 CLI 使用说明、工具、记忆、subagent 命令和 runner 协议入口。
- 新增 `SUBAGENT_RUNBOOK.md`：详细说明 subagent / capability / runner 的运行流、工单目录、命令、结构化输出协议、写回规则和安全边界。
- 更新 `TESTS.md`：区分安全定向测试和可能触发真实 API 的完整冒烟脚本。
- 更新 `TEST_CHECKLIST.md`：按普通 run、工具、capability、subagent、runner、文档分组列检查项。
- 更新 `CODEBASE_TREE.md`：加入 `SUBAGENT_RUNBOOK.md` 并说明职责。

后续要求：
- 改 subagent / capability / runner 主链路时，除了代码和测试，也要同步检查 `SUBAGENT_RUNBOOK.md`。

## 2026-04-29 / Subagent 验收器

状态：已落地

思路：
- runner 完成后只能进入 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`，不能自己标记 DONE。
- 父代理需要一个独立验收入口，读取 evidence、tests、patches、blockers、capability request/gap 和 runner 结构化输出。
- 默认必须 dry-run，只有显式 apply 才能写回状态。

已落地：
- `AcceptanceReviewFinding` / `AcceptanceReviewRecord` / `AcceptanceReviewReport`。
- `SubAgentManager.review_acceptances()`：批量验收等待验收的 run。
- `SubAgentManager.write_acceptance_review_report()`：写出 `subagent_acceptance_report.json` 和 `SUBAGENT_ACCEPTANCE.md`。
- `python3 -m agent_py_agent subagents-acceptance`：默认 dry-run。
- `python3 -m agent_py_agent subagents-acceptance --apply --run-id <run_id>`：验收通过时标记 `DONE/VERIFIED`，失败时标记 `BLOCKED/FAILED`。

当前验收检查：
- 工单现场完整。
- 任务确实处于等待验收状态。
- 通道不是 `BROKEN`。
- runner 结构化输出可解析，或至少能按人工证据验收。
- 至少有一条 ok evidence。
- 没有失败 evidence。
- 没有 open capability request/gap。
- `output.json` 没有 blocker。
- tests 不失败。
- patches 没有 `planned` / `blocked` 未处理项。

后续方向：
- 验收器可以接入可执行测试命令，但必须先做命令 allowlist 和超时审计。
- patch apply 需要独立审核链路，不能由验收器直接应用未知 patch。

## 2026-04-29 / 测试默认真实 API

状态：已落地

思路：
- 后续验收级、冒烟和回归测试默认直接调用真实 API。
- echo/fake backend 只能用于纯函数、解析器和局部单元定位，不能作为最终通过依据。
- 完整冒烟入口 `python3 agent_py_agent/tests/run_tests.py` 应按当前配置请求真实模型 API。

已落地：
- 更新 `TESTS.md`，把真实 API 作为标准完整冒烟要求。
- 更新 `TEST_CHECKLIST.md`，要求收口前运行完整冒烟并确认使用真实 API。
- `agent_py_agent/tests/run_tests.py` 改为自动发现并运行所有 `test_*.py` / `test_` 函数，避免手写清单漏掉新增测试。

后续注意：
- 如果真实 API 不稳定，要记录失败类型，而不是直接降级成 echo 后端通过。
- 新增测试命令时，区分“局部定位测试”和“真实 API 收口测试”。
- 新增 `test_*.py` 或 `test_` 函数后，不需要手动加入完整冒烟清单，但必须确认完整冒烟脚本发现了它。

## 2026-04-29 / 真实 API E2E、测试隔离和 patch 审核链

状态：已落地

思路：
- 完整冒烟不能只跑普通 `run/chat`，还要覆盖真实 API 的 `subagent-run --execute`。
- 冒烟测试不能污染默认记忆和默认 subagent 工单目录。
- runner 声明的 `patches` 需要独立审核链路；验收器不能直接把未审核 patch 当成完成。

已落地：
- `agent_py_agent/tests/run_tests.py` 创建临时配置，隔离 `memory_path` 和 `subagent_workspace`。
- 完整冒烟会创建真实 API 子代理工单，运行 `subagent-run --execute`，确认 backend 不是 echo、工具调用发生、结构化输出可解析、evidence 写回，并由父代理验收为 `DONE/VERIFIED`。
- 新增 `PatchReviewRecord` / `PatchReviewReport`。
- 新增 `SubAgentManager.review_patches()` 和 `write_patch_review_report()`。
- 新增 `python3 -m agent_py_agent subagents-patches`，默认 dry-run，显式 `--apply` 才写回 `review_status`。
- 验收器新增 `patches_reviewed` 和 `patch_status_valid` 检查：未审核的 applied patch、planned/blocked patch、未知状态 patch 都不能进入 `DONE/VERIFIED`。

后续注意：
- patch 审核器目前只审核 runner 已声明的 patch 状态，不自动应用 diff。
- 后续真正做 patch 集成器时，需要 owner、写入边界、diff 审计和测试命令 allowlist。

## 2026-04-29 / 父代理一轮调度器

状态：已落地

思路：
- 当前 chat 退出后父代理不常驻，但工单状态已经可恢复。
- 在做 daemon 前，先把“一轮父代理应该如何推进任务树”做成可审计命令。
- 调度器必须默认 dry-run；真实 runner 调用需要比普通 apply 更明确的开关。

已落地：
- 新增 `DispatchRecord` / `DispatchReport`。
- 新增 `SimpleAgent.dispatch_subagents()`：按 due-check、action apply、capability route、runner、patch review、acceptance 顺序执行一轮调度。
- 新增 `python3 -m agent_py_agent subagents-dispatch`。
- `subagents-dispatch --apply` 会写回低风险动作、能力路由、patch 审核和验收，并写调度审计日志。
- `subagents-dispatch --apply --execute-runners` 才会调用模型 runner。
- 新增 dispatch 单测：dry-run 规划、patch 审核后验收、runner 执行后验收。

后续方向：
- 在 dispatch 稳定后加 `--watch` 或独立 daemon。
- daemon 需要运行锁、停止信号、轮询间隔、最大 API 消耗和崩溃恢复记录。

## 2026-04-29 / 父代理 watch 模式

状态：已落地

思路：
- 一轮 `subagents-dispatch` 已经能推进任务树，但退出后不会继续巡检。
- 在独立 daemon 前，先让同一个命令支持 watch 循环，并保持 CLI 可测试、可退出。
- watch 必须有运行锁，避免两个父代理同时推进同一批工单。

已落地：
- 新增 `DispatchWatchRecord` / `DispatchWatchReport`。
- 新增 `SimpleAgent.watch_subagents()`。
- `python3 -m agent_py_agent subagents-dispatch --watch` 会持续循环 dispatch。
- `--interval` 控制轮询间隔，`--max-cycles` 控制安全退出，`--force-lock` 用于人工处理残留 lock。
- watch 写 `subagent_dispatch_watch_heartbeat.json`、`subagent_dispatch_watch_report.json`、`SUBAGENT_DISPATCH_WATCH.md`、`subagent_dispatch_watch_log.jsonl` 和 `DISPATCH_WATCH_LOG.md`。
- `--execute-runners` 现在必须和 `--apply` 同时使用。

后续方向：
- 增加外部停止信号或 stop 文件。
- 增加 API 消耗预算、每轮最大耗时和 daemon/service 包装。

## 2026-04-29 / 安装后命令入口

状态：已落地

思路：
- 现在可以用 `python3 -m agent_py_agent` 运行，但正式 agent 更应该安装后直接敲命令名。
- 暂定命令名为 `my-agent`，后续品牌名确定后只改 packaging script，不改业务入口。

已落地：
- 新增 `pyproject.toml`。
- 新增 console script：`my-agent = agent_py_agent.__main__:main`。
- 新增 `agent_py_agent/__init__.py`，让包在标准 packaging 下更明确。

后续方向：
- 增加 gateway 命令族：`my-agent gateway start/status/stop`。
- gateway 负责后台常驻、pid/lock/heartbeat、日志和外部控制；现有 `subagents-dispatch --watch` 作为 gateway 的核心工作循环。

## 2026-04-29 / 父代理 LLM planner

状态：已落地

思路：
- 仅靠 heartbeat 容易变成“报平安”，不能保证进入完整 LLM 决策链。
- watch 的规则调度器能推进 runner/验收/能力路由，但缺少父代理自己读状态并给出行动建议的一层。
- planner 必须有 gate：有 active/pending/stalled/needs-intervention 时，不允许模型只返回 `HEARTBEAT_OK`。

已落地：
- 新增 `ParentPlannerParsedOutput` / `ParentPlannerRecord` / `ParentPlannerReport`。
- 新增 `SimpleAgent.run_parent_planner()`，通过 `SimpleAgent.run()` 触发完整父代理 LLM turn，允许只读工具核对状态。
- `subagents-dispatch --planner` 会先收集 board、due-check、action plan、runner candidates、patch review、acceptance、open capability request/gap。
- gate 非空时调用父代理 LLM；如果模型只回 `HEARTBEAT_OK`，记录为失败。
- planner 输出 `runner_instruction` 可作为本轮 runner 的补充指令；`suggested_max_runners` 只能降低 CLI 上限，不能提高。
- 新增 planner prompt/response/report/log 审计文件。

后续方向：
- 让 planner 的 action plan 接入更丰富的受控动作，如 spawn-subagents、reassign、takeover。
- gateway 后台化后，把 planner tick 作为后台事件的一等公民。

## 2026-04-29 / 配置驱动 daemon 入口

状态：已落地

思路：
- `subagents-dispatch --watch --planner --apply --execute-runners --interval 30 --max-runners 1` 太长，不适合作为日常启动命令。
- 在 gateway/service 形态确定前，先提供 `my-agent daemon` 前台入口，并把常驻参数移到 `agent_config.yaml`。
- 默认配置必须安全：不写回、不执行 runner；用户确认后再把 `daemon_apply` 和 `daemon_execute_runners` 改为 true。

已落地：
- 新增 `daemon_*` 配置：planner、apply、execute_runners、interval、max_runners、limit、max_cycles、max_cards、probe、reviewer、runner_instruction。
- 新增 `my-agent daemon`，按配置启动前台常驻调度。
- daemon 支持少量 CLI override，用于临时测试或手动覆盖配置。
- 明确 `0` 值语义：`max_cycles=0` 持续运行，`max_runners=0` 不执行 runner，`max_cards=0` 不限制，`interval=0` 不等待且主要用于测试。

后续方向：
- 在这个入口之上做真正后台 `gateway start/status/stop/restart`。
- 增加 pid 文件、stdout/stderr 日志轮转和 Windows 后台进程管理。

## 2026-04-29 / Gateway 参数分层

状态：已落地配置层

思路：
- 普通用户不应该理解 `interval`、`max-runners`、`limit` 这类底层调度参数。
- 用户层只关心任务规模，比如最多多少子代理/孙代理；不关心每轮调度多少条。
- 高级用户可以自己调，但默认应该是 `auto` 或“不设硬上限”。

已落地：
- 新增 `GATEWAY_DESIGN.md`，记录 OpenClaw、Hermes、Claude session 和 Codex 云任务形态的参考。
- 新增用户层任务规模配置：`task_max_subagents`、`task_max_grandchildren`，默认 `0` 表示不设硬上限。
- 新增未来 gateway 策略配置：`scheduler_mode`、`runner_concurrency`、`runner_start_rate`、`runner_timeout_seconds`、`runner_failure_policy`，默认 `auto`。
- `daemon_max_runners` 默认改为 `"auto"`；当前前台 daemon 会映射成保守值 1。
- `daemon_limit` 默认改为 `0`，表示每个阶段不限制记录条数。

后续方向：
- 做真正的 `my-agent gateway start/status/stop/restart`。
- 把 `runner_concurrency` 和 `runner_start_rate` 接到后台 worker pool，而不是前台同步循环。
