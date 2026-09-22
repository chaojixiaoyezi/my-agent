# Workstream Handoff

## 基本信息

- workstream：决策模型 TODO11 / TUI 决策设置入口
- branch：`codex/decision-model-integration`
- worktree：现有 `decision-model-plan/my-agent-dsh` 独立工作台
- owner：父代理下的 decision_settings_review 子代理
- date：2026-09-22

## 本线目标

让用户在原 `/model` 浮层管理可选决策设置、逐字段恢复继承，并在明确操作后测试决策连接。所有保存沿原认证配置服务，不切换当前聊天模型。

## 实际完成

- 原 `/model` 菜单末尾新增“决策模型设置”，原选项顺序不变。进入后选择用户长期或本会话临时范围。
- 同一设置读回展示 effective 值和 sources；编辑总开关、默认模型、前台单次/前台阶段/后台阶段时间，以及逐接入点的 mode/profile_id/timeout_seconds。
- 可写字段只取原回执 `field_scopes` 与界面支持字段的交集；不另建权限或默认配置源。线程隐藏后台时间和 Curator，并说明需到用户长期设置。
- 原 overrides 中的历史线程后台字段仍可逐项 reset；界面明确显示旧覆盖只供清理，不暗示 Curator 消费它。
- 模型选择读取原 `list.profiles`，按 `capability == decision` 过滤；不可用模型仍可管理，available_for 仅作显示。共享模型沿原目录，不另造索引；明确清空模型提交空串，恢复继承删除覆盖，两者分开。
- 服务商/决策模型新增编辑复用原 `manage_providers` 入口，不复制凭据表单。保存开关、时间和模式不请求模型，服务失效仍可关闭。
- 每次保存携带打开表单时的 `revision`，同时比较 owner/thread；成功和失败均返回范围菜单重新读取。冲突不自动重放旧修改。
- 测试分成“选择模型”和“开始测试”两步；单纯选择模型、取消确认或保存设置均不发测试请求。测试秒数可调且必须有限正数，默认 4 秒。
- 测试结果展示 message/model_name/elapsed_seconds/error_type/usage.input_tokens；输入未知写“未知”，输出位置留白，不显示价格或输出 token。当前聊天模型、任务队列和停止事件保持原状态。

## 改动文件

- `agent_py_agent/cli/chat_parts/tui_model_menu.py`：仅新增菜单分发和 decision_probe 等待文案。
- 新增 `agent_py_agent/cli/chat_parts/tui_decision_menu.py`。
- 新增 `agent_py_agent/tests/test_tui_decision_menu.py`。
- 本交接文件。

本片不修改 settings、Gateway、策略服务、账本、模型管理表单及共享模块文档。后端运输/探测由父侧实现，field_scopes/runtime_scope 由相邻 HTTP 子代理维护。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_tui_decision_menu.py agent_py_agent/tests/test_tui_model_menu.py -q --tb=short
python3 -m ruff check agent_py_agent/cli/chat_parts/tui_decision_menu.py agent_py_agent/cli/chat_parts/tui_model_menu.py agent_py_agent/tests/test_tui_decision_menu.py
git diff --check
```

结果：23 项通过（本片 14 项、原菜单 9 项）；相关 Ruff 和 diff 检查通过。

本片 6 个实际 prompt_toolkit Application / create_pipe_input 场景使用原临时 owner/thread 设置服务，Gateway 只替换运输与免费固定测试回执；另外 8 项校验秒数与原范围元数据。覆盖 owner/thread 保存、逐点模式/时间、恢复继承、无可用服务关闭、CAS 冲突重读不重放、明确测试与取消、清空模型与继承区别、历史后台字段清理、原服务商入口与聊天选择隔离。真实按键发现并修复 ScrollablePane 的 Widget/Container 接缝。

## 影响范围

- 不新增持久结构、配置层、凭据库、后台任务或真实模型默认配置；此片无 schema migration。
- 用户仅浏览或保存设置不会请求模型，明确测试按钮会调用原运输的 decision_probe。
- 时间变更不重置已开始阶段；该原服务合同在界面说明，前端不计算运行余额。

## 需要主线重点复查

- 原认证模型运输支持 `decision_read`、`decision_patch`、`decision_reset`，payload 为 `{"decision": {"scope": ..., "expected_revision": ..., "changes": ...}}` 或 fields；读取只传 decision.scope。身份只由原 session 解析，菜单不发送 owner_id/thread_id。
- `decision_probe` payload 为 `{"profile_id": ..., "timeout_seconds": ...}`。错误也保留 ok/model_name/elapsed_seconds/usage.input_tokens/error_type/message，网络截止时间与用量归属由后端负责。
- 不要把菜单展示的 field_scopes 当认证边界；原服务仍需验证 owner/thread 和字段范围。
- 父侧统一更新 CODEBASE_TREE、TUI 模块/功能文档与 TODO，统一 doc-sync、strict-size 和打包 gate。

## 需要其他线协调

后端与前端按固定字段合同可并行；settings/projection 和运输合入后需要一次完整原 Gateway→本菜单回归。本片没有占用插件命令文件。

## 剩余风险

- 本轮验证为真实终端输入管道配原设置服务和运输 stub；没有运行真实 TUI 进程连接真实 Gateway 或收费模型，没有检验 Jev 建议质量、实际额度或部署。
- 脱敏回执和测试超时依赖原后端合同；前端只投影指定字段，不负责停止仍存活的网络 worker。
- 本片未提交、推送或部署；全仓严格 gate 由父侧统一执行，线上 CI 未作为本片验收来源。

## 后续建议

建议下一步：父侧先完成原 Gateway/本地模型操作的设置与探测运输，合并 field_scopes 范围修复后做组合验证，再统一严格 gate 与本地提交。可与模块文档同步并行；真实模型验收需要明确调用范围，不能用界面保存成功代替实际模型或建议收益证据。
