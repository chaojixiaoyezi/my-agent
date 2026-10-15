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

## 通用输入等待收尾追加（2026-10-15）

### 真实故障与原证据

- 基线为已发布 `.20261015.3` 的 `0a19363c53f254b6ec6034027dfbe2c47faa2d08`。本次只读准确合成群原请求引用 `ce2a2e143dc75ab2`，不读取密钥、密码或表单值。
- 06:52:19 本机原入站到达；凭据操作成功且卡片已发送，原输入 pending/armed、TaskLink active。主运行随后记录 `failed/unfinished/TASK_INPUT_WAIT/input_dependency`，Gateway 为 `USER_REPLY_UNAVAILABLE`；实际只有业务模型采样，没有无工具等待表达。
- 根随后以真实卡点击取消，07:02:58～59 本机回调成功；本线只读确认原输入 cancelled、armed=false、retire、准确refs释放。它证明取消链，不能证明原提交/Wake已验收。
- 根因在通用工具归档后的 `input_wait_response`：直接返回空正文与不属于用户等待族的unfinished，分别触发Gateway空回复门与RuntimeDB失败收口。旧343项只检查发卡、输入账和回调，没有检查整轮response.ok及主运行状态；该覆盖缺口保留，不把此前离线通过扩写为全过程成功。

### 复用边界与状态表

先读现有自然回复、completion、turn/end及原Wake消费/attempt完成合同。成熟参考先检索Codex和OpenAI Agents Python合同文件索引（本次输入等待关键词没有准确行），再定位原源码：Codex `request_user_input.rs` 在原session/turn/call等待输入或取消；Agents SDK `run_internal/run_loop.py` 的 `NextStepInterruption` 保留原RunState及生成记录，不把中断写成业务失败。只读参考版本分别为 `578c1b2230288104041e880a86d0f7f3a5ca6e47`、`464043a5e6e7f7dcfb37c9a2fac770ae7e8a19ef`；不引入第三方或复制另一执行链。

| 原事实 | 当前执行片结果 | 恢复权威 |
| --- | --- | --- |
| 原输入pending，或同准确任务仍有已armed的必要输入 | needs_user_input / TASK_INPUT_WAIT / input_dependency，turn blocked | 原输入回调 |
| 输入终态且原 `task-input:<id>` 发布回执pending | unfinished / TASK_INPUT_RESUME_PENDING / input_dependency，turn blocked | 仅原Wake；不加入should_continue_task或Goal自动续跑 |
| 本卡retire且无其它必要输入 | cancelled / TASK_INPUT_CANCELLED / input_dependency，静默 | 只收当前run/attempt；TaskLink与父兄弟不取消 |
| 原Wake handled，准确旧attempt对应的AgentRun已有不同core attempt | cancelled / TASK_INPUT_SUPERSEDED / input_dependency，静默 | 原后继attempt；旧片沿CAS不能覆盖它 |
| 终态无原Wake，坏账，或handled无后继证据 | failed / TASK_INPUT_WAKE_UNAVAILABLE / input_dependency | 明确故障，不伪造待恢复许可 |
| 无工具表达空正文/typed供应商异常 | 保留已复核输入等待，公开USER_REPLY_UNAVAILABLE | 原输入/Wake；不合成普通回执 |

原工具记录完整归档后只登记 `queue_natural_user_reply`，同模型以零工具薄视图表达；最多沿原有界重写，不再采样业务或重发卡。表达前后复读同一输入行；回调竞争使事实变化时只重写表达。只投影input_id/provider/status/expires_at，任意metadata和私人值不进入表达。两个输入表达阶段均禁止消费deferred业务工具。child/grandchild的Wake目标使用原root TaskLink.thread_id；handled身份通过原attempt→AgentRun核对，不把业务run_id、agent_run_id混作一个编号。

typed provider错误只在准确输入表达阶段保留生命周期；普通编程异常、预算拒绝与宿主中断仍按原错误/控制合同处理。原stop结果优先于模型正文。standalone工作区对原needs_user_input/approval_required投影BLOCKED。未增加配置开关、轮询、固定普通回执、第二输入或另一调度器。

### 本轮独占文件

- 生产：`conversation/input_tool_boundary.py`、`agent_core/_tool_loop_service.py`、`agent_core/tool_loop/natural_user_reply.py`、`agent_core/runtime_mixin.py`、`gateway_parts/response_renderer.py`、`agent_core/run_task_workspace_writer.py`。
- 测试：`test_gateway_credential_live_entry.py`、`test_persistent_task_input.py`、`test_card_cancellation.py`、`test_run_closeout_settlement.py`、`test_run_task_workspace_writer.py`、`test_background_public_receipt_contract.py`、`test_feishu_oauth.py`。既有后台公开回执测试只迁移增加agent参数；旧OAuth夹具由固定一次采样改为一次业务加一次零工具表达，保留原工具归档/脱敏断言。
- 本交接、`docs/design/IM_CREDENTIAL_COLLECTION.md`、`TESTS.md`。本轮没有新文件；根另负责全局文档和Gateway模块导航，以及独占的凭据schema描述。

