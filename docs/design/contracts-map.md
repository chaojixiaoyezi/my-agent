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
- `agent_py_agent/agent/contracts/recovery_actions.py`：恢复动作枚举。
- `agent_py_agent/agent/contracts/tool_protocol_v2.py`：工具调用归一化；旧 flat tool call 里的业务字段保持开放世界，不把 input 里的 `status` 误判成协议状态枚举。
- `agent_py_agent/agent/contracts/gates/`：工具、路径、审批、幂等、交付质量等运行门。
- `agent_py_agent/agent/contracts/delivery_contract_doctor.py`：入口级 delivery_contract 自检，负责 schema、路径边界、开放世界扩展声明和返工动作。
- `agent_py_agent/agent/contracts/effective_contract_snapshot.py`：最终生效合同快照。
- `agent_py_agent/agent/contracts/run_trace_contract.py`：运行 trace 结构。
- `agent_py_agent/agent/contracts/contract_status.py`：合同失败状态汇总。
- `agent_py_agent/agent/contracts/contract_trace.py`：finding 调试链。
- `agent_py_agent/agent/agent_core/delivery_requirement_materializer.py`：从普通用户需求物化开放世界 delivery/orchestration 合同。
- `agent_py_agent/agent/agent_core/tool_loop_orchestration_contract_decision.py`：显式协作请求的最终回答返工门，检查真实 orchestration 工具事实。
- `agent_py_agent/agent/agent_core/orchestration_shared_context.py`：父级小型读取 brief 进入子代理 `context_packs` 的通用桥接层；工具刚成功返回时可直接缓存，归档扫描作为补充，只传摘要和 refs，不写业务专项字段。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_refs.py`：父级调度结果索引层；从本轮 touched run 和 runner-created child 汇总状态、摘要、`output_json` 和产物 refs，供 `dispatch_subagents`/`subagent_board` 在长记录前先展示关键机器事实。
- `agent_py_agent/agent/agent_core/orchestration_sibling_roster.py`：同批子代理身份索引层；批量创建后把 peer `run_id/name/role/goal` 写入 `sibling_roster` context pack，解决同批兄弟彼此不可见的问题。它只提供索引，不发布未来产物路径，也不制造等待关系。
- `agent_py_agent/agent/agent_core/orchestration_create_items.py`：批量子代理参数解析层；只把 `items` / `tasks` 解析成独立任务 bundle，不再拒绝 sibling 共享输出，也不再从输入/输出路径推断批次依赖。父级显式给 item 的 read refs 会原样保留为读线索；路径不存在不会卡启动。
- `agent_py_agent/agent/agent_core/runner_ref_fields.py`：runner 路径字段解析 helper；只提取显式输入/输出 refs 供提示、写根和报告使用，不再因为 `required_read_paths` 缺失或不存在而跳过 runner。真实缺文件由子代理运行时工具结果返回给模型处理。
- 已删除旧输入物化工具：`subagent_input_materialization.py` / `materialize_subagent_inputs` 不再存在。父代理要么在 prompt/结构化参数里给清楚路径，要么让子代理运行后自己报告缺文件；系统不再自动复制“父级可读文件”来修补启动门。
- `agent_py_agent/agent/agent_core/runner_prompt_context_summary.py`：runner 上下文摘要层；把 targeted collaboration request 的结构化线索包渲染给响应代理。
- `agent_py_agent/agent/agent_core/agent_tree_status.py`：代理树只读状态模型；供 `inspect_agent_tree`、watch 和后台主代理读取 task/run/parent/depth/current tool/progress/artifacts/blockers。
- `agent_py_agent/agent/agent_core/services/watch_service.py`：watch 观察/推进边界；默认只观察代理树，显式 `advance` 才调用 dispatch。
- `agent_py_agent/agent/subagents/services/takeover_run.py`：超时/断连 run 的接管创建层；接管 run 必须继承原 run 的 `quality_contract`、`context_manifest`、`context_packs`、`output_files/output_refs`、artifact/evidence refs 和写入根，只重写 `takeover_*` 审计字段。复用旧 takeover 时也会补齐缺失交接字段，避免恢复链把“该读什么、该写哪儿、兄弟是谁”丢掉。
- `agent_py_agent/agent/subagents/runner_rendering.py`：子代理 prompt 渲染层，Context Pack 使用开放字段小型展示，避免新字段落盘但不进模型上下文。
- `agent_py_agent/agent/collaboration/store.py`：协作 case/request/evidence append-only 账本；case 推荐 `open/close` 窗口语义，多目标 request 按 responder 身份逐个闭环，deadline 到点后暴露 timed out、missing responders 和 unavailable targets，允许带部分证据继续推进。
- `agent_py_agent/agent/collaboration/tools.py`：协作工具入口；包含 `deadline_seconds`、refs-first evidence、结构化 request lifecycle、目标 run_id 解析、不可达目标回执和 case status。
- `agent_py_agent/agent/conversation/`：长期 thread、message、observation、wake signal 和后台主代理唤醒 runtime；本地 internal channel 可在无飞书/微信时测试同一套会话语义。

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
- `agent_py_agent/agent/tooling/registry_resilience.py`：只读工具可有限重试，大输出落 artifact ref，mutating/dangerous 仍由幂等和审批门约束。

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

- 显式协作请求现在会物化为 `orchestration_contract.v1`，并在 root 最终回答前检查 `create_subagents` / `dispatch_subagents` 等真实工具记录；没有满足时返回结构化返工提示，而不是让 root 自己读完后口头说完成。默认 `execution_required=true`，所以“创建子代理记录”不等于“子代理已执行”；只有外部结构化 case 明确 `execution_required=false` 时才允许只规划/只建记录。
- 已删除旧 root 控制面硬边界：当前轮 child runs 存在时，系统不再用“父级禁止读正文/禁止写产物/派工前禁止读 source”这类运行门卡住模型。是否由父级自己看文件、写汇总，交给普通用户 prompt、任务树状态和最终 closeout 验收处理。
- `case_status` 不只展示请求和证据，也会输出 `rework/rework_targets`，把阻塞请求和缺证据请求转成通用返工目标。
- `dispatch_subagents` 的显式目标字段接受 `run_ids/include_run_ids/subagent_ids/target_subagent_ids/target_run_ids/agent_ids/child_run_ids/children/items[].run_id`，`direct_children=true` 表示当前作用域的直接孩子；顶层给出目标 ID 时默认真实推进 runner。这是工具协议容错，不是业务任务模板。同批目标不再按 worker/coordinator 偷偷拆两波，系统按父级给定顺序和并发参数执行；真要流水线，父级应先派 A、看 A 完成、再派 B。
- 调查/查询/监控类 leaf worker 可以只交付结构化 `evidence_refs`。父级空测试报告判定会把非内部 evidence ref 视为可 inspect 的事实源；顶层 `evidence_refs/artifact_refs` 会在 parser 层补成 refs-only evidence packet，避免证据丢失。
- `create_subagents` item 里出现明确资料文件路径但模型漏填 `required_read_paths` 时，系统会把这些路径补进 context manifest，并在 runner context 中作为 `allowed_read_roots` 传给工具 path gate。它只授权读取，不进入写入根或产物根。
- `context_manifest` 仍可携带 refs-only 短写列表或字符串，并归一成 `required_read_paths` 给 runner prompt 和读授权使用；`read_file:/path/to/a.txt` 这类“工具动作+文件路径”会归一成真实文件路径。但这些 refs 不再是 runner 启动硬依赖，缺失时不会产生 `input_dependencies_skipped` 或 `input_materialization_recovery`。
- 父级验收里的 `file_exists/path_exists/artifact_exists` 是通用存在性别名，会归一成 `file_check`；缺 `file_path` 时只从同一 `output.artifacts[].path` 的机器字段展开，不从自然语言猜路径。
- `context_manifest` 对象现在也是开放 refs carrier：对象任意值里的文件 ref 会进入 read refs 提示和读权限辅助，不再依赖 `source_file/alert_file` 这种字段名白名单，也不再作为启动依赖过滤 runner。
- read refs 会消除“完整路径 + 同名短写”的重复噪音：如果同一结构化输入里已有 `/.../source_01.txt`，裸 `source_01.txt` 不会再生成一条重复提示。
- 当前子代理自己的输出 refs 不会变成自己的读线索；同批其他子代理的输出 refs 如果被父级显式放进 item read refs，会保留为普通读线索。路径不存在不会阻断 runner；如果确实需要 B 等 A，应由父代理在 A 完成后再派 B，而不是让 create 阶段替模型猜流水线。
- `goal` 里的文件路径只进入 `hint_read_paths`，作为可读提示和工具授权，不作为启动硬依赖；硬依赖只来自结构化输入字段。
- `create_subagents` 的写根授权有一个窄的结构化继承：同一参数包里，输出 ref 与真实存在的输入 ref 共享足够窄任务目录时，输出文件父目录会被加入 child `extra_write_roots`，使临时任务目录可以正常交付；这不是 prompt 解析，也不会把 `/`、home 或系统目录授权出去。
- `write_boundary.allowed_read_roots` 会进入文件工具的临时 workspace roots。读授权和写授权在 registry invoke 层保持一致，不再出现 runner context 显示可读但 `read_file` 实际拒绝的双轨。
- 已废弃：`pending_dispatch_redirect.v1` 不再作为 `create_subagents` 的拦截路径。创建子代理不会因为当前轮还有未 dispatch 的 run 而拒绝追加新子代理；是否继续派工、是否先推进旧 run，交给父代理根据 tree/board 状态判断。
- 已废弃：全局 `subagent_workflow_mode=auto` 不再默认套到普通子代理。workflow 必须由本次工具调用显式请求，避免 leaf worker 被自动拆成 implement/verify 孙代理后又被正文读取 guard 卡住。
- `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE` 是统一状态机里的 `VERIFYING`，`current_turn_run_state.awaiting_acceptance_run_ids` 会提示跑验收路径，不再落入未处理 `manual_review`。如果目标 run 已经等待验收，`dispatch_subagents(apply=true, run_ids=[...])` 会默认进入验收-only 续推：不重复 runner，执行父级验收，并自动应用验收 follow-up。
- `create_subagents` / `schedule_child_subagents` 预检现在和 runner 写边界对齐：`output_files` 是产物目标，`extra_write_roots/write_roots/target_roots` 是显式写入根；目标落在显式根下可以创建，普通 goal 文本不能授予写权限。
- `CollaborationStore.overview()` / `my-agent collaboration overview` 提供只读 readiness 体检，方便真实复杂任务前确认是否还有 ready case、阻塞请求或缺证据请求等待主代理处理。
- 后台主代理 scheduler 会合并同一个 thread 的多条 wake signal，同一轮只叫醒一次主代理。
- 协作状态和工具协议都保持开放世界：状态名、能力名、实体 key 和工具 input 字段不靠封闭枚举硬拒。

## 2026-05-24 运行门状态更新

最近主代理运行门的详细说明见 `docs/design/main-agent-runtime-gates.md`。

当前关键结论：

- bootstrap 开工物化硬门已删除。它不是安全门，也不是最终验收门，不能再拦截普通 `web_search`、`fetch_url`、`read_file`、`list_files`。
- 交付验收现在支持显式 `submit_for_acceptance` 和无工具最终回复触发的隐式验收。
- delivery contract Doctor 首次返回结构化返工上下文，若同一坏机器合同继续不可运行，则确定性 `DELIVERY_CONTRACT_DOCTOR_BLOCKED`，避免入口合同损坏时无限循环。
- 探索熔断、本地进展门、closeout 返工预算都改成配置化；delivery repair 独立运行门已删除，返工提示统一由 closeout 的 `[delivery-contract-check]` 和 `repair_guidance` 承担；数字字段统一遵守 `0` 表示不按次数阻断。
- 质量问题继续走 closeout / repair / replay 闭环，不新增“必须先写某个专项中间文件”的前置硬门。
