# 工具表面收敛与副作用错误契约（Workstream B：W0–W6）

本文档说明 2026-11-14 合入的工具表面瘦身、业务域归并、动作上界授权、确定性副作用归因与原生推荐去重设计及验收边界。

## 1. 五项核心架构决策

### 1.1 104 个业务叶工具归并为 3 个领域工具 + 1 个独立人工确认工具
- **分域归并（104 -> 3）**：将原先注册表中的 104 个细粒度 `business_<action>` 叶工具统一收敛为 3 个按 `action` 判别分支（`oneOf`）的领域工具：
  - `business_space`（40 个空间、资产、接口绑定、权限与策略动作）；
  - `business_execution`（26 个后台代理、会话任务与消息动作）；
  - `business_activity`（38 个工作流、运行控制、审计、产物与健康查询动作）。
- **独立真人确认入口保留**：`assets.versions.accept` 保持为独立工具 `business_assets_versions_accept`（1 个动作），绑定 `ApprovalPolicy("always", require_human=True)`，在 `ask`、`auto` 乃至 `admin_full_access=True` 模式下均强制要求真实人类确认，绝不混入上述 3 个领域工具枚举。
- **核心正文读取与工作流独立性**：`read_business_asset` 保持独立核心只读工具；21 个点号业务工作流动作及 `workflows.health.get` 始终留在 `business_activity` 中，不依赖 `enable_self_map` 开关。
- **死工具下线与非执行型重定向**：移除无生产引用的旧 `BusinessSpacesTool` 类；模型或历史输入误调 `business_spaces` 时由执行器直接返回 `TOOL_NOT_IN_RUNTIME_SNAPSHOT`（`reported_error_code="TOOL_RETIRED_REDIRECT"`、`effect_outcome="not_started"`、`retryable=False`）及明确目标工具指引，不声明任何操作锁。

### 1.2 显式版本化别名迁移与统一操作幂等身份
- **宿主双版本输入解码**：`agent_py_agent/agent/tooling/versioned_input_decoder.py` 维护冻结的 104 项退役工具名到 `(domain_tool, action)` 映射，在可信宿主输入协议版本 `1` 与 `2` 下将未执行的旧 `business_<action>` 调用纯函数转换为规范领域调用，完整保留 `call_id`、`operation_id`、`idempotency_key`、`request_id`、`expected_revision`、`params` 以及挂起恢复携带的 `input_migration`（归档至 `prior_migration`）。
- **共享操作身份防重复执行**：`BaseTool.operation_identity_for` 对 3 个业务领域工具统一返回 `"business_" + action.replace(".", "_")`，使规范领域调用与历史叶名称调用在 `operation_store` 中共享完全一致的 `tool` / `idempotency_namespace` 与 `args_hash`。已完成的历史叶记录在重放时直接返回原持久化结果，绝不二次调用底层 `execute_business`；冲突 `action` 或未就绪路由在进入 `claim_tool_operation` 前即返回 `effect_outcome="not_started"`。
- **可信实例回执核验**：`inspect_registered_business_receipt` 仅对当前 `ToolRuntimeSnapshot` 中真实登记的业务领域工具实例执行原始业务回执查询，拒绝未登记替身对象。

### 1.3 单调 `tool:action` 动作上界与有界静态 Schema 投影
- **单调交集不扩权**：`agent_py_agent/agent/tooling/registry_auth.py`（`normalize_tool_grants`、`action_ceilings`、`intersect_tool_grants`、`creation_snapshot_tool_grants`）统一处理父子派工、预设（`READ_ONLY_SUBAGENT_TOOLS` / `CODING_SUBAGENT_TOOLS` / `COORDINATOR_TOOLS`）、角色归一与后台唤醒（`SESSION_TASK_WAKE_ALLOWED_TOOLS`）授权交集：
  - 孤立的 `tool:action` 令牌不单独授予工具可见性（必须存在对应裸工具名或合法旧叶别名）；
  - 父级存在 `tool:action` 动作上界时，子级或预设传入裸领域工具名（如 `business_execution`）只会维持或收窄父级动作集合，绝不放宽上界；
  - `disabled_tools` 中的旧叶名称（如 `business_spaces_get`）精确从对应领域工具中减去单个动作；若动作上界包含当前宿主未就绪的动作，快照按 `TOOL_ACTION_NOT_PERMITTED` 保守失败关闭。
- **有界静态缓存**：`model_spec_for_actions` / `_narrow_model_spec_for_actions` 同步裁剪 `action.enum`、`oneOf` 分支、可达 `$defs` 与合法 `examples`，并以静态 `(tool_name, spec_revision, sorted_actions)` 为键存入上限 128 项的 `_NARROWED_MODEL_SPEC_CACHE`（LRU），禁止将动态 `owner_id` / `run_id` 放入缓存键。

