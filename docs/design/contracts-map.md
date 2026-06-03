# My-Agent 合同地图

这份地图只记录代码层机器合同，不把 prompt 文案当事实来源。读代码时优先从核心稳定层开始，再看实验性运行链路。

## 参考项目入口

每次改合同前，先看 `/Users/example/study-agent/all-agent/` 下对应索引和源码：

- 长期助手：集中状态、工具网关、运行状态快照。
- 通道运行时：task/run 控制面、pending tool call、结构化 stop reason。
- 终端交互：路径权限、工具注册和失败边界。
- 会话运行时：结构化工具调用、事件流和 refs-only 恢复。

## 成熟度分层

### 核心稳定合同

这些模块是运行底座，优先保持小而硬：

- `agent_py_agent/agent/contracts/error_taxonomy.py`：错误分类和错误合同。
- `agent_py_agent/agent/contracts/error_classification_rules.py`：错误分类规则集合。
- `agent_py_agent/agent/contracts/state_machine.py`：任务状态、dispatch、repair、recovery 决策。
- `agent_py_agent/agent/contracts/recovery_actions.py`：恢复动作枚举和统一词表。`state_machine`、`error_taxonomy`、`GateDecision.recommended_action` 以及 dict payload 里的 `"recommended_action"` 都必须使用同一套 `RecoveryAction` 值；动作名只能表达一个动作，不能写成 `xxx_or_yyy`。需要分支时放在 `fallback_action`、`reason`、`recovery_hint` 或 gate evidence 里。
- `agent_py_agent/agent/contracts/tool_protocol_v2.py`：工具调用归一化；旧 flat tool call 里的业务字段保持开放世界，不把 input 里的 `status` 误判成协议状态枚举。
- `agent_py_agent/agent/contracts/gates/`：工具、路径、审批、幂等、交付质量等运行门。
  所有 runtime gate 统一返回 `GateDecision`，序列化时必须包含
  `allow_action`、`block_task`、`severity`、`model_message`、`operator_message`
  和 `evidence_refs`。`DENY/BLOCKED` 不能靠调用方自己猜语义；普通工具换路只是不放行本次动作，
  只有显式 `terminal_block` 或 evidence 标记时才算阻断整个任务。
  gates 根目录只保留跨类入口和稳定门面；具体实现按 `tool/`、`command/`、`network/`、`artifact/`、
  `document/` 分包。rate limit 和 pipeline 已内聚进各自主文件；
  delivery quality 的 gate 入口保留在 `contracts/gates/delivery_quality.py`，指标和语言规则下沉到
  `contracts/delivery_quality_checks.py`，避免在 gates 目录为一个小概念拆出 3-4 个平行文件。