### 验证与未验边界

改前group/user/OAuth整轮response.ok断言均红于USER_REPLY_UNAVAILABLE；表达期间原卡取消也先红，准确复现refs=0/Wake=0而run仍created。修复后对应整轮与竞争矩阵通过。24文件首轮619项仅一个旧OAuth夹具固定1次调用失败，另4项为既有runtime_guidance xfail；迁移该夹具后原三文件104项通过，再跑同24文件为615 passed/4既有xfail/0 failed/error（`/tmp/input-wait-24-focused-final.xml`）。最后补typed取消/预算拒绝/代码异常三条阴性，Gateway整文件25项全过（`/tmp/input-wait-gateway-final.xml`）；非作者按冻结13文件独立复核最新24文件：622项收集，618 passed、4项既有xfail，0 failed/error；测试前后文件摘要一致，无剩余P1/P2。不能把两个组合数字叠加。没有删失/改写原失败证据。

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_credential_live_entry.py agent_py_agent/tests/test_tool_approval_foreground_scope.py agent_py_agent/tests/test_task_credential_storage.py agent_py_agent/tests/test_im_credential_card_preflight.py agent_py_agent/tests/test_task_credentials.py agent_py_agent/tests/test_feishu_oauth.py agent_py_agent/tests/test_persistent_task_input.py agent_py_agent/tests/test_unattended_tool_approval.py agent_py_agent/tests/test_im_tool_approval.py agent_py_agent/tests/test_im_credentials_security_matrix.py agent_py_agent/tests/test_ingestion_task_credential_scope.py agent_py_agent/tests/test_task_credential_forms.py agent_py_agent/tests/test_background_tool_approval.py agent_py_agent/tests/test_task_input_capacity.py agent_py_agent/tests/test_task_input_cancel_deadline.py agent_py_agent/tests/test_card_cancellation.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_tool_loop_model_turn.py agent_py_agent/tests/test_turn_end.py agent_py_agent/tests/test_background_input_recovery.py agent_py_agent/tests/test_run_closeout_settlement.py agent_py_agent/tests/test_run_task_workspace_writer.py agent_py_agent/tests/test_background_public_receipt_contract.py agent_py_agent/tests/test_gateway_client.py -q --tb=short
```

新增检查包括：response.ok和主run/attempt、零工具模型次数、card一次、submit单Wake、cancel释放、表达越权调用拒绝、空正文和typed provider异常、表达中原stop、callback与多输入竞争、child/grandchild原Wake路由、无回执/handled同attempt的明确失败、准确后继及错误run阴性、零业务deferred消费和原等待工作区投影。局部Ruff和strict code-size已通过（hard=0、blocked=False，baseline未改）；最终严格gate、准确新包及真实submit单Wake由根执行。本线未提交、部署、重启、改真实配置或数据、外发。

4项预期失败均在既有`test_runtime_guidance.py`，标记原因分别是两项native steer正文标记迁移、一项native截断/steer轮数及一项audit证据原文回放迁移，未新增跳过或修改标记。当前doc_sync明确未过：根并行Gateway/调度改动还需两页Gateway模块文档及scheduler/tool.py同文件注释；根负责统一同步后再严格gate。本线三份文档已同步，`git diff --check`通过。

### 建议下一步

根先等待非作者按冻结diff复核事件等待、静默取消、原attempt/CAS与父兄弟边界，再串行跑最终严格gate并发行新版；随后用真实新卡完成提交单Wake验收。可并行只读复核或隔离fake回归，真实卡/私密配置/服务/发行保持根单一执行者。不要把原`.3`卡成功当作本次全过程验收，也不要把旧343项和本轮计数累计。


## 输入等待薄表达事实追加（2026-10-15）

### 真实事实与归因边界

已发布`.20261015.4`（`9f2bc0120869cd07a3b2eb18f9d8ae8b2da07890`）真实群卡出现、字段无重复；根以B提交合成值后，本线仅读准确测试请求元数据：原输入ready、唯一原Wake handled、后继attempt done、主运行与TaskLink done，申请操作仍仅一次。根观察等待自然正文却称无工具/无法发卡，故卡片与权限链通过、自然表达失败分别记账。未读取秘密/表单值，未做实际canary等值扫描；原工具输出只检查敏感值字段有无，不扩大成全链路秘密证明。

准确Gateway响应不保留provider prompt，不能反推实际wire全文；原model usage证明业务采样加表达采样各一次，无重试。部署源码中`prepare_input_wait_reply`没有其它阶段已有的`turn_execution.presentation_only`与公开操作核验，零工具薄表达清掉了业务历史。此为可证上下文缺口；不声称已经证明模型内部因果。安全诊断、两次快照及红绿JUnit均保留仓外，不复制真实owner/actor、表单或聊天正文进仓库。

### 最小通用修复与验证

沿本交接已有Codex/Agents参考和原自然表达合同，仅将`completion._interim_reply_facts`既有安全执行投影抽为`turn_execution_reply_facts`，原调用与`input_tool_boundary.prepare_input_wait_reply`共用。当前请求归档经`public_operation_verification`只留下聚合工具/Schema动作/终态及计数；presentation_only说明表达轮有意无工具，成功数不由输入pending、正文或任意metadata制造。input_resume_pending重写保留同一事实，不新增业务模型采样、卡片、Wake、输出关键词门或固定回执。无归档时真实成功数为0。

本片独占生产仅`agent_core/tool_loop/completion.py`和`conversation/input_tool_boundary.py`；测试仅`test_gateway_credential_live_entry.py`、`test_persistent_task_input.py`，文档仅本交接、凭据设计与TESTS。无新文件、依赖或配置；不改公共native布局。真实fake backend messages中的唯一canonical UserTurn包含准确JSON事实，其后公共RuntimeFactsTurn仍投影为user role；测试分别核验末尾typed UserTurn和实际wire事实，不把二者混称。合成canary注入参数、output、任意metadata、信封、内部operation/call/ref、旧IR/provider历史及tool_context，验证phase、表达IR/user_prompt和native消息均不含canary。

首轮3项反例均在旧源码缺turn_execution处红；最终冻结下列5文件181收集=177通过、4项既有runtime_guidance xfail、0失败/错误，包含更严格native canary断言；JUnit为仓外`input-expression-facts-final.xml`，安全摘要为`input-expression-facts-finalfreeze.json`，不累计交叉数字。局部Ruff、doc_sync、strict尺寸（hard=0、blocked=False）与diff均通过；根统一严格gate、构包和真实自然表达重验。本线未提交、部署、外发或改真实数据。

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_credential_live_entry.py agent_py_agent/tests/test_persistent_task_input.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_background_public_receipt_contract.py agent_py_agent/tests/test_current_turn_execution.py -q --tb=short
```

