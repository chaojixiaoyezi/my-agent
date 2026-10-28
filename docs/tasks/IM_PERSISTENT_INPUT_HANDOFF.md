# IM 持久必要输入与凭据交接

日期：2026-10-14。状态：本地实现与隔离定向验收；部署、main 推送与发行由根代理统一完成。

## 解决问题

原 `request_credentials` 在 worker 内等30分钟，并把卡片绑到 claim；直接返回会使回调失效。现在工具登记必要输入并立即返回结构化 `waiting_input`，完整原工具归档与 native 配对后结束当轮，worker、claim 和 runner 执行槽沿原链释放。原任务保留 active，输入本人完成、取消或过期后从原 Wake 恢复一次。普通群消息走自己的原消息入口。

## 唯一事实与接口

- `conversation/input_dependencies.py`：canonical 行位于 owner `SessionTaskStore.storage.session_tasks_dir/inputs/<sha256(input_id)>.json`。只存 owner、scope、provider、期限、状态与 opaque refs，不存秘密。此行是唯一输入状态源。
- `ThreadTaskLink.input_dependency_refs` 只存 ID 查找投影；`input_dependency_index.py` 使用原 task path、原记录锁增删。复用原私交互 `AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT=32` 作为每根输入容量，不是owner审批和输入混合队列。owner配额→原task→输入行的固定锁序内检查数量并保存canonical，33条拒绝为 `ValueError('TASK_INPUT_CAPACITY_EXCEEDED')`；重复同ID不增槽，终态移除refs放槽。任务等待查询仅读取该 task 的 refs，禁止遍历 owner 历史。
- `create_input_dependency(agent, scope, *, input_id, provider, expires_at, reference='', metadata=None)`：当前 scope 必须真实 active；同 ID/同绑定重放，不重置期限。
- `arm_input_dependency(agent, input_id)`：原归档和 native 调用/结果配对后调用。提前完成的回调只保存结果，直到此边界才发布。
- `resolve_input_dependency(agent, input_id, *, status='ready', result_refs=())`：保存 ready/cancelled/expired/revoked/failed；相反或迟到终态拒绝。成功采用原 `WakeStore.raise_signal`、稳定 dedupe 与 `retain_handled=True`。
- `reconcile_input_dependencies(agent, *, task_id, now=None)`：只对准确任务核期限/取消，并重投未完成发布。期限沿原 RuntimeDB `wake_queue` 的 `kind=task_input_deadline`；最近输入期限一次检查，不增加定时线程/队列。原五分钟 `_reconcile_wake_queue` 在所有Goal补醒前准确读取输入refs，直接重排原绝对期限并跳过Goal写入，不依赖 `list_pending_wakes(1000)` 判断不存在；分页遗漏不会覆盖输入deadline，已armed期限也在归档边界再次确认。
- `cancel_task_inputs(agent, task_id, *, run_id='')`：准确控制停止后即时清未提交输入与 refs；指定run只清该节点。共同 `subagents/cancellation._close_projection` 已在授权及原执行权关闭后接入，选中子树成员逐项清；Gateway `_prepare_main_task_stop` 已在根停止提交后清整根。输入回收失败记原未确认事实，不阻止原资源收口。无业务恢复、不改 UNKNOWN 工具副作用、不撤销独立 ready 凭据。
- 任意必要输入工具返回 `result_envelope.input_dependency={input_id,status:'waiting_input',expires_at}`，共享 `input_tool_boundary.py` 在归档后 arming，返回空正文的 `unfinished/TASK_INPUT_WAIT/input_dependency` 与显式 `turn_end_reason=blocked`。宿主不合成用户报告，不再模型轮询。
- 本轮后续未开始工具沿原 runtime transition deferred 记录完整失败配对；已启动并行段保持原完整收账。审批线负责该接缝。

## 凭据生命周期与受控认证

`request_credentials` schema 支持多字段、basic/bearer/header/login/signature、ready TTL、status/update/revoke；开关关闭或配置不可读时工具不可用。`CredentialCardOptions` 集中表单、方案、期限和更新引用，避免参数膨胀。

