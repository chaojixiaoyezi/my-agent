# REFACTORING BACKLOG

LLM: keep this file current. Do not copy old split plans back in.

当前方向：

- 不按行数强拆文件。
- 优先合并只转发调用、隐藏主链路、保留历史路径名或历史字段的层。
- 只在一个文件同时承担无关职责时拆分。
- 每次改主链路后运行对应 focused tests，再刷新 `CODE_SIZE_REPORT.md`。

---

## Active Items

### 多代理产物交付链路系统性脱节（R4 实测挖出，待专项修复）

R2/R3/R4 真实任务轮串出同一条主线：子代理产出 → 主代理汇总 → 最终交付
这条链路有系统性脱节。R4（GoAttack 复刻）最典型：3 个子代理因写权限被锁
全部阻塞、提交 capability_request，主代理未处理、9 轮草草交付 1 个
requirements.txt 却判 ok=true。拆解为四个独立可修子项（详见
docs/audits/R4-goattack-20260611.md）：

1. **子代理产物路径对齐写权限边界**：派工时 output_files 应解析到子代理的
   allowed_write_roots 内（如 work/agents/<id>/output/），而非 workspace 根；
   路径声明统一相对化。根因见 subagents/context_bundle_contracts.py:94。
2. **capability_request 在 run 模式的主代理处理回路**：未决请求必须触发解锁
   或显式拒绝，不允许被静默跳过（R4 中主代理全程无处理痕迹）。
3. **交付验收增加"声明产物对账"**：声明的 output_files 缺失应 block/标记
   （R4 声明 ~40 实交 1 仍 ok=true）。与 R3 的"验收覆盖实际文件"互补——
   R3 修了"实际文件要验"，这里缺"声明文件要在"。
4. **主代理汇总子代理产出到最终交付位置**应是可靠收尾步骤，缺失时 block。

### compact 工程鲁棒性补强（终端交互/终端应用 双源码印证，待实现）

三方对比（docs/audits/，my-agent vs 终端交互 源码 vs 终端应用）显示
my-agent 的 compact 设计领先（结构化续接 + 决策点推 + 主子代理分层），
但缺三样 终端交互 和 终端应用 共同具备的鲁棒性机制，有现成蓝本可参考：

1. **microcompact / tool-result 细粒度清理**：保留近 N 个 tool result、清理
   更早的。蓝本 终端交互 src/services/compact/microCompact.ts + apiMicrocompact.ts。
2. **thrash circuit breaker**：连续 N 次 compact 失败就停并报错。蓝本
   终端交互 autoCompact.ts:257-265（3-strike，附 BQ 数据：thrash 浪费
   250K API calls/天）。
3. **单轮内 PTL retry**：单轮 context 溢出时丢最老 API round 重试。蓝本
   终端交互 truncateHeadForPTLRetry。

## Completed Cleanup

- 2026-06-10: 产物验收的"运行时遇到未登记格式"能力补齐（先实证 长期助手/工具运行时/
  终端应用 三家都是"通用兜底降级 + 插件热加载"，会话运行时 偏严格拒绝；按三家做法补齐）。
  - 通用兜底打开器 `open_fallback`：未登记格式按"长相"产 GenericView（文本/zip 成员），
    让未知格式也能跑声明字段校验（required_strings/required_sections/min_size/required_files），
    格式无关、永不随格式增长；无声明则落第 0 层放行，不拒绝（守开放世界铁律）。
  - 打开器热加载：`~/.my-agent/openers/`（或 MY_AGENT_OPENERS_DIR）目录扫描，丢一个十几行
    打开器脚本即可支持新格式深度校验，不改主代码、不重启。坏脚本隔离加载（异常只记
    OPENER_LOAD_ERRORS、不打断主链路、好打开器不丢）；内置格式不可被插件覆盖。
  - 钉子测试 test_artifact_openers_fallback.py（6 项）：未知格式兜底/放行/zip成员/热加载/
    坏脚本隔离/内置不可覆盖。文档 docs/modules/contracts/artifact-openers.md。
  - 47 个格式验收测试仍逐位过；全量快速套件失败集是基线子集（零新增）；
    code-size strict 0/0、doc sync、offline matrix 全过。