建议下一步：根先统一审阅2生产文件的安全投影与5文件冻结结果，再与其它独占线集成严格gate及新包；真实群/私聊仅少量验证自然表达和提交单Wake。可并行只读复核，实际UI、配置、服务、发行继续由根串行执行，不以fake正文代替真实表达验收。


## 创建者姓名投影追加（2026-10-15）

### 基本信息与目标

同一main集成树，基线`b3ff9ac3f45721e537efd5df5cfbdc0afbe5b975`，输入线获根授权独占3生产与3测试文件；不提交、推送、部署或改生产配置/身份索引。真实`.5`原订阅与群投递已由根UI及只读结构快照分别证明，创建者ID正确；根随后用原正式members接口确认2项均有name，而原展示索引不存在。只修原认证资料到展示索引接线，不改任务、创建者或授权合同。

### 实际完成与改动文件

- `adapter/feishu_provider_facts.py`从同次完整open_id分页响应保留可选姓名，无额外查询；adapter本身不猜home或写展示索引。
- `user_space/group_authorization_context.py`完成原provider/chat/完整成员核验后，名单内姓名才交身份模块；展示IO/结构失败只记录固定事件及异常类型，不含值，不吞原授权拒绝。
- `user_space/identity_store.py`复用原provider索引和同一锁，读取原字节并追加变化的展示行，原私有原子writer保存；不嵌套调用自取非重入锁的append入口。原关联、subject及扩展字段保留，新记录仅默认user；空名不覆盖、无名字不读、同名零追加/重写、并发同锁防丢更新；锁忙由原blocking=False明确让出，展示调用点记录BlockingIOError类型后继续授权，不等待或加后台重试；坏索引不重建。
- 原`test_feishu_bot_observations.py`、`test_home_identity_store.py`、`test_group_authorization.py`；原群治理、设计台账、TESTS、gateway两页模块说明及本交接。无新文件、依赖、配置或第二身份索引，因此无文件树变动。

参考先读本仓库原identity_store关联解析、私有JSONL/锁、GroupAuthoritySnapshot及creator展示合同；检索OpenClaw合同索引未命中此窄分支，再读`extensions/feishu/src/directory.ts`（`9cf12734e675a1ec63abb09bf65e4f6d76835a1e`）中稳定open_id与可选name分离的投影，仅复用其展示/身份分离思想，不复制contact查询或fallback。

