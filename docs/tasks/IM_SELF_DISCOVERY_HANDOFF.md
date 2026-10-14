# IM 自身能力发现与默认继承交接

## 基本信息

- workstream：P07 / P08 / P09 本人只读发现、精简目录与默认工具继承。
- 基线：`b3e5aae686c067ee19c03672ab717f7526175acb`；使用当前已附属修复工作树，保留开工时既有未提交修改。
- owner：6.1sol 发现实现子代理；2026-10-14。
- 未提交、未部署、未推送，也未向真实飞书发消息。

## 实际完成

- 扩展原 `list_tools`，不建立第二份注册表、能力账或授权入口。完整 manifest 与模型短页共享当前 `ToolRuntimeSnapshot`。
- 宿主通过 `ToolRegistryParams.self_context_provider(snapshot, context)` 注入 `tool_self_context`。身份只读已解析 owner 与原冻结 actor；错误 actor 不投影成合法身份，不接受模型选择其它 owner。
- 本人审批沿原 `read_permission_state`，区分 `ask/auto/full-access`、实际执行模式与 `owner/deployment` 来源。`/status` 共用 `permission_self_context`。
- manifest v3 将旧作用域标签放在 `permission_scope`；`permission_mode` 只表示真实审批模式，未绑定 owner 的独立 registry 明确显示 `unknown`。
- 文件投影读取本次执行器的读写上下文；新增兼容默认值 `ToolInvocationContext.execution_permission_context`，由 `registry_invoke` 只从当次实际宿主边界冻结网络/Shell小字典，模型参数无入口。Shell/网络继续与原 owner/child 收紧事实组合；不投影宿主私有根、完整配置或凭据。
- 原快照已授权集合内区分 `ready/deferred/unavailable/disabled`，给出用途与原原因。禁用/不可用查询不恢复工具，也不说明某份资源已经获授权。
- 模型短页保留每项用途（最多 220 字）、可用性、效果与审批策略；`always_effects` 和 `owner_grant_parameters` 不再丢失。完整参数继续保存在归档。
- `tool_search` 保持原折叠检索，并允许按准确、当前可见工具名取得完整 Schema；不扩展授权。输出与说明明确同线程累计、跨 Compact 恢复、每轮重核权限。
- `role_templates` 是 `SELF_DISCOVERY_TOOLS` 和 `OWNER_TASK_TOOLS` 唯一声明位置，coding 与后台目录复用；read_only 只补发现入口，不默认补云写入、凭据和外发。
- 创建先组合角色候选，再与当次真实父工具快照取交集；最终角色归一化再次按持久的父创建快照收紧。父未持有的云/消息工具不会自动给予 child。
- `capability_request/record_lesson` 继续按原合同是 child 专属控制面，不能因 root 默认隐藏就永久从 child 删掉；创建期仍按 owner registry 复核可用性。
- leaf 的直属下级控制仍由原角色合同删除；默认 leaf 不获得宿主管理工具。正式 `scheduled_job_due` 的完整 owner 目录 `allowed_tools=None` 原路径未改。
- 后台内部默认目录补发现、当前 owner 凭据/云/消息及读产物、补丁、HTTP/Web 通道；owner/task/禁用表与投递路由继续收紧。显式配置保持精确名单。

## 改动文件

- `agent/tooling/registry.py`、新增 `agent/tooling/self_context.py`、`agent/tooling/tool_search_state.py`；必要合同追加 `models.py`、`registry_invoke.py`。
- `agent/contracts/tool_manifest_contract.py`、`agent/gateway_parts/status_projection.py`。
- `agent/agent_core/orchestration/tool_grants.py`、`create_policy.py`。
- `agent/subagents/role_templates.py`、`role_contracts.py`、`agent/conversation/background_tool_policy.py`。
- 新增 `tests/test_im_self_discovery.py`；同步 manifest、runtime scope、default deferral 测试。
- `tests/test_background_main_agent_runtime.py` 的目录测试修复四个无真实 Thread/TaskLink 的旧夹具：使用原 Store 建账，不删身份门或替换生产逻辑。
- 主线独占 `core.py` provider 接线、根文档/台账/配置同步与最终发布；测试使用生产 core 接线，没有手工 provider 注入。

