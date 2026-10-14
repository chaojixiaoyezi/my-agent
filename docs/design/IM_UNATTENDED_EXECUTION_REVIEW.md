当前实施导航：P01–P12 已落通用主链，综合验收与发行见 [实施合同](IM_UNATTENDED_IMPLEMENTATION.md)。下面保留只读审计时点和原方案；“未实施”仅描述该历史阶段。

# 企业 IM 无人值守执行、审批与 CLI 接入评估

状态：2026-10-14，源码定位与方案评估完成，产品修复未实施。用户明确一个管理员服务几十万员工，员工布置任务后可能离开；正常任务不能依赖逐条人工审批，也不能把所有请求转给管理员。原安全专项仍暂停，本轮只核对所报审批故障、正常执行、资源释放、凭据与 CLI 的接缝。

用户后续要求先审计能力发现：真实审批模式未披露、普通子代理工具默认不含新增通道能力、关闭凭据开关仍声明 ready 等事实见[只读审计](IM_CAPABILITY_DISCOVERY_AUDIT.md)。本轮不执行产品修复。

## 已核事实与现场边界

| 事项 | 当前事实 | 证据 |
| --- | --- | --- |
| 自动执行 | 已有 owner `permission_mode=ask/auto/full-access`；auto 免可选确认，当前缺字段默认 ask；普通/群缺 IM 选择入口 | `user_space/approval_mode.py`、`owner_policy_seed_payloads.py` |
| 取消子代理 | 模型 `cancel_subagents` 的 effect 为 dangerous，默认 ask 会询问；handler 另有直接父子与 owner 授权 | `agent_core/orchestration/tools/cancel.py`、`tooling/action_policy.py` |
| 停止通路 | `/stop` 和代理详情停止已有不经工具审批的原控制链，核对准确 root/claim/子树并清理 | `gateway_parts/control_service.py`、`conversation/agent_control.py` |
| 卡片点击 | WS 用旧 persona 的 token/choice 给新审批卡去重，新按钮只有 reference/decision，全部撞 `card:None:None` | `adapter/feishu_ws.py:237`、`conversation/im_tool_approval.py:85` |
| 审批等待 | 同步 0.05 秒轮询，worker、请求/子 session 租约仍占用；已投递私人卡有效期为 1800 秒 | `agent_tool_approval.py:168`、`im_tool_approval.py:21`、`round_execution.py:533` |
| 无人回应 | 无消费者/有效卡时 1.5 秒后 unavailable；普通工具却仍投影原 APPROVAL_REQUIRED；续租消费者没有独立绝对截止 | `agent_tool_approval.py`、`round_execution.py:580`、`plugin_gate_policy.py:150` |
| 等待数量 | MAX_PENDING_COUNT=32 只截断查询，发布时未形成同等上限；失效项隐藏不等于已回收 | `agent_tool_approval.py:120-149/226-245` |
| 凭据收集 | 现有私人卡、加密 TaskCredential 和 cref；Basic/Bearer/header 已接宿主请求，输入仍同步等30分钟 | `tooling/task_credential_tool.py`、`user_space/task_credentials.py` |

两张不同审批卡的真实 WS 回调入口合成复现只收到第一张；首张要求解锁、密码回调完成后重按原卡也被吞。探针自然 exit 0，零真实飞书请求，事实存 `/tmp/IM_APPROVAL_CARD_DEDUP_READONLY_PROBE.json`。这确认底座缺陷存在，不能证明现场管理员点击失败必由它引起；现场原始回调和错误尚未取得。

不能将所有 pending 都说成无限：已送私卡有30分钟上限；活跃消费者续租路径可长期等待。现有配额限制不等于释放执行资源，也不能证明几十万用户容量。

## 推荐的统一行为

