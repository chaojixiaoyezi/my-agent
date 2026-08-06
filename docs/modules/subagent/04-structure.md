# Subagent Structure

本文只描述当前子代理主链路。

`SubAgentManager` 不再接收 `closeout_for_all_task_nodes`，`runner_result_service.py` 也不生成
`task_node_closeout` 副本。canonical task/result 是唯一结果事实源；父代理通过结构化 status、blockers、
findings、artifact refs 和 result payload 阅读子代理工作，再由模型向用户汇总。

## Memory Candidate 接口

- `SubAgentManagerInitParams.candidate_service` 只接收当前 owner 已创建的 CandidateService，不在
  `subagents/` 内创建候选仓库。
- `services/runner_result_service.py` 在 canonical result 已成功解析和保存后调用
  `services/memory_candidates.py`；一次批次把去重后的 lesson 与结构化 finding 转成统一
  `CandidateObservation`。
- task/run/artifact/evidence 只以 typed refs 传递；子代理自然语言摘要不决定 scope、审核状态或晋升。
- dry-run、坏 result 和未找到正式 CandidateService 时不写候选；正式结果事实仍由 canonical task/result
  持有，Candidate 只是待审核的跨任务复用提议。

## 2026-07-30 启动上下文必需字段

- `goal`、`output_contract`、`permissions`、`constraints` 和 `workspace_refs` 是子代理启动所需的
  执行与权限事实，缺失时继续 fail-closed。
- `acceptance_checks` 是可选质量说明，不再作为每个 child 的启动硬门。持续值守、调查或仅回传
  结构化 finding 的任务可以没有人工验收清单；调用方明确提供时仍原样进入同一 context bundle，
  供 child 和父代理核对。
- 这个边界适用于所有子代理，不根据 `/audit`、任务正文或角色名称分支，也不放宽工具权限。

## 2026-07-29 默认输出路径结构

- 模型或用户显式给出的输出引用保持原值，继续参加 shared-output 冲突检查。
- 编排器为了让 child 回传结果而生成的默认引用带
  `system_default_output_ref=true`。run id 产生后，唯一的 output-ref rebind 入口把它改成
  `work/child_outputs/<run_id>/<slot>-<slug>.md`；canonical state、runner context 和父级结果读取都使用
  这个真实引用。
- 批量创建前还没有 run id，因此预检查不把内部默认槽位当成跨 run 业务锁。真正的用户文件、
  artifact 和显式共享引用不享受该例外。该规则只读结构化来源标记，不解析 goal、agent 名或文件内容。

## 2026-07-29 后台 claim 与当前执行轮

- `ConversationStore.claim_background_run` 仍是同一 thread 的独占执行租约；scheduler 只有取得租约后
  才构造 `source=background_main_agent` 的运行参数。
- 该参数对精确 task 携带 per-turn `conversation_task_turn_active=true`，表示“当前轮是租约持有者”，
  使工具网关不会把它当成外部第二执行器。该标志不进入 owner 长期状态，也不由模型提供。
- 其他前台、后台或不同 task 的轮次没有这个当前轮事实，继续经过
  `conversation_workspace_execution_blocker`。因此续派 child 的可用性修复没有放宽并发写边界。

## 2026-07-28 工具能力继承边界

- `services/hierarchy/scheduler.py` 把已经解析的 child role 与父 task 的当前 `allowed_tools` 一并交给
  `services/hierarchy/tool_policy.py`。
- `tool_policy.scheduled_child_tools` 先整理显式请求与角色候选，再统一与父工具集合求交集。
  worker 随后移除 child-creation 工具；coordinator 不做额外扩权。所有后代因此只能沿树继续减法。
- task-local child 虽保存父 conversation/task id 作为结构化 lineage，但工具准入读取其既有
  `context_scope=task_local`，不进入主 conversation execution lane 的防双执行判断。
- 这条链复用现有 role/template/scheduler，没有新增研究型、编码型、测试型等底层 Agent 分类，
  也没有保留旧的“默认工具表自动补权”兼容分支。

## 2026-07-27 共享工具历史窗口

- 子代理没有独立的 live tool-context compactor。每次 provider 调用都与主代理共用
  `agent_core._tool_loop_service` 中的工具历史窗口入口；原生协议的完整请求计量和整对回收也在这里完成，
  不是 `subagents/` 下的专属 service。