### 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_feishu_bot_observations.py agent_py_agent/tests/test_home_identity_store.py agent_py_agent/tests/test_group_authorization.py agent_py_agent/tests/test_schedule_owner_scope.py agent_py_agent/tests/test_group_authorization_settings.py -q --tb=short
```

另补原索引锁真实竞争红测，旧默认阻塞等待2秒，改用原非阻塞锁后授权不等展示锁；`creator-name-display-lock-red.xml`保留。三层首红分别为缺member_display_names、缺展示更新helper及真实creator显示仍未知，仓外`creator-name-wiring-red.xml`保留。最终5文件214 passed、0 failed/error/skipped，JUnit为`creator-name-wiring-focused.xml`，不与此前87项或其它组合重复累计。首次扩展矩阵一项fixture误用OwnerHomeResult.owner_kind，已按真实identity.owner_kind修正而未改产品契约。局部Ruff、doc_sync、strict尺寸和diff以冻结安全摘要为准；最终严格gate由根执行。

覆盖已有main/linked/非默认owner完整关联及扩展policy字段、仅默认user新记录、不创建owner、同名不同subject/群不借权、原provider/chat/完整成员错误零展示写、非open_id和分页边界、空名及同名不擦写、无名不扫描索引、并发主体不丢、坏JSON保留原字节、预期展示写失败不阻断授权且日志无值。

### 影响范围、复查点和剩余风险

provider身份索引仍参与既有关联解析，根应重点审查仅display_name/updated_at变化及新默认user等价性，不能把它降格成可任意重建的缓存。显示失败不授予或撤销权限；只有原provider/成员/策略/actor检查决定授权。新数据仅来自已有认证响应，不从USER.md正文、模型、同名或最近群成员猜人。索引命中但已有异常映射的情况只保留原映射，不借姓名修复或覆盖它。

真实`.6`姓名展示、跨平台包内运行与发行尚未验证；当前`.5`服务及原失败证据未动。姓名历史积累沿原索引保留合同，本片没有新索引/清理器/周期平台查询，也不宣称解决全公司身份索引性能。无可用姓名在索引读取前退出，未变化不产生写入。

建议下一步：根先审3生产文件的数据来源、原关联保留及同锁写入，再统一focused/严格gate并构新候选；少量真实原群资料读取后验证创建者姓名。可并行只读审阅或包准备，真实UI、配置、服务、提交和发行仍由根单点执行，不以合成显示代替新版真实验收。

## 原输入结算与运输事实投影追加（2026-10-15，.8 候选）

基线 `4f6aa346b4a6fc1daf16b2e94507551533224ec0`。真实 `.7` 私聊原卡送达并提交后，输入 ready、原 Wake handled、后继 attempt done；原工具账的两个独立 operation 分别返回 waiting_input、ready。模型初轮否认发卡能力、后继仍催填写属于表达/投影缺口，不能把卡片和输入账成功当成自然正文正确。精确只读诊断在仓外 `private-input-v7-expression-diagnostic.json`，原 Wake 引用类型确认在 `private-input-v7-wake-ref-types.json`；没有读取表单值、私密凭据文件或原始密钥。

复核原自然回执、InputDependency/Wake、宿主事件合同及此前 Codex `request_user_input.rs` / Agents SDK interruption 参考后，复用原 typed 事件与公开回执：不增加状态源、恢复入口、正文关键词门或固定普通回复。改动仅四生产：`conversation/lifecycle_wake_event.py`、`tooling/task_credential_tool.py`、`conversation/input_tool_boundary.py`、`agent_core/tool_loop/natural_user_reply.py`。原终态事件只带准确标量与结果存在布尔值；原凭据行的投递确认进入本次匹配输入的易失薄表达，未知不能冒充已发送。子代理投影、owner/attempt、原 opaque 引用权限及原单 Wake 保持。

直接测试为 `test_lifecycle_wake_host_event.py`、`test_gateway_credential_live_entry.py`、`test_persistent_task_input.py`；既有推荐工具测试补真实临时 TaskLink 前提，未放宽 HostActor 产品门。首红 12 项因缺 input_dependency/request_delivery 失败，证据 `private-input-state-red.xml`；三文件随后 102 项通过。最终 focused 额外包含 `test_task_credentials.py`、`test_tool_approval_foreground_scope.py`、`test_im_credentials_security_matrix.py`，结果与冻结 SHA 保存仓外交接，不能将组合重复累计为新增覆盖。没有新增重要文件、依赖或配置，因此无需文件树变化。模块设计已同步；全局 TESTS/DESIGN_LEDGER 与严格 gate 由根统一。

建议下一步：根先结合独立只读审阅运行最终严格 gate，再在准确新版少量复验私聊/群的等待和提交后表达；可以并行准备离线 runner，真实 UI、凭据、服务与外发仍由根唯一操作。即使模型偶发继续说错，也应记录事实并重新定位通用上下文，不扩成正文硬判定。
