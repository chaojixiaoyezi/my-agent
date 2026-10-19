# 冷启动 C 请求元数据泵

状态：cold准入、周期恢复和queued/cursor消费者已在临时C合同中实现；绑定输入的执行来源、真实多进程/native/Count2仍不由本片证明。startup沿独立正式入口片运输，不从首拍猜测。

`GatewayRunContext.agent is None` 的正 count C 沿 `CoordinatorMetadataRequestPump`
复用原控制面、Root、controller、lease 与唯一 `PersistentAdmission`。初始化和每拍均核实同一
原对象/PID/代次；复制、替换和关闭后拒绝继续。初始化不构造 Agent、owner pool、Store 或业务线程。

每拍先调用同一 gate 的 `release_settled()`，再调用原 `dispatch_pending_requests()`：

- W 请求沿原 prepared 身份、owner/actor/media、会话公平、全局/owner 限额与 P3 短门盖章，继续留在原 pending，等待实际 W 消费。盖章不等于执行完成。
- 元数据 host 是真实 `GatewayControlPlane`，不是业务 Agent。原 P2 隔离判据仍优先；C 路由在原 prepared/归属校验后、创建 P3 或 claim 前保持 pending。
- 配额只从同一 control 的 `home_root` 通过原 typed 路径函数投影。原策展日额度、审计迁移、未结 P3 占用和 pending/lease 豁免不变；非策展请求仍在路径投影前原样返回。
- 线程返回、responses/done 投影和缺失终态都不能释放 P3。仅原 gate 对规范 terminal 的原身份校验可以释放；关闭 pump 不清 gate 或 P3。
- 原派发异常仍原样上送。周期恢复和terminal投影各自退避，逃逸异常交原段观察器且保原对象；单条结构化错误保存原报告，不重建异常、claim或entered，不伪造成功或默认scope。

healthy C 继续使用原 `_RequestDispatcher`，仅输入对账函数把当前context与同Root保留的原gate交同一queued消费者；Count0 的原派发、业务执行、完整输入对账、恢复、通知和限流主体保持。
本切片没有新队列、权限表、reserve 账、配置或服务。

## 同gate周期恢复与终态投影

派发先取得本拍机会，再分别运行到期的修复段。`_RecoverThrottle(control)`复用main的timeout/3（至少2秒）恢复节流；终态投影复用原0.75秒节奏、启动首次完整pass和16项有限页。两个段各用原`LoopErrorBackoff`，互不延后派发或另一个段。没有新增线程、文件游标或第二恢复实现。

周期处理构造原`_RecoveryContext`，其中`agent`和`admission_gate.agent`始终是同一个实际ControlPlane；gate仍由同一个Root保留。直接调用原`recover_gateway_processing_requests_report`→`recover_stale_processing`→原controller的`recover_processing`。终态调用原`repair_gateway_terminal_projections`并传同一context/gate；原P2/P3、RuntimePolicy、full canonical CAS、owner/thread、initialize=False和同步短门均保持。只原canonical终态规则可以释放P3，修复计数、错误报告或空资源表都不是释放证明。

`_last_recovery_report`和`_last_projection_report`只保存最近一次原返回对象用于诊断，不保存异常栈、不落盘、不签权限；进入新调用先清旧观察，无法取得报告时保持None。原报告含errors时仅增加所属段退避；直接逃逸异常仍交原loop观察。未知Runtime、失效源或坏指纹保持processing/P3原责任，不转普通业务失败或重放模型。

## 明确未接入

周期 pump 只接与 healthy 周期段同义的 `startup=False`。正式 CLI 后续已沿原 startup 恢复入口单独运输启动事实，详见 [启动恢复](COORDINATOR_STARTUP_RECOVERY.md)；pump 首拍和 None Agent 仍不是启动或安全重启来源，原预算不在本消费者重置。正 count 的 B4 内部滚动重启尚不支持，拒绝后保留服务。