- `agent_core.tool_ir_compact` 只负责对 provider-neutral IR 做 ToolCall/ToolResult 整对删除；
  当前模型窗口、90% 阈值和 recent-tail 都读取公共 `RuntimeCompactPolicy`。子代理仍以自己的
  run workspace 保存事实，但不拥有另一套上下文算法。
- `live_context_compaction` 已从 runner context、result payload、finalization 和父代理结果投影删除。
  通用 Compact 仍负责运行上下文压缩；结构化 task state、原始运行证据和持久 conversation
  transcript 各自维持原有职责，不新增兼容分支。

## 2026-07-27 Compact 路线收敛

- 主代理和子代理都调用 `memory_archive` 的同一套 Compact。区别只来自运行时注入的 workspace：
  主代理使用当前 conversation workspace，子代理使用
  `tasks/<date>/<task>/work/agents/<run_id>/`，不是两套算法或两套阈值。
- 子代理专属 Compact service、session continuation、continue-packet 转接层、owner/run/agent
  Compact 索引和独立恢复目录均已删除。子代理只保存通用
  `compact_applies`、checkpoint/state/summary 与原始运行证据。
- 当前 task 的结构化 goal 和 next actions 是恢复后的最高任务权威；旧摘要、归档包装和读取游标只能
  补充事实。只有任务显式声明 `full_source_read` 时，未完成读取游标才可成为下一动作。
- 子代理仍隔离在自己的 run home；它不拥有第二份长期 Memory，也不能读取父代理或其他 owner 的
  私有人格、Memory、Skill 或任务文件。

## 核心链路

```text
SimpleAgent orchestration tool
  -> SubAgentManager
  -> services/base.py creates SubAgentTask
  -> services/persistence writes canonical state
  -> runner worker runs model/tool loop
  -> services/runner_result_service.py records result/artifacts/status
  -> parent inspects tree / cancels / takes over / summarizes
```

## 入口

- `agent/subagents/manager.py`：子代理 root manager，负责初始化、基础生命周期、工单路径和服务组合。
- `agent/subagents/kernel.py`：从 canonical state、projection 和 task workspace 生成稳定树快照。
- `agent/subagents/parsing/`、`agent/subagents/rendering.py`、`agent/subagents/role_templates.py`：
  放当前子代理协议解析、展示渲染和模板策略；不再保留单独一跳 facade。
- `agent/agent_core/subagent_mixin.py`：子代理生命周期入口，包含 run/finalize、结构化修复、
  recovery snapshot 和 parent planner 记录；旧私有 repair/planner mixin 不再作为跳转层存在。
- `agent/agent_core/subagent/params.py`：子代理生命周期和 parent planner 参数类的权威位置。
- `agent/subagents/service_window.py`：持续型委派语义(A4)的唯一事实源——
  `service_window_remaining_seconds(task)` 按 attributes.long_running +
  service_window_seconds + created_at 算值守窗口剩余;消费方=子代理收口抑制
  (`agent_core/subagent/progress_closeout.py`,窗口未走完不因落产物自动 DONE)与
  父侧 wake 载荷(`subagents/runner_completion_wake.py`,`service_window_incomplete`
  结构化事实)。
- `agent/subagents/runner_completion_wake.py`：runner 终态只通过
  `ConversationStore.append_observation_with_wake` 发布父级通知。该入口保证 wake-first 顺序、双向 ID
  关联和 observation fallback；禁止恢复成两个彼此独立的 append/raise 调用。
- `agent/agent_core/orchestration/`：主代理模型可见的 `create_subagents`、`dispatch_subagents`、`inspect_agent_tree`、`cancel_subagents` 等工具实现。
- `cli/gateway_loops.py::_GatewayOrphanReconciler`：不执行模型的独立周期控制器；从 owner
  投影发现未完成 run，再调用 orchestration 层现有的结构化孤儿监督。它与后台主代理的 LLM
  scheduler 分线程运行，但不建立第二套恢复状态机。
- `agent/agent_core/runner/`：子代理 worker、prompt、session heartbeat、timeout policy。
- `agent/subagents/runner_session_liveness.py` 与
  `agent/agent_core/orchestration/tools/cancel.py`：`runner_session.in_process` 区分 Gateway 内线程与
  独立子进程；前者只能协作中断，后者才可发送操作系统信号，禁止把宿主 PID 当 child PID。
- `cli/subagents.py`：子代理 CLI 命令和注册入口，包含基础、监控、层级和 leadership recovery 命令；不再通过单独 registration / hierarchy 注册文件跳转。

## 状态和路径

