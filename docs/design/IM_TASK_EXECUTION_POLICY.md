# IM 任务执行策略与合同

状态：2026-10-12，用户已授权主线发布和打包；此前本地验收保留，下方合同作为发布版本的执行语义，服务器安装不自动切换。

IM 新计划绑定原 ThreadStore 的独立执行线程；TUI/旧计划保留原执行线程。L 整合后可显式按次 isolated；inherit 从原布置聊天读取最新合法模型选择，fixed 保存明确档案引用，凭据不复制。见 [整合记录](../tasks/L_T_IM_INTEGRATION_HANDOFF.md)。
`/delegate enable|disable|status` 保存逐发送者偏好，到部署时区下一零点失效；原入站回执冻结模式与版本。
只读宿主接缝（2026-10-16，独立切片，尚未接入 F3 冷入口）：
`capture_im_dispatch_for_owner(home_paths=..., config=..., scope=..., capability_root=..., capability_config_path=None)`
复用原 `host_im_dispatch.v1` 字段。`capability_root` 必须由可信宿主显式传入原 Agent 构造 root；
有实际显式覆盖时再传准确 `capability_config_path`。二者不是授权，不能从 actor、配置身份或 owner home 猜来源。
调用方必须已经完成真实 owner 配置覆盖与宿主身份解析；`scope.resolved_owner` 缺失、非规范或与完整规范路径不符时拒绝，
不会从角色、正文、配置或渠道名称补出 owner，也不签发执行租约。
`delegation_preference_by_paths` 与旧 Agent 入口共用唯一偏好文件、发送者键、版本及原业务时区日界线；
控制写仍走原原子事务，读取只取完整旧/新 JSON，不创建锁、缓存、owner、Agent 或 Store。
能力配置与旧 Agent 的 `capability_config_path_for` 共用同一纯路径解析：实际显式路径优先，否则原构造 root 下的配置，
该 root 对远程 owner 不一定是其 owner home；选定文件缺失仍沿原随包默认，不改去读 owner home。
原 Agent 的 typed 缓存及更严策略继续保留，冷读不附加缓存。坏配置、owner policy 的读取错误或禁用 `create_subagents`
都使新快照保持原模式，旧 Agent 的更严运行时策略也保留。已受理回执恢复只消费原冻结快照，不随开关、零点或路由变化重算。
来源选择必须随本次读取结果保留：已选用户文件后读取失败，即使随后文件被移走，也返回不可用，不能根据读后二次 `exists()`
换成默认开启配置。存在性未核实也不等于确认缺失。仅旧 Agent 本次确认用户文件缺失、随包读取又失败时保留原默认值合同；
冷入口任何读取失败均返回不可用。读取时新文件出现也不重选本次来源，下一次读取仍按原优先级检查，不缓存缺失默认值。
该接口不改变真实工具权限、网络策略或调度额度；F3 的实际 lease、来源和冷入口接线由其调用方验收，不能用此切片测试代替。
开启后原辅助模型按结构化 chat/task 路由；宿主与模型工具共用 `conversation/session_task_dispatch.py` 的耐久派发，工作项进入原 SessionTaskStore/WakeStore，聊天主模型只生成接单回应。
`/status` 的 IM 设置与孩子状态由 `conversation/im_task_status.py` 读取原事实源，Gateway 不导入核心派活工具或子代理实现；此次移位不改变 schema、权限、幂等键或任何配额；普通模型派活目标没有宿主委派标记时不写该策略，委派目标仍先绑定准确任务 ref 再发正文与 wake。
后台主代理经真实 ToolExecutor/G1 启动子代理，复用根/owner 容量，等待孩子后由原自然回复模型汇总；孩子提出结构化能力申请时，根只获得原父级裁决工具，不代做业务。
容量等待沿原不执行退避；unfinished 不写 DONE，失败不伪装成功；关闭和跨零点不变更已接任务。
`/status` 补当天偏好、截止时间、后台排队/执行与子代理额度。
实施证据和验证边界见 [本轮交接](../tasks/IM_MULTI_TASK_PUBLIC_PACK_HANDOFF.md)；下文保留评估时的现状、约束与验收要求。

