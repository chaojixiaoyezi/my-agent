# Orchestration Tool Reference

这份文档记录编排工具的长说明。模型 prompt 里只注入短摘要；需要改工具语义时，先改代码里的短摘要，再同步这里的详细解释。

## create_subagents

用途：让当前代理创建一个或多个直接下级。主代理、子代理和孙代理都使用这一个入口；创建后立即由宿主启动。

工具面按结构化角色决定：根主代理和 `can_spawn_children=true` 的 coordinator 才有
create/guidance/cancel/resolve 四个直属下级控制入口；普通 leaf 不带这些入口，只执行自己的任务并可用
`capability_request` 请求自身所需权限。需要下一层协作时，应把该 child 明确创建为 coordinator。

关键原则：

- 顶层 `goal` 始终是原生工具 schema 的必填字段。创建一个子代理时它就是该子代理的目标；使用
  `items` 批量创建不同子代理时，它写整批派工目的，每个 item 仍必须有自己的独立 `goal`。
  这里的 `goal` 只是内部派工说明，与用户命令 `/goal` 无关；普通聊天中的实际任务同样可以自主派工。
  空参数调用会作为无效原生工具调用处理，不能静默退回主代理独自完成。
- 模型工具不提供 `count` 克隆模式。单个具体工作直接传 `goal`；多个可并行工作用
  `items` 逐项声明不同目标和交付边界。重复 item 会整批拒绝，不会部分创建。
- `covers` 是可选 exact-id 映射。只有 child 与一个仍 open Todo 确实是同一工作时才提供；省略时 child
  以真实 run id 形成独立进度行，不关闭现有 Todo。提供的未知、已关闭或同批重复 id 会在创建任何 run
  前整批拒绝，宿主不从 goal 或标题猜绑定。返工已关闭项先用 task_progress 对原 id 传
  `status=in_progress, correction=true` 重开，或省略 covers；不得拿无关 open id 顶替。
- `output_files/output_refs` 是可选交付元数据，不是文件所有权。同批、同级或父子路径
  重叠不再拒绝创建，也不会自动加入持久 workspace 锁或其它代理的 `locked_files`。
  这不改变路径授权：显式输出仍必须在父级 workspace 上界内。
- `allowed_tools` 只是工具偏好提示，不是安全边界；基础读写工具由系统按角色和目标补齐。
- 子代理自己的资料线索写到对应 item 的 `input_refs`，公共资料才放顶层。
- 普通 child 自动继承直接父级的结构化工作区上界。用户明确了产物路径时可写 `output_files`；批量派工
  可由每个负责写入的 item 分别声明。它负责交付身份、读取顺序和冲突提示，不是完整写集、创建前置条件
  或普通 child 的权限来源；goal 或 output_files 都不能把写权扩大到父级 workspace 外。没有明确路径时
  不要强造。
- root 的执行纪律覆盖用户完整目标；child 的直接父级 `goal` 是该 child 的完整工作边界。child 应完整
  完成这份具体任务，但不能因根目标更大而实现未分给自己的兄弟项；这是模型软纪律，不是 goal 文本硬门。
- 裸相对路径按当前可信 cwd/workspace 解析，例如从 `/root` 启动时 `abc/index.html` 就是
  `/root/abc/index.html`。只有显式 `output/report.md`、`work/notes.md` 才指向 task 内部 staging；绝对路径
  原样交给写边界裁决，不能改写成 task output 后返回成功。
- 子代理没有声明产物路径时不生成假业务文件；父代理从直属 lifecycle event 的 typed status、最终回复、
  `final_report_ref` 和真实 artifact refs 接收结果。显式声明产物时，创建/树快照才会在
  `child_output_read_order` / `primary_artifact_refs` / `expected_outputs` 暴露对应引用；同时可参考
  `primary_artifact_stats` 的大小、行数和字符数决定是否补读或重派。不要直接翻
  `work/agents/<run_id>/` 里的内部状态文件。历史 `system_default_output_ref` 只作恢复迁移，不进入这些
  模型可见结果字段。
- 替换旧子代理时使用 `replacement_for_run_ids`，让系统记录结构化接管关系。
- 模型侧角色索引只展示角色 id、中文说明和能力标签；模板文件路径只留给调试接口，不进入 prompt。
- `role` 选择角色模板；`agent_name` 只用于人类显示和点名，不参与机器角色判断。
- 运行时判断使用模板字段，例如 `can_spawn_children`、`depends_on_outputs`、`can_run_tests`；
  不按 `tester/coordinator/验收/汇总` 这类普通词或显示名做硬判断。

该必填形态参考 会话运行时 v2 `会话运行时-rs/core/src/tools/handlers/multi_agents_spec.rs`：`task_name` 与 `message`
都由 JSON Schema 标为 required；也核对了 通道运行时 `src/agents/tools/sessions-spawn-tool.ts`，其
`sessions_spawn.task` 同样是必填正文。两者都不要求用户先进入某个 goal 模式。my-agent 保留
`items` 式的明确批量派工，不保留复制同一份可写任务的模型入口，也不保留
“goal 或 items 二选一、因此两者都可空”的漏洞。

## 宿主内部代理树投影（不是模型工具）

用途：给 `/status`、TUI、恢复器和运维诊断只读投影主代理、子代理、孙代理状态树。

