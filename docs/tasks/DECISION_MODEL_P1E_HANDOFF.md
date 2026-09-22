# Workstream Handoff：决策共用设置服务（TODO 04 / P1-E）

## 基本信息

- workstream：decision-model-plan，TODO 04 共用设置与原模型工具接线。
- baseline：`d57368ed1`；工作树与父侧用量线共享，文件按明确认领分开。
- owner：decision_settings_review；父侧负责共享文档、后续消费者和 UI 集成。
- 状态：本地实现及定向验证完成；没有提交、推送、部署或收费模型调用。

## 本线目标

让用户设置入口与 my-agent 代操作使用同一份 owner/thread 决策设置，能够字段修改、恢复继承、比较版本并读回有效值；不会新增配置库、凭据副本、Agent 实例或保存时网络调用。

## 实际完成

公共入口为 `agent_py_agent/agent/settings/decision_settings.py`：

```python
execute_decision_settings_operation(context, operation, payload, *, thread_id="")
```

- `operation`：`read` / `patch` / `reset`。
- `payload.scope`：`owner`（默认，长期覆盖）或 `thread`（当前会话临时覆盖）。
- `patch`：`changes` 为扁平字段路径到值的非空对象。
- `reset`：`fields` 为非空字段路径数组；删除覆盖，重新继承，不能靠写 `null` 代替。
- 所有修改必须携带 `expected_revision={"owner": int, "thread": int}`；布尔值不能冒充版本号。
- 返回字段为 **`revision` 单数**，并含 `ok/schema/scope/thread_id/overrides/effective/sources`；写入还包含 `before`。
- `overrides` 分 owner/thread 两层，字段使用扁平路径；`effective.points` 按点嵌套，`sources` 保留字段来源及继承链。

通用字段：`enabled`、`timeout_seconds`、`stage_timeout_seconds`、`background_timeout_seconds`、`profile_id`。
默认分别为关闭、2 秒、4 秒、4 秒、未绑定。接入点固定为：

| 接入点 | 默认定义权威 | 可覆盖字段 |
| --- | --- | --- |
| model_selection | AgentConfig / agent_config.yaml | mode、timeout_seconds、profile_id |
| subagent_model | CapabilityConfig / capability_config.yaml | 同上 |
| skill_tool | CapabilityConfig / capability_config.yaml | 同上 |
| recall | MemorySettings，原 AgentConfig/YAML 记忆字段镜像 | 同上 |
| curator | MemorySettings，原 AgentConfig/YAML 记忆字段镜像 | 同上 |

所有点的 `mode` 默认 `off`，合法值为 `off/observe/apply`。总开关关闭保留模式，只将投影 `effective_mode` 置为 off。
点级时间/引用默认 `None`，继承通用策略；curator 继承后台单次期限，其他点继承前台期限。
点级 `profile_id=""` 表示明确未绑定，删除该覆盖才恢复通用模型继承。

JSON 设置请求只接受真正的有限正数秒，拒绝 bool、NaN、Infinity、零及负值；原简化 YAML 的浮点/null 字符串在配置入口显式转换，再使用同一校验。
`max_request_seconds` 只表示配置单次/阶段上限的最小值，**不代表正在进行阶段的剩余时间**。服务不持有阶段时钟、不延长旧请求，也不重置阶段预算；实际消费者仍须与阶段余额及调用方余额取最小值。

## 身份、并发和错误

`context` 必须包含可信 `home_paths` 与 `config`。`home_paths` 使用原 owner_provider/owner_kind/owner_id/config_dir，不能从模型参数构造。
提供 `thread_id` 时还必须有原 `conversation_store`；有持久 owner_home 的线程要求上下文也有匹配的 owner_home_dir。
能力默认优先从 `capability_config`、`capability_router.config` 或显式 `capability_config_path` 取得；未注入时只使用 CapabilityConfig 默认，不猜其他文件。

固定锁顺序为 owner 原模型目录锁 → 原 thread 文件锁。
owner 修改在锁内完成读取、两层 CAS、字段修改、原子保存、读回；thread 修改在 owner 锁保持期间走原 `threads.update_atomic()`，在最新 thread 上再次检查 owner 与版本。
普通模型管理仍保留新决策字段；thread 原子写入合并原 JSON 扩展字段，不覆盖并发 Compact/上下文数据。

