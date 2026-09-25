# P5-C 交付质量提示：只读接缝审计

状态：只读审计；没有接线、真实 Jev 调用或质量效果验收。基线为本隔离 worktree `ef497a904` 加当前共享未提交工作；本文件不代表主线已实现交付质量点。

## 原权威链与不能越过的边界

- `agent/verification/runtime.py::record_tool_verification` 在公共工具出口被动记录真实 `run_command` 的分类、退出码、范围，以及成功写入后旧验证的 stale 状态；事实附在原 `ToolResult.metadata.handler_details`。`agent/verification/repository.py` 的 owner/thread/task 证据账本是权威，提示或模型正文不能新造验证事件，也不能把 targeted 说成全仓通过。`agent/agent_core/tool_context/reducer.py::render_tool_result_for_live_prompt` 已将这些事实以 `[runtime-verification-facts]` 放在不可信工具正文之外；`agent/tooling/operation_verification.py::render_current_turn_execution_facts` 又提供最近工具批次的有界结构化执行事实，最终 `build_operation_verification` 只从原归档统计副作用状态。
- `agent/agent_core/tool_loop/response_decision.py::_no_tool_calls_decision` 决定无工具响应是否自然结束，已有空回复、未知副作用、截断与子代理声明交付物的独立处理。`tool_loop/deliverable_closeout.py::deliverable_closeout_block` 仅对 `task_local/control_plane` 中宿主已声明的路径做有界存在性检查；主会话不适用。`agent/agent_core/_finalization_service.py::FinalizationService.finalize` 随原终态保存会话、工具和用量。`agent/conversation/closeout.py::decide_closeout` 从结构化状态决定继续/停止。Jev 分数、文字判断或缺席不得进入这些函数作为新门，不得增加模型续跑或改写原 final。
- `agent/conversation/goal_tools.py::UpdateGoalTool.execute` 以精确 Goal/task、revision 和原工具权限写 `complete/blocked`；暂停/恢复仍归用户/系统。`agent/agent_core/subagent/finalize_helpers.py::record_finalized_runner_result` 只由 typed `turn_end_reason` 映射 child 生命周期，`DONE` 不等于质量验收。`agent/subagents/result_registered_artifacts.py::collect_registered_artifacts` 只从本 run 的 no-follow 产物注册表取 ready 文件，排除工具输出归档、删除和其它 run。Jev 不得据建议制造/晋升 artifact ref，不得代替父代理验收或改变子代理状态。
- `docs/modules/delivery/01-closeout.md` 明确普通任务由模型依据原结果自然交付；Todo、验证、产物用于判断和如实报告，不是普通完成硬门。`docs/design/DECISION_MODEL_INTEGRATION.md` 已把此点定义为“当前产物引用与原工具证据 → 主模型复核上下文”，故这里应是提示，不是新的裁判。

## 最小安全接缝

建议首片只在**原工具已经执行并完成归档后**，以 `agent/agent_core/_tool_loop_service.py::_record_tool_call` 的 `archive_record`、canonical `ToolResult` 和 `params` 为同一冻结来源，调用独立 `tool_context/decision_delivery_quality.py`；与现有 `external_material_order_hint` 相同，最多把一段短提示追加到 `result_rendered`，text/native 都消费同一段。原结果、状态、refs、历史、权限和收口调用顺序不变；关闭时不准备材料、不发请求。不要在 `_no_tool_calls_decision` 或 `FinalizationService.finalize` 接入：那里已经拿到最终答复，再咨询会产生额外回合或迟到改写。

首片候选只考虑当前工具回执中已有的、结构化且可绑定的**复核焦点**：例如 `handler_details.verification_evidence.id/status/scope`、`verification_state.last_verification_id/status`，或原归档/本 run 注册表里已 ready 的 exact artifact ref。候选是给主模型选择“先核对哪项”的锚点，不是质量等级、完成布尔值、测试命令生成器或待执行操作。输入应限制为最少的 owner 安全投影、当前请求的有界片段、候选 ID 与状态/范围；不上传本地绝对路径、原工具输出、完整 artifact 内容、密钥或未授权子代理数据。来源是 external_data 时沿原脱敏和不可信数据边界；无法安全投影直接放弃增强。只有至少两个有意义的合法候选、且当前模型下一轮确实还能消费工具结果时才值得发 Jev 请求；单个 stale 状态由现有事实提示即可，不需让 Jev 重做确定性判定。

