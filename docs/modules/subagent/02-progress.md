# Subagent Progress

## 2026-06-10 合约身份 helper 去重

- 幂等/修复合约身份两个文件合一，消除 3 个逐字重复的私有 helper；纯内部去重，
  公开入口函数名和行为不变，focused 合约测试全绿。

## 2026-06-10 服务层 facade 清理与链路拉直

- 删除 `services/__init__.py` 的 11 个 service re-export；manager 与调用方全部直连实现模块，导入链不再经过包枢纽。
- `services/capabilities|runner_context|runner_result/` 三个单模块包打平为同级 `*_service.py` 文件，相对导入深度同步减一。
- `parsing/__init__.py` 尾部对 `envelope` 的反向 re-export 删除，`services/hierarchy/__init__.py` 清空转发；两处模块级循环导入消除（AST 级检测确认全包无真循环）。
- 行为不变：仅导入路径调整，focused tests（manager/board/hierarchy/parsing/capability requests）全绿。

## 2026-06-09 状态投影降噪

- `status --json` 的 `subagents.hot` 只显示当前可行动的近期风险项；超过 72 小时没有更新或进展的
  风险项计入 `historical_hot_count`，不再占满默认状态输出。
- 历史风险项不被删除，仍可通过 subagent board、`subagents --all` 和审计文件查看。这个变化只影响
  人和 gateway/chat 默认状态投影，不改变子代理调度、验收或历史记录。
- `SubAgentBoardItem` 现在携带结构化 `created_at`，启动恢复检测不再因为缺字段把 active work
  误报为 0；如果检测失败，`status --json.active_work.detection_errors` 会显式暴露结构化错误。
- `status` 展示层会截断长 goal，并拆出 `current_summary` / `history_summary`；
  原始 goal 和完整历史仍在 task/board/detail 事实源里，状态页不再承担大报告或历史审计职责。
- `subagents` 默认命令同样只展示当前 hot 或最近项；历史 hot 只计入 `historical_hot`。
  需要看完整历史时显式使用 `--all` 或 status/owner/root 过滤。

## 2026-06-07 真实 all-agent 子代理对照

- 真实运行目录：
  `validation/real_runs/20260607-000144-subagents-all-agent`。
- Prompt 使用普通中文：主代理找几个帮手分头阅读 `/Users/example/study-agent/all-agent`
  下的项目，最后由主代理核对、合并报告并提交验收。
- 运行完成并 `submit_for_acceptance` 通过：`tool_rounds=51`、
  `ctx_tokens≈137464`，任务内触发真实 compact 1 次，compact 后继续读取子代理产物并完成提交。
- 子代理链路可跑通：task workspace 下产生 14 个子代理目录，最终均为 `DONE`；
  `output/` 下生成综合报告和 12 份项目分报告。
- 暴露的真实问题：
  - 主代理会高频重复 `inspect_agent_tree` / `list_files output`，虽然已有 `wait` 工具，
    但 `inspect_agent_tree` 的重复查看 cooldown 默认只有 30 秒，模型一轮往返常常刚好越过该窗口。
  - `read_file` 读目录时返回过泛的失败面，真实 run 中表现为 `UNKNOWN_ERROR`，不利于自动改用
    `list_files`。
  - 无结构化交付合同时，uncontracted closeout 只能验收“本轮写了报告”，不能证明用户列出的
    每个项目都被等质量覆盖；本 run 中 `letta-main` 路径不存在，最终 note 仍说“13 个项目全部完成”，
    但独立分报告实际是 12 份。
  - `my_agent_main.md` 只有 25 行，说明父代理汇总时会过度相信子报告存在，缺少覆盖质量结构化索引。
- 已修复：
  - `inspect_agent_tree` 重复查看 cooldown 默认改为读取
    `subagent_watch_interval_seconds`，仍允许显式 `cooldown_seconds: 0` 关闭；这是软提示和缓存摘要，
    不是硬门。
  - `inspect_agent_tree` 在 cooldown 内先比较 task-local `task.json` 的 mtime/size 指纹；
    树状态没变时直接返回上次的紧凑提示，不再完整读取和渲染整棵树。任一 task 状态文件变化时仍回到完整
    kernel snapshot，避免隐藏新进展。
  - `read_file` 遇到目录返回 `PATH_IS_DIRECTORY` 和建议的 `list_files` 调用，不再给泛化未知错误。
  - 默认配置 `workspace_root` 改回空值，保持“未配置时使用启动目录”的主链路语义。
  - `create_subagents` 的 `count > 1` 模式不再把同一个 `output_files` /
    `output_refs` 复制给所有 child。共享目标会记录到 `shared_requested_output_*`，
    每个 child 获得 task-local `work/child_outputs/...` 独立结果槽，避免真实 runner
    把多个子代理产物写成同一个文件。
  - 子代理 runner finalize 现在优先识别当前 run 的
    `[MAIN_AGENT_DELIVERY_COMPLETE]` 成功块：如果 runtime closeout 已经验收 task
    output 产物，就合成标准 `DONE` / `VERIFIED` 子代理结果，不再进入
    `SUBAGENT_RESULT` repair 轮把 task output 误判成源目录缺文件。

