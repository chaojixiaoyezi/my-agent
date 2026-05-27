# 设计思路台账

> 2026-05-27 当前路线备注：早期条目里提到的 `orchestration_contract`、`materialize_subagent_inputs`、`subagent_dispatch_closeout`、`parent_acceptance` 专项收口、`scheduling_warnings`、领域/重复目标调度提示等，都是历史试错记录。当前生产路线是：调度工具只返回 refs/tree/status，普通协作不靠中间验收门卡住；最终质量统一回到 closeout 和任务树事实。

## 2026-05-27 / 配置单一来源与 create 默认开跑

状态：本地已落地，focused tests 已跑；未提交

摘要：
- 运行门默认值的读取下沉到 `agent_py_agent/agent/settings/runtime_guard_config.py`，避免 `tooling` 反向导入 `agent_core` 造成循环依赖，也避免同一数字散落在多个 reader 里。
- `max_tool_rounds`、单代理工具预算、runner 失败补跑、同 run 重派、工具重复失败和工具限流都从 `runtime_guard_config.yaml` 读取默认值。`AgentConfig` 不再携带 `max_tool_rounds` / `tool_agent_budget_*` 的隐藏默认；旧配置仍作为兼容字段可被显式传入。
- 单代理工具预算默认从 600 秒 / 50 次改为 600 秒 / 200 次，并且默认值只保存在 `runtime_guard_config.yaml`。以后改 YAML 会直接影响运行门，不需要再同步改 dataclass 默认值或测试里的断言数字。
- `create_subagents` 默认创建后立即真实启动新 run；只有显式 `defer_start=true` 才只建记录不跑。返回 payload 会写 `auto_start`，让主代理知道这批 run 是已启动、已延迟还是启动失败。
- `dispatch_subagents` 保留为运行中的提示注入/催办/恢复工具，新增 `prompt`、`message`、`guidance` 别名，统一归一成 `runner_instruction`。它仍可人工推进卡住项、重跑指定 run、查状态并尝试恢复。

验证：
- `python3 -m pytest -q agent_py_agent/tests/test_runtime_guard_config_shared.py agent_py_agent/tests/test_tool_agent_budget.py agent_py_agent/tests/test_main_agent_auto_resume.py agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_orchestration_dispatch_subagents_tool.py --tb=short`
- `python3 -m pytest -q agent_py_agent/tests/test_settings_config.py agent_py_agent/tests/test_config_normalize.py agent_py_agent/tests/test_runtime_guard_config_shared.py --tb=short`

## 2026-05-26 / 删除协作流程里的走钢丝硬门

状态：本地已落地，focused tests 已跑一轮；不提交

摘要：
- 本轮按“先跑通协作流程，不在流程里塞卡点”的规则，删除了旧的 1-5 类硬门：派工前正文读取门、委托期正文读取门、直接写入门、输入依赖启动门、输入物化工具、sibling workflow 自动依赖、共享输出/count 复制具体文件目标硬拒绝、coordinator/child 状态收尾改写，以及 delivery repair / bootstrap materialization 这类和 closeout 重叠的返工门。
- 删除的生产模块包括：`orchestration_predelegation_read_guard.py`、`orchestration_body_read_guard.py`、`orchestration_direct_write_guard.py`、`tool_body_read_guard_stage.py`、`tool_direct_write_guard_stage.py`、`subagent_input_materialization.py`、`runner_input_dependencies.py`、`runner_workflow_dependencies.py`、`orchestration_item_dependencies.py`、`tool_bootstrap_materialization_guard.py`、`tool_delivery_repair_*.py`。
- `runner_input_dependencies.py` 被替换为 `runner_ref_fields.py`：只保留结构化输入/输出 ref 解析能力，不再判断“输入依赖是否就绪”、不再过滤 runner 候选、不再生成缺输入阻断。`required_read_paths` 继续是读提示/读授权信息，不是 runner 启动前必须满足的验收项。
- `create_subagents(items=...)` 不再根据 sibling `input_refs/output_refs/dependencies` 自动生成 `workflow_depends_on`。如果用户真要流水线，父代理自己按“先派 A、看 A 完成、再派 B”的方式显式调度；普通并行协作不被隐藏依赖拖住。
- `create_subagents(count=...)` 不再因为目标里有具体 `output_files/output_refs/artifact_refs` 就拒绝。共享输出风险交给 prompt 分工、tree/board 状态和最终 closeout 暴露；不在创建阶段提前卡住普通任务。
- `subagent_finalize_helpers.py` 不再用 coordinator/child 状态启发式把 runner 改成 `needs_child_creation`、等待父级验收或其他状态。子代理写产物、上报 tree/board/case，父代理看事实决定下一步；统一质量验收仍回到 closeout。
- 旧协作 closeout/acceptance 的专项卡点也被降级或删除。测试里如果只是为了证明旧硬门会阻断，删除；如果日志有意义，保留为普通测试断言或文档记录，而不是继续让运行时照着旧门卡住。
- 继续清理后，又删除了 `workflow_depends_on` 运行时依赖和 runner 角色阶段候选门：dispatch 不再因为 tester/reviewer/acceptor 角色或 sibling phase 关系只放行一部分 runner。真要流水线，由父代理显式“先派 A，等 A 结果，再派 B”；普通协作/临时响应不被隐藏阶段卡住。
- runner prompt 里的旧 `input_contract.resolved_read_paths` 改成 `read_refs`：它只是可读线索和授权范围，不是启动前置条件。子代理读不到某条路径时，应该记录限制、换线索、上报父级，而不是在 runner 启动前被系统判死。
- `create_subagents(items=...)` 不再静默删除 item 自己显式传入的 sibling output read refs。既然 `required_read_paths` 只是读线索，不是启动硬门，就应把父级/模型给出的线索原样交给子代理；路径暂时不存在时由子代理运行后记录限制或上报，而不是 create 阶段偷偷改参数。
- `create_subagents(items=...)` 不再因为 `agent_name/role` 里出现 `grandchild`、`小小傻妞` 这类展示名就硬拒。真实层级只看 parent/root/depth 机器字段；名字写错最多是命名可读性问题，不应成为普通协作任务的启动卡点。
- `sibling_roster` 继续保留同批 peers 的 `run_id/name/role/goal`，但不再发布 peer 的未来 `output_refs/output_files`。这样子代理知道“可以找谁”，不会被未来同伴产物路径误导。
- `pending_requests_for_agent()` 只列 open case 里的待办。case 到 deadline 关闭后，未回复对象会进入“未回复事实”，不会在下次 tick 又把旧请求塞回 responder inbox。
- `open_case` 相关提示从“必须继续 request”改成“需要别人回应时建议 request；只记录事件可以停在 open case”。协作工具提示只做软引导，不再把账本步骤写成验收卡点。
- 参考项目复查结论：长期助手 的 `delegate_task`、终端交互 的 `AgentTool`、通道运行时 的 consult runtime 都以 prompt/task + 工具权限 + workspace/session/timeout 为主，没看到“启动前输入物化门”“sibling 输入输出依赖自动推断门”这类硬卡点；会话运行时 SDK 的 `Thread.run/runStreamed/resumeThread` 更强调 thread 续跑、结构化事件、sandbox/approval 配置，也没有把普通任务拆成隐藏的 worker/coordinator 阶段门。
- 顶层 dispatch 完成态恢复确定性 closeout：所有当前 scope 子代理已经 `DONE/VERIFIED` 时，工具轮后直接返回 refs-first 状态，不再额外请求一次模型。失败/阻塞时仍只返回中文子代理状态摘要，旧 `Subagent State Notice` 文案不再出现在生产代码。
- QA/tester/acceptor 早于实现产物的调度不再硬阻断，也不再通过 `scheduling_warnings` 给隐藏提示。调度层只创建/复用/返回待 dispatch 状态；父级是否先测、后测或补派，由模型根据 tree/refs 自己判断。

设计结论：
- 协作 case 的基本语义保持简单：打开、收集、到 deadline 关闭/汇总。没有回复的对象写成未回复事实，不让整个任务无限等。
- 子代理发现问题后，可以按 prompt 调用协作工具、直接问同级、上报父级或写产物；系统记录事实和状态，但不因为“没按某个内部模板动作”直接判任务失败。
- 真正需要硬的地方仍然是安全/权限/路径/工具执行边界和最终 closeout 验收；普通协作调度不再用中间模板门代替 LLM 判断。

验证：
- `python3 -m compileall -q agent_py_agent/agent agent_py_agent/tests`
- `python3 -m pytest -q agent_py_agent/tests/test_runner_ref_fields.py agent_py_agent/tests/test_tools/test_tool_loop.py::test_tool_loop_and_prompt_transcript agent_py_agent/tests/test_subagent_runtime_guards.py::test_final_response_guard_ignores_dry_run_dispatch_scope agent_py_agent/tests/test_agent/test_subagent_runner.py::test_subagent_runner_repairs_missing_structured_output --tb=short`
- `python3 -m pytest -q agent_py_agent/tests/test_orchestration_tools.py agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_runner_dispatch.py agent_py_agent/tests/test_orchestration_dispatch_child_refs.py --tb=short`
- `python3 -m pytest -q agent_py_agent/tests/test_agent/test_subagent_runner.py agent_py_agent/tests/test_subagent_runtime_guards.py --tb=short`
- `python3 -m pytest -q agent_py_agent/tests/test_subagent_phase_gates.py agent_py_agent/tests/test_runner_dispatch.py --tb=short`
- `python3 -m pytest -q agent_py_agent/tests/test_subagent_phase_gates.py agent_py_agent/tests/test_runner_dispatch.py agent_py_agent/tests/test_runner_prompts.py agent_py_agent/tests/test_manager_runner_context.py agent_py_agent/tests/test_dispatch_workflow_modes.py agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_create_subagents_items_policy.py agent_py_agent/tests/test_subagent_runtime_guards.py agent_py_agent/tests/test_tools/test_tool_loop_subagent_closeout.py --tb=short`
- `python3 -m pytest -q agent_py_agent/tests/test_collaboration_control_plane.py agent_py_agent/tests/test_local_collaboration_subagent_integration.py agent_py_agent/tests/test_background_main_agent_runtime.py agent_py_agent/tests/test_conversation_store.py agent_py_agent/tests/test_conversation_wake_events.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_create_subagents_items_policy.py agent_py_agent/tests/test_runner_prompts.py agent_py_agent/tests/test_manager_runner_context.py agent_py_agent/tests/test_dispatch_workflow_modes.py --tb=short`
- `python3 -m pytest -q agent_py_agent/tests/test_collaboration_control_plane.py::test_pending_requests_for_agent_ignores_closed_cases agent_py_agent/tests/test_orchestration_create_subagents_tool.py::TestCreateSubagentsToolWorkspaceDefaults::test_items_mode_sibling_roster_does_not_publish_future_outputs --tb=short`
- `ruff check` 目标文件通过；`git diff --check` 目标文件通过。

## 2026-05-26 / 无固定协调员协作控制面阶段 0-8

状态：历史记录，已被“删除协作流程里的走钢丝硬门”部分覆盖；其中提到的协作验收硬卡点、输入物化启动门和旧测试期望不再作为当前路线

摘要：
- 真实 5 子代理无固定协调员 smoke 暴露的根因不是 IP 任务专项，而是控制面有三处通用缺口：发现者可能只把“需要协作”写进产物却没有 case/request；批量子代理可能继承父级最终输出路径互相抢写；`dispatch_subagents` 全部 DONE 后曾经过早替主代理收口，切断第二轮协作判断。
- 阶段 0：`create_subagents` 批量模式不再把顶层 `output_files/output_refs/artifact_refs` 当作每个 child 默认输出。item 自己显式写的输出仍保留；若多个 child 仍共享同一文件型输出，返回 `coordination_warnings` 结构化提醒，不直接硬杀任务。
- 阶段 1：新增 `raise_collaboration_event` 通用工具，一步打开 case 并创建 request。它接收开放世界的 `observed_facts/query_hints/response_contract/context_refs`，不写 IP、日志、API、数据库等业务分支。
- 阶段 2-4：已有 `request_collaboration/list_collaboration_requests/reroute_collaboration_request/submit_evidence/update_collaboration_request` 继续承接显式目标、能力路由、响应发现、换路和证据提交。
- 阶段 5：临时查询 worker 不新增专项协议；上级或响应者用现有 `schedule_child_subagents` 派短生命周期 worker，并把 `case_id/request_id/context_refs` 作为结构化上下文传下去。
- 阶段 6：后台主代理和长期会话只通过 `case_status/inspect_agent_tree/wake signals/thread messages` 判断升级或汇报，不把 watch 只读巡逻变成默认 dispatch。
- 阶段 7：后续真实规模测试按 5 -> 10 -> 20 子代理逐级放大，小规模没稳定前不跑大规模。
- 阶段 8：文档明确记录几条走偏反例：固定协调员、事后猜产物、共享最终输出路径、all-green 自动收口、为测试样例写专项路径。它们保留为教训，不再作为默认路线。
- 真实 5 子代理 smoke 的第二轮暴露了后续通用缺口：父级验收能拒绝“产物声明待协作但没有 case/request/evidence ref”，但自动建议的 repair worker 只有读写文件工具，不能补开协作账本。已把父级验收 repair worker 的工具授权统一到 `repair_contract_allowed_tools()`，同时包含读写、`read_artifact` 和协作账本工具；repair contract 也明确要求协作未交接时必须留下结构化 ref，不能只写自然语言说明。
- 复验又暴露一个状态建议冲突：`parent_acceptance_repair_advice` 已经要求处理父级验收 REJECT，但 `current_turn_run_state` 仍把同一个 run 归为普通 awaiting acceptance，建议再次跑验收。已新增 `parent_acceptance_rejected_run_ids`，父级验收 REJECT 统一提示 `resolve_parent_acceptance_rejected_refs`，具体是 `raise_collaboration_event` 还是 repair worker 以 `parent_acceptance_repair_advice.suggested_tool_call` 为准，避免模型陷入重复验收或慢修复。
- 第三轮真实 smoke 暴露协作意图识别过窄：子代理写 `collaboration_events=[{status: pending, collaboration_requirement: ...}]` 时，旧验收器没有识别为待协作。已扩展为通用开放世界列表识别：字段名带 collaboration/coordination，列表项有 pending/open/needed 状态或需求/请求/问题/下一步字段且没有 case/request/evidence ref，就不能验收通过。
- 第四轮真实 smoke 暴露工具协议别名不一致：`create_subagents` 返回 `dispatch_run_ids`，模型照抄给 `dispatch_subagents`，但 dispatch 只识别 `run_ids/subagent_ids/...`，导致反复停在 PLANNING 或只推进局部 run。已把 `dispatch_run_ids/dispatch_subagent_ids` 归一为 `run_ids`，这是通用协议兼容修复，不是任务专项分支。
- 第五轮真实 smoke 暴露 repair 路径太重：父级验收已经明确说“请调用 raise_collaboration_event”，但顶层建议仍让主代理创建修复子代理，导致 5 子代理样例在修复 worker 上拖到 180 秒。已改成：协作未落账这类失败优先给 `raise_collaboration_event` 直接建议，context_refs 指向发现者产物和验收报告；普通产物/脚本/文件失败才走 repair worker。
- 第六轮真实 smoke 暴露短写协作意图漏检：子代理产物写 `requires_collab=true` 和 `collaboration_request={...}`，父级验收仍放行，主代理开始汇总而不是落协作 case。已扩展开放世界识别：`collab/coord` 相关字段、非空 request/question/query/observed_facts 等意图字段且没有 case/request/evidence ref 时，必须返工落账。
- 第七轮真实 smoke 暴露两个通用协议兼容点：模型把 dispatch 目标写成 `orchestration.subagent_run_ids`，旧 dispatch 别名没识别；模型把 `output_refs` 写成对象 `{"output_path": "...", "description": "..."}`，旧持久化把整个对象字符串当成文件路径，导致明明写了产物也被父级验收误判缺失。已修成：dispatch 识别 `subagent_run_ids/dispatch_subagent_run_ids`；创建和验收层只从结构化 ref 对象中提取路径字段，保留扩展字段但不把它们变成硬文件名。
- 第八轮真实 smoke 产物存在但判定不通过：主代理最终自己读取 5 个源文件写了结果，说明控制面没有守住“只指挥子代理”。直接根因是模型把 `context_manifest` 写成中文说明“请仔细阅读 source_01.txt...”，输入依赖门把这句说明里的 `阅读source_01.txt` 抽成硬输入路径，导致 5 个子代理没启动。已修成：字符串 `context_manifest` 只有整段是纯路径列表时才作为输入 refs；普通说明文本不再生成缺失输入依赖。结构化 `required_read_paths/input_refs`、dict 输入字段和 list refs 仍然照常生效。

后续方向：
- 先跑 focused 语法和协作工具注册/closeout 相关测试；再用普通中文 prompt 重跑 5 子代理无固定协调员 smoke。只有 5 子代理稳定通过后，才进入 10/20 响应时间和偶然性验证。

## 2026-05-25 / 父级验收存在性别名对齐

状态：本地已落地，focused tests 通过，待真实 11 子代理 smoke 复验

摘要：
- 真实 11 子代理协作 smoke 暴露：worker 已经写出 `check_result_*.json` 产物，但父级验收执行器不认识 `validation_method=file_exists`，导致 `test_execution.json` 记录 `unknown_validation_method`，子代理停在 `AWAITING_ACCEPTANCE/NEEDS_ACCEPTANCE`。
- 修复方向是通用协议对齐，不是 IP 或协作任务专项：`file_exists/path_exists/artifact_exists` 作为存在性别名归一到 `file_check`。
- 如果存在性测试项没有 `file_path`，只从同一 `output.artifacts[].path` 的机器字段展开目标；不从测试名、summary 或任务正文猜路径，继续遵守“自然语言不是机器事实来源”。

验证：
- 一次性最小复现确认旧代码会把 `file_exists` 报成 `unknown_validation_method`，补丁后会展开成 `file_check + file_path` 并通过真实文件元数据验收。
- `ruff check agent_py_agent/agent/subagents/execution_test_items.py agent_py_agent/agent/subagents/execution_executor.py`
- `pytest -q agent_py_agent/tests/test_subagent_test_executor.py agent_py_agent/tests/test_subagents_tests_command.py agent_py_agent/tests/test_runner_input_dependencies.py agent_py_agent/tests/test_orchestration_input_materialization_tool.py --tb=short`

后续方向：
- 用普通中文 prompt 重跑 11 子代理协作真实 smoke，确认 worker 验收不再卡在 `file_exists`。注意：后续复盘已废弃“同一次 dispatch 第二波 coordinator”做法，汇总顺序应由父级显式控制。

## 2026-05-25 / 协作会话 P0 稳定性修补

状态：本地已落地，focused tests 通过，未提交

摘要：
- 新增 `list_collaboration_requests` 只读工具：响应者不知道 `case_id/request_id` 时，可以按当前 runner 身份或显式 `agent_id/agent_name/agent_role` 发现点名给自己的待响应协作请求。它只读结构化 case/request 账本，不按 IP、日志、API 等业务内容做专项判断。
- 后台主代理上下文新增预算裁剪层 `conversation/context_budget.py`：消息、observation、wake signal、agent tree 等只在 prompt 副本里截断长字符串和超大列表，原始账本/产物不改写。这样长期会话不会因为 recent messages、agent tree 或 wake signals 过大而把一次后台唤醒撑爆。
- JSON 文件写入新增 `update_json_file_atomic()`：把 read-modify-write 放在同一个 per-file 锁内，并给 `write_json_file_atomic/read_json_file` 增加跨进程文件锁。JSONL append 继续走现有 append 锁；wake 去重按 `thread_id + dedupe_key` 建小索引，避免全量 pending wake 扫描造成大锁。
- `BackgroundMainAgentScheduler` 新增 per-thread background claim：同一 thread 已有未过期后台运行时，下一轮 tick 不重复唤醒；claim TTL 是“不续约就认为死了”的租约窗口，不是后台任务最长运行时间。运行中会启动后台 heartbeat 线程按 TTL 的安全间隔续约；结束时先停 heartbeat、限时 join，再按 `claim_id` finish，进程崩溃则自然停止续约并让 claim 过期。
- `CollaborationCoordinator` 在 case 升级前会把已过 deadline 且未响应的 request 标为 `timeout`，并写入 `metadata.timeout`。这让主代理能看到“哪条协作请求超时、deadline 是多少”，再决定换路、补派或汇报阻塞，而不是只收到抽象升级事件。
- 真实 smoke 发现：主代理只是 dry-run `dispatch_subagents` 检查输入依赖、调用 `materialize_subagent_inputs` 补齐父级文件、再 dry-run 复核时，工具轮数到顶会被子代理事实收口抢答成“未完成链路”。已调整为只有 runner 真实执行/重试过的 run 才触发工具上限和最终回答事实收口；纯 dry-run、状态检查、输入物化复核不覆盖主代理自然回复。
- 同一 smoke 还发现 dispatch 缺输入时会同时暴露“输入物化建议”和“父级验收 repair child 建议”。后者是时序噪声，因为 runner 没启动，`PLANNING/UNVERIFIED` 不能被当成验收修复任务。现在缺输入 payload 顶层 `next_action` 固定为 `materialize_or_provide_missing_inputs`，并去掉该场景下的 repair advice。
- 真实模型会把 child 缺的短 ref `source.weird` 写成父级相对路径 `parent_inputs/source.weird`。物化服务现在 exact ref 优先，exact 不匹配但 basename 唯一时也会把 child 原短 ref 回绑到受控物化路径，避免复制成功但旧短 ref 仍卡住下一轮 dispatch。
- 可物化缺输入时不再输出 `dispatch_terminal=stop_dispatch_and_report_blockers`。真实模型会被 terminal 带停，甚至口头声称调用了未执行工具；现在有 `input_materialization_recovery` 就以物化/补路径为唯一顶层路线。

验证：
- `python3 -m pytest agent_py_agent/tests/test_collaboration_control_plane.py::test_collaboration_tools_are_registered_and_write_case_flow agent_py_agent/tests/test_collaboration_control_plane.py::test_list_collaboration_requests_finds_targeted_request_without_case_id agent_py_agent/tests/test_collaboration_control_plane.py::test_targeted_collaboration_request_carries_clue_packet_to_responder_context agent_py_agent/tests/test_collaboration_control_plane.py::test_coordinator_marks_expired_request_timeout_before_escalation agent_py_agent/tests/test_background_main_agent_runtime.py::test_background_context_budget_truncates_large_messages agent_py_agent/tests/test_background_main_agent_runtime.py::test_scheduler_skips_thread_with_active_background_claim agent_py_agent/tests/test_background_main_agent_runtime.py::test_scheduler_processes_collaboration_cases_before_waking_agent agent_py_agent/tests/test_conversation_wake_events.py::test_observation_and_wake_signal_are_durable_and_idempotent agent_py_agent/tests/test_conversation_store.py::test_update_json_file_atomic_updates_under_single_file_transaction -q`
- `python3 -m pytest agent_py_agent/tests/test_background_main_agent_runtime.py::test_scheduler_renews_background_claim_while_runtime_is_still_running agent_py_agent/tests/test_background_main_agent_runtime.py::test_scheduler_default_heartbeat_interval_stays_below_small_ttl agent_py_agent/tests/test_background_main_agent_runtime.py::test_scheduler_skips_thread_with_active_background_claim -q`
- `python3 -m pytest agent_py_agent/tests/test_subagent_runtime_guards.py::test_dispatch_limit_response_ignores_seen_but_not_dispatched_scope agent_py_agent/tests/test_subagent_runtime_guards.py::test_dispatch_limit_response_keeps_actual_dispatched_scope agent_py_agent/tests/test_subagent_runtime_guards.py::test_final_response_guard_ignores_dry_run_dispatch_scope -q`
- `python3 -m pytest agent_py_agent/tests/test_orchestration_input_materialization_tool.py::test_dispatch_materialize_dispatch_tool_loop_clears_missing_input -q`
- `python3 -m pytest agent_py_agent/tests/test_subagent_input_materialization.py::test_materialize_subagent_inputs_rebinds_parent_relative_missing_ref_by_unique_basename -q`

后续方向：
- P0 真实复验时继续使用普通中文任务 prompt，不写内部 case/schema 字段。若协调仍慢，优先看 responder 是否调用 `list_collaboration_requests`、是否收到 `targeted_requests`、是否提交 `submit_evidence` 和 `update_collaboration_request`，不要添加业务专项模板或固定 180 秒规则。
- P1 再考虑 archive/purge、active task 清理、按唤醒原因分层工具集、accept/decline wrapper、FakeChannel 故障模拟。

