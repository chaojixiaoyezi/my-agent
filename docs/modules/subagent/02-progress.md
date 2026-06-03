# Subagent Progress

父验收旧链路已经移除。子代理只负责执行自己的任务、写结果和证据引用；上级通过任务树、状态、refs 和普通 closeout 继续推进。

## 2026-06-03 worker/session、grant wake、patch apply 审计与 runtime overlay

- runner worker 现在写 `runner_session_pool.v1` session lease，包含 worker pid、heartbeat、完成/失败状态和最近 session history；这是长期 worker/session 可观察账本，不替代 runner result。
- 子代理创建支持从 attributes 显式写入 `config_overlay_ref`，child 默认继承 parent overlay；takeover 会保留 runtime config scope，并标出 takeover record。
- 子代理 worker 执行前会把 task `runtime_identity.config_overlay_ref` 装载成 run/task scoped config layer，并把有效 `config_sources/config_layers/warnings` 写回 task attributes。
- capability grant 后会创建 observation 和 wake signal，并写回 `capability_grant_wake`；失败则写结构化 `capability_grant_wake_error`。
- patch apply record 增加 owner policy、batch validation 和 failure recovery 证据，覆盖 applier、owner policy snapshot、write roots、locked files、测试结果和 rollback 下一步。

## 2026-06-03 树状态、轮询和旧 workspace 污染修复

- `SubagentKernel` 选择 `source_refs` 时优先最新可见 task workspace，避免同 owner/date/slug 下旧任务排在前面时把父代理带回旧目录。
- `inspect_agent_tree` 节点增加 `not_done_reason`、`running_seconds` 和 `seconds_since_progress`，父级能看见未完成原因、运行时长和多久没进展。
- `inspect_agent_tree.child_result_index` 不再只展示完成后的 artifact。运行中的子代理即使还没有 `artifact_refs`，也会暴露 `final_report_ref`、`summary_ref`、`checkpoint_ref`、`agent_work_dir`、`recent_tool_trace`、`not_done_reason` 和 `readiness`，方便父代理等待、引导或验收，不再因为 `primary_artifact_refs=[]` 就乱扫 task/work 目录。
- 相同范围短时间重复调用 `inspect_agent_tree` 会返回 cooldown 缓存快照和 warning，不再让父代理高频轮询放大状态扫描。
- 新增 `cancel_subagents` 控制面工具，父级可以按 run_id/root/status 取消下级，并写 CANCELLED/ABANDONED、审计和 attempt 废弃记录。
- 后台启动进程如果秒退、参数错或 import 失败，会立刻标记 `CHANNEL_ERROR` / `channel_status=BROKEN`，不再伪装成仍在 planning。

## 2026-06-02 canonical state 与派生投影收敛

- 子代理详细状态的权威位置收敛到当前任务工作区的 `work/agents/<run_id>/canonical_state.json`。
- 旧工单目录中的 `task.json` / `run.json` 只保留 locator/projection 作用；读取时如果 canonical state 存在，会自动跳回权威 payload。
- owner 侧 `owner_home/agents/<run_id>/state.json` 现在是 refs-only projection，只用于 tree、doctor、compact 和恢复发现，不再当第二套事实账本。
- 保存链路改为先写 canonical state，再同步状态报告、owner projection、global index、LocalStore 等派生投影。
- 派生投影失败不会回滚 canonical save，也不会把任务改成失败；失败摘要会写到工单目录的 `projection_warnings.json`，方便父代理或人工排查。
- 子代理结构化结果里的 artifact 路径会优先登记到统一 artifact registry；缺失或暂时解析不了的路径只作为恢复线索保留，不在 runner 层直接阻断任务。
- `static_site_*` 平铺文件收敛到 `subagents/static_site/`。静态站点测试仍只通过
  `run_static_site_check` 公开入口调用；parser/check/record helper 不再散在
  `subagents/` 根层。
- `execution_*` 平铺文件收敛到 `subagents/execution/`。测试执行器、测试记录、
  测试报告和测试项准备保留包级公开 API；具体 file/content/pytest/static-site
  helper 不再散在 `subagents/` 根层。
- `services/hierarchy_*` 平铺文件收敛到 `subagents/services/hierarchy/`。层级调度、
  层级恢复和 QA 波次建议仍由同一套 service 使用；agent naming、write policy、
  idempotency 和 role/tool policy helper 不再散在 `services/` 根层。
- `services/patch_apply_*`、patch review helper 和 patch spec normalizer 收敛到
  `subagents/services/patch_apply/`。patch apply / review / test command / record
  helper 仍服务同一套 patch flow，不新增第二套 patch 执行入口。