- 权威状态：当前 task workspace 的 `work/agents/<run_id>/canonical_state.json`。
- runner-session heartbeat 是 canonical state 内的窄 lease 事实，不是任务内容转换。
  `persistence.save_runner_session` 在 run-local guard 内只改 session/heartbeat 与轻量
  locator mtime；`persistence.save` 的完整 workspace/compact/projection 同步也持同一
  guard。禁止把周期 heartbeat 再接回完整保存链。
- 模型可见的子代理工作根：当前 task workspace 的 `work/agents/<run_id>/`。旧
  `.my_agent/subagents/<run_id>` 只做 locator / owner projection / 查找索引，不是
  `task_dir`、write root 或 artifact root。
- 子代理的 runner identity、父 conversation task lineage 与当前 cwd 是三项独立结构化事实。
  `conversation_task_id` 只表示结果和生命周期归入哪个父任务；child 由精确 owner task 绝对写路径
  重绑定 `run_workspace` 时，只改变当前 runner cwd，不得 reopen、supersede 或 select 全局会话任务。
  后续 task promotion 只验证父 link 仍有效，不得再以父 task path 覆盖 child cwd。该边界对照 会话运行时 的
  `parent_thread_id + config.cwd` 和 通道运行时 的 `parentSessionKey + childSessionKey` 分离关系实现。
- 状态机：完成只写 `DONE`；失败/阻塞只写当前协议枚举，不把旧标签、大小写变体或自然语言别名提升为机器状态。
  `failure_type` 也一样：runner/action 原始结果可以留作审计文本，但写入 `task.failure_type`、
  重试、恢复和验收前必须是当前已知枚举；未知值不能靠小写化或旧标签兼容变成机器状态。
- 状态判断走 canonical state 和 `subagents.models` 中的 `TaskStatus` /
  `VerificationStatus` helper；旧 `subagents/state_machine.py` 私有转换表已删除，
  避免 `WAIT_CHILD` 等历史状态绕过当前协议。
- 恢复候选、agent tree bucket、due-check、leadership recovery 和 runner 结果
  payload 不再各自维护失败/完成状态集合；这些机器判断从 `subagents.models`
  读取当前协议集合，未知旧标签只保留为审计文本。
- 恢复模式和 capability 等待状态也只认当前结构化枚举。未知 `rerun_*` / `takeover_*`
  前缀、`NEEDS_TOOL` 这类旧别名、工具错误正文，都不能触发自动重跑、接管、授权或验收状态变更。
- 子代理 runner 默认复用主代理当前 `AgentConfig`，包括 `model_context_window_tokens`、
  `memory_compact_auto_trigger_percent`、`runner_timeout_seconds`、runner 并发和工具预算。
  只有任务自己携带结构化 `config_overlay_ref` 时才形成 run/task layer 覆盖；不要为
  子代理 compact 或常规真实测试另建第二套参数。
- 自适应重试拆分父任务时不再写历史自定义状态 `SPLIT`；父任务进入当前协议
  `TAKEN_OVER`，拆分关系只记录在结构化 `attributes.split_into` 和 `child_ids`。
- capability request 的打开/终态判断集中在 `model_capabilities.py`。`OPEN` 代表待处理，
  `GRANTED` 代表已授权且可避免重复申请，`GAP` 和 `CLOSED` 是当前终态；旧
  `RESOLVED`、`APPROVED`、`REJECTED` 不再被 kernel、protocol、runner、board 或
  runner context 静默当成当前终态。
- capability route 自动匹配只读结构化能力字段：`needed_capability`、requested tool/skill/mcp/
  command、constraints 和 scope。任务目标、问题描述、期望输出、证据摘要这类自然语言
  只用于人类审计和模型理解，不能参与自动 grant query。
- 子代理过程文件：`work/agents/<run_id>/...`。
- 用户最终交付：主代理汇总后写当前 task `output/`，或用户显式指定的输出目录。
- 当前 run 没有用户显式指定输出目录时，`output_files` / `output_refs` / `artifact_refs`
  里的相对路径默认归一到当前 task `output/`；项目文件写入必须来自明确项目路径、
  修复合同、目标 refs 或 `extra_write_roots` 等结构化授权。
- owner projection：`owner_home/agents/<run_id>/state.json` 保存可重建索引和当前状态投影，用于
  tree、恢复、跨 session 查找以及 Gateway 冷启动后的 owner 发现；canonical state 仍在
  task workspace，投影不能取代它成为状态权威。