## 2026-05-25 / 长期协作控制面阶段 0-8

状态：本地已落地，离线 focused tests 和小型 live E2E 通过，未提交

摘要：
- 阶段 0 修正 `context_manifest` 输入/输出/线索混判：输入依赖只读取 input-like 结构化字段，`output_path/output_files/output_refs` 不再被当成启动前必须存在的输入文件；类似 IP、版本号这类 dotted numeric clue 不再被误判为文件 ref。
- 阶段 1-2 复用已有 `collaboration` 和 `conversation` 控制面，把长期 thread、wake signal、observation、case/request/evidence 作为结构化账本；它们不绑定飞书/微信，也不绑定日志/IP/API 业务。
- 阶段 3 给 runner prompt 补齐 targeted collaboration clue packet：`problem_statement/observed_facts/query_intent/query_hints/routing_requirements/response_contract/context_refs` 会进入响应代理上下文。字段保持开放世界，`query_hints` 只是软提示，响应代理可以拆分、改写或换来源。
- 阶段 4 跑通同级协作路由：子代理可以打开 case、发请求、提交 refs-first evidence、更新 request 状态，主代理通过 `case_status` 和代理树继续判断。
- 阶段 5 补父子孙冒泡：`raise_main_event` 和 `raise_observation` 在子/孙代理只传 `task_id` 时，会从真实任务树推导 `source_agent_id/parent_agent_id/root_task_id`，避免模型猜错 lineage 字段。
- 阶段 6 增加短协调等待与 watch 边界：`request_collaboration` 支持 `deadline_seconds`；`watch` 默认只观察代理树，只有显式 `advance` 才推进 dispatch；显式 advance 会按当前 workspace run scope 调度，不再因为没有当前聊天轮 scope 被拦。
- 阶段 7 live E2E 先暴露了一个通用 bug：一个 request 发给多个目标时，任一目标提交 evidence 后，旧逻辑会让其他目标丢失 pending inbox。已改为按 responder 身份逐个闭环；多目标 request 只有所有目标都有 evidence 后才算 completed。
- 阶段 8 同步文档并跑整体验收。所有新增行为都在控制面层，不新增 IP、GitHub、PDF、API 等专项模板或专项验收器。

验证：
- `python3 -m pytest agent_py_agent/tests/test_runner_input_dependencies.py -q`
- `python3 -m pytest agent_py_agent/tests/test_runner_prompts.py agent_py_agent/tests/test_subagent_context_bundle.py agent_py_agent/tests/test_subagent_prompt_contract.py -q`
- `python3 -m pytest agent_py_agent/tests/test_orchestration_tools.py agent_py_agent/tests/test_subagent_hierarchy_limits_cli.py agent_py_agent/tests/test_subagent_context_bundle.py agent_py_agent/tests/test_local_collaboration_subagent_integration.py -q`
- `python3 -m pytest agent_py_agent/tests/test_collaboration_control_plane.py agent_py_agent/tests/test_agent/test_planner_and_watch.py agent_py_agent/tests/test_background_main_agent_runtime.py -q`
- live E2E：`/Users/example/my_agent/live-agent-runs/collaboration-control-plane-e2e-20260525-stage7-rerun/stage7_e2e_summary.json`。本轮创建 10 个 source 子代理和 1 个触发子代理；10 个 source 都响应同一个多目标 request，3 个命中，7 个未命中但给出检查范围；最终 `pending_request_count=0`、`completed_request_count=1`、`ready_for_main_agent=true`，后台主代理被唤醒并发送汇报。

后续方向：
- 继续做真实 LLM 长期任务验收时，只用普通用户 prompt。若失败，先看结构化账本里是哪一层断：输入 refs、targeted request、evidence、request lifecycle、wake signal、watch advance、background main-agent。
- 长期驻守类任务后续要进入 watch/schedule runtime，不用普通子代理 dispatch loop 硬跑无限循环；watch 负责观察和必要时唤醒/推进，主代理 LLM 负责二次判断、调度和用户风格汇报。

## 2026-05-25 / 父级共享 brief 与开放 Context Pack 渲染

状态：本地已落地；真实协作 run 已能防假完成，正在补齐父级结果索引收口

摘要：
- 真实 11 子代理协作 run 暴露：主代理先读到告警线索后，资料源子代理仍只知道“与告警相关的关键线索”，但不知道具体线索值；模型自己传的 `context_packs.files` 也没有被 runner prompt 渲染，子代理只看到包名。
- 新增 `orchestration_shared_context.py`：工具归档后从最近小型成功读取结果生成 refs-first `parent_recent_read` brief，包含 `summary/path/ref/source_tool`，不会复制大正文，也不是验收硬门。
- `_tool_loop_service.py` 在工具记录阶段先从本次成功的小型读取结果直接刷新父级共享 brief，再从 `archive_tool_calls` 做补充刷新；这样即使小结果还没被 archive/window 链路扫到，紧接着的 `create_subagents` 也能继承父级已知线索。
- `create_subagents` 创建子代理时把该 brief 追加到每个 child 的 `context_packs`，让协作代理共享父级已读到的小型线索。
- `runner_rendering.py` 的 Context Pack 渲染改成开放字段小型展示：除 `kind/summary/path/ref/role` 外，`files/refs/source_tool/name` 等新增字段也会显示，避免新字段静默进入 task.json 却不进 runner prompt。
- 补丁后真实 run `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-142000` 正常退出，但暴露第二个通用收口问题：source child 的 `latest_summary/output.json` 已包含关键发现，协调产物漏汇总其中一个 child；系统最终没有假报完成，而是用状态摘要拦住了最终回复。当前版本已把旧 `Subagent State Notice` 文案改成中文子代理状态摘要，避免包装词误导排查。
- `dispatch_subagents` 和 `subagent_board` 现在在顶层提前暴露 `child_result_index`，把每个 child 的 `run_id/status/verification_status/summary/output_json/artifact_refs` 放在巨大 `records/items` 前面。父级先按这个索引核对 child 摘要与协调汇总，避免因为 artifact 读取截断、只看输出目录或只信单个协调产物而漏掉已产出的发现。
- 同一轮真实复验继续暴露：协调子代理会因为不知道同批资料源子代理的 `run_id/agent_name` 而 BLOCKED。新增 `orchestration_sibling_roster.py`，批量创建完成后给每个 child 附加 `sibling_roster` context pack，只列出同批 peers 的 `run_id/name/role/goal`。它不再发布 peer 的未来 `output_refs/output_files`，避免把“未来同伴产物”注入上下文。
- 这不是 IP 专项修复；IP 只是真实协作验收的样例。规则只处理“父级已读取的小型上下文如何安全传给子代理”和“开放 context pack 字段如何展示”。

验证：
- 一次性脚本确认 `refresh_parent_shared_context_from_tool_record()` 支持 `{"filesystem": {"path": ...}}` 形态的 `read_file` 参数，能直接生成 `parent_recent_read` 并被 `append_parent_shared_context()` 注入。
- 一次性脚本确认 `refresh_parent_shared_context_cache()` 后 `append_parent_shared_context()` 会追加 `parent_recent_read`，且 `context_packs.files` 会在 runner 渲染里出现。
- 一次性脚本确认 `attach_sibling_roster()` 会给同批 child 持久化 peer roster，runner prompt 能展示 peer run id。
- `ruff check agent_py_agent/agent/agent_core/orchestration_shared_context.py agent_py_agent/agent/agent_core/orchestration_tools.py agent_py_agent/agent/agent_core/_tool_loop_service.py agent_py_agent/agent/subagents/runner_rendering.py`
- `pytest agent_py_agent/tests/test_orchestration_tools.py agent_py_agent/tests/test_orchestration_dispatch_completion_gate.py -q`
- 真实 run `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-135034` 已生成 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-135034/outputs/investigation_result.json`，机器核验命中 `source_02/source_05/source_08`，未确认数为 0。
- 真实 run `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-142000` 验证了系统不会在子代理链路未通过时假完成；本轮记录到的缺口是父级需要更靠前、更紧凑的 child 结果索引。

后续方向：
- 用 `child_result_index + sibling_roster` 补丁后的代码重跑普通中文 prompt 的协作调查真实用例，确认协调子代理能看到同批资料源 run id，并由它自己完成协作汇总。
- 如果长任务仍因 compact 停住，下一轮真实测试应使用可保存 session，并显式开启或测试 auto-compact resume；不要用 `--no-save` 测“完全无人值守长跑”。

## 2026-05-25 / 子代理输入引用归一化

状态：本地已落地，待重新跑真实协作验收

摘要：
- 真实多代理协作 run 暴露：主代理把 `context_manifest` 写成 `read_file:/abs/path/file.txt` 这类工具动作引用时，输入依赖门把整个字符串当成文件路径，导致真实文件存在仍被报 `missing_input_refs`，所有子代理卡在 `PLANNING`。
- `runner_input_dependencies.py` 新增通用文件引用归一化：`read_file:/path`、`write_file:~/path` 等工具动作前缀会在文件依赖判断前剥离，`http://`、`https://` 等真实 URL 不受影响。
- `orchestration_create_context.py` 在创建 context manifest 时也复用同一归一化，避免把带工具名前缀的脏 ref 持久化为硬输入依赖。
- 这不是 IP/日志/GitHub/PDF 专项修复；机器层仍然只看结构化 ref，不根据自然语言任务内容推断业务规则。

后续方向：
- 重跑普通自然语言的多代理协作真实用例，确认子代理能从 `PLANNING` 推进到执行、验收和最终汇总。

## 2026-05-25 / 显式协作请求入口合同

状态：历史方案，已在 2026-05-27 协作简化中废弃；当前不再生成 orchestration 硬合同，也不再用最终回答返工门卡住 root。

摘要：
- 真实 MiniMax 场景暴露：用户普通语言明确要求“创建 11 个子代理/联合其他代理调查”时，主代理仍可直接读完资料并写最终报告，因为旧逻辑只在已有 `task_attributes.subagent_delegation` 或已经创建过子代理时保护派工边界。
- 当时新增过通用 `orchestration_contract`，后来真实协作测试证明这会把模型卡到固定流程里，已删除。
- 当前只保留 tree/refs/dispatch 状态事实；是否继续派工、补查或汇报，由主代理/父代理根据这些事实判断。
- 对照参考：通道运行时 的 subagents 工具把 list/steer/yield 做成显式控制面，OpenHuman 文档强调 subagent/delegate 是可见工具决策；本仓库吸收的是“显式协作必须有工具事实和状态回路”，不是业务模板。

验证：
- 历史测试已删除；当前覆盖见 `test_materialized_delivery_contract_drops_orchestration_contract` 和 dispatch handoff 测试。
- `pytest agent_py_agent/tests/test_delivery_requirement_materializer.py agent_py_agent/tests/test_runtime_delivery_materialization_entry.py -q`
- `pytest agent_py_agent/tests/test_tools/test_tool_loop.py::test_tool_loop_blocks_predelegation_source_body_read agent_py_agent/tests/test_tools/test_tool_loop.py::test_agent_can_delegate_to_subagents_from_tool_call agent_py_agent/tests/test_orchestration_direct_write_guard.py -q`

## 2026-05-25 / 通用多代理协作控制面

状态：已落地 request 生命周期与结构化换路版，离线 focused tests 通过

摘要：
- 新增 `agent_py_agent/agent/collaboration/`，把多代理联动拆成通用 `AgentCapability / CollaborationCase / CollaborationRequest / EvidencePacket / CaseDecision` 账本，不绑定日志、API、数据库、GitHub、论文、PDF、XLSX 等任务专项。
- 新增 `open_case / request_collaboration / submit_evidence / update_collaboration_request / reroute_collaboration_request / case_status` 编排工具。子代理和协调代理默认能参与协作 case，但只能提交结构化请求、证据 refs、请求状态、换路动作和摘要，不直接绕过主代理对用户收口。
- `CollaborationRequest` 采用 append-only 快照更新；`case_status` 折叠为最新状态，同时暴露 pending/blocked/completed/declined 计数、缺证据 request_id、`ready_for_main_agent` 和历史快照计数。
- `reroute_collaboration_request` 和 `update_collaboration_request(target_agent_ids=...)` 会把换路真正落到 `CollaborationRequest.target_agent_ids`，并记录 `original_target_agent_ids`、`rerouted_from`、`rerouted_to` 与审计 decision，避免模型只在自然语言或 metadata 里说“已换路”。
- `CollaborationCoordinator.tick()` 会把证据达标、deadline 到期、请求阻塞或请求可收口的 case 转成 `ObservationEvent / WakeSignal`，接入现有后台主代理唤醒链路；调度器只负责叫醒，LLM 主代理负责二次分析、调度和汇报。
- 协作层坚持 refs-first：大日志、大文件、API 返回、数据库快照、截图和文档正文不进入协作账本，只进入 artifact/tool result 等外部 refs。
- 新增 `update_case_status` 与 `collaboration update-status`，允许主代理/coordinator/人工推进 case 生命周期；状态字段保持开放世界，但关闭/解决/完成类状态必须有 summary、decision_type 或已有 decision，避免 case 无审计关闭。
- `case_status` / `update_collaboration_request` / `reroute_collaboration_request` 的外置工具结果会保留 request/evidence/ready 等 live-prompt 摘要，避免状态工具结果被压缩后模型看不到关键机器字段。
- `case_status` 新增 `rework/rework_targets`，把阻塞请求和缺证据请求转成通用返工目标；它只读结构化 request/evidence 状态，不按任务文本写专项分支。若 metadata 里有 `alternate_sources_available`、`candidate_target_agent_ids` 或 `alternate_target_agent_ids`，会暴露候选目标和 `primary_tool=reroute_collaboration_request`，给主代理一个明确可执行的换路动作。
- 新增 `CollaborationStore.overview()` 和 `my-agent collaboration overview`，给真实复杂任务前做只读控制面体检：ready case、阻塞、缺证据、证据和决策数量一眼可见。
- 子代理创建完成后会登记一份开放世界的 `AgentCapability` 快照：显式 `attributes.capabilities`、角色、展示名、工具名原值都会保留；read/search/fetch 等已知工具只额外补 `query` 这类通用别名，`submit_evidence` 补 `evidence_submission`，不把能力词汇封成枚举。这样 `request_collaboration(required_capabilities=...)` 即使没有显式 target，也能按结构化能力找到现有子代理。
- `open_case` 现在优先使用已存在 thread；如果模型给了猜测的 thread_id/task_id，系统会先按显式 `task_id/run_id` 解析，解析失败但当前处在子代理 runner 内时，再使用当前 runner 的真实 run_id 物化一个 `internal` conversation thread 并绑定该任务。这样本地真实测试、后台 runner 和无飞书/微信通道的子代理也能进入协作账本，不要求用户或模型先手工准备外部会话 id，也不要求模型猜内部 run_id。
- `request_collaboration` 升级为通用线索协作请求：新增 `problem_statement / observed_facts / query_intent / query_hints / routing_requirements / response_contract / context_refs`。这些字段不包含 IP、hostname、订单号等业务枚举；`kind`、`label`、`value`、`terms` 等由 LLM 或上游工具按当前任务生成，系统只保证结构化透传、refs-first 和响应闭环。
- `submit_evidence` 升级为通用协作响应包：新增 `queried_scopes / used_query_hints / miss_reason / response_facts / followup_suggestions / query_actions`。响应代理可以命中、未命中、阻塞或建议继续找其他来源；查不到不是失败，但必须能说明查了什么、限制是什么、证据 refs 在哪里。
- 子代理 runner prompt 只把 `query_hints` 当软提示：响应代理可以完整查、拆分查、改写查、扩大/缩小范围或换来源。机器层不根据 `kind` 写专项逻辑，也不要求用户在普通 prompt 里手写这些字段。
- `BackgroundMainAgentScheduler.tick()` 合并同 thread 的多条 wake signal，同一轮只叫醒一次后台主代理，避免多证据/多阻塞事件造成重复 LLM 唤醒。
- `tool_protocol_v2` 修正旧 flat tool call 的开放世界兼容：业务参数 `status` 不再被协议状态枚举吞掉，`update_case_status(status=needs_replan)` 这类工具调用能保留业务状态。
- 本地 `create_subagents -> dispatch_subagents -> run_subagent` 已接入长期会话继承。主代理在绑定 `RunParams.task_id` 的 thread 内创建子代理时，子/孙代理会继承 `conversation_thread_id/conversation_task_id`，并能用自己的 `run_id` 调用 `raise_main_event/open_case` 回到原 thread。
- 新增 `background-main-agent status` 只读控制面看板，汇总长期会话、绑定任务、wake queue、未处理 observation、progress policy、协作 case 和代理树；它不调用 LLM、不 dispatch、不改状态。
- 新增本地真实协作验收：两个真实子代理分别打开 case/发请求、提交 refs-first 证据/更新请求状态；后台主代理重启后被协作 case 唤醒，并通过 `case_status + inspect_agent_tree` 完成下一步判断。
- 对照来源：通道运行时 的事件账本/replay、长期助手 的后台 wake gate、终端交互 的控制请求/响应模式。吸收的是控制面模式，不复制业务任务模板。

后续方向：
- 继续给真实长期任务增加观测指标，让用户能看到活跃 case、待响应/阻塞/完成请求和已提交证据。
- 后续接入飞书/微信/真实后台服务时，只把通道适配到 Conversation/Wake，不把通道逻辑写进 collaboration 核心。

## 2026-05-24 / 主代理运行门收口与文档同步铁律

状态：部分落地，最新代码本地未提交

摘要：
- 新增开发铁律：每次开发必须同步更新文档。运行语义、合同门、配置、工具行为、验收流程、真实测试方法、架构边界或长期规则变了，同一轮必须更新 `AGENTS.md`、`DESIGN_LEDGER.md`、`docs/design/`、`CODEBASE_TREE.md` 或对应说明；如果不需要文档变更，最终汇报必须说明原因。
- 新增 `docs/design/main-agent-runtime-gates.md`，记录最近几轮主代理运行门调整：探索熔断配置化、本地进展门配置化、显式 `submit_for_acceptance`、无工具最终回复隐式验收、closeout 返工预算配置化、删除 delivery repair 独立运行门、删除 bootstrap 开工物化硬门。
- delivery contract Doctor 现在第一次给结构化返工上下文，第二次仍不可运行时返回 `DELIVERY_CONTRACT_DOCTOR_BLOCKED`，避免机器合同自身坏掉后无限循环；普通产物质量失败仍由 closeout 返工单处理。
- 删除 bootstrap 开工物化硬门的方向已经确定：它不是安全门，也不是最终验收门，不能再拦截普通 `web_search`、`fetch_url`、`read_file`、`list_files`，也不能把普通任务变成必须先写某个系统指定中间文件。
- 当前真实任务测试原则继续保持：prompt 用普通人语言；失败先沉淀离线样本和通用底座修复；禁止新增专项模板或新的非安全前置硬门。
- open write session 门已经从“连续 2 次忽略就阻断”改成“写入事务保护 + 周期提醒”：最终回答、提交验收、读/写未提交目标文件会被立即拉回；非冲突工具继续执行；提醒按未处理 open session 的模型回合计数，默认每 3 回合提醒一次，不再由这个门自己 blocked。

后续方向：
- 迁移旧 closeout 集成测试到显式/隐式提交验收语义。
- 用普通用户 prompt 重跑单周 GitHub 任务，观察自由检索、产物生成、提交验收和返工闭环。
- 产物内容质量问题继续走事实声明、来源引用、事实核对、closeout 返工单，不回退到开工前置模板。

## 2026-05-22 / 主代理阶段 0-6 运行硬门补齐

状态：已落地第一版，focused gate/closeout 测试通过，code-size hard/high-risk/soft 清零

摘要：
- 阶段 0：先看 `/Users/example/study-agent/all-agent/` 的 xlsx 索引和源码，再动本仓库。重点参考 长期助手 的集中 tool guard / approval、通道运行时 的结构化 artifact records、终端交互 的路径/权限边界、会话运行时 的结构化 tool protocol 和恢复 refs。
- 阶段 1：新增 `Run Contract Gate`，收口前必须有 request/run/task/workspace 和有效合同 hash；普通 run 缺 run/task 时在运行入口补齐，不让 closeout 继续靠空 scope。
- 阶段 2：工具归档新增 compact result envelope 和 `tool_result_refs`，把 runtime_gate 的 operation/idempotency/effect 证据保进 `archive_tool_calls`。
- 阶段 3：新增 `Artifact Provenance Gate`，产物即使内容验收通过，也必须能从当前 run 的工具归档记录证明“是谁、哪次 operation、哪个 idempotency_key 写出来的”；旧文件不能冒充本轮产物。
- 阶段 4：closeout 报告新增 `state_gate`，完成必须先有 `RUNNING -> VERIFYING` 的结构化状态门。
- 阶段 5：新增 `Final Closeout Gate`，最终成功只认 `run_contract_gate + runtime_gate + state_gate + acceptance_gate` 四个子门同时放行。
- 阶段 6：新增 `Recovery Lineage Gate`，恢复链路继承旧产物时必须带 `source_run_id + operation_id + path/artifact_ref`，不能把旧产物静默算作当前 run 成果。

约束：
- 这轮没有新增 GitHub/PDF/XLSX/购物站等专项合同；测试里的具体文件名只作为 fixture。
- 机器事实来源只读结构化字段：contract、scope、archive_tool_calls、runtime_gate、tool_result_refs、acceptance_report、recovery lineage。
- 成功 closeout 后重置 local-progress 计数但保留本次 recovery signature，避免旧失败债污染新 attempt，同时保留恢复审计归属。

验证：
- `python3 -m pytest -q agent_py_agent/tests/test_runtime_gate_contracts.py agent_py_agent/tests/test_runtime_gate_integration.py agent_py_agent/tests/test_main_agent_delivery_closeout.py --tb=short`
- `ruff check ...`
- `python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json`

## 2026-05-22 / Runtime tool gateway 缺口 1-4 硬门

状态：已落地第一版，registry focused tests 已通过

摘要：
- 参考 通道运行时 的 typed tool descriptor / plan executor 检查、长期助手 的集中工具守门、终端交互 的结构化权限上下文后，把缺口 1-4 放到工具入口，而不是继续放在最终 verifier 里补救。
- 缺口 1：`Tool Manifest Gate`。每个可执行工具必须声明 effect、参数 schema 和副作用幂等策略；mutating/dangerous 工具缺 idempotency policy 不能进执行。
- 缺口 2：`Path / URL / Command Gate`。工具调用里的 path/url/command 字段在执行前统一检查，阻断 symlink 越界、workspace 外写入、私网 URL、file URL 和未显式允许的 shell 操作符。
- 缺口 3：`Approval Binding Gate`。dangerous + real action 不能只看“有审批”，审批必须绑定 tool/run/operation/idempotency_key/args_hash，防止审批 A 后执行 B。
- 缺口 4：`Idempotency Ledger Gate`。副作用工具必须带幂等键；同 key 同 args 已完成时复用旧结果，同 key 不同 args 直接拒绝。
- `execute_registry_call` 现在先过 protocol/manifest/path/side-effect/approval/idempotency 门，再进入 registry 鉴权和真实工具执行；门的结果写入 `runtime_gate`，供 replay、closeout 和审计读取。
- `tool_manifest_payload` 同步暴露 effect/default_mode/requires_idempotency/requires_approval/timeout/output_refs，避免 context bundle/list_tools 和实际执行门事实不一致。

约束：
- 这轮仍然不增加 GitHub、论文、购物站等专项合同；产物类型 schema 可以存在，任务业务规则不能写进生产门。
- 机器判断只读取 ToolSpec、payload、write_boundary、approved_actions、idempotency_ledger 和 workspace_roots 等结构字段，不扫描普通自然语言。

## 2026-05-22 / 主代理六步稳定化执行规程

状态：设计落地到文档，代码验证进行中

摘要：
- 在 `docs/design/main-agent-contract-testing.md` 固化当前六步执行规程：离线测试门、主代理 fast 验证、P0 合同硬点、少量真实 LLM canary、复杂真实任务并行、真实问题沉淀为离线回归。
- 这次不是新增一套架构，而是把已有 Phase 2.5 路线变成当前开发纪律：不频繁提交，真实任务不作为主要调试方式，发现问题先参考 `/Users/example/study-agent/all-agent/` 下的项目，再做通用底座修复。
- 继续遵守两条铁律：禁止专项合同；代码不得依赖普通自然语言文本作为机器事实来源。
- 参考方向：通道运行时 的 task/run/control-plane 思路，长期助手 的活动记录与恢复边界，会话运行时 的结构化事件和 refs-first 验收。只吸收通用架构策略，不复制业务专项逻辑。
- 2026-05-22 已完成第一轮小型真实 LLM canary：`main-artifact` suite 在 `/Users/example/my_agent/live-lab-runs/20260522-main-artifact-canary-01` 通过，主代理完成大输出 artifact 读回、报告写入、compact apply 和 resume handoff。