| 编号 | 行为 | 复用与约束 |
| --- | --- | --- |
| U01 | 已授权日常操作默认自动执行 | 复用 auto；宿主统一默认，未显式选择的旧/新 owner 同源继承，显式 ask 保留、坏策略拒绝；不逐人配置或批量改几十万文件 |
| U02 | 停止本人任务和子树无需审批 | 复用原停止/取消原语；配置不能让本人止损挂起，仍先验证准确 actor/owner/root/父子控制关系；审计及资源收口保留 |
| U03 | 群内和无人值守执行不等待工具审批 | 必须确认但无人可处理时，在发布 pending 前明确拒绝本操作；正常已授权操作沿 auto，权限不足不回落管理员或提权 |
| U04 | 必要输入是持久依赖，不是运行进程 | 凭据/OAuth 可以等对应人输入，但让出 worker、子代理与执行槽；原任务/Wake/续跑合同记录期限、取消和一次恢复，超期结算，不另建等待线程或队列 |
| U05 | 凭据按任务或监控授权保存 | 群触发后发原人私聊卡；复用 TaskCredential，补有效期、更新、撤销及失效后暂停依赖任务；长期监控不因聊天锁定逐轮索要密码 |
| U06 | 内网认证由宿主处理 | 在原 HTTP/凭据出口补通用登录正文、会话/CSRF、刷新和签名能力；业务脚本只交普通数据与引用；秘密环境变量给脚本不等于模型不可见 |
| U07 | CLI 是飞书能力后端 | 复用 feishu_cloud、Delivery、owner/actor/群目标；群应用身份，个人数据本人 OAuth，CLI 不能解决内网账号、审批或停止；详见 CLI 评估 |
| U08 | 运行槽与用户规模分离 | 复用每用户/群/根任务配额、全局队列和公平调度，等待不占执行槽；按容量限制运行并回收子树/后台进程，不能按员工人数启动常驻 runner |

读写自己的业务目录、受限 Shell、已开放的 Skill/MCP/公共能力及云资源，应在已有权限内自动执行。安装新插件/MCP、修改宿主/权限、扩大网络范围及当前未授权资源仍受结构化策略约束；群中未满足的本人确认直接拒绝。资源删除可以在已有明确授权范围执行，但不能因 auto 变成跨 owner 或整机删除。

auto 不是 Full Access。权限与网络策略必须先于可选确认；子代理继承原父级与 owner 上界，能写分配的任务目录和被授予的业务目录，不继承管理员整机路径。源码中 SOUL dangerous/always 与 LAN 长期许可前置存在静态合同差异，实施前须核正式执行链，不能仅按注释声称 auto 保住所有强制确认。

## 等待与凭据接线不能跳过的合同

当前审批/凭据回调要求原 claim/attempt 有效，直接把 while 改成 return pending 会让旧卡失效。必须沿原暂停、Wake、attempt 续接合同设计合法持久等待，保留 actor、原调用、参数哈希、资源、operation/idempotency 和精确恢复绑定；原任务已取消、调用变更或授权撤销后，旧卡不得继续业务。

原无人可批准/过期应投影明确原因与不可重询的该次结果，不能继续返回原 APPROVAL_REQUIRED 让模型反复请求。等待条目需原账绝对期限与发布数量门，失去进程后按同源身份回收，不以展示列表隐藏替代结算。

30分钟目前限制输入卡和等待，ready cref 使用时没有独立 TTL 校验；监控 job 绑定可以用于后续正式轮次，尚无完整 ready 更新/撤销入口。现有 header 注入不等于支持登录 JSON、自动 Cookie/CSRF、Token 刷新或签名，CLI OAuth 也不替代第三方平台凭据。

私人卡片客户端密码遮蔽、清空、首次私发仍待真实平台验收；回调只认原人不证明群内输入仅原人可见。默认群内只留触发入口/状态，秘密填写在私人通道；若未来原人可见的群临时表单通过官方与桌面/移动验收，再考虑增加入口。

## 验证、参考与实施顺序

- 本轮已有四文件定向测试自然 exit 0，共89项：owner_approval_mode、im_tool_approval、orchestration_cancel_subagents_tool、im_subagent_permission_matrix。没有生产源码改动，不将这些既有测试当成新去重缺陷已修或真实群验收。
- 参考先查 Free-Code 合同索引 sheet2 的 forkSubagent(5)、bashPermissions(74)、modeValidation(155)，再读正式 permission 流：硬规则先裁决、非交互 ask 转拒绝、取消信号直达执行链。采用通用结构，不复制其模型分类器或无界授权。
- 第一批：止损免审、typed 卡片去重、群/后台明确拒绝与错误终态；统一默认 auto 的硬授权前置一并验证。只有这些通过才改变企业默认。
- 第二批：原任务持久输入等待/唤醒、凭据生命周期与通用认证；CLI 受控后端可并行，但正式运输和响应合同必须先齐。
- 验收必须覆盖普通私聊、群、管理员非 full、主子孙及后台；无人回应时执行槽/进程不增长、取消时 pending/后代/后台进程收口、重复/旧卡不重复业务、已许可任务正常产出。另做共享执行容量与大量仅登记 owner 的压测，不能声称89项已证明企业容量。

