# IM 云文档监控与任务耗时核查

日期：2026-10-13。状态：用户已授权修复、推送 main 和打包；14 项已完成原链路实现与源码验收，发行版本为 `.20261013.5`，准确提交/包摘要及包验证由 release.json 和同包验收说明记录。未修改生产配置、聊天、任务或凭据。下方定位段保留修复前证据，实际验证结果以实施追加为准。

源码基线为 `84a63ea87646570b83e5e808cb29b021c5301b27`，对应最近发布的 `.20261013.4` 源码。本文核对本轮新增反馈和截图，不重复计入以前已交付的 R01～R21。现场报告中的 2026-10-14 时间属于报告原时区，不作为本机部署时间。

共整理 **14 项：监控与命令 9 项、云文档 4 项、耗时 1 项**。前 13 项已确认代码行为、合成复现或产品缺口；耗时已确认若干增时机制，但具体小任务的主要原因仍需计时对照。代码机制得到确认，不等于已经确认内网服务器那一次失败的触发点。

检查只使用源码、默认配置、匿名数值用量和隔离合成账本；不查看生产聊天、任务、记忆、人格正文或凭据。

## 问题总表（修复前定位）

| 编号 | 问题 | 当前结论 | 建议顺序 |
| --- | --- | --- | --- |
| M01 | 命令后的机器人提及被当参数 | text/post 共用入口已复现 | 首批 |
| M02 | 默认空时区使已执行监控列表和历史报错 | 列表、历史已复现，暂无执行时间时不触发 | 首批 |
| M03 | 查询和控制异常丢失具体原因，提示用户刷新 | 多类异常和参数错误得到同一结果 | 首批 |
| M04 | 只读查询依赖可读写的锁文件 | 代码机制确认，不能据此认定现场只读挂载 | 首批 |
| M05 | 查询等锁没有等待期限 | 隔离跨进程竞争确认；现场持锁者未知 | 首批 |
| M06 | 已死亡且过期的运行恢复未接生产调度 | 正常调度合成复现仍留 running | 首批恢复链 |
| M07 | 现有恢复把 unknown 留在活动账，未正常写历史 | 手动调用恢复的合成复现确认 | 与 M06 一起 |
| M08 | 旧群监控缺 creator，合法但对创建人不可见 | 两个合法任务只显示新任务的合成复现确认 | 归属迁移 |
| M09 | 运行快照漏报坏 run，可错误显示 healthy | 同一账本损坏报告与健康快照矛盾已复现 | 与 M07 一起 |
| C01 | 普通用户和群缺少可规模化自助文档授权 | 当前仅管理员逐资源、逐操作登记 | 独立授权线 |
| C02 | wiki 和底层文档需要两次人工登记 | 工具说明、实现和测试确认 | 与 C01 一起 |
| C03 | 管理工具的只读查询也强制审批 | 整个 admin_controls 的 always 策略确认 | 与 C01 一起 |
| C04 | 云接口拒绝原因缺少结构化细分 | HTTP/API 拒绝归类较粗，未证明本次网络失败 | 独立小切片 |
| L01 | 小任务可能做重或每轮等待很久 | 提示、上下文、多轮机制确认，具体主因待测 | 并行计时 |

## 命令与监控查询

### M01 机器人提及位置影响命令参数

`adapter/feishu_mentions.py:13–36` 的文本处理只删除前置可信 bot key；`:41–53` 的富文本处理只删除前置 bot 节点。后置提及沿 `adapter/protocol.py:48–61` 保留，交给原命令解析器。

`@机器人 /schedule` 转为无参数列表；`/schedule @机器人` 的 bot 占位成为 `command.value`，被 `gateway_parts/schedule_control.py:87–92` 当成监控编号，返回 `SCHEDULER_NOT_FOUND`。两种截图结果已通过真实转换和控制函数的合成调用复现。

同源影响包括 `/verbose stream`、`/delegate enable` 不能正常解析，`/status`、`/stop` 参数无效，`/help` 误收 topic，以及 `/schedule history job01` 多出参数。text 和 post 均受影响。