后续方向：
- 先跑离线矩阵、replay、code-size 和 fast tests；失败则先做离线 regression，再改通用合同层。
- 只有离线门禁和主代理 fast 验证稳定后，才进入真实 LLM canary 和复杂真实任务并行。

## 2026-05-21 / 主代理 Phase 2.5 阶段 1-4 合同化

状态：已落地第一版

摘要：
- 新增 `main_agent_core_entrypoints` 合同，冻结主代理状态机、工具执行器、验收闸门、RunLog、ToolTrace、ApprovalGate 和 effective contract 这些核心入口，并要求机器事实来自结构化字段。
- 新增 `dry_run_mainline_contract`，用通用字段校验 dry-run 主线的输入、工具结果、产物 refs、证据 refs 和副作用隔离；告警分析只是测试 fixture，不进入生产专项分支。
- 新增 `live_llm_fake_tool_contract`，规定真实 LLM + 假工具试跑必须保存 prompt/response/tool-trace/contract refs 和工具边界违规指标，方便回放和回归。
- 新增 `tool_adapter_readiness_contract`，把真实只读工具和 dry-run 工具上线前要满足的 effect、schema、测试覆盖、审批、幂等和 mode 字段做成通用合同。
- 已把四个区域加入 `check_offline_contract_matrix.py`，后续离线合同矩阵会防止这些入口被漏掉。
- 对照来源：通道运行时 的 effective tool policy pipeline、长期助手 的 inactivity/status/atomic write、会话运行时 的结构化工具与审批边界；本仓库只吸收结构化合同思想，不复制专项任务逻辑。

后续方向：
- 用 fake tool / fake LLM / replay 把真实 LLM 失败样本落进这些合同。
- 真环境测试只做最终验收和失败样本来源，不再作为主要开发循环。

## 2026-05-21 / 主代理 Phase 2.5 阶段 5 真实工具 dry-run 合同

状态：已落地第一版

摘要：
- 纠正阶段号：Shadow Mode 属于动作 6 预备；动作 5 应该先证明真实只读 / dry-run 工具能通过统一工具执行器跑起来。
- 新增 `real_tool_dry_run_contract`，用通用 probe 字段校验 `probe_id`、`operation_id`、`tool_executor_ref`、`result_schema_ref`、`effect`、`mode`、`result.ok`、幂等键和参数 hash。
- Focused 测试实际跑了 `ToolRegistry.execute_call` 下的 `read_file` 只读工具和 `controlled_exec` dry-run 计划入口，不再只看 adapter readiness 声明。
- 合同阻断 direct SDK 绕行、read-only probe 带副作用、dry-run 结果变成 real execution、缺幂等键或缺 args hash。
- 已把 `real_tool_dry_run` 加入离线合同矩阵 gate；外部飞书 / 日志平台 / 防火墙 dry-run 以后接入时必须沿用同一个 probe 合同。

后续方向：
- 外部真实工具进入隔离环境后，补 live probe 记录，但不把业务名和业务流程写进生产合同。
- Shadow Mode 只能建立在阶段 5 真实工具 wrapper probe 通过之后。

## 2026-05-21 / 主代理 Phase 2.5 阶段 6 预备 Shadow Mode 合同

状态：已落地第一版

摘要：
- 新增 `shadow_mode_contract`，把影子模式定义为“真实分析 + 只读/干跑工具 + 建议/草稿/证据/人工复核记录”，但禁止任何真实副作用动作。
- 合同要求风险评分、证据来源、建议动作、dry-run 结果、审批草稿、operator review ref 和 human review 都是结构化字段。
- 人工不同意 Agent 建议时，必须给 `mismatch_reason_codes` 或 `missing_evidence_codes`，方便后续改业务合同，而不是回头读自然语言聊天记录猜原因。
- 已把 `shadow_mode` 加入离线合同矩阵 gate，防止后续只做 dry-run 但漏掉人工对比账本。

后续方向：
- 后续可以进入 TaskTree 前置账本：先做单代理任务树和阶段节点，不急着恢复多 Agent。
- Shadow Mode 真实试跑产生的新偏差，要沉淀成 fake tool / fake LLM / replay 样本。

## 2026-05-21 / 主代理 Phase 2.5 阶段 6-10 真实测试前闸门

状态：已落地第一版

摘要：
- 新增 `shadow_mode_runtime_contract`，要求 Shadow run 必须引用阶段 5 真实工具 probe，并落人工对比 artifact；静态 Shadow 报告不再等同于“影子模式已跑”。
- 新增 `task_tree_ledger_contract`，先把父子任务、依赖、状态、产物 refs 和验收 refs 收成 TaskTree 账本；父任务成功前必须检查关键子任务状态。
- 新增 `long_task_recovery_contract`，把长任务的 run scope、checkpoint、compact/resume、恢复状态和副作用幂等账本做成机器闸门。
- 新增 `failure_sample_library_contract`，要求失败样本必须有 contract fixture、fake tool trace、fake LLM trace、replay spec、expected errors 和 regression test ref。
- 新增 `small_real_acceptance_gate`，大型真实任务前只允许 bounded 小型真实验收：隔离 workspace、只读/dry-run、无真实副作用、可验收、可 replay。
- 这些合同已加入离线合同矩阵；测试 fixture 只是通用结构样本，不把购物站、论文翻译、GitHub xlsx 等专项任务写进生产合同。

参考与约束：
- 对照 `/Users/example/study-agent/all-agent/2.txt`：阶段 6 是 Shadow Mode，之后先做 TaskTree，不直接恢复真实多 Agent。
- 参考 通道运行时 的 task/run registry、长期助手 的长任务活动/恢复记录、会话运行时 的结构化工具和 refs-only 恢复；只吸收架构策略，不复制业务专项逻辑。

## 2026-05-21 / 主代理状态机补 waiting_reason 与 terminal_outcome

状态：部分落地

摘要：
- 共享状态机现在不只给 `lifecycle_phase`，还补了 `waiting_reason` 和 `terminal_outcome`，把“在等工具/等用户审批/等验收/等本地进展”和“真完成/超时/阻塞/仍活跃”拆成机器字段。
- `APPROVAL_REQUIRED` 不再只落在 `recovery_decision` 里，投影时会直接归到 `WAITING_FOR_USER`；`DONE 未验收` 统一进 `VERIFYING`；`TIMEOUT/CHANNEL_ERROR` 统一进 `BLOCKED`。
- 这一步借了参考项目的思路但没照搬名字：通道运行时 把 `status` 与 `terminalOutcome/deliveryStatus` 分开，会话运行时 把 runtime status 和事件流分开；我们这里先做轻量版通用底座。
- 后续方向：继续把更多上层控制面、进度展示和恢复决策改成直接消费这些结构化字段，减少从 `status` 单字段和自然语言提示里二次猜。

## 2026-05-21 / 主代理合同测试底座扩到 runtime issue 与 replay 快照

状态：部分落地

摘要：
- 合同 fixture 不再只校验 artifact 和 tool trace，开始接收通用 `runtime_issue` 事实；fixture 可以声明 `required_issue_codes` 和 `forbid_succeeded_when_issue_codes_present`。
- fake LLM / replay 现在都能携带 `runtime_issue`、`state_snapshot`、`closeout_snapshot`、`acceptance_report` 这几类机器事件，不再只剩 `tool_result + final`。
- replay 的成功态冲突判断扩展到三层：`runtime_issue`、`state_snapshot`、`closeout_snapshot`；如果最终写的是 `SUCCEEDED`，但这些结构化事实仍显示阻塞或未收口，就会报冲突。
- 第一批新增失败样本继续保持通用底座，不引入任务专项分支：`tool_failed_cannot_complete`、`bootstrap_materialization_required`、`repeated_exploration_should_redirect_or_block`、`model_claims_done_without_evidence_should_fail`。
- 后续方向：把主代理真实任务里新暴露的问题优先沉淀成这四层样本（contract/fake_llm/replay/scenario_pack），然后再回真实环境验收。

## 2026-05-08 principal/conversation/config isolation reserve
状态：已落地第一版
摘要：
- `RuntimeIdentity` 已接入 `SubAgentTask` 保存/读取，记录 `service_owner_id`、`requester_id`、`effective_principal_id`、`conversation_id`、`root_run_id`、`memory_namespace` 和配置覆盖 scope。
- LocalStore run metadata 会投影 `runtime_identity`、`memory_scope`、`config_scope`；默认 `conversation_memory_policy=not_enabled`、`promotion_policy=explicit_review`、`writes_global_config=false`。
- `status` / `subagents` 的 `Takeover View` 可以显示 principal、conversation、memory namespace 和 config scope 摘要，便于未来飞书/微信/CLI 多入口与员工会话隔离排查。
- 当前只是审计和扩展口子：不启用员工长期记忆，不允许 conversation/run overlay 自动提升到全局配置，也不把子代理 task/run working memory 直接写成员工记忆。

## 2026-05-11 / Stage7 购物网站真实 E2E：coordinator 只委派，large tool-call 只摘要

状态：待验证

摘要：
- 真实 Stage7 购物网站烟测证明 root->lead->coord->leaf 链路能开始工作，且 report-only coordinator 被产物目录写保护挡住是正确行为。
- 新决策：coordinator 遇到最终产物写入被 `allowed_write_roots` 拒绝时，不应申请自己拿产品写权限，也不应让父代理代写；它必须创建 worker/writer/leaf_worker，并把路径、文件名、验收条件原样传下去。
- 新决策：assistant 生成的大工具调用参数（尤其 `write_file(content=<large html>)`）不能原样回灌下一轮 live prompt。系统只回灌工具名、路径、字段大小、hash 和短预览；完整内容以目标文件/debug detail ref 为准。
- 待验证：清理 Stage7 runtime/deliverables 后重跑购物网站全链路，目标覆盖注册、登录、商品列表/详情、购物车、结账和成功页，且按钮和图片引用可用。

这份文档用来记录我们在交流中形成的新思路，避免后续开发时忘记上下文。

后续 AI 开发者必须先读：
- `LLM_GUIDE.md`（总入口，含开工前/收工后清单）
- `docs/ROADMAP.md`（待做功能）
- `docs/COMPLETED.md`（已落地功能）
- 本文件（设计决策来源）
- 必要时读取 `docs/design/` 下对应模块设计文档

每次出现新的架构想法、命令语义、能力边界或长期方向，都要在这里追加记录，并标明是否已经落地。主设计台账只放导航和摘要；超过约 100 行的模块细节放到 `docs/design/`。

## 状态标记

```text
已落地       已经有代码或配置实现
部分落地     已有基础结构，但还没完整接入运行链路
设计中       已形成方向，还没写代码
待验证       已写代码，但还需要真实场景验证
暂停         暂时不做，但保留背景
```

## 2026-05-11 / Subagent Controlled Shell Gateway 与能力申请闭环

状态：设计中

摘要：
- 用户明确希望子代理、孙代理后续能调用 shell / 本机 CLI / 网络工具 / MCP 工具，但不能给裸 `exec`。设计方向是“受控网关”：子代理把命令请求交给网关，网关按角色、capability grant、路径边界、网络策略、输出预算和风险等级决定是否执行。
- 受控网关要给未来工具扩展留口子：Playwright、Chrome tools、scrapling、curl、日志分析 CLI、用户自装工具、skill 自带脚本、MCP 暴露工具都应注册为 tool/capability card，而不是写死在 runner prompt 或 scheduler 里。
- 子代理不直接拿 `rm`。删除语义改为任务级 trash：每个 task workspace 自动有 `trash/`，删除=移动到 trash 并写 manifest，记录原路径、操作者、原因、时间、可恢复信息；长期任务如果用户清空 trash，工具调用前可自动重建；清理由 TTL/大小上限/任务完成钩子控制。
- 输出外置不能无上限。任何 shell/MCP/tool 输出都要有 output budget：stdout/stderr 捕获上限、artifact 上限、head/tail 或 slice 策略、截断标记、hash/refs、重复读取去重、任务级累计预算和全局并发读取限制。1G/1T 日志不能被直接完整写进 artifact；日志分析优先走 `rg`、`tail`、`head`、`wc`、offset slice、采样和索引。
- curl / 网络工具需要读写分级：GET/HEAD 可以作为低风险候选；POST/PUT/DELETE、上传、带敏感 header/token 的调用需要更高层 grant 或用户确认；响应体同样走 budget、脱敏、外置 refs。
- 能力申请闭环要正式接入 shell/tool/skill/MCP：子代理发现缺工具、缺 skill、缺路径权限、缺网络权限、缺输出预算或没有解决办法时，必须写结构化 `capability_request`；父级能解决就下发 scoped `capability_grant`，不能解决就继续上抛；最终无解必须写 `capability_gap`、finding、shared blackboard 或 skill_spark 候选，不能静默丢失。
- Shell Gateway 和 Capability Grant 责任分离：grant 决定“能不能用、可用范围是什么”，gateway 决定“怎么安全执行、怎么限制输出、怎么审计”。后续 MCP/tools/skills 都应复用同一套审计和预算记录。
- 子代理必须有“写入失败也能说话”的兜底通道：如果 `write_file` 或任务文件落盘失败，runner final response 仍可带结果，父级负责保存 fallback report；不能让内部写工具异常导致子代理彻底失声。

今晚节奏建议：
- 先开发最小底座，再做真实测试。原因是当前真实测试会暴露“缺 shell/缺工具/输出太大/申请没闭环”等已知问题，但如果没有受控记录入口，问题会散在日志里，复盘成本高。
- 今晚第一小步建议只做“设计/记录 + capability request schema 扩展 + trace 记录口”，不要一上来开放真实 shell。字段先预留 `tool_kind=shell|mcp|skill|network|path|output_budget`、`requested_command/tool`、`cwd_scope`、`network_scope`、`output_budget`、`risk_self_assessment`、`fallback_attempted`、`escalation_target`。
- 第二小步再做受控 shell dry-run / allowlist 第一片，先只允许 `pwd`、`ls`、`rg`、`python -m pytest`、`node --version`、GET/HEAD curl 这类低风险动作。
- 第三小步才进入真实 E2E：让 root 自己创建下级，叶子遇到工具不足先申请，父级路由 grant/gap，再继续执行。测试同时覆盖申请成功、申请失败、无解记录 gap、输出截断和 trash 行为。

## 2026-04-30 / Subagent 质量契约与用户少说派工

状态：部分落地

模块设计文档：[docs/design/subagent-quality-contract.md](docs/design/subagent-quality-contract.md)

摘要：
- 子代理质量差的核心原因通常不是能力不足，而是父会话没有把目标质量、成功样本、交付红线和验收标准结构化传下去。
- 子代理应从“独立负责人”降级为“受控施工队”：负责生产材料、局部检查、挑错和修指定缺陷；不能定义完成标准，不能决定最终交付。
- 后续引入 `QualityContract`、context pack、context manifest、producer/critic/reviewer 角色拆分和父会话反验收。
- 新增痛点到解决项映射：防 fake done、防机械 PASS、防长 prompt 稀释重点、防多个 worker 各自当总负责人、防成功样本只留在聊天记忆里。
- 设计方向调整为“内置 workflow 模板，不内置固定 subagent 角色”：模板描述拆工拓扑、证据要求和验收闸门，worker 职责在运行时动态生成。
- workflow 支持 `auto | manual | off` 三档；默认可以自动套模板，用户也可以关闭、手动指定或复制内置模板到用户目录后修改。
- 用户少说模式是目标：用户只表达任务和偏好，系统自动选 profile、写质量契约、派 producer/critic、落证据和验收报告。
- 规范补充：后续每一个开发项都要显式写“解决问题”，说明它解决哪个用户痛点、系统风险或交付缺口，避免只罗列模块名和 workflow 名。
- 这条规范追溯适用于已经写进模块设计文档的旧 Phase；旧 Phase 后续被补录、拆工或复盘时，也要补上“解决问题”，不只约束新增 workflow。

已落地：
- `SUBAGENT_RUNBOOK.md` 已记录质量契约、受控施工队、上下文包、反验收和阶段路线。
- `TEST_CHECKLIST.md` 已补充相关检查项。
- `docs/design/subagent-quality-contract.md` 承载完整模块设计。
- Phase 1 已落地：
  - `AgentConfig` 增加 `subagent_workflow_mode: auto | manual | off`、内置模板开关、用户模板目录和 review rounds，非法值会回退并记录 warning。
  - 新增 `agent_py_agent/agent/subagent_workflows/`，支持加载内置 JSON workflow、用户 JSON 覆盖模板、模板校验和 `solves` 字段。
  - `SubAgentTask` / `SubAgentExecutionContext` 增加 `QualityContract`、`ContextManifest` 和 `context_packs`，执行上下文 Markdown 明确子代理不能自判最终完成。
  - 验证：Subagent workflow 专项组合 14 passed；packaging/agent/tools 回归 63 passed；全量 `python -m pytest` 186 passed。

后续方向：
- 第一批并行 worker 已完成：配置与开关、模板 schema/store、QualityContract/Context Pack。解决问题：先把用户少说模式、可控开关、模板持久化和交付质量契约打底，缓解“父会话说不清、worker 各干各的、成功标准只在聊天里”的痛点。
- 第二批并行 worker 建议拆为 Workflow Router、Workflow Compiler、Parent Gate / Acceptance Planner。解决问题：把自然语言任务稳定路由到合适 workflow，并在派工前生成可执行计划和父级验收闸门，缓解“派错工、漏验收、机械 PASS、fake done”的痛点。
- 详细开发步骤、内置项、开关项、用户必须表达的内容和 workflow 模板库计划见模块设计文档。

## 2026-04-30 / Log Analysis 第一版验收与 worker 切片

状态：部分落地

模块设计文档：[docs/design/log-analysis.md](docs/design/log-analysis.md)

摘要：
- 日志分析第一版已经有 SecurityAlertV1 接入、JSONL store、受控 query、软检测器、case、route/report 和 analyst/reviewer 合同。
- 父会话初验发现 ingest/query 默认路径、EvidenceRef、dispatch contract、case dedup、缺时间戳关联、report finding 过滤等 P0/P1 问题，并已修复复验。
- 第二轮已接入 `my-agent logs status/ingest/query/hunt-ip/trace-case`、ToolRegistry 安全工具授权、结构化 query plan 和 prompt 工具名统一。
- 当前复验：LOG CLI/tools/detector/model 组合测试 52 passed，全量 172 passed，手工 logs ingest -> query 查回 3 rows。
- 剩余主要是 runtime capability 自动接线、storage audit、Live Lab replay 和 README 快速开始。

后续方向：
- 下一批 worker 优先做 runtime capability 自动接线、storage audit、Live Lab replay 和用户文档。
- 是否引入 DuckDB/Parquet/Kafka/ML 依赖仍由父会话裁决，不交给 worker 默认决定。

## 2026-04-29 / 可见真实环境测试台 Live Lab

状态：部分落地

思路：
- 开发 agent 不能只靠单元测试，需要能在用户看得见的终端里跑真实 runtime。
- 测试台必须显示发给 my-agent 的 prompt、实际命令、stdout/stderr、耗时、退出码和证据路径。
- 默认必须隔离 workspace，避免真实测试污染开发仓库。
- 默认不烧真实 API；只有显式 `--real-llm` 才跑真实 LLM case。

已落地：
- `scripts/live_agent_lab.py` 作为薄入口。
- `scripts/live_lab/cli.py` 管参数。
- `scripts/live_lab/runner.py` 管隔离配置、命令执行、transcript 和 summary。
- `scripts/live_lab/cases.py` 管 health、bad-weather、gateway ask 和 long subagent 场景。
- `scripts/open_live_lab.sh` 可在 macOS 新开可见 Terminal。
- `validation/live_lab/` 已加入 `.gitignore`。

后续方向：
- 增加 memory 长任务、tools 边界任务、问题任务和多轮恢复任务。
- 把真实测试结果摘要索引进 LocalStore，方便之后按 case 和失败类型搜索。
- 增加可配置任务矩阵，支持批量跑大量真实 LLM case。

## 2026-04-29 / 并行开发 Workstream 工作台

状态：部分落地

思路：
- 多个 会话运行时/AI 可以并行，但必须先把目录、分支、职责边界和交接格式定清楚。
- 主工作区只做集成和验收；每条开发线用独立 `git worktree` 和 `workstream/<name>` 分支。
- 并行线不直接合并 main；完成后写 handoff，由主线统一检查 diff、跑测试、解决冲突和提交。

已落地：
- `WORKSTREAMS.md` 定义 workstream 规则、预置开发线和主线集成流程。
- `HANDOFF_TEMPLATE.md` 定义交接格式。
- `scripts/workstream_create.sh` 创建 worktree 和分支。
- `scripts/workstream_status.sh` 查看主仓库和所有 worktree 状态。
- `scripts/open_workstream.sh` 打开某条开发线的可见终端。
- `scripts/workstream_common.sh` 统一路径、分支命名和名称校验。

预置开发线：
- `memory`
- `framework-runtime`
- `tools-boundary`
- `live-lab-test`

后续方向：
- 根据真实使用情况加入 `workstream_sync.sh`、`workstream_handoff_check.sh`。
- 主线集成时增加“读取 handoff + diff + 测试结果”的固定 checklist。
- 如果并行线数量增加，再考虑自动生成每条线的专属 会话运行时 prompt。

## 2026-04-29 / Memory 第一批痛点归档

状态：部分落地

思路：
- 记忆系统的问题不是“没有记忆”，而是层级、召回、任务状态、flush、lesson 抽象和未来 skill 沉淀之间没有稳定同步。
- 关键规则不能只依赖 RAG；需要 HOT 层、INDEX 和固定引用入口。
- 任务状态不能只写进 memory，必须以任务目录、证据和测试结果为准。
- 历史 session 里的“已完成/已修复”只能作为线索，不能直接当当前事实。
- 子代理成果必须进入任务目录和可恢复摘要，不能只留在聊天汇报里。
- 自学习先不做，只预留 Skill Draft / Learning Candidate 的位置。

已落地：
- 新增 `MEMORY_BACKLOG.md`，把第一批痛点去重成 15 类，并记录初步设计原则。

后续方向：
- 等第二批痛点输入后继续合并去重。
- 再讨论 memory 最小闭环：`memory doctor`、`memory flush`、结构化 `memory write`、HOT/INDEX 入口和子代理收束摘要。
- 在正式开发前先明确 memory 分层和写入验证规则。

## 2026-04-30 / Memory 全量归档等级与压缩前 Hook

状态：部分落地

思路：
- 原始会话可以做全量冷归档，但不能直接进入 prompt。
- 普通用户不应该面对一堆细碎开关，更适合一个 `memory_archive_level`。
- `memory_archive_level` 采用 0-3 级：0 最完整，3 最小但仍保留最大化恢复任务所需字段。
- 多轮上下文压缩后仍会遗忘，所以压缩前必须有 hook，先保存结构化恢复快照，再允许压缩。
- hook 快照不和 daily memory 混放，按天写入独立 `memory/hooks/YYYY-MM-DD.jsonl`。
- hook 快照默认保留 7 天，用户可以按硬盘情况调大或不限制。
- 记忆要按功能分目录：daily、hooks、raw、index、hot、lessons、toolchains、tasks 各自独立。
- token / context budget 必须实时可见，不能等用户已经丢状态才发现。
- 记忆配置解析必须安全默认：乱码、注入字符、非法枚举、越界数字都不能直接生效，要回落默认/安全值并可观察。

已落地：
- `MEMORY_BACKLOG.md` 记录了 raw archive 0-3 等级、等级 3 的恢复底线、压缩前 hook 字段、每日 hook 文件、默认 7 天保留期、token 可见性要求和配置安全默认规则。

后续方向：
- 设计 `memory_archive_level` 配置项。
- 设计 `memory_hook_retention_days`、`memory_hook_enabled` 和 `memory_hook_archive_level` 配置项。
- 设计 memory 配置解析器和 `memory config doctor`，展示最终生效值与回落原因。
- 设计 compression snapshot 存储格式。
- 在 `status` / chat / Live Lab 中展示 token 估算和压缩风险。
- 实现前先定义脱敏策略，避免全量归档写入 key、cookie、token。

## 2026-04-30 / Memory 长期规则索引化与强制路由

状态：部分落地