- `agent_py_agent/agent/contracts/delivery_contract_doctor.py`：入口级 delivery_contract 自检，负责 schema、路径边界、开放世界扩展声明和返工动作。
- `agent_py_agent/agent/contracts/effective_contract_snapshot.py`：最终生效合同快照。
- `agent_py_agent/agent/contracts/run_trace_contract.py`：运行 trace 结构。
- `agent_py_agent/agent/contracts/contract_status.py`：合同失败状态汇总。
- `agent_py_agent/agent/contracts/contract_trace.py`：finding 调试链。
- `agent_py_agent/agent/agent_core/delivery_requirement_materializer.py`：从普通用户需求物化开放世界 delivery 合同；不再生成 orchestration 硬合同。
- `agent_py_agent/agent/agent_core/orchestration/shared_context.py`：父级小型读取 brief 进入子代理 `context_packs` 的通用桥接层；工具刚成功返回时可直接缓存，归档扫描作为补充，只传摘要和 refs，不写业务专项字段。
- `agent_py_agent/agent/agent_core/orchestration/dispatch/refs.py`：父级调度结果索引层；从本轮 touched run 和 runner-created child 汇总状态、摘要、`run_closeout_ref` 和产物 refs，供 `dispatch_subagents` / `inspect_agent_tree` 在长记录前先展示关键机器事实。读取 child 列表、单个 child 或 child output 失败时必须返回结构化 `load_error` / `output_load_error`，不能伪装成没有子代理结果。
- `agent_py_agent/agent/agent_core/orchestration/scope_resolution.py`：编排身份裁决层；`inspect_agent_tree` / `dispatch_subagents` 在显式 run/root/parent 参数与当前 runner 上下文冲突时输出 `scope_resolution/scope_warnings`，让权限收窄可见，不靠线程 current 静默猜身份。
- `agent_py_agent/agent/agent_core/orchestration/sibling_roster.py`：同批子代理身份索引层；批量创建后把 peer `run_id/name/role/goal` 写入 `sibling_roster` context pack，解决同批兄弟彼此不可见的问题。它只提供索引，不发布未来产物路径，也不制造等待关系。
- `agent_py_agent/agent/user_space/capability_resolver.py`：owner/shared/builtin 能力短名解析层；owner/shared 索引里的坏 JSONL 行或读取失败必须进入 `index_load_errors`，正常候选继续可用。模型看到的是“能力索引部分坏了”，不能把坏索引误判成“没有这个 skill/tool/workflow”。
- `agent_py_agent/agent/user_space/identity_store.py`：provider 身份到 owner home 的路由层；provider identity JSONL 坏行必须进入 `load_errors`，好记录继续用于 owner 解析。坏身份索引不能被模型或后台入口误判成“用户没有 owner home / 未绑定身份”。
- `agent_py_agent/agent/user_space/owner_lifecycle.py`：owner 生命周期状态层；`owner_status.json` 损坏时必须返回 `load_error`，状态面板和 doctor 标为 `UNKNOWN`，不能把坏状态文件默认为 active。
- `agent_py_agent/agent/user_space/home_indexes.py`：全局 owner/task/run/agent 索引层；`latest_*_refs_report()` 必须保留坏 JSONL 行的 `load_errors`，好记录继续返回。索引局部损坏不能让后台恢复或看板误以为没有活跃任务、run 或 agent。
- `agent_py_agent/agent/user_space/temporary_grants.py`：owner 临时授权账本；坏 grant JSON 必须进入 `load_errors` 并从 active grant 列表排除，不能用空 payload 补成 active 授权。
- `agent_py_agent/agent/user_space/owner_policy.py`：owner 权限、配额、retention、skill/tool policy 读取层；坏策略 JSON 必须进入 `OwnerPolicyBundleReport.load_errors`、`EffectiveOwnerPolicy.to_dict().load_errors` 和 `home_doctor.owner_policy.load_errors`。坏配置不能被静默吞成默认策略，也不能扩大权限。
- `agent_py_agent/agent/user_space/home_runtime_status.py`：owner home 状态面板；`system/schema_version.json` 读取失败必须进入 `schema_load_error`，不能把坏 schema 文件伪装成空 schema。
- `agent_py_agent/agent/user_space/home_backup.py`：owner home 备份清单层；坏 `backup_*/manifest.json` 必须进入 `HomeBackupSnapshotsReport.load_errors` 和 `home_doctor.backup.load_errors`，正常 snapshot 继续可见。
- `agent_py_agent/agent/user_space/home_index_rebuild.py`：owner/task/run/agent index 重建层；坏 task/agent state 必须进入 `HomeIndexRebuildResult.load_errors`，对应 ref 标为 `UNKNOWN`，不能把坏 state 写成正常 active。
- `agent_py_agent/agent/user_space/owner_compact_indexes.py`：owner compact 索引指针层；坏 pointer JSON 必须进入 `OwnerCompactIndexRefsReport.load_errors` 和 `home_doctor.compact_indexes.load_errors`，不能只退化成模糊 dangling。
- `agent_py_agent/agent/user_space/context_bundle_artifacts.py`：主代理 context bundle 产物引用回填层；context bundle JSON 损坏时必须返回 `load_error`，不能只给 `missing_or_invalid_context_bundle` 这种模糊状态，让模型/CLI 误以为只是文件没生成。
- `agent_py_agent/agent/user_space/compact_layout.py`：task/run/agent compact 基础包和分支索引层；`branches.json` 损坏时，新包仍可创建，但必须在重写后的分支索引里保留 `load_errors`，不能无痕覆盖坏账本。
- `agent_py_agent/agent/user_space/compact_injection.py`：compact 包 prompt 注入层；`continue_packet.json` 损坏时必须在注入文本里显示读取错误和路径，不能把坏续接包渲染成一组“无”，误导模型以为任务没有下一步。
- `agent_py_agent/agent/memory_archive/compact_context_bundle/refs.py`：compact apply 主 context bundle 引用层；显式或自动 context bundle ref 指向坏 JSON / 编码错误 / 缺失文件时，payload 和 restore refs 要保留 `load_error`，不能把坏引用清成“没有 context bundle”。普通 scope mismatch 仍不附加无关 bundle。
- `agent_py_agent/agent/user_space/home_runtime_compact_refs.py`：task workspace compact refs 暴露层；`latest.txt` 或 `latest` 指针坏了要进入 `compact.load_errors`，同时继续返回可用 fallback refs，不能把坏 compact 指针伪装成“没有恢复包”。
- `agent_py_agent/agent/user_space/home_daily_memory_query.py`：owner 每日记忆查询层；JSONL 坏行或文件读取失败要进入 `load_errors`，好记忆继续返回，不能把局部坏行伪装成“当天没有记忆”。
- `agent_py_agent/agent/user_space/home_runtime_query.py`：owner task workspace 查询层；task `state.json` 损坏时 item 必须携带 `state_load_error`，不能把坏状态账本伪装成空状态。
- `agent_py_agent/agent/user_space/capability_requests.py`：owner 能力申请账本；坏 request JSON 必须进入 `load_errors` 并从 request 列表排除，不能补成假的 open request。
- `agent_py_agent/agent/agent_core/orchestration/create_items.py`：批量子代理参数解析层；只把 `items` 解析成独立任务 bundle，不再拒绝 sibling 共享输出，也不再从输入/输出路径推断批次依赖。父级显式给 item 的 read refs 会原样保留为读线索；路径不存在不会卡启动。
- `agent_py_agent/agent/agent_core/runner/ref_fields.py`：runner 路径字段解析 helper；只提取显式输入/输出 refs 供提示、写根和报告使用，不再因为 `required_read_paths` 缺失或不存在而跳过 runner。真实缺文件由子代理运行时工具结果返回给模型处理。
- 已删除旧输入物化工具：`subagent_input_materialization.py` / `materialize_subagent_inputs` 不再存在。父代理要么在 prompt/结构化参数里给清楚路径，要么让子代理运行后自己报告缺文件；系统不再自动复制“父级可读文件”来修补启动门。
- 已删除旧调度提示/去重 helper：`scheduling_warnings`、`hierarchy_duplicate_domains.py`、`hierarchy_leaf_targets.py` 和 `hierarchy_scope_domains.py` 不再参与生产路径。`schedule_child_subagents` 只返回创建、复用、待 dispatch、tree/status 相关事实；重复领域、共享目标、不同主题协作交给父级模型按任务语义处理。
- `agent_py_agent/agent/agent_core/runner/prompt_context_summary.py`：runner 上下文摘要层；把 targeted collaboration request 的结构化线索包渲染给响应代理。
- `agent_py_agent/agent/agent_core/agent_tree/status.py`：代理树只读状态模型；供 `inspect_agent_tree`、watch 和后台主代理读取 task/run/parent/depth/current tool/progress/artifacts/blockers。树、direct children、QA 扫描这类父级事实源遇到账本读取失败时要暴露结构化 runtime error，不能返回空树或空 QA 结果。
- `agent_py_agent/agent/agent_core/services/watch_service.py`：watch 观察/推进边界；默认只观察代理树，显式 `advance` 才调用 dispatch。
- `agent_py_agent/agent/subagents/services/takeover/run.py`：超时/断连 run 的接管创建层；接管 run 必须继承原 run 的 `quality_contract`、`context_manifest`、`context_packs`、`output_files/output_refs`、artifact/evidence refs 和写入根，只重写 `takeover_*` 审计字段。复用旧 takeover 时也会补齐缺失交接字段，避免恢复链把“该读什么、该写哪儿、兄弟是谁”丢掉。
- `agent_py_agent/agent/subagents/runner_rendering.py`：子代理 prompt 渲染层，Context Pack 使用开放字段小型展示，避免新字段落盘但不进模型上下文。
- `agent_py_agent/agent/subagents/result_artifact_evidence.py`：子代理结构化结果里的 artifact refs 解析和登记层；解析 sibling/child 产物引用时，如果 child state 读取失败，要把 `artifact_ref_load_errors` 写回 task attributes，不能把坏 child 账本误判成“child 没产物”。
- `agent_py_agent/agent/subagents/run_budget.py`：子代理 runner 成本 refs-only 汇总层；坏 `runner_result.json` 必须进入 `SubagentRunBudgetReport.load_errors`，不能静默跳过导致父代理/CLI 低估模型调用、工具轮数或 token 成本。
- `agent_py_agent/agent/subagents/services/persistence/service.py`：子代理状态保存层；读取 `output.json` 派生 checkpoint artifacts 时，如果输出账本损坏，必须在 legacy checkpoint 和 agent-run checkpoint 的 `load_errors` 中保留 `subagent.persistence.output_json`，不能把坏输出账本伪装成没有 tests/next actions/blockers。
- `agent_py_agent/agent/subagents/patch/patch_service.py` / `patch_apply.py` / `services/patch_apply/executor.py`：patch review/apply 报告层；批量扫描时坏 `output.json` 必须生成 `OUTPUT_LOAD_ERROR` 记录并保留 `load_errors`，不能静默当成没有 patch。真实 apply 成功后如果旧输出账本已坏，新写出的 `output.json` 也要携带读取错误诊断。
- `agent_py_agent/agent/collaboration/store.py`：协作 case/request/evidence append-only 账本；case 推荐 `open/close` 窗口语义，多目标 request 按 responder 身份逐个闭环，deadline 到点后暴露 timed out、missing responders 和 unavailable targets，允许带部分证据继续推进。自动按能力匹配 responder 时，如果 capability roster 读取失败，request metadata 和工具结果必须暴露 `capability_roster_load_error`，不能伪装成“没有合适响应者”。协作 JSONL 账本里的单行坏数据会进入 `load_errors`，正常行继续返回；坏 requests/evidence/participants/decisions 不能被模型误解成“没人响应/没有证据/没有参与者/没有决策”。
- `agent_py_agent/agent/collaboration/tools.py`：协作工具入口；包含 `deadline_seconds`、refs-first evidence、结构化 request lifecycle、目标 run_id 解析、不可达目标回执和 case status。
- `agent_py_agent/agent/agent_core/orchestration/dispatch/collaboration_candidates.py`：dispatch 为待响应协作请求挑选 runner 的候选扫描层；读取 pending request 失败或读到坏协作账本时返回 `collaboration_candidate_load_errors`，不能把坏账本伪装成“没有协作候选”。
- `agent_py_agent/agent/conversation/`：长期 thread、message、observation、guidance、task link、wake signal、progress policy 和后台主代理唤醒 runtime；本地 internal channel 可在无飞书/微信时测试同一套会话语义。后台上下文读取失败必须以 `Runtime Load Errors` 暴露给模型，不能静默退化成“没有消息、没有运行中提示、没有绑定任务、没有唤醒、没有代理树”；单个坏 JSONL 行、坏 task link、坏 wake 文件或坏 progress policy 不能吞掉同目录里的正常记录，也不能把账本读取失败解释成“没有定时汇报策略”。事件、协作和运行中 guidance 通过 `thread_for_task` 单点反查会话时，坏 task link 必须抛出可报告的数据损坏错误，不能退化成“这个 task 没绑定 thread”。`thread.json`、`bindings.json`、`user_latest.json` 也必须有 report 版读取；索引损坏时不能静默创建重复 thread，显式 thread_id 指向坏 thread 时必须返回 `load_error`，不能误报 `unknown_thread`。
- `agent_py_agent/agent/session/manager.py`：旧 session 文件账本层；`session.json` 损坏时 report 版查询必须保留 `session.manager.session.read`，正常 session 继续返回，不能把坏会话文件误判成“会话不存在”。
- `agent_py_agent/agent/session/cross_channel.py`：旧 session 跨通道绑定层；`channels.json` 损坏时 report 版查询必须保留 `session.cross_channel.channels.read`，正常会话继续返回，不能把坏绑定文件误判成“该用户没有跨通道会话”。
- `agent_py_agent/agent/session/admin_query.py` / `context_sync.py`：旧 session 管理员查询和跨通道上下文同步层；读取 task registry 失败时必须返回 `load_errors` 并在格式化上下文中显示“读取警告”。坏任务表不能被模型误解成“没有活跃任务”，管理员最近活动也不能把 registry 失败吞成空时间线。
- `agent_py_agent/agent/gateway_parts/io.py` / `queue_service.py`：gateway 文件队列 JSON 读取、CLI 状态渲染和 index 重建层；旧 `read_json_file()` / `rebuild_gateway_index()` 继续兼容返回 dict/count，report 版必须保留读取错误。CLI `render_gateway_status()` 读取坏 state/heartbeat 时要显示 `state_load_error` / `heartbeat_load_error`。`rebuild_gateway_index_report()` 遇到坏 history/request/response 时要保留 `load_errors`，好记录继续索引，不能把坏状态账本渲染成默认 stopped、空 heartbeat 或把坏历史跳过成“没有记录”。
- `agent_py_agent/agent/gateway_parts/request_worker.py` / `request_execution.py` / `response_renderer.py` / `recovery.py`：gateway 请求执行、response 读取和 processing 恢复层；坏 inbox/processing request 文件必须生成 `GATEWAY_REQUEST_LOAD_ERROR` 和 `request_load_error`，并归档到 failed。坏 response 文件必须生成 `GATEWAY_RESPONSE_LOAD_ERROR` 和 `response_load_error`；CLI/chat poll 和 worker 不能把它当成“还没响应”、等待超时或重跑请求。执行完成前后再次读取 processing request 失败时必须把 `final_request_load_error` 留在 response，不能把 lease/归档字段缺失伪装成天然为空。`recover_gateway_processing_requests_report()` 要保留 `load_errors`，旧 `recover_gateway_processing_requests()` 继续只返回计数。坏请求文件不能被误解释成 `EMPTY_PROMPT`、`UNSUPPORTED_KIND`、空请求或普通 `kind=unknown` 业务失败。
- `agent_py_agent/agent/gateway_parts/adapter.py`：外部文件 adapter 到 gateway ask 的转换层；坏 adapter inbox 消息文件必须生成 `ADAPTER_MESSAGE_LOAD_ERROR` 和 `message_load_error`，并归档到 failed。坏 adapter 消息不能被误解释成用户发送了空 prompt，也不能进入 gateway ask 执行链。
- `agent_py_agent/agent/gateway_parts/runtime_status.py`：gateway daemon runtime status 层；`read_runtime_status_report()` 要暴露坏 status JSON、编码错误和非对象根。旧 `read_runtime_status()` 继续返回 payload/None。写入新状态时如果旧状态文件已坏，必须把 `previous_status_load_error` 写进新状态，避免把损坏痕迹覆盖成“没有历史状态”。
- `agent_py_agent/agent/gateway_parts/scoped_locks.py`：gateway 多实例/身份 scoped lock 控制层；lock 文件存在但 JSON 损坏、编码错误、空文件或非对象根时，`acquire_scoped_lock()` 必须返回 `lock_load_error`，不能把坏锁文件伪装成“没有锁”或空 `existing`。坏锁文件不会被自动删除，避免错误扩大成抢锁或释放别人的锁。
- `agent_py_agent/agent/gateway_parts/lease_service.py` / `lease.py`：gateway processing lease 心跳层；`lease.py` 只是旧名字兼容门面，实际状态和刷新逻辑只在 `lease_service`。坏 processing request JSON 必须通过 `refresh_processing_lease_report()` 返回 `load_error`，旧 boolean 入口继续只返回 `False`。坏 request 文件不能被误解释成普通心跳停止，也不能维护第二套 heartbeat 活跃集合。
- `agent_py_agent/agent/gateway_parts/daemon_control.py` / `queue_service.py`：gateway PID record 和 CLI status 层；`get_running_pid_report()` 要暴露坏 PID JSON、编码错误和非对象根，`render_gateway_status()` 要显示 `pid_load_error`。坏 PID record 不能被静默删除或误渲染成普通 `stopped`；纯文本 PID 的旧读取仍只走 `read_pid_file()` 兼容入口。
- `agent_py_agent/agent/gateway_parts/supervisor.py`：gateway supervisor 健康检查层；坏 heartbeat、PID record 或 adapter state JSON 必须进入 `_last_health_load_errors` 并返回不健康，不能把坏 heartbeat 当成启动期、把坏 PID 当成未运行、或把坏 adapter state 当成健康。
- `agent_py_agent/agent/memory_store/jsonl.py` / `_jsonl_indexing.py`：JSONL 记忆事实流水和可选 LocalStore/FTS 索引层；`search_report()` 必须在索引搜索失败时继续 fallback 到 JSONL/daily/fallback 记忆，同时返回 `memory_store.local_store.search` 的 `load_errors`。索引坏不是新硬门，也不能被解释成“没有相关记忆”。
- `agent_py_agent/agent/memory_push.py`：运行中相关记忆注入层；`push_relevant_memories_report()` 必须保留记忆搜索或索引错误，旧 `push_relevant_memories()` 只做兼容文本返回。记忆搜索坏了不能被模型误解成“没有相关记忆”，也不能阻断主任务。
- `agent_py_agent/agent/gateway_parts/http_service.py` / `http_handlers.py`：gateway HTTP 服务线程、状态接口和 result 接口；`serve_forever()` 异常必须记录 `last_error_report` 并写入 `state.server_error`，`/status` 要把该错误带出。`gateway_state.json` 损坏时 `/status` 必须返回 `state_load_error`。pending/processing 请求文件或响应 JSON 损坏时，`/result/<id>` 必须返回 `request_load_error` / `result_load_error`，不能把坏账本吞成 `unknown`、正常 queued/processing 或一段不可机器处理的字符串。
- `agent_py_agent/agent/agent_core/tool_loop/round_subagent_output.py`：子代理 `output.json` 自动收口层；当前 run 或 `output.json` 读取失败必须返回 `[SUBAGENT_RESULT_LOAD_ERROR]`，不能把坏 JSON 补成空结果或假 evidence packet。
- `agent_py_agent/agent/agent_core/subagent/progress_closeout.py`：task-local progress 自动收口层；当前 run 账本或 `progress/latest_tool_progress.json` 读取失败必须返回 `[SUBAGENT_PROGRESS_LOAD_ERROR]`，不能把坏进度快照伪装成没有进度。
- `agent_py_agent/agent/subagents/services/session_progress/`：子代理 task-local 工具进度层；记录新写入进度时，如果上一轮 `latest_tool_progress.json` 损坏，必须在新快照和 continue packet 的 `reserved.load_errors` 里保留 `subagent_tool_progress.previous_progress`，不能把坏历史进度伪装成没有历史进度。
- `agent_py_agent/agent/subagents/services/persistence/recovery_outputs.py` / `compact_continue_packet.py`：子代理恢复输出文件和 task-local continue packet 生成层；`output.json`、session compact metadata 或 latest tool progress 读取失败时写入 `reserved.load_errors`，不能把坏输出/恢复/进度账本伪装成空输出或没有状态。
- `agent_py_agent/agent/agent_core/subagent/compact_continuation.py`：子代理 runner 消费 task-local compact continuation 的 prompt 渲染层；`latest_continue_packet.json` 或 session compact metadata 存在但坏 JSON、编码错误、过大或不是对象时，必须在 prompt 里展示 `packet_load_error` / `metadata_load_error` 的结构化上下文和路径，同时保留 checkpoint/summary fallback。
- `agent_py_agent/agent/subagents/services/recovery/strategy.py` / `recovery/instructions.py`：子代理恢复策略层；`latest_continue_packet.json` 存在但读取失败、编码错误或 JSON 根不是对象时，策略 payload 必须带 `packet_load_error`，runner 提示必须说明坏包上下文和路径，并降级到 checkpoint/summary fallback refs，不能把坏包伪装成普通缺包。
- `agent_py_agent/agent/memory_archive/compact_resume/failsafe.py`：主代理 compact resume 的 fail-safe checkpoint 扫描层；raw/hooks JSONL 中坏行或文件读取失败必须进入 `fail_safe_checkpoint_load_errors`，同一文件里的好 checkpoint 继续返回，不能把归档扫描失败伪装成“没有兜底 checkpoint”。
- `agent_py_agent/agent/memory_archive/compact_resume/io.py`：主代理 compact resume 的 apply artifact 读取层；`apply_bundle`、`restore_refs`、`work_state_snapshot`、`self_check`、`compact_context` 等 refs 读取失败必须进入 `artifact_load_errors`，不能只表现成恢复字段为空。
- `agent_py_agent/agent/memory_archive/compact_resume/__init__.py` / `blocked.py`：主代理 compact resume 的 metadata 入口；metadata 文件存在但坏 JSON、编码错误或不是对象时，blocked payload 必须返回 `metadata_load_error` 和 `blocked_compact_metadata_load_error`，不能和普通缺 metadata 混在一起。
- `agent_py_agent/agent/memory_archive/compact_work_state/archive.py`：主代理 compact apply 的目标/下一步来源扫描层；snapshot 或 raw/archive JSONL 坏行必须进入 `work_state_snapshot.source_load_errors`，compact resume context 必须展示 `Source Load Errors`，不能把来源扫描失败伪装成没有目标或没有下一步。
- `agent_py_agent/agent/task_progress.py` / `agent_py_agent/agent/memory_archive/compact_work_state/sources.py`：长任务进度和覆盖账本层；`progress.json` 损坏、编码错误或 JSON 根不是对象时，`read_task_progress_report()`、`task_progress_summary()` 和 compact `work_state_snapshot.task_progress.load_error` 必须保留结构化诊断。坏进度账本不能被模型误解成“没有进度表”。
- `agent_py_agent/agent/run_intent.py` / `agent_py_agent/agent/tooling/_filesystem_write.py`：运行意图和参考目录写入软提醒层；runtime fact `task.json` 损坏时必须通过 `latest_run_intent_report()` 和 write_file `soft_feedback.run_intent_load_errors` 暴露诊断，不能静默退化成“没有目标路径/参考目录”。compact work_state 汇总运行意图时，坏 task.json 也要进入 `run_intent.load_errors`。
- `agent_py_agent/agent/session/resume.py`：会话恢复上下文层；`memory.jsonl` 文件读取失败、编码错误、坏 JSONL 行或非对象行必须进入 `recent_memory_load_errors` 并在 `format_resume_context()` 中展示“最近记忆读取警告”。坏最近记忆不能被解释成“暂无历史记录”。
- `agent_py_agent/agent/conversation/store_claims.py` / `conversation/runtime_context.py`：后台主代理 claim 账本层；`background_claim.json` 损坏、编码错误或不是对象时必须进入 `claim_load_error`，并出现在后台恢复快照里。坏 claim 不能被后台唤醒误解成“当前没有 claim / 没有接管状态”。

