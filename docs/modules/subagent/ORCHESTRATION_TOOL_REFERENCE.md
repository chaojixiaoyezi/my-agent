# Orchestration Tool Reference

这份文档记录编排工具的长说明。模型 prompt 里只注入短摘要；需要改工具语义时，先改代码里的短摘要，再同步这里的详细解释。

## create_subagents

用途：让主代理创建一个或多个子代理。默认创建后立即启动，只有显式 `defer_start: true` 才只建不跑。

关键原则：

- 顶层 `goal` 始终是原生工具 schema 的必填字段。创建一个子代理时它就是该子代理的目标；使用
  `items` 批量创建不同子代理时，它写整批派工目的，每个 item 仍必须有自己的独立 `goal`。
  空参数调用会作为无效原生工具调用处理，不能静默退回主代理独自完成。
- 不同工作切片优先用 `items`，不要用 `count` 复制同一个空泛目标。
- `allowed_tools` 只是工具偏好提示，不是安全边界；基础读写工具由系统按角色和目标补齐。
- 子代理自己的资料线索写到对应 item 的 `input_refs`，公共资料才放顶层。
- 用户明确了产物路径时写 `output_files`；没有明确路径时不要强造。
- 子代理没有声明产物路径时，运行时会给它分配 task-local `work/child_outputs/...`
  默认产物路径，并在返回值里暴露 `child_output_read_order`。父代理汇总时优先读
  `child_output_read_order` / `primary_artifact_refs` / `expected_outputs`，同时可参考
  `primary_artifact_stats` 里的大小、行数和字符数判断是否需要补读或重派；不要直接翻
  `work/agents/<run_id>/` 里的内部状态文件。
- 替换旧子代理时使用 `replacement_for_run_ids`，让系统记录结构化接管关系。
- 模型侧角色索引只展示角色 id、中文说明和能力标签；模板文件路径只留给调试接口，不进入 prompt。
- `role` 选择角色模板；`agent_name` 只用于人类显示和点名，不参与机器角色判断。
- 运行时判断使用模板字段，例如 `can_spawn_children`、`depends_on_outputs`、`can_run_tests`；
  不按 `tester/coordinator/验收/汇总` 这类普通词或显示名做硬判断。

该必填形态参考 会话运行时 v2 `spawn_agent`：任务身份与任务正文都由 JSON Schema 标为 required，而不是
只写在提示说明中。my-agent 保留现有批量派工能力，但不保留“goal 或 items 二选一、因此两者都可空”的漏洞。

## inspect_agent_tree

用途：只读查看主代理、子代理、孙代理状态树。

它不会创建、调度、恢复或验收任务。要推进已有子代理时用 `dispatch_subagents`；只是给运行中的代理补一句话时用 `send_guidance`。

子代理状态、进度、channel 状态和内部 refs 都以这个工具为模型可见状态面。普通文件工具和
shell 不应该读取或遍历 `work/agents/<run_id>/canonical_state.json`、`final_report.md`、
`summary.md`、`compactions/` 等内部文件；这些文件是审计/恢复资料，不是父代理的正常汇总入口。
如果父代理误读这些内部路径，文件工具会返回 `WRONG_STATUS_SURFACE` / `internal_agent_status_ref`
类结果，并附带 `child_result_index_row`。父代理应改读其中的 `read_order`、
`primary_artifact_refs` 或声明产物，不要继续猜内部目录。
运行中或规划中的子代理不会把内部 `final_report.md` 放入父代理的 `read_order`；只有完成后
缺少更好的结构化产物时，它才会作为兜底审计 refs 出现在读取顺序里。
如果只是等一会再看进度，用 `wait`，不要用 shell 的 `sleep`。

## inspect_collaboration

用途：只读查看协作 case，或列出当前/指定代理的待处理协作请求。

它不是子代理运行状态面。没有待处理协作请求时返回正常空结果，不把“没有请求”当工具失败；
如果参数里有结构化 `run_id` / `task_id` / `root_id`，或指定了真实存在的子代理 run id，
返回值会附带 `suggested_tool_call: inspect_agent_tree`，让模型直接切到代理树状态面。

## dispatch_subagents

用途：推进、恢复或重跑已有子代理。

关键原则：

- 查看状态只用 `inspect_agent_tree`，不要为了看一眼触发调度。
- 后台自动 dispatch 必须使用创建这些 run_id 的同一个 `workspace_root`；cwd 只服务
  Python 模块加载，不能决定子代理树归属。
- `dry_run` 是唯一预览开关，`true` 只预览，`false` 真实推进。
- `run_ids` 用于精确指定要推进的子代理。
- `max_runners` 控制本轮最多推进几个，不知道时省略。

## schedule_child_subagents

用途：当前子代理在自己的名下继续创建下一层子/孙代理，保持任务树层级可恢复。

关键原则：

- 顶层主代理第一次派工继续用 `create_subagents`。
- 子代理创建自己的下级时用这个工具。
- 平级补充提示用 `send_guidance`，推进已有下级用 `dispatch_subagents`。

## raise_event

用途：记录普通进展、阻塞、观察或需要主代理处理的事件。

关键原则：

- `urgency: urgent` 会写 wake queue，让主代理尽快处理。
- 非紧急事件只进账本，等后台 tick 或父级查看。
- `event_type`、`severity` 都是开放字符串，不做业务专项枚举硬拒。

## task_progress

用途：记录或读取当前任务的软进度账本，帮助长任务和 compact 后续接。

关键原则：

- 它只是软账本，不代表验收通过。
- `action` 只接受 `read` / `update`。`create`、`init`、`begin`、`write` 等旧别名不再自动兜底成写入。
- `items` 和 `coverage` 都是开放结构，不限定项目、论文、API、日志、文件等对象类型。
- 把条目标成完成时，建议带文件、来源、产物路径或工具结果引用；缺证据时只提醒，不阻断。