思路：
- 长期规则不能一条条塞进常驻 memory，否则用久后会变成第二个臃肿上下文。
- 常驻 memory 只做入口导航：从 `MEMORY.md` 指向 routing index，再由 index 指向正式规则文件。
- 正式规则文件可以写得详细，但默认不进入 prompt；只有命中触发条件时才读取。
- 只靠提示词要求 AI “记得去读 index”不可靠，运行时必须做一层确定性 memory router。

建议结构：
- `MEMORY.md`：极短顶层导航，只告诉模型有哪些 index。
- `memory/routing/INDEX.md` 或分主题 routing 文件：写 trigger、aliases、scope、authority_path、priority、stale_check。
- `references/.../*.md` 或 `memory/rules/.../*.md`：正式长期规则正文。

防止 AI 不遵守的工程手段：
- 每轮用户输入先过关键词 / FTS / route matcher，产出 `required_read_paths`。
- 高置信和 strict 规则由代码自动读取；中置信规则作为候选提示；低置信只记录。
- 正式任务、安全边界、工具权限、记忆写入等关键场景开启 strict gate：命中规则但没有读取权威文件时，不允许直接给最终结论。
- 每次读取写 `memory_read_receipt`，记录 route_id、路径、hash、耗时和触发原因。
- `memory doctor` 检查索引路径是否存在、触发词是否冲突、正式规则是否过期。
- 加路由测试：给定触发词，必须命中指定 index 和 authority_path。

已落地：
- `MEMORY_BACKLOG.md` 记录了长期规则索引化、两级/三级导航、索引字段、strict/soft 路由模式和 receipt 思路。

后续方向：
- 定义 `MemoryRoute` 数据结构。
- 实现 `memory-route` / `memory doctor` 的最小版本。
- 把路由命中结果接入 chat、gateway request 和正式任务恢复流程。

## 2026-04-30 / Memory 压缩方式调研

状态：设计中

调研对象：
- LangGraph / LangChain：短期记忆超上下文后支持 trim、delete、summarize 和 checkpoint。
- OpenAI Realtime：支持 auto / disabled truncation，也支持 retention ratio；说明了截断会从最旧消息开始丢上下文。
- OpenAI Agents SDK：`OpenAIResponsesCompactionSession` 通过 trigger hook 自动 compact，并会重写 session history。
- LlamaIndex：短期 chat history 有 token ratio，超出后把旧消息 flush 到长期 memory blocks。
- OpenAI Agents SDK sandbox memory：run 结束后先做 conversation extraction，再由 consolidation agent 汇总到 `MEMORY.md` / `memory_summary.md`。
- MemGPT：把上下文窗口当快内存、外部存储当慢内存，走操作系统式分层记忆。
- 模型助手 Code：启动入口保持短规则，复杂流程放 skill / scoped rules，避免常驻上下文膨胀。

结论：
- 不采用单一压缩方式；直接截断太危险，纯摘要会漂移，纯 RAG 不可靠。
- 第一版采用组合策略：实时 token 预算 -> 压缩前 hook -> 滚动摘要或结构化摘要 -> raw 冷归档 -> 后台提取 daily/task/lesson 候选。
- 压缩摘要只服务“下一轮模型继续推理”，不能当事实源；事实源必须回到 task `STATUS/HANDOFF/ACCEPTANCE/TESTS`、daily memory 和 hook/raw archive。
- 严禁无 snapshot 的 truncation。即使是应急滑窗，也必须先写 `memory/hooks/YYYY-MM-DD.jsonl` 并 readback 验证。
- 正式任务默认使用 `structured_summary`，普通聊天默认 `rolling_summary`，后台再用 `extract_then_consolidate` 做长期沉淀。

已落地：
- `MEMORY_BACKLOG.md` 新增“压缩方式调研”章节，记录各家做法、优缺点和我们的模式枚举：`off`、`truncate_after_snapshot`、`rolling_summary`、`structured_summary`、`extract_then_consolidate`。

后续方向：
- 定义 compression snapshot JSON schema。
- 定义摘要 prompt，要求输出 snapshot/task refs，禁止把摘要写成事实结论。
- 定义 token budget 估算器，把 system、messages、tools、tool results、中文文本都纳入估算范围。
- 在恢复链路实现固定顺序：compression snapshot -> task 权威文件 -> daily memory -> HOT/routes -> raw archive/RAG。

## 2026-04-30 / Memory 第一版骨架并行落地

状态：部分落地

思路：
- memory 可以先开工，但第一版只做可测试骨架，不急着把所有流程接进主循环。
- 三条线可以并行：配置安全默认、长期规则 router、压缩前 hook/raw archive。
- 主线负责收口语义一致性，尤其是安全默认值不能和 router 行为冲突。

已落地：
- 配置线：新增 `MemorySettings`、`MemoryConfigWarning`、配置规范化和 warning receipt；`AgentConfig` 新增 memory 配置字段；`agent_config.yaml` 写入中文注释。
- 路由线：新增 `memory_routing/`，支持 JSON/Markdown route index、关键词/别名匹配、soft/strict path resolution、route 诊断和 read receipt 结构。
- 归档线：新增 `memory_archive/`，支持 `CompressionSnapshot`、`RawMemoryEvent`、每日 hook/raw JSONL、snapshot readback 验证、hook retention 和保守 token 估算。
- 主线修正：`memory_rule_auto_read_limit=0` 明确为“不自动选择读取路径”，避免配置写 0 时扩大读取范围。
- `agent/` 根目录只保留 `memory_settings.py` 兼容门面，真实实现归到 `settings/memory.py`，继续遵守分层原则。

后续方向：
- 把 router 命中结果接入 `SimpleAgent.run()` 的 prompt 构造前置步骤。
- 增加 `memory doctor` / `memory-route` CLI，展示配置 warning、route 诊断、hook 留存状态和 read receipt。
- 把 `append_snapshot` 接到真实压缩前 hook；目前还没有真实压缩流程，所以只先保留存储 API。
- 设计 raw 大正文落盘和脱敏策略，避免把 key/cookie/token 全量写入冷归档。

## 2026-04-30 / Memory 可见诊断与主循环薄接入

状态：部分落地

思路：
- memory 骨架不能只停在库函数；必须让用户和开发者能看见它怎么路由、哪里有配置回退、归档目录有没有动静。
- 接主循环时要薄：不重写工具循环，不让长期规则全文常驻，只在命中 route 时注入短 authority section。
- raw archive 先跟随 `save/auto_save_memory`，用户 `--no-save` 时不写冷归档，避免违反显式不保存语义。

已落地：
- 新增 `memory-route`：给定 query，读取 `memory/routing/INDEX.md` 或 `--index` 指定文件，输出 route matches、required/candidate paths 和诊断。
- 新增 `memory-doctor`：展示 memory effective config、config warnings、route index 校验、hook/raw 目录状态。
- 新增 runtime routing context：安全限制在 workspace root 内读取 authority 文件，生成 injected sections 和 read receipts。
- 新增 raw archive run helper：把一轮 run 的 user/assistant/tool metadata 写入 `memory/raw/YYYY-MM-DD.jsonl`，生成稳定 event_id/content_hash 和 token estimate。
- `SimpleAgent.run()` 已接入：
  - 调模型前读取命中 route 的短 authority section 并注入 prompt。
  - 保存对话时写 raw archive；`save=False` 时不写。
  - `AgentRunResult` 增加 routed rule 和 archive 统计字段。

后续方向：
- 把 read receipts 写入 LocalStore event，方便 timeline/doctor 查“本轮到底读了哪些规则”。
- 增加 route index 默认模板和创建命令，降低用户第一次配置成本。
- 接入真正压缩前 hook，当前 raw archive 已接主循环，但 compression snapshot 还只是存储 API。
- 做脱敏策略和大正文 blob 存储，不把工具输出正文直接塞进 raw event preview。

## 2026-04-29 / 大文件拆分第一步

状态：部分落地

思路：
- `__main__.py` 应该是 CLI 入口，不应该长期承载 gateway 文件协议细节。
- gateway 是独立协议边界：路径、请求队列、adapter、恢复、响应、索引重建都可以单独成模块。
- 新拆出的模块必须按“两层注释”写：第一行给 LLM 技术契约，第二行开始给人讲大白话。

已落地：
- 新增 `agent_py_agent/agent/gateway.py`。
- 从 `__main__.py` 拆出 gateway 路径、adapter 路径、JSON 文件读写、pid/日志小工具、请求提交、响应等待、processing 恢复、adapter inbox/outbox 处理、gateway 请求执行和 gateway 索引重建。
- `__main__.py` 保留旧导入兼容，测试仍可从 `agent_py_agent.__main__` import 旧函数名。
- 新增 `ARCHITECTURE_GUIDE.md`，记录架构边界、拆分原则和两层注释规范。
- `gateway.py` 的关键函数都补了 LLM contract + Human version 两层注释。

后续方向：
- `subagent.py` 继续拆：models、rendering、parsing、dispatch、acceptance。
- `__main__.py` 继续拆：scenario-test、local-doctor/local-rebuild。
- 后续每拆一块，都要保留测试命令和变更说明。

## 2026-04-29 / 大文件按职责拆分完成

状态：已落地

思路：
- 不是为了凑行数拆文件，而是按变化原因拆：CLI、工具、gateway 协议、LocalStore、SimpleAgent 编排、subagent 管理各自成边界。
- 旧入口继续兼容，避免一次重构让测试、脚本和历史导入全部断掉。
- 新拆出的包都用模块 docstring 写“两层注释”：第一段给 LLM 技术契约，后面给人讲白话边界。

已落地：
- `agent_py_agent/cli/`：把 `__main__.py` 拆成 common、local、subagents、daemon、gateway、adapter、scenario、chat、parser。
- `agent_py_agent/agent/agent_core/`：把 `core.py` 拆成 runtime、subagent、dispatch、planner、runner prompt、runner dispatch、orchestration tools。
- `agent_py_agent/agent/tooling/`：把 `tools.py` 拆成 models、filesystem、web、parser、registry、write_boundary。
- `agent_py_agent/agent/gateway_parts/`：把 `gateway.py` 拆成 paths、io、process_control、logging、recovery、runtime、adapter。
- `agent_py_agent/agent/local_storage/`：把 `local_store.py` 拆成 models、schema、records、search、events、maintenance。
- `agent_py_agent/agent/subagents/`：`subagent.py` 继续作为兼容入口，manager 能力拆成 models/reports/rendering/parsing/policies/probe 和多组 manager mixin。

当前约束：
- 生产 Python 文件当前没有超过 500 行；最大文件 `agent_py_agent/cli/gateway_process.py` 为 498 行。
- 不鼓励跨模块引用 internal 细节；新功能优先走兼容入口或对应职责包公开 API。
- 历史函数 docstring 还会随着后续触碰继续补齐到完整“两层注释”。

验证记录：
- `python3 -m py_compile agent_py_agent/__main__.py agent_py_agent/cli/*.py agent_py_agent/agent/*.py agent_py_agent/agent/*/*.py`
- `agent_py_agent.tests.test_tools`
- `agent_py_agent.tests.test_agent`
- `agent_py_agent.tests.test_local_store`
- `agent_py_agent.tests.test_packaging`
- `agent_py_agent.tests.test_cli_reference`
- `python3 -m agent_py_agent --help`
- `python3 -m agent_py_agent local-doctor --json`
- `python3 -m agent_py_agent gateway status`

后续方向：
- 继续把 `manager_*` 里少数 100 行以上复杂函数细拆成 validator/policy/render/repository。
- 给迁移出来的历史函数逐个补齐更细的人类大白话注释。
- 增加 import 边界测试，防止后续从上层模块反向依赖底层实现。

## 2026-04-29 / 第二轮目录归位

状态：已落地

思路：
- 第一轮解决“文件太大”，第二轮解决“根目录仍然平铺”。
- 根目录只保留兼容门面，真实实现进职责目录。
- 未来方向可以先建目录和 README 定义边界，但不写无意义 wrapper 或空代码。

已落地：
- `backend.py` -> `backends/base.py`
- `config.py` -> `settings/config.py`
- `capabilities.py` / `capability_config.py` / `skills.py` -> `capability/`
- `file_io.py` -> `io/jsonl.py`
- `memory.py` -> `memory_store/jsonl.py`
- `prompting.py` -> `prompting_parts/builder.py`
- 新增 `agent_py_agent/agent/DIRECTORY_GUIDE.md` 作为 agent 目录地图。
- 新增未来目录并用 README 固定含义：`clients/`、`repositories/`、`observability/`、`security/`、`validators/`。

保留兼容：
- `agent.backend`
- `agent.config`
- `agent.capabilities`
- `agent.capability_config`
- `agent.skills`
- `agent.file_io`
- `agent.memory`
- `agent.prompting`

后续方向：
- 为目录边界补 import-lint 风格测试，防止上层反向依赖底层。
- 未来新增外部依赖时优先进入 `clients/`；新增安全硬门禁时优先进入 `security/`；新增跨领域校验时优先进入 `validators/`。

## 2026-04-29 / 本地事实源

状态：部分落地

思路：
- 本地 agent 先保留“文件优先”的可读性，但补一层结构化账本。
- SQLite 负责记录卡片、来源、元数据、更新时间和审计事件。
- FTS5 负责本地全文检索；如果运行环境不支持，就自动退回 LIKE。
- 文件系统保存正文和未来大 artifact，避免数据库变成一个难维护的大黑盒。
- JSONL 保存追加式事件流水，适合人工排查、备份和未来上传同步。

已落地：
- `agent_py_agent/agent/local_store.py`：LocalStore 第一版。
- `JsonlMemory` 双写：记忆继续写 JSONL，同时索引到 LocalStore。
- `local-store-status`、`local-search`、`local-index-memory` 三个 CLI 命令。
- `status` / `timeline` 两个观察入口：一个看当前总览，一个看最近事件。
- 配置项：`local_store_path`、`local_store_files_dir`、`local_store_events_path`、`local_store_fts_enabled`。
- gateway request、gateway 生命周期、subagent run、work log、runner result、execution context、acceptance、patch review、dispatch、watch、parent planner、capability route、action apply、channel probe 已接入 LocalStore。

后续方向：
- 增加定期 compact/rebuild/backup 命令。
- 公司级使用时，本地仍为第一事实源，远端只做同步、备份、组织视图和跨设备协作。
- 向量检索后续可以作为附加索引，而不是替代 SQLite/FTS5/文件/JSONL 这一层。

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
- `/Users/example/Downloads/DISPATCH_PAIN_POINTS.md`
- 文档生成时间：2026-04-28
- 原归属目录：`/Users/example/.长期助手/tasks/2026-04-28/dispatch-pain-points/`

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
- Subagent run 必须有 `workdir` / `task_dir` / `artifact_dir`，并默认禁止写 HOME、Desktop、Downloads、`.通道运行时`。
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

## 2026-04-29 / 外部痛点文档：PAIN_POINTS 小中大型/超大型任务

状态：已记录，部分约束已落地

来源：
- `/Users/example/Downloads/PAIN_POINTS.md`
- 文档更新时间：2026-04-29T00:23:18
- 范围：不重复派工痛点本身，聚焦小众、大型、超大型任务里的上下文、证据、边界、验收和恢复问题。

去重判断：
- 已由现有框架覆盖或部分覆盖：Fake Done 防护、父代理独立验收、runner 不直接 DONE、子代理工单落盘、due-check、channel probe、gateway 恢复、LocalStore/timeline、目录边界字段、patch review、防止空心 heartbeat。
- 与 `DISPATCH_PAIN_POINTS` 重叠：completion notification 不等于完成、父代理不能放养、通道故障和任务失败要分离、P0/P1/P2 分级、子代理输出必须落盘、工具失败要 fallback。

新增或强调的关键痛点：
- 小任务容易裸做：不建现场、不测入口、不留 Action Receipt，把 import PASS 当功能 PASS。
- 中任务容易边界不清：缺 SPEC、缺 `SKILL_USAGE.md`、测试清单不是从需求正推、只测核心路径不测异常路径。
- 大任务容易集成漏项：缺架构/接口契约，横向拆太多但没有竖向闭环，并行后缺集成验收。
- 超大型任务容易状态失控：跨天/压缩/多会话后上下文断片，任务树、owner、交付物、依赖关系失控。
- 不能把时长当工作量，禁止 sleep/heartbeat/cron 空循环凑自治时长。
- 长上下文恢复必须靠任务目录和最小恢复入口，而不是靠聊天记忆。
- 安全/敏感任务要和普通 QQ/聊天主会话隔离，短摘要回流，失败要区分模型风控、工具缺失、权限、网络、证据不足。
- 小众领域不能用通用模板糊过去，开工前要读 skill / references / 外部知识库，并记录使用证据。
- 数据规模必须可断言，例如 `stats.json` 或测试脚本断言，不接受少量 demo 数据冒充规模交付。
- 浏览器/Web/PWA/游戏验收不能只看首页，要覆盖导航、输入、点击、状态变化、localStorage、控制台错误、移动端触控、失败提示。
- 文件很多时必须明确当前事实源：`STATUS.md` 当前状态，`ACCEPTANCE.md` 验收，`TESTS.md` / `TEST_CHECKLIST.md` 验证，`DEBRIEF.md` 复盘。

本轮已落地：
- 子代理标准工单新增恢复/证据入口：
  - `ACTION_RECEIPTS.md`
  - `TEST_CHECKLIST.md`
  - `BUGS.md`
  - `SKILL_USAGE.md`
  - `HANDOFF.md`
- `validate_work_order()` 会把这些新增文件纳入工单完整性检查；旧工单可通过 `repair_work_order` 补齐。
- execution context 的 write boundary 会把这些入口路径下发给 runner，方便子代理按最小事实源写证据。

仍未开发，先记录：
- 顶层任务现场模板：按 small / medium / large / huge 生成 `STATUS.md`、`SPEC.md`、`HANDOFF.md`、`BUGS.md`、`ACTION_RECEIPTS.md`、`SKILL_USAGE.md`、`TEST_CHECKLIST.md`。
- SPEC 编号到 TEST_CHECKLIST / Evidence 的追踪链，避免功能数量多时漏项。
- Tool fallback log：`TOOL_FALLBACK_LOG.md` / `diagnostics.md`，并把工具可用性反馈回 Tool Card。
- Browser/PWA/Game 自动验收器：关键路径、控制台错误、移动端触控、重载恢复、localStorage 检查。
- 数据规模断言器：读取 `stats.json` 或生成测试，确认文档数、切片数、问题数、视图数等不缩水。
- 安全任务隔离策略：独立任务目录、短摘要回流、模型风控分类、`TRIED / FINDINGS / NEXT_ANGLES`。
- 跨天 daily memory checkpoint / handoff 自动生成。
- 顶层 boundary doctor：检查任务是否越权写 HOME、Desktop、Downloads、`.通道运行时` 等禁区。
- “竖向闭环优先”调度策略：大任务先跑一条从入口到验收的可用链路，再横向扩规模。

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
- 新增 `GATEWAY_DESIGN.md`，记录 通道运行时、长期助手、模型助手 session 和 会话运行时 云任务形态的参考。
- 新增 `GATEWAY_RESEARCH.md`，扩展调研到进程守护、AI gateway、Jupyter kernel、Celery/RQ/n8n、Temporal/LangGraph/CrewAI、Node-RED/Home Assistant、Ollama/PM2/Supervisor/Task Scheduler 等方案。
- 新增用户层任务规模配置：`task_max_subagents`、`task_max_grandchildren`，默认 `0` 表示不设硬上限。
- 新增未来 gateway 策略配置：`scheduler_mode`、`runner_concurrency`、`runner_start_rate`、`runner_timeout_seconds`、`runner_failure_policy`，默认 `auto`。
- `daemon_max_runners` 默认改为 `"auto"`；当前前台 daemon 会映射成保守值 1。
- `daemon_limit` 默认改为 `0`，表示每个阶段不限制记录条数。

后续方向：
- 做真正的 `my-agent gateway start/status/stop/restart`。
- 把 `runner_concurrency` 和 `runner_start_rate` 接到后台 worker pool，而不是前台同步循环。

## 2026-04-29 / Gateway 独立主体与委托模型

状态：已落地设计

思路：
- 多 gateway 不应该是“主完整、副低配”的关系。
- 每个 gateway 都是完整独立 agent，有自己的身份、任务账本、记忆、工具、密钥和能力目录。
- 上下级只表示授权、委托、汇报和协调关系，不削弱任何 gateway 的自身能力。

已落地：
- `GATEWAY_DESIGN.md` 增加多 gateway 组织模型。
- 明确 `identity 决定所有权，grant 决定访问权，delegation 决定协调权`。
- 明确 root gateway 可保留 reclaim 协调权，但 active coordinator 不自动获得 root 的全部私有状态。
- 第一版单机 gateway 的 schema 预留 `gateway_id`、`agent_identity_id`、`coordination_epoch`、`delegation_id`、`grant_scope`、`attempt_id` 等字段。

后续方向：
- 第一版仍从单机 gateway 做起，但任务账本和事件日志提前带 gateway/identity/delegation 字段。
- 后续再做跨机器通信、授权交换、本体备份和迁移。

## 2026-04-29 / Organization Gateway Model

状态：已落地设计

思路：
- my-agent 可以组成组织：root gateway 创建组织，其他人或机器通过 invite key 安装并注册自己的完整 my-agent。
- 副 gateway 可以在授权范围内继续邀请自己的下级 gateway，形成公司/部门/成员式组织树。
- 组织关系随时可调整，但调整的是协调权和授权范围，不是 gateway 自身能力。

已落地：
- `GATEWAY_DESIGN.md` 增加 Organization Gateway Model。
- 设计 invite key 字段：`org_id`、`issued_by_gateway_id`、`parent_gateway_id`、`allowed_scopes`、`can_invite_children`、`expires_at`。
- 明确组织架构要有独立状态：parent/children、membership、capability summary、last_seen、trust level、grants、delegations、coordination epoch。
- 明确组织事件日志：join/leave/suspend/revoke/delegate/reclaim/reparent/invite/grant/revoke。
- 增加开工前待补充清单：invite 签名撤销、secret 管理、artifact 同步、离线接管、版本兼容、权限审计和隐私策略。

后续方向：
- 第一版 gateway 先落单机 `gateway_id` / `agent_identity_id` / `org_id` / event log。
- 第二阶段再实现 invite、组织账本和跨 gateway 通信。

## 2026-04-29 / 第一版本地 Gateway 控制面

状态：已落地

思路：
- 先做薄而稳的本地后台外壳，不直接上 worker pool、HTTP API、多机器和组织通信。
- 用户命令面先稳定为 `my-agent gateway start/status/stop/restart/logs`。
- 内部暂时复用现有 daemon/watch 调度，后续再替换成 SQLite jobs 和 worker subprocess pool。

已落地：
- 新增 `gateway` 命令族：`start`、`status`、`stop`、`restart`、`logs`、内部 `run`。
- 新增 gateway 控制面文件：`gateway.pid`、`gateway_state.json`、`gateway_heartbeat.json`、`gateway_stop.request`、`gateway.log`。
- 新增 `gateway_workspace`、`gateway_heartbeat_interval`、`gateway_stale_seconds`、`gateway_stop_timeout` 配置。
- `watch_subagents()` 支持 stop file，gateway stop 可以在调度轮次之间正常退出。

后续方向：
- `chat` / TUI attach 到 gateway。
- 接入 SQLite task ledger、jobs、leases 和 worker pool。
- 增加 systemd / launchd / Windows Task Scheduler 安装入口。

## 2026-04-29 / Gateway 本地消息入口

状态：已落地

思路：
- gateway 不能只是后台调度壳子，还需要接受用户消息并触发完整 LLM turn。
- 第一版先用本地文件 inbox/response 队列，不急着引入 HTTP server、WebSocket 或 SQLite。
- CLI 客户端先验证协议：请求落盘、gateway worker 取走、模型调用、响应落盘、客户端等待或稍后读取。

已落地：
- 新增 `my-agent gateway ask "<prompt>"`，向后台 gateway 投递聊天/任务请求。
- 新增 `my-agent gateway result <request_id>`，读取异步请求结果。
- 新增 gateway 请求目录：`requests/pending`、`requests/processing`、`requests/done`、`responses` 和 `gateway_requests.jsonl`。
- gateway 后台进程启动 request worker，和 dispatch watch 并行常驻。
- `gateway status`/heartbeat 会带 request counts，方便判断是否堆积。

后续方向：
- 让 `my-agent chat` 默认 attach 到 gateway，而不是只在前台进程里跑。
- 将文件队列升级为 SQLite jobs/leases，支持崩溃恢复、重试、超时和 worker pool。
- 再向上接 TUI、HTTP/WebSocket、本地托盘服务和跨 gateway 通信。

## 2026-04-29 / Gateway 说明白话化