## 2026-06-09 子代理路径合同收敛

- 真实 live lab 暴露子代理执行上下文同时暴露旧 `.my_agent/subagents/<run_id>` locator
  和当前 task workspace，导致模型把旧目录当自己的 `task_dir`，artifact 聚合也会接受旧目录产物。
- 当前修复把模型可见 `task_dir`、write boundary 和 protocol write contract 收敛到
  `tasks/<date>/<task>/work/agents/<run_id>/`。旧 locator 只保留为 owner/index 查找面，
  不再作为模型写入根或 artifact 候选根。
- `output_files` / `output_refs` 中带括号占位符路径段的值，例如 `[任务目录]/report.md`
  或 `[workspace]/report.md`，会被视为非真实文件合同；不会进入 required refs、
  allowed write roots、declared output refs 或自动物化路径。
- 这不是按中文词判断，而是结构规则：括号占位符路径段不是当前 run 的真实文件路径。
  真正的输出目录仍必须来自结构化参数、当前 task workspace、授权写入根或用户明确给出的普通路径。

## 2026-06-06 主链路小跳转清理

- 删除只服务单一调用点的 facade/helper 文件，把能力请求解析、action rescue 渲染、runner
  guidance 注入、runner tool 过滤和 root task policy 折回当前权威模块。
- 真实主代理 all-agent 阅读任务暴露了工具协议示例污染：目录示例仍写
  `parameter_name` 占位字段，模型会照抄成错误工具参数。当前工具协议示例改为真实
  `read_file` 顶层参数；registry 统一拒绝未知顶层参数并返回 `TOOL_INVALID_ARGUMENTS`，
  不再让 `list_files` 这类有默认值的工具静默把错参当成功。内部字段通过
  `ToolSpec.internal_parameters` 隐藏声明，不展示给模型。
- persistence 保存链路继续收直：`identity`、`security`、`status_report`、`failure_handoff`、
  `inheritance`、`output_load_errors`、`recovery_outputs` 等只服务持久化保存的私有 helper
  已折回 `persistence/service.py`；`thought.md` 渲染折回 projection 写入点。
- session progress 写入链路继续收直：工具结果路径提取不再放单独 `paths.py`，而是跟
  `record_subagent_tool_progress` 保持在同一入口里，方便排查“工具写了什么、进度如何投影”。
- subagent markdown 渲染继续收直：dispatch/watch/parent planner 和 patch review/apply
  渲染不再通过 `rendering_dispatch.py`、`rendering_patch.py` 两个中转文件跳转，统一由
  `subagents/rendering.py` 持有。
- `runner_context` 现在直接构造执行上下文、写入边界、runtime guidance 和 runner allowed
  tools；角色模板相关判断留在 `role_templates`。
- `subagent_mixin.py` 现在直接持有 run/finalize、结构化修复、recovery snapshot 和 parent
  planner 记录链路；旧 `_subagent_repair_mixin.py`、`_subagent_planner_mixin.py`
  两个私有跳转层已删除，跨模块参数类统一放在 `agent_core/subagent/params.py`。
- 这轮清理不新增工具、不新增硬门，只减少跨文件跳转和旧入口。
- 父代理汇总子代理结果时，优先读取创建/树快照返回的 `child_output_read_order`、
  `primary_artifact_refs` 和 `expected_outputs`。没有声明产物路径的子代理会获得
  task-local `work/child_outputs/...` 默认产物路径；`count > 1` 批量复制出来的共享
  `output_files` / `output_refs` 也会被拆成这样的独立结果槽。`work/agents/<run_id>/`
  继续作为内部状态、审计和恢复目录；父代理查状态走 `inspect_agent_tree`，等待走
  `wait`，不把 shell sleep 或内部目录遍历当成正常控制面。