修复应沿原入站入口处理可信 bot 的控制寻址提及，保留真实参数、其它人的提及、代码块及普通消息意义；不按展示昵称删除文字，不放宽直接 @机器人群聊触发门。

### M02 空时区和执行时间显示不兼容

配置合法默认 `timezone=""` 表示服务器本地时区，但 `schedule_control.py:140–141` 直接调用 `ZoneInfo(agent.config.timezone)`。有非空执行时间时抛 `ValueError`，被控制层变成 `SCHEDULER_UNAVAILABLE`。

合成测试中，空时区的 `/schedule` 和 `history` 都失败，改为 `Asia/Shanghai` 后成功；新建未运行监控仍显示“暂无”。这解释了既有只测新任务的回归为什么没覆盖已运行监控。

应复用 `scheduler/schedule.py:358` 的现有时区解析，空值使用服务器真实时区；不另造默认 UTC。现场是否命中本条件仍需配置数值或异常事实确认。

### M03 错误原因和可行动反馈丢失

`execute_schedule_control` 的 `:109–110` 将 OSError、ValueError、AttributeError、SchedulerRepositoryError、ScheduleValidationError 合成同一文案和错误码，没有保留异常类型、errno、等待阶段或关联诊断。函数内也没有对此失败记录结构化日志。

合成验证的权限拒绝、只读文件系统、账本不可读、owner 不匹配、属性错误及多余参数全部返回相同文案，`details=None`。因此“看到兜底说明不是参数写错”不成立。内部抛出的参数校验文案没有到达用户。

外层 `control_operation_service.py:365–390` 有未捕获异常的错误回执；这里的异常已变成普通失败结果，外层不能恢复其原始原因。另有记录级解析错误只进入 `load_error_codes`，列表可能仍显示“暂无”，也应在用户可见反馈中明确查询不完整。

应区分参数、无匹配、版本冲突、权限/只读、锁等待、账本损坏和身份不一致，并保存脱敏的错误码、类型、errno、阶段、耗时、owner 和请求引用。IM 没有“刷新”操作；读请求可做有界内部重试，写控制不得在副作用未知时盲重放。

### M04 只读投影仍需可读写锁文件

`repository.list_jobs():189–196` 沿 `_store_snapshot():663–675` 读取。即使锁外读字节及解析成功，也要取得 `locked_json_path(store.json)` 后复核 stat。共用锁打开侧采用 `O_RDWR`，缺锁时还需创建文件。

因此只读挂载或锁文件不可写会让查询失败；文件正文可读并不足以保证列表可用。真实 owner 宿主目录是否受到这种限制尚未确认，模型沙箱内的只读复现不能替代宿主控制请求的证据。

可评估复用完整原子快照和有界版本复核，为纯展示减少对可写锁的依赖；写控制继续保留原 owner、quota、CAS 和锁顺序。现有 `test_scheduler_scan_costs.py:1368–1380` 明确测试了读锁合同，修改需同步核对，不能随手移除所有锁。

### M05 查询等锁没有期限

`common/json_io.py:510–532` 默认先阻塞线程锁，再取得 `flock(LOCK_EX)`，没有 deadline 或取消检查。真实子进程持锁的隔离测试中，`list_jobs()` 等到释放后才完成，约 252ms；这证明等待机制，不证明现场发生了长期锁竞争。

writer 在 `repository.py:919–950` 临界区还做 quota、SQLite due-index、history 追加和原子写。quota 启用时会扫描 owner home，SQLite busy timeout 为 10 秒。这些可能延长查询等待，应测临界区耗时，不直接当死锁。

可复用原锁的非阻塞模式和原仓库，给查询明确等待期限、内部短重试和“正在读取/账本暂忙”的控制反馈。等待期限、重试次数和写操作的未知结果需要分别表达。

## 监控恢复和旧记录

### M06 死亡运行恢复没有进入生产轮询