### 产物和证据合同

这些模块负责“文件存在”之外的通用验收，不写具体任务专项规则：

- `agent_py_agent/agent/contracts/artifact_acceptance.py`：产物总验收入口。
- `agent_py_agent/agent/contracts/artifact_csv_acceptance.py`：CSV 表格结构。
- `agent_py_agent/agent/contracts/artifact_xlsx_*`：XLSX 结构和证据。
- `agent_py_agent/agent/contracts/artifact_binary_signature.py`：二进制文件签名。
- `agent_py_agent/agent/contracts/staged_checkpoint_acceptance.py`：阶段 checkpoint。
- `agent_py_agent/agent/contracts/evidence_contract.py`：证据来源、claim、verified 状态。
- `agent_py_agent/agent/contracts/artifact_collection_*`：集合类产物字段、分组、证据。

### 工具韧性合同

这些模块负责让工具失败、大输出和副作用行为在进入模型上下文前变成结构化事实：

- `agent_py_agent/agent/tooling/registry_execution.py`：统一工具入口，先过 runtime gate，再执行工具。
- `agent_py_agent/agent/tooling/registry_runtime_gate_pipeline.py`：工具执行前唯一 pipeline；重复工具/无进展历史、路径/URL/command、限流、幂等、审批和副作用都在这里汇总成统一 `GateDecision`。
- `agent_py_agent/agent/agent_core/tool_guard/call_guardrail.py`：工具循环历史记录器，只保存 `tool_guardrail_records` 和软提示，不再作为独立 block 入口。
- `agent_py_agent/agent/tooling/registry_resilience.py`：只读工具可有限重试，大输出落 artifact ref，mutating/dangerous 仍由幂等和审批门约束。
- `agent_py_agent/agent/contracts/gates/command/policy.py`：共享命令策略；普通工作区清理不靠命令名硬拒，灾难级删除、裸盘写入、格式化和关机重启仍硬拒。`run_command` 和 shell gateway 复用同一套判断，避免两边策略漂移。
- `agent_py_agent/agent/artifacts/shell_protection.py`：`run_command` 前后保护统一 artifact registry 里的 ready 产物；执行前备份，执行后按存在性、大小、hash 和客观格式 lint 更新同一 `artifact_id` 的 ready/invalid 状态。
- `agent_py_agent/agent/artifacts/registry.py`：统一 artifact registry；支持单文件产物和 file group 逻辑产物，file group 用同一个 `artifact_id` 指向多个成员文件。
- `agent_py_agent/agent/agent_core/delivery_closeout/artifacts.py`：closeout 只把 `data/artifacts/registry.jsonl` 当作产物事实账本；`.agent_delivery/closeout.json` 只是验收报告快照，不是产物账本，旧 `artifacts_manifest.json` 不再作为权威输入。
- `agent_py_agent/agent/tooling/_filesystem_write.py`：通用 `write_file` 原子写入，支持文本 `content` 和二进制 `data_base64`。
- `agent_py_agent/agent/tooling/_filesystem_patch.py`：通用 `apply_patch` 文本补丁，替代 append/replace/session 等多套专项写入工具。
- 已删除旧专项写入/构建工具：`append_file`、`replace_in_file`、`file_write_session`、`write_structured_json`、`data_to_workbook`、`markdown_to_pdf`。复杂格式由模型选择脚本/命令/库生成，系统只保留通用写入、路径边界和最终收口。

