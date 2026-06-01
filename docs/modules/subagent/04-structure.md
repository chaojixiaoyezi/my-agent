# Subagent Structure

当前结构不再包含独立父验收层。`create_subagents` 默认创建并立即启动任务节点；只有显式 `defer_start=true` 才只建不跑。运行中只想给某个代理补一句话时用 `send_guidance`；`dispatch_subagents` 用于人工催办、推进卡住项、重跑指定 run、查状态并尝试恢复；任务完成后的产物检查统一由普通 closeout / 交付检查处理。

`defer_start` 支持单个 `items[]` 子任务。开发、研究、写作这类生产 worker 可以默认开跑；
测试、找错、验收、汇总这类依赖前置产物的子代理，可以在自己的 item 上写
`defer_start=true`，等产物 refs 出现后再启动。系统只给软提醒，不把“先开发还是先测”
做成硬门。

当父代理派新的修复/接管子代理替换旧 run 时，`create_subagents` 可以传
`replacement_for_run_ids`。
旧 run 会进入 `TAKEN_OVER`，并写 `takeover_by` 指向新 run；后续普通调度不再把旧 run
当活跃候选，但旧记录、证据和日志仍保留给审计和恢复。

`create_subagents` 的“立即启动”是后台启动：工具调用本身只负责创建 run、写入 `background_start` 标记、拉起 runner 调度后台进程，然后立刻把 `run_ids`、启动状态和任务树快照返回给父代理。父代理不会同步等待所有子代理完成，因此可以继续和用户对话、继续规划，或稍后用 `inspect_agent_tree` 查看进展。

真实模型后端的后台启动会调用独立 `subagents-dispatch --apply --execute-runners --run-id ... --background-launch-id ...` 进程。这个进程启动时把任务树里的 `background_start.status` 更新为 `running`，结束时更新为 `finished`，异常时更新为 `failed` 并写错误摘要；日志用无缓冲 Python 进程输出，方便父代理或人工快速看到后台 runner 是否真的启动。

后台启动使用显式运行身份加线程级兜底。每个子代理 runner 会形成自己的 `RunScope`，包含 `run_id`、`task_id`、`parent_run_id`、`root_run_id`、`root_task_id`、`depth` 和 `agent_kind`。工具调用、工具结果、归档记录和 `agent_events` 会优先写这份结构化身份；线程级 runner 上下文只作为权限上界和旧调用链的兼容兜底。这样父代理一边继续派第二个子代理时，不会被第一个正在运行的后台子代理污染身份，也不会把新子代理误挂进兄弟子树。

当显式身份参数和当前 runner 上下文冲突时，系统不静默猜。`inspect_agent_tree` / `dispatch_subagents` 会把裁决写进 `scope_resolution`，例如 `source=current_runner_context`、`effective.run_id=current-child`、`ignored_explicit.parent_run_id=sibling-parent`。这表示当前 runner 仍被限制在自己的子树里，但父级或调试者能看见模型曾经请求过外部 scope。

真执行子代理时始终通过独立 worker agent 跑。`runner_timeout_seconds=0` 只表示不设总时长超时，不表示把子代理模型回合放回父代理对象里执行。这样父代理、后台调度线程和子代理 runner 不会共享临时运行字段。

子代理的 `latest_tool_progress` 只记录真正的产物写入进度。`web_search`、`web_fetch`、`read_artifact` 这类只读工具产生的工具归档文件只是资料来源，不会被当成用户交付产物；但这些成功的只读调用仍会更新任务树里的 `current_tool`、`last_progress_summary`、`latest_summary` 和一个很小的非零 `progress`，让父代理能看出子代理仍在推进。真实写入产物会把进度提升到可见阶段，runner 记录 `DONE/COMPLETED` 后进度归一为 `1.0`。如果未来某个工具确实生成用户产物，需要在工具结果里显式写 `progress_path`、`product_path`、`deliverable_path` 或 `user_artifact_path`。