- `takeover_*` 平铺文件收敛到 `subagents/services/takeover/`。接管 readiness、
  source refs 和幂等 takeover run 创建仍服务原有恢复编排，不改变接管深度、
  写入根继承或 load_error 语义。
- `services/board*.py` 平铺文件收敛到 `subagents/services/board/`。看板核心服务、
  facade、due-check、action-plan 和 board item helper 仍只服务同一套 tree/board
  可观察状态，不新增第二个状态事实源。
- `services/action*.py` 和 `actions.py` 平铺文件收敛到 `subagents/services/actions/`。
  动作 apply service、options、params、records、handler、leadership 和 takeover handler
  仍服务原有 action-plan 写回流程，不新增第二套恢复/接管入口。
- `services/indexing*.py` 平铺文件收敛到 `subagents/services/indexing/`。索引
  service、params、本地记录、dispatch/watch 记录索引仍服务原有 LocalStore / report
  indexing 流程，不新增第二套索引事实源。
- `services/persistence*.py` 平铺文件收敛到 `subagents/services/persistence/`。
  持久化 service、模型归一化、投影同步、recovery output、状态报告、安全/继承/失败交接
  helper 仍服务同一套 canonical state 保存流程，不新增第二套事实账本。
- `services/dispatch*.py` 平铺文件收敛到 `subagents/services/dispatch/`。dispatch
  service、params、report/watch builder 和日志 appender 仍服务同一套调度报告写回流程，
  不新增第二套调度状态源。
- `services/leadership_recovery*.py` 平铺文件收敛到
  `subagents/services/leadership_recovery/`。领导权恢复 plan/apply 仍服务原有恢复编排，
  不改变 coordinator handoff 语义。

## 2026-06-02 自学习候选去重收敛

- learning draft 去重不再把“短教训出现在长复盘里”直接当成同一条候选。精确相同仍直接合并；短文本之间的合理包含仍保留高相似度；长文本改走词元和短语片段相似度。
- 去重相似度现在组合 word token、word shingle、字符片段、编辑相似度和长度差封顶；它只影响 learning draft 候选合并，不会提升正式 skill，也不会阻断任务。
- 验证样例里，同一经验换说法可以超过候选合并阈值；不相关文本保持低分；短教训嵌在长复盘里低于合并阈值。
- 这样保留了“相同经验多次出现就提高置信度”的能力，同时避免一篇长报告因为包含某句局部经验，就把多个不同 lesson 误合并成一个候选。
- 参考 通道运行时 的短文本不靠 substring 误判重复、终端交互 的明确来源/索引去重思路后，当前实现仍保持自学习草稿层，不提升正式 skill、不新增硬门。

## 2026-05-27 子代理权限和记忆配置

- 子代理新增有效权限快照 `effective_permissions`：父级 `restricted` 会继续下传 `restricted`，父级 `workspace-write/full-access` 下的子代理最多拿 `workspace-write`，不会自动继承全盘 shell 权限。
- `run_command` 会从执行上下文的 `write_boundary.shell_access_mode` 读取系统下发权限；模型参数不能给自己提权。
- 默认子代理工具集补齐基础读、搜、artifact、网络、写文件、打补丁、受控命令、只读树状态、协作和能力申请工具，避免“少填 tool_preset 就创建残废代理”。
- `inspect_agent_tree` 会按当前身份裁剪：主代理可看全树；子代理/孙代理只看自己和后代，不能用 root 参数读平行子树。
- runner 内部的 `dispatch_subagents` 会固定在当前 run 的直接孩子范围内；模型显式传入其他 `parent_run_id` 也不会跳到平行子树。
- runner 内的消息、能力申请和协作证据入口同样按当前身份裁剪。模型如果传错 `sender_run_id`、`source_agent_id`、`requester_agent_id`、`actor_agent_id`，系统不会替别的 run 写账；工具结果会把被忽略字段放进 `scope_resolution.ignored_explicit`。
- `schedule_child_subagents` 现在和 `create_subagents` 一样默认创建后后台启动；启动参数会保留当前 runner 作为 parent scope，不会把孙代理挂到顶层或平行子树。
- 子代理任务级记忆命名空间改为 `subagent:{root_run_id}:{run_id}`，并把保留策略写入 `attributes.memory_scope`。新增配置 `subagent_memory_retention_policy`、`subagent_memory_delete_after_days`、`subagent_destroy_summary_required`；这些只描述归档/清理边界，不是新硬门。
- 为保持 code-size 清零，把后台启动逻辑拆到 `agent_core/orchestration/background/dispatch.py`，把 kernel 数据模型拆到 `kernel_models.py`；行为保持 refs/tree/status 路线不变。