## 2026-06-06 状态精确化

- 子代理运行、恢复、tree、closeout 统一按当前协议状态判断；`COMPLETED`、`SUCCESS`、`ERROR`
  等旧标签只保留为原始审计文本，不再隐式兼容成 `DONE`、`FAILED` 或 `CHANNEL_ERROR`。
- 显式写入子代理状态时只能使用当前 `TaskStatus` 协议值；`completed`、`succeeded`
  这类旧成功别名会 fail closed，不会静默改写任务状态。
- dispatch workflow 候选和 runner 子结果摘要继续收敛到当前 `TaskStatus` /
  `DISPATCH_INELIGIBLE_STATUSES`；`CANCELLED`、`ABANDONED`、`TAKEN_OVER`
  不再被漏判成未完成子代理，`PAUSED` 仍按未完成保留给父代理处理。
- remembered run unfinished、parent-timeout recovery、compact continue packet 和 board risk
  也改为调用 `subagents.models` 的共享状态 helper；旧大小写/别名状态不会在这些链路里
  被各模块单独解释成完成、失败或可收口。
- agent tree 展示、due-check、leadership recovery、recovery orchestration、runner
  payload 和 QA repair payload 也不再维护本地失败状态集合；机器判断统一走
  `TaskStatus` / `SUBAGENT_FAILURE_STATUSES`，展示文案只消费已经归一的状态。
- 派发状态投影遇到旧标签或未知状态时，仍保留原始 status 供审计，但不会给父代理
  `summarize_or_report_verified_runs` 这类收口建议；必须先检查 agent tree 或人工处理。
- 恢复状态机不再把 `PLANNED`、`QUEUED`、`WAIT_CHILD` 旧别名提升成当前协议状态；旧状态进入
  `manual_review`，避免跨版本残留污染当前 run。
- 缺少结构化 `failure_type` 时，恢复快照不再从 `runner_last_error` 或自由文本错误里猜恢复码；
  工具错误文本分类只保留在工具结果诊断层，不能替代任务状态事实。
- 恢复 mode 只认当前显式枚举，例如 `rerun_from_continue_packet`、`rerun_from_checkpoint`、
  `takeover_from_continue_packet`、`takeover_from_checkpoint`；不再用 `rerun_*` / `takeover_*`
  前缀把未知旧值提升成自动重跑或接管。
- capability 等待状态只认当前协议 `PENDING_CAPABILITY_REQUEST`，不再把 `NEEDS_TOOL`、
  `WAITING_FOR_TOOL` 等旧/模糊状态别名自动升级成能力申请。
- capability request 自身只认当前状态：`OPEN` 是待处理，`GRANTED` 是已授权，
  `GAP` 是没有可用能力，`CLOSED` 是本轮已关闭。历史 `RESOLVED`、`APPROVED`、
  `REJECTED` 不能静默当成已处理终态；它们会继续作为需要人工/路由处理的状态暴露出来。
- 工具结果没有显式 `error_code` / `error_type` 时，机器错误码统一是 `UNKNOWN_ERROR`；
  日志里的错误正文可以给模型看，但不能反推出结构化错误码、任务状态或验收结论。
- 本地运行时失败的模型提示只读取结构化 `context` code，例如 `*.subagents.load`
- 子代理 task workspace 的身份只来自当前 `root_id` / `id` / `parent_id` 和明确的
  `run_workspace.task_root`；已有 `work/state.json` / `work/task.yaml` 不再反推本轮
  task_id，避免同名旧目录污染当前子代理树。
  或 `conversation.guidance.*`。普通错误文本或自由格式 context 里出现
  `subagent/load/guidance` 这类词，不会改变失败类型、任务状态或验收语义。