### 离线测试合同

这些模块用于 fake model/fake tool/replay，不应进入真实任务专项逻辑：

- `agent_py_agent/agent/contracts/offline_*_contract.py`
- `agent_py_agent/agent/contracts/dry_run_mainline_contract.py`
- `agent_py_agent/agent/contracts/shadow_mode_contract.py`
- `agent_py_agent/agent/contracts/failure_sample_library_contract.py`

### 主代理真实任务合同

这些文件仍处于迁移期，当前存在 `main_agent_task_*` 和 `main_agent_real_task_*` 双轨历史：

- `main_agent_task_*`：当前 CLI 已优先使用的通用任务合同入口。
- `main_agent_real_task_*`：真实任务旧命名和兼容入口。

当前不能直接大删，因为同名文件并非全部完全一致，且测试仍覆盖两套入口。长期目标是：

1. 先把公共逻辑收进通用模块。
2. 让旧命名只做 thin wrapper。
3. 等兼容测试和真实任务回放稳定后再删除旧实现。

### 子代理合同

子代理仍然要复用主代理底座，不能长出另一套规则：

- `agent_py_agent/agent/subagents/context_bundle_contracts.py`
- `agent_py_agent/agent/contracts/task_tree_ledger_contract.py`
- `agent_py_agent/agent/agent_core/orchestration_*`