## 审批占住群会话追加审计（2026-10-14；现场报告日期为 10-15）

用户追加 `.20261014.2` 的群会话事故，要求并入只读审计。报告给出的 active run、BLOCKED child、当前 Gateway PID 的 `attempt-exec` 锁、attempt generation=10、无新用户消息入对话及隔离 wake 均为现场转述；本轮未取得该现场原始数据库、回调和逐代事件，不把下述机制确认当作事故全链路复现。审计源码仍为 `b3e5aae686c067ee19c03672ab717f7526175acb`，发行 `.20261014.2` 源码为 `a488b76bc`。

### 结论与必要纠正

| 编号 | 审计结果 | 证据与边界 |
| --- | --- | --- |
| F01 | 审批超时没有给普通工具形成明确拒绝终态 | `agent_tool_approval.py:168-195` 在私人卡过期且无活跃 consumer 时删精确 pending，返回 unavailable；`round_execution.py:580` 经 `plugin_gate_policy.py:150` 对非插件仍保留原 APPROVAL_REQUIRED。该码可重试，不等于本次操作已被拒绝；后续新的调用可能重新请求审批。 |
| F02 | 单卡 30 分钟不等于任务总等待上限 | `im_tool_approval.py:21/139` 限制卡期限；wait 的 `lease_fresh` 为 TUI/Web consumer 与 IM 有效卡的 OR，consumer 持续续租时没有独立绝对截止。不能写成“完全没有超时”，也不能据卡 TTL 声称自动释放执行槽已实现。 |
| F03 | 新消息可受理后等不到模型安全点 | `http_handlers.py:751/1029` 将控制命令先分派，普通输入走当前执行的 steer；`control_service.py:677/1701` 持久记录 guidance，等下一模型安全点消费。同步审批等待期间不推进模型循环；对话无新消息不足以证明飞书入站丢失，需合查 ingress、control receipt 与 guidance 状态。 |
| F04 | 模型止损被审批，显式控制已有免审批路径 | `cancel_subagents` dangerous 仍可 ask；`control_service.py:470` 的 `/stop` 经控制面停止本人准确目标，不走 ToolExecutor。应修模型止损与自动超时，而非只让用户自己输入命令。当前事故未提供 `/stop` 的真实控制回执，不能断言此入口也已失效。 |
| F05 | 当前 PID 锁和第 10 代不能直接视为僵尸/审批重试十次 | attempt generation 是执行代次；合法续片/Compact 也会创建新代。已知仍活 PID 的执行锁不能仅凭过期或无模型输出强删，否则旧执行仍可能写入；需核原 run/attempt/claim、进程出生时间、线程/取消状态和代次事件。 |
| F06 | restart_gateway 强制审批不是新版本才引入 | `030e282f7`（10-02）已加入 always；`.20261012.8`、`.20261013.4`、`.20261014.2` 均含此声明。admin_controls 后续放宽只读 list/resources，写入仍 always。既往成功与本次失败的原因仍需准确 tool_call、模式、批准 binding、配置来源和执行入口证实。 |
| F07 | 报告把 MCP 配置误当全局开关 | `agent_config.yaml:1099-1119` 的 tool_approvals 属于每个 mcp_servers 的逐工具配置，内置工具另读自身 ApprovalPolicy 与 owner 模式。关闭 im_tool_approvals_enabled 只停止 IM 发卡并保留 TUI/Web 等待，不能作为全局免审或不挂起的修复。 |

