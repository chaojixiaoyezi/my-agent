# 群共享凭据交接

## 基本信息

- workstream：群共享凭据发现、选择与授权。
- branch：main；基线 `f1961cf00d379b80c3e7208a42ccb008c3f31214`。
- owner：credential_live_preflight；2026-10-15。
- 本线独占：新增 `user_space/task_credential_groups.py`、`tests/test_group_shared_credentials.py` 与本文。原生命周期、HTTP、工具、配置及全局文档分别由主线/工具线整合，不交叉写入。

## 本线目标与实际完成

用户明确：群卡提交的凭据可供本群使用；按实际填写人优先本人同站点凭据，本人尚未提供时可使用本群任一可用凭据。本人未撤销的拒绝、过期或待配置记录不能触发换用其他成员；本人明确撤销后允许使用其他共享凭据。名称、备注、模型正文和 HTTP 响应正文都不参与权限判定。

原 owner 私密目录中的 TaskCredential row 是唯一存储。本模块只做本次调用的只读选择与摘要，不另建数据库、索引、别名、租约或密文副本。实时成员由原 `group_operation_actor` / `current_group_authority` 核验；不增加逐人 grant。原网络、安全沙箱、话题身份、监控授权及停止门不变。

接口：

- `new_credential_access_scope(agent, scope)`：只有能力开关明确开启、规范群 typed actor 和完整实时成员通过才选 group；私聊和关闭开关保持 task。启用群缺事实直接失败，不变成等待。
- `group_credential_access(agent, row, scope, operation='use')`：只对 task/缺字段返回 False 交原门处理；明确 group 拒绝直接抛安全 typed 错误，未知 scope 不能隐式降级。精确 use 引用要符合本人优先；status 对当前成员安全可见；configure/update/revoke/share 只限原请求人或实际填写人。关闭共享仍可 status/revoke，不阻止停用。
- `discover_group_credentials(agent, *, url='', name='', operation='list')`：只输出白名单安全摘要、推荐引用、推荐原因、本人存在事实。find 先按原结构 URL 归一 origin，先判断本人存在，再过滤可用性和名称，不能通过另一个 name 绕过本人优先。无 URL 的 list 不跨站推荐。`shareable_credentials` 只列本人可管理的旧 ready 群任务凭据，复用原 `assert_credential_record_signature`，其他成员看不到旧私用引用；不自动 share。
- `resolve_group_credential(agent, row, scope, *, origin)`：只接受当前原 group row 作为同群/准确 origin handle；有本人记录时选本人当前可用记录，本人全部不可用就快返。没有本人记录时优先仍可用的请求引用，否则优先原实际填写人的有效新记录，再稳定选共享候选。解析不改旧 handle、不复制秘密。原 HTTP 调用方负责保留 requested/effective opaque refs，并将实际认证状态写实际选中引用及代次。

## 生命周期、并发与边界

own 只读取 `submitter_actor_id`，不能把请求人当作填写人。revoked/cancelled/failed 以及未提交 pending 不占 own 池；未主动撤销的 rejected/expired/待配置仍占 own 池。多个本人有效引用可用，C 没有本人凭据时也可显式选另一个有效共享引用，稳定推荐不是唯一许可名单。

原 `replaces_ref` / `replaces_generation` 仅生成本次 superseded 投影：同 owner/group/origin、原代次严格匹配、合法原请求者/原填写者发起更新、原提交时间和 ready 期限有效，才排除已成功替换的旧记录。新记录后来被撤销仍不复活旧 key；取消或未填的新卡不退休旧 key。坏关系保守保留旧 own，循环关系拒绝；不修改旧 row 或历史任务。

所有成员查询必须在调用方原凭据写锁外执行；管理调用方先校验原观察值，再在原锁里做 unchanged/CAS。没有持久权限缓存，也不把一次成员事实变成可跨请求使用的授权。主子身份沿原 ToolApprovalScope、TaskLink 与 HostActor；child 另核本次 run/attempt 与 canonical 活动子执行一致，不能由旧 attempt 读取最新资格。群 topic 只投影同一 base group，原 actor/topic 不改写。

