# 四大一等公民（Monitor / Workflow / Script / Skill）与插件全量数据及执行网关设计

状态：2026-10-21 底座第一阶段落地中（`agent_py_agent/agent/assets/`、`workflows/`、`observability/`、`tooling/`、`gateway_parts/plugin_broker_api.py`）。

## 将脚本文件交付给业务任务

脚本在正规工作流步骤中从环境变量 `MY_AGENT_RUN_OUT` 取得输出目录，将要交付的文件写进该目录。成功归档后，`workflows.inspect_run` 的 `steps[].output_artifacts` 返回相对路径、大小、SHA-256 和 `artifact_id`。业务任务可将这两个标识传给 `artifacts.get`，其中 `version_ref` 使用原 SHA-256，再通过原产物内容接口下载。

编号指向原工作流运行、步骤尝试和清单条目。查询和下载均核实冻结业务任务、所属空间、读取权限以及原文件版本。输出到 stdout 的文件路径仅作为脚本返回数据。

升级旧运行索引后，超长自定义编号的定位按页补建。尚未覆盖目标时，详情与下载均返回可恢复的 503，并携带 `WORKFLOW_ARTIFACT_RUN_LOCATOR_INCOMPLETE`。重查同一编号或运行列表维护会继续推进。

恢复确认后的原尝试及 foreach 的每个成功批次保留自己的清单。业务客户端可以从 `workflow_runs.get` 查到有权读取的文件编号和 SHA-256。工作流允许交付最多 64 MiB 的单个文件，服务器按块核对整个文件的摘要后发送请求的字节范围。读取超大文件只增加有界内存开销，下载期间需要与请求范围相应的临时磁盘空间。

## 原生独立审核接入（2026-11-12 源码候选）

工作流可在 `descriptor.metadata.workflow_review_policy` 声明 `steps`、`minimum_reviews` 和 `ttl_seconds`。步骤进入副作用准入时，通过原 ORCH 证据库登记审核需求，版本绑定运行、步骤、尝试、候选定义和业务范围。作者身份来自活跃宿主调用，调用退出后失效。

另一受权执行者通过 `manage_requirement_provenance` 登记精确 `requirement_id`、`input_version` 和 `workflow_effect.v1` 范围的独立审核。待审步骤保留原尝试身份，恢复时核验每个审核者的最新结论、有效期及当前执行权限。返工结论使该步骤继续待审，候选变更后重新绑定审核。

业务订单停在审核门时，由创建候选的原任务派出独立审核者，再沿原运行恢复。其它任务派出的审核者会收到 `REVIEW_TASK_SCOPE_MISMATCH`，回执注明登记尚未开始，并给出回到原任务派工的恢复动作。

默认情况下，宿主按缺少的审核人数，通过原任务的子代理工具一次派出独立审核者。同一候选续跑复用原派工回执。审核者只取得本次审核所需工具，通过 `query_provenance` 阅读冻结输入、步骤和脚本，沿原版本登记意见。工作流策略的 `reviewer_model` 可指定已配置的模型档案，省略后继承父任务选用的模型。每个受审步骤可能产生子代理模型用量。

需要自行安排外部审核时，可以在该工作流策略内设置 `auto_dispatch: false`，或在能力配置内设置 `workflow_auto_dispatch_reviewers: false`。外部程序可通过原协作邮箱提交报告。工作流放行还需能核验原任务、独立执行身份和候选版本的正式审核回执。

原模板目录新增简单分析和开发阶段草案。开发草案提供架构、实现、审核、回归、发布五阶段，使用前需要配置真实工具或脚本步骤，并以其实际回执验证回归和发布结果。简单分析沿无状态分析流程执行。

定向验证覆盖原生 HTTP、实际工具登记、版本和归属不符、自审拒绝、审核返工及作者上下文回收。组合回归另核验原任务的审核、恢复、一次脚本发布和发起会话收件。

## 工作流反向引用的有界维护（2026-11-12 源码候选）

`data/workflow_reference_index.sqlite3` 仅保存定义文件的反向引用投影。保存、删除和批量发布在原资产锁内登记待确认项，再提交定义并更新投影。引用查询最多返回一页父工作流，并读取对应定义复核；缺索引、覆盖未完成、发布待确认或超页均保留具体原因和覆盖未知。

原维护任务每轮至多处理 128 项历史发现或残留引用清理。进程内最多保留 32 个目录游标，空闲 60 秒可回收；重启后逐页重放。池锁仅保护借还操作，跨用户的文件读取可以并行。坏记录在当前扫描周期跳过，保留覆盖未知，完成本轮后等待下次常规维护。

原 Store 和批量入口负责即时更新。外部直接在原位修改已有 `workflow.json` 的正文，当前目录时间戳检测存在覆盖限制；通过规范保存入口重新发布可更新投影。目录新增、删除与历史缺失记录由渐进发现核对。

## 研判隔离的当前可用边界（2026-11-12）

`llm_skill_analysis` 的 `config.analysis_isolation="stateless_skill"` 可用：仅将步骤输入、前步输出和指定 Skill 材料交给无状态模型调用，不开放工具，不载入主会话或写记忆。

`lightweight_subagent` 尚未实现具备真实截止期限和逐次只读效果核验的执行器。显式选择时，生产入口在模型及工具执行前返回 `WORKFLOW_ISOLATION_UNAVAILABLE`，记录 `effect_outcome=not_started` 和替代建议；不会静默换成无状态分析。传入一个 callable 也不能启用此能力。

需要先读取材料再研判时，使用两个明确步骤：

```json
[
  {"step_id":"read","name":"读取材料","mode":"tool_call","on_error":"fail",
   "tool_spec":{"tool_name":"read_file","arguments_map":{"path":{"step_id":"$inputs","key":"path"}}}},
  {"step_id":"analyze","name":"无状态研判","mode":"llm_skill_analysis","on_error":"fail",
   "config":{"analysis_isolation":"stateless_skill"}}
]
```

工具步骤仍经当前 owner 的原生 dispatch、工具快照及权限门；工作流配置不能新增工具授权。前步输出作为后步 `prior_outputs` 提供，研判阶段不自行调工具。该组合已经过本地真实读文件步骤和原分析主链验证；它不代表已实现轻量子代理，也不替代部署后的模型验收。

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
