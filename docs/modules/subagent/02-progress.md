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