## 2026-05-27 调度卡点清理

- 删除 orchestration 最终回答硬门、dispatch 本地收口、runner artifact integrity 收尾改写和 hierarchy scope/duplicate 硬门。
- `dispatch_subagents` 只把索引、状态和 refs 交回给父级；系统不再替父级生成完成/失败结论。
- 层级 scheduler 只负责按父级显式 spec 创建子任务，不再因为领域、重复、QA 顺序或写根漂移直接阻断。

## 2026-05-27 产物缺失不再改写 runner 状态

- 删除 `result_artifact_integrity.py`。子代理在结构化结果里声明了某个本地产物 ref，但文件暂时不存在时，runner 不再直接改成 `BLOCKED/missing_artifact_refs`。
- 删除 artifact integrity 专用 repair advice。调度返回里不再生成 `artifact_integrity_repair_advice` 或“创建专门修复子代理”的固定建议。
- 产物缺失、路径不对、内容损坏和格式不合格，统一回到普通 closeout / 父级模型判断 / 任务树状态里处理。

## 2026-05-27 最近工具轨迹

- runner 工具观测会把最近 5 条工具调用摘要写入任务 `attributes.recent_tool_trace`。它只记录工具名、是否成功、时间、摘要和可选路径，不保存大正文。
- `inspect_agent_tree` 会把这些摘要放进节点的 `recent_tool_trace` 和 `evidence_layer.recent_tool_trace`。父代理可以据此判断子代理是否还在真实推进，而不用读取完整模型对话或大产物。
- 这不是新门，也不改变任务状态；工具失败、产物缺失和返工仍由普通状态、blockers、refs 和 closeout 处理。

## 2026-05-28 后台启动和进度投影

- `create_subagents` 的真实模型后端会拉起独立 `subagents-dispatch` 后台进程，并把
  `background_start.status` 写回任务树：`launching` 表示刚启动，`running` 表示后台
  dispatch 已接手，`finished` 表示本轮启动执行完，`failed` 会带错误摘要。
- 这个标记只是可观察状态，不是新硬门。父代理看到 `failed` 后可以重派、接手、问用户
  或继续看其他子代理，不会因为这个字段自动终结整个任务。
- 工具进度现在会同步更新 `SubAgentTask.progress`：成功只读工具给一个很小的非零进度，
  成功写入产物给更明显的阶段进度，runner 记录 `DONE/COMPLETED` 后归一为 `1.0`。
- 这样父代理看 `inspect_agent_tree` 时能区分“刚创建但没动”“正在调用工具”“已经完成”，
  不需要读完整模型对话，也不会把只读工具归档误当成用户产物。
- `defer_start` 可以写在单个 `items[]` 子任务上。生产 worker 仍默认创建即启动；
  测试、找错、验收、汇总这类依赖前置产物的子代理可以单独挂起，等产物 refs 出现后
  再由父代理显式启动。系统只给调度建议，不用硬门替模型判断。
- `create_subagents` 只支持 `replacement_for_run_ids`。父代理派接管/修复子代理时，把被替换的旧 run 写进去；
  系统会把旧 run 标记为 `TAKEN_OVER` 并记录 `takeover_by`，后续 tree/board/dispatch
  默认不再把旧 run 当成活跃任务。旧 run 的记录和证据仍保留，方便审计和恢复。
- 子代理 runner 完成或失败后，会把状态更新写入绑定的长期会话，并投递一条普通
  wake signal。这个信号只负责叫醒父代理继续看树、派测试、找茬、补派或汇报；
  不做本地验收、不替父代理抢答，也不把任务卡进新的等待态。

## 2026-05-31 owner-bound 子代理收敛

- 子代理创建时默认继承当前 owner_id；外部平台用户、CLI 主账号和后续群空间的子代理不会再靠路径猜归属。
- `effective_permissions` 增加 owner_id、owner_home、owner policy 派生的 disabled_tools、max_subagents、max_depth。它是系统派生快照，不接受模型自己提权。
- 工具上下文会过滤 owner policy 显式禁用的工具；父级没有的工具/权限不会因为子代理层级变深而突然出现。
- 子代理保存时会在 `owner_home/agents/<run_id>/` 写 refs-only projection，里面只放 state 和 refs，不复制大正文、不制造第二套事实源。
- 子代理保存还会刷新当前任务目录的 `work/compact/task_rollup.json`。父代理恢复时先看任务级 rollup，就能知道哪些 child run 完成、卡住、产物在哪里，再决定是否深入某个 child compact；旧 `tasks/<root_id>/compact/...` 只作为迁移期读取兼容。
- 如果 manager 持有 `home_paths`，子代理保存会登记 `global_index/active_agents.jsonl`；这只是发现索引，不替代 task/run/agent 工作区正文。
- 如果 manager 持有 `home_paths`，子代理保存会同时登记 `global_index/active_tasks.jsonl`、`active_runs.jsonl` 和 `active_agents.jsonl`。父代理、tree、doctor 和 compact 恢复共用这套轻量发现入口；完整权威状态在 task-local `work/agents/<run_id>/canonical_state.json`，旧工单目录只保留同 payload 镜像和兼容定位。
- 旧 `subagent_workspace` 继续作为 runner 工作目录兼容入口；owner projection 只是统一查找、恢复、tree 和 compact 的索引视图。

