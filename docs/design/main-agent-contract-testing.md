# 主代理合同驱动测试与阶段计划

这份文档定义主代理后续开发的主路线。

核心切换只有一句话：

> 真实环境测试保留，但不再作为主要开发方式；主开发流程切到“合同单测 + fake tool + fake LLM + replay + 少量真实验收”。

---

## 1. 为什么要切换

过去这几轮暴露了同一个问题：

- 一边跑真实任务一边修
- 每次等待模型、工具、网络、文件系统、上下文一起变化
- 问题复现慢
- 修复反馈慢
- 失败很难稳定复现

真实环境里经常混杂这些变量：

- 模型输出随机性
- 工具执行速度
- 网络抖动
- 真实文件状态变化
- prompt 长度和上下文波动
- 外部资源返回不稳定

这会让“修一个小 bug”变成“重新跑一遍整个链路才知道有没有用”。

所以后面要把真实环境的职责收窄成：

- 证明最终整条链路真的能跑通
- 暴露新的真实失败样本

而不是拿它做第一层调试工具。

---

## 2. 参考项目与借鉴点

本计划默认先参考 `/Users/example/study-agent/all-agent/` 下的项目，再做本仓库实现。

### 会话运行时-main

重点学习：

- 结构化事件流
- thread/session 持久化恢复
- 工具调用与中间状态分离
- output schema / structured status

我们借鉴的方向：

- 机器事实必须结构化
- 工具、状态、恢复都要能 replay
- 中间事件不能只靠自然语言摘要

### 通道运行时-main

重点学习：

- control plane
- task/run/session registry
- stopReason / pendingToolCalls / session 状态表达
- 执行态与聊天态分离

我们借鉴的方向：

- 主代理和后续子代理都要有清楚的 task/run 状态台账
- runtime 问题必须能从结构化状态里看出来

### 长期助手-agent-main

重点学习：

- activity / inactivity tracking
- inactivity-based timeout
- 长任务稳定性
- 工具网关和后台任务恢复

我们借鉴的方向：

- timeout 不只看墙钟时间
- “没进展”要靠活动/产物/状态判断
- 长任务要能恢复，不要轻易误杀

### 终端应用-main / 模型助手 Code

重点学习：

- background session / resume 体验
- 少量软约束
- 工具只在必要时调用

我们借鉴的方向：

- 自然语言软约束可以有，但数量少、边界清楚
- 不要把流程偏好写成一堆硬规则

### claw-code-main

重点学习：

- TaskRegistry
- parity harness
- replay-safe contract

我们借鉴的方向：

- 失败样本和回放应该成为正式开发资产

### openai-agents-python-main

重点学习：

- handoff
- guardrail 边界
- agent orchestration 抽象

我们借鉴的方向：

- guardrail 应该是少而硬的边界，不是流程经理

### langgraph-main

重点学习：

- 显式状态机
- 有状态流程图

我们借鉴的方向：

- 主代理状态机必须可画图、可测试、可恢复

### agentscope-main

重点学习：

- MsgHub
- session + SQLite
- 消息路由

我们借鉴的方向：

- 后续 message/card/runtime 控制面可以参考它的消息组织方式

### openhuman-main

重点学习：

- memory tree
- SQLite + markdown knowledge base
- 长期记忆组织

我们借鉴的方向：

- 记忆要分层、可索引、可压缩、可落盘为可读文件

### 终端交互-main / openclaude-main / langchain-master

这些作为补充参考：

- 看模块拆分、任务层、hook chain、service 化方向
- 不作为第一优先主参照

### my-agent-architecture-review-20260519-clean / my-agent-feature-card-message-runtime

重点学习：

- 当前架构问题基线
- Card / Message / Task / Worker 的近似目标形态

我们借鉴的方向：

- 后续只把通用 runtime card 和 message route 变成机器合同
- 专项真实任务只留在测试 fixture 和最终验收，不进入生产合同

---

## 3. 测试金字塔

后续测试按五层执行。

### 第 1 层：合同/纯函数单测

验证这些：

- 状态机迁移
- verifier
- closeout 判断
- guard 触发条件
- 错误分类
- tool manifest
- path / artifact / checkpoint 校验

特点：

- 不调真实模型
- 不调真实工具
- 几秒内跑完

### 第 2 层：fake tool 测试

用假工具模拟：

- 读文件成功/失败
- 写文件成功/失败
- builder 成功/失败
- 查询结果为空/非空

目标：

- 验证工具结果如何影响状态机和 verifier

### 第 3 层：fake LLM 测试

用假模型故意输出坏行为：

- 说完成但没产物
- 写空产物
- 重复同一工具
- 调不存在工具
- 忘记最终报告

目标：

- 验证即使模型胡说，框架也不能被骗

### 第 4 层：trace replay

把真实环境里出现过的问题沉淀成轨迹：

- contract_ref
- tool_result
- final
- 后续可扩展 state/event closeout 快照

当前推荐最小事件类型：

- `contract_ref`
- `tool_result`
- `runtime_issue`
- `state_snapshot`
- `closeout_snapshot`
- `acceptance_report`
- `final`

目标：

- 不重跑真实环境，也能复现已知失败

### 第 5 层：真实环境验收

真实环境只回答一个问题：

> 结构化底座在真实模型、真实工具、真实文件、真实网络下还能不能跑通。

它不是日常主调试方式。

---

## 4. 第一到第五阶段

下面五个阶段是当前开发主线。

### 第一阶段：测试底座骨架

目标：