### 通用解析 helper

`agent_py_agent/agent/common/value_parsing.py` 是模型/工具参数解析的薄入口：

- `string_list()` 通过 `StringListOptions` 支持 JSON list 字符串、多行列表、逗号列表和普通字符串。
- `bool_value()`、`positive_int()`、`non_negative_int()` 统一布尔和数字解析。
- `dict_value()`、`dict_values()` 统一开放世界 payload 的 dict/list[dict] 解析。

新增控制面或工具模块不要再各自复制 `_string_list`、`_bool_param`、`_positive_int` 这类 helper。
旧模块可以在被修改时逐步迁移，不做全仓机械替换，避免把合同语义一起改乱。

## 读代码顺序

1. 先看 `error_taxonomy.py` 和 `state_machine.py`，理解错误如何变成状态/恢复动作。
2. 再看 `tool_protocol_v2.py` 和 `gates/`，理解工具调用如何在入口被约束。
3. 再看 `artifact_acceptance.py`、`staged_checkpoint_acceptance.py`、`evidence_contract.py`，理解产物和证据如何验收。
4. 再看 `contract_status.py`、`contract_trace.py`、`effective_contract_snapshot.py`，理解调试、回放和快照。
5. 最后看 `main_agent_task_*` / `main_agent_real_task_*`，这些是集成层，不应作为新合同设计的起点。

