# P5-C 现有 Todo 优先级建议首片交接

## 基本信息

- workstream：P5-C 规划建议，仅当前主代理已存在的 open Todo。
- branch：`codex/decision-model-integration`；共享独立 worktree，起点 HEAD `ef497a904`，其他并行线原有 dirty 保留。
- owner：planning/Todo 消费接缝；2026-09-22。

## 本线目标与实际完成

新增默认关闭的 `planning` thread 决策点，复用原 owner/thread 设置、Decision profile、阶段/单次期限、用量账本和采用前有效性核对。用户可从现有 TUI 设置入口自行调 `off/observe/apply`、模型和秒数；主代理也可经原 `user_config` 结构化设置服务修改，设置不会自动发 Jev 请求。

只在主代理 `task_progress(action=read)` 返回**当前 canonical 账本**时触发：要求本轮展示代次/修订、2–24 个未关闭项、完整且有界的当前问题。候选来自原 Todo exact ID/标题/状态；复用派工模块的 canonical child ID 排除自动种入的子代理展示行。Jev 仅回答一个“优先评估哪个已有项”的 choice，也可回答 `not_needed/no_match/abstain/need_data`。`observe` 不增加模型可见字段；合法 `apply` 只向原 read 回执追加 `planning_priority_hint`，原 items 顺序、状态、账本、Goal、子代理和权限都不变。主代理若派工，仍自行调用原 `create_subagents` 走原合同。

等待后重读同一账本，复核当前问题、候选内容与顺序、display generation/revision、Decision 策略/连接/身份与绝对期限。关闭、断连、超时、异常、非选择、历史 run、child、无计划或超长问题保留原回执；用户停止照原链传播。网络请求只出现在有多项当前 Todo 的 read 路，不随模型 token 或其他工具调用触发。详情边界见 [前置审计](DECISION_MODEL_PLANNING_AUDIT.md)。

## 本线文件

- 新增 `agent_py_agent/agent/agent_core/decision_planning.py`、`agent_py_agent/tests/test_decision_planning.py`、本交接。
- 小范围编辑 `agent_py_agent/agent/agent_core/task_progress_tool.py`、`agent_py_agent/agent/settings/decision_settings_schema.py`、`agent_py_agent/agent/settings/config.py`、`agent_py_agent/config/agent_config.yaml`、`agent_py_agent/cli/chat_parts/tui_decision_menu.py`。
- `decision_settings_defaults.py` 的点到 AgentConfig 映射由既有注册表动态推导，无需新增专门分支；`_memory_types.py` 属记忆点，不拥有 planning 默认。共享 `DESIGN_LEDGER.md`、Goal、TESTS、CODEBASE_TREE 由主线按并行整合边界更新。

## 测试与证据

定向运行：

```bash
python3 -m pytest -o addopts='' agent_py_agent/tests/test_decision_planning.py agent_py_agent/tests/test_task_progress_tool_dispatch_reconcile.py agent_py_agent/tests/test_task_progress_advisory.py agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_decision_settings_scope.py agent_py_agent/tests/test_tui_decision_menu.py -q --tb=short
```

结果：133 passed。Ruff 对本线代码/测试通过；`python3 scripts/check_doc_sync.py` 为 `DOC_SYNC_PASS`，strict code-size `blocked=False`，`git diff --check` 与本线 py_compile 通过。全目录 Ruff 当时只剩并行 ownership 的 `_tool_loop_service.py` 与 `test_decision_pre_recall.py` 两处 I001，已分别交给对应 owner；本线未改。单测使用真实 Todo 账本和原 read 工具，仅 fake 原 decision service；覆盖设置继承/秒数/TUI、apply、observe/off/错误、非选择、坏答案、账本/问题过期、历史 run/child/单项/空账本、取消与 child 展示行。未启动 Gateway、未请求真实 Jev，不能据此宣称真实规划质量已验。

主线随后用私有隔离 owner 的原 Todo read 工具与真实 Jev 做两轮 `off/observe/apply`：首次 4 秒观察请求真实超时，原回执不变，后续采用因原冷却零请求；第二轮仅把隔离期限改为 8 秒，关闭零请求，观察与采用各一次成功，实际 `jev-1.13.0`，各输入783、约0.75/0.77秒，均建议精确候选 `todo_2`。采用只追加 `todo-rollback` 软提示，原 Todo 顺序、状态与磁盘账不变；临时 thread 设置已恢复，原模型目录哈希不变。共3次真实HTTP尝试，脱敏证据在仓库外 `~/.codex/private-tests/decision-model-live/p5c-planning/{20260922T224010,20260922T224101}/summary.json`。这验证接口与超时保留，不代表实际 Gateway TUI、用户业务规划质量或任务完成收益。

## 主线复查与剩余风险

- 主线需把新增模块与配置点登记到共享导航和测试说明，并复核本文件与并行线共同修改的 settings/TUI 行，不要从当前脏工作树的整份 `git diff` 误认单线 ownership。
- 首片只在主代理主动读取 Todo 时提出建议；新任务尚无可信计划、或模型未读 `task_progress` 时沿原规划。多个 read 可各自调用一次 Jev，均受点/阶段短期限约束；若真实日志显示同轮重复读取显著增加延迟，再在宿主原运行身份内设计一次性去重，不建第二持久缓存。
- 决策请求包含当前问题和有限 Todo 标题；仅在用户已配置并打开本点时发送。真实 Jev 的延迟、`need_data` 分布及建议质量待少量真实验收。`task_progress` 原 serial 工具执行期间会等待该短时可选请求，须在验收中观察多任务并发读写延迟。

建议下一步：主线先做共享文档登记和本线聚合测试，再用已授权真实 Jev 的一组有 2–3 个原 Todo 的普通中文任务分别验 `off/observe/apply`、输入 token 与超时回退；可与召回/子代理模型线并行，但不要改原 Goal 或创建权限链。