- 建立 contracts / fake_tools / fake_llm / replay 基础目录和最小 runner

落地点：

- `agent_py_agent/tests/contracts/`
- `agent_py_agent/tests/fake_tools/`
- `agent_py_agent/tests/fake_llm/`
- `agent_py_agent/tests/replay/`
- `agent_py_agent/tests/scenario_packs/`
- `agent_py_agent/tests/support/`

完成标准：

- 能跑最小合同 fixture
- 能跑 fake tool
- 能跑 fake LLM
- 能跑 replay case
- fixture / fake / replay 都能表达 runtime issue、state snapshot、closeout snapshot 这些机器事实

### 第二阶段：主代理通用合同

目标：

- 禁止专项合同
- 把任务需求上升为通用 artifact / staging / recovery / verification 合同

建议分类：

- `single_file_artifact`
- `multi_file_artifact`
- `structured_data_artifact`
- `builder_backed_artifact`
- `renderable_document_artifact`

完成标准：

- 新任务不需要专门加一条底层逻辑
- 只能通过组合通用合同表达差异

### 第三阶段：统一状态机

当前状态：

- 已完成（2026-05-21）

目标：

- 主代理生命周期统一到一套结构化状态机

建议状态：

- `PLANNING`
- `RUNNING`
- `WAITING_FOR_TOOL`
- `WAITING_FOR_LOCAL_PROGRESS`
- `WAITING_FOR_USER`
- `VERIFYING`
- `BLOCKED`
- `FAILED`
- `DONE`

完成标准：

- 每个状态迁移都有结构化条件
- 不能靠自然语言文本决定关键状态
- `state_machine_transitions.py` 提供共享状态迁移合同
- replay 会检查 `state_snapshot` 序列是否合法

### 第四阶段：工具与错误合同

当前状态：

- 已完成（2026-05-21）

目标：

- 工具失败、builder 失败、artifact 缺失、checkpoint 坏掉，都走统一错误分类和恢复动作

建议错误类型：

- `TOOL_UNAVAILABLE`
- `TOOL_RESULT_INVALID`
- `APPROVAL_REQUIRED`
- `ARTIFACT_MISSING`
- `ARTIFACT_EMPTY`
- `CHECKPOINT_INVALID`
- `CHECKPOINT_NO_ROWS`
- `NO_PROGRESS`
- `APPROVAL_REQUIRED`

完成标准：

- 系统知道失败后该重试、回退、阻塞还是等待
- 不靠 prompt 文案兜底
- `tool_manifest_contract.py` 统一输出 visible/executable tools、failure taxonomy 和 failure contracts
- context bundle 与 `list_tools` 使用同一份共享 tool manifest payload

### 第五阶段：Replay 正式化

当前状态：

- 已完成（2026-05-21）

目标：

- 真实环境失败样本全部沉淀成 golden trace

建议最小轨迹：

- `contract_ref`
- `tool_result`
- `final`

后续可扩展：

- `state_snapshot`
- `closeout_snapshot`
- `runtime_issue`

完成标准：

- 真实环境出现的新失败，24 小时内要变成 replay case
- 修复后 replay 必过，再上真实环境
- `tests/replay/specs/*.json` 成为 declarative replay case
- `scripts/check_replay_contracts.py` 成为 replay gate

---

## 5. 第一批必须沉淀的失败样本

这一批是当前最值钱的。

- `missing_artifact_should_fail`
- `empty_artifact_should_fail`
- `tool_failed_cannot_complete`
- `bootstrap_materialization_required`
- `repeated_exploration_should_redirect_or_block`
- `staged_json_no_rows_cannot_complete`
- `builder_not_called_cannot_complete`
- `model_claims_done_without_evidence_should_fail`

这些样本优先做成：

- contract fixture
- fake LLM case
- replay case
- 对同一类回归，再组合一个 `scenario_pack`

建议合同字段继续保持通用：

- `artifacts.required[*].required_sections`
- `artifacts.required[*].json_requirements.required_keys`
- `artifacts.required[*].json_requirements.required_non_empty_paths`
- `tools.required_calls`
- `tools.required_successful_calls`
- `runtime.required_issue_codes`
- `runtime.forbid_succeeded_when_issue_codes_present`
- `final_status.allow_succeeded_only_if`

---

## 6. 真环境和测试的比例

建议执行比例：

| 类型 | 占比 |
| --- | ---: |
| 状态机 / verifier / 合同单测 | 30% |
| fake tool / fake LLM 集成测试 | 30% |
| replay 测试 | 20% |
| focused integration | 15% |
| 真实环境验收 | 5% |

真实环境不再承担主要开发反馈功能。

---

## 7. 交付纪律

以后每修一个真实问题，都按这个顺序：

1. 先从真实任务里抽一个最小失败样本
2. 做成 contract fixture / fake LLM / replay
3. 先让测试稳定复现失败
4. 再改产品代码
5. 测试变绿
6. 最后才回真实环境验收

---

## 8. 当前状态

当前仓库已经开始具备这条路线的第一批骨架：

- 已有 delivery closeout / contract / verifier 相关测试
- 已有 `tests/contracts`
- 已有 `tests/fake_llm`
- 已有 `tests/replay`
- 已有 `tests/scenario_packs`
- 已有 `tests/support` 里的最小 runner

但还不够完整：

- fake tool 还很薄
- replay 事件模型还偏小
- 失败样本库还不够多
- 还没有把这周的真实任务问题全部沉淀进去

所以后续第一优先级不是再盲跑真实环境，而是把这套底座补齐。