## 2026-06-02 子代理账本读取错误可见化

- 新增统一运行时错误报告 `runtime_error_report()`。本地 IO、解析、编码和账本损坏类错误会被压成小的结构化 payload，
  给模型/父代理看；代码 bug 仍标成不可恢复，不能伪装成“没有数据”。
- `dispatch_subagents` 生成 `current_turn_run_state` 或 `child_result_index` 时，如果 `subagents.load(run_id)`
  失败，会返回 `task_load_errors` / `load_error`，状态为 `LOAD_FAILED`。
- 父代理现在能区分“子代理没有产物”和“子代理状态账本读取失败”。前者按任务继续催产物或补派；
  后者应刷新 tree、重建索引或接管恢复，而不是误判子代理没干活。
- `create_subagents` / `schedule_child_subagents` 创建 run 后，如果会话绑定 `bind_task()` 失败，
  工具结果会返回 `conversation_bind_errors`。子代理创建和启动不因此取消，但父代理能知道“会话回路账本有问题”，
  而不是把它误解成子代理没有异常。
- 这不是新硬门。它只把原来被吞掉的异常暴露出来，不会因为某个读取失败直接终止任务。

## 2026-06-02 协作 case 响应覆盖账本

- `inspect_collaboration` 的 case 状态新增 `response_coverage`。它按 request 汇总目标数量、已响应数量、
  未响应数量、不可达数量，以及每类目标的短样本。
- 这个字段解决的是大规模协作时的可读性：父代理不用展开上千个 request/evidence 行，也能知道“哪些目标回了、
  哪些没回、是否有不可达目标”。
- `CollaborationCoordinator` 关闭收集窗口时，也会把 `response_coverage` 写进 decision/wake metadata。
  主代理醒来后看到的是结构化覆盖账本，而不是只看到自然语言摘要。
- 这不是验收门，也不会因为未响应目标阻断任务。到 deadline 后，case 可以带着已回、未回、不可达信息继续推进，
  由发现者、父代理或主代理判断下一步。

## 2026-06-02 子代理恢复编排账本

- 新增 `SubAgentRecoveryOrchestrator`，统一消费 `SubagentRecoveryStrategy` 输出的 refs-only 策略。
- 编排结果只分成几类通用步骤：续跑原 run 的 dispatch 建议、幂等 takeover、leadership recovery 计划、人工检查、无动作。
- 每一步写入 `subagent_recovery_ledger.jsonl`，记录建议动作、实际编排动作、是否 dry-run、是否 applied、是否还需要父代理 dispatch 或人工裁决。
- 默认不自动改变任务树；只有调用方显式 `apply=True` 时才会复用已有 `create_takeover_run()`，避免恢复建议散落在多个模块里又互相看不见。

## 2026-05-28 模型可见路径收敛

- `inspect_agent_tree`、`create_subagents`、`dispatch_subagents`、`schedule_child_subagents` 和 CLI/人工看板这类模型可见状态输出，只投影当前布局的 `task_root`、`work/agents/<agent_id>`、artifact/evidence refs；旧式 `data/subagents/...` 路径仍留在内部兼容恢复文件中，但不会被占位符遮住后继续返回给模型。
- 当前内部编排/状态工具的 tool-output 归档也会写入当前模型可见正文；更早生成的旧归档在经 `read_artifact` 展开时会按来源工具再净化一次，避免历史 `create_subagents` / `inspect_agent_tree` 结果把旧路径重新带回上下文。普通 `read_file`、网页、命令输出和用户产物正文保持原文。
- `create_subagents` 的模型可见输入统一使用 `input_refs` / `context_manifest` 描述“交给子代理自己读的资料线索”。旧 `required_read_paths` 只保留在解析兼容层，避免继续把旧字段教给模型。
- 旧式 work-order 目录不再作为状态面主路径返回，避免父代理接管时按 `data/subagents/<run_id>/...` 猜旧路径或直接 `read_file` 旧账本。
- 2026-06-02 继续补齐：`context_bundle`、`task_envelope`、runner prompt 摘要、board goal/summary 和 child result summary 也走同一套模型可见路径投影；旧路径夹在自然语言里时只保留可用文件名线索，不再把 `data/subagents/...` 作为下一步事实暴露给模型。
- 父级读取子代理结果时，应优先用 `agent_work_dir`、`final_report_ref`、`artifact_refs` 和 `evidence_refs`，不要自己拼子代理目录。
- 如果模型仍然拿旧路径或抄错路径去读，`read_file/list_files/search_text`
  会返回 `path_not_found=true`、`candidate_paths` 和下一步建议；这只是恢复提示，
  不会自动读取候选、不会申请新权限，也不会把任务改成失败。