旧 `InspectAgentTreeTool`、模型 Schema 和公开导出已删除。内部代码直接调用
`agent_tree_status_payload`；它不会创建、推动或验收任务，也不能重新包装成模型工具。正常运行的 child
自主继续，并用 lifecycle event 把进展、阻塞和结束通知直接父级。

子代理状态、进度、channel 状态和内部 refs 以这个内部 projection 为运维状态面。普通文件工具和
shell 不应该读取或遍历 `work/agents/<run_id>/canonical_state.json`、`final_report.md`、
`summary.md`、`checkpoint.json`、`memory_archive/` 等内部文件；这些文件是审计/恢复资料，不是父代理的正常汇总入口。
如果父代理误读这些内部路径，文件工具会返回 `WRONG_STATUS_SURFACE` / `internal_agent_status_ref`
类结果，并附带 `child_result_index_row`。父代理应改读其中的 `read_order`、
`primary_artifact_refs` 或声明产物，不要继续猜内部目录。
运行中或规划中的子代理不会把内部 `final_report.md` 放入父代理的 `read_order`；只有完成后
缺少更好的结构化产物时，它才会作为兜底审计 refs 出现在读取顺序里。
模型没有查询入口，也不要用 shell 的 `sleep` 或另建巡检代理。child 的生命周期事件会直接唤醒父级；
用户需要了解情况时由 `/status` 读取同一内部 projection。

## inspect_collaboration

用途：只读查看协作 case，或列出当前/指定代理的待处理协作请求。

它不是子代理运行状态面。没有待处理协作请求时返回正常空结果，不把“没有请求”当工具失败；
它不能代替子代理状态面；如果目标实际是 child，模型应使用创建回执/生命周期事件中的 refs，或等待宿主
继续通知，不能跳转到已经删除的巡检工具。

## 内部自动启动（不是模型工具）

代码里的 dispatcher、scheduler 和 worker pool 仍负责进程选择、并发额度、重启恢复和完成通知，但它们不是
模型可见工具。模型不能也不需要“再推一下”已创建的 child；创建成功就表示已经进入自动启动链。系统也
不为每批 child 周期性调用 LLM 巡场，真实状态变化会直接通知父级。

child 启动上下文中的父级 read/search 预览只取当前 tool loop archive，不使用跨任务进程缓存；正式的
跨轮信息继续由 transcript、Compact 和 typed refs 承担。

child 生命周期事件到达 root 后，后台模型轮必须继续同一个 active task：原 task link goal 仍是用户任务，
完成通知只是 runtime continuation。宿主会从该 root 自己的 canonical tool index 恢复此前派工和进度调用，
以便 one-shot 去重、执行守卫和模型上下文都知道已经做过什么；不会扫描 child 私有目录，也不会把 Audit
运营通知并入任务。这个内部恢复不是新的模型工具，模型仍没有 inspect/wait/dispatch 入口。

## send_guidance

用途：给当前代理直接创建的一个 child 插入一段普通自然语言要求。

参数只有 `target` 与 `message`。它不支持 thread/task/case、多个 run、children/descendants 广播、priority
或 delivery 控制，也不会启动、推进或验收目标。孙代理由它的直接父代理管理；要停止 child 使用
`cancel_subagents`。用户对主代理的插入走 active-turn 输入链，不通过这个模型工具。

所有层级继续拆分时仍调用 `create_subagents`。运行中要补充上下文用 `send_guidance`，要停止用
`cancel_subagents`；状态变化由宿主事件送达。已经结束而目标仍有缺口时，创建一个分工明确的新 child，
并通过 `replacement_for_run_ids` 保留接管关系。

## cancel_subagents

用途：当前代理按精确 `run_id/run_ids` 打断或取消自己直接创建的 child。

模型 Schema 不提供 `root_id`、`status` 或整棵子树操作；child 的 child 由 child 自己管理。宿主恢复/运维
仍可调用内部批量 primitive，但不得把它重新暴露给模型。

## resolve_capability_requests

用途：直属父级批准或拒绝直属 child 的结构化 capability request。

OPEN 请求优先于普通 completed 收尾，child 保持 `BLOCKED`。grant/deny 完成后，宿主把同一个 run 自动
排回 `PENDING` 并通过裁决 lifecycle event 触发续跑；工具不是“推动”按钮，也不会创建第二个 child。

## 宿主事件回传（不是模型工具）

旧 `raise_event` 已删除。普通 child 不需要调用工具证明自己仍在工作；runner 的模型/工具阶段、能力申请、
阻塞和终态由宿主直接写 observation/wake 与 canonical state，再通知直接父级。Audit/长期监控也调用同一
内部事件服务，不重新注册一个模型自报入口。

## task_progress

用途：记录或读取当前任务的软进度账本，帮助长任务和 compact 后续接。

关键原则：

- 它只是软账本，不代表验收通过。
- `action` 只接受 `read` / `update`。`create`、`init`、`begin`、`write` 等旧别名不再自动兜底成写入。
- `items` 和 `coverage` 都是开放结构，不限定项目、论文、API、日志、文件等对象类型。
- 把条目标成完成时，建议带文件、来源、产物路径或工具结果引用；缺证据时只提醒，不阻断。
