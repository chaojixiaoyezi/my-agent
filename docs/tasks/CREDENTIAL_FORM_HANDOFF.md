# 简单凭据表单 S23 交接

- 日期：2026-10-14。
- 工作树：`credential-form-s23-20261014`，基线 `56617f7a8`。
- 状态：本地实现与合成回归通过，未提交、未推送、未部署、未真实发卡。

## 完成

沿原 TaskCredential 记录/加密/输入依赖新增 `authentication=collected` 与 `collection_mode=username_password/password`，URL 可缺省并在卡上输入；可选名称备注只是元数据。提交先结构校验 URL 并绑定准确 origin，不发登录请求、不改变网络权限。默认保存标 `verification_status=unverified`，回执明确尚未验证。

同一 `request_credentials operation=configure` 可给已收集引用绑定 basic/header/bearer/login/signature；配置只引用原字段，登录仍须同源，不重新索要秘密。collected 引用直接 HTTP 使用明确拒绝，避免猜协议或任意目标发送。

已 ready 引用更新创建同目录新候选 `credential_ref`，以 `replaces_ref` 关联；原记录及原 HTTP 使用保持可用。新候选不会自动覆盖旧引用。pending 重发仍用同引用增加 generation 并重新签名，退休准确旧输入且不产生取消 Wake。旧回调、重复提交不能写入新代次。未确认送达的 pending/failed 原卡拒绝直接重发。

群请求签名包含 `presentation={kind:group,chat_id:owner_id}` 与 `submitter_policy=current_group_member`，投递到原群。提交按运输提供的 `body.submitter` 复核准确群、真实 actor 与当前成员事实；取消限原请求人，替换限原请求人或原提供者。实际提供者写 `submitter_actor_id`，名称不参与授权。私人沿原身份锁。

HTTP 明确 401 记 `verification_status=rejected` 并阻止下次使用；403/网络错误不冒称密码错误，不自动发卡。登录 401 在解析响应前登记拒绝。没有可靠验证方式一直保持 unverified，不以业务 2xx 冒充认证成功。

## 文件

- `task_credential_schema.py`：收集模式字段与 collected schema。
- `task_credentials.py`：卡片、请求、提交、代次、候选及群接线。
- 新 `task_credential_forms.py`：表单/可信提供者校验、配置认证、退休旧输入。
- 新 `task_credential_verification.py`：准确代次认证拒绝事实。
- `task_credential_http.py`：collected 拒绝与401记录。
- `task_credential_tool.py`：可缺省URL/协议、collection_mode、configure schema与执行。
- 新 `test_task_credential_forms.py`：四组合、秘密URL拒绝、旧有效保留、迟到卡、401/403和不发新卡。

## 验证

`/Users/xiaoyezi/my-agent-dsh/.venv/bin/python -m pytest agent_py_agent/tests/test_task_credential_forms.py agent_py_agent/tests/test_task_credentials.py agent_py_agent/tests/test_task_credential_http.py -q --tb=short`：27通过。

相关生产/测试文件 Ruff 与 `git diff --check` 通过。严格 code-size 检查 `blocked=False`；自动生成报告已恢复，避免无关改动。

## 集成关注与边界

1. 根需将本线 `task_credentials.py` 请求片段与 storage 线错误阶段包装手合；工具 schema/execute 与 storage 线同文件手合。群运输线独占 Gateway/适配器。
2. 根同步 CODEBASE_TREE、TESTS、设计/台账；本线按分工未改总文档。
3. configure只允许collected的首次认证绑定；修改已配置协议应重新明确申请新卡，避免旧密文用途悄然变化。
4. 新候选不自动替换监控已存引用；调用方须明确采用新引用。可验证成功的自动晋升策略未实现，不能声称已验证或已替换。
5. 原 login GET/HEAD 的一次401 session刷新仍保留，未新增重试策略；根应确认与“错误仅汇报”的期望是否要求退休原刷新行为。
6. 真实飞书输入password兼容、可见性、提交清空及群成员证明依赖运输线和真实客户端验收，不能由合成测试证明。
7. 尚未运行全仓严格远端提交gate，本线不提交远端。

建议下一步：根优先集成三个片段，复核候选引用与监控实际采用路径，再运行组合安全回归和完整严格 gate。非作者 agent 可并行审查群运输与表单边界，不能并发改同一请求函数，也不能用真实秘密或旁路发卡冒充验收。

## 容量替换追加

根要求补充后，通用 `create_input_dependency` / `update_task_input_refs` 新增 `replaces_input_id`：只接受同 owner/scope/provider 的 pending 输入，在原 TaskStore 锁内容量计算时扣旧槽，persist成功后同次换ref；登记失败原ref与原卡恢复。无先删旧再登记的容量窗口。新增容量1重发与登记失败保留旧卡回归。涉及额外两个通用输入模块，根需同步该合同文档。组合凭据/HTTP/输入容量目前33通过（追加失败恢复用例待最后一次重跑）。