旧`reconcile_gateway_input_receipts`完整未结分支不直接交给C，那里有真实guidance/claim/receipt写入，不能调用foreign pool补Agent。原terminal post对同一已绑定输入的既有结算仍由原恢复source短门执行，与下面的queued/cursor扫描不同。

## 原queued/cursor元数据闭包

`multiprocess_input_reconcile.reconcile_coordinator_queued_inputs`只接当前实际ControlPlane、Root与已被该Root保留的同一gate；同时核原controller/lease、PID/thread、上下文引用和规范队列路径。它不创建Store、Agent、owner目录、P3、Thread或第二队列。queued收件可在owner隔离时保留，修复也不要求额外的旧W停止/P3业务资格；后续原admission仍决定能否执行。

queued物化继续调用原`_materialize_prepared_request_locked`，完整原prepared、owner/owner_id、actor/media、摘要与canonical/孤立投影检查均保持。源在原input T→controller→lease→Root同步短门内运行，并通过可选`write_request`在实际原子写前重核完整receipt及原prepared字节；None仍是原Count0写路。不重新prepare或热读管理员绑定版本。cursor独立取得原cursor事务后进入同一C末门，通过原`_commit_reconcile_cursor`及可选`write_cursor`保留唯一格式；它不是上条owner的许可，也不是另一个调度账。

`consumed`只计原receipt事实；pending/active_pending/terminal_unknown只计`deferred`和`unsupported_states`，不读guidance、不迁移、不改receipt正文。原cursor可记录已examined的延期行并轮转，避免前缀活输入饿死后面的queued；延期不计为已消费、已结或成功修复。整页延期沿独立state错误退避，派发/恢复/terminal各段仍有本拍机会。

原运输未知、缺上下文、C失效、Bounded退出未确认和GlobalWrite原类型均同对象上抛，故障行不推进cursor，不依赖error_code文字匹配。普通同码异常仍沿原坏行计错/轮转；部分queue写成而cursor未成允许下一次原幂等修复，绝不据此重跑业务或清P3。source退出、复制、跨线程、Root关闭或receipt变化后的保留callback不能继续写。

真实cold terminal叶子现有`log_gateway_payload(ControlPlane)`缺local_store的继承side-effect诊断仍明确保留；该日志错误不会替代canonical事实，也不能通过伪装Agent/Store消掉，后续由原日志边界单独处理。

## 验证边界

直接合同使用临时原 coordinator guard、controller、prepared receipt、P3 和规范 terminal 输入；
没有实际 W、模型、网络、服务或原生进程。原 terminal 夹具不冒称真实业务完成。
相邻回归保留 Count0、公平、准入等待、派发错误、关闭和原身份字段断言。

建议下一步：独立复验本片实际cold/healthy输入段与原Count0邻接后顺序合入；active/bound运输和日志边界分别闭合，startup只沿已冻结的正式CLI入口组合。可并行准备active来源合同，生产接线保持独占，真实Count2/native由根统一验收。

## C queued短门内的普通资源竞争

原input/cursor T在C末门之前取得；末门内的JSON资源读取、原queue写和cursor写均显式`blocking=False`，只尝试各自原锁。`io.read_json_file_report`仍走gateway原读锁与`lock_busy`报告；`io.write_json_file_atomic`仍走common原写锁及同一个无尾换行私有原语，不能为了非阻塞换锁身份。默认True保留Count0原等待、序列化与权限。

实际`BlockingIOError`同对象停止本轮，该行cursor不推进，也不包装为权限错误或执行结果未知；原输入段退避记录资源暂忙。receipt、已有canonical和已完成queue写均不假回滚/结清：若cursor忙而queue已成功，原幂等修复可下轮观察它。普通同码RuntimeError仍按原坏行轮转。非阻塞只约束原锁获取，不宣称任意文件系统I/O零延迟。

普通自家持锁线程分别覆盖receipt内读/末读、已有candidate读、queue写和cursor写，原产品在观察窗内实际等待，修后持锁者未释放时Root已能关闭；所有测试线程finally释放并join。没有启动W/native/Count2或安全探测。完整receipt C CAS仍是单独只读方案，queued source不接受任意callback或active迁移。