`repository.py:975` 已有 `recover_interrupted_executions`，但源码仅找到定义和测试调用。`service.py:155` 的正常调度先预约、再处理 queued；旧 running 不进入 queued，活动车道又阻止下一轮预约。

隔离条件为合法 run 已开跑、claim 已过期、进程身份明确死亡。正常预约/排队调用后，`new_reservations=0`、`queued_runs=0`，旧 run 仍为 running。

应在原 owner scheduler 生命周期接入有界恢复，同时复用正确终态结算；不能只等 queued 的 claim 入口顺带发现 running。

### M07 恢复的终态没有进入正常执行历史

直接调用现有恢复后，`repository.py:983–989` 只把结果更新回 `store.runs`。已开工死亡 run 由 `:1455–1456` 转为 unknown，但活动账本默认解析 `:1064` 不接受该终态。

隔离复现中，恢复数量为 1，原始记录为 unknown，history 数量为 0，损坏报告包含 `SCHEDULER_RUN_INVALID`。正常 `finish_run():555–575` 会移出活动账、更新 job 并写历史，恢复没有复用这些步骤。

必须让恢复生成的真实 unknown 沿正确结算写入历史，不把可能已产生副作用的一轮变成“失败、可重跑”。M06 与 M07 应作为同一恢复链修复。

### M08 旧群任务缺创建人事实而不可见

`SchedulerJobCreateRequest.creator` 默认 None，job 校验允许缺失。`schedule_control.py:53–57` 和 `scheduler/tool.py:669–674` 的群本人过滤要求准确 channel/actor，因此旧合法任务被静默排除。现有 `run_preparation.py:13–25` 的迁移仅补 alias。

隔离账本含旧任务和新任务各一个，合法解析 2、错误 0，查询只显示有 creator 的新任务。旧任务仍可能被调度，但用户无法通过原入口发现或管理它。

迁移需要原冻结来源或其它准确的创建人记录；没有证据时应提供明确接管流程。不能把当前发言人填成旧创建人，也不能让全群自动取得控制权。

### M09 运行健康快照遗漏坏 run

`repository.py:681–696` 汇总 job 的解析错误，却只取 run 解析结果而丢弃其错误；`:627–634` 据此把健康状态标为 healthy。

M07 的同一合成账本可同时出现 corruption report 的非法 run 和 runtime snapshot 的 healthy、空错误列表。此缺口也适用于其它坏 run，不仅恢复后的 unknown。

应复用同一份解析事实汇总 job/run 错误；损坏、不完整和“确实没有活动运行”必须分开显示。

## 云文档自助授权和错误反馈

### C01 当前授权流程不能规模化

`user_space/resource_grants.py:18–31` 只接受准确资源和非空操作集，`:57–59` 仅管理员能登记；无许可会在 `feishu_cloud_tool.py:134–135` 请求前拒绝。管理员和 Full Access 也不能隐式越过资源许可。当前的隔离门有效，但缺少员工和群自助提供本人已有文档权限的入口。