### 1.4 权威 `effect_outcome`、`unknown` 禁盲目重试与持久化 `next_action`
- **写前确定性拒绝归因 `not_started`**：核心文件工具（`write_file`、`edit_file`、`apply_patch`）、管理配置工具（`user_config`、`admin_controls`、`manage_models`、`runtime_governance`、`compact_policy`、`controlled_exec`）、领域工具（`scripts`、`workflows` 等）及 `MCPProxyTool` 在任何真实外部写入或远端发送前发生的校验/权限/策略拒绝，均显式返回 `effect_outcome="not_started"`，避免误触发 `TOOL_OPERATION_OUTCOME_UNKNOWN` 停机。
- **部分提交与提交后读回异常**：
  - `apply_patch` 多文件补丁在首个文件落盘后遇后续文件单点模拟 I/O 错误时返回 `TOOL_PARTIAL_COMMIT`（失败目标证实未改动时 `effect_outcome="failed"`，无法证实时 `effect_outcome="unknown"`，`retryable=False`），在 `handler_details` 中携带 `files_modified`、`failed_path` 与已就绪 `artifact_refs`；
  - `scripts` / `workflows` 主写入成功后若读回阶段发生单点模拟 I/O 错误，返回 `TOOL_POST_COMMIT_READBACK_FAILED`（`effect_outcome="unknown"`，`retryable=False`），重放时命中既有操作记录而不重复写入；
  - 主写入成功但后置输出归档异常时保留 `projection_error_code="TOOL_OUTPUT_ARCHIVE_FAILED"` 诊断，不覆盖主执行成功事实；
  - 预检 `validate_invocation` 意外抛出内部异常时统一收口为 `TOOL_INVOCATION_VALIDATOR_FAILED`（`effect_outcome="not_started"`，`retryable=False`，异常摘要自动脱敏且零操作锁声明）；
  - MCP 传输层发送后连接中断保持 `effect_outcome="unknown"`、`retryable=False`；远端明确返回 `isError=True` 时归一化为 `error_code="TOOL_RUNTIME_ERROR"`、`reported_error_code="MCP_REMOTE_ERROR"`、`effect_outcome="failed"`、`retryable=False`，保留具体远端错误正文与下一步建议。
- **持久化下一步指引**：`ToolHandlerOutcome.next_action` 经密钥脱敏并截断至 `<= 2048` 字符后持久化进操作记录，在重放恢复时原样保留，并在 `ToolResult.render_for_prompt()` 中唯一渲染一次 `[tool-next-action]`。

### 1.5 零新增配置项、全量嵌套字段说明、原生推荐去重与 O(1) 快照索引
- **零新增配置键**：全程复用既有配置项（`business_spaces_enabled`、`enable_self_map`、`tool_catalog_mode`、`tool_catalog_deferred_categories`、`tool_write_inline_max_chars` 等），未向 `agent_config.yaml`、`AgentConfig`、`parameter_registry.py` 或 `capability_config.yaml` 新增任何配置开关。
- **嵌套 Schema 说明与可执行无秘密示例**：补齐核心、管理、延迟领域、业务域及 Computer Use MCP 工具各层属性说明（零缺省说明路径）与通过 `validate_tool_input` 自检的无秘密示例；Computer Use 上游已知工具补充简明中文说明并保持参数签名一致（`type_text` 保留本地 `clear_existing=False` 单一定义，`scroll_screen` 保留可选 `x`/`y`）。
- **原生推荐去重与快照索引**：`tool_protocol="native"` 下 `# Recommended Tools` 自动排除已注入原生通道的直出工具及当轮经 `tool_search` 新加载的工具（`_dedup_native_recommendations_section`），仅提示未加载的授权折叠工具且保留 `# Capability Packages` 等非工具段落；`ToolRuntimeSnapshot.runtime(name)` 通过快照绑定的不可变字典索引实现 $O(1)$ 查找。

## 2. 主要实现模块与测试入口