## 通用控制回执纯读候选（2026-10-17）

正count实际HTTP工作片在读取前捕获、原ACL通过后重核同一upper/lower设施；普通/control-status只读原严格receipt，不需要business Agent，不调用reconcile/死亡封账/原命令，不新建JSON锁。ready C同样只读，prepared/executing/terminal_unknown原样显示；这只是已有快照，不是业务完成或物理停止证明。Count0原对账保持；D28显式v5 private_approval在ready时仍走其原决定证据对账，cold只展示原已存状态，W原poll负责结清，不由GET签发决定。模型领域wrapper复用通用观察器，保留原unavailable/changed映射；观察器不授权模型或写账。

本片未开放cold通用POST、主owner quarantine执行或agents/memory管理；这些仍须独立原source/receipt/W主链，不能只删除None门。新增普通真实HTTP测试原8红转绿；9文件129项普通通过，F/M未由该组件修改，根最终集成/Count2/native另记。

2026-10-17 control-status02：按独立评审保原公开GLOBAL_WRITE_REJECTED错误码；只回退两处generic work ValueError的展示码，不调用模型授权源。新增普通HTTP失效一红转绿，九文件130普通通过；D28组合与最终Git严格gate仍由根验收。

## 冷C普通控制POST候选（2026-10-17）

基础owner受原P2/P3隔离、`server.agent is None`时，正count的非model、非C管理控制只进入固定 `coordinator_control_transport`。G1、global-write-v1拒绝、显式命令解析和原scope投影顺序保持；owner只由原配置/认证scope解析，主owner仍明确waiting，不借远端W执行主owner。ready及Count0继续原调用，模型和私卡v5仍留原领域。

固定producer捕获当前实际finish_request工作对象、upper/lower/socket/线程/原executor、control/runtime/paths/controller/lease、认证middleware、完整正文/command/scope/owner和原operation identity。每段和末写重核同对象、原credential、原完整输入与lifetime，finally只关闭原来源并退精确pin，清理失败不能盖首个typed错误。没有perform(callback)、伪Agent/HCM、第二队列/receipt/schema或逐kind C业务。

原operation T始终在Root外只try；争用时可严格读取同ID既存，无既存明确busy。原builder/codec是回执唯一权威，cold只prepare/读取，不local reconcile。既存prepared/claimed/executing/completed/terminal_unknown全部返回原事实，不重新选worker/target、不补dispatch或重发effect；digest冲突仍原409。

首次route复用ready的同一个纯builder，原owner_worker/count/c_run_id和control_dispatch_binding保持。target慢读先在Root外；最后controller mutex→lease→Root短门内再次核来源/target，在原common JSON try锁下对照完整raw字节/缺失事实，然后原无尾换行私有writer一次写prepared+dispatch。不可读不能变成{}修复；CAS变化只读严格原事实或明确冲突。目标缺失沿原CONTROL_EXECUTION_TARGET_REQUIRED失败，不猜latest或未来任务。

legacy target只以显式config/home解析canonical owner Store，foreign清原runtime覆盖，initialize=False不建目录。原thread bindings/thread/claim/task读取是无写锁的原子只读；foreground processing读取额外透传原blocking=False/deadline，busy原样上抛，不混同缺目标。所有安全点共用conversation/input_resource_retry生成的一个预算，末writer用common原deadline；默认True/None及原target/identity算法不变。该预算约束步骤和锁等待，不宣称普通文件系统IO硬实时。

C只发布prepared或原目标缺失拒绝；原WorkerControlPump仍是认领与执行者，实际pool.get/pin实例后进入原领域，stop沿原独立槽。普通回归证明实际HTTP→receipt→本地原W控制泵→真实Agent/Store→status/schedule/delegate/stop，不证明独立W/native进程、物理资源死亡、P3释放或完整F3完成。typed源错误同对象穿过receipt/source，HTTP最外层映射原error_code；common writer后抛错时已存prepared保持，同ID不补发。