慢成员查询返回后再次纯本地核验原 scope/child attempt；查询期间 claim 结束或 child 执行上下文换代即拒绝，不能消费已失效的结果发请求。此重核不做第二次 REST，不是新增授权缓存或原操作之外的重试。

认证拒绝通知用 `operation='notification'` 检查精确原 ref 仍在未被替换的 current rows，不要求 usable，允许原 rejected 行继续提示用户处理。成功提交 replacement 后旧行不再发通知；pending/cancelled 替换不使原通知失效。status 仍能查看旧行，通知不借 resolver 转成新引用，关闭共享也不继续群通知。

扫描只枚举原受保护目录的规范 `cred_*.json`，逐段 no-follow 读取，不解密。损坏 JSON、无效编码、链接、未知 scope 不能被跳过后误判为“没有本人凭据”。摘要不输出密文、认证头、query URL、备注、用户名、任意 metadata 或表单值；错误不携原异常正文。

## 参考与区别

先核本仓凭据、HostActor、群治理、工具发现和原卡 MAC 合同。参考 OpenClaw `src/agents/auth-profiles/order.ts` 的 eligibility/order 分离及显式 order 为空仍拥有选择权（约 269–280、343–355 行），复用“先定可选权限集合，再做稳定推荐”的模式；本群 own-first、主动撤销后可共享是本次用户明确语义，不照搬参考项目的 cooldown/轮换或错误后重试。

## 测试与证据

仅合成临时 owner、实际 Store/TaskLink/claim/typed HostActor、原 request/card 回调加密和 fake 官方完整成员/fake HTTP；没有真实平台、配置、服务或凭据操作。

```bash
python -m pytest agent_py_agent/tests/test_group_shared_credentials.py -q --tb=short
python -m ruff check agent_py_agent/agent/user_space/task_credential_groups.py agent_py_agent/tests/test_group_shared_credentials.py
```

原新增 API 缺失红、旧 child attempt 曾可借最新资格的红，以及坏 replacement deadline 会退休 old own 的红均保留于仓外测试证据；后续定向绿结果及最终 SHA 由本线 frozen handoff 记录。测试覆盖 A/B/C、多引用、同群新任务、子执行旧 attempt、话题与跨群、私聊/退群/缺事实、管理分权、关闭开关仍可撤销、损坏/链接、秘密 canary 摘要、显式旧 task share 提示，以及实际 HTTP 401 → 正式 revoke → 下次调用旧 handle 使用 A，403/超时零换号重试。

最终本线直接测试 60 passed，0 failure/error/skip；含通知旧 ref 与成功/取消/待填 replacement、成员查询期间 claim 结束/child attempt 改变后的零 HTTP 发送。Ruff 通过；原严格尺寸检查 hard=0 / blocked=False，新增通知和查询后复核由主线最终同源 gate 再绑定。源码/测试/本文冻结摘要记录于仓外 `group-shared-source-frozen-handoff.json`，本线不再编辑。

## 需要主线重点复查与剩余风险

- 主线原 HTTP/管理/发卡接线及工具线 schema、find/list/share 是本模块生效的必要整合，分别保留本线定向证据，不把接口测试当租户真实成功。
- 同群共享不扩大网络权限；HTTP 401、403、超时的处理仍沿原安全 typed 状态，不能按正文猜密码错误。真实服务若不按标准状态拒绝认证，不能保证自动识别。
- 列表扫描为原 owner 记录的一次只读投影，没有新增索引；未做大量群记录/大租户容量承诺。原 member 查询可能沿既有最佳努力身份展示投影写索引，不构成权限缓存。
- 全局 TESTS、DESIGN_LEDGER、CODEBASE_TREE 与统一严格 gate、发行、真实群验收由主线完成；本线不提交、不推送、不部署。

## 建议下一步

主线先独立审查同一组 API 与原锁外授权/锁内 CAS 接线，再跑整合 focused/严格 gate；工具线可并行独立复核实际 find → web_fetch 与旧 task share。真实验证必须继续由唯一平台操作者单 prompt 发起，核对 actual submitter、requested/effective refs、认证失败后的用户主动撤销及跨群零发送，不能把模型措辞当账本事实。