## 2026-05-28 Artifact Registry 收敛

- 新增统一 artifact registry，产物登记后得到 `artifact_id`。同一产物移动、
  重建或修复时更新同一个 `artifact_id` 的最新记录。
- `write_file` 等工具结果会暴露机器路径，工具归档会把真实存在的输出文件登记进
  registry；子代理结构化结果里的 `artifacts/file_path/output_path` 也会登记。
- `dispatch_subagents`、`inspect_agent_tree` 和 closeout
  都优先返回 registry 的 `artifact_id/path/registry_ref`。旧路径字段还保留，
  但只作为兼容投影。
- closeout 支持 file group，但 file group 必须先进入统一 artifact registry。
  `.agent_delivery/artifacts_manifest.json` 不再是权威账本，避免模型或子代理写出第二套账导致
  父代理、tree、closeout 和人工排查看到不同事实。
- `.agent_delivery/closeout.json` 只是系统验收报告快照，用来解释本轮通过/返工原因；
  它不能替代 `data/artifacts/registry.jsonl`，也不应该由模型在产物目录里手写。
- 显式声明 `delivery_mode: message` 或 `requires_artifact: false` 的任务可以没有落盘产物。
  这类任务仍会写 closeout 报告，但不会触发 `ARTIFACT_REF_MISSING`。
- 这不是新硬门。registry 只解决“谁是最新产物事实”的问题；缺产物、坏格式和内容质量
  仍由普通 closeout 或父级模型根据任务目标处理。

## 2026-05-28 看板秒数和汇总准备度

- `inspect_agent_tree` 节点里新增 `running_seconds` 和 `seconds_since_progress`。它们只是观察字段：
  一个表示子代理大概跑了多久，一个表示距离最近真实进展多久。
- 父代理可以据此判断要不要查看、提醒、补派或接手，但系统不会因为秒数自动阻断任务。
- 看板顶层新增 `aggregation_readiness`，列出子代理总数、已完成数、registry 中可读产物数量、
  以及未完成 run id。它帮助父代理汇总前先读 refs 或继续推进缺口，不替代 closeout。

## 2026-05-28 旧辅助合同清理

- 删除未被生产链路调用的旧 helper：`hierarchy_capability_contracts.py`、
  `repair_goal_identity.py` 以及旧 task/real-task 兼容读取文件。
- 当前子代理进展只认真实任务账本、refs、artifact registry、工具轨迹和 closeout
  结果；不再通过这些旧 helper 生成额外能力继承文本或 repair 目标猜测。
- 这次是删死代码和假信号，不改变正常子代理创建、启动、看树、写产物和汇报流程。

## 2026-05-29 运行中补充提示

- 新增统一 `send_guidance` 账本入口。父代理、用户或兼容工具给某个 run 补一句话时，
  子代理下一轮 runner prompt 会在 `GUIDANCE_DELIVERED` 中看到，并带 guidance id、
  目标、优先级和发送者。
- 旧 `subagent_message` 工具已移除；纯补充提示统一用 `send_guidance`。
  `dispatch_subagents.runner_instruction` 只用于“补一句并立刻推进该 run”，并同步写入同一份 guidance 账本。
- guidance 是软提示，不是验收条件：不会阻断、不会替父代理做结论、不会改变原任务目标。
  子代理看到后由模型自己决定换来源、补证据、写阶段文件或上报。
- `send_guidance` 也支持批量目标：可以用 `run_ids` 点名多个 run，或用
  `target_scope=children/descendants` 给某个 run 的直接孩子或整棵下级子树写同一句软提示。
  `inspect_agent_tree` 会展示每个节点未读 guidance 数量和最近几条提示，方便父代理知道谁还没接到新要求。