`auto` 只免可选确认，不能越过 always。若要让管理员已授权的重启/管理写操作免逐次确认，必须明确修订这些工具的部署策略及操作范围，不能仅切 auto 后宣布达标。用户报告中的“需要提权的命令”须按真实权限裁决：权限内免可选确认，超出 owner/沙箱/网络授权则立即明确失败；不能因为取消审批而授予宿主 root 或跨 owner 权限。

相同等待期限缺口还见于 `gateway_parts/permission_bridge.py:59` 的交互 stream 等待；它检查取消、决定文件和模式提供者，没有绝对 deadline。`ToolApprovalWaitOptions` 同样没有最大等待字段。非交互 stream 会立即返回 unavailable，应与交互等待分开；本轮加速探针中的交互 stream 到模拟 3600 秒仍需显式取消退出。

### 生命周期与恢复边界

现有后台恢复器不是完全缺失：ConversationRuntime 已周期扫描孤儿 attempt（约 5 分钟）；RuntimeDB 对仍活的 runner 不按孤儿处理。`runtime-stale-attempts --settle` 面向缺 runner 身份的旧记录，不能当作当前 Gateway PID 持锁事故的通用修复命令。`attempt-exec:<agent_run_id>` 是单个 AgentRun 的执行权，不是群级互斥锁；不能仅凭该行证明它独自锁死整个群。

审批同步等待时，`runner/session_pool.py:97-124` 的独立心跳仍续租并读持久取消。默认 `runner_timeout_seconds: off`，普通子代理没有外层总时间上限兜底。执行器确实退出时已有 `executor_liveness.py` / `executor_recovery.py` 的准确恢复：无未知副作用则失败收口，有未知副作用保留 BLOCKED/UNKNOWN；活着等待审批不属于此类退出。

需新增的合同落在原审批/执行生命周期：绝对等待期限不能被 consumer 续租、新卡或 attempt 换代无限延长；超时写明确原因及不可自动重询的结果，原请求、子树、pending 与执行槽分别收口。本人取消需绕过可选审批而保留控制关系验证；未确认旧执行已停时不能先释放独占执行权给另一轮。对于有真实外部副作用但结果未知的操作，保留 unknown，不伪造为未执行或自动重做。

### 现场归因仍缺的最小证据

- 同一根请求所有 attempt 的创建/结束时间、reason、status、父子关系、claim 与 PID/start-time；generation=10 单字段不足。
- 审批的 permission_id、创建/过期时间、运输状态、决定及 unavailable 原因；只要元数据，不要账号、命令秘密或聊天正文。
- 最新群消息的 provider ID 对应 ingress 阶段、Gateway 受理/steer 回执和 guidance 消费状态；隔离 wake 的结构化原因。
- 失败审批卡的引用类型、回调结果码、原 actor/owner、原卡与当前 attempt 的绑定是否一致；与已有去重碰撞分别核实。
- restart_gateway 前后调用的准确源码版本、有效 owner 模式、工具策略、批准来源和 handler_executed；管理员标签本身不能解释差异。

### 追加验收要求（待实施）

1. 群/普通私聊/admin 非 full 的主、子、孙与后台执行：需要的权限已授权时不逐条审批；缺权限明确拒绝。卡发不出、无人处理、决定迟到均不永久占执行槽。
2. 无人回应直到期限、consumer 持续续租、反复新 call/换 attempt 三组都验证总期限和结算，不只测单卡到期。
3. 等待期间通过普通消息和明确 `/stop` 分别验证：受理状态可查，取消无审批且真实停止后代/托管进程；后续新消息能正常执行。
4. 区分活执行、已死 PID、PID 重用、旧 claim 和未知副作用，恢复不得双执行、误杀其它任务或跨 owner 清锁。
5. 卡片去重、真实身份与原调用重放单独验证；功能发现显示真实模式和失败原因。不能把关闭卡片运输当免审完成。

### 本轮隔离验证结果