### 必须继续闭合的已发布能力

F4/F5已发布的`/agents`、`/memory candidates`以及client memory search/remember在正count冷C仍不可达，属于F3功能缺口，不能改标unsupported后宣称完成。管理kind继续留C且保持authenticated live seal；最小原服务分别为AgentsWriteService和Candidate/Promotion，后续须从真实依赖拆接到C全局写口，不能造Agent facade或把seal落盘/转W授权。`/client/memory recent`已沿原只读叶子；search/remember已识别但返回COORDINATOR_BUSINESS_NOT_READY，Count0仍走原recall/remember及Candidate→review→promote。未知operation继续原UNSUPPORTED_OPERATION/usage，不因本片扩大语义。

建议下一步：先非作者复核此六产品路径片并由根顺序组合；另在新独立副本设计管理/记忆原入口接合，列最低依赖与白名单后复审。普通fixture可并行只读复验，共享实现由唯一作者写入；本片不推送、部署或触碰日常服务，D24/P6持续排除。

## 冷C规则管理领域A43候选

原 `coordinator_owner_management` 只在当前实际HTTP/work/原C/lease/controller与操作者封印成立、且原operation T首次prepared→executing后调用固定规则领域适配器。`coordinator_agents_control` 复用原target选择、真实资源配置、AGENTS仓库和原预览/消费未知/CAS算法，不构造Agent或替换dispatcher。

`coordinator_actor_resources` 只从实际source组合原ConversationStore，初始化False；默认模型解析复用C1按准确home的纯入口，不读取全局用户环境替代。其固定prepare端口不接任意owner/path/callback：真实操作者线程和通道绑定只进receipt-owner P2；显式目标选择后，正文及nonce仍进实际target P2。两者共用当前原身份，不得混用目标P2写本人线程。resource tuple、Store/默认callback身份和canonical路径在当前源检查中核验。

原 `AgentsWriteService` 的普通文件失败依然保留 consumed_unknown，不能自动确认或重放；来源自身首native由B38原槽保存，业务catch即使暂时吞掉也不能正常出口。原bounded未结/global writer拒绝/原运输未结在原领域catch前同对象上抛，不进行诊断补写后宣称completed。原nonce若已耐久消费则保持未知，不补偿还原。

当前server的插件观察入口从真实ControlPlane取得配置/home，不用伪Agent；固定白名单facts只在原completed回执耐久后发一次，重放/GET不发，观察器失败不重签来源或反向改变已完成事实。默认关闭零事件。plugin panel共享hub仅补真实配置读取，不改插件权限、展示服务或建立另一pool。

本片只闭合规则领域，记忆搜索/保存及候选管理仍需原资源writer和准确preview事务后继闭合；不是发行批准，也不以受控异常类型断言替代物理退出验收。

### 原owner写拒绝类型唯一位置

原两个异常类仅从Gateway搬到common/owner_write_errors.py，公开Gateway导入仍是相同类；没有新码、新slot、判权或恢复动作。默认report按原class name保持，__module__自然变化；fresh process导入顺序回归守依赖无环。下层记忆/quota可同对象传播实际拒绝，不能按code文本或普通RuntimeError全部透传。

## 2026-10-17 群配置锁出生末门修复70（候选）

记忆接线核对发现原assistant.configure虽不查额外群监控授权，仍会取得原tool_policy文件锁；冷规则入口此前未给锁文件出生传当前资源写门。现仅给原group_capability_guard增加默认None的birth_commit，默认调用形状与身份/群监控语义保持；实际冷规则使用已绑定本人owner_home的原writer partial。FD创建/chmod在原owner P2/Root短门，线程/OS锁等待在Root外，不把整业务包Root。