## 测试与证据

- 15 个相关测试文件最终版本：366 通过、0 失败、0 跳过，JUnit `/tmp/im-self-discovery-final-focused.xml`；此前 364 通过批次在 `/tmp/im-self-discovery-focused.xml`。
- `test_gateway_conversation_control.py -k status`：16 通过，JUnit `/tmp/im-self-status-focused.xml`。
- 新增自身发现文件最后单独验证：13 通过，JUnit `/tmp/im-self-discovery-final.xml`。与最终 366 批次有重复，不相加为唯一总数。
- 新测使用真实 SimpleAgent、ToolExecutor、ToolRuntimeSnapshot、Store、角色创建与后台策略；仅模型响应及外部服务 readiness 为假。
- 当次上下文合同追加五文件 70 通过，JUnit `/tmp/im-self-context-boundary.xml`；涵盖注册表、插件运输/读写和父子输入范围。
- fake native LLM 第一轮调用 `list_tools`，下一轮真实收到本人权限事实；无真实模型或外发请求。
- 覆盖普通/群本人、ask/auto 与状态同源、非法审批账安全拒绝、禁用/未配置目录、完整 always 策略、父授权正负例、read_only、跨 Compact 与线程身份、具体资源仍需独立授权。
- Ruff 本线通过；`git diff --check` 通过。严格 code-size 本线无新增硬失败；整体尚有其它线未收口问题，由主线统一 gate 复查，不能当作全仓严格 gate 已通过。
- 初次四个后台夹具红测和中间新增测试断言错误均保留在本轮会话输出；生产身份门未弱化。并行 P05 修复期间两项旧假 agent 测试暂时报错，后续 364/366 批次均绿。

## 需要主线复查

- `core` 必须为正式 owner 和其权限视图注入同一个只读 provider；当前已接线且新测不手工替换。
- `/help` 和模型短导航指向 `list_tools/tool_search`，自然语言导航不替代任何门。
- 关闭凭据等 availability 由凭据线负责，需组合后复跑全部相关 focused gate；查询不打开关闭的能力。
- `background_main_agent_allowed_tools` 默认应为空自动目录；非空显式配置按原合同精确收窄，不自动补能力。
- `CODEBASE_TREE` 请纳入新 `self_context.py`、发现测试及本交接；`TESTS`、设计审计和台账由主线统一记录最终状态。
- 非法审批策略在执行前仍拒绝 `list_tools`，`/status` 可返回固定安全错误投影；不增加 read-only 绕过审批策略损坏的旁路。

## 建议下一步

先由非作者复核 scoped 身份、父级交集和当前真实工具可用性，再由主线串行运行严格 gate 与准确源码打包；发现、凭据与持久等待可以并行审查，但只能由各文件负责人修复。真实 IM/模型验收须单次 prompt 后旁观原日志与产物，不补任务产物、不读宿主秘密、不替员工审批；部署、main 推送和本机生命周期只由主线执行。


## OAuth 交叉复核追加实施（2026-10-14）

