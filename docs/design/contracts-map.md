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
- `agent_py_agent/agent/contracts/tool_protocol_v2.py`：工具调用归一化。
- `agent_py_agent/agent/contracts/gates/`：工具、路径、审批、幂等、交付质量等运行门。
- `agent_py_agent/agent/contracts/delivery_contract_doctor.py`：入口级 delivery_contract 自检，负责 schema、路径边界、开放世界扩展声明和返工动作。
- `agent_py_agent/agent/contracts/effective_contract_snapshot.py`：最终生效合同快照。
- `agent_py_agent/agent/contracts/run_trace_contract.py`：运行 trace 结构。
- `agent_py_agent/agent/contracts/contract_status.py`：合同失败状态汇总。
- `agent_py_agent/agent/contracts/contract_trace.py`：finding 调试链。

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
