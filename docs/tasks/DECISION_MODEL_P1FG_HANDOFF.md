# 决策模型 P1 服务、原账本与用量交接

## 基本信息

- workstream：decision-model-plan
- branch：codex/decision-model-integration
- owner：父代理集成；Astra max 实际调用，high 共用设置与策略
- date：2026-09-22
- 基线：d57368ed1；本片仅本地实现，尚未发布/部署

## 本线目标

让可选决策真正经过原配置、短期限 HTTP、原 worker/准入和原调用账。
关闭、超时、没额度或设置变化时仍保留原业务路径，不增加第二套配置、计费或调度状态。

## 实际完成

- 共用设置和原 user_config，完整字段/迁移见 [设置交接](DECISION_MODEL_P1E_HANDOFF.md)。
- `begin_decision_stage` 在准备材料前固定阶段；`decide` 只返回建议，观察模式不能采用。
- 同进程设置通知精确取消，逆序通知不回滚覆盖；发送及采用前复查配置、身份、连接与期限。
- 稳定资源键不含 operation_id；旧 worker 未退出不能换阶段叠加。
- 实际 worker 复用可选准入、身份头及 HTTP observer；caller/worker 分别保留同一本账的准确记录。
- 原账本终态单调、用途互斥分区、逐字段 usage 来源和累计快照增量；缺报不是零。
- 调用结束自动更新活动 TUI 原统计行：额外决策输入，无输出/价格，不覆盖生成轮/工具/缓存/速度。
- `usage_only` 不等待持久会话写锁；原下一模型边界保存显示，finalizer 保存用量，不提前结算混合活动范围。

## 改动文件

- `agent/conversation/decision_service.py`、`decision_policy.py`、`decision_model_call.py`。
- 原 `contracts/model_call_ledger.py`、`agent_core/model/call_runtime.py`、`usage.py`。
- 原 conversation `model_metrics.py`、`store_usage.py`、`auxiliary_model_call.py` 及 finalization。
- 原 TUI `tui_model_metrics.py`；设置/模型目录/线程迁移文件见设置交接。
- 定向测试、配置 YAML、设计/进度/文件树同步。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_decision_model_profiles.py agent_py_agent/tests/test_decision_protocol.py agent_py_agent/tests/test_typesafe_decision.py agent_py_agent/tests/test_decision_call_resources.py agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_decision_settings_notifications.py agent_py_agent/tests/test_decision_service.py agent_py_agent/tests/test_decision_service_http.py agent_py_agent/tests/test_decision_model_call.py agent_py_agent/tests/test_decision_usage_metrics.py agent_py_agent/tests/test_model_call_ledger.py agent_py_agent/tests/test_model_call_ledger_partitions.py agent_py_agent/tests/test_model_profiles.py agent_py_agent/tests/test_model_provider_management.py agent_py_agent/tests/test_user_config_capability.py agent_py_agent/tests/test_conversation_store.py agent_py_agent/tests/test_tui_model_metrics.py -o addopts='' -q --tb=short
python3 -m pytest agent_py_agent/tests/test_decision_service_http.py -o addopts='' -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
```

结果：联合 399 passed；随后新增实际会话写锁争用检查，HTTP 组合 8 passed。
Ruff 和 strict code-size 通过（hard=0），未放宽尺寸基线。
`python3 scripts/check_doc_sync.py --staged` 与 `git diff --cached --check` 均通过，仅检查本片 staged 文件，
避免把并行 P2 尚未交接的变更算入本片。
全工作树 clean-package 当前会拒绝并行 P2 新增且未跟踪文件，不作为发布通过证据；本轮无远端提交。

## 影响范围与主线复查

原目录 v4、线程 v10 的迁移必须与另一任务集成保留；原字段、模型选择和未知扩展不丢失。
显式保留句柄只保护原账本明细/累计范围，不构成新 ledger；所有路径 finally 释放 caller 保留。
异步实际 worker 仍可能存活；Python 线程不承诺强制终止阻塞 DNS，只保证放弃等待和残留容量受限。
只有本地 HTTP 数据流，不能据此证明供应商模型质量、已安装 TUI 或真实业务消费者通过。

## 需要其他线协调

插件任务独立推进。本线不修改其原工作区、不改变日常模型、不重启 Gateway。
本交接时另一任务正将 `tooling/cancellation.py` 迁到 `common/cancellation.py`；后续隔离分支合入已提交插件基线 `f04ec3a42`，本线新增调用已直接导入公共模块，未恢复旧 facade。后续未提交工作仍待集中对齐。

## 剩余风险

- 跨进程设置变化通过采用前复读拒绝旧建议；没有跨进程 socket 即时取消广播。
- 同时独立后台范围的即时显示与持久输入 token 归属须随其业务入口验收，不冒用前台会话；决策不写 USD 成本账。
- P2 消费者、完整设置菜单、真实 Jev/官方 MiniMax 与多 TUI 尚未验收；默认保持关闭。

## 建议下一步

并行接 07 子代理选模型与 08 Curator 标注。创建锁内不得等待网络，Curator 使用真实 owner 后台身份，
建议不能跳过原授权、模型预算、记忆验证和游标提交；原设置/调用层已稳定，无新证据不扩大基础层工作。