- 输出路径合同只接受真实结构化路径。`[name]/file.md` 或 `【name】/file.md`
  这类括号占位符路径段会被过滤出 required refs、declared refs、write roots 和 artifact
  roots；普通自然语言说明可以留给模型阅读，但不能成为机器写入授权。
- `agent_name` 是展示名，不是层级或角色事实。默认展示名使用 `agent-d<depth>-<role>-<index>`；
  深度、权限、模板和状态仍只读结构化字段，不能从显示名、中文叫法或英文别名里反推。
- `role` 选择角色模板时只认明确模板 id；不做“字符串里包含 tester/worker 就套模板”的宽匹配，
  也不再把旧层级别名静默映射成当前模板。
- 层级继承状态写在 `attributes.inherited_parent_context`；`goal` 只承载给模型阅读的任务说明和
  父级边界摘要，不承担机器状态判断。

## Services

| Service | 负责什么 |
|---|---|
| `base.py` | create_run/split、owner 继承、runtime config scope |
| `persistence/` | canonical state 读写、projection、global index、LocalStore 投影 |
| `dispatch/` | dispatch/watch/parent planner 报告 |
| `runner_context_service.py` | 执行上下文、写入边界、任务配置、runtime guidance、runner allowed tools |
| `runner_result_service.py` | runner 输出解析、状态和 artifact refs 写回 |
| `board/` | board、due-check、action-plan（直接导入 `board.service` 等实现模块） |
| `actions/` | action-plan 应用、取消、接管动作记录 |
| `hierarchy/` | 多层级 child scheduling、recovery packet、leadership recovery（直接导入 `hierarchy.service` 等实现模块，包 `__init__` 不再转发） |
| `patch_apply/` | patch review/apply/report/rollback |
| `capability_service.py` | capability request/grant/gap 路由 |
| `memory_gate/` | 子代理 task-local 候选经验，不自动写长期记忆 |

可复用执行方法来自同一 `SkillsService` snapshot；当前任务计划来自 `task_progress`；子代理执行只走上面的原生编排工具，不存在第二个 workflow service。

## Recovery And QA Signals

恢复器只根据 `BLOCKED`、`FAILED`、`TIMEOUT`、`CHANNEL_ERROR` 等结构化状态和 refs 行动。
dispatch、runner summary、parent-timeout recovery、通用 Compact work-state 和 board risk
使用同一组状态 helper 判断 done/verified、failure、ended、dispatch-ineligible 和 handled terminal，
不在各自模块维护额外的状态别名表。
QA 失败只来自任务状态、结构化 `ok: false`、`passed: false`、blockers、测试记录或读取错误；
`ERROR`、`FAILED` 这类写在 summary/旧 payload 里的普通词不会自动触发 repair wave。
runner 成功也必须有机器事实：可解析 `SUBAGENT_RESULT`。原始和 repair 回复都缺结构化结果时，finalizer 必须写
`BLOCKED/UNVERIFIED/structured_output_parse_error`，禁止用普通正文或空正文回填 DONE/VERIFIED。

## Guidance

运行中补充提示统一落 conversation guidance 账本。目标可以是 agent run、thread、task 或 case。子代理下一轮执行上下文会读取点名给自己的 guidance，并在 runner prompt 中显示。它只是补充上下文，不是机器验收门，也不自动替模型完成任务；停止、取消、接管和验收要走对应结构化状态或控制入口，不能靠解析自然语言 guidance 来改写任务合同。

## Cancel And Takeover

主代理可以用 `cancel_subagents` 按 run_id/root/status 取消下级。取消会写 CANCELLED/ABANDONED、废弃 active attempt、尽量 interrupt/terminate 已知 pid/session，并写审计记录。该工具只处理能被当前 canonical loader 正常读取的 run；账本损坏时返回结构化 load error，不私自扫描旧 locator 或其他目录兜底。主代理说明取消/接管原因后，可以继续汇总和验收。

takeover replacement 的来源权威入口是 `context_bundle.takeover`：创建时由
`services/takeover/refs.py::source_handoff` 生成有界结构化快照，包含 source run id、状态、
未完成步骤、摘要、阻塞项和 refs。模型不应使用普通文件工具读取 canonical/checkpoint 等
受管状态面；refs 是按需证据指针，不是启动前置条件。旧 attributes 中只有 refs 而没有
handoff 的 run 保持可读，但新创建/重新合并的 takeover 必须补齐 handoff。

## Create-Time Boundaries