状态：已落地

思路：
- `gateway ask/result` 会保留，但它们不是最终普通用户每天必须敲的命令。
- 它们的定位是本地协议验证口、开发者调试口、聊天工具/TUI 接入前的最小客户端。
- 文档和代码注释要把“为什么要有这些命令”“以后接聊天工具后谁来调用它们”“每个队列目录是什么意思”说清楚。

已落地：
- `GATEWAY_DESIGN.md` 增加 `gateway ask/result` 大白话解释和本地队列目录说明。
- `CLI_REFERENCE.md` 增加同步/异步示例、请求流转和定位说明。
- `README.md`、`agent_py_agent/README.md` 增加普通用户视角解释。
- `agent_config.yaml` 增加 gateway 请求队列配置注释。
- `agent_py_agent/__main__.py` 增加 `GatewayPaths`、请求写入、崩溃恢复、worker loop 和 response 输出的代码注释。

后续方向：
- `chat` / TUI 接入后，把 `gateway ask/result` 收到“开发者命令”层级。
- 外部聊天工具接入时复用同一个 request/response 协议，并把响应自动回发给用户。

## 2026-04-29 / Chat 接入 Gateway

状态：已落地第一版

思路：
- 先不改变 `my-agent chat` 的默认行为，避免破坏现有前台交互路径。
- 新增 `my-agent chat --gateway`，让 chat 成为 gateway 客户端。
- 普通自然语言消息走 gateway request/response；本地命令 `/memory`、`/remember`、`/subagents` 暂时继续在当前 CLI 里处理。

已落地：
- `chat --gateway` 启动时检查 gateway 是否正在运行；未运行时提示先 `my-agent gateway start`。
- chat worker 复用 `submit_gateway_ask()` 写入 gateway inbox，并等待 response。
- `/status` 在 gateway 模式下额外显示 gateway 存活状态和 pending/processing/done/responses 数量。
- 新增 `--gateway-timeout`，可临时覆盖单条消息等待时间。

后续方向：
- 稳定后考虑让 `my-agent chat` 默认 attach 到 gateway。
- 把更多 slash command 也改成 gateway 请求，减少前台 CLI 对本地状态的直接操作。
- 给 chat/TUI 增加异步完成通知，而不是每条消息都由当前 worker 等 response。

## 2026-04-29 / 默认入口自动进入 Gateway Chat

状态：已落地

思路：
- 用户安装后应该能直接敲 `my-agent` 使用，不需要先学习 `gateway start` 和 `chat --gateway`。
- 默认入口应该自动确保 gateway 存活，然后进入 gateway chat。
- 退出 chat 不关闭 gateway，保持“前台客户端可退出，后台本体继续值班”的体验。

已落地：
- argparse 子命令改为可选；没有子命令时进入 `cmd_default()`。
- `cmd_default()` 调用 `ensure_gateway_started()`，未运行则自动 `gateway start`。
- gateway 存活后，默认入口进入 `cmd_chat()` 的 gateway 模式。

后续方向：
- 观察默认入口稳定性，再考虑是否让显式 `my-agent chat` 也默认 attach gateway。
- 补更好的首次启动引导，例如配置 API key、模型后端和 gateway 状态提示。

## 2026-04-29 / 主代理自然语言派工工具

状态：已落地

背景：
- 之前 chat 里的主代理会建议“可以拆给 subagent”，但普通自然语言消息不能稳定地真正创建工单和触发调度。
- CLI 已有 `spawn-subagents`、`subagents-dispatch` 等命令，但它们还没有进入主代理可调用工具目录。
- 后续要做隔离版全流程场景测试，需要从 chat/gateway 入口模拟真实用户派活，而不是只靠手动 CLI 拼流程。

已落地：
- 新增 `create_subagents` 主代理工具：把自然语言里的拆分/派工意图落成子代理工单。
- 新增 `subagent_board` 主代理工具：让主代理读取子代理看板、状态和风险旗标。
- 新增 `dispatch_subagents` 主代理工具：让主代理触发一轮父代理调度。
- `dispatch_subagents` 默认 dry-run；`execute_runners=true` 必须配合 `apply=true`，避免误触发真实 runner/API。
- `create_subagents` 默认授予 read-only 工具；只有 `tool_preset="coding"` 或显式 `allowed_tools` 才给写文件能力。

测试：
- 新增工具级回归：模拟模型在普通 `agent.run()` 中调用 `create_subagents`，确认子代理工单真实落盘。
- 同一测试覆盖 `subagent_board`、`dispatch_subagents` dry-run，以及未 `apply=true` 时拒绝真实 runner。

后续方向：
- 基于这些工具增加隔离 fixture 全流程场景测试：chat -> 创建多个子代理 -> dispatch -> runner -> 验收 -> 汇报。
- 给真实场景测试固定临时 `memory_path`、`subagent_workspace`、`gateway_workspace` 和 fixture 工作区，确保不污染当前开发仓库。

## 2026-04-29 / 隔离全流程场景测试

状态：已落地第一版

背景：
- 用户希望能“看完整流程”，而不是只跑分散的单元测试或 smoke。
- 全流程测试必须能调用真实 API，但不能污染当前开发仓库，也不能把 runner 文件写到项目源码里。
- gateway、主代理自然语言派工、subagent runner 和父代理验收需要被串起来观察。

已落地：
- 新增配置 `workspace_root`：为空时保持默认项目根；设置后 memory、subagent、gateway、prompt_files 和文件工具都以该目录为根。
- 新增 `my-agent scenario-test`：
  - 每次创建独立 `scenario-*` 临时目录和 `fixture_project`。
  - 写入隔离配置，把 `workspace_root` 指向 fixture。
  - 默认启动隔离 gateway，使用 `gateway ask` 触发主代理创建多个子代理。
  - 随后由当前进程执行 dispatch，允许真实 runner/API、写入 fixture 内报告，并执行父代理验收。
  - 输出看板、runner 证据文件、`scenario_summary.json` 和 `SCENARIO_SUMMARY.md`。
- 修复真实场景暴露的问题：
  - 同一轮 `run()` 内重复的编排工具调用会被去重，避免模型反复创建相同子代理。
  - 达到工具轮数上限时会再让模型生成最终回答，不再直接返回最后一个工具调用块。
  - 工具解析器兼容模型把 `[TOOL_CALL]` 误写成 `[SUBAGENT_CALL]` 的常见情况。
  - runner 回写会记录系统真实执行过的工具；验收不再相信模型自称的 `used_tools`。
  - 验收会按 `acceptance_checks` 核对 `read_file/write_file` 证据，并检查本地 artifact 路径是否真实存在。
  - 有工具执行记录时，prompt 会把 `Tool Transcript` 放在用户任务之后，并追加继续指令，避免模型每轮被末尾任务说明拉回起点、重复调用同一个工具。

安全边界：
- 默认不复用开发仓库的 `data/`。
- runner 写文件工具只能看到 fixture 工作区。
- `--direct` 可跳过 gateway，便于定位 gateway 和主代理自身的问题差异。
- `--dry-run` 可跳过真实 runner API，只观察派工和调度计划。

后续方向：
- 增加更极端的 fixture：runner 失败、结构化输出损坏、能力请求、stalled、gateway 重启恢复。
- 把场景测试报告做成更适合前端/TUI 展示的事件时间线。

## 2026-04-29 / 坏天气场景测试第一版

状态：已落地第一版

背景：
- happy path 已经能证明主链路能跑通，但真正要长期可靠，需要把失败和恢复场景也做成可重复测试。
- 这些场景必须继续隔离运行，不能污染开发仓库。

已落地：
- `my-agent scenario-test --case verification`
  - 构造一个伪造完成的子代理：声称 read/write 成功，也声称有 artifact。
  - 实际不写 artifact 文件。
  - 父代理验收必须拒绝，并把任务标成 `BLOCKED / FAILED`。
- `my-agent scenario-test --case gateway-restart`
  - 模拟旧 gateway 崩溃时请求卡在 `requests/processing`。
  - 执行 gateway 启动恢复步骤，把请求退回 `requests/pending`。
- `my-agent scenario-test --case structured-repair`
  - 模拟 runner 已回复但 `[SUBAGENT_RESULT]` JSON 损坏。
  - 父代理触发修复回合，只整理格式，不新增事实。
  - `runner_result.json` 记录 `structured_repair_attempted=true` 和 `structured_repair_ok=true`，修复后进入验收。
- `my-agent scenario-test --case runner-retry`
  - 模拟 runner 第一次模型调用出现临时错误。
  - 任务先落成 `BLOCKED / runner_error`，记录 `runner_attempts=1` 和最后错误。
  - 下一轮 dispatch 识别为可重试错误，执行 `retry_runner`，成功后进入父代理验收并变成 `DONE / VERIFIED`。
- `my-agent scenario-test --case all`
  - 依次运行 `verification`、`gateway-restart`、`structured-repair`、`runner-retry`、`happy`。

调度策略：
- 新增 runner 尝试计数：`runner_attempts`、`runner_last_attempt_at`、`runner_last_error`。
- `runner_failure_policy: "auto"` 当前表示总尝试次数为 2；`"off"` 表示不自动重试；数字字符串如 `"3"` 表示最多尝试 3 次。
- 自动重试只覆盖 `runner_error`、`structured_output_parse_error`、`tool_result_missing`、`model_error`、`api_error`、`transient_error`。
- 不自动重试能力缺口、验收失败、通道 BROKEN、接管任务，避免父代理在权限或事实不明时重复消耗 API。

后续方向：
- 增加 stalled 接管、能力缺口上抛再 rerun 的场景。
- 给 `scenario_summary` 增加事件时间线，方便 TUI/网页观察。

## 2026-04-29 / Qwen XML-ish 工具调用兼容

状态：已落地

背景：
- 真实模型或其他 agent runtime 可能不会严格输出我们提示词里的 `[TOOL_CALL]` JSON，而是输出类似 Qwen/通道运行时 的 XML-ish 方言：`<tool_call><function=read><parameter=file_path>...</parameter></function></tool_call>`。
- 之前这类输出会落在解析器外；如果 runtime 侧尝试解析不完整片段，还可能出现 `Failed to parse input at pos ...` 一类错误。

已落地：
- `ToolRegistry.parse_tool_calls()` 继续优先支持标准 `[TOOL_CALL]` / `[SUBAGENT_CALL]` JSON 块。
- 新增 XML-ish `<tool_call>` 方言解析：`read` -> `read_file`、`write` -> `write_file`、`search`/`grep` -> `search_text`、`list`/`ls` -> `list_files` 等常见别名会自动归一化。
- 参数别名会归一化：`file_path`、`filepath`、`filename`、`file` -> `path`。
- 参数内容会做 HTML entity 解码，简单 JSON 值会尽量还原成对象/数组/数字/布尔值。
- 如果 `<tool_call>` 只有半截、缺少 `</tool_call>`，不会让主循环崩溃，而是返回 `__parse_error__`，让后续模型回合有机会修正格式。

测试：
- 新增工具解析回归：标准 `[SUBAGENT_CALL]` alias、XML-ish read、XML-ish write、半截 XML-ish parse error。

后续方向：
- 如果接入更多模型方言，再把解析器扩展成显式 adapter 列表，并记录每种方言的命中率和失败样本。

## 2026-04-29 / Runner actual_tools 系统证据

状态：已落地

背景：
- 真实 runner 可能确实调用了 `read_file` / `write_file`，但结构化 evidence 里只写“读取 README 成功”“写入报告成功”，没有把工具名写进 `kind`、`command` 或 `summary`。
- 旧验收逻辑会因此误判缺少 `read_file` 证据，哪怕系统自己的 `actual_tools` 已经记录了真实工具调用。

已落地：
- `record_runner_result(..., actual_tools=[...])` 会把实际成功执行过的授权工具落成系统证据，格式为 `kind=<tool>`、`command=<tool>`。
- 验收继续优先信任系统真实工具记录，而不是模型自称的 `used_tools`。
- 保留 artifact 路径存在性等独立验收项，避免只有工具名而没有真实交付物时误过。

测试：
- 新增回归：模型 evidence 不写 `read_file` / `write_file` 字符串，但 `actual_tools` 记录真实执行时，父代理验收应通过。

## 2026-04-29 / 代码体检与后续拆分计划

状态：已完成检查，暂不做大规模重构

当前规模：
- tracked 文本总行数约 1.77 万行。
- Python 约 1.35 万行，Markdown 约 0.39 万行。
- 最大文件集中在：
  - `agent_py_agent/agent/subagent.py`：约 4.8k 行，承担子代理模型、存储、看板、due-check、action、capability route、runner、patch、acceptance、dispatch 等职责。
  - `agent_py_agent/__main__.py`：约 2.7k 行，承担 argparse、chat、gateway、scenario-test、daemon、subagent CLI 等职责。
  - `agent_py_agent/agent/core.py`：约 1.6k 行，承担主循环、runner、planner、dispatch 编排工具等职责。
  - `agent_py_agent/agent/tools.py`：约 1.0k 行，承担工具规格、工具实现、检索和工具调用解析。

文档状态：
- `README.md`、`CLI_REFERENCE.md`、`TESTS.md`、`SUBAGENT_RUNBOOK.md`、`GATEWAY_DESIGN.md`、`GATEWAY_RESEARCH.md`、`CODEBASE_TREE.md` 已覆盖当前主链路。
- 本轮补充 `CODEBASE_TREE.md`，加入 `test_cli_reference.py`、`test_packaging.py`，并补充 XML-ish 工具调用解析和 `actual_tools` 系统证据说明。

判断：
- 当前先不急着重构，因为 gateway / scenario-test / runner / acceptance 正处在高频变化阶段，大规模移动代码会增加回归成本。
- 但 `subagent.py` 和 `__main__.py` 已经明显超过长期维护舒适区，后续做 gateway worker pool、SQLite ledger、多 gateway 组织通信前，应该分阶段拆分。

建议拆分顺序：
1. 先拆纯数据和纯解析，风险最低：
   - `agent/subagent_models.py`：dataclass / enum / schema。
   - `agent/subagent_parsers.py`：`parse_subagent_runner_output()`、parent planner parser、结构化输出修复相关纯函数。
   - `agent/tool_call_parser.py`：`[TOOL_CALL]` JSON 和 XML-ish 方言解析。
2. 再拆子代理业务域：
   - `agent/subagent_storage.py`：路径、读写、工单目录初始化。
   - `agent/subagent_acceptance.py`：验收 finding / report / apply。
   - `agent/subagent_dispatch.py`：due-check、action plan、dispatch/watch。
   - `agent/subagent_runner.py`：execution context、runner result、actual_tools 证据。
3. 再拆 CLI：
   - `cli/parser.py`：argparse 构造。
   - `cli/gateway.py`：gateway start/status/stop/ask/result。
   - `cli/scenario.py`：scenario-test fixture 和坏天气场景。
   - `cli/chat.py`：前台 chat 和 gateway chat 客户端。
4. 最后拆测试文件：
   - `test_agent.py` 按 acceptance、dispatch、runner、capability、board/probe 分文件。

约束：
- 每次拆分只移动一个低耦合区域，先保持 import 兼容，跑完整 `run_tests.py` 后再继续。
- 不在同一轮同时改行为和大移动文件，避免不知道失败来自重构还是功能变化。

## 2026-04-29 / 本地恢复、诊断、worker 和 adapter 第一版

状态：部分落地

已落地：
- `local-doctor`：诊断 LocalStore、memory JSONL、gateway 队列和 subagent 工单目录是否一致。
- `local-doctor --repair`：处理超时的 gateway `processing` 请求，未超尝试次数则退回 `pending`，超过则写响应并归档到 `failed`。
- `local-rebuild`：从 memory、gateway、subagent 文件事实源重建 LocalStore，支持 `--source` 和 `--reset`。
- `status` 增加 `suggested_actions`，人类输出也显示建议下一步动作。
- gateway 请求队列新增 `failed` 目录、processing lease、attempts、超时重排和失败归档。
- `gateway_request_workers`：gateway ask/request 第一版保守 worker pool，默认 1 个 worker。
- `runner_concurrency`：runner 并发第一版，默认 `auto -> 1`，只有显式数字才会并行多个不同 run。
- `adapter file`：文件协议适配器，外部聊天工具/TUI 可写 `inbox/*.json`，adapter 投递 gateway 后写 `outbox/*.json`。

仍未开发，先记录：
- LocalStore compact / backup / export / import 命令。
- LocalStore rebuild 的更完整来源覆盖：dispatch/watch/planner 全量历史、任意 `reports/*.json` 的类型化恢复、artifact 大文件索引。
- gateway 请求取消、优先级、租约续期、迟到响应去重策略和更细的错误分类。
- gateway worker 的 API 预算、启动速率、自适应并发和 per-model 限流。
- runner pool 的进程级隔离、session 复用、超时硬中断和跨进程锁。
- adapter 的 HTTP/WebSocket/平台插件版，以及鉴权、会话映射、去重、消息编辑/撤回。
- TUI 观察面板，复用 `status` / `timeline` / `adapter file` / gateway request protocol。

## 2026-04-29 / 注释重构规范

状态：设计已记录，待分阶段落地

背景：
- 当前 Python 代码里函数/类定义约 650 个，其中大量 docstring 是一句话说明，`__main__.py`、`subagent.py`、`core.py`、`tools.py` 尤其需要更清晰的 LLM/人类双层说明。
- 直接全仓一次性重写注释会让 diff 过大，也会进一步放大已经偏大的文件体积；应分模块、分职责迁移。

目标注释格式：

```python
def example(...):
    """LLM: technical summary of the contract, side effects and invariants.

    人话说明：
    这个函数用来做什么，什么时候会被调用。

    具体例子：
    - 输入什么，输出什么。
    - 它会写哪些文件、调用哪些模型或工具。
    - 它失败时怎么表现，调用方应该怎么处理。

    注意边界：
    - 哪些参数不能乱传。
    - 哪些副作用需要审计。
    """
```

规范：
- 第一行给 LLM/后续维护者读，使用技术语言，写清 contract / invariant / side effects。
- 第二行开始给人读，用大白话解释，默认小白能懂。
- 对有副作用的函数必须写清：会不会写文件、调用 API、启动进程、改状态、消费真实模型。
- 对恢复/调度/验收函数必须写清：输入事实源、状态流转、失败时的 fallback。
- 对工具和 adapter 必须写例子，例如一个 JSON 输入如何变成输出文件。
- 测试函数不强求长注释；测试名和断言清楚即可。

建议落地顺序：
1. 先改稳定的小模块：`config.py`、`memory.py`、`local_store.py`、`backend.py`。
2. 再改工具边界：`tools.py`，重点写清读写边界、网络工具、工具解析器。
3. 再改核心编排：`core.py`，重点是 `run()`、`run_subagent()`、dispatch/watch/planner。
4. 再改 CLI：拆分 `__main__.py` 后分别注释 gateway、adapter、scenario、chat。
5. 最后改 `subagent.py`：先拆 models/storage/acceptance/dispatch/runner，再补双层注释。

不建议：
- 不在当前大文件状态下把 650 个函数一次性塞长注释；这会让 `__main__.py` 和 `subagent.py` 更难读。
- 不把注释当设计替代品；复杂流程仍应通过类型、报告 JSON、测试和 runbook 表达。
## 2026-04-30 / 模块文档四件套导航

状态：部分落地

导航：新的模块级长期文档入口是 [docs/modules/README.md](docs/modules/README.md)。每个功能模块使用四件套：`01-discussion.md` 记录灵感和讨论，`02-progress.md` 记录推进、解决的问题和测试，`03-purpose.md` 解释初心和设计想法，`04-structure.md` 说明结构树、核心文件、数据流和新手学习路径。

模块索引已建立：
- [docs/modules/subagent/](docs/modules/subagent/)：链接 subagent 质量契约、受控派工、workflow preview 和旧 runbook。
- [docs/modules/log-analysis/](docs/modules/log-analysis/)：链接日志分析 design、backlog、acceptance、evidence 和当前 work order 方向。
- [docs/modules/memory/](docs/modules/memory/)：链接长期记忆、规则路由、raw archive、恢复和诊断。
- [docs/modules/gateway/](docs/modules/gateway/)：链接后台 gateway、本地请求队列、chat attach、恢复和 adapter。
- [docs/modules/live-lab/](docs/modules/live-lab/)：链接可见真实环境演练、离线 replay、suite/case 产物。

同步门：`scripts/check_doc_sync.py` 已覆盖 `log-analysis`、`subagent`、`memory`、`gateway`、`live-lab`。covered module 改代码时，需要同步更新模块 `02-progress.md`、`04-structure.md`，实现代码新增时还要同文件补注释或 docstring。

维护约定：旧文档暂不搬迁；本台账继续只放摘要和导航，模块细节后续优先追加到 `docs/modules/<module>/`。

## 2026-05-02 / Subagent Autonomous Dispatch 自动化派发

状态：设计中

模块设计文档：[docs/design/subagent-autonomous-dispatch.md](docs/design/subagent-autonomous-dispatch.md)

摘要：

新增 `subagent_automation_level` 配置（1/2/3），控制主代理多大程度优先使用子代理完成任务：
- 级别1：几乎所有多轮任务都派子代理（>= 2 轮就派）
- 级别2：中型任务派子代理（>= 4 轮就派）
- 级别3：大型/超大型或用户指定才派（>= 8 轮）

核心机制：
1. **任务规模预判**：基于 plan 步骤数和工具数量估算任务轮数；goal 自然语言关键词不再作为代码层加权依据
2. **模型速度感知**：`bench-model` 命令测试不同上下文大小的速度，建立速度模型
3. **动态超时**：根据输入 token 数和速度模型计算合理超时，避免"一刀切"
4. **失败分析器**：分析子代理失败根因（超时/能力缺口/任务太大等），给出建议
5. **自适应重派**：根据分析结果调整策略（提高超时/拆分任务/补充能力），不是机械重派
6. **主代理代劳防护**：级别1时主代理不能绕过子代理直接执行多轮任务

待做：
- 实现 estimate_task_complexity()
- 实现 bench-model CLI
- 实现 SubAgentFailureAnalyzer
- 实现 adaptive_retry() 和 split_task()
- 集成到 dispatch 循环

## 2026-05-02 / Acceptance Real Execution 验收真实执行

状态：设计中

模块设计文档：[docs/design/acceptance-real-execution.md](docs/design/acceptance-real-execution.md)

摘要：

验收不能只看子代理"填表"，必须有系统级真实执行验证：
1. **TestExecutionRecord**：记录测试命令的真实退出码/stdout/stderr（2026-05-08 已落地第一片：纯数据模型、序列化、输出截断和 `passed` 派生结果）
2. **TestExecutor**：执行测试命令，支持三种验证方式（command/file_check/content_check）（2026-05-08 已落地第一片：最小执行器、workspace 内文件检查、literal 内容检查）
3. **命令安全**：allowlist + 阻止高风险 shell 字符 + 超时限制（2026-05-08 已落地第一片：shell=False、基础 allowlist、危险字符拦截、超时记录）
4. **集成验收**：验收时自动执行测试，结果作为验收依据（2026-05-08 已落地第一片：通过 `AcceptanceReviewOptions(execute_tests=True)` 显式 opt-in，dry-run 会写 `test_execution.json/md` 并生成 P0 阻断 finding；默认仍不自动执行）
5. **存储**：test_execution.json 保存执行记录，支持 CLI 查看（2026-05-08 已落地：`write_test_execution_report()` 写 JSON/Markdown，JSON 是事实源，Markdown 只做展示；`subagents-tests <run_id>` 可查看，`--re-run` 才显式重跑）
6. **配置**：`acceptance_execute_tests` 默认关闭，`acceptance_test_timeout_seconds` 默认 120；`subagents-acceptance` 可用 `--execute-tests` / `--no-execute-tests` / `--test-timeout` 覆盖单次验收。

待做：
- 后续再补聚合统计、保留策略和更细的 allowlist 配置。

## 2026-05-08 / Agent Runtime Control Plane 预留边界

状态：设计中

摘要：

在 Agent Runtime Control Plane、subagent workspace、memory gate 和 tool output 外置继续推进前，需要预留四类长期扩展边界，避免第一版把结构写死：

