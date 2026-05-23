# 合同地图 / Contracts Map

最后一次更新：2026-05-23

## 一、三层任务架构

task 和 real_task 已通过"合一后扫尾"统一为三层结构：

### Layer 1: Runtime (核心实现)

所有任务执行逻辑的唯一实现点。新增功能只能改这一层。

| 文件 | 职责 |
|---|---|
| `main_agent_task_runtime.py` | 任务运行主入口和状态机 |
| `main_agent_task_runtime_adapters.py` | task/real_task 的 adapter 注册 |
| `main_agent_task_runtime_adapter.py` | Adapter 协议定义 |
| `main_agent_task_runtime_execution_recovery.py` | 执行恢复逻辑 |
| `main_agent_task_runtime_issue_codes.py` | 结构化问题码 |
| `main_agent_task_runtime_issues.py` | 运行时问题检测 |
| `main_agent_task_runtime_results.py` | 结果结构和序列化 |
| `main_agent_task_runtime_revalidation.py` | 重新验证逻辑 |
| `main_agent_task_runtime_subprocess.py` | 子进程管理 |
| `main_agent_task_runtime_summary.py` | 任务摘要生成 |

### Layer 2: Facade (通用 facade)

`main_agent_task_*.py` — 通用任务 facade，提供标准 task 入口。这些文件委托到 Layer 1 runtime，不包含独立业务逻辑。

### Layer 3: Compat (兼容入口)

`main_agent_real_task_*.py` — real_task 兼容 facade，仅保留 schema/root/name 适配。调用 `main_agent_task_runtime*.py` 核心实现，不复制业务逻辑。

**铁律**: 新代码必须接 runtime 层，禁止在 real_task 或 task facade 层写新业务逻辑。

## 二、Gate 体系

运行时安全门系统，共 26 个 gate 文件。所有 gate 通过 [gate_pipeline.py](agent_py_agent/agent/contracts/gates/gate_pipeline.py) 的显式 DAG 编排。

### 入口合同

| Gate | 文件 | 功能 |
|---|---|---|
| tool_call | `gates/__init__.py` | 工具调用入口检查 |
| tool_manifest | `gates/tool_manifest.py` | 工具声明校验 |
| tool_effect | `gates/tool_effects.py` | 副作用级别检查 |

### 运行时安全

| Gate | 文件 | 功能 |
|---|---|---|
| path_url_command | `gates/path_url_command.py` | path/URL/command 安全检查 |
| command_policy | `gates/command_policy.py` | 危险命令/操作符检测 |
| network_safety | `gates/network_safety.py` | DNS rebinding、SSRF 防护 |
| tool_rate_limit | `gates/tool_rate_limit.py` | 工具调用限流熔断 |
| tool_guardrail | `gates/tool_guardrail.py` | 工具循环检测（重复失败/无进展） |

### 产物和交付

| Gate | 文件 | 功能 |
|---|---|---|
| artifact_gate | `gates/artifact_gate.py` | 产物存在/完整性 |
| artifact_provenance | `gates/artifact_provenance.py` | 产物来源追溯 |
| delivery_quality | `gates/delivery_quality.py` | 交付质量门 |
| delivery_quality_language | `gates/delivery_quality_language.py` | 交付语言检查 |
| delivery_quality_metrics | `gates/delivery_quality_metrics.py` | 交付指标 |
| delivery_quality_trace | `gates/delivery_quality_trace.py` | 交付追溯 |

### 状态和审批

| Gate | 文件 | 功能 |
|---|---|---|
| approval_binding | `gates/approval_binding.py` | 审批绑定 |
| run_contract | `gates/run_contract.py` | 运行合同 |
| idempotency_ledger | `gates/idempotency_ledger.py` | 幂等账本 |
| state_event_ledger | `gates/state_event_ledger.py` | 状态事件账本 |

### Skill 和能力

| Gate | 文件 | 功能 |
|---|---|---|
| skill_guard | `gates/skill_guard.py` | Skill 安全扫描和安装策略 |

### Compaction

| Gate | 文件 | 功能 |
|---|---|---|
| compaction_gate | `gates/compaction_gate.py` | Compaction 前后状态保护和恢复检查 |

### 基础设施

| Gate | 文件 | 功能 |
|---|---|---|
| gate_pipeline | `gates/gate_pipeline.py` | Gate DAG 编排引擎 |
| gate_pipeline_decisions | `gates/gate_pipeline_decisions.py` | Pipeline 决策记录 |
| gate_pipeline_specs | `gates/gate_pipeline_specs.py` | Pipeline 规格定义 |
| models | `gates/models.py` | Gate 通用数据模型 |
| registry | `gates/registry.py` | Gate 注册表 |
| runtime_reports | `gates/runtime_reports.py` | 运行时报告 |
| adapters | `gates/adapters.py` | Gate 适配器 |

## 三、Pipeline 集成点

Gate pipeline 的接入点：

1. **Tool execution** (`registry_runtime_gate_pipeline.py`): tool_call → tool_manifest → path_url_command → tool_rate_limit → tool_effect
2. **Artifact delivery**: artifact_gate → delivery_quality → delivery_quality_trace
3. **Compaction**: compaction_gate (pre + post checks)
4. **Skill install**: skill_guard → approval_binding
5. **Recovery**: run_contract → state_event_ledger → idempotency_ledger

## 四、开发顺序

新功能必须遵守：
1. 合同单测 → fake tool → fake LLM → replay → 少量真实验收
2. 先参考对照项目 (长期助手, 通道运行时, 会话运行时)
3. Gate 失败必须输出结构化 decision，不能只靠自然语言
