# REFACTORING BACKLOG

LLM: keep this file current. Do not copy old split plans back in.

当前方向：

- 不按行数强拆文件。
- 优先合并只转发调用、隐藏主链路、保留历史路径名或历史字段的层。
- 只在一个文件同时承担无关职责时拆分。
- 每次改主链路后运行对应 focused tests，再刷新 `CODE_SIZE_REPORT.md`。

---

## Active Items

## Completed Cleanup

- 2026-06-10: 阶段2 字符串判断清零 + 阶段6 子代理参数统一。
  - `contracts/recovery.py`：5 组散落的错误码前缀规则（repairable/recovering/hard_stop/
    category/recommended_action）收敛为单一 `CodePolicy` 注册表（精确码 > 最长家族前缀 >
    fail-closed），187 码 × 13 状态等价校验 0 差异；finding 显式声明的 recommended_action/
    category（当前协议枚举值）优先于推导；信封全 blocked 时不再被门状态兜成 repair_required。
  - `contracts/state_machine.py`：can_dispatch/can_repair/can_closeout 对协议错误状态
    fail-closed（未知状态不再是"可修复的 BLOCKED"）。
  - 新钉子测试 `test_recovery_code_policy.py`：未知码 fail-closed、精确码优先、声明覆盖、
    classify_error 仅限自检模块、协作 raw_* 审计字段只写不读。
  - 阶段6：子代理 runner 复用主代理同一 agent 对象（合同测试钉死，禁自建 backend/config）；
    thought/plan 默认模板归一到 `runner/prompts.py` 单一权威；新增 capability 配置
    `subagent_compact_trigger_percent`（0=继承主代理，>0 仅作用于 task_local 回合）。
  - 评估后保留：`explicit_root_allowed_tools`（spawn 时增补）与 `allowed_tool_set`
    （runtime 集合化）属不同层职责，非重复实现；`recovery_mode_from_protocol_value`
    未知值→MANUAL_REVIEW 是正确的 fail-closed；collaboration `_unavailable_reason`
    比较的是协议常量。

- 2026-06-10: 第二批 facade/碎片合并（阶段1，详见 docs/modules/*/04-structure.md）。
  - `contracts/gates/` 打平：command/artifact/network/document/tool 五个子包并入单层模块
    （`command_policy.py`、`artifact_gate.py`、`artifact_provenance.py`、`network_safety.py`、
    `document_content.py`、`tool_*.py`），positions/address_projection/content_extractors 并入唯一消费者；
    `gates/__init__.py` 155 行转发枢纽清空，15 个调用方直连权威模块。
  - `subagents/services/` 三个单模块包打平为 `capability_service.py` / `runner_context_service.py` /
    `runner_result_service.py`；`services/__init__.py` 11 个 re-export 删除。
  - `delivery_closeout/`：三个 `*_repair.py` 并入 `repairs.py`；`recovery_models.py`+`config.py` 并入
    `models.py`；`source_checkpoint.py` 并入唯一消费者 `staging_recovery.py`。
  - `orchestration/`：create_target_roots→create_context、create_idempotency→create_constraints、
    create_items→create_payload、create_conversation→create_policy（8 文件→4）；顶层 init 枢纽清空。
  - `memory_archive/compact_context_bundle/` 包并入单模块，导入路径不变。
  - 模块级真循环清零：gates.delivery_quality↔staged_checkpoint（claims/source_refs 归位
    evidence_contract）、log_analysis models/contracts 尾部 re-export、parsing/hierarchy/services
    init 转发，共 6 处。
  - 已验证：focused pytest、compileall、doc sync、offline contract matrix、code-size strict 0 hard/0 high-risk。

- 2026-06-06: 删除第一批只转发/影子入口。
  - `delivery_contract_prompting_recovery_bool.py` 并入唯一调用方 `delivery_contract_prompting_staged.py`。
  - `coordinator_seed_tools.py` 并入 `orchestration/create_policy.py`。
  - `orchestration/runner_instruction.py` 并入 `orchestration/dispatch/tool.py`。
  - `tooling/filesystem_write.py` 删除，测试和调用改走 `tooling/filesystem.py` 主入口。
  - `cli/memory_commands.py` 删除；实际 Python 导入一直走 `cli/memory_commands/__init__.py`，该文件只是同名影子入口。
  - `conversation/store.py` 和 `collaboration/store.py` 删除，公开 Store 类放回真实实现文件。
  - 已验证：focused pytest、py_compile、doc sync 均通过。

1. `agent_py_agent/agent/subagents/manager.py`
   - 当前定位：子代理管理主入口，允许比以前更大。
   - 下一步只在职责明显分叉时拆；不要再拆出基础 manager 薄层。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_manager_board_class.py agent_py_agent/tests/test_subagent_coordinator_due_check.py -q`

2. `agent_py_agent/agent/gateway_parts/request_execution.py`
   - 当前定位：gateway 请求执行主链路。
   - 下一步优先排查慢响应和上下文膨胀；只有出现无关职责才拆。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_gateway_request_runtime_errors.py agent_py_agent/tests/test_gateway_chat_conversation_context.py -q`

3. `agent_py_agent/agent/agent_core/orchestration/dispatch/mixin.py`
   - 当前定位：主代理 dispatch/watch 入口。
   - 下一步保留一条清晰调用链，避免新增转发层或历史参数层。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_dispatch_mixin.py agent_py_agent/tests/test_orchestration_dispatch_subagents_tool.py -q`