1. **扩展备用字段和接口**：核心记录、事件和控制面 API 需要预留 `reserved` / `extensions` / `metadata` 等受控扩展槽，新增实验字段优先进入保留槽，稳定后再提升为正式字段。接口设计优先使用 Request/Options/Result bundle，避免后续靠不断加散参扩展。
2. **父子代理继承上下文**：子代理需要能继承父代理的相关能力、约束、记忆路由、上下文包、工具授权和质量契约，但继承必须可裁剪、可覆盖、可撤销，不能默认把父代理全部上下文灌入子代理。当前已落地第一版 explicit inheritance manifest，记录 inherited / overridden / dropped 项，并写入 `reports/inheritance_manifest.json`；它是审计与接管事实，不是自动展开父级上下文的开关。
3. **层级查询和接管视图**：控制面查询不应只假设“主代理查子代理”。任意上级代理（主代理或中间子代理）都可能需要查询自己的下级树；后续还要预留同级协调者、兄弟父级、takeover/rescue 代理在授权范围内查询某个 subtree 或 blocked run 的能力。第一版字段里需要保留 requester / scope / visibility / takeover_hint 这类扩展位，但事实源仍回到 task/run workspace。当前 `status --json` / 人类 `status` 和 `subagents` 看板已经能展示共享进度摘要。
4. **共享进度反馈面板**：子代理之间需要共享任务级反馈面，包含 progress、current step、blockers、messages、findings、evidence packets 和 parent rollup。当前已落地第一版 `SharedProgressPanel` 查询面，把 runtime query、rollup、blocked runs、inheritance manifest refs 和 failure handoff refs 合成上级/接管代理可读状态包；shared blackboard 可以作为协作摘要，但不是事实源；事实仍来自 verified finding、evidence packet、artifact manifest、failure handoff 和 checkpoint。
5. **Failure Handoff / 失败交接**：子代理可以失败、超时或被黑盒大输出拖垮，但不能“白死”。当黑盒工具、外部系统或未知输出可能瞬间撑爆上下文时，子代理必须尽量先保存 checkpoint / tool-output artifact / minimal failure event，再进入外置和压缩流程；如果最终仍挂掉，也要留下警告、现场锚点、避坑提示、恢复建议和最小证据，方便后续 takeover/rescue 子代理不要机械重复同一个坑。当前已落地第一版 `FailureHandoff`，失败/阻塞保存时会写 `reports/failure_handoff.json`，记录 `failure_type`、`risk_level`、`warning`、`last_safe_checkpoint_ref`、`artifact_refs`、`avoid_next_time` 和 `recommended_next_action`；它只做审计和恢复线索，`auto_rescue=false`，不自动重试或接管。当前还会写 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md` 接管前必读包，把 failure handoff、checkpoint、status report、artifact manifest、evidence/artifact refs 排成 recommended read order；Shared Progress/status/subagents 只展示 takeover packet 数量和 refs，不读取正文。
6. **Security Signal / 安全信号预留**：安全劫持、安全欺骗、prompt injection、工具权限异常、插件/供应链风险等不能靠后续口头补救，第一版需要先在 task/run 记录里留审计字段。当前已落地 `SecuritySignal` 和 `security_review_required`，只保存 `signal_type`、`severity`、`summary`、`evidence_refs`、`artifact_refs` 和 `reserved`，LocalStore metadata 暴露 signal count / types / review flag；它不拦截、不判罪、不自动改授权，只给后续 security policy/gate 接入预留事实入口。通道运行时、长期助手 等公开安全问题可作为后续研究输入，但必须先核验来源和复现场景，再沉淀成 risk pattern 或 test fixture，不能直接把未验证传闻写进策略。
7. **Principal / Conversation / Run 记忆隔离口子**：飞书、微信、Web、CLI 多入口并发时，“主代理”不能等同于全局唯一 my-agent。Agent Service 属于 `service_owner_id`，但每次聊天应有 `requester_id` / `effective_principal_id` / `conversation_id` / `root_run_id`；员工通过管理员的 my-agent 发起任务时，员工会话自己的 root run 和子代理运行树仍要独立保存 artifact、snapshot、checkpoint、failure handoff 和 tool-output refs。长期记忆先预留 namespace 与 policy 字段，不默认为每个员工开启独立长期记忆；默认只隔离 session/run 产物，后续可在显式授权后增加 `user_memory`、`team_memory`、`org_memory`、`project_memory` 等层级，并通过 visibility / retention / consent policy 控制是否写入和召回。
8. **任务记忆和员工会话记忆分离**：子代理的大量上下文属于 task/run working memory，任务完成后可以按保留策略清理，只留下 summary、verified facts、artifact refs、snapshot refs、failure handoff、acceptance/test evidence 和必要审计索引，方便反查、复盘和接管；这些临时记忆不应沉淀成员工长期记忆。员工 conversation memory 则是产品层能力：用于记住员工偏好、常用项目、授权过的工作方式、未完成事项和跨会话协作习惯。第一版不默认做员工长期记忆，只保留 `conversation_memory_policy` / `memory_namespace` / `promotion_policy` 口子；后续如要启用，应把“从任务事实提升到员工记忆”的流程做成显式 promotion：必须有来源 refs、可解释摘要、可撤销记录、保留期限和权限范围，不能把子代理临时上下文或未验证 finding 直接写成员工长期记忆。
9. **配置隔离和实验沙箱**：员工可能会让自己的 conversation 做测试、改配置、试权限或开玩笑触发危险配置；这些操作默认只能写入 conversation/run scoped config overlay，不能直接改全局配置、组织配置、管理员个人配置或服务级安全边界。配置写入需要区分 `global_config`、`tenant_config`、`project_config`、`principal_config`、`conversation_overlay`、`run_override`；越靠上的层级越需要显式授权、审计和回滚。my-agent 的定位要同时覆盖个人、个体、组织、企业甚至国家级部署，所以第一版必须把配置作用域、继承链、override 来源、effective config diff 和 rollback ref 留出来，避免某个员工会话的测试行为污染全局运行。

后续方向：

- 在 Agent Runtime Control Plane v1 里统一保留扩展槽和 bundle-first API。
- 继续把 inheritance manifest 接入执行上下文摘要和接管视图，但仍保持 audit-only；后续如需真实继承策略，应先加显式 policy/gate，而不是默认扩大子代理上下文。
- 继续把 `SharedProgressPanel` 接入 CLI/status 展示和 shared workspace facts；面板只暴露 refs 和投影摘要，不读取 artifact 正文，也不替代 task/run workspace 事实源。
- tool output externalizer 前已加入 fail-safe recovery snapshot，ToolContextReducer 也已接入 live prompt 注入前：大工具输出写 artifact 前先记录工具名、hash、大小、run/task/request id 和下一步建议；下一轮 prompt 只放 artifact 摘要、路径和 checkpoint refs，不再直接塞回完整大正文。
- takeover/rescue 第一段已接入 `takeover_readiness_ref`：action plan 的 `rescue_context_refs` 和 takeover apply 的 `evidence_paths` 会先暴露 `reports/takeover_readiness.json`，再按包里的 recommended read order 显式列出 failure handoff、checkpoint、status report、artifact manifest 或 artifact refs；第一版仍保持 refs-only，不自动读取大 artifact 正文，也不自动接管或重试。
- rescue packet / rescue action plan 第一段已落地：`ActionPlanItem` 和 `ActionApplyRecord` 带 `rescue_packet`，记录 `dedupe_key`、`issue_kinds`、`repeat_count`、`retry_policy.max_attempts`、上抛目标、人工确认建议和 `recovery_entrypoints`；`auto_retry=false`、`auto_execute=false`、`reads_artifact_bodies=false` 是当前边界。它只做计划和审计，不自动 rescue。
- 后续做 Security Gate 时优先基于 `SecuritySignal` 扩展：先补外部案例调研、风险分类、detector fixture 和 audit report，再决定是否接入权限收窄、工具隔离或人工确认流程。
- 后续接外部 IM / 企业用户时，先实现 `RuntimeIdentity` / `ConversationScope` 这类轻量身份包，把 service owner、effective principal、conversation、root run 和 memory namespace 写进 artifact/snapshot/control-plane metadata；员工长期记忆是否启用保持 policy 决策，不和第一版 artifact 隔离绑死。
- 后续做员工记忆时，先实现 task/run working memory 的清理与保留包，再实现 conversation memory 的显式 promotion 队列；主代理可以读取员工授权范围内的 conversation summary / preference card / open tasks，但不能默认读取员工私有 run artifacts 或管理员个人记忆。
- 后续做配置系统时，先实现 scoped config overlay 和 effective config viewer：员工会话里的配置测试默认落到 `conversation_overlay` / `run_override`，只有通过明确的 admin approval / policy gate 才能 promote 到 project、tenant 或 global 层；所有 promote 都要写 audit event、diff、rollback ref 和发起人的 effective principal。

## 2026-05-08 / Memory-resume fail-safe checkpoint refs
状态：已落地

摘要：
- `memory-resume --from-compact` 现在会把工具输出外置前写入的 metadata-only fail-safe checkpoint 纳入恢复入口。
- checkpoint 来自 compact restore refs 指向的 hook JSONL；恢复包只展示 path、line_no、snapshot_id、工具名、hash、size、next_actions 等摘要，不读取 artifact 正文。
- `compact_resume_handoff` 和 Compact Resume Context 新增 `Fail Safe Checkpoints` 小节，接管者先读 checkpoint 摘要，再决定是否显式读取 artifact。
- 这条边界继续遵守：checkpoint 不是 compact，summary 不是 verified fact，artifact ref 不是任意文件路径，完整 artifact body 不是默认 prompt 内容。

## 2026-05-08 artifact explicit read command/tool
状态：已落地

摘要：
- 新增 `memory-artifact-read` 命令和 `read_artifact` 工具，作为从 artifact ref 到正文内容的显式读取入口。
- 读取前必须命中 `memory_archive/artifacts/tool_outputs/index.jsonl`；artifact path、sha256、call_id 只是登记记录的查找 key，不等于任意文件路径读取权限。
- reader 会校验登记路径仍在 `memory_archive/artifacts/tool_outputs/` 下，读取 artifact JSON 后校验正文 sha256，并支持 `offset` / `max_chars` 切片；`max_chars=0` 才读取完整正文。
- 这一步把 “artifact_ref != arbitrary file path” 和 “完整大正文必须显式读取” 从设计边界落到 CLI/tool 层。
## 2026-05-08 status/board takeover view
状态：已落地
摘要：
- `status --json` 的 `subagents.shared_progress` 现在包含 `takeover_entries`，按 run 展示 `failure_handoff_ref`、`takeover_readiness_ref` 和 `recommended_read_order`。
- 人类 `status` 和 `subagents` 看板新增 `Takeover View` 小节，接管者可以先看到具体 run、当前 step、handoff/readiness refs 和前几条推荐读序。
- 展示层只读取 `takeover_readiness.json` 这个恢复索引里的 recommended read order，不读取或内联 artifact 正文；artifact body 仍必须后续通过显式 artifact 读取入口访问。
## 2026-05-08 Parent Acceptance Controller v1 dry-run

状态：部分落地
摘要：
- 2026-05-08 新增父级验收控制器第一片：父代理可通过 `plan_parent_acceptance(run_id)` 读取子代理 `output.json`、`test_execution.json` 和 handoff refs，生成 refs-only 的 dry-run 决策。
- 决策当前覆盖 `execute_tests`、`inspect_only`、`request_human` 和 `rescue`：缺真实测试报告但 tests 安全时建议执行；命令预检高风险时要求人工确认；真实测试已通过时只需继续普通验收；失败/阻塞任务走 rescue。
- 2026-05-08 新增 `subagents-acceptance-plan <run_id>` CLI，可用人类视图或 `--json` 展示父级 dry-run 决策，仍只展示 refs/summary，不读取 artifact 正文。
- 2026-05-08 `status --json`、人类 `status` 和 `subagents` 看板新增 `Acceptance Plan` 摘要：对待验收、失败或阻塞 run 展示父级 dry-run 决策，继续保持 refs-only。
- 2026-05-08 新增 `subagents-acceptance-plan <run_id> --write` 和 `write_parent_acceptance_decision(run_id)`，可把 dry-run 决策写入 `reports/parent_acceptance_decision.json` 审计文件；文件声明 `dry_run=true`、`refs_only=true`，不保存 artifact 正文。
- 2026-05-08 新增显式 `--apply` 第一片：只有 `inspect_only` 会桥接到既有 acceptance apply；`execute_tests`、`request_human`、`rescue` 只写入 `reports/parent_acceptance_apply.json` 拦截审计，不自动跑 tests、不自动 rescue、不绕过人工确认。
- 2026-05-08 新增 `--next-action` 第一片：父/上级代理可读取当前 plan 和 apply 审计 refs，得到 `run_tests`、`request_human_confirmation`、`plan_rescue` 或 `apply_acceptance` 建议；它只返回建议命令和 refs，不执行建议、不写状态。
- 当前仍没有自动策略配置；父/上级代理后续可以读取 `parent_acceptance_decision.json`、`parent_acceptance_apply.json` 和 next-action 结果，再按策略显式执行测试、请求人工或进入救援。

## 2026-05-08 Parent Acceptance Auto Policy v1 草案

状态：设计中

模块结构文档：[docs/modules/subagent/04-structure.md](docs/modules/subagent/04-structure.md)

摘要：
- Auto Policy v1 只定义父级验收 next-action 的自动化策略草案和审计 schema，不改变当前运行行为；第一版必须保持 dry-run，不执行 tests、不 apply acceptance、不 rescue、不修改 task 状态。
- 2026-05-08 已落地 dry-run 实现：`plan_parent_acceptance_auto_policy(run_id)` 和 `subagents-acceptance-plan --auto-policy` 会写入 `reports/parent_acceptance_auto_policy.json`；当前只生成 `allow/blocked`、`would_execute` 和 `executed=false` 审计。
- 建议配置字段先按“全局默认 + 能力路由细则”拆分：如果后续实现成用户可见的主验收策略，必须同步 `agent_py_agent/config/agent_config.yaml` 与 `AgentConfig`；如果只是 capability/subagent 路由内部的授权、次数、allowlist 或层级参数，应进入 `agent_py_agent/config/capability_config.yaml` 与 `capability_config.py`，不要塞进主配置。
- 主配置草案建议包含 `acceptance_auto_policy_enabled=false`、`acceptance_auto_policy_mode=dry_run`、`acceptance_auto_policy_auto_execute=false`、`acceptance_auto_policy_action_allowlist=["run_tests"]`、`acceptance_auto_policy_write_audit=true`、`acceptance_auto_policy_max_actions_per_run=1`。其中 allowlist 第一版默认只建议 `run_tests`，但 `auto_execute=false` 使它也只能生成 would-run 审计。
- 第一版动作边界：`run_tests` 只能记录“如果允许会执行哪个测试入口”；`request_human_confirmation` 只能生成确认请求意图；`plan_rescue` 只能引用 takeover/readiness/rescue packet；`apply_acceptance` 只能说明仍需显式 apply gate。任何 `requires_human=true`、安全信号、未知 action、缺少决策 refs 或越权配置 scope 都必须阻断自动执行。
- 审计 JSON 建议写入 `reports/parent_acceptance_auto_policy.json`，作为机器事实源；Markdown/CLI 只展示摘要。字段至少包含 `schema_version`、`record_type`、`created_at`、`run_id`、`root_run_id`、`policy`、`decision_refs`、`next_action`、`allowed_by_policy`、`dry_run`、`auto_execute`、`would_execute`、`executed=false`、`blocked_reason`、`requires_human`、`safety_signals`、`rescue_refs`、`runtime_identity`、`config_scope` 和 `reserved`。
- 安全预留：后续接入 `requires_human`、rescue、安全信号、Security Gate、租户/员工 conversation 配置隔离时，Auto Policy 只能读取这些事实和 policy 结果，不能绕过它们；tenant/principal/conversation/run scope 的 effective config diff 与 rollback ref 要进入审计，防止员工会话测试污染全局策略。

## 2026-05-09 Parent Acceptance Auto Policy dispatch/watch dry-run 接入

状态：已落地

摘要：
- `subagents-dispatch` 现在会在 acceptance dispatch record 上写入 `parent_acceptance_policy_ref`、decision、action、would_execute、executed 等 refs-only 摘要，并生成 run-local `reports/parent_acceptance_auto_policy.json`。
- `SUBAGENT_DISPATCH.md` 展示同一组摘要，方便父代理或人类先看报告再沿 ref 进入具体 run 审计。
- `subagents-dispatch --watch` 不在 watch 层重新运行 policy；watch record 只引用本轮 dispatch JSON/Markdown，避免 watch 循环把 `would_execute=true` 误解成自动执行。
- 当前边界不变：不执行 tests、不 apply acceptance、不 rescue、不修改 task 状态；`executed=false` 仍是硬约束。

## 2026-05-09 Parent Acceptance Auto Policy 半自动计划字段

状态：已落地

摘要：
- `parent_acceptance_auto_policy.json` 的 policy 现在包含 `execution_mode=manual_only`、`automatic_execution_allowed=false` 和 `recommended_command`。
- 这一步只把“如果要继续，应该手动跑哪条命令”写成机器可读字段；不会启动进程、不会跑 tests、不会 apply、不会 rescue，也不会改 task/run 状态。
- `reserved.semi_auto_plan` 记录 stage、manual confirmation 和 `dry_run_only/no_process_execution/no_task_state_mutation` 等硬边界，为后续受控调度器读取做准备。

## 2026-05-09 Parent Acceptance Auto Policy dispatch 半自动摘要

状态：已落地

摘要：
- `subagents-dispatch` 的 acceptance record 现在透传 policy 的 `execution_mode`、`automatic_execution_allowed` 和 `recommended_command`。
- JSON report 和 `SUBAGENT_DISPATCH.md` 都能直接看到 manual-only 边界，避免 watch/调度层只看 `would_execute=true` 就误判为可以自动执行。
- 行为边界不变：dispatch/watch 仍只写审计和引用，不启动 recommended command，不 apply，不 rescue，不改 task 状态。

## 2026-05-09 Parent Acceptance Auto Policy preflight 审计

状态：已落地

摘要：
- Auto Policy 现在输出 `preflight_status`、`ready_for_manual_execution`、`ready_for_automatic_execution`、`preflight_checks` 和 `preflight_blockers`。
- 对 allowlisted `run_tests`，preflight 可以是 `manual_ready`，但 `ready_for_automatic_execution=false` 且 blocker 保留 `automatic_execution_disabled`。
- 对需要人工确认、状态修改或不在 allowlist 的动作，preflight 会给出明确 blocker；这只是审计和未来调度输入，不执行命令。

## 2026-05-09 Parent Acceptance Auto Policy dispatch preflight 摘要

状态：已落地

摘要：
- `subagents-dispatch` 的 acceptance record 现在透传 `preflight_status`、`ready_for_automatic_execution` 和 `preflight_blockers`。
- `SUBAGENT_DISPATCH.md` 展示同一组 preflight 摘要，让 watch/调度层直接看到“manual_ready 仍不等于自动放行”。
- 这仍是报告层字段，不触发 tests/apply/rescue，也不会根据 preflight 自动修改 task/run 状态。

## 2026-05-09 Parent Acceptance Auto Execution dry-run facade

状态：已落地

摘要：
- 新增 `ParentAcceptanceAutoExecutionRequest` / `ParentAcceptanceAutoExecutionResult`，把 future executor 入口统一成 bundle。
- 新增 `plan_parent_acceptance_auto_execution(run_id)` 和 `subagents-acceptance-plan --auto-execution`，写入 `reports/parent_acceptance_auto_execution.json`。
- 第一版固定 hard guard：`execution_allowed=false`、`guard_status=blocked`、`executed=false`、`mutates_task_state=false`，只展示 recommended command、policy ref、blockers 和 safety boundaries。
- `subagents-dispatch` 的 acceptance record 和 `SUBAGENT_DISPATCH.md` 透传 `parent_acceptance_auto_execution_*` 摘要；watch 仍只沿 dispatch report/Markdown 查看，不执行命令。

## 2026-05-11 Stage7 shopping E2E R2

状态：已落地第一轮修复，仍需 R3 复测

摘要：
- 购物网站真实 E2E R2 证明 root 单入口链路能创建 4 个 coordinator 和多个 worker/leaf，但还没有交付完整购物流程。
- 两个架构口子已修：child spec goal 自己写出的产物路径会进入 write-root 候选；`write_file` 这类大工具 payload 在 assistant round 和 tool-call record 两处都会摘要，不再反复进入 prompt。
- 设计边界不变：coordinator/researcher/tester/acceptor 看到产物路径也不能拿最终产物写权限；只有 worker/writer/leaf_worker 且有明确写入意图时才继承候选根。
- 下一轮 R3 必须补强父级静态站点验收：缺失页面、`${...}` 占位符、失效链接/图片和不可达按钮都应阻断验收。

## 2026-05-11 Stage7 shopping E2E R3 stabilization

状态：已落地调度稳定性修复，待 R4 复测

摘要：
- coordinator 显式 allowed_tools 现在会合并内置 coordinator 工具包，确保协调节点不会因为模型漏写 `schedule_child_subagents` / `dispatch_subagents` 而失去继续派工能力。
- `dispatch_subagents` 新增 `run_ids` / `include_run_ids` 精确推进入口；runner 候选会先按这些 id 过滤并按给定顺序执行，解决 root 想先跑 auth/catalog 但队列跑偏的问题。
- runner-context 进度 payload 的建议工具调用会把 unfinished direct child ids 放进 `run_ids`，让父节点继续调度时不必靠自然语言记忆。
- live tool context 的历史记录改用 `[tool-record ...]` / `[tool-output-record ...]` 中性标签，并把 dict payload 渲染成摘要行，降低模型复制历史记录造成 parse error 的概率。
- 下一轮 R4 要验证 root 是否能按指定孩子顺序推进，并继续把购物网站完整流程验收到 register/login/product/cart/checkout/order success。

## 2026-05-11 Stage7 shopping E2E R4 URL path guard

状态：已落地，待 R5 复测

摘要：
- R4 真实测试确认 root 已能创建 4 个 coordinator，auth 分支能创建 leaf_worker 并写出 `auth.html`。
- 新问题：catalog 分支在 goal 里引用 `https://picsum.photos/...` 和 `https://images.unsplash.com/...` 时，写入根提取器把 URL 片段误当成本地路径，scheduler 因越界写入根拒绝创建 worker。
- 修复：`_extract_write_dirs()` 增加 URL span 过滤；Windows 盘符正则不再从单词中间匹配，避免 `https://` 的 `s:/` 误判。
- 设计边界：URL 仍然可以作为页面图片/src 内容出现在 worker 任务描述里，但不会进入 `allowed_write_roots`；网络工具/抓取授权后续仍走 capability/shell gateway，不走写入根。
- 下一步：R5 复测 catalog worker 是否能正常创建，并补 quality 阶段依赖，避免过早验收未完成产物。

## 2026-05-11 Stage7 shopping E2E R5 duplicate/artifact guard

状态：已落地，待 R6 复测

摘要：
- R5 真实测试中 auth leaf 写出了 deliverables 产物，但 root 重复创建 checkout/quality 同域 coordinator，说明同父级需要领域去重。
- 当时新增过同父级 coordinator-domain guard；后续协作简化已删除这类 scheduler 硬门，重复/范围由父级根据 tree 状态判断。
- `artifact_registry.py` 现在把 task 的 `allowed_write_roots` 纳入 artifact manifest 安全解析根；被授权写出的业务产物能记录 exists/size/hash，越界路径仍然 blocked 且不读取正文。
- 下一步：R6 必须验证重复 coordinator 被阻断、deliverables 产物在 takeover manifest 中可解析，并继续推进 producer/quality 阶段依赖。

## 2026-05-11 Stage7 shopping E2E R6 multi-run instruction guard

状态：已落地，待 R7 复测

摘要：
- R6 真实测试中 root 没有重复创建 checkout/quality，说明同域去重生效；但同轮 dispatch auth/catalog/cart 时，auth 专属 `runner_instruction` 被广播给所有 runner，导致 cart 分支创建 auth leaf。
- `dispatch_runner_batches.py` 新增多 runner 指令保护：一个批次包含多个 pending runner 且有共享 `runner_instruction` 时，清空该共享指令并写入 `ignore_multi_runner_instruction` 调度记录。
- 单个 run_id dispatch 仍保留专属 `runner_instruction`，用于父节点对某一个孩子补充上下文。
- 工具说明和 coordinator prompt 已同步：多个孩子一起跑时不要写子任务专属 runner_instruction；需要专属补充就拆成单 run_id dispatch。
- 下一步：R7 复测身份不串线，再补 producer/quality 阶段依赖和完整购物站静态验收。

## 2026-05-11 Stage7 shopping E2E R7 scoped run-id guard

状态：已落地，待 R8 复测