## 任务树

任务树是展示层，不是身份来源。`SubAgentManager.save()` 每次保存任务时都会从 `SubAgentTask` 的机器字段重建 `attributes.system_tree`，包括 `run_id`、`root_id`、`parent_id`、`child_ids`、状态、进度、artifact refs 和 evidence refs。即使模型在结果里写了一个假的 `system_tree`，保存链路也会覆盖成系统派生值。

真正的运行事实源是显式 run 账本：`agent_runs` 记录当前状态，`agent_events` 记录每次 run 保存和工具完成。工具完成事件会带 `run_id`、`parent_run_id`、`root_run_id`、`root_task_id` 和操作号。父级看 tree 时只是在读这些事实的投影，不再靠“当前子代理是谁”猜来源。

子代理应该做的是写自己的结果、证据引用和必要的工作文件；父级或主代理通过 `inspect_agent_tree`、`run_closeout_ref` 和 refs 看状态，不要求子代理手动维护树。

每次子代理保存时，系统会同步当前任务目录的 `work/compact/task_rollup.json` 和 `task_rollup.md`。这个 rollup 是父级恢复和汇总的入口摘要：它列出子 run 状态、refs 和最近 compact 包位置，不复制大产物正文，也不替代具体子代理的 `task.json` / artifact registry。旧 `tasks/<root_id>/compact/...` 只作为迁移期读取兼容。

调度层只创建和推进任务节点，不再用额外 scope/duplicate/QA 硬门替父级做流程裁决。需要流水线、去重或重试时，由父级根据树状态和任务目标显式安排下一步。

## Artifact Registry

产物事实统一进 `data/artifacts/registry.jsonl`。每条记录有 `artifact_id`、
`path`、`sha256`、`size_bytes`、`run_id`、`task_id`、`agent_id`、`kind`、
`mime_type`、`status` 和来源工具。工具写文件、子代理结构化结果、closeout
重验收都会把已确认存在的交付物登记进去。

父代理、任务树、看板和调度摘要现在都优先返回 registry 中的
`artifact_id`、当前路径和 `registry_ref`。旧 `artifact_refs` 仍会保留一段时间，
但只是兼容字段：如果 registry 里有记录，就以 registry 为准。模型在报告里写的
路径、旧任务目录、搜索结果里的文件名，都只能当恢复线索；系统不能把这些文本当成
最终产物事实。

当产物被移动、重建或修复时，继续更新同一个 `artifact_id`。这样父代理汇总、tree
展示和 closeout 都会看到最新文件，不会因为旧 `preferred_path` 或子代理口头路径
而读错产物。

## 三层可观察状态

`inspect_agent_tree` 的每个节点都提供同一套只读三层状态：

- `liveness`：状态、心跳时间、更新时间，用来判断代理是否还活着。
- `progress_layer`：进度、当前工具、当前步骤、最近进展摘要，用来判断是否真的在推进。
- `evidence_layer`：registry-backed 产物 refs、证据 refs、blockers、能力缺口和最近工具轨迹，用来判断结果在哪里、哪里卡住、需要谁补能力。

这三层都是观察事实，不触发调度、不执行验收、不阻断任务。父代理看到异常后可以自己决定催办、补派、接手、汇报或等待。

状态面只返回当前布局路径。`workspace_refs.task_root` 指向任务级目录，`workspace_refs.agent_work_dir` 指向具体代理运行目录；旧式 `data/subagents/<run_id>` work-order 路径只作为系统兼容恢复材料存在，不放进模型可见的 `workspace_refs`。当前布局里，任务根目录只保留 `output/` 和 `work/` 两个一眼能懂的目录；子代理、孙代理等下级代理运行窝统一在 `task_root/work/agents/<agent_id>/`，包括其 `context_bundle.json`、状态、compact 和产物引用。主代理不是当前任务的 child agent，它自己的长期 memory、compact、日志和状态仍属于 `owners/local/main`，不会写进 `task_root/work/agents/`。

