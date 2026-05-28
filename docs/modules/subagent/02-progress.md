# Subagent Progress

父验收旧链路已经移除。子代理只负责执行自己的任务、写结果和证据引用；上级通过任务树、状态、refs 和普通 closeout 继续推进。

## 2026-05-27 子代理权限和记忆配置

- 子代理新增有效权限快照 `effective_permissions`：父级 `restricted` 会继续下传 `restricted`，父级 `workspace-write/full-access` 下的子代理最多拿 `workspace-write`，不会自动继承全盘 shell 权限。
- `run_command` 会从执行上下文的 `write_boundary.shell_access_mode` 读取系统下发权限；模型参数不能给自己提权。
- 默认子代理工具集补齐基础读、搜、artifact、网络、写文件、打补丁、受控命令、只读树状态、协作和能力申请工具，避免“少填 tool_preset 就创建残废代理”。
- `inspect_agent_tree` 会按当前身份裁剪：主代理可看全树；子代理/孙代理只看自己和后代，不能用 root 参数读平行子树。
- runner 内部的 `dispatch_subagents` 会固定在当前 run 的直接孩子范围内；模型显式传入其他 `parent_run_id` 也不会跳到平行子树。
- runner 内的消息、能力申请和协作证据入口同样按当前身份裁剪。模型如果传错 `sender_run_id`、`source_agent_id`、`requester_agent_id`、`actor_agent_id`，系统不会替别的 run 写账；工具结果会把被忽略字段放进 `scope_resolution.ignored_explicit`。
- `schedule_child_subagents` 现在和 `create_subagents` 一样默认创建后后台启动；启动参数会保留当前 runner 作为 parent scope，不会把孙代理挂到顶层或平行子树。
- 子代理任务级记忆命名空间改为 `subagent:{root_run_id}:{run_id}`，并把保留策略写入 `attributes.memory_scope`。新增配置 `subagent_memory_retention_policy`、`subagent_memory_delete_after_days`、`subagent_destroy_summary_required`；这些只描述归档/清理边界，不是新硬门。
- 为保持 code-size 清零，把后台启动逻辑拆到 `orchestration_background_dispatch.py`，把 kernel 数据模型拆到 `kernel_models.py`；行为保持 refs/tree/status 路线不变。

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

## 2026-05-28 模型可见路径收敛

- `inspect_agent_tree` 和 `subagent_board` 的模型可见输出只暴露当前 `task_workspace`、`agent_run_workspace`、artifact refs、evidence refs 和 recovery refs。
- 旧式 work-order 目录仍留在兼容恢复文件中，但不再作为状态面主路径返回，避免父代理接管时按 `data/subagents/<run_id>/...` 猜旧路径。
- 父级读取子代理结果时，应优先用 `agent_run_workspace`、`final_report_ref`、`artifact_refs` 和 `evidence_refs`，不要自己拼子代理目录。
- 如果模型仍然拿旧路径或抄错路径去读，`read_file/list_files/search_text`
  会返回 `path_not_found=true`、`candidate_paths` 和下一步建议；这只是恢复提示，
  不会自动读取候选、不会申请新权限，也不会把任务改成失败。

## 2026-05-28 Artifact Registry 收敛

- 新增统一 artifact registry，产物登记后得到 `artifact_id`。同一产物移动、
  重建或修复时更新同一个 `artifact_id` 的最新记录。
- `write_file` 等工具结果会暴露机器路径，工具归档会把真实存在的输出文件登记进
  registry；子代理结构化结果里的 `artifacts/file_path/output_path` 也会登记。
- `dispatch_subagents`、`subagent_board`、`inspect_agent_tree` 和 closeout
  都优先返回 registry 的 `artifact_id/path/registry_ref`。旧路径字段还保留，
  但只作为兼容投影。
- 这不是新硬门。registry 只解决“谁是最新产物事实”的问题；缺产物、坏格式和内容质量
  仍由普通 closeout 或父级模型根据任务目标处理。

## 2026-05-28 看板秒数和汇总准备度

- `subagent_board` 行里新增 `running_seconds` 和 `seconds_since_progress`。它们只是观察字段：
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
