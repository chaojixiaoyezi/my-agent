# 凭据 HTTP 错误与有限会话交接

## 基本信息

- workstream：凭据 HTTP / 认证错误最小切片。
- branch：main，基线 `fb2984d28a00c31984aa21a0bfd67661170e7850`。
- owner：凭据线；与主线默认保留期、后台通知线分文件协作。
- date：2026-10-15。

## 本线目标与原缺口

提交后默认凭据不自动过期由主线实施；本线保留原 owner、actor、root/job、origin 与网络门，修复错误分类及有限登录会话。旧 `_login_headers` 在判断 401 前先要求 `RawResponseParts`，而真实网络层对 HTTP 错误返回 `ToolHandlerOutcome`，导致登录 401 未记拒绝，403、超时和网络错误都被改成 `CREDENTIAL_INPUT_UNAVAILABLE`。不能用错误正文猜密码错误，也不能自动发卡、取消监控或重做写请求。

先读原凭据 HTTP/verification、web_fetch 网络/HTTP 分类及回执净化、InputDependency、调度和后台公开回执合同。复用原 `PERMISSION_BLOCKED` / `TOOL_TIMEOUT` / `NETWORK_REQUEST_FAILED` 分类和宿主净化器，不引入依赖、配置、另一个凭据库或新错误码。此前成熟参考索引中的宿主输入中断与原状态保留方向保持；HTTP 分类的直接权威是本仓库已验证的 `web._format_http_error` 和 `web_fetch_runtime._finalize_hop`。

## 实际完成与改动文件

- `user_space/task_credential_http.py`：登录失败保留原 typed 结果；不向模型透出任意登录错误 body，仍向原归档前净化器交回本次秘密。200 缺少声明的 token/Cookie 等会话信息安全返回 `CREDENTIAL_INPUT_UNAVAILABLE/http_status=200`，不认定密码错误。GET/HEAD 保留一次原有 401 会话刷新，POST 不重放业务。会话提交复用主线 `credential_ready_expired`；ready 的零 deadline 不截断有限 session，正 deadline 继续取 min；最新记录已 rejected 时不写回缓存或发送后续业务。
- `user_space/task_credential_verification.py`：只从原两种响应 DTO 取整数 HTTP 状态；仅明确 401 记录拒绝。原配额与记录锁内只允许同凭据引用、正整数 generation、同密文且此前非 rejected 的首次转换；迟到新代、换密文和重复/并发拒绝不改写时间或产生新的行动义务。
- 新 `test_task_credential_http_failures.py`：临时 owner、假运输、真实工具/存储边界，无真实网络或账户。
- 本交接。全局 TESTS、DESIGN_LEDGER、CODEBASE_TREE 由主线整合；默认 TTL/schema/config 与后台通知由其他独占线负责。

## 安全公开事实

只有原 HTTP 401 生成 `credential_authentication`：

```json
{"status":"rejected","http_status":401,"record_updated":true,"user_action_required":true,"credential_ref":"原 opaque 引用","generation":1}
```

`record_updated` 与 `user_action_required` 同为本调用是否新增同引用同代拒绝；false 时省略引用/代次，不把旧请求的拒绝误当新代坏凭据。引用只是原授权 opaque 标识，不能据它解密或跨 owner 使用。通知线仍须消费原真实工具 operation、核对当前 owner/scope/代次与 rejected 状态，并沿稳定引用/代次去重；不能靠模型 dict 或正文创造通知资格。本线只提供事实，不宣称消息已经送达。

403 仍是访问/权限拒绝，超时和网络保持原分类，200 会话提取失败不猜认证原因；这些结果不写 rejected。成功 HTTP 也不自动证明密码或业务已验证。401 后原引用被原使用门拒绝，用户可以决定更换或停用；不会自动发新卡或自动取消监控。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_task_credential_http_failures.py agent_py_agent/tests/test_task_credential_http.py agent_py_agent/tests/test_task_credential_forms.py agent_py_agent/tests/test_task_credential_lifetime.py agent_py_agent/tests/test_task_credentials.py -q --tb=short
```

首红记录 10 项失败，其中 9 项暴露上述产品缺口，另 1 项为 bearer 误用双字段登录夹具；最终补合法单字段夹具覆盖 bearer/header，没有放宽产品 schema。仓外 `credential-http-failure-red.xml` 保留。最终 focused/XML/SHA 以仓外冻结交接为准，不累计中途组合。

覆盖：登录 401/403/timeout/network、401 后同引用禁止继续认证、其它故障后引用仍可用、basic/bearer/header/signature 拒绝、GET 刷新失败不再发业务与 POST 不重放、同代拒绝阻止缓存复活、200 缺会话不是密码错、有限/不限期 ready 下 session 仍有限、迟到 generation/密文、顺序重复与双线程原锁仅一次提醒资格、非 typed/浮点 HTTP 状态和秘密 canary 隔离。

## 通知审计与剩余边界

当前后台并非无条件通知：新 IM 计划可默认 local；预检失败与工具规则失败原有 suppressed 出口；origin+explicit 允许模型选择 internal_only，已有强制公开义务只含附件/用户插话。仅补 HTTP 错误事实不能据此承诺所有故障已通知。后台线需在原资格、原表达、原 outbox 内收口，不造新调度器或用关键词硬拦普通回复。

改密码也不一定立即让目标服务已有 Cookie/Token 失效；只能在实际使用收到明确拒绝时知道。登录会话仍沿有限 session_seconds，不随凭据默认持续保留而无限缓存。未做真实登录、外部网站验证、服务或配置修改，合成测试不等同真实目标服务认证语义。

## 建议下一步

主线先合并 HTTP 事实与后台通知的准确引用/代次合同，独立复核首次转换去重、旧代发送前撤销和原 outbox 幂等，再统一 focused/严格 gate。可以并行准备离线 runner；真实 UI、服务、配置、外发及用户决定是否重填/停用继续保持唯一操作者，不能替用户自动重发卡或改监控。