- 原控制/插话/后台输入三文件共 37 项通过：`test_gateway_control_operation.py`、`test_steer_delivery_recovery.py`、`test_background_input_recovery.py`。XML：`/tmp/my-agent-approval-stall-controls-20261014.xml`。
- 原收口/执行器退出/后台审批三文件共 60 项通过：`test_r103_closeout_gate.py`、`test_executor_exit_recovery.py`、`test_background_tool_approval.py`。XML：`/tmp/approval-lifecycle-audit-20261014.xml`。两批均自然退出、零失败/错误/跳过；不能据既有测试绿宣称新增问题已修。
- 加速时钟合同探针：实际发布/签名/等待函数配合合成运输与固定 scope，单卡到 1800 秒返回 unavailable 并删 pending，普通结果仍 APPROVAL_REQUIRED/retryable；新调用得到新 permission_id 与新的 1800 秒。过期卡加持续 TUI consumer，模拟 3600 秒仍 pending，仅显式 cancel 退出。证据：`/tmp/my-agent-approval-audit-evidence/approval_wait_probe.json`。
- 真实 RuntimeRepository 临时数据库探针：活 PID 锁过期 24 小时仍 active、孤儿数为 0、reclaim=lock_active；确认退出的探针子进程则识别孤儿并 cancelled。零审批事件也可合法生成第 10 代；重复活 child 被拒；精确取消收口后锁消失。证据：`/tmp/approval-lifecycle-probe-20261014.json`。
- 三版源码 54 个纯审批裁决组合均符合现有策略：restart 在 ask/auto/full 下无准确批准均 ask，有准确批准才 allow；三版重启工具源字节 SHA-256 相同。历史工具/策略模块使用当前共享类型，非三个旧安装包实测，handler 调用为 0。证据：`/tmp/ADMIN_APPROVAL_REGRESSION_AUDIT_20261014.json`。
- 所有探针无真实模型、飞书外发、生产 owner 访问或重启。现场一小时挂起、卡片点击失败与第 10 代的完整因果仍待原元数据；只改审计文档，未修产品。

## 逐项修订方案（2026-10-14，说明方案，未实施）

用户要求具体说明每项怎么改。本节承接 F01～F07、U01～U08 和发现审计 D01～D08，不新增并行权限账、等待队列或云工具入口。执行应清楚分为：当前权限内自动执行、需要人工确认但本入口不支持时明确拒绝、缺凭据/OAuth等外部输入时持久等待并释放执行资源。