## 开发铁律

- 代码不得依赖普通自然语言文本作为机器事实来源。
- 失败必须有结构化 finding、error code、可恢复动作或明确 blocked 状态。
- 合同失败优先进入返工循环，只有权限、越界、审批拒绝、不可恢复损坏等情况才终止。
- 新增合同先写离线测试，再接运行门，最后才跑真实 LLM。
- 真实任务中的网页、论文、GitHub、Excel 等要求只能进入测试 fixture 或 runtime contract 数据，不进入代码专项判断。
- 每次开发必须同步更新文档；运行语义、合同门、配置、工具行为或验收流程变了，同一轮必须更新 `DESIGN_LEDGER.md`、`docs/design/`、`CODEBASE_TREE.md` 或对应说明。

## 2026-05-25 协作控制面状态更新

最近多代理协作控制面的详细说明见 `docs/design/main-agent-runtime-gates.md` 第 17 节。

当前关键结论：

- 已删除旧 orchestration_contract 最终回答门：协作请求不再被入口物化成专门硬合同，也不再因为没有执行某个协作工具而本地阻断最终回答。
- 已删除旧 root 控制面硬边界：当前轮 child runs 存在时，系统不再用父级检查门替主代理判断是否完成；主代理通过 tree/refs/dispatch 状态继续调度或汇报。
- `inspect_collaboration` 展示请求、证据、`case_window` 和 `collection_result`。它只告诉上级谁已回、谁没回、谁不可达、证据 refs 在哪；旧 `rework/rework_targets` 已删除，协作控制面不再生成额外返工门。
- `dispatch_subagents` 的显式目标字段只接受 `run_ids`，`direct_children=true` 表示当前作用域的直接孩子；顶层给出目标 ID 时默认真实推进 runner。同批目标不再按 worker/coordinator 偷偷拆两波，系统按父级给定顺序和并发参数执行；真要流水线，父级应先派 A、看 A 完成、再派 B。
- 调查/查询/监控类 leaf worker 可以只交付结构化 `evidence_refs`。父级空测试报告判定会把非内部 evidence ref 视为可 inspect 的事实源；顶层 `evidence_refs/artifact_refs` 会在 parser 层补成 refs-only evidence packet，避免证据丢失。
- `create_subagents` item 里出现明确资料文件路径但模型漏填 `required_read_paths` 时，系统会把这些路径补进 context manifest，并在 runner context 中作为 `allowed_read_roots` 传给工具 path gate。它只授权读取，不进入写入根或产物根。
- `context_manifest` 仍可携带 refs-only 短写列表或字符串，并归一成 `required_read_paths` 给 runner prompt 和读授权使用；`read_file:/path/to/a.txt` 这类“工具动作+文件路径”会归一成真实文件路径。但这些 refs 不再是 runner 启动硬依赖，缺失时不会产生 `input_dependencies_skipped` 或 `input_materialization_recovery`。
- 最终收口里的 `file_exists/path_exists/artifact_exists` 是通用存在性别名，会归一成 `file_check`；缺 `file_path` 时只从同一 `output.artifacts[].path` 的机器字段展开，不从自然语言猜路径。
- `context_manifest` 对象现在也是开放 refs carrier：对象任意值里的文件 ref 会进入 read refs 提示和读权限辅助，不再依赖 `source_file/alert_file` 这种字段名白名单，也不再作为启动依赖过滤 runner。
- read refs 会消除“完整路径 + 同名短写”的重复噪音：如果同一结构化输入里已有 `/.../source_01.txt`，裸 `source_01.txt` 不会再生成一条重复提示。
- 当前子代理自己的输出 refs 不会变成自己的读线索；同批其他子代理的输出 refs 如果被父级显式放进 item read refs，会保留为普通读线索。路径不存在不会阻断 runner；如果确实需要 B 等 A，应由父代理在 A 完成后再派 B，而不是让 create 阶段替模型猜流水线。
- `goal` 里的文件路径只进入 `hint_read_paths`，作为可读提示和工具授权，不作为启动硬依赖；硬依赖只来自结构化输入字段。
- `create_subagents` 的写根授权有一个窄的结构化继承：同一参数包里，输出 ref 与真实存在的输入 ref 共享足够窄任务目录时，输出文件父目录会被加入 child `extra_write_roots`，使临时任务目录可以正常交付；这不是 prompt 解析，也不会把 `/`、home 或系统目录授权出去。
- `write_boundary.allowed_read_roots` 会进入文件工具的临时 workspace roots。读授权和写授权在 registry invoke 层保持一致，不再出现 runner context 显示可读但 `read_file` 实际拒绝的双轨。
- 已废弃：`pending_dispatch_redirect.v1` 不再作为 `create_subagents` 的拦截路径。创建子代理不会因为当前轮还有未 dispatch 的 run 而拒绝追加新子代理；是否继续派工、是否先推进旧 run，交给父代理根据 tree/board 状态判断。
- 已废弃：全局 `subagent_workflow_mode=auto` 不再默认套到普通子代理。workflow 必须由本次工具调用显式请求，避免 leaf worker 被自动拆成 implement/verify 孙代理后又被正文读取 guard 卡住。
- `DONE` / `VERIFIED` 是统一状态机里的 `VERIFYING`，`current_turn_run_state.pending_closeout_run_ids` 会提示跑验收路径，不再落入未处理 `manual_review`。如果目标 run 已经等待收口，`dispatch_subagents(dry_run=false, run_ids=[...])` 会默认进入验收-only 续推：不重复 runner，执行最终收口，并自动应用验收 follow-up。
- `create_subagents` / `schedule_child_subagents` 预检现在和 runner 写边界对齐：`output_files` 是产物目标，`extra_write_roots/write_roots/target_roots` 是显式写入根；目标落在显式根下可以创建，普通 goal 文本不能授予写权限。
- `CollaborationStore.overview()` / `my-agent collaboration overview` 提供只读 readiness 体检，方便真实复杂任务前确认是否还有 ready case、阻塞请求或缺证据请求等待主代理处理。
- 后台主代理 scheduler 会合并同一个 thread 的多条 wake signal，同一轮只叫醒一次主代理。
- 协作状态和工具协议都保持开放世界：状态名、能力名、实体 key 和工具 input 字段不靠封闭枚举硬拒。

