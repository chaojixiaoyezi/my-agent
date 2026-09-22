# Workstream Handoff

## 基本信息

- workstream：决策模型 TODO08 / P2 Curator 前置标注
- branch：`codex/decision-model-integration`
- worktree：现有 `decision-model-plan/my-agent-dsh` 独立工作台
- owner：父代理下的 decision_settings_review 子代理
- date：2026-09-22
- 基线：本轮开始 `d57368ed1`，共用 P1 服务已由父侧本地提交至 `e131b1f6e`；本片未提交。

## 本线目标

在原 Curator 批次提取前增加可关闭、可观察的临时分类和优先级建议；原批次、证据、验证、提交、晋升和游标继续由原记忆链负责。增强失败不能阻断原提取。

## 实际完成

- `annotate_curator_batch(agent, batch, run_id, *, max_input_chars, caller_deadline=None)` 返回 `(batch, warnings)`；没有独立后台或记忆写权限。
- 原 `MemoryCuratorDependencies.annotate_batch` 为宿主 callable，形参 `(batch, run_id, caller_deadline)`。`core._wire_memory_curator` 闭包复用现有 agent，`_execute` 在收集完整非空批次后、原 `extract_with_retries` 前调用一次。
- 共用 API：`begin_decision_stage(..., operation_id=run_id, scope="owner_background", caller_deadline=...)`，随后 `decide(..., point="curator", ...)`。owner 后台公共 scope 扩展由父侧实现。
- 宿主 params：原 lease run_id 必填，request/task/thread 为空、task_attributes 为空；不从材料借历史 thread/run。owner 设置、认证、连接、冷却、取消、可选准入、worker 和用量都复用 P1。
- 每来源两道 choice 题（分类、优先级），最多 32 个来源；上下文和原提取仍保留全部原消息、审计、已有正式记忆，不删、不重排。候选只是本消费点的提示分类，不是记忆合同或写入权限。
- begin 的原设置快照 `enabled_points` 不含 curator 时不准备或编码材料，后续发送/采用仍复读设置；快照不代表权限。
- choice 显式提供 not_needed/need_data/no_match/abstain，分别记入 tag_outcome/priority_outcome，不能混入标签或等同错误。need_data 缺项引用由宿主按本来源绑定，截断消息携带 full_source_ref；无工具授权，不新建补资料链。
- off/observe/失败保持原批次对象；apply 仅附 `CuratorDecisionAnnotation` 临时输入。消费前核对候选版本与当前完整批次摘要，逐题 error 不污染成功兄弟题。
- 临时注释携带来源类型、精确 ID/hash、分类、优先级、实际模型和输入摘要。prompt 明确非权威性质，放在原稳定说明之后。注释超过原提取字符预算就全部放弃提示。
- 缩批仍由原 backend 执行；批次模型投影按仍存在的 refs/hash 过滤注释，不能借尾部建议遗漏原 processed 覆盖或推进游标。
- 原 run warnings 仅追加固定无正文诊断，不新增持久字段；批次注释与 `_RunContext.expires_at` 仅为内存字段，无持久 schema 迁移。
- `curator_backend.extraction_budget_seconds` 为原模型总预算唯一公式，提取、原 lease 和可选头寸共用。lease 仍是原提取上界加 90 秒；增强最多借剩余正缓冲的一半，另一半留提交。原 lease 真实时间转成冻结 caller_deadline，只缩短增强，不扩 lease 或重置阶段。

## 改动文件

- 新增 `agent_py_agent/agent/memory_store/decision_curator.py`
- `agent_py_agent/agent/memory_store/curator.py`：依赖、原提取入口、lease 时间快照和头寸
- `agent_py_agent/agent/memory_store/curator_inputs.py`：临时注释及按当前 refs/hash 投影
- `agent_py_agent/agent/memory_store/curator_backend.py`：共用提取预算与非权威提示说明
- `agent_py_agent/agent/core.py`：仅 `_wire_memory_curator` 注入 callable
- 新增 `agent_py_agent/tests/test_decision_curator.py`
- `docs/modules/memory/02-progress.md`、`04-structure.md`
- 本交接文件

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_decision_curator.py agent_py_agent/tests/test_memory_curator_v2.py agent_py_agent/tests/test_curator_adaptive_timeout.py agent_py_agent/tests/test_curator_timeout_adaptive.py -q --tb=short
python3 -m ruff check agent_py_agent/agent/memory_store/decision_curator.py agent_py_agent/agent/memory_store/curator.py agent_py_agent/agent/memory_store/curator_inputs.py agent_py_agent/agent/memory_store/curator_backend.py agent_py_agent/agent/core.py agent_py_agent/tests/test_decision_curator.py
git diff --check
```

结果：117 项通过（新 45 项、原相关 72 项）；相关 Ruff、diff 检查通过。没有收费模型调用、全仓 pytest、提交、推送或部署。

覆盖：off 不编码材料，四种题级非选择结果及精确缺项引用，off/observe/apply/失败，单题失败，候选和材料过期，注释字符预算，来源 hash 改变，原缩批，64 题上限但全量材料保留，用户中断传播，原提取失败不推进游标，原提取成功精确推进，真实公共 service→原 worker→原账本三路，极大后台配置与过期 caller 截止保留原提取，lease 坏时间/过期/正头寸及共用预算公式。

## 影响范围

- 默认关闭，不增加正常聊天请求或修改主模型；当前只接 Curator，未接召回。
- 只给原提取模型添加临时建议，不直接保存记忆、修改人格、过滤材料或推进游标。
- 决策保留原 ledger 的 `purpose=decision`、`auxiliary=true` 和真实 Curator run。thread 为空，不伪造会话用量归属。

## 需要主线重点复查

- 父侧 owner_background scope 的权限、关闭通知与固定阶段边界；本片测试不代替该通用合同测试。
- `_annotation_deadline` 只借原剩余缓冲的一半，用户极大期限仍被 caller 头寸收紧。
- `CODEBASE_TREE.md`、总设计、执行 TODO 和最终测试汇总由父侧统一更新；最终 doc-sync/strict-size/包检查仍归父侧 gate。

## 需要其他线协调

- 不修改共用 decision_service/settings/model_call/metrics；公共后台身份扩展由父侧负责。
- 未触 TUI/Gateway 菜单、工具审批、插件线或子代理选择线。

## 剩余风险

- 验证限本地 fake 决策、真实原 worker/账本和原 Curator 提取提交链；没有真实 Jev 质量、服务端时延、实际 TUI 或部署证据。
- 原 Curator 没有独立的持久模型用量结算，本片只在原调用账中按后台 run 留事实，不新增用户会话累计或后台用量数据库。
- 32 来源之外的材料不做可选标注，仍完整交原提取；分类/优先级本身不保证正确或提高记忆质量。

## 后续建议

先由父侧复核 owner 后台公共边界与本片精确 diff，再统一 focused/gate 和本地提交。子代理模型选择可以并行接线；不得把标注当证据、跳过材料授权或直接晋升事实。真实模型对照验收放到全部首批消费者与配置入口贯通之后。