`create_subagents`、`dispatch_subagents`、`schedule_child_subagents` 和人工/CLI 看板返回前也会走同一层模型可见路径净化，避免嵌套 `agent_tree` 或 `child_result_index` 把旧路径重新吐给模型。当前内部编排/状态工具输出被外置到 tool-output artifact 时同样保存净化后的正文；历史旧 artifact 被 `read_artifact` 展开时也会按来源工具净化一次，但普通文件、网页、命令和用户产物正文不做这种替换。

旧模型工具 `subagent_board` 已撤掉，避免和 `inspect_agent_tree` 形成两个状态入口。底层仍可写
`subagent_board.json` / `SUBAGENT_BOARD.md` 给 CLI 或人工排查，但模型看状态只走
`inspect_agent_tree`。

## Closeout

默认配置 `closeout_for_all_task_nodes: false`，表示只对主/root 交付做普通 closeout，子代理结果只更新状态、refs 和任务树。

如果显式改成 `true`，每个子代理、孙代理等任务节点完成时会写 `attributes.task_node_closeout`，内容来自同一份 runner 结果事实：状态、verification、artifact refs、evidence refs、blockers、findings 和 next actions。它只是返工提示和可观察事实，不创建父代理验收状态，不引入 acceptance 角色，也不把子代理卡进新的等待态。

## 权限和工具

子代理默认继承一套能正常干活的基础工具，包括读文件、列文件、搜索、读 artifact、联网检索、写文件、打补丁、受控命令执行、只读树状态、协作和能力申请工具。角色模板只追加职责重点，不应该把基础工具拿掉。`inspect_agent_tree` 是按身份裁剪的只读工具：主代理可以看全树；子代理/孙代理只能看当前 run 的 `own_subtree`，即自己和自己的后代。

层级工具分两类：顶层主代理第一次派工用 `create_subagents`；已经运行中的子代理要创建下一层，用 `schedule_child_subagents`。两者体验保持一致：默认创建后后台启动，并返回 `run_ids`、启动状态和树状态；只有显式 `defer_start=true` 或 `schedule_child_subagents.dry_run=true` 才只建或预览。真实模型后端会把启动动作交给独立 `subagents-dispatch` 进程，避免一次性 CLI 退出后把子代理线程一起带死；离线 `echo` 后端仍可用进程内线程快速跑测试。区别只在身份边界：`schedule_child_subagents` 会从当前 runner 上下文自动绑定 `parent_id`，因此孙代理挂在当前子代理名下，而不是凭模型传一个父 id。运行中的 `dispatch_subagents` 同样默认只推进当前节点的直接孩子；即使模型显式传了别的 `parent_run_id`，runner 内也会压回当前 run，并在 `scope_warnings` 里说明，避免误催平行子代理。

shell 权限按“不能比父级更大”派生：

- 父级是 `restricted` 时，子代理也是 `restricted`。
- 父级是 `workspace-write` 时，子代理是 `workspace-write`。
- 父级是 `full-access` 时，子代理仍只拿 `workspace-write`，不会自动获得全盘 shell 权限。

这个有效权限会落在 `effective_permissions` 和执行上下文的 `write_boundary.shell_access_mode` 里，`run_command` 实际执行时会读取这个机器字段。模型自己在参数里写更大的权限不会生效。

外部插件、额外系统工具和未来 skill 不默认自授。父级可以在派工时显式给 `allowed_skills`，子代理也可以通过能力申请链路请求更多工具或 skill；批准和授予仍由上级/系统决定。

owner 归属现在也会随子代理落账：

