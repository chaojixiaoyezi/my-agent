# Workstream Handoff

## 基本信息

- workstream：credential-live-preflight；根同线逐文件独占，非作者只读复核。
- branch/worktree：沿根已指定的 main 集成树；基线 `376a476cf1e83b7a8a0bc5dc9878a27517150dd6`。
- owner：凭据入口修复线；日期：2026-10-15。
- 本线未提交、推送、部署、重启、修改真实配置或外发；生产现场仅按根指定请求只读核验。

## 本线目标

修复首次普通 Gateway 请求直接申请持久输入时缺 TaskLink，以及前台 claim 与长期 task 编号不同被误拒绝的问题。沿原任务晋升、执行车道、RuntimeDB 和输入账，不为凭据或 OAuth 创建第二条恢复链。

## 实际完成

- 现场原请求引用 `bca85cd16d35bdd6` 对应的 RuntimeDB 主链存在，ConversationTaskLink 缺失；原前台 claim 使用 `gateway:<request_id>`。只读原 scope 重放得到无 errno 的 `FileNotFoundError`，与现场 `preflight/not_started` 相符；安装字节码与四个相关 canonical 源文件一致。私有交互目录和密钥文件存在，本次尚未进入密钥初始化；没有读取密钥或表单值。
- `request_credentials` 声明原 `promotes_task=True`，各生命周期共用规范 root；原 Registry 在 handler 前调用 `promote_conversation_task_for_run`。`feishu_auth` 仅 `action=authorize` 声明原参数条件晋升，状态查询不创建 TaskLink。
- 主 scope 使用原 `durable_task_id` 区分当前主运行和规范任务；子代理仍优先读原 canonical run/attempt。前台只接受当前宿主 `GatewayTaskBindingWriter` 的准确请求路径，复用 `read_gateway_execution_target` 完整核对原 claim、processing、write-ahead 绑定、终态/取消与运输代次。
- 在同一 owner RuntimeDB 上复用 `ManagedOperationStore.require_authority` 校验当前 attempt/generation/status，并复核准确 main agent_run、task 的 owner/thread。前台 scope 冻结 core attempt；同 claim 换 core attempt 后旧 scope 失效。没有 typed writer 时不猜基础 Gateway 或 owner 队列地址。
- 缺失/不可读 scope 通过 typed `ToolApprovalScopeUnavailable` 返回 `CREDENTIAL_TASK_NOT_ACTIVE`、`execution_scope/not_started`；原 key、record、delivery、receipt 存储分类不变。
- 额外获根授权：普通 `feishu/user` 的 OAuth 只读 status 从 canonical owner 确认本人，不因首次查询缺 TaskLink 失败；管理员和 revoke 保留原执行范围。
- 参考只读 Codex checkout `578c1b2230288104041e880a86d0f7f3a5ca6e47` 的 `tools/context.rs::ToolInvocation` 和 `tools/network_approval.rs::PendingHostApprovalKey`：分别冻结 session/turn/step/call 与环境/host/turn/execution 身份，没有复制另一批准权威。

## 改动文件

- `agent_py_agent/agent/conversation/tool_approval_scope.py`：原主子范围、前台目标/权威核验及 typed 不可用错误。
- `agent_py_agent/agent/tooling/task_credential_tool.py`：原任务晋升声明和执行范围错误分类。
- `agent_py_agent/agent/tooling/feishu_auth_tool.py`：authorize 的原参数条件晋升。
- `agent_py_agent/agent/user_space/feishu_oauth.py`：仅 `oauth_status` 的普通用户查询身份切片与注释。
- `agent_py_agent/tests/test_gateway_credential_live_entry.py`：首次群/用户 Gateway 队列、假模型/假运输、提交/取消/单 Wake、OAuth 授权及状态查询。
- `agent_py_agent/tests/test_tool_approval_foreground_scope.py`：不同 request/run/task/transport/core attempt、准确 base 队列及阴性矩阵。
- `agent_py_agent/tests/test_im_tool_approval.py`：既有群剩余确认拒绝测试补正式合成 HostActor 与 `assistant.configure` 授权前提，不绕过产品门。
- 本交接文件；全局文档、文件树与测试导航由根整合。

## 测试命令和结果

改前先红：首次 group/user Gateway 凭据测试均真实返回 `CREDENTIAL_STORAGE_UNAVAILABLE/preflight/FileNotFoundError` 且无卡；规范 TaskLink 已存在的前台 claim 测试为 `active=False`。修复后两者转绿。强化 OAuth status 输出断言后，改前再次因没有 `not_authorized` 状态失败；status 小切片修复后通过。

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_credential_live_entry.py agent_py_agent/tests/test_tool_approval_foreground_scope.py agent_py_agent/tests/test_task_credential_storage.py agent_py_agent/tests/test_im_credential_card_preflight.py agent_py_agent/tests/test_task_credentials.py agent_py_agent/tests/test_feishu_oauth.py agent_py_agent/tests/test_persistent_task_input.py agent_py_agent/tests/test_unattended_tool_approval.py agent_py_agent/tests/test_im_tool_approval.py agent_py_agent/tests/test_im_credentials_security_matrix.py agent_py_agent/tests/test_ingestion_task_credential_scope.py agent_py_agent/tests/test_task_credential_forms.py agent_py_agent/tests/test_background_tool_approval.py agent_py_agent/tests/test_task_input_capacity.py agent_py_agent/tests/test_task_input_cancel_deadline.py -q --tb=short
```

结果：343 项全部通过。初轮 211 项有一个既有群设置 fixture 缺授权失败；补齐正式合成前提后，该测试及最终组合通过。只有既有 ctypes 布局弃用告警。

局部 Ruff、`git diff --check` 通过；strict code-size 的 `hard=0`、`blocked=False`，未修改 baseline，报告保存在仓库外。根尚需在全局文档同步后运行最终严格 gate；本线结果不代替真实飞书客户端验收。

## 影响范围与剩余风险

- 输入回调仍沿原 TaskLink/输入行/原 Wake：首次提交只唤醒一次准确任务；取消静默退休本卡、释放 refs，不发布业务 Wake。配额、截止、旧卡/旧 attempt、子代理与存储故障矩阵均保留。
- 独立 HTTP 前台审批回调若没有原当前 typed writer，仍 fail-closed；不为解决它把宿主路径写进模型字段，也不按 owner 目录猜队列。该审批来源恢复合同不在本次完成范围。
- 首次普通请求 revoke 与 local/main 的 OAuth 状态仍保留原 scope/管理员身份前提；本次不宣称这些路径已改造。
- 未验证真实飞书卡片字段渲染、回调或登录；没有修改安全锁、重建密钥、补造生产任务或重复外发卡片。

## 建议下一步

根先合并非作者 scope/owner/attempt 复核意见并同步模块文档、树与 TESTS，再串行跑严格 gate；随后由根按既有授权执行准确版本部署和真实客户端验收。适合其他 agent 并行只读复核 OAuth 与输入恢复，TaskLink、claim、生产服务和真实外发必须由根保持单一执行者。