- `DONE` 仍是唯一已完成状态；`FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`BLOCKED`
  是可恢复/阻塞状态，恢复器和 strategy 只扫描这些结构化状态。
- offline subagent closeout contract 也只认当前 `TIMEOUT`；`TIMED_OUT` 这类旧别名
  只能作为异常/未知状态处理，不能触发 `CHILD_TIMEOUT` 语义。
- `blocked_reason` 只是解释字段：它可以写入 blockers、报告和父代理提示，但不能单独把
  `DONE`、`RUNNING` 或未知状态改成 `BLOCKED`，也不能把 `failure_type` 猜成
  `capability_request`。需要阻塞时必须写结构化 `status=BLOCKED`、`failure_type`
  或正式 `capability_requests`。
- `output.json`、checkpoint 和 QA payload 只读结构化字段、`ok` 布尔、blockers 和 refs；
  summary、角色描述、旧状态词只作为展示或软上下文。
- 子代理结果产物只从当前结构化 schema 进入 artifact refs：`artifacts`、
  `artifact_refs`、`evidence kind=artifact` 和 `evidence_packets[].artifact_refs`。
  `deliverables`、`files`、`output_files`、`files_modified`、顶层 `path/file_path`
  等旧结果别名不再被悄悄恢复成产物；派任务时的 `output_files` 仍是创建子代理的目标路径字段。
- `create_subagents` 不再用 `working_buttons`、`verified_images`、`no_comments`
  这类专项交付约束做入口硬拦。它们可以作为结构化任务上下文传给子代理，最终由父代理验收、
  QA 或 closeout 证据判断，不在派工前阻断主链路。
- test failure classification 不再从 stdout/stderr 文本里的 `SyntaxError`、`AssertionError`
  或超时词猜恢复类别。机器分类只读 `executed`、`passed`、`validation_method`、
  `validation_result.reason` 等结构化字段；输出尾部只保留为人类审计摘要。
- 全局错误 taxonomy 不再用多语言正则从普通错误正文猜 `TOOL_TIMEOUT`、`WRITE_FORBIDDEN`
  等机器码。工具/后端/runner 必须产出显式 `error_code` 或 `failure_type`；没有结构化码时就是
  `UNKNOWN_ERROR`。
- 测试准备不再把 `command: static_site_check` 解释成验证器选择。验证器入口只认
  `validation_method`；`command` 是实际执行命令，不承担 schema 选择。
- 文件存在性验证只认 `validation_method: file_check`。旧的 `file_exists`、
  `path_exists`、`artifact_exists` 不再作为可执行验证方法别名。
- 协作能力匹配只读取显式 capability 字段和真实工具名；不再从工具名里的
  `read/search/write/dispatch` 等字样自动生成 `query/write/delegate` 这类抽象能力。
- 工具动作布尔参数只认 JSON 布尔、数字和 `true/false/1/0`；`yes/on/apply/run/full`
  这类普通词不能改变执行行为。

## 2026-06-04 收敛

- 删除旧 manager mixin 和过渡转发文件，`SubAgentManager` 现在直接拥有初始化、基础生命周期和工单路径。
- `SubAgentBoardService`、`SubAgentPatchService`、`SubAgentHierarchyService` 直接作为当前服务入口，不再保留单独转发文件。
- CLI 子代理命令注册合并到 `cli/subagents.py`，不再保留单独的 registration / hierarchy 注册 facade。
- 子代理状态继续以 task-local canonical state 为权威；owner projection 和 global index 只做查找。
- `cancel_subagents` 是父代理处理卡住下级的控制面：可取消、废弃 attempt、记录审计，再由父代理接管或汇总；如果 canonical loader 读不到该 run，会返回结构化 load error，不用旧路径扫描假装取消成功。
- `inspect_agent_tree` 重复查看只给紧凑提示和直接摘要；需要等待时用 `wait` 登记下次查看间隔，不把轮询做成硬门。
- `create_subagents` 不再因为已经有活跃子代理就默认拒绝第二批；父代理可以先派一批，后面按需要继续派。
- 子代理可以写 task workspace 里的协作产物；最终交付由主代理汇总到 `output/` 或用户指定目录。

## 运行约定

- 子代理没有长期个人记忆，只保留 task-local 状态、事件、artifact refs、compact 和候选经验。
- 子代理模板可以定义简短 persona、description、skills 和机器能力字段；当前 runner 仍走现有执行链路。
- `role` 是模板选择字段，`agent_name` 只是展示名。创建任务时会把模板能力快照写入
  `attributes.role_template`，后续调度、恢复、timeout 和 runner prompt 只读这个快照或模板字段，
  不从显示名或普通中文/英文描述里猜角色。
- 默认 `agent_name` 也只作为展示标签，格式为 `agent-d<depth>-<role>-<index>`；多层级调度只用
  `depth` / `parent_id` / `root_id` 等结构化字段，不再从默认名或用户叫法里解析层级。
- 层级继承标记只写 `attributes.inherited_parent_context=true`；给模型阅读的 `goal`
  不再塞 `inherited_parent_context=true` 这类内部机器标记。
- capability request/grant/gap 是可观察工作项，不是默认阻断任务的硬门。
- workflow mode 是显式配置能力，不应该替普通中文任务自动加限制。