`adapter/feishu_cloud.py:74` 只取共享应用 tenant token，没有终端用户 OAuth/refresh 和可信 actor-to-token 绑定。应用能操作的资源范围不等于该人或该群能操作的范围。[飞书官方调用身份说明](https://open.feishu.cn/document/server-docs/docs/drive-v1/file/get) 明确区分应用和用户身份。

个人方向：一次用户 OAuth，宿主保存 token/refresh 引用，后续使用该用户身份由飞书校验具体资源；保留管理员的禁用、范围上限和审计。模型不接触明文凭据，沿原 owner 和 secret ref 扩展，不建立第二套授权账。

群方向：准确核验当前群的资源共享权限与实际操作能力，再使用宿主应用身份。不能借群里某人的私人 token，把只对该人开放的资料公开给全群。[飞书官方权限总览](https://www.feishu.cn/hc/en-US/articles/360025093793-overview-of-feishu-docs-permissions) 支持群、用户组等协作者及继承权限；真实有效权限还需处理独立权限与保护范围。

官方权限检查请求模型只有 token/type/action，支持 TENANT/USER，没有任意 owner 或群 ID 参数。因此共享 tenant token 的检查只能证明当前调用身份；它不能单独证明发起者权限。[官方 SDK 请求模型](https://raw.githubusercontent.com/larksuite/oapi-sdk-python/v2_main/lark_oapi/api/drive/v1/model/auth_permission_member_request.py)。

“资源编号前缀相同”不是授权范围事实；wiki 空间、文件夹、部门、群范围需要验证真实归属、继承和撤销，不能直接用 token 通配替代。

### C02 wiki 与业务对象双人工登记

`feishu_cloud_tool.py:38` 和 `test_feishu_cloud_tool.py:163–171` 要求 wiki_resolve 与背后 sheet/docx 独立授权。它避免解析自动扩大权限，但把一个用户文档操作拆成两次管理员登记。

应由宿主沿可信 wiki 返回映射得到规范业务对象，结合用户或群的有效权限处理，减少重复人工编号。wiki 可定位不等于自动获得编辑权，这个边界继续保留。

### C03 只读管理查询也弹审批

`admin_controls_tool.py:95–98` 在同一个工具上使用 ApprovalPolicy(always)，即使 action=list/resources 被声明 read_only，仍要求本人确认。`action_policy.py:462` 不让自主或 Full Access 跳过 always。

这解释管理员登记被 APPROVAL_REQUIRED 拦在 handler 之前的反馈；它是当前明示策略。应把只读查看、资源授权变更和已授权资源的业务操作分别表达。已授权 feishu_cloud 普通业务操作本身不是 always，不能说每次表格追加都强制审批。

### C04 云接口拒绝原因分类不足

`adapter/feishu_cloud.py:88–95` 将不同 HTTPError 统一为 FEISHU_CLOUD_HTTP_ERROR，将非零 API code 统一为 FEISHU_CLOUD_API_DENIED，供应商数字 code 只放在文案。

本地未授权、坏策略、网络关闭、缺宿主凭据已有区别；缺口在供应商侧身份失效、缺 scope、资源权限、不存在和限流的结构化分类。这会影响用户理解和恢复选择，但未证明是这次失败的触发原因。

补充受限错误字段即可，不将供应商正文或秘密直接交给模型。429/5xx 或不可验证写入继续保留 unknown，追加不能盲重试。

## L01 小任务耗时的证据和待确认部分

1. `prompts/default.md:5` 允许普通问答直接回答，但 `:26` 将回复数字等都要求工具证据，适用范围较宽；`settings/config.py:131–151` 共用工程执行、读取与验证指导。它们是软提示，不是宿主强制所有问题搜索。
2. 每个 native 工具轮继续发送当前摘要、保留历史及本轮累计 IR，入口为 `tool_model_generation.py:750–780` 和 `backends/message_adapter.py:61–67`；已有窗口/Compact，不是无限发送完整原始历史。
3. 默认工具轮数上限 5000 是失控保护，没有按“小任务”提前结束的目标；持续获得新合法结果时仍能继续扩展查询范围。
4. Mac 基础推理配置是 max，但线程可覆盖；近期匿名账有单次主调用输入 220860 tokens，以及另一条 27 轮主调用并伴随重试。没有正文分类和完整阶段耗时，不能把这些直接定成小任务过度调查。
5. 用户说明此现象在 delegate 之前就存在，因此 delegate 不作为该老现象的解释。向量工具推荐在已核对 Mac 配置中关闭，自学习不强制前台每轮多总结一次。基础 Decision 关闭也不能排除 owner/thread 覆盖，近期账中确有 decision 桶记录。

应先复用 ModelCallRecord 的首字/总耗时、token/cache 和工具 duration_ms，对独立短问答与短执行任务分阶段对照。进程内明细默认保留最近 128 条，持久 model_usage 主要是累计摘要，不能宣称旧账已经能完整还原各阶段时长。

普通问答和小执行任务的提示应限定当前范围、与风险相称地核验；不采用固定几个搜索就强行停机或关键词判断“小任务”的硬门。

## 对收到报告的纠正

| 报告判断或建议 | 核对结果 |
| --- | --- |
| 零字节锁文件保存两天等于残留死锁 | 不成立。永久 flock sidecar 正常保留；隔离测试老文件可读，杀死持锁进程后锁自动释放，文件仍保留。 |
| O_EXCL 与 flock 冲突、旧文件阻止再次加锁 | 不成立。默认 exclusive=False，创建失败后打开旧 inode 再 flock；真正存在性单例锁是其它入口。 |
| file_lock_support.py 实现另一套 flock | 不成立。该文件只是无 fcntl 平台的一次告警，共用 JSON 锁在 json_io.py。 |
| 每次列表都走 _live_repository 初始化所有群 | 不成立。列表和历史提前返回；写控制才调用。冷的当前 owner 首次初始化仍可能发生。 |
| 正常写入可能被查询读到半截 JSON | 尚无此证据。当前正常 writer 使用唯一临时文件加原子 replace；外部截断、损坏与崩溃耐久问题需另证。 |
| 通用失败说明参数没错 | 不成立。参数校验异常也被相同 except 捕获，后置 bot 提及还会制造多余参数。 |
| 直接把 runtime-stale-attempts --settle 接入可修 scheduler | 不成立。该命令结算 runtime.db 的 agent_attempts，scheduler 的 store/history 是另一领域账本。 |
| 删除计划后还存在 running 就一定应自动清掉 | 不成立。删除未来计划允许已开工一轮收尾；死亡恢复必须用租约与 PID/启动指纹事实。 |
| 应用有文档编辑权就让任意 owner 使用 | 不成立。需要可验证的本人或本群权限，当前精确许可门不能直接取消。 |
| 从 history 重建可当当前任务和权限事实 | 不能直接采用。history 不是当前计划、配置或权限的权威账，最多明确标为历史诊断。 |

## 已完成的验证和后续验收

已完成：合成 text/post 入站解析；空时区列表/历史及显式时区对照；六类控制错误投影；真实隔离进程竞争、永久锁文件和死亡释放；过期死亡运行正常轮询与显式恢复；旧 creator 查询过滤。最终组合 `test_im_command_access.py`、`test_schedule_control.py`、`test_scheduler_unknown_recovery.py`、`test_feishu_cloud_tool.py` 共 43 项既有定向回归通过，未覆盖的新问题另由合成案例揭示。文档同步检查及 `git diff --check` 通过。

定向命令（使用项目现有测试解释器）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/agent_py_agent" python -m pytest \
  agent_py_agent/tests/test_im_command_access.py \
  agent_py_agent/tests/test_schedule_control.py \
  agent_py_agent/tests/test_scheduler_unknown_recovery.py \
  agent_py_agent/tests/test_feishu_cloud_tool.py -q --tb=short
```

未完成：内网服务器那次失败的精确异常与持锁者、真实 kill/restart 的整条 Gateway 链、真实飞书普通用户/群 OAuth 和有效继承权限验收、小任务耗时主要原因。这里没有把合成服务当成真实飞书送达或真实现场根因。

后续应覆盖：

- 已运行监控和历史，前后 bot 提及，text/post 和其它人的提及保留。
- 锁忙、只读、权限、坏 JSON、身份不符、参数错误分别得到可理解回执和脱敏关联诊断；IM 不要求用户刷新。
- scheduler 与查询同时进行；查询按期限成功或明确报暂忙，写控制未知结果不重复执行。
- kill/restart 后已死过期 run 正确记 unknown 历史、释放原活动车道，活 runner 和不可核验 runner 不误回收。
- 旧任务归属可证明后迁移；不可证明时明确接管，群成员不能获得他人任务控制权。
- 文档本人可编辑、应用可编辑但本人无权、群共享、只读、撤权、wiki 映射及未知写入读回，全部不下发 app_secret。
- 新旧会话、相同模型档位和独立小题的首字、模型、工具、恢复耗时对照。

参考核对了本地 OpenCode 的合同索引（没有 flock 命中）及 `packages/core/src/util/flock.ts` 的有界等待/取消接口；其目录存在性锁与本项目永久内核 flock 语义不同，只借鉴等待边界，不复制删锁恢复。

## 建议下一步

完成联合严格 gate、Linux 原生 TUI/实际模型及离线包更新验收，然后按用户授权推 main、交付命令。调度与云授权已由独立切片实现，非作者复核并行；授权入口和 scheduler 终态各由单一负责人维护，复用原 owner、审批、secret ref、控制回执和调用账。

## 实施追加（2026-10-13，源码验收完成）

- M01/M02：text/post 控制消息支持可信机器人提及在前、后或两端；保留其它提及、真实参数、代码块和普通文本。空时区沿原 canonical timezone，不再直接构造空 ZoneInfo。
- M03：参数先验证；失败提供稳定错误码与诊断编号。日志仅含操作、阶段、异常类、errno、耗时和可信 owner，不记录参数、异常正文或文件路径；不要求飞书用户“刷新”。部分记录/旧归属不可确认时保留提示，不伪装完整空表。
- M04/M05：列表和状态读取原账本的完整原子代次，不新建写锁、不等待写锁；写方保留原 flock、quota 与版本/claim CAS。
- M06/M07/M09：启动与 tick 接原死亡恢复；明确死亡且已开工的运行进入 unknown 历史并离开活动表，未开工才可排队。已有同代准确终态优先，不重复结算；坏 run 保留健康错误。活进程、身份未知或租约未过期不误收回。
- M08：旧群 creator 只从准确冻结 execution_origin 恢复；无证据明确 unmanaged，不借后来发言者或管理员猜测身份。
- C01/C02：默认允许按准确个人 open_id 或群 chat_id 的实时协作者权限自助使用；每次重查撤权，保留原管理员精确 grants。wiki 按供应商 canonical 映射验证来源权限，无须再人工登记底层对象。表格写入核对保护范围；群不可借个人编辑者身份，append 实际行未知时不能凭起始范围绕过保护。
- C03/C04：只读管理查询免逐次审批，修改/危险动作仍强制确认。云错误细分 scope、身份、拒绝、缺失、限流、额度、暂不可用及映射/保护范围；未知写结果不盲重试，秘密不进入模型结果。
- L01：默认提示按任务规模使用证据和验证，满足需求后无新问题不重复检查；用户自定义提示及推理强度不改。原调用账新增匿名毫秒计时，/status 显示最近请求模型累计、首包等待及覆盖次数，区分实际未知与零；并行累计不冒充墙钟或工具耗时。

详细交接：[调度](../tasks/IM_SCHEDULER_REPAIR_HANDOFF.md)、[云授权](../tasks/IM_CLOUD_REPAIR_HANDOFF.md)、[耗时](../tasks/IM_LATENCY_REPAIR_HANDOFF.md)。最终联合测试、Linux 验收及准确发行提交待收口后补记；合成 HTTP 不算真实飞书租户验证，具体小任务延迟的现场主因不据提示修订宣布已确认。

### 联合验收记录

冻结版本 53 个直接相关测试文件：1250 passed、0 failed、0 skipped。Ruff、doc sync、strict code-size、diff 检查通过；新增文件登记后 clean-package 复验。无新增第三方依赖，生产/测试改动未达 10000 行阈值，按项目规则使用 focused，不重复全仓测试。Linux 测试和自包含运行时的真实 TUI/官方 MiniMax 验收正在执行；发布只在最终 gate 与包检查通过后进行。

最终错误合同登记后，命令与错误码 68 项再次通过。Linux 524 项组合首轮为 523 passed/1 failed/0 skipped；失败是旧基线同样失败的事实缓存测试用同尺寸原地覆盖，当前/缓存元数据签名完全相同。生产唯一写方是原子 JSON 替换，夹具改为同源 writer，保留全部断言、无 sleep/skip；相关文件单独复验，不改 owner 缓存生产逻辑。

Ubuntu 24.04、2 CPU、自包含 CPython 3.11 与官方 MiniMax-M2.7 的两次原生 TUI 验收：普通两句问答 8.62 秒、无工具、主调用 1 次；小文件任务 11.52 秒、主调用 3 次，write_file/read_file 均成功，模型产物准确为 42。各有独立冷启动 capability probe 1 次，无 retry；输入回显 38/34 毫秒。每项仅一次 prompt，不帮模型补产物。稳定空闲约 181 MiB、1–2% 单核 CPU，TUI+Gateway 为 2 个测试进程，退出 TUI 剩 1，停止自己的 Gateway 后 0。此处测试新 wheel 的 CPython 3.11 运行时，最终字节码包仍另做安装/导入/TUI smoke。

原生进程恢复验收通过：实际 PID 指纹与 2 秒 claim，过期活 runner 保留；只 SIGKILL 测试自身子进程后，冷 SchedulerService 将原 run 记 unknown 历史，活动账移除；marker 只有一次开工，重复恢复不重复历史、不重新排队旧动作。它证明原调度服务入口和真实进程死亡链，不冒称完整 Gateway 在业务运行中被 kill 的现场复现。

云授权非作者独立三文件 64 项通过，无确认的跨 owner/群借私人编辑权、秘密泄漏或未知写重放；主工具链对 unknown 撤销后续工具权并阻塞收口。真实租户继承 ACL、保护范围省略字段等尚未实测，证据不足明确拒绝，保留精确管理员授权，不以应用整体有权代替普通用户授权。

Linux 唯一失败修正后，整份 scan_costs 45 passed/0 skipped；Mac 同文件 45 passed。其余 Linux 523 项原已通过，不将 45 与 523 相加成新的无重复组合。六项本地严格 gate 采用实际 focused 与最后增量复验、Ruff、doc sync、strict code-size、diff、clean-package；线上 CI 不作为本次源码验收来源。

### 多 Python 进程反馈

用户服务器截图中可见 6 个 appuser 的 python3；顶部 281 tasks 属于全机进程总数。结合用户此前更新输出，只能确认 PID 504266 为 Gateway、504294 为 adapter，其他进程职责及 CPU 原因未经现场命令证实。内存约 1.6 GiB available、swap 未使用，不据截图认定 OOM 或重复 Gateway。新增隔离验收采样自有 TUI/Gateway 的空闲/工作/停止元数据，禁止按 python 名称杀全机进程；不能将测试机结果外推为现网六个 PID 已诊断。

## 追加缺陷 A：末轮非空文本被当作定时回执（只读定位，未修复）

用户反馈 `.20261013.4` 一次定时巡检有 25 次调用、0 重试，末轮却产生无关的分类 JSON，并沿 `delivery=origin` 投递为 `sent`。本次未读取生产正文或根请求日志；**现场文本来自主模型偏题、辅助响应串用、提示上下文或其它来源，尚未确认**。已验的 14 项源码与 `.20261013.5` 候选包不代表 A 已解决；候选包当时尚未发布，后续 B 修复与 `.6` 发布另行验收。

### 当前机制与合成证据

- `agent_core/tool_loop/response_decision.py::_no_tool_calls_decision` 核对协议、取消、截断、未知副作用和空正文；正常非空末轮可以直接 `break`，没有任务语义匹配审查。
- `conversation/channels.py::project_user_reply` 净化明确内部/工具协议，普通 JSON 可合法作为用户回复，保留原文是当前合同。
- `conversation/background_execution.py::collect_background_execution_result` 将最终正文投影为字符串；当前公开交付未携带该文本对应的模型调用用途与回复阶段绑定。
- `conversation/background_delivery.py::record_background_response` 核对真实 owner/来源绑定、投递资格、任务终态以及正文/附件，未验证正文是否是当前定时运行的公开回执。`done` 是执行生命周期，`sent` 是运输回执，均不等于业务内容已验证。

使用临时目录、原正式调度器/Store 与模型、运输替身复现，未调用真实模型或渠道，未修改产品代码：

| 合成检查 | 观察结果 |
| --- | --- |
| 一份非空合成 JSON，ResponseDecision 参数带 25 个已执行工具标记（不是 25 次真实工具执行） | `break`，正文不变，空正文修复 0 次 |
| 相同正文进入用户投影及现有 natural_user_reply 形态检查 | `plain_text`，原样保留并接受 |
| 原 scheduler + Store，`delivery=origin` | 外发 1 次，历史 `done/sent`，唤醒确认 |
| 相同正文，`delivery=local` | 外发 0 次，历史 `done/local_only`，监控正常结算 |

这些结果复现了“非空末轮直接成为 origin 回执”的机制，**没有复现或确认现场模型为何偏题**。不能根据 JSON 外形、分类字段、关键词或占位符推断来源，不能把合法 JSON 一律拦下。

### 现有正式控制与未实施方案

可明确执行 `/schedule notify <job_id> local`，也可选 `none`：监控继续运行并保留内部执行结果，后续运行不把原始收尾正文逐轮外发。这是已有正式通知控制，**不是语义校验或偏题修复**。已预留/在跑的 run 保持原冻结通知策略，不无依据修改所有 `origin` 任务。新 IM 任务默认 `local` 已实现；非空内部结果可成功静默。供应商空回复仍走已有有界修复，不等于运行时已收到“本回合无需公开回执”的声明。

最小通用方向尚未实施：先复用原调用账与交付信封，保留最终回复的准确 `call_id/purpose/run_id/task_id/thread_id` 和宿主回复阶段，拒绝将其它用途或其它 run 的响应冒充公开回执；再分离内部执行结果与公开回执，优先复用 `queue_natural_user_reply` 的结构化事实包、零工具表达轮及消息工具真实回执的去重机制。来源绑定能阻止响应串用，不能保证主模型自身不偏题。

`require_user_facing_reply` 当前并不存在，不能当作已支持参数；内部结果与公开回执分离、明确无公开回执状态均是待设计能力。现有 `delivery_contract_doctor` 校验合同结构，`decision_delivery_quality` 提供验证焦点建议，`natural_user_reply` 检查响应形态，它们都不是现成的任务语义审查器。内容质量若增加复核，应采用有界提示/返工/弃权，不新造业务质量硬门，不依据自然语言裁决授权或完成。

建议下一步：先用不含正文的末轮调用来源与阶段元数据区分响应串用和主模型偏题，再决定通用修复；回归须同时覆盖合法 JSON、跨 run/错误用途回复、内部静默成功、公开回执缺失不误报，以及冻结重投不重跑业务。A 的定位与方案不得计入 `.5` 已修复清单。

## B 和本轮发布源码收口

B 复用原 120 秒配置与同消息窗口，补实际模型长等待，并修复独立审查发现的展示阻塞取消风险。最终 33 focused 文件 602 passed、0 failed、0 skipped；原14项1250通过与Linux切片的数字各自保留、不重复累加。Ruff、doc sync、strict code-size、diff、clean-package 均通过；匿名真实锁与停止/超时的独立故障注入确认 provider cancel 先行、关闭后的旧事件不补写。新交付候选为 `.20261013.6`，从严格 gate 的准确源码提交构建，包内清单绑定 commit/source SHA；实际整包验证事实随包保存。

A 的偏题源头和公开回执分离仍未完成，不能据 B 通过或 main 发布宣布 A 已修复。真实飞书云权限/运输、现场长历史与 Windows SSH 客户端没有本轮完整验收；测试机 Ubuntu24.04 的轮恢复、原生TUI/官方MiniMax及候选更新各按真实范围记录。