| 项 | 修改位置与方法 | 用户行为与验收重点 |
| --- | --- | --- |
| P01 日常默认自动执行 | 原 approval_mode 解析增加部署默认来源，配置归 capability_config；owner缺permission_mode才继承，显式值保留，坏策略拒绝。每次执行仍读原工具/路径/网络/资源权限。 | 员工无需逐人设置；主/子/后台在授权范围内免可选确认。auto不改变access_mode或扩大读写/网络范围；旧管理员full配置不得被默认值意外降级。 |
| P02 管理员工具 | 在原 ToolRuntimePolicy 按可信local/main与结构化operation修订正常重启/管理操作的确认要求；不是仅切auto，也不是整个admin_controls给群开放。保留重启排空、冷却和操作审计。 | 管理员已允许的操作无需再次点卡；普通群不会因发起者是admin而获得宿主身份。权限配置变更仍须原部署管理入口，模型不得自行提高自己的权限。 |
| P03 本人止损 | cancel_subagents/停止本人任务/托管进程接原控制取消原语并免可选审批；保留准确owner、父子关系和目标。控制不得排在被停执行的模型队列或执行锁之后。 | ask/auto/full均能停止本人目标；同时核对后代、审批记录、托管进程和执行槽收口。未确认停止须如实报未确认，不能先清锁返回成功。 |
| P04 审批终态与总期限 | 群/无人值守在发布pending前将剩余人工确认要求转明确不可自动重试的拒绝；保留交互审批时沿原记录写绝对deadline，统一原IM/stream等待。建议沿用当前30分钟上限并允许部署调整，consumer不能延长。 | 无人可批/运输失败/到期分别有明确原因。普通unavailable不再退回可重试APPROVAL_REQUIRED；同一任务原操作和参数摘要的超时/拒绝不会因新call_id或attempt而重开等待，新明确用户授权才允许重新请求。 |
| P05 等待、聊天与恢复 | 正规输入等待沿原SessionTask/Wake保存依赖，退出执行并释放worker/claim/槽；“任务待输入”与“当前线程在执行”分开。恢复重核授权后只调度一次。旧记录按原执行身份和死亡/取消证明收口。 | 用户能继续在同群聊天；新消息不再被投进没有安全点的审批循环。持久输入保存成功且旧执行确实退出后才放行新的执行权；活PID不按时间粗暴删锁，未知外部副作用保持unknown。 |
| P06 卡片回调 | 飞书WS按typed kind与准确reference/action/event身份去重，复用原pending原子决定；无效/未解锁点击不得提前消耗合法决定，重复成功回调返回原结果。 | 多张卡分别可用，解锁后可重按；迟到、转发、他人点击、旧attempt卡不复活已结束任务。凭据卡同样检查运输去重，但秘密不进日志。 |
| P07 自身能力查询 | 扩展原list_tools/tool_search/runtime snapshot只读投影，返回真实模式与来源、身份范围、可用/折叠/未配置/禁用原因、正规使用入口；/status与/help共享来源。提示词仅给短导航。 | 问“你能做什么/为什么不能”能查事实作答；普通/群能查自身，不能读取全局配置、其它owner或秘密。模型可解释，不能由查询直接修改授权。 |
| P08 目录与实际一致 | 修request_credentials availability读取同一开关与依赖；list_tools短视图保留用途和必要原因，工具详情按需读取。同步tool_search实际线程加载语义与原两项冻结集合测试。 | 关闭功能不会继续显示ready；未授权、未配置与接口失败能区分。可用性查询不等于某份资源已授权，不为修测试扩大权限。 |
| P09 主子/后台目录 | 在原tool_grants/create_policy/role_contracts统一生成默认目录，取父级实际可用集合、角色需要及owner/任务授权交集；给正常子代理补自查，正常业务能力按派工授权继承。内部后台续跑使用同源任务快照，正式schedule保留原完整owner目录。 | 子代理不因旧coding名单漏掉已授予云/凭据能力；leaf不因此获得创建下级或宿主管理权限。Compact/恢复保持身份和授权版本，撤销后下次执行生效。 |
| P10 凭据输入与使用 | 复用TaskCredential与私人卡，字段按受控schema声明；群触发、原人私填。补ready引用有效期/更新/撤销和P05等待恢复；原宿主HTTP路径补通用登录正文、受限Cookie/CSRF、刷新与声明式签名。 | 明文不进群/模型/脚本上下文；只有任务/监控授权范围可用，子代理按原任务绑定使用。不能把秘密注入任意脚本环境后声称模型不可见；不开放任意代码读取密钥计算。 |
| P11 飞书CLI | 保留feishu_cloud和Delivery入口，宿主CLI后端只执行准确授权操作；使用已核官方扩展接口，先对齐拒绝重定向、原始错误与未知写入结果。本人OAuth单独绑定owner+actor，群默认应用身份。 | 员工无需装CLI或复制secret；当前已授权表格可读写，个人资源需本人授权，群不能借admin登录。消息/文件仍沿原群目标与投递幂等链。 |
| P12 配额与诊断 | 复用原owner/root/全局配额及调度器；等待记录不占活worker，发布依赖有数量上限，禁止每用户独立轮询。原审计记录等待原因、开始/截止、权限来源、取消和结算，重启按准确身份恢复。 | 大量离线用户不会留下等审批的常驻线程；达到额度明确排队/拒绝且可停止。状态区分执行/排队/待输入/已拒绝，不能仅凭心跳显示正常。 |

实施优先顺序：P03/P04/P06和P05的拒绝/取消收口先闭环，再切P01/P02部署默认；P07/P08可独立并行，P09联测前后台权限；P10的持久输入与P11接口接入共用P05恢复合同。P12贯穿所有阶段。

每项均按合同单测→真实工具/假模型组合→隔离Gateway/IM回调→少量真实模型和飞书验收。验收覆盖普通私聊、群、admin非full/full、主子孙、后台/监控、Gateway重启、取消/超时/重复回调和跨owner负例。真实测试用专用身份/资源；本节仅说明方案，不改变当前安装或生产数据。

## 建议下一步

先实施第一批并冻结取消、卡片与默认执行的真实组合回归；异步输入恢复和 CLI 可以按原模块分工并行，根统一集成。优先消除无人处理的等待资源，再扩大能力；不改现场监控、不替员工批准、不借管理员身份。