## 2026-05-24 运行门状态更新

最近主代理运行门的详细说明见 `docs/design/main-agent-runtime-gates.md`。

当前关键结论：

- 网络工具设计见 `docs/design/network-tools.md`。主入口是 `web_search`、`web_fetch`；旧 `fetch_url` 入口已撤掉。网络安全只守 URL/DNS/metadata/私网等运行时边界，不承担交付质量或流程前置门。
- bootstrap 开工物化硬门已删除。它不是安全门，也不是最终收口门，不能再拦截普通 `web_search`、`web_fetch`、`read_file`、`list_files`。
- 交付验收现在只支持显式 `submit_for_acceptance` 或外部显式验收入口；普通最终回复不触发交付验收。
- delivery contract Doctor 只返回结构化诊断提示；同一坏机器合同不会再输出 `DELIVERY_CONTRACT_DOCTOR_BLOCKED`，也不会阻断普通交付。
- provenance、delivery quality payload、fact evidence payload、事实口径这类元数据质量问题默认进入 closeout 报告的 warning/evidence，不参与硬放行；只有外部显式结构化合同声明 `enforcement: required` 时才会阻断。
- 探索熔断和 closeout 返工预算改成配置化；本地进展门只做配置化软提醒，不再阻断任务；delivery repair 独立运行门已删除，返工提示统一由 closeout 的 `[delivery-contract-check]` 和 `repair_guidance` 承担；数字字段统一遵守 `0` 表示不按次数阻断。
- 质量问题继续走 closeout / repair / replay 闭环，不新增“必须先写某个专项中间文件”的前置硬门。