摘要：
- R7 真实测试确认多 runner 指令串线已修正；auth 分支完成，catalog 分支开始产出，cart 分支暴露 child run_id 抄错后空转的问题。
- `dispatch_runner_batches.py` 现在会在 runner 候选执行前预检显式 `include_run_ids`。缺失、越界或不属于当前 parent/root scope 的 id 会生成 `runner_selection/invalid_run_ids` 阻断记录。
- 阻断记录会列出 `invalid_run_ids`、`valid_scope_run_ids`，并在错误 id 和唯一可见 child 共享短后缀时输出 `possible_corrections`。系统不自动纠正，避免隐式跑错任务。
- 同父级重复领域去重继续保留 checkout/quality 这类真实重复保护，但现在过滤 generated id 片段和泛化编号词，避免误挡 `grand-1` / `grand-2` 这类恢复树 checker sibling。
- 下一步：R8 复测 coordinator 是否能根据阻断记录重试正确 child id，再继续做 producer/quality 阶段依赖和静态购物流程验收。

## 2026-05-11 Stage7 shopping E2E R8 path-drift and recovery visibility

状态：已落地，待 R9 复测

摘要：
- R8 真实测试确认 auth/catalog 可以产出页面，但 cart coordinator 把父级 `/build` 目录漂移成 sibling `/stage7_r8_build`，说明“child spec 自己写路径就授权”还缺少父级权威根锚定。
- 当时新增过 child write-root drift guard；后续已删除 scheduler 范围硬门，路径错误由工具执行/通用路径边界返回给模型处理。
- `read_artifact` 仍不允许读取任意文件；当模型只抄错 artifact path 前缀但文件名在 index 中唯一时，reader 会修复到登记记录，然后继续做 trusted tool-output 目录检查和 sha256 校验。
- `dispatch_subagents` 顶层 payload 新增 `runner_selection_recovery`，`subagent_board` 顶层新增 `actionable_run_ids` 并截断长 goal，降低真实 runner 在大报告/外置摘要里看不到关键 id 的概率。
- 下一步：R9 用干净 runtime/deliverables 复测 cart 分支是否能被 drift guard 纠回 `/build`，再继续做 producer/quality 阶段依赖和完整购物站静态验收。

## 2026-05-18 Natural-language fact source ban

状态：已落地，持续守卫

摘要：
- 新增架构铁律：代码不得依赖普通自然语言文本作为机器事实来源。自然语言可以给 LLM/人理解，但路由、权限、验收、恢复、派工、产物归属和状态流转必须读结构化字段、状态码、refs、schema、工具记录或文件系统事实。
- 已迁移子代理关键旧兜底：workflow route、input/output refs、写入根、repair identity、QA 硬要求、domain/scope、leaf target、root/coordinator seed 都不再从 goal/prompt/summary 的普通句子里抽硬规则。
- 新增/强化 `test_code_does_not_use_plain_language_as_machine_facts`，把已删除的旧入口列入架构守卫，后续同类回归要先加结构化字段和测试，不能再补关键词表。
- 下一步：主代理复杂任务 E2E 继续按这条铁律压测；如果真实模型说法变化导致失败，优先补 protocol/schema/refs，不补自然语言关键词。

## 2026-05-21 Main-agent contract substrate split for startup and recovery

状态：已落地，已验证

摘要：
- `main_agent_task_execution_files.py` 和 `main_agent_real_task_execution_files.py` 里原来重复的 artifact/path/bootstrap 组装逻辑，已经抽到共享模块 `main_agent_execution_contract_artifacts.py`。主代理 task/real-task 现在共用一套 artifact path contract、checkpoint path contract、bootstrap target 和 startup action 生成逻辑。
- `delivery_contract_prompting.py` 继续保持“主渲染入口”职责，但 recovery continuation 那一半已经拆到 `delivery_contract_prompting_recovery.py`。这样 bootstrap / artifact quality / recovery continuation 三层职责分开，后面继续改 closeout/recovery 时不会再把主 prompting 文件堆胖。
- 这次拆分没有引入专项合同；所有新增 helper 都只读结构化 artifact、staging_contract、recovery finding 和 reconciliation groups。没有从任务文案或 stdout 摘要里反推机器事实。
- 验证链路：focused tests 覆盖了 staging、delivery prompting、bootstrap guard、delivery closeout；`ruff`、`check_doc_sync`、`check_code_size --baseline`、`git diff --check` 也一起过了。
- 结果：被这次 touched 的 execution-files soft 项已消掉，`delivery_contract_prompting.py` 的 touched-file high-risk 也通过模块拆分收掉。仓库仍有历史 `main_agent_delivery_closeout.py` 软项，后续继续在 closeout/recovery 这层做通用拆分。

## 2026-05-21 Main-agent phases A/B/C completed

状态：已落地，已验证

摘要：
- 阶段 A（统一状态机）完成：新增 `state_machine_transitions.py`，把允许状态流向、迁移条件和非法状态序列检查做成共享机器合同。`trace_replay.py` 现在会把非法 `state_snapshot` 序列标成 `STATE_TRANSITION_SEQUENCE_CONFLICT`。
- 阶段 B（工具与错误合同）完成：新增 `tool_manifest_contract.py`，统一输出 `visible_tools`、`executable_tools`、`permission_mode`、`failure_taxonomy` 和 `failure_contracts`。主代理 context bundle 和 `list_tools` 已改成共用这份 payload。
- 阶段 C（Replay 正式化）完成：新增 declarative replay specs `agent_py_agent/tests/replay/specs/*.json`、共享 runner `replay_case_runner.py` 和 gate 脚本 `scripts/check_replay_contracts.py`。Replay 不再只是零散单测，而是可以批量执行、JSON 汇总和单独验收的固定资产。
- 验证链路：focused tests 覆盖状态机迁移、tool manifest、context bundle、list_tools、replay trace、replay case runner 和 replay gate；随后全量 fast suite、`ruff check agent_py_agent scripts`、`check_doc_sync`、`check_code_size --mode strict --baseline`、`git diff --check` 也全部通过。
- 下一步：继续拆 `main_agent_delivery_closeout.py` 的历史 soft/high-risk，把 recovery action 组装和 closeout 判定进一步拆成共享模块；真实复杂任务仍放在后面统一验收，不回到边跑边改。

## 2026-05-22 Main-complex canary HTML commit gate

状态：已落地，web-only 真 LLM 复验通过

摘要：
- `main-complex` 真实 LLM suite 里 5 个 case 通过，`main_direct_web_app` 暴露一个通用提交边界问题：长 HTML 被 `file_write_session` 恢复后，`finish` 只校验 JSON，不校验 HTML 完整性。
- 修复落在通用工具提交边界：`.html/.htm` 在 `file_write_session finish` 前复用 `artifact_integrity`，结构损坏、`href="#"` 和缺失 hash target 会返回 `ARTIFACT_INTEGRITY_FAILED`，不会把坏页面提交为最终事实。
- 第二轮 `main-complex` 复验确认坏页面不再提交，但暴露默认流式写入边界过窄：普通 `write_file` 在约 4K 时过早切入 staged writer，模型混用直接写入和 session 写入后被 open-session guard 阻断。
- 修复落在通用传输策略：默认 streaming inline write abort 上限放宽到 32K，显式小阈值仍可用于测试/策略覆盖。常见完整 HTML/CSS/JS 写入能自然收尾，失控长流仍会进入 staged writer 恢复。
- 新增离线回归覆盖坏 HTML 不能提交、目标文件不落地、session reset 后可重写、外置工具结果仍保留 `reset_tool_call` 这类机器动作字段、默认写入流能越过旧 4K 边界；同时保留 JSON checkpoint 预提交校验。
- 长期助手 对照已用隔离 `长期助手_HOME`/venv 跑同题 Web app，产物通过本仓库静态验收；它本轮成功主要来自一次性完整写入和主动搜索检查，`write_file` 对 HTML 仍是 lint skipped，所以本仓库选择把校验放在工具提交边界。
- 真 LLM 复验：`20260522-main-web-only-04` 只跑 `health` + `main_direct_web_app`，`LIVE_LAB_PASS`，Web 任务 245.40 秒完成并生成 `index.html`、`styles.css`、`app.js`、`README.md`。
- 验证链路：`test_file_write_session.py`、`test_artifact_integrity.py`、`test_tool_stream_boundary.py`、`test_tool_loop_write_sessions.py`、目标 `ruff` 和 `check_code_size --mode strict --baseline` 已通过，`high-risk=0`、`soft=0`。

## 2026-05-22 Complex task entry contract and data package gate

状态：已落地，待真实 4 并行复验

摘要：
- 4 个普通并行复杂任务暴露的共性不是某个任务不会写，而是“普通 run 没有结构化交付合同时，系统只能拦空转，不能按产物合同驱动修到合格”。
- 受控主代理任务 runner 现在把“一次普通自然语言 prompt”和 `delivery_contract.json` 分离固定：命令层只给被测主代理一个 prompt，机器事实通过独立合同文件进入运行参数和 closeout。
- 新增通用 `data_analysis_package` case，不写销售专项 gate，而是用 `source_data.json`、`analysis.xlsx`、`report.pdf`、`dashboard.html` 四种产物类型和 `collection_contract` 约束 row count、必填列、staging builder、HTML 完整性。
- 新增回归覆盖“只有几百行却声称至少一千行”：即使 workbook 已存在，只要 `source_data.json.rows` 少于 1000，artifact acceptance 必须返回 `COLLECTION_TOO_FEW_ITEMS`。
- 参考项目取舍：学习 长期助手/通道运行时/会话运行时 的 mandatory gate 思路，把约束挂在 runner、tool gateway、artifact validator、delivery closeout，而不是解析 prompt 或最终回复里的普通自然语言。
- 下一步：跑离线门控链路后，先单个复杂任务真实复验，再用 4 个主代理并行跑 GitHub、论文、购物站、数据分析包。

## 2026-05-25 Long-running background MainAgent thread runtime

状态：已落地，已做离线验收

摘要：
- 新增 `agent_py_agent/agent/conversation/`，把长期主代理线程拆成 `ConversationThread`、`MessageLogEntry`、`ChannelBinding`、`ThreadTaskLink`、`ProgressPolicy` 五类机器事实；它们只描述会话、渠道、任务和定时策略，不承载任何专项任务模板。
- `ConversationStore` 用本地 JSON/JSONL 账本保存 thread、消息、任务绑定、渠道绑定和 progress policy。换进程重新创建 store 后，可以恢复同一用户在 fake Feishu / fake WeChat / internal 之间的上下文；新渠道默认不复用最近 thread，只有消息入口明确继续上下文时才复用，避免同用户新任务串线。
- `BackgroundMainAgentRuntime` 会装载会话消息、任务绑定和 `inspect_agent_tree` 快照，再调用同一个 `SimpleAgent.run()`。scheduler/watch 只负责叫醒，不替 LLM 判断工作内容。
- `BackgroundMainAgentScheduler.tick()` 只查到期 `ProgressPolicy` 并唤醒 runtime；发送路径先用 `FakeChannelHub` 验证 internal/fake Feishu/fake WeChat，后续真实飞书/微信 adapter 只需要接入同一 route 语义。CLI 已提供 `background-main-agent message/bind-task/tick/service`，用于本地模拟渠道消息、绑定任务、单次 tick 和前台循环 tick。
- 参考项目取舍：长期助手 的 SessionSource/cron due-job、通道运行时 的 watch 控制面、OpenAI Agents 的 resume tracker 共同点是“恢复和投递靠结构化会话事实，不靠模型记忆普通文本”。本轮实现吸收这条，不复制它们的业务模板。
- 验证链路：`test_conversation_store.py` 覆盖跨渠道和重启恢复，`test_background_main_agent_runtime.py` 覆盖定时唤醒和重启后继续汇报，`test_fake_channel_resume.py` 覆盖 fake Feishu/fake WeChat 同用户恢复同一 thread，`test_background_main_agent_cli.py` 覆盖本地 CLI 入口；目标 `ruff` 已通过。

## 2026-05-25 Descendant event wake queue for background MainAgent

状态：已落地，已做离线验收

摘要：
- `ConversationStore` 新增 `ObservationEvent` 和 `WakeSignal` 账本。子代理、孙代理或外部通道可以写入结构化观察事实；紧急事件进入 durable wake queue，普通待复核事件等下一次 tick 交给主代理。
- 新增 `raise_observation` / `raise_main_event` 编排工具，并把它们加入默认子代理/层级/工作流工具授权。它们只写结构化事件和 wake signal，不执行调度、不替模型做业务判断。
- `BackgroundMainAgentScheduler.tick()` 现在按顺序处理 urgent wake、未处理 observation、到期 progress policy。`BackgroundMainAgentRuntime` 会把 `Recent Observations` 和 `Pending Wake Signals` 注入上下文，由同一个主代理 LLM 判断是否二次分析、调度或汇报。
- `background-main-agent observe` 提供本地模拟入口；`service` 的 sleep 改成短轮询 wake queue，紧急事件不必等完整 interval。
- 参考项目取舍：学习 长期助手 cron wake gate 的“先判定是否叫醒，再让 agent 处理”结构；没有增加 API/告警/安全专项模板，也没有把普通观察变成硬阻断。
- 验证链路：`test_conversation_wake_events.py` 覆盖 observation 持久化、wake 幂等、urgent 立即唤醒、普通待复核 tick 处理和工具反查 task thread；`test_background_main_agent_cli.py` 覆盖 observe CLI 和 service wake interrupt。

## 2026-05-25 Subagent collaboration control-plane affordance

状态：已落地，已做 focused 验收

摘要：
- 真实 MiniMax 小场景显示：协作工具虽然已经授权给子代理，但 runner prompt 没有把 `open_case/request_collaboration/submit_evidence/update_collaboration_request/reroute_collaboration_request/case_status` 作为通用动作入口讲清楚，模型会退回读写报告而不是进入协作账本。
- `runner_prompts.py` 现在只在当前 `allowed_tools` 包含协作工具时追加“协作控制面”段落。段落按实际授权工具解释何时开 case、何时请求补证据、如何提交 refs-first 证据、如何更新请求、何时结构化改派。
- 后续已调整措辞：如果 runner 同时有 `open_case` 和 `request_collaboration`，提示只说明“需要其他代理回应/补证据时，再创建 request”。这属于软动作建议，不是 closeout/acceptance 卡点；空 case 可以作为日志事实存在。
- 新增点名协作请求注入：`CollaborationStore.pending_requests_for_agent()` 会按结构化 `agent_id/agent_name/agent_role/target_agent_ids` 找出尚未提交证据、未完成/未拒绝的 request；它兼容系统给展示名追加的数字后缀，例如 `Agent-B-2` 可响应发给 `Agent-B` 的请求，也支持发给结构化 role 的请求，例如 `target_agent_ids=["agent-b"]`。`SubAgentManager` 把这些 refs 写入 `context_bundle.collaboration.targeted_requests`，runner prompt 明确要求响应者复用已有 case/request，按 `case_status -> submit_evidence -> update_collaboration_request` 收口，避免 B 代理再开第二个 case。
- `dispatch_subagents` 的 runner candidate 现在会优先包含“被新协作请求点名”的空闲/已完成代理，并且在显式 `include_run_ids` 时仍只在当前 scoped 范围内处理。这样 A 后创建 request、B 先前已 DONE/VERIFIED 的时序也能自然进入下一轮 B responder，而不是直接派 repair 代理或让 B 永远不知道 request。
- `dispatch_subagents(router=None, execute_runners=True)` 会跳过 runner 后置 capability route+rerun 闭环，但保留 runner 本身执行结果；显式传入 CapabilityRouter 时才尝试把新 `capability_request` 自动授权并重跑一轮。
- 这不是专项模板：不解析用户 prompt，不绑定 GitHub/日志/API/论文等业务类型，也不要求用户写 case 字段。它只把已有结构化工具权限变成真实模型可理解、可执行的控制面入口。
- 验证链路：`test_subagent_prompt_contract.py` 增加 prompt contract 回归，`test_collaboration_control_plane.py` 覆盖 targeted request 查询和 execution context 注入，`test_local_collaboration_subagent_integration.py` 继续覆盖 fake 子代理协作闭环。

## 2026-05-25 Explicit orchestration root control-plane boundary

状态：已落地，focused 验收通过；真实 MiniMax 场景已复现旧问题并保留现场

摘要：
- 真实协作调查场景暴露了一个通用边界缺口：入口已经物化 `orchestration_contract.v1`，root 也创建并调度了子代理，但当第一批子代理验收不顺时，root 会改成自己直接读取已委派的源文件补洞。
- 这不是 IP 专项问题，而是“显式协作任务里 root 是否仍保持控制面角色”的问题。修复后，`orchestration_contract.requires_orchestration=true` 会被视为 refs-only 委托意图，入口运行参数同时设置 `subagent_delegation=True` 和 `refs_only=True`。
- root 已经有当前轮 child runs 且 acceptor 未完成时，`orchestration_body_read_guard.py` 会继续阻断 root 读取普通 source/product 正文；root 仍可读 `subagent_board`、`dispatch_subagents` artifact、task/output/status 这类控制面元数据，然后继续调度、创建 repair worker，或明确报告子代理阻塞原因。
- 这条规则只读结构化 `task_attributes.orchestration_contract`，不扫描 prompt 里的 IP、hostname、日志、论文等业务内容，也不限制 leaf worker 读取自己负责的资料。
- 同一真实场景还暴露模型把 `dispatch_subagents` 的 `run_ids` 写成 `subagent_ids`、`target_subagent_ids` 或 `items:[{"run_id":...}]`，导致工具只 dry-run、子代理一直停在 PLANNING。修复后 `dispatch_subagents` 接受 `run_ids/include_run_ids/subagent_ids/target_subagent_ids/target_run_ids/agent_ids/items[].run_id` 作为同一类显式目标；顶层显式给目标 ID 时默认 `apply=true`、`execute_runners=true`，显式 `apply=false` 仍保留 dry-run。
- 验证链路：新增 `test_top_level_orchestration_contract_blocks_source_body_after_children_exist`、`test_top_level_subagent_ids_alias_runs_and_executes` 和 `test_top_level_items_run_id_alias_runs_and_executes`，并复跑显式协作入口物化测试；目标 `ruff` 已通过。真实现场保存在 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-093000`、`/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-093500` 和 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-094000`。

## 2026-05-25 Evidence-only subagent acceptance for investigation work

状态：已落地，focused 验收通过；待真实 MiniMax 复验

摘要：
- 真实协作调查场景 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-094500` 里，部分 leaf worker 已经读取资料源、给出命中/未命中事实和 evidence ref，但父级验收把 `test_execution.json` 的 `total=0 failed=0` 当成失败，导致任务停在 `AWAITING_ACCEPTANCE` 并诱导 root 创建修复子代理。
- 这不是 IP 专项问题，而是通用“调查/查询/监控类子代理可能只产出证据事实，不一定产出新文件 artifact”的问题。修复后，空可执行测试报告只有在缺少可追踪证据时才会转 rescue；如果 task/output/evidence packet 指向非内部的 `evidence_refs` 或 `artifact_refs`，父级会进入 `inspect_only -> apply_acceptance` 轨道。
- runner 解析层现在兼容顶层 `evidence_refs/artifact_refs`：模型没有把 refs 包进 `evidence_packets` 时，系统会补一个 refs-only evidence packet，避免证据在 parser 到 acceptance 之间丢失。
- 这条规则只读结构化 refs 和 run-private 路径边界，不扫描 prompt 里的 IP、hostname、日志、GitHub、论文等业务自然语言；内部 `output.json`、reports、agent-run 目录仍不能冒充外部证据。
- 验证链路：一次性复现脚本先确认旧逻辑会丢顶层 refs；修复后复跑 `test_parent_acceptance_controller.py`、`test_subagent_parsing.py`、`test_result_processors_edges.py`、`test_orchestration_tools.py` 和 `test_orchestration_dispatch_completion_gate.py`。

## 2026-05-25 Explicit read refs become read-only subagent roots

状态：已落地，focused 验收通过；待真实 MiniMax 复验

摘要：
- 真实协作调查场景 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-101000` 暴露的卡点不是业务判断，而是路径授权链断开：root 创建了 11 个子代理，source 子代理都拿到了明确文件路径，但 runner 的 path gate 只看到主工作区和写入根，导致读取 `/Users/example/my_agent/live-agent-runs/.../inputs/sources/*.txt` 时统一 `PATH_WORKSPACE_ESCAPE_BLOCKED`。
- `create_subagents` 现在会把 item `goal` 里的显式文件路径 token 补进 `context_manifest.hint_read_paths`。这是路径形态提取，不解析用户任务语义；模型如果已经传了 `required_read_paths/source_refs/input_files`，仍优先保留这些结构化字段作为硬输入依赖。
- runner context 会把 `context_manifest.required_read_paths` 和 `hint_read_paths` 写成 `write_boundary.allowed_read_roots`，供工具 path gate 使用。这个 root 只扩大读取边界，不进入 `allowed_write_roots/product_write_roots`，不会让子代理写入用户输入目录。
- 这条规则适用于任意显式资料文件，不绑定 IP、日志、论文、GitHub 或某种文件格式；协议、产物类型和业务实体仍保持开放世界。
- 验证链路：`ruff check` 目标文件通过；一次性构造验证确认 goal 显式文件路径进入 `hint_read_paths` 且变成 `allowed_read_roots`；复跑 `test_parent_acceptance_controller.py`、`test_subagent_parsing.py`、`test_result_processors_edges.py`、`test_orchestration_tools.py` 和 `test_orchestration_dispatch_completion_gate.py`。

## 2026-05-25 Structured write roots align create preflight with runner boundary

状态：已落地，focused 验收通过；待真实 MiniMax 复验

## 2026-05-25 Awaiting-acceptance final response scope

状态：已落地，focused 验证通过；待真实 MiniMax 复验

摘要：
- 阶段 2 主代理自然语言 smoke 暴露：子代理已经真实 dispatch、读取物化输入并写出 `output.json`，状态停在 `AWAITING_ACCEPTANCE/NEEDS_ACCEPTANCE`，但最终回答守卫把它当作硬 blocker，整段替换成 `Subagent State Notice`。
- 修复后区分两种口径：`blocking_task_ids()` 仍用于“严格完成”判断，待验收不算完成；最终回答覆盖只看需要修复的事实，例如 `FAILED/BLOCKED/TIMEOUT/CHANNEL_ERROR` 或父级验收 `REJECT`。因此主代理可以如实回答“子代理已跑、结果在哪、当前等待验收”，不会被泛化阻断抢答。
- 这不是放松防假完成：工具上限收口、缺质量角色、父级验收拒绝和终态失败仍会覆盖模型草稿；只是避免把正常待验收状态当成故障。
- 验证链路：新增 `test_final_response_guard_keeps_awaiting_acceptance_status_answer`，并复跑 `test_subagent_runtime_guards.py` 与 `ruff check` 目标文件通过。

## 2026-05-25 Declared output refs reach runner prompts

状态：已落地，focused 验证通过；待真实 MiniMax 复验

摘要：
- 同一阶段 2 复验继续暴露：父级 `create_subagents` 声明了 `output_files=[".../summary_result.txt"]`，验收正确把它当成交付目标；但 runner prompt 没把这个路径显式渲染给子代理，导致子代理只写内部 `output.json`，父级验收因声明产物缺失而 REJECT。
- 修复后，`output_files/output_refs` 会进入 `context_bundle.output_contract.declared_output_refs`、`task_packet.file_contract.required_file_refs/declared_output_refs` 和 `write_contract.declared_output_refs`。`render_execution_context_markdown()` 新增 `Declared Output Targets` 小节，明确内部 `output.json` 只是运行报告，不能单独冒充用户产物。
- 这不是任务专项，也不从自然语言目标里抽文件名；只传递父级工具参数里的结构化机器字段，让验收合同和 runner 可见合同同源。
- 验证链路：新增 `test_context_bundle_maps_declared_output_files_to_required_refs` 和 `test_render_execution_context_highlights_declared_output_refs`，并复跑 `test_subagent_context_bundle.py`、`test_runner_rendering_class.py`、`test_runner_prompts.py` 与 `ruff check` 目标文件通过。

## 2026-05-25 Logical output refs are not file paths

状态：已落地，focused 验证通过；待真实 MiniMax 复验