- 2026-06-10: 产物格式验证器重构为"打开器注册表 + 通用检查器"两层（先看 终端应用/
  长期助手/会话运行时/工具运行时 实证：四家都是"打开⊥验证正交 + 薄注册表 + 格式专属校验单独层"，
  没有一家把格式专属逻辑塞进一个通用检查器）。
  - 新增 `artifact_openers.py`（第 1 层薄注册表）：`OPENERS = {格式: 打开器}`，每个打开器只
    把 bytes 变结构化视图或返回"打不开"finding（XLSX_INVALID/CSV_INVALID/PDF_INVALID_SIGNATURE/
    DOCX_INVALID/JSON_* 等逐字保留），不做内容校验。未登记格式没有打开器 → 落第 0 层
    （存在/非空/残桩，格式无关），守"开放世界禁止封闭枚举"。
  - 第 0 层（存在/非空/残桩）已存在；第 2 层结构校验改为吃打开器产出的视图，不再重复打开。
  - 删 `artifact_csv_acceptance.py`、`artifact_document_acceptance.py`：打开逻辑进 openers，
    csv header/min_rows、docx/txt/pdf quality 校验进 artifact_acceptance.py。
  - `artifact_xlsx_contract.py` 退化为纯 xlsx 解析模块（workbook_text/worksheet_tables/
    required_columns_with_blank_values，供打开器用）；死的 xlsx_contract_findings 块删除，
    xlsx 结构校验（TOO_FEW_SHEETS/MISSING_REQUIRED_COLUMNS/REQUIRED_COLUMN_EMPTY_VALUES）
    移入 acceptance 的 `_xlsx_structure_findings`，吃视图不重开。
  - 新增格式成本：简单格式≈OPENERS 注册一行（落通用校验）；带专属结构校验≈再加一薄函数。
  - static_site 不并：它委托 subagents/static_site/validator.py 做多文件跨文件 DOM/JS 校验，
    是子系统不是单文件格式打开器，按"职责清晰不硬并"保留。
  - 逐位不变：47 个格式验收测试全过，全量快速套件失败集是基线子集（零新增），
    code-size strict 0/0、doc sync、offline matrix、replay 全过。

- 2026-06-10: 第三批合并（严格按"为可维护性合并、不为减文件数合并"原则）。
  - contracts/：tool_protocol_v2_models→tool_protocol_v2、llm_activation_(models/fixtures/timeout_budget)→llm_activation_readiness、artifact_xlsx_reader→artifact_xlsx_contract。
    都是"数据模型/读取器拆分自唯一逻辑父文件"的 facade 形态，类型在前逻辑在后读起来更顺。
  - subagents/services/：idempotency_contract_identity + repair_contract_identity → contract_identity，
    去重 3 个逐字相同的私有 helper（_iter_packs/_string_tuple/_normalized_path），两个身份计算改名区分。
  - 清理阶段1遗留的空目录 compact_context_bundle/。
  - 合计 −6 个源文件，纯结构整理零行为变化。
  - 严格评估后**保留不合**（按原则该留）：lifecycle_runner_attempts/lifecycle_capability_records
    （两件不相关职责）、_memory_types（2 消费者共享类型）、control_plane_codec（序列化层）、
    web_markdown（HTML→MD 转换器）、filesystem_structured_read、home_runtime_compact_refs、
    registry_auth（并进 704 行 execution 更难读）、artifact_* 格式验证器族、offline_* 合约族。
    这些是命名自解释、职责单一的内聚文件，合进大文件降可读性。
  - 验证：编译、ruff、focused tests、doc sync、offline matrix、code-size strict 0/0；
    全量快速套件失败集是基线子集（仅 2 个基线既有失败，零新增）。

- 2026-06-10: 阶段5 旧兼容审计与收尾（逐项核对写入方后处置，未盲删）。
  - 已删：home_layout 8 个旧根 memory 字段（memory_dir/daily/raw/hooks/lessons/routing/
    routing_index_md/indexes），bootstrap 不再创建旧根目录；默认路由表与默认 lessons 播种
    迁到 owner 权威位置（原来播在被查询忽略的根索引=死配置，现在真实生效）；
    staged_checkpoint 的 `staging.source_ref` 死臂（全仓无写入方）。
  - 核对后判定非旧兼容、保留：task_progress 的 `id or title`（工具 schema 明确声明两字段）；
    content_recovery 的 source_tool/tool_name/tool（write-abort 与 tool-call 两种活协议形态）；
    collaboration updated_at→created_at（时间戳数据卫生）。
  - 显式推迟（有删除条件）：agent_work_dir/agent_run_workspace 双键（存量 compact 续接包
    在用旧键，删除条件=续接包数据迁移完成）；registry member rows 的 path→relative_path
    （删除条件=确认无旧 group 记录需要重验）；delivery doctor 的 output_mode 同义键与
    offline 合同 id/path 别名（删除条件=prompt 合同文档明确唯一键后）。

- 2026-06-10: 阶段3 gateway/chat 性能可观测 + 阶段4 coverage 补全。
  - gateway：inbox mtime 扫描门（空闲不再每 0.2s 全量 glob）、worker-0 恢复扫描节流
    （timeout/3）、heartbeat 新增 queue_ages 观测；jsonl 路径锁引用计数回收。
  - chat：`read_jsonl_tail_report` 尾部倒读（5000 行账本取 20 条实测 41x，逐位一致）。
  - coverage：新增 `directory_tree` 覆盖类型（min_read_ratio / max_candidates 合同可声明、
    候选截断显式暴露、修复提示带 missing_files）；shell 输出改中段截断（保头+保尾+
    省略标记+总行数），结论不再被截掉。
  - 核实后跳过：list_files 分页早已存在（offset/next_offset/page_window）；registry↔ledger
    打通已存在（metadata.coverage_items）；"artifact 声明覆盖源文件"不做——会成为模型
    自证通道，违反"模型输出不能自己证明自己"。
  - 推迟（待 R2 实测）：subagent tree 投影缓存、lane 化并发。

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