旧A43实际HTTP两用例复现2失败，修后2项通过，核实际policy锁inode对应FD、出生持准确群owner P2、flock门外，以及出生前拒绝不打开锁、不completed、不重放。中间首次裸writer遗漏owner_home keyword的TypeError已保留，准确partial后通过，非放宽权限。统一原群/规则/存储组合及非作者复核另记；F3尚未发行。


## /ask 首次远端控制与 /control 共用原C物化（2026-10-19，585候选）

Feishu适配器的普通slash由`/ask`提交；只修`/control`会遗漏ready C的原persistent分支，新owner在W只读构造时仍会因缺目录报`resource_missing`。合法正count、非model/非C管理的`/ask`控制现复用原`coordinator_control_transport`，同一真实HTTP work、认证scope、owner、controller、lease、Root与完整operation原件CAS下，先由C调用唯一`materialize_coordinator_owner_home`创建规范owner目录/种子，再发布原prepared+dispatch。`/ask`命令仍只取原goal优先、其次prompt；运输适配不让body.command覆盖已解析命令，也不改入站body。

不在无来源的`prepare_remote_control`补owner创建，不构造管理员或业务Agent，不给W增加ensure/fallback。原W registry/出生/claim/typed scope与existing-only共享资源检查不变；资源缺失仍是启动安全故障，原`terminal_unknown`和Future向组合根上抛责任保留，不能据回执unknown伪造W存活或正常closure。Count0、主owner、model/C管理、非法slash及普通任务保持原入口；目标拒绝、已存回执重放/状态只读仍不补建owner。

离线回归使用普通临时C租约、原控制回执及真实W控制泵/owner池，仅HTTP工作片与认证为明确fake，不预mkdir目标owner。原新群ready失败与cold waiting记录保留；修后新增精确普通runtime与完整相关消费者闭包另在585工件中记录。它们不代根真实Feishu新home复验、完整full、生命周期、最终源或两OS包验收。

/ask与/control既然共用唯一控制身份合同，缺稳定metadata.message_id统一沿原CONTROL_OPERATION_ID_REQUIRED返回400，冷/ready均不得执行。此为根显式批准的API错误优先级迁移，原冷/ask 503 waiting不再适用缺ID控制输入；不增加wait_missing_identity开关、无来源默认回退或临时operation ID。原凭据拒绝仍先于身份错误，零owner/dispatch/业务副作用均验证，旧失败证据保持。

## /ask 与 /control 的原领域分派一致性（2026-10-19，588候选）

585接回了普通远端控制，但适配器全部slash实际都POST /ask，原model、规则与记忆管理仍被/ask冷态503截断；/control已具备的原领域因而不可达。588把合法已解析控制后的分派从handle_control抽为唯一共用函数。两个入口各保原认证、一次JSON读取及自己的文本权威：/ask取goal优先prompt，/control取command优先prompt。/ask仅把已解析文本写入独立body副本的command，原handler.path、正文对象、HTTP work和来源身份都不改；不伪造_read_json、Root或source。

冷C规则/记忆仍调用原coordinator_owner_management及其原错误映射；ready仍走原persistent ingress封印。模型元数据合同仅允许真实/ask与/control上的原view/select/set_default词法，client-only目录操作与vector不由文本借权。model_request_scope和每次末核继续按实际route及原正文权威检查；模型profile、ThreadStore、原卡和门外Delivery复用原实现。普通远端仍走585的C唯一owner初始化与原W只读执行；Count0、本机、任务命令、普通输入、全局写服务拒绝与G1权限门均保持。

585新增的special冷503预期在本片明确迁移为原领域实际完成和更强回执/owner/重放断言，非法slash旧cold/ready语法边界不迁移；585冻结红绿记录保留。相同认证/metadata/body的真实临时HTTP原6节点在585为/control三通过、/ask三失败503，新候选全通过。补冷/ready、用户/群、模型选择/默认、规则预览与确认、记忆候选预览/取消、缺凭据/缺稳定ID零写、字段优先级及跨入口同操作重放/冲突。完整消费者选择和实际XML另绑588仓外证据，不以collect或fake证明真实飞书、full或包通过。