摘要：
- 阶段 2 第二次复验显示 MiniMax 会把 `output_refs` 写成 `["source_file", "subagent_summary"]`，这在开放世界协议里是合理的逻辑字段名；旧验收把它们当文件路径查存在，导致正常子代理结果被误判为缺产物。
- 修复后只有 path-like 输出 ref 才进入文件存在合同：绝对路径、带目录分隔符的相对路径、或带安全文件后缀的具体文件名。`source_file/subagent_summary` 这类非路径值仍保留在 `declared_output_refs` 里供模型理解结果键，但不会进入 `required_file_refs`，也不会触发 `declared_output_refs_exist` 缺文件。
- 这遵守开放世界铁律：`output_refs` 可表示文件、artifact URI、逻辑字段、外部系统 ref 或未来协议对象；系统只能对确定是本地文件路径的值做文件存在硬验收。
- 验证链路：新增 `test_context_bundle_keeps_logical_output_refs_out_of_required_files` 和 `test_acceptance_ignores_logical_output_refs_when_checking_files`，并复跑 `test_subagent_context_bundle.py`、`test_runner_rendering_class.py`、`test_acceptance_workspace_root.py` 与 `ruff check` 目标文件通过。

摘要：
- 同一真实协作调查场景继续暴露第二个通用不一致：模型按工具提示为 `output_files` 指向 `/Users/example/my_agent/live-agent-runs/.../outputs/*.json`，随后又补了 `extra_write_roots=/Users/example/my_agent/live-agent-runs/.../outputs`，但 `create_subagents` 预检仍只用主仓库 workspace 判断，导致结构化写入授权没有生效。
- `orchestration_write_guard.py` 现在把 `extra_write_roots/write_roots/target_roots` 纳入 create/schedule 预检允许根；`output_files/artifact_refs` 仍只是目标文件，不会自动变成宽授权根。
- 安全边界保持不变：普通 `goal/thought` 不产生写权限；`/` 和用户 home 这类过宽根会被忽略；目标文件必须落在主 workspace 或显式结构化写入根下，否则继续拒绝。
- 这条规则让创建前预检和 runner 的 `allowed_write_roots` 语义对齐，避免“系统要求模型补 extra_write_roots，但补完仍被同一预检拒绝”的死路。
- 验证链路：`ruff check` 目标文件通过；一次性构造验证确认授权 outputs 目录可通过、Desktop 非授权目标仍拒绝；复跑 `test_orchestration_tools.py` 和 `test_orchestration_dispatch_completion_gate.py`。

## 2026-05-25 Refs-only context manifest shorthand and input dependency blocker

状态：已落地，focused 验收通过；待真实 MiniMax 复验

摘要：
- 真实协作调查场景 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-103000` 暴露第三个通用链路问题：模型把 `context_manifest` 写成 refs-only 列表，例如 `context_manifest=["/path/source_01.txt"]`，旧逻辑只接受对象，导致完整路径被丢弃。随后系统只能从 goal 中得到 `source_01.txt` 这类短文件名。
- `orchestration_create_context.py` 现在把 `context_manifest` 的字符串或列表短写归一成 `required_read_paths`。对象条目不会被转成字符串，避免把上下文对象误当文件路径。
- `dispatch_runner_batches.py` 现在在显式 `run_ids` 被输入依赖过滤到没有 runner 候选时，返回 `runner_selection/input_dependencies_missing` 记录，逐个列出 run_id 和缺失的 `missing_input_refs`。这样主代理知道要补路径、重建任务或换策略，而不是继续重复空 dispatch。
- 这条规则只读取结构化 refs，不解析 IP、域名、日志、论文、GitHub 等业务语义；它修的是控制面引用传递和调度可观测性。
- 验证链路：一次性复现脚本确认旧形态会丢 refs；修复后确认列表型 `context_manifest` 能写入 `required_read_paths`，输入依赖缺失会生成 `input_dependencies_missing` 记录；`ruff check` 目标文件通过。

## 2026-05-25 Pending dispatch redirect and awaiting-acceptance state

状态：已落地，focused 验收通过；待真实 MiniMax 复验

摘要：
- 真实协作调查场景 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-112500` 已经能让第一批资料源子代理读取自己的文件，但 root 在第一批 run 尚未 dispatch 前又继续 `create_subagents`，导致同一批工作被重复创建。
- `create_subagents` 现在会检查当前 root 轮次是否已有可调度但尚未 dispatch 的 run。如果有，工具不会继续扩容，而是返回 `pending_dispatch_redirect.v1`：`created=0`、保留 `dispatch_run_ids`、给出可复制的 `next_action.dispatch_subagents`。这不是任务失败，也不解析业务语义；如果确实要追加全新的独立任务，也要等已有 run dispatch 之后再创建。
- `state_machine.py` 现在把 `AWAITING_ACCEPTANCE`、`VERIFYING` 和 `verification_status=NEEDS_ACCEPTANCE` 统一投影为 `waiting_reason=acceptance` / `lifecycle_phase=VERIFYING` / `recovery_decision=wait_for_acceptance`，不再落入 `manual_review` 未处理状态。
- `current_turn_run_state` 新增 `awaiting_acceptance_run_ids`。当当前轮没有 blocked、但有待验收 run 时，状态合同会建议 `dispatch_subagents(apply=true, execute_runners=false, run_ids=[...])` 跑验收路径，而不是诱导 root 汇报完成或重复创建新代理。
- 验证链路：一次性复现脚本先确认旧状态机会把 `AWAITING_ACCEPTANCE` 打成 `manual_review`；修复后确认待验收 run 进入 `VERIFYING` 并输出 acceptance 建议。复跑 `ruff check` 目标文件和 `test_main_agent_state_machine_contract.py`、`test_orchestration_tools.py`、`test_orchestration_dispatch_completion_gate.py`。

## 2026-05-25 Structured task refs grant external task output roots

状态：已落地，一次性复现脚本通过；待 focused/真实复验

摘要：
- 真实协作调查场景继续暴露一个通用路径断点：用户给了独立任务目录 `/Users/.../live-agent-runs/...`，模型在 `create_subagents.items[].context_manifest` 里给了输入文件路径，在 `output_files` 里给了同一任务目录下的输出文件路径，但系统只认主仓库 workspace 和显式 `extra_write_roots`，第一轮就把用户要求的输出目录拒掉。
- 修复后，`context_manifest` 被视作开放的 refs carrier：任意结构化字段值里出现的文件 ref 都会归入输入依赖。它不维护 `source_file/alert_file` 这类封闭字段名，也不解析业务语义。
- `create_subagents` 预检和 `CreateRunParams.extra_write_roots` 现在会从同一个结构化参数包中推导窄写根：只有当 `output_files/output_refs/artifact_refs` 的输出路径与至少一个真实存在的输入 ref 共享一个足够窄的任务目录时，才把输出文件父目录加入写入根。这样用户临时任务目录可以跑通，但 `/`、用户 home、`/etc` 这类宽根不会因为模型填了 output path 就被授权。
- `registry_invoke.py` 修正读根接线：`write_boundary.allowed_read_roots` 现在会像写根一样临时并入 `read_file/list_files/search_text` 的底层 filesystem workspace_roots，避免上层 path gate 已放行、底层文件工具仍报 `PATH_WORKSPACE_ESCAPE_BLOCKED` 的不一致。
- 文件 ref 识别改成开放世界扩展名规则：已知格式仍是快路径，未知后缀如 `.xml`、`.pptx`、`.customext` 也能作为结构化文件 ref 参与调度和写根判断，不再因为“不在内置表”就丢失。
- 输入 ref 去重现在会保留覆盖范围更明确的完整路径，丢弃同一字段里的短写重复项。例如 `/tmp/task/source_01.txt` 和 `source_01.txt` 同时出现时，完整路径作为事实源，短写不再触发 `missing_input_refs`。
- 子代理自己的 `output_files/output_refs/artifact_refs` 不会进入 `required_read_paths`。即使模型在 goal 中同时写了“读取输入文件”和“写到输出文件”，系统也不会要求未来产物在 runner 启动前已经存在。
- `goal` 中抽到的文件路径降级为 `context_manifest.hint_read_paths`：它只给 runner 一个可读授权和提示，不参与 `missing_input_refs` 启动依赖判断。硬依赖仍只来自 `required_read_paths/input_refs/input_files` 等结构化字段。
- 这条修复不新增专项 case，不读取 prompt 自然语言来决定授权，只使用工具参数中的结构化输入/输出 refs 和文件系统存在性事实。

## 2026-05-25 Orchestration contract requires execution by default

状态：已落地，一次性复现脚本通过；待真实 MiniMax 复验

摘要：
- 真实协作调查场景 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-144053` 暴露一个通用合同缺口：入口物化出来的 `orchestration_contract.v1` 只要求 `create_subagents`，所以主代理创建 11 个子代理后可以误以为协作合同已满足，甚至在子代理仍是 `PLANNING/UNVERIFIED` 时尝试 `submit_for_acceptance` 或等待用户确认。
- 修复后，普通协作合同默认包含 `execution_required=true`。入口提示会要求新合同写入 `create_subagents` 与 `dispatch_subagents`；运行时和提示层也会把旧合同里的 `create_subagents` + `execution_required=true` 解释为还必须真实 `dispatch_subagents`。这表达的是通用事实：创建任务记录不等于执行任务；只有真实 dispatch 之后，子代理才会开始工作、写结果、进入验收。
- 为开放场景保留逃生口：如果外部结构化 case 明确声明 `execution_required=false`，系统允许只创建/规划子代理，不强制要求 `dispatch_subagents`。这避免把“只想设计代理分工”的任务误伤成必须执行。
- 运行时兼容旧合同：即使旧 `orchestration_contract` 为兼容外部结构化输入而保留 `required_tools=["create_subagents"]` 原样，只要没有明确 `execution_required=false`，最终回答前的编排合同也会把缺少 `dispatch_subagents` 识别为待返工，而不是让任务口头完成。
- 这条规则不绑定 IP、日志、文件数量、agent 数量或某个测试目录；它只看机器字段 `orchestration_contract` 和真实工具调用记录。
- 验证链路：`ruff check` 目标文件通过；一次性脚本确认入口合同会自动补 `dispatch_subagents`，旧合同缺 dispatch 会被 `missing_orchestration_requirements` 打回，`execution_required=false` 时不会强制 dispatch。

## 2026-05-25 Dispatch orchestration wrapper normalization

状态：已落地，一次性复现脚本通过；待真实 MiniMax 复验

摘要：
- 真实协作调查复验 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-150500` 显示上一刀有效：模型创建子代理后主动调用了 `dispatch_subagents`，不再直接 submit/等待用户确认。
- 新卡点是工具协议兼容：模型把 `run_ids/concurrency/mode` 包在 `orchestration:{...}` 里，并使用 `mode=parallel/async`。旧 `dispatch_subagents` 只读取顶层参数，导致真实 run ids 没被识别，连续返回模式错误。
- 修复后，`dispatch_subagents` 在入口先展开 `orchestration` wrapper；`concurrency/parallelism/runner_concurrency` 归一到 `max_runners`；`mode=parallel/async/execute/run/real` 归一成 `apply=true + execute_runners=true + workflow_mode=off`，`mode=dry_run/preview/plan` 归一成 dry-run 预览。
- 这不是放松安全边界：仍然只接受机器字段，不从自然语言里猜 run_id；真正执行还要经过已有 path、scope、lease、acceptance 和 runner gate。
- 验证链路：`ruff check` 目标文件通过；一次性脚本确认 wrapped dispatch 参数会被解包并得到正确执行开关。

## 2026-05-25 Same-batch coordinator defer

状态：已废弃，2026-05-26 已从运行时删除

摘要：
- 真实协作调查 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-152000` 已能创建并真实 dispatch 11 个子代理，但 coordinator 和 worker 同批并行启动，worker 结果尚未稳定时 coordinator 已开始汇总，最终漏掉了 source_05/source_08 的命中事实。
- 当时的修复是：同一批显式 `run_ids` 同时包含 worker 与 coordinator/reviewer/acceptor 时，如果 worker 仍在 `PLANNING/PENDING/RUNNING`，调度层先只启动 worker，暂缓 coordinator。等 worker 至少离开预结果状态后，下一轮 dispatch 再启动 coordinator 做汇总、复核或验收。
- 这条规则不解析 IP、日志、字段名或业务实体，只读结构化角色、agent_name 和状态机状态。它解决的是“汇总者不能和被汇总对象并发抢跑”的控制面时序问题。
- 验证链路：`ruff check` 目标文件通过；一次性脚本确认 worker+coordinator 同批时先返回 worker，worker 到 `AWAITING_ACCEPTANCE` 后 coordinator 可进入候选。
- 复盘结论：这仍然是隐藏流水线，和“不要让运行时替父代理偷偷卡流程”的规则冲突。当前版本已删除 `split_deferred_coordinator_candidates` 和第二波 coordinator 续跑逻辑；父代理要先收集再汇总时，应该显式先 dispatch 收集者，确认结果后再 dispatch 汇总者。

## 2026-05-25 CLI run dialogue memory isolation

状态：已落地，待真实 MiniMax 复验

摘要：
- 真实协作调查复验 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-160000` 暴露上下文污染：`my-agent run` 的一次性任务从长期记忆召回了旧 GitHub 任务 prompt，模型把 Related Memory 里的旧对话误当成当前任务，读完告警后转向去做 GitHub XLSX。
- 修复后，`source=cli_run` 的普通一次性执行默认过滤 `kind=dialogue` 的历史记忆，只保留规则、经验、事实等非对话记忆。用户明确说“继续/恢复/刚刚/run_id”等恢复意图，或调用方显式开启 `resume_context=True` 时，旧对话才会重新参与 prompt。
- `chat/gateway` 长会话不走这个过滤，仍保留对话连续性；子代理/控制面 `task_local/control_plane` 原本就不注入主代理记忆，语义不变。
- 这不是专项修复，也不是靠 prompt 软约束让模型“别看旧任务”；它把当前任务和旧对话记忆在入口结构上分开，避免旧用户任务被当成新的可执行指令。

## 2026-05-25 Top-level orchestration scope must be current-turn scoped

状态：已落地，待真实 MiniMax 复验

摘要：
- 同一复验继续暴露旧账本污染：本轮还没创建任何子代理时，顶层 `refs_only` body-read guard 和隐式 `dispatch_subagents` 会扫描全局 `data/subagents`，把历史子代理当作当前任务的孩子或可调度对象。
- 修复后，顶层 root 只有在本轮已经记录了 `create_subagents/schedule_child_subagents/dispatch_subagents` 触碰过的 run_id 时，才把这些 run 作为当前委托范围。没有本轮 run_id 时，body-read guard 不会用历史子代理阻断普通输入读取；顶层隐式 dispatch 会返回 `no_current_turn_scope` 返工提示，要求先创建本轮子代理或显式传入 run_ids。
- 这条规则不删除历史任务，也不改变显式恢复/接管能力；它只禁止“未指定范围时扫描旧 workspace 猜当前任务”，避免真实任务被旧测试数据污染。

## 2026-05-25 Item batch context and explicit dispatch scope

状态：已落地，focused 验证通过；待真实 MiniMax 复验

摘要：
- 真实协作调查 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-163000` 暴露两个通用控制面问题：`create_subagents.items[]` 会把顶层 `context_manifest` 的开放字段整体继承给每个子任务，导致 `source_files` 清单里的所有文件都变成每个 worker 的硬依赖；显式传 `agent_ids/run_ids` 的 `dispatch_subagents` 在 workflow、patch review、acceptance finalize 阶段仍会扫描历史 workspace。
- 修复后，批量创建子代理时不再把全局 `context_manifest` 整段复制给每个 child。每个 child 的硬 `required_read_paths` 只来自显式 required refs、共享 directive/brief context pack，以及该 child 自己 goal 中明确存在的文件路径；全局 `source_files`、`sources_base`、`output_dir` 这类开放字段只保留为上下文，不再卡 runner。
- 共享 directive/brief 不是业务专项：它只看 context pack 的机器角色是否是 `primary_directive/directive/instruction/brief`，不解析 IP、域名、论文、GitHub 等任务实体。
- 显式 dispatch 的后处理现在继承同一 scope：workflow 记录、leadership recovery inspect、patch review 和 acceptance finalize 都不会在传入 `run_ids/agent_ids/root_id/parent_run_id` 时扫旧任务。这样同一个全局 `data/subagents` 可以继续长期保存历史任务，但本轮调度报告只描述本轮目标。
- `dispatch_subagents` 的 run id 入口补充了常见模型别名 `child_run_ids/children`，并把 `direct_children=true` 解释成当前作用域的直接孩子，而不是字符串 run_id `"True"`。这些入口只映射到同一个机器字段 `include_run_ids` 或当前轮已知作用域，不会从自然语言里猜 run，也不会绕过已有 scope、lease、runner 和 acceptance gate。
- 同一批显式目标不再按 worker/coordinator 自动拆两波执行。显式 `run_ids` 是父级的操作请求，系统按给定顺序和并发参数推进；如果需要先收集再汇总，父级应分两次 dispatch。
- 验证链路：`ruff check` 目标文件通过；一次性脚本确认 child refs 只包含 alert directive 和自己的 source 文件，acceptance scope 只返回本轮 run；`test_orchestration_tools.py` 与 `test_delivery_contract_prompting.py` 通过。旧 `test_orchestration_body_read_guard.py` 仍包含“顶层必须禁止读正文”的历史期望，和前面放松普通任务卡死规则冲突，未修改测试文件。

## 2026-05-26 Local-progress guard no longer blocks

状态：已落地，focused 验证通过

摘要：
- 本地进展门从“closeout 失败后连续只读到一定次数就停止”改成“只按固定间隔给返工建议”。它仍然记录 failure/work-progress 指纹和连续只读轮次，但不会把任务置为 blocked。
- 本地进展门只保留 `local_progress_unlimited_hint_interval` 一个参数，默认每 10 轮给软提示。
- 这样 closeout 失败后的真实返工路径统一回到 closeout 结构化返工单和模型继续执行，不再因为“只读几轮”提前结束任务。
- 验证链路：`test_tool_local_progress_guard.py`、`test_exploration_fuse_config.py`、`test_runtime_guard_config_shared.py` 通过。

## 2026-05-25 Subagent input materialization recovery

状态：已落地，focused 验证通过；待真实 MiniMax 复验

摘要：
- 审查发现输入依赖诊断还有一个控制面漏点：显式 `run_ids` 只有“全部候选都被过滤”时才报告缺输入；如果部分可跑、部分缺输入，缺输入项会静默消失。自动候选路径也直接用 `input_dependency_ready_candidates` 过滤，主代理看不到哪个 runner 被跳过。
- 修复后，`runner_input_dependencies.py` 新增 ready/skipped 分流结果，`dispatch_subagents` 会对显式和自动候选统一写 `runner_selection/input_dependencies_skipped` 记录。记录里包含机器字段 `input_dependency_skipped_run_ids`、`input_dependency_missing_refs_by_run`、`input_dependency_checked_roots_by_run` 和 `input_dependency_recoverable`，不要求模型从中文 message 里猜。
- `dispatch_subagents` 顶层 payload 新增 `input_materialization_recovery`，里面给出可复制的 `materialize_subagent_inputs` 工具调用建议。
- 新增 `subagent_input_materialization.py` 和模型工具 `materialize_subagent_inputs`。它把父级可读的本地普通文件复制到 `subagents/_shared_inputs/<run_id>/`，写 `materialized_inputs.json` 记录 source、hash、size 和目标路径，再用受控路径替换 child `context_manifest.required_read_paths`。原始 refs 保存在 `task.attributes.original_required_read_paths`，供审计使用。
- 安全边界保持硬规则：不跟随 symlink，不复制目录/设备文件，不把整个文件系统授权给 child；未知后缀不会被拒绝，因为输入交接层只处理文件 refs，不做封闭格式枚举。
- 同轮顺手恢复 `_run_ids_from_dispatch` 旧 helper 名作为 `_run_ids_for_scope` 的 thin wrapper，并让当前轮 scope 不再被旧 acceptance 记录扩展，避免历史验收污染本轮收口。
- 验证链路：新增 `test_runner_input_dependency_diagnostics.py`、`test_subagent_input_materialization.py`、`test_orchestration_input_materialization_tool.py`；复跑 `test_runner_input_dependencies.py`、`test_runner_dispatch.py`、`test_orchestration_dispatch_subagents_tool.py`、`test_orchestration_tool_specs.py` 和上述新增测试通过。

## 2026-05-25 Acceptance-only dispatch default for waiting subagents

状态：已落地，focused 验证通过；待真实 MiniMax 复验

摘要：
- 真实 11 子代理协作 smoke 暴露：10 个 worker 已经写出结果并进入 `AWAITING_ACCEPTANCE/NEEDS_ACCEPTANCE`，但模型如果只按看板建议继续 `dispatch_subagents(apply=true, run_ids=[...])`，旧默认不会自动跑父级验收，也不会自动应用验收 follow-up，任务容易卡在“已产出但未验收”。
- 修复后，`dispatch_subagents` 的默认策略会检查显式目标 run 是否已等待验收。若是，`apply=true` 会默认进入验收-only 续推：`execute_runners=false`、`execute_acceptance_tests=true`、`auto_apply_acceptance_followup=true`。显式参数仍优先，模型明确传 `execute_acceptance_tests=false` 时不会被覆盖。
- `subagent_board` 的完成状态建议也同步改为验收-only。只有 blockers 不是纯待验收时，才建议继续 runner dispatch。这样不会为了验收已产物任务重复跑子代理。
- `current_turn_run_state.awaiting_acceptance_run_ids` 的建议工具调用也显式带上 `execute_acceptance_tests=true` 和 `auto_apply_acceptance_followup=true`，避免模型只看到 `execute_runners=false` 后不知道下一步要跑验收。
- 这不是业务专项规则，也不是新的前置硬门。它只看结构化状态 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`，把“runner 已产出，下一步验收”变成通用控制面默认。
- 验证链路：一次性脚本先复现旧默认 `execute_acceptance_tests=false/auto_apply_acceptance_followup=false`；修复后确认待验收目标会默认打开验收和 follow-up，`subagent_board` 给出验收-only 建议。复跑 `ruff check` 目标文件和 `test_orchestration_dispatch_subagents_tool.py`、`test_orchestration_board_payload.py`、`test_agent/test_planner_and_watch.py` 通过。

## 2026-05-25 Takeover run structured handoff inheritance

状态：已落地，一次性复现脚本通过；待真实 MiniMax 复验

摘要：
- 真实 11 子代理协作 smoke 继续暴露恢复链问题：旧 coordinator 被 180 秒外层 wrapper 杀掉后进入 `RUNNING` stale，due-check 创建 takeover run，但新 takeover 没有继承原 coordinator 的 `context_manifest`、`context_packs` 和 `output_files/output_refs`。结果就是新接管者不知道要读告警文件、兄弟清单和最终输出目标。
- 修复后，`takeover_run.py` 创建接管 run 时会继承原 run 的 `quality_contract`、`context_manifest`、`context_packs`、机器产物 refs、artifact/evidence refs 和写入根；只重写 `takeover_source_run_id`、`takeover_source_refs`、`takeover_chain_depth` 等接管审计字段。
- 如果旧代码已经创建过缺交接字段的 takeover run，后续复用它时会自动补齐源 run 缺失的结构化交接单，但不会覆盖已有 takeover 自己新增的字段。
- 这不是 IP 协作专项修复。它只解决通用接管语义：恢复/接管不能把“该读什么、该写哪儿、有哪些兄弟/上下文包”丢掉。
- 验证链路：一次性脚本先确认旧行为会丢 `required_read_paths/hint_read_paths/task_pack_refs/context_packs/output_files/output_refs`；修复后同脚本全部继承通过，并额外确认复用旧 takeover 时会补齐缺失字段。

## 2026-05-26 Generic write surface replaces special builders

状态：已落地，pytest 全量通过

摘要：
- 本轮按“不要把主代理变成模板执行器”的纠偏原则，删除模型可见的专项写入/构建工具：`append_file`、`replace_in_file`、`file_write_session`、`write_structured_json`、`data_to_workbook`、`markdown_to_pdf`。
- 当前模型可见写入面收敛为 `write_file` 和 `apply_patch`：`write_file` 原子写入完整文本或 `data_base64` 二进制；`apply_patch` 做局部文本修改、新增、删除、移动。
- PDF、XLSX、Word、PPT、视频、XML、未知格式等开放世界产物不再走固定 builder。模型可以用授权命令、脚本或库生成，再通过通用写入和 closeout/artifact acceptance 验收。
- 这次也标记了之前走偏的方向：open write session、固定 workbook/pdf/json builder、builder-ready 自动推进，都容易把普通任务塞进单一路径。以后遇到产物质量问题，优先修 closeout 验收、工具错误回执、路径安全和通用返工，不再新增任务专项工具或前置硬门。
- 验证链路：`python3 -m compileall -q agent_py_agent/agent agent_py_agent/tests` 通过；`python3 -m pytest -q agent_py_agent/tests --tb=short --maxfail=20` 全量通过；`python3 scripts/check_code_size.py --mode warn` 输出 `hard=0 high-risk=0 soft=0`。
