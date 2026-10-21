# 四大一等公民（Monitor / Workflow / Script / Skill）与插件全量数据及执行网关设计

状态：2026-10-21 底座第一阶段落地中（`agent_py_agent/agent/assets/`、`workflows/`、`observability/`、`tooling/`、`gateway_parts/plugin_broker_api.py`）。

## 1. 背景与解决的核心问题

1. **终结工作流与固化脚本靠记忆散乱存放**：
   - 将 **工作流（`Workflow`）** 与 **固化脚本（`Script`）** 提拔为与 **技能（`Skill`）**、**监控/定时（`Monitor`）** 平齐的 Owner 空间一等公民成员，具备唯一权威存储目录（`<owner_home>/workflows/<id>/`、`<owner_home>/scripts/<id>/`）、标准元数据描述文件与专用工具。
2. **统一标准提取 Key 驱动自由拼接**：
   - 四类一等成员统一提供 `asset_descriptor.v1` 标准提取 Key：`id`、`kind`、`name`、`summary`、`version`、`owner_id`、`access_targets`（访问什么系统/接口）、`data_sources`（查询什么源与范围，含 `evaluated_candidates` 已评估源备忘）、`credential_refs`（凭据引用名与状态，不含明文密钥）、`io_contract`、`refs`（正向引用）、`used_by`（反向引用）与 `runtime_summary`。
   - 通过 `build_asset_topology` 一次性输出如 `Monitor A -> Workflow B -> {Script C, Script D, Skill E, Skill F}` 的完整节点、连线图及环路检测事实（`nodes + edges + cycles + has_cycles`，节点附 `in_cycle`）。
3. **双模工作流执行与四大运行韧性机制**：
   - **平时纯脚本极速跑，异常唤醒 LLM 自愈**：固定采集步（`mode="deterministic_script"`）由底座从 `ScriptStore` 读取并物化冻结代码至一次性隔离目录，经 `AttemptExecutionSandbox` 与 `scrub_subprocess_env` 拉起受控子进程执行，零 LLM 参与；当退出码非 0、超时或输出不满足 `io_contract` 时，按 `on_error="escalate_to_agent" | "fail" | "skip"` 分流，升级时置 `status="paused_for_resume"`、`needs_repair=True` 并记录脱敏 `escalation_result` 供 Agent 诊断与断点修复。
   - **数据源动态增减与新源评估备忘**：查询目标（如 `active_appnames: ["A", "B", "C"]`）保存在 `script.json` 的 `data_sources` 配置 Key 中并通过环境变量/JSON 注入脚本；出现新源（如 `D`）时，Agent 试查评估后可将结果追加记入 `evaluated_candidates`（`decision: "active" | "ignored" | "pending"` 及原因，完整保留历史评估链），避免重复折腾且前端直观可见。
   - **工作流 Step 级断点续跑**：每次工作流执行在 `<owner_home>/data/workflow_runs/<workflow_run_id>/` 通过 `nofollow_fs` 与 `locked_json_path` 持久化 `run.json` 与每步输出快照 `steps/<step_id>.json`；中途因网络或凭据中断后，支持从失败步 `resume`，不重复执行前序已成功步骤。
   - **新 Skill 动态挂载**：工作流分析步支持静态 `skill_ids` 与动态 `skill_tags`，并支持通过工具或接口一键绑定新沉淀的 Skill。
4. **向插件暴露全量包装接口（资产拓扑 + 任务调用数据 + 工具耗时账本 + 主动执行控制）**：
   - **资产与拼接拓扑接口（`assets.*`）**：列出、读取、保存四类一等公民（对外剥离宿主绝对路径，只读插件默认隐去脚本源码与 Skill 正文并标记 `source_redacted=True`），热修改数据源配置 Key 与拼接绑定，查询 `assets.topology`。
   - **每次布置任务的完整调用数据接口（`telemetry.tasks.*` / `telemetry.workflows.runs.*`）**：查询每次布置任务的触发来源、状态、耗时、主/子/孙代理执行树（`runs`）、每轮模型耗时与 Token 明细（`rounds`）、挂载的一等资产与产出文件（`artifacts`），并支持直接从 `ToolTelemetryLedger` 自动合成真实运行时任务调用树。
   - **每个工具调用时间与明细账本接口（`telemetry.tool_calls.*` / `telemetry.tool_stats`）**：在验证出口以独立 `try/except` 隔离记录每次工具调用的精确开始/结束时间戳、毫秒级耗时（`duration_ms`）、调用方身份、深度脱敏入参摘要、退出码与 P50/P95 统计。
   - **主动运行工具/脚本/工作流接口（`execute.*`）**：在当前 Owner 权限边界内直接调用注册工具、试跑/执行固化脚本、触发或断点恢复工作流，自动记入遥测账本，并对冲突身份参数暴露 `scope_resolution` 与 `scope_warnings`。

## 2. 模块边界与权威路径

| 模块路径 | 权威职责 |
| :--- | :--- |
| `agent_py_agent/agent/assets/descriptor.py` | 四类一等成员统一标准提取 Key 契约（`AssetDescriptor`、`DataSourceSpec`、`EvaluatedCandidate` 等）与敏感字段过滤 |
| `agent_py_agent/agent/assets/script_store.py` | Owner 空间 `<owner_home>/scripts/<script_id>/` 固化脚本存储、数据源配置热更新与评估记录 |
| `agent_py_agent/agent/assets/workflow_store.py` | Owner 空间 `<owner_home>/workflows/<workflow_id>/` 工作流定义存储与步骤引用自动推导 |
| `agent_py_agent/agent/assets/monitor_adapter.py` | 监控/定时任务标准提取 Key 适配与工作流/脚本/Skill 绑定元数据 |
| `agent_py_agent/agent/assets/skill_adapter.py` | Skill 标准提取 Key 解析与扩展元数据读写 |
| `agent_py_agent/agent/assets/topology.py` | 正反向引用计算（`refs` / `used_by`）与自由拼接拓扑图构建（`build_asset_topology`） |
| `agent_py_agent/agent/workflows/` | 双模工作流执行引擎（`engine.py`）、确定性脚本执行器（`script_runner.py`）与 Step 级断点账本（`run_store.py`） |
| `agent_py_agent/agent/observability/telemetry_ledger.py` | 每个工具调用时间与明细账本（`<owner_home>/data/telemetry/tool_calls.jsonl`）及耗时分位统计 |
| `agent_py_agent/agent/observability/task_call_inspector.py` | 每次布置任务的完整调用数据账本（`<owner_home>/data/telemetry/task_calls/`） |
| `agent_py_agent/agent/tooling/scripts_tool.py` | Agent 管理与试跑固化脚本、增删数据源、登记新源评估结果的工具入口 |
| `agent_py_agent/agent/tooling/workflows_tool.py` | Agent 管理、运行、断点恢复工作流及动态挂载 Skill 的工具入口 |
| `agent_py_agent/agent/gateway_parts/plugin_broker_api.py` | 面向插件的包装接口分发服务（资产拓扑、任务遥测、工具耗时、主动执行） |
| `agent_py_agent/agent/plugin_host_broker_topics.py` | 对接 `plugin_host_api` 的扩展只读主题投影与 Broker 桥接 |