合法 `apply` 只能选**一个本轮 exact 候选 ID**，渲染成“可先复核这个证据/引用；其状态与范围仍以原事实为准”的有限文字。`observe` 请求和计量照常，但不显示建议；`off` 零 Jev、零额外持久化；`not_needed/no_match/abstain/need_data`、回答缺项/重复/越界、普通错误及超时均只返回空提示。`need_data` 不补读 artifact、不自动跑测试、不询问用户逐次操作。用户取消继续传播，不能吞成普通故障。复用 `conversation/decision_service.py` 的点设置、阶段绝对期限、消费复核与用量账；采用前重读当前配置/权限/身份、候选来源和归档 hash/revision，过期则丢弃。原工具结果和原 final 不等待“质量合格”，只受当前可调短期限影响。

首片还需确认 `archive_record` 中 artifact ref 的 ready/owner/run 来源是否足够；不够就先只用 verification 候选，不能把 display 字符串、模型报称文件或 archive blob 当产品产物。较长交付物的语义质量、用户要求是否完全覆盖、图片/文档可读性、测试充分性，现有结构化候选均不能证明，Jev 的主观评分不能作为运行合同。真实效果只能通过主模型是否作出更准确的复核/如实报告来观察，不用分数驱动 `update_goal`、Todo、派工、授权或 artifact 状态。

## 对照项目与核对范围

- 已查本机参考副本 hermes-agent 的 `agent/verification_evidence.py` 的被动证据语义、`agent/verification_stop.py` 的 edit→verify-on-stop 有界续跑、`agent/turn_finalizer.py` 的已有答复保存。前者与本仓库可复用；后两者提供“额外续跑可能挤掉答复”的反例，本点不复制 verify-on-stop 门。
- 已查本仓库 `docs/modules/verification/04-structure.md`、`docs/modules/delivery/01-closeout.md`、`docs/modules/subagent/04-structure.md` 的相关段、上述源码和测试。没有逐行审查整个 Hermes、Codex 或其他参考项目，也没有核对它们所有版本或外部最新实现；对照只支撑上述边界，不代表全面优劣判断。

## 定向证据、仍缺条件与所有权

只读执行 `python3 -m pytest -o addopts='' agent_py_agent/tests/test_verification_runtime.py agent_py_agent/tests/test_verification_repository.py agent_py_agent/tests/test_deliverable_closeout_gate.py agent_py_agent/tests/test_subagent_registered_artifact_handoff.py agent_py_agent/tests/test_closeout_machine.py agent_py_agent/tests/test_conversation_goal_tools.py -q --tb=short`：`4931 passed`。这些测试验证现有原链，不证明 Jev 提示效果；本审计无生产代码，因此 Ruff 不适用。

实施时应加独立 `delivery_quality` 默认 off 配置（YAML/dataclass/schema/逐点设置/TUI 同源），以及 focused fake 决策测试：off 零请求、observe 零展示、合法 exact ID、所有非选择、格式错/重复/过期/超时、取消传播、并发来源或设置变化、text/native 同段、原验证/产物/Goal/child 状态不变。再做受控真实 Jev + 原主模型对照，记录输入 token、阶段时延和主模型是否正确引用原测试范围/产物，不能拿 fake 通过冒充真实质量增益。

所有权建议：一个实施者仅负责新 `decision_delivery_quality.py`、`_record_tool_call` 的窄 hook、逐点配置和 focused tests；共享设计台账、Goal、测试总览与文件树由主线整合。若 artifact ready 来源难以精确消费，第一片缩到 verification exact IDs；若仍不能构造至少两个可靠候选，先保留审计，不为“接入数量”发无价值请求。

建议下一步：先做候选来源/ready 状态的最小只读投影与 fake 决策实验，确认能给主模型增加有用且不重复的复核线索，再接默认关闭的软提示。此片适合与模型选择或自学习链并行，但不得同时修改 `_tool_loop_service.py::_record_tool_call`、配置 schema 或共享文档；原收口、Goal、artifact registry 和权限继续归各自现有权威链。
