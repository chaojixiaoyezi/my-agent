# P5-G 设置恢复基础原语交接

## 基本信息

- workstream：decision-model-plan。
- branch：`codex/decision-model-integration`；基线 HEAD `ef497a904`，叠加本线未提交改动。
- worktree：本线独立 Codex worktree；不修改日常 Gateway 配置。
- owner：主线；日期：2026-09-22。

## 本线目标

为以后有授权的自动对照提供一个原设置事务内的精确恢复动作。原 `patch` 与 `reset` 分两次保存时会暴露中间状态，并在两次之间给用户修改留下覆盖风险；恢复必须在原 owner/thread CAS 内一次完成。

## 实际完成

- `execute_decision_settings_operation` 增加宿主内部 `restore`，请求包含同一 scope 的 `set` 和 `unset`、完整 `{owner, thread}` 版本。它不进入菜单或模型可传的 `user_config` action。
- `set` 复用原字段/作用域/决策模型引用校验，`unset` 只清已登记字段；交集、重复、坏值、越范围、空操作和已删除模型引用均在写入前拒绝。
- 沿原 owner→thread 锁顺序完成读取、CAS、一次版本前进和正式读回，返回原 `before`。用户或其它任务先写入时旧版本冲突，不重读最新版本后盲目覆盖。
- 原进程内设置通知沿同一成功提交出口发出；恢复后实际关闭的在途决策精确取消，普通失败不产生通知。

## 改动文件

- `agent_py_agent/agent/settings/decision_settings.py`：内部恢复操作与原子字段变换。
- `agent_py_agent/tests/test_decision_settings.py`、`test_decision_settings_notifications.py`：真实原文件/线程存储、冲突、范围、删除引用、通知与取消。
- `docs/design/DECISION_MODEL_INTEGRATION.md`、`DESIGN_LEDGER.md`、`LLM_GUIDE.md`、`docs/ROADMAP.md`、`docs/COMPLETED.md`、`docs/tasks/DECISION_MODEL_GOAL.md`、`TESTS.md`：状态与边界。

## 测试命令和结果

```bash
python3 -m pytest -o addopts='' agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_decision_settings_notifications.py agent_py_agent/tests/test_decision_settings_scope.py agent_py_agent/tests/test_decision_model_operations.py agent_py_agent/tests/test_user_config_decision_operations.py -q --tb=short
ruff check agent_py_agent/agent/settings/decision_settings.py agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_decision_settings_notifications.py
python3 scripts/check_doc_sync.py
git diff --check
```

结果：**138 passed**，Ruff、doc sync 和 diff 均通过。测试使用临时原目录和会话存储，不发真实 Jev/普通模型请求。未运行远端提交严格 gate，未提交、推送或部署。

## 影响范围与主线复查

普通 `read/patch/reset` 继续走原服务，`restore` 只作为内部原语；不新增配置项、持久 schema 或第二套设置账。主线集成时需复查模型目录版本升级是否改变旧 schema fixture，并重跑上述组合。

## 剩余风险

此原语不判断实验是否有权恢复，也不能证明保存成功但操作回执丢失时是否该重试；这些必须由后续结构化授权、原操作事实和 UNKNOWN 恢复合同解决。不能用它撤销已经执行的业务工具或覆盖用户后改。请求前时间/次数/输入 token 预算、真实收益评价及自动应用仍未实现。

## 建议下一步

先完成模型切换的真实出站与持久配置代次，再在原设置/调用账边界设计有授权、请求前预留的单样本实验；这一片可与独立召回前判断审计并行，须避免多人同时修改 `decision_settings.py` 和设置测试。