私卡保留原人、owner、任务/监控、同源认证范围和1800秒输入期。字段全为密码输入，模型只拿 cref。Fernet 保存原字段和有限期会话密文，ready TTL 独立校验，更新提高 generation、发新幂等卡；旧卡不能覆盖新授权。撤销清除密文/会话缓存。容量拒绝发生在发卡前，删除本次新凭据行；更新拒绝恢复旧授权及其密文，不使已就绪凭据丢失。发卡失败将依赖结算为failed放槽。未armed的提前终态也立即放槽，但不能发业务Wake；原归档hook稍后从canonical行arming时仍恢复一次。

## 状态只读投影

`im_task_status.py` 从本聊天原SessionTask准确目标线程、原root引用及TaskLink input refs读取；每条输入状态仍取canonical，不扫描owner输入历史。保留queued/running接口，accepted待输入从running扣除并单列waiting_input；孩子按准确root report读取，BLOCKED单列subagent_waiting与subagent_waiting_input。`/status`明确“待输入（等待不占执行资源）”。输入、根、目标线程或子记录坏账显示不可用，不补零；未引用的历史坏输入不会影响本聊天。

`user_space/task_credential_http.py` 的 `credential_authenticated_request(agent, reference, request, *, send)` 只接受宿主原 HTTP 请求出口：同源 JSON 登录、Cookie/CSRF、有限期 Token/Cookie cache、GET/HEAD 明确401后一次刷新、SHA256/HMAC-SHA256 声明式签名。POST 等变更不自动重做。禁止秘密传入 Shell、环境变量或任意脚本。

core 装配 `web_tool.credential_authenticator = lambda ref, request, send: credential_authenticated_request(agent, ref, request, send=send)`；该主路仍采用原 `fetch_raw_response` 每跳 IP pin/网络门。携带私人认证的请求禁止跨 origin 重定向，避免307泄漏登录正文。认证响应禁公共缓存，原字段、派生Token、Cookie各值、CSRF、签名及常见JSON/URL回显在格式化/产物/归档前净化。

## 审批拒绝与恢复

`task_rejected_actions.py` 按准确 task_id 保存工具名、args_hash、原 binding 与决定/原因；模型前恢复、工具归档后合并，不保存参数正文。新 request/attempt/call、Compact、input ready 均不清拒绝。普通成功无拒绝时不创建空记录；重新继承拒绝时只读原账，不增加写入。审批线仍负责 reason_code 与拒绝结果合同。

## 验证与边界

新增 `test_persistent_task_input.py`、`test_task_credential_http.py` 与 `test_task_input_cancel_deadline.py`：31项通过；与原凭据、SSRF、后台输入、工具配对、SessionTask、子取消和Gateway控制十文件组合共167项通过，自然退出。追加 `test_im_task_input_status.py` 7项、`test_task_input_capacity.py` 5项及原控制渲染31项，最终十三文件focused复验见根集成记录。覆盖真实工具 + fake native LLM 一次请求后结束/完整归档、释放 claim 后本人提交、重复回调/handled Wake 不重发、提前提交后 arming、取消/过期、owner/actor/卡绑定、ready TTL/更新/撤销、跨attempt拒绝持久化、合成原网络出口、本机127.0.0.1真实HTTP登录、Cookie/CSRF/秘密回显净化、GET受限刷新与POST无自动重试、跨origin307秘密正文不转发及任意脚本schema拒绝。追加真实 `cancel_subagent_tree` 子孙/兄弟隔离、Gateway `/stop` 清根输入、旧CANCELLED子状态不阻塞根guard、1001队列分页遗漏不覆盖原绝对输入期限、同任务多输入到期后重排下一准确期限；32/33真实线程并发容量、重复ID、提前终态放槽、凭据拒绝无新行/卡片及更新保留原密文，状态会话隔离/准确refs/坏账不可用。根代理统一远端前严格gate、clean-package和发行复验。

未外发真实飞书、未读取生产密钥、不把本地合成验收说成平台私卡客户端密码遮蔽、移动端填写或大规模生产验收。根代理统一执行严格本地gate和真实本机部署复验。

## 建议下一步

根代理先集成 OAuth provider与core受控认证注册，复核已接入的原停止入口，再跑 focused/ruff/doc-sync/import-boundaries/diff/clean-package gate（2026-10-28 状态补充：原 `strict-size` 数值门已退役，不再作为现行执行要求）；审批、CLI、发现线可并行联验，但不要并发改本持久输入/凭据文件，不读取生产secret或用旁路产物代替被测任务结果。最后验证群后续聊天、主子孙输入恢复、真实私卡和发行版本，仍按原精确任务/actor/attempt守住取消及未知副作用边界。