- CAS：`DecisionSettingsConflict`，`code="decision_settings_conflict"`。
- 身份：`DecisionSettingsAccessError`，`code="decision_settings_access_denied"`。
- 字段、引用、格式错误：沿原 `ModelProfileError`。
- 只校验本次写入的引用；已有连接失效、缺凭据或共享撤销仍可读取并关闭。
- 新引用必须存在、用途为 decision；共享引用始终检查发布/撤销、用途及禁止共享 OAuth。
- `resolve_shared_model(..., require_enabled=False)` 只为离线保存跳过启用/凭据检查，旧调用默认 True。
- `connection.configured` 仅反映本地配置；`connection.network="unchecked"`，不声称服务在线。

原 `tooling/user_config_tool.py` 新增 `decision_read/decision_patch/decision_reset`，不增加工具或扩大原 main_agent 注册边界。
线程只取原 `runner.context.current_task_attributes()` 的 agent_thread_id，缺时取 conversation_thread_id；拒绝模型传入 owner/thread/run 身份。
工具 CAS 返回原 `STALE_VERSION`，保留 `reported_error_code="DECISION_SETTINGS_CONFLICT"`；权限/参数分别为 `TOOL_PERMISSION_DENIED/TOOL_INVALID_ARGUMENTS`，副作用为 not_started。
存储 IO 故障返回 `TOOL_PERSISTENCE_FAILED` 和 unknown，要求重新读回核对，不能冒充未保存。

## 持久结构迁移

- 原模型目录升级为 `owner_model_profiles.v4`，新增 `decision_settings={schema:"decision_settings.v1",revision:0,overrides:{}}`。
- v1/v2/v3 只读显式迁移，不在读取时重写；下次原管理入口写入才保存 v4。保留原编号、连接、选择及根扩展字段；旧 schema 夹带新决策覆盖时拒绝，不能静默丢弃。
- 原会话升级为 `conversation_thread.v10`，同样只新增版本化覆盖。v1-v9/无版本旧记录缺字段时补空继承；v10 坏信封及未知线程版本拒绝。
- 覆盖内不复制默认值、凭据、阶段计时或连接状态。revision 只代表这两层设置覆盖；实际调用仍须重新核验原模型引用、共享授权及权限。

## 改动文件

- 新增 `settings/decision_settings.py`、`decision_settings_schema.py`、`decision_settings_defaults.py`、`decision_settings_projection.py`。
- 扩展原 `model_profiles.py`、`model_provider_schema.py`、`shared_model_catalog.py`。
- 原 AgentConfig/YAML、CapabilityConfig/YAML、MemorySettings/记忆归一入口新增各自负责字段。
- `conversation/models.py` 与 `store_threads.py` 增加线程覆盖迁移及原子保留。
- 原 `tooling/user_config_tool.py` 及 `test_user_config_capability.py` 接线和验证。
- 新增 `test_decision_settings.py`；更新 `test_decision_model_profiles.py` 与 `test_model_provider_management.py` 的当前版本断言。
- 父侧更新 `test_conversation_store.py` 的 v10 写出断言；本线没有改该文件。

## 验证

12 个直接相关测试文件联合 **251 项通过**：decision_settings、user_config_capability、settings_memory、model_profiles、decision_model_profiles、shared_model_catalog、capability_config、settings_config、thread_model_selection、model_provider_management、model_oauth、memory_config。
最后统一空覆盖 factory 后重跑 decision_settings + user_config_capability **47 项通过**。
覆盖真实锁竞争单一获胜、owner 锁贯穿 thread CAS、字段恢复、失效/缺 key/共享撤销、跨 owner/thread 拒绝、未知不降级及旧字段保留。
定向 Ruff 与 `git diff --check` 通过。未运行全仓 pytest 或收费模型。
中途 strict code-size 通过并生成 CODE_SIZE_REPORT；它早于最后工具接线，最终严格 gate 由父侧统一运行，不能把该中途结果当最终远端验收。

## 剩余边界与建议下一步

UI 菜单/Gateway 尚未接本服务，属于 TODO 11；本片不声称完整双入口用户验收完成。
实际决策消费者、运行中关闭取消、旧建议应用前复核、阶段预算冻结仍由 TODO 06 等后续入口完成；设置服务只提供版本和不重置预算的合同。
原 STALE_VERSION 的通用恢复提示目前提到 read_file；工具错误正文已要求重新读取设置，若需调整原错误合同由父侧统一决定。
父侧需同步 CODEBASE_TREE、设计/状态/测试文档；本线只写本交接，不改共享文档。

建议下一步：先接 TODO 06 的请求快照和应用前 revision/权限复核，接口稳定后让本线只读复核热关闭竞态；UI 可在相同公共 API 上并行接线，不能另建偏好存储或仅在 caller 超时后释放存活 worker 的资源。