- 根转交本人 OAuth 修复后，本线独占 `user_space/feishu_oauth.py`、`tooling/feishu_auth_tool.py`、`gateway_parts/feishu_oauth_service.py` 与新增 `tests/test_feishu_oauth.py`；`status_projection` 同源审批函数注释补齐。
- `feishu_auth` 绑定宿主 `__network_access_mode`，拒绝当次网络禁用时零私人卡；发起和 UAT 使用继续复核真实 run/child/owner 网络、本人身份与原执行资格。普通/群不能凭显示身份扩权，本机管理员回调再次核对原私人绑定。
- 配置只读 `deployment_capability_config(home)`；冻结 app/应用密钥摘要、回调和请求 scopes。当前字段省略不是兼容授权，配置变化必须重授权。
- 兑换或刷新均在 quota/记录锁外，提交时复核原行、任务和撤销；兑换失败写 failed 并结算原输入，不留 exchanging。ready 已保存而 Wake 提交失败时保留密文，重复原 callback 只补幂等恢复，不再次兑换 code。
- 严格校验 token/refresh 类型、期限、供应商本人证明形状与密文；没有 refresh token 不虚报 refresh 授权，已有 refresh token 缺期限则拒绝。status 如实给 expired/configuration_changed，回执无 token/state/链接。
- 参考官方 CLI v1.0.98 的 `internal/auth/uat_client_refresh_test.go` 与 token 解析：OAuth v2 成功可省略 code，须明确有 access_token；user_info 保持原 code/data/open_id 证明。新的真实 HTTP adapter 合成测试没有绕过该入口。
- OAuth 自身 55 项；与本人发现、折叠、云工具和云实时授权组合五文件 149 通过、0 失败、0 跳过，JUnit `/tmp/feishu-oauth-freeze.xml`。全部合成接口，无真实模型、账号登录或飞书外发。
- 本线 Ruff、文档同步、严格 code-size（hard=0）和 diff-check 通过；主线仍须按组合后的准确源码跑完整发布 gate。OAuth 卡运输失败、错误账号/配置变更、管理员解绑、坏 token/PKCE、网络收紧、锁外撤销竞态、ready 后恢复故障均有定向回归。
- 交叉复核发现的取消输入未接、子代理 CANCELLED 仍挡父 Wake、1000 条对账遗漏覆写输入期限，已交原持久输入负责人修复；本线未越界改其产品或测试。

建议下一步：先由非作者检查 OAuth 当前冻结版本，再由根统一验证 UAT 对权限证明与业务请求使用同一身份，随后严格 gate 和部署。不同 owner/群/child 的权限负例可以并行复核，但不得真实外发或把合成登录当正式员工授权已完成。

## 容量收尾与只读交叉复核追加（2026-10-14）

- 原 `request_oauth` 的失败捕获已覆盖输入创建失败，无需增加第二条补偿路径；同步双层注释明确容量拒绝也清密文并写 failed，没有依赖时不创建补偿记录。
- 新第 56 项使用原 TaskStore/输入/Wake 填满 32 个准确 refs：OAuth 请求固定错误、failed、密文清空、零新卡且无新输入行；原输入归档后取消并发布 Wake 放槽，下一次授权正常创建。定向证据 `/tmp/feishu-oauth-capacity.xml`；最终全部 56 项通过 `/tmp/feishu-oauth-final56.xml`，本次 Ruff、doc sync、diff-check 通过。
- CLI/UAT 只读子集 22 项通过 `/tmp/feishu-cli-readonly-review.xml`；单独合成投影探针确认最终 CLI 不读取 bot token，UAT/应用秘密在值、键及 JSON/URL 回显中均净化。
- 非作者发现参数展示直接读 `config.my_agent_home` 而实际宿主沿 `configured_home_root` 的环境优先级；根修复后，同一个合成 `MY_AGENT_HOME` 探针中 runtime、parameter 与 fact 已一致。
- 非作者临时复制官方源码，只把真实官方 cmd 合成运输用例的 bot 改为 user，复现先验 `user_info` 被拒后业务已成功却残留 scope 错误；已交 CLI 根负责人修复并增加真实官方 user 生命周期回归。本线不修改 CLI、配置或发布代码。

建议下一步：等待持久输入和 CLI 负责人冻结后，由根统一运行最终组合 gate；准确源码提交后再核对发行内源码摘要、wrapper 与 native SHA。可以并行做负例只读复核，不得把临时合成运输当真实飞书员工授权或真实外发验收。