后续设计追加（2026-10-12，待实施）：用户确认监控按本人统一管理，管理员 TUI/IM 创建的持久计划均须在 Gateway 重启后恢复调度；管理员可在 IM 查询本人跨会话监控、按要求控制/恢复并调整通知位置。现有“IM 独立后台、TUI 沿原执行线程”只是上下文选择，不能成为必须保持 TUI 在线或只能从原会话管理的条件；多规则与旧线程迁移按显式记录处理。详见 [重启与跨入口管理](SCHEDULER_EXECUTION_POLICIES.md#owner-schedule-recovery-control-review)，本轮未改变已发布行为。

## 1. 用户要求与两项策略的关系

1. IM 布置的定时任务固定使用独立后台执行会话，支持模型、工具及长任务；管理员 TUI 布置的计划沿用现有行为。
   并发复用原每 owner 后台会话上限，不另建一份相同含义的额度。
2. IM 提供 enable/disable 的临时委派开关。开启时任务交子代理执行，结果交主代理后回复用户；
   用户期间可聊天、布置其它任务。默认关闭，每天业务时区零点恢复关闭，不影响已接任务。

两项策略正交：定时任务的执行会话由创建入口决定，不受当天委派开关或零点重置影响。
临时开关只影响 IM 新接普通任务的自动委派策略；关闭继续原行为，不全局禁止模型正常使用子代理。
管理员从 IM 创建的定时计划也在后台；管理员从 TUI 创建仍沿原路径，判据是可信入口，不是文本或管理员称谓。

## 2. 实施前基线与合同接缝（历史）

- 原 schedule 创建把可信当前 thread 写入 job，scheduler 到期把同 thread 和独立 run_id 写入 WakeStore。
  需要在创建原 job 的事务中准备隔离执行会话和来源/回送引用，不在到期时根据 prompt 猜来源。
- 原每 thread claim 保证单飞，不同 thread 可独立执行；后台按 owner 轮转并限额。
  该额度限制同时执行的后台会话，不是历史会话总条数，也不是聊天响应时间保证。
- 普通用户的会话任务派工现被 SESSION_TASK_NOT_ALLOWED 明确禁止，需要同 owner 的最小授权接线。
  不将普通用户改成 main，不把管理员会话工具全部开放。
- create_subagents 已异步，子代理完成已有 exact run/attempt 的直属父级通知、去重、持久收口。
  但有孩子时父级任务仍保持未完成，结果忙时在安全点接收、闲时沿原车道唤醒；
  不能仅增加“全部交给子代理”的提示词就宣称完成聊天与任务的分离。
- max_subagents 当前按根任务统计；owner_policy 另有全 owner max_subagents/max_active_agents。
  独立根任务不能各自扩出一份无视 owner 配额的额度；孙代理和未结束运行也沿原统一计数。

## 3. 定时任务后台执行

来源聊天与执行会话分别引用原 canonical ConversationThread；执行结果保留原 scheduler run/任务身份。
每个独立计划拥有隔离执行归属，不把一个外部入站绑定复制到多个内部会话，不将多个计划全塞进一个后台 thread。
支持 LLM 请求、工具、Goal/等待、子代理与原 Compact/恢复，不将“后台”定义成只能运行脚本。

job 保存创建来源、执行 thread 和可信投递上下文引用；通知地址由原绑定/owner 事实重新校验。
普通用户结果沿统一 DeliveryService 回送受理时冻结的原布置者 IM 地址，保持现有后台私聊通知方式；后来的群发送者不会替换收件人，模型不能任填他人或群地址。
TUI 原计划不迁移；已存在的 IM job 是否迁移、何时生效须单独列迁移记录，不追改在途 run 的执行归属。
超出后台会话容量留在原持久队列；同计划仍在跑又到期时，沿明确的有界重叠/补跑政策处理。
五项到点代表五项各自登记可观测，不要求在 2C4G 同时启动五个模型请求。

## 4. 当天自动委派

命令为 `/delegate enable|disable|status`，本地临时分支已实现。
这是用户临时执行偏好，不是扩大工具授权；enable_subagents、owner/tool policy 和实际额度仍须通过。

- enable：主代理负责理解需求和准备结构化任务，任务主体交子代理；派发后结束短接单回合。
- 普通聊天及必要澄清继续由主代理处理；控制命令走原控制面，不为 `/status` 或关闭开关再创建子代理。
- 自动委派执行决定读取参数和结构化任务动作，不从“查一下/监控/长期”等正文词语硬判任务类型。
- 子代理完成后主代理按真实状态、产物 refs 和任务要求汇总并发送最终结果，保留用户要求的主代理交付链。
- 汇总主代理在该任务的独立后台执行会话工作；共享同一 owner 的身份、人格和模型选择规则，
  不把长汇总重新插进聊天 thread。来源聊天、接单 request 与执行 task/run 分别关联，
  接单回合结束不意味着执行任务已完成，不强行把未结束子树标 DONE 来释放聊天。
- 各任务独立归属、停止和交付；满额的新工作项在既有耐久任务/唤醒链等待准入，
  不通过忽略额度、临时换成前台执行或每条消息重置计数来兜底。
- disable：后续任务回原模式；正常模型工具选择、原有子代理能力不被禁止，已有任务继续原执行方式。

若把所有子代理结果仍送回当前聊天会话，让该会话等待和长时间汇总，就不能满足“不堵聊天”；
因此独立任务执行与主代理后台汇总是此开关的底层要求，不是另一个用户可见开关。
独立会话解除执行互斥；模型端点、网络和机器仍共享，不能承诺任何负载下零延迟。
沿原调度/模型准入链保留聊天资源余量，不新增第二个 Gateway 或独立的旁路调度器。

### 4.1 后台主子权限与审批追加（2026-10-12，R06/R07，历史目标；2026-10-19状态勘误）

用户把开启开关后的后台主子链路列为本轮权限修复的核心验收：普通用户、管理员非 full-access 和管理员 full-access 都必须在自身获准目录正常工作。后台派发、父级唤醒、子/孙执行和主代理汇总均复用原 owner、工作范围、run/attempt 与工具裁决，不因从聊天进入后台而丢权限，也不增加权限。

主代理对孩子的能力申请沿原父级工具裁决；主代理自身或孩子需要用户批准的具体调用沿同一审批账处理，不能互相替代。只有 IM、没有 TUI 在线时，用户也应能收到并决定本人任务的有效审批，随后原执行继续；等待不占住来源聊天或其它独立任务。当前原私人卡发布、无TUI审批等待与精确scope决定已由 `conversation/im_tool_approval.py` 接入原审批账；正Count回调沿原 `private_approval_control`，`test_im_tool_approval.py` 有无TUI及实际HTTP回原waiter的组件回归。最终新源真实IM的卡片送达、本人决定、原执行续跑及主代理回送仍未由组件测试代验；历史“待实施”不再表示入口不存在，亦不把孩子的capability grant当成用户对具体调用的批准。

关闭开关、零点、并发任务和恢复都要纳入验收：前两者不改变已接任务的执行方式，后续撤权与审批失效仍按原合同生效；通知和决定严格绑定原用户、任务与运行轮。详见 [家目录与后台审批方案](OWNER_HOME_PERMISSION_REVIEW.md#background-delegation-permissions-review) 和 [原审批桥的 IM 追加](SUBAGENT_TOOL_APPROVAL_BRIDGE.md#im-approval-followup-20261012)。本追加只改文档，不改变已发布策略。

## 5. 零点重置与任务快照

建议使用原部署 timezone（可明确设为 Asia/Shanghai），不使用开发电脑时区，也不以固定 86400 秒代替下一业务零点。
为用户保存唯一的当天启用有效期，读取时验证到期，下一业务日有效值自动为 disable。
不需要零点遍历所有用户写文件、不唤醒 LLM、不发送重置通知；Gateway 零点停机，重启后同样按已过期处理。
重复 enable 仍到当天结束；disable 立即影响后续受理。状态显示启用/关闭、有效截止时间和所用时区。

接单的原持久记录冻结执行方式与设置版本；排队、执行中、等待结果、汇总和投递恢复沿该记录。
零点和 disable 只影响后续新接任务，不取消、转成前台或回收旧任务的子代理。
这种快照只固定执行方式，不冻结或绕过后续安全权限撤销、原审批及 UNKNOWN 恢复合同。
定时计划的后台策略独立持久，不随用户当天偏好改变。

用户范围必须来自可信渠道身份，不能让群里一人 enable 改变全群其它用户，也不能影响该管理员的 TUI。
源用户设置与任务 owner/群 workspace 权限是不同概念；若当前 schema 不支持逐发送者偏好，先明确补齐再开放群聊。
普通用户只能改自身偏好；管理员配置与能力参数继续各守原设置/能力配置权威，不为临时开关另建影子设置文件。

## 6. 展示与验收

后续 `/status` 分别显示：委派偏好/有效期、后台任务与排队数、子代理数量/额度；独立任务不能冒充子代理。
进度消息按各 task/request 隔离，最终回执保留原实际投递状态；固定控制回复与模型生成普通回复的边界不变。

必须验证：

1. 普通 IM 与管理员 IM 创建计划均隔离；管理员 TUI 创建计划行为不变；所有来源都支持原 LLM 路径。
2. enable 后两个独立任务运行时聊天可单独执行；某任务失败、停止、汇总不影响另一任务。
3. 不同 root 的子代理仍计入 owner 原配额；满额不部分创建、不偷偷增加容量，待准入可观察。
4. 零点前接单、零点后执行/汇总仍保留原方式；零点后新任务关闭；disable 同理。
5. 零点 Gateway 离线后恢复、设置和提交并发、任务重试、时区/DST 边界均不延长临时偏好或重派任务。
6. 结果唤醒与聊天同时发生不启动两个同 thread 父执行器；最终送达去重，来源与产物不串任务或 owner。
7. Fake tool/LLM/replay 后，在单 Gateway 的真实 Linux 与 IM 测试身份上验证；分别记录接单、模型首字、
   排队、运行和投递时延，不能把快速确认当成真实聊天生成或任务完成。

## 7. 源码落点与实施顺序

- `scheduler/tool.py`、`scheduler/repository.py`、`scheduler/service.py`：创建入口、job/run 关联与原到期队列。
- `conversation/store_threads.py`、`session_tasks.py`、`session_messaging.py`、`run_claim.py`：执行 thread、任务/权限与单飞。
- `cli/gateway_loops.py`：既有 owner 轮转和后台会话额度。
- `agent_core/orchestration/capacity.py`：根/owner/task/单次统一子代理容量，拒绝本批部分创建。
- `subagents/runner_completion_wake.py` 及结果持久收口模块：精确终态、直属父级交接、恢复与去重。
- `command_catalog.py`、IM/Gateway 控制服务、原设置领域：命令、临时偏好、生效读取及状态投影。
- `delivery/service.py`、原后台投递与 PendingGatewayReply：可信回送、单条进度和独立最终结果。

沿用前轮已核对的 OpenClaw isolated cron/run identity 与 delivery 分离模式，以及本项目父子并行合同；
本轮源码复核只是评估，不作为此功能已实现、真实 IM 已验证或上游最新状态的声明。

建议先实现入口决定定时计划执行会话，再接当天委派和主代理后台汇总，最后做并发与跨零点联合验收。
其它 agent 可并行只读审阅合同、准备测试数据；实施时不能同时修改同一调度/会话/父级通知函数或重启线上实例。


## 2026-10-18 委派主session唯一发话（489本地候选，未发布）

用户开启/delegate后，原host_im_dispatch/host_im_task_routing受理快照和SessionTask/WakeStore派工不变。主sender的短回合只沿原零工具模型生成一次final；原宿主确认派工后才设置本request的final-only普通IM投影，不发送该ack的draft/progress，不影响下一个聊天请求或rich内部记录。排队/派发未确认不制造成功ack。

独立后台work会话与child只执行及向原父交结果，最终摘要保原SessionTask终态/refs；后台普通片只在自身线程内部留档，不把work模型记录冒充sender。原session_task_result:task:status键的guidance先耐久追加，再同key原session_message wake；主sender忙时沿原安全点reserve→submit→ACK，精确同key已consumed才跳过wake，空闲时沿主sender自身claim/model零工具汇总为最终IM通知。后台actor/child执行权不复制给主sender；群后来新发言者不能替换原结果群目标。原取消、actual typed、deadline/P3责任保持。

终态后guidance/wake半提交只在原来源重入补同task/status，不重做模型或业务、不再扣配对额度；accepted未能推进、回报写/读忙不能按已交接确认原wake。原完成root的准入仅给这条无模型metadata补账入口保留路径。空摘要/仅refs按真实终态与完整guidance/wake发布事实收口，不造正文。已external_sent的旧冻结信封仍只补canonical，不重发或重新执行。

共享只读im_delegation_delivery核原current claim对象/同claims、已发布runtime_authority、当前runner/attempt、root TaskLink、owner与原binding；坏链/缺根不降普通。原创建目录最终做本人send_message减法，末消息工具另核同一实际来源并拒绝向origin抢话；其它明确获准的外部业务动作仍走原权限，不扩大收件人。普通child/非delegate及原canonical scheduler execution_run保持既有目录/通知，包括合法旧work线程的schedule followup；不能只见线程旧标记便吞掉调度通知。

额度fallback对真正委派保原quota等待/wake，不由后台发固定通知、不假称主sender模型成功；原额度恢复仍需原控制事实。此候选没有新增队列、存储schema、registry或新执行源。准确普通离线测试/输入SHA/未闭点在489工件；未启动真实服务、模型、Feishu或native，不能代root真实Count2验收。

489 多结果边界：不同原session_message消息键不能合批后只按primary消费判据退休；仍逐原wake准入。主sender表达轮从本轮原reserve实际注入条目逐一回核canonical SessionTask/key/sender，将所有已注入委派结果放入事实包，原submit→ACK逐条确认；不得只说primary却消费sibling。

489 本地域兼容：current child与原claim/BG运输先核；仅原typed RunParams default/isolated、无会话thread、无host actor是普通本地范围，不需要Conversation TaskLink。conversation域丢attrs仍查canonical，缺child根/缺claim的BG运输不降普通。真实SimpleAgent本地两域及原创建幂等普通邻例回归；旧SimpleNamespace测试明确使用原RunParams域，不伪造权限。