- **运行时契约与授权索引（W0）**：`agent_py_agent/agent/tooling/models.py`、`executor.py`、`registry.py`、`registry_auth.py`、`versioned_input_decoder.py`、`agent_py_agent/agent/contracts/error_taxonomy.py`、`agent_py_agent/agent/agent_core/_tool_loop_service.py`；测试 `agent_py_agent/tests/test_tool_slimming_w0_contracts.py`。
- **业务域归并与网关桥接（W1）**：`agent_py_agent/agent/gateway_parts/business_api.py`、`agent_py_agent/agent/plugin_host/business_action_specs.py`、`agent_py_agent/agent/conversation/background_tool_policy.py`；测试 `agent_py_agent/tests/test_tool_slimming_w1_business_domains.py`。
- **核心文件/会话/编排契约（W2）**：`agent_py_agent/agent/tooling/_filesystem_write.py`、`_filesystem_edit.py`、`_filesystem_patch.py`、`process_sessions.py`、`agent_py_agent/agent/agent_core/orchestration/tool_specs.py`；测试 `agent_py_agent/tests/test_tool_slimming_w2_core_contracts.py`。
- **延迟领域工具 Schema 与提交后读回保护（W3）**：`agent_py_agent/agent/tooling/scripts_tool.py`、`workflows_tool.py`、`asset_delivery_tool.py`、`audit_group_tools.py`、`feishu_cloud_tool.py`、`feishu_auth_tool.py`、`agent_py_agent/agent/scheduler/tool.py`；测试 `agent_py_agent/tests/test_tool_slimming_w3_domain_schemas.py`。
- **管理配置工具 Schema 与写前证明（W4）**：`agent_py_agent/agent/tooling/user_config_tool.py`、`admin_controls_tool.py`、`runtime_governance_tool.py`、`compact_policy_tool.py`、`controlled_exec.py`、`agent_py_agent/agent/capability/model_profile_tool.py`；测试 `agent_py_agent/tests/test_tool_slimming_w4_admin_schemas.py`。
- **Computer Use 中文说明与 MCP 三段式错误契约（W5）**：`agent_py_agent/agent/tooling/computer_use_server.py`、`computer_use_observation_tools.py`、`mcp_registration.py`、`mcp_protocol.py`；测试 `agent_py_agent/tests/test_tool_slimming_w5_computer_use.py`。
- **端到端往返与组合验收（W6）**：`agent_py_agent/tests/test_tool_slimming_w6_wire_acceptance.py`、`test_tool_progressive_disclosure.py`、`test_tool_gateway_contract.py`、`test_tool_operation_result_ref_wire.py`。

## 3. 工具面分层计数与字符指标（集成提交实测）

在隔离 `HOME` / `MY_AGENT_HOME` 下按 `SimpleAgent` 默认装配实测（含编排工具 `include_orchestration=True`）：

| 装配场景 | 注册总数 (`registered`) | 模型可发现总数 (`visible`) | 首轮直出数 (`upfront`) | 首轮直出 Schema 字符数 (`upfront_chars`) | 全部可见 Schema 字符数 (`visible_chars`) | 可见 `business_*` 工具 |
|---|---:|---:|---:|---:|---:|---|
| `business_spaces_enabled=False`（核心 + 常规扩展） | `70` | `63` | `29` | `41,884` | `170,871` | `[]` |
| `business_spaces_enabled=True`（出厂默认业务开启） | `73` | `66` | `29` | `41,884` | `262,911` | `business_activity`、`business_execution`、`business_space` |
| `business_spaces_enabled=True` + 治理/投影与正式采用端口全就绪（105 动作全开） | `74` | `67` | `29` | `41,884` | 按就绪分支展开 | 上述 3 项 + `business_assets_versions_accept`（外加核心 `read_business_asset`） |

说明：因 W1–W5 为全部嵌套对象属性补齐了中文契约说明与合规示例，按全量展开统计的 `visible_chars` 包含完整分支文档，而模型首轮实际加载的原生直出面固定收敛在 **29 个核心工具（`41,884` 字符）**，其余折叠领域工具仅在任务需要时经 `tool_search` 按名并按动作上界裁剪后按需加载。

## 4. 定向验证与未覆盖的真实环境边界

- **已完成本地定向验证**：`test_tool_slimming_w0..w6_*.py` 七套分片回归与组合往返验收，以及 `test_tool_gateway_contract.py`、`test_tool_operation_result_ref_wire.py`、`test_tool_progressive_disclosure.py` 全部通过（零跳过、零预期失败）；真实本地 `GatewayHTTPServer` `/client/business` 与 `BusinessClient` 往返、Anthropic/OpenAI 原生工具 Schema 转换、104 项双版本别名解码重放及 5 类单点模拟故障恢复均已实测通过。
- **残余验收边界**：本阶段验证基于本地确定性 `echo` 模型、进程内 MCP 传输与隔离测试网关完成；真实云端供应商线上原生工具流式采样、小参数模型在复杂多分支业务动作下的首次调用准确率，以及 `2C4G` Linux 真机端到端全链路实测留待集成合入后统一验收。