`create_subagents` 只负责结构化派工、目标路径、写入安全和 lineage 记录。业务质量约束
（例如按钮是否可用、图片是否可验、注释是否允许）可以随任务上下文传递，但不能变成
派工入口硬门；父代理应在读取子代理 refs 后验收或安排 QA。

## Collaboration Capabilities

子代理模板或创建属性可以显式声明 `capabilities`、`collaboration_capabilities` 或
`provided_capabilities`。协作路由按这些字段和真实工具名匹配，不从工具名子串、角色说明
或 summary 自动推断 `query`、`write`、`delegate` 等抽象能力。

## Artifact Rule

子代理可以写自己的过程产物和协作文件，但最终用户交付由主代理汇总。子代理 `final_report.md` 这类内部文件只作为证据/引用，不会自动变成用户最终交付。

子代理结果回报里的产物入口只认当前结构化字段：`artifacts`、顶层
`artifact_refs`、`evidence kind=artifact` 和 `evidence_packets[].artifact_refs`。
`deliverables`、`files_modified`、顶层 `file_path/path` 这类历史别名不会被恢复成
artifact refs。创建任务时给子代理的 `output_files` 是目标路径合同，不是结果回报别名。

声明产物只表示预期交付，不授权 runtime 自动创建内容。runner result 只登记确实存在、
可解析到当前任务工作区且通过 registry 校验的文件；缺失声明保持缺失，交给父代理根据
结构化缺口继续工作或报告。禁止从另一个文本产物按后缀或同名搜索复制，禁止把 summary/
findings 渲染成占位文件。唯一允许的收尾复制是 `output_delivery_map` 明确记录的真实
`source -> target`，并且 source 必须存在、target 必须仍在声明写围栏内。

`create_subagents` 的模型入口只有单 `goal` 和明确 `items` 两种形态，不克隆同一份任务。
多个 child 必须在 `items` 里声明不同工作；需要精确写不同业务文件时，每个 item 显式声明
自己的输出路径。顶层交付目标归父任务，不会暗中复制到所有 child。

## 2026-06-10 Facade 清理

- `services/__init__.py` 不再 re-export 各 service 类；`SubAgentManager` 与所有调用方直接导入实现模块。
- 单模块包 `services/capabilities|runner_context|runner_result/` 打平为 `capability_service.py` / `runner_context_service.py` / `runner_result_service.py`。
- `services/board/__init__.py`、`services/hierarchy/__init__.py`、`parsing/__init__.py` 尾部 re-export 删除；envelope/hierarchy 调用方直连实现模块，消除模块级循环导入。

## 2026-06-10 合约身份去重合并

- `idempotency_contract_identity.py` 与 `repair_contract_identity.py` 合并为
  `contract_identity.py`：两类合约共用同一套 pack 遍历与字符串归一化 helper
  （`_iter_packs` / `_string_tuple` / `_normalized_path`），原来逐字重复三份；
  合并后各自身份计算用 `_idempotency_identity` / `_repair_identity` 区分，
  共享 helper 留一份。调用方（create_context / create_constraints /
  hierarchy/schedule_idempotency）改导入合并后模块，行为不变。

## 2026-06-11 产物落点投影层

- 新增 `services/output_alignment.py`：纯投影、不改 task。`anchored_output_refs(task)`
  产出 OutputAnchoring（anchored_refs / delivery_map / warnings）；
  `anchor_refs_for_execution(task, refs)` 给合同投影层批量翻译目标 refs。
  消费方：`context_bundle_contracts.task_packet/output_contract`（执行合同）、
  `result_artifact_evidence.deliver_anchored_outputs_to_declared`（收尾搬运）、
  `delivery_closeout/subagent_aggregation`（声明对账复用 looks_like_output_path）。
- `agent_core/orchestration/tools/capability.py`：`resolve_capability_requests` 工具
  实现（grant/deny + 安全围栏 + wake）；注册链 core.py → orchestration_tools.py。
- `services/lifecycle.py`：capability request/grant/gap 与 evidence 生命周期写入；grant 保存后只对精确
  `capability_request` 阻塞 child 做 conversation link 的 `blocked -> active` CAS，使同 run 可以续跑，
  但 cancelled/terminal link 不会被复活。
- `services/runner_context_service.py`:`task_product_write_roots` 子代理产物写区
  (过滤自己 agent 目录与 report 区);为空时 `_task_workspace_fallback_roots`
  回退任务工作区 output/work,保证子代理总能写产物(batch3 C3/G4 修复)。