- 子代理默认继承创建它的主代理/父代理 owner_id，不再空着靠路径猜是谁的任务。
- `effective_permissions` 会记录 owner_id、owner_home、owner policy 的工具禁用列表、max_subagents 和 max_depth 等机器事实。
- 子代理保存时会在 `owner_home/agents/<run_id>/` 写一份 refs-only projection：只放 state/refs，不复制大产物正文。父代理、tree、compact 或后台恢复要找子代理时，优先用这些 refs 定位。
- 同一保存流程还会把子代理对应的 task/run/agent 三层引用写进 owner 的
  `global_index/active_tasks.jsonl`、`active_runs.jsonl` 和 `active_agents.jsonl`。
  这只是轻量地图，方便 doctor、tree 和恢复入口找到最新 task/run/agent；真实状态仍以任务工作区和 run 账本为准。
- 旧 `subagent_workspace` 仍是运行兼容入口，避免破坏现有 runner；owner projection 是同一份任务事实的索引，不是第二套任务账本。

## 记忆和压缩

子代理有自己的任务级记忆命名空间，格式为 `subagent:{root_run_id}:{run_id}`。这和主代理长期记忆是隔离的：子代理工作时可以保留本轮恢复线索、compact 包和阶段总结，但不会自动写入父级长期记忆。

子代理记忆保留策略来自配置：

- `subagent_memory_retention_policy`：默认 `parent_review_or_cleanup`，表示先由父级/用户复核后再决定保留或清理。这个字段不做封闭枚举，后续清理器可以识别自定义策略名。
- `subagent_memory_delete_after_days`：默认 `0`，表示不按天数自动删除。
- `subagent_destroy_summary_required`：默认 `true`，表示子代理销毁或归档前应留下最终总结事实，方便父级接手、提炼 skill 或人工复盘。

当前这些是账本和恢复边界，不是新硬门。真正是否提升到主记忆、是否删除、是否沉淀 skill，后续由显式复核/清理流程处理。

## Runner 结果

runner 结构化结果只负责把状态、summary、artifacts、evidence packets、findings、能力申请和 next actions 合并回任务账本。它不再额外执行“声明的本地产物路径必须存在，否则改写为 BLOCKED”的专用完整性门。

父级看到子代理结果后，可以按 refs 读取产物、继续调度、要求返工或提交 closeout。系统不再在 runner 层单独制造 `artifact_integrity_repair_advice`，避免和统一 closeout 形成两套验收/返工机制。

## 旧兼容层清理

已经确认无生产调用的 task/real-task 读取兼容文件、repair 目标辅助文件、
capability contract 文本片段辅助文件已删除。当前结构保持一套事实源：

- 创建和身份：`create_subagents` / `schedule_child_subagents` 写真实 run 账本。
- 观察和汇总：`inspect_agent_tree` 读树、refs 和 registry。
- 质量和返工：统一走普通 closeout，不再另造父验收、repair contract 或自然语言继承合同。

以后如果确实需要恢复某类旧兼容能力，应先把它接入这三条事实源，而不是重新增加一套平行 helper。

## Guidance 账本

运行中补充提示统一落在 conversation guidance 账本：

- `send_guidance` 是新入口，目标可以是 `agent_run`、`thread`、`task` 或 `case`。
- 主代理工具循环会读取自己 run/task/thread 的未投递 guidance。
- 子代理执行上下文会把点名给当前 run 的 guidance 放进 `context_bundle.reserved.runtime_guidance`，
  runner prompt 再渲染成醒目的 `GUIDANCE_DELIVERED` 块，包含 guidance id、目标、
  优先级、发送者和正文，方便 tail 日志时确认哪一轮吃到了提示。
- 旧 `subagent_message` 工具已移除；纯补充提示统一用 `send_guidance`。
  `dispatch_subagents.runner_instruction` 只表示“补一句并立刻推进该 run”，会同时写入 guidance 账本。

这套结构只负责“下一轮让模型看见补充提示”。真正推进仍靠 `create_subagents`、
`schedule_child_subagents`、`dispatch_subagents` 和任务树状态；真正验收仍靠普通 closeout。
