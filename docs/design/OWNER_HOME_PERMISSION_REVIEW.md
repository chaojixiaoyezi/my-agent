## 2026-10-19 管理员在线自诊断（587候选，真实服务另验）

管理员排查自身Gateway应先调用原 `gateway_status`，无需反复下载或用文件工具打开受保护的原始队列日志。当前生命周期的64KiB尾读保留，新增严格 `gateway_native_diagnostic.v1` 投影，最多返回最近8条原native阶段、操作、固定错误码及有限资源分类；记录不含正文、路径、owner、原ID或指纹。未知schema／字段／动态值和超过4KiB的行不公开，未知仍为未知，不能据未收录诊断断言无故障。`metadata_only`仍不读日志。

原producer只为exact资源异常增加 `resource_missing` 与 `owner_home`、原 `_owner_directories` 固定26项对应的 `owner_directory_0`至`owner_directory_25`机器事实。其它资源名、路径、异常正文与子类不借此公开；原ACK、重试、停止、执行及权限不变。此观察能区分native阶段与owner目录缺失，不能把全局同时间错误归给某个用户或证明C未执行。具体请求归属继续用 `audit_records` 原scope及显式跨owner审计许可，不新增root模式、权限表或第二日志。

原 `owner_logs_dir` 自身日志可经文件和Shell原门读取；规范Gateway日志位于受保护的 `workspace/runtime/services`，可能含多owner正文，Full Access也不开放该整目录。`gateway_status`仍只注册给原local/main管理员主代理，普通/群无该工具；管理员非full保原owner墙、宿主凭据和托管存储保护。工具原deferred目录补明确日志/native关键词，并验证原 `tool_search` 的名称及中文日志查询能发现它。合成临时Home、真实主RunParams/claim与ToolExecutor回归不是用户现场原因确证，也不代表线上真实服务验收。

# 主子代理家目录权限与审批评估

日期：2026-10-12。状态：评估及隔离探针完成，未修改产品代码，待实施。
源码基线：已交付 `.20261012.8` 对应的 `08fd572f6`。

## 1. 范围与结论

用户最新明确 R06 同时覆盖三类：普通用户、管理员非 full-access、管理员 full-access；每一类都验证主代理、子代理及孙代理在自身 my-agent 家目录内的获准工作区。父 full-access 的孩子误拦是其中一个重点，普通用户和管理员非 full-access 同为必修必验范围，不能只作为附带回归。修误拦时保持原用户隔离和保护边界。

用户进一步强调的核心场景是 `/delegate enable` 后的后台任务：从 IM 接单、后台主代理派发子/孙代理，到父级处理能力申请、汇总并回送，权限必须全程正确，不能依赖前台 TUI 恰好在线。R07 同时覆盖主代理给孩子作能力裁决，以及主代理自身或孩子调用工具时需要用户决定的审批；两者不能混为一次授权。本文下方补充的是待实施验收，尚未证明现有 IM 已有完整审批入口。

已经复现实际不一致：在宿主明确授权给子代理的自身工作目录中，`write_file` 可以写入，`run_command` 对同一文件写入却被真实 macOS Seatbelt 拒绝；父代理为 workspace-write 或 full-access 都能出现。不是只凭“子代理不继承 full-access”推断问题。

另确认普通 `tasks/` 工作目录会被范围合并与祖先保护一起误拦；并发现反方向的缺口：`locked_files` 在文件写门有效，但未进入 Shell 写保护投影。审批正常路径回归通过，换代缓存有条件复现缺口，尚不能解释现场所有偶发审批异常。

本轮只使用临时 owner、合成请求及测试文件。未读取现场聊天正文、业务文件、真实凭据或审批正文，没有调用真实模型、发送飞书、修改在线服务或生成更新包。真实 Shell 探针在本机 macOS，Linux 的同类代码顺序已查，新的 Linux 真机复测尚未做。

## 2. 先把“家目录”和权限含义分清

- 本文的 owner home 是 my-agent 为当前身份解析出的目录，例如安装根 `data/owners/local/main` 或普通用户自己的 owner 目录。安装根、整个 data 根和操作系统的 `$HOME` 都不是同一个目录。
- `user_space/approval_mode.py::permission_config` 与 `subagents/effective_permissions.py::child_shell_access_mode` 明确保留孩子的 workspace-write 上界；本机管理员父代理 full-access 不代表孩子获得全盘权限。
- 这个限制不应妨碍孩子在已经授权的自身工作目录写正常工作文件，也不应造成文件工具和 Shell 对同一路径意见相反。
- 普通用户的配置文字不能授予 full-access；跨 owner、宿主控制状态、运行中安装目录继续受保护。管理员 full-access 也不能绕过宿主控制文件保护或操作系统 UID/ACL，只是 my-agent 权限模式。
- 文件范围、工具可见性、具体调用审批和 OS 权限是不同检查层。`auto/full-access` 免除一部分逐次询问，不会自动修复已编译错误的文件范围。

## 3. 本轮复现结果

| 场景 | 实测结果 | 结论边界 |
| --- | --- | --- |
| 父 workspace-write，孩子自身 `runs/.../work/agents/<child>/`，同一文件先文件工具再 Shell 写 | 文件工具成功，Shell `COMMAND_FAILED` / `Operation not permitted`，原内容未变 | 真实 ToolExecutor、WriteFileTool、ShellTool、Seatbelt；身份使用隔离夹具 |
| 父 full-access，同一个孩子目录和操作 | 同样文件工具成功、Shell 被拒 | 正中用户强调的父 full-access＋孩子家目录场景 |
| 正常本机主代理 workspace-write，已声明自身 `tasks/.../work` 写根 | `WRITE_FORBIDDEN` | 使用真实 HomePaths 和生产范围合并函数 |
| 本机主代理 full-access，同一个普通 tasks 路径 | 成功 | 本轮没有复现“主代理真正 full-access 下任何普通家目录文件都失败” |
| 父 full-access，孩子在已声明的普通 tasks 路径 | `WRITE_FORBIDDEN` | 家目录内较窄任务写根在范围合并后丢失，见下节 |
| 普通 owner，主/子写自己 `notes/`、自己 `tasks/`、另一个 owner | notes 成功；tasks 被拒；跨 owner `PATH_CROSS_OWNER_BLOCKED` | 普通用户没有全盘放行；同时存在家目录业务路径误拦 |
| 自己家内被 `locked_files` 锁定的同一文件 | 文件工具拒绝且 handler 未执行，Shell 成功改写 | 确认同 owner 文件锁的执行不一致，不等于已证明跨用户越权 |
| 子代理 approved_session 后，合成换 attempt 并保留同一 sink | 第二次返回 approved，无新 pending | 条件性缓存缺口；没有执行具体工具，不能算现场已发生越权 |

以上都是现有代码的结果，不是修复后的验收成绩。

## 4. 已确认的三处路径问题

### P1：子代理自身窄授权被 Shell 的宽只读覆盖吞掉

生产路径如下：

1. `subagents/services/runner_context_service.py::_build_write_boundary` 把宿主自己的工作目录放进 allowed roots。
2. `agent_core/tool_runtime_ledger.py::_retained_child_work_roots` 保留这个与宿主身份、task_dir 相符的窄根；同文件的 `_attach_owner_control_write_guards` 同时保护祖先 `runs/`、`tasks/`。
3. `tooling/write_boundary.py::_forbidden_root_blocks_target` 按路径具体程度比较：更窄的正式工作根可以穿过祖先保护，相同或更窄的禁止仍生效。因此文件工具成功。
4. `tooling/registry_invoke.py::_tool_params_with_runtime_boundary` 却把全部 forbidden roots 直接传成 `__sandbox_protected_write_paths`。
5. `attempt/sandbox.py::AttemptExecutionSandbox._macos_profile` 在允许写根之后追加祖先 deny；`tooling/sandbox.py` 的 Linux 挂载构造也在可写根之后追加只读路径，没有保留上述路径优先级的同一语义。Linux 实际挂载效果仍须真机对照，不能仅由构造顺序代替实测。

此前 `test_child_own_workspace_write.py` 主要验证文件工具真实 handler；它能证明自身窄授权保住了，不能证明 Shell 同样能写。这解释了文件回归通过但真实子代理仍反复报权限问题的可能路径。

### P1：普通任务写根在 owner 合并时丢失，随后整棵 tasks 树被拒

`agent_core/tool_runtime_ledger.py::_attach_owner_task_write_scope` 对普通 owner 模式把范围整理成 owner home、子代理特定自身窄根和墙外授权根。普通的家目录内任务写根没有全部保留下来；随后 `_attach_owner_control_write_guards` 将 `owner_runs_dir/owner_tasks_dir` 整体加入禁止根。

结果是允许根只剩宽 owner home，而禁止根 `owner/tasks` 更具体，正常工作文件就被误拒。不是 Unix chmod 能解决的权限问题。

现有 `test_owner_home_workspace.py::_home_case` 只构造了 `owner_home_dir/owner_runs_dir`，缺少真实 HomePaths 的 `owner_tasks_dir`。因此“旧 tasks 文件可编辑”的测试未经过这一祖先保护条件。后续回归应使用完整 HomePaths，而不能删除生产保护来迎合简化夹具。

### P1：文件锁没有完整传到 Shell

`tooling/write_boundary.py::_locked_boundary_error` 对文件工具明确拒绝 `locked_files`；`registry_invoke.py::_tool_params_with_runtime_boundary` 的进程保护集合包含 forbidden roots、运行中安装根、任务宿主记录，却漏了 locked files。

隔离实测同一 locked 文件：`write_file` 零 handler 拒绝，Shell 成功修改。需要把锁和其它绝对保护落实到所有写入口，包括重定向、脚本、PTY 和后台进程；不能靠检查命令正文是否包含某个文件名。

## 5. 审批现状与尚未确认的部分

- 正常主、子、孙审批已有精确 run/root/thread、主 claim、子 execution_attempt_id 校验。父会话投影曾把孩子当主任务的旧问题，当前 `conversation/tool_approval_scope.py::tool_approval_scope` 已优先查 canonical child；本轮相关批准/拒绝回归通过，不能再把旧问题当作当前唯一原因。
- 两类授权要分别诊断：能力申请回答工具/路径是否可用；具体审批回答这次调用是否可执行。批准具体调用不能扩大目录范围；若 Shell 编译后的权限仍错误，批准后仍然可能执行失败。
- `conversation/agent_tool_approval.py::wait_for_agent_tool_approval` 使用 1.5 秒 consumer 发现等待与 15 秒租约；无有效 consumer 时返回 unavailable 并清理该条请求。TUI 切任务、未续租、消费落后、取消或换代都需要按真实身份和时序区分。仅凭“没有弹框”无法判断是拒绝、无人接收、过期还是发布前身份失配。
- `_request_scope`、坏记录、读写异常等部分失败最终合并为 unavailable，现有结果不足以直接定位现场原因。建议在原审批账/事件内补结构化原因与时间，不记录命令正文、参数中的秘密或用户任务内容，不创建第二份授权账。
- **缓存边界缺口**：`AgentToolApprovalSinkMixin.request_permission` 的会话缓存键目前是 `run_id + claim_id + tool/args_hash`，没有子代理的 `execution_attempt_id`。主代理 claim 换代能失效，孩子的 claim 为空时，同 sink 跨子 attempt 仍能命中旧批准。本轮用真实首次审批＋临时任务记录换代复现 approved、零新 pending。
- 上述缓存探针有意复用了旧 sink，换代是隔离故障注入；尚未证明生产恢复链会在这个时序复用同一对象，也没有让工具产生越权副作用。应修复合同遗漏并补旧 sink/迟到调用测试，但不能据此宣称已定位用户现场偶发审批。
- Full Access 下仍需用户确认的 always 类工具、插件当次门和未授予的长期操作键属于现行边界；应展示真实原因，不能统一当作“Full Access 失效”或自动放行。

## 6. 推荐修复方式

按用户要求优先复用与改造原 owner/path/write boundary、ToolExecutor、OS 沙箱及审批桥，先定位各层对同一事实的不同解释，再在原入口统一；不为主代理、子代理或某种用户各加独立权限表，不新建审批队列。必要的通用裁决函数由现有模块承接，并让全部写入口消费。

1. **同一次调用只编译一份有效路径策略。** 从原 owner、写边界和 canonical run/attempt 读取真实授权，文件工具、Shell、PTY 与后台进程消费同一裁决。保留 cwd、owner 地址、可读根、可写根的区别。
2. **祖先保护与绝对保护分开。** 普通祖先只读可以被宿主明确授权的更具体工作根覆盖，同路径 deny 优先；宿主状态、安装目录和精确锁等绝对保护最后生效，不能因授予子目录而穿透。不要简单删除 runs/tasks 保护或交换所有 allow/deny 顺序。
3. **保住自身业务目录和孩子工作区。** 由完整 HomePaths、当前真实任务/孩子身份确认目录归属；普通业务文件与宿主运行记录分别按原结构事实保护。不得以“路径在我家”或模型声明目录就开放另一个孩子的状态、策略文件或控制数据库。
4. **审批绑定原执行轮。** pending、缓存和批准后重执行都包含 child attempt/main claim；消费者迟到、决定写回失败、取消和恢复有明确结果。文件范围不足与审批未满足分别报告，不让模型靠重复申请或换工具绕过。
5. **展示和诊断读实际裁决。** `/status` 或诊断结果可展示父模式、子有效模式、当前 cwd、owner root、实际写根以及最后拒绝层。来源是结构化执行事实，不只展示管理员标签或配置中的 full-access。
6. **统一子进程家目录说明。** `_subprocess_text_env` 当前只在宿主 HOME 落入 hidden_roots 时改写 HOME；不能默认把 `~` 与 my-agent owner home 等同。应核对普通工具缓存/依赖安装路径，但本轮未证明这是上述同文件失败的原因。

参考已核对本地 Codex `codex-rs/protocol/src/permissions.rs::resolved_entry_precedence` 及 `resolve_access_with_cwd_uses_most_specific_entry` 测试：路径具体程度与同层冲突优先级属于策略本身，不能让 UI、文件判断和 OS 转换各自解释。审批桥既有参考及合同见 [主子审批桥](SUBAGENT_TOOL_APPROVAL_BRIDGE.md)。只借鉴通用边界，不更换现有 owner/任务账。

<a id="background-delegation-permissions-review"></a>

### 6.1 自动委派后台链路与主子审批（R06/R07，历史目标；接线状态见下方勘误）

基于原 [IM 自动委派策略](IM_TASK_EXECUTION_POLICY.md#4-当天自动委派) 补齐接线与验证，不新增执行模式、权限开关或另一套任务系统：

1. **后台切换保留正确身份和工作范围。** IM 接单后，来源用户、owner、执行 thread/task/run、主 claim 与孩子 attempt 都沿原结构化引用传递。后台主代理与孩子在各自已授权的业务目录中正常读写；换 thread、排队后启动、唤醒或恢复不能丢失写根、误套宿主保护，或把普通用户变成管理员。后台主代理汇总时读取孩子产物也沿原获准 refs/路径。
2. **主代理能处理孩子的能力申请。** 使用原 capability request 和父级裁决工具，按准确 parent/root 唤醒后台主代理处理；前台接单回合已结束、用户正聊别的任务或没有 TUI 在线，都不能导致申请无人接收。父级只在自己可授予的上界内决定，批准/拒绝能回到原孩子，重复通知不反复创建或执行。
3. **需要用户决定的工具审批送达 IM。** 主代理自身和孩子都复用原具体工具审批账，沿已有 IM 控制/卡片和可信绑定提供入口。用户能辨认哪个任务、哪个执行者、什么操作需要决定；回调复核原用户/owner、请求及当前运行轮，不能让同群其他人或模型代批。父级授予工具能力不等于用户批准具体调用。
4. **只有 IM 也能完成等待与恢复。** 原审批桥主要对接 TUI/Web consumer，必须核对后台 main claim、当前选中任务限制和 consumer 租约的接缝，再扩展原入口；不能只补一张通知卡。等待一项审批不堵住来源聊天和其它独立任务。未送达、未解锁、拒绝、取消或失效分别沿原状态/控制结果处理，不静默丢请求、不显示已批准却永久等待；真正的用户批准仍需通过原身份与个人锁门，已有合法告警出站不受影响。
5. **模式和生命周期不混淆。** 非 full-access 按有效 ask/auto 等策略执行；合法 full-access 沿原实际规则减少询问，不额外人为增加普通文件操作审批。`/delegate disable` 和零点重置只改变新任务，已受理工作仍沿原任务和权限链执行；撤权、取消、main claim/child attempt 换代及 Gateway 恢复继续重新校验，旧批准不能跨到另一轮。已发生副作用沿原操作账去重，不能为续跑重新派一份任务。

这里的“主代理审批”既包含主代理对孩子能力申请的裁决，也包含主代理自身向用户申请具体操作批准；实施与验收必须分别记录，不能用其中一条通过代替另一条。

2026-10-19 状态勘误：上述“待实施”是历史目标，不代表当前缺少IM工具审批入口。当前 `conversation/im_tool_approval.py` 已接原私人卡发布、`im_approval_wait_live` 无TUI等待和原scope决定；`agent_tool_approval.py` 在原pending发布后接入该入口，`gateway_parts/private_interaction_transaction.py` 将正Count批准／拒绝交原 `private_approval_control`，取消仍沿原退休门。`test_im_tool_approval.py` 覆盖无TUI原waiter、子孙当前attempt、错误actor／旧claim及真实HTTP回原等待；这些仍是临时组件与替身运输证据。最终新源真实IM的送达、个人锁、用户决定、原执行恢复和主声道交付仍须分别验收，不能把capability grant当成human tool approval，也不能把现有入口宣称为灵活命令参数授权已经完成。

## 7. 验证记录及实施验收

本轮现有定向测试：

- 家目录、孩子自身工作区、第二层工作根、本机/远程 owner、后台主审批七文件共 98 项通过。
- Gateway 孩子/孙审批、父会话投影、用户模式、插件消费与继承模式四文件按审批相关关键词筛选，53 项通过。
- 额外临时探针经过实际 ToolExecutor 和本机 OS 沙箱，确认上文路径三处问题；只读复核/合成审批换代另记，未增加永久测试或修改生产代码。
- 现有测试通过不构成新问题已解决，新增对照正好说明现有覆盖缺口。没有以此替代真实 Ubuntu/TUI/飞书验收。

后续必须覆盖的矩阵：

| 维度 | 必测项 |
| --- | --- |
| 身份与模式 | 普通用户的现有合法模式；管理员非 full-access（现有 ask/auto 等）；管理员 full-access，三类分别必验；普通用户伪造 full 必须拒绝 |
| 有效文件范围 | 审批模式与文件范围分别取真实配置；覆盖现有 workspace-write 等受限范围及合法 full-access，不能只按管理员标签或免审批推断可写范围 |
| 执行者 | 三类身份模式分别覆盖主代理、直接孩子、孙代理；各自在自身授权目录可正常工作，父 full-access 不取消子代理的原隔离 |
| 执行链路 | 前台直接执行与 `/delegate enable` 的 IM 接单、后台主代理派子/孙、父级汇总和回送分别验证；既有定时后台任务沿同一权限链回归，不只测试裸子代理夹具 |
| 审批来源与入口 | 后台主代理自身具体工具审批、孩子能力申请由父级裁决、主/子具体调用交用户决定分别验证；IM 在线但 TUI 从未启动或已退出，批准后仍能继续原执行 |
| 路径 | owner 普通业务目录、tasks、孩子自己的 run work；其它 owner、兄弟工作区、宿主状态与安装根 |
| 写入口 | write/edit/apply_patch、Shell 重定向/Python open、PTY、后台命令；同文件同预期 |
| 优先级 | 宽允许＋窄拒绝、宽祖先只读＋窄正式授权、同路径冲突、锁文件、软链接/硬链接与不存在的目标 |
| 生命周期 | 首次/连续工具调用、并发孩子、审批等待/拒绝/取消、切任务、consumer 失联、同 run 换 attempt、旧决定迟到 |
| 后台连续性 | 多个已委派任务同时有申请、用户同时正常聊天；disable/跨零点不丢已有任务，Gateway 恢复按原状态核验；重复/错用户/错任务决定不生效，副作用和交付不重复 |
| 平台 | Linux bubblewrap 真机与 macOS Seatbelt；普通 OS 用户执行，不用 root 权限掩盖问题 |

先用准确生产边界与完整 HomePaths 写红测，修统一裁决，再跑上述组合。自动委派集成必须从原入站与派发入口启动，经过真实 ToolExecutor、父级通知和审批桥，不能仅手工组装孩子后验一个文件 handler。

最终在隔离 Linux 安装中用官方 MiniMax-M2.7 分别验证真实 TUI 和真实 IM：IM 场景保持 TUI 未启动，开启 `/delegate enable`，普通用户、管理员非 full-access、管理员 full-access 各给一次普通任务，观察后台主/子工具账、审批流、产物及回送；测试者只作正常用户控制和审批，不代替模型补文件，不改线上现有任务。IM 平台未接通时可完成替身/replay，但必须把真实 IM 验收留为未完成。

建议下一步：优先贯通自动委派的后台主子权限与审批，先修自身工作区和 Shell 一致性并补齐锁保护，再沿原父级通知、审批桥和 IM 控制验证无 TUI 的完整链路。安装更新与 IM 帮助展示可以独立并行；路径编译、运行边界合并和审批执行者各保持单一修改负责人。当前等待实施指令，不合并、不推送、不打包。

## 2026-10-18 管理员 Full Access 的普通 MCP 确认修复候选

本节实现状态与下述新增授权粒度设计分开验收，普通 MCP 修复不代表命令范围和父级及时续跑均已完成。

解决问题：用户已在正式权限入口选择管理员 Full Access，普通 MCP 的宿主 `tool_approvals: always` 仍逐次询问。原审批读口会将 full-access 压成 auto，统一工具门又保留 always，因此不能仅改展示或关闭 IM 发卡。

候选在每次 registry 调用中经独立只读 `permission_state_reader` 读取原 owner 权限状态（原 `approval_mode_reader` 字符串协议不变；新读口坏型/失败不得回退放行），并与当前有效 owner 墙、原装配 access/path 模式及主代理类型复核；只有已核管理员、当前显式 full-access、空有效墙与 full 路径同时满足，才免普通 MCPProxyTool 的逐次 always。该布尔只为当次宿主投影，默认 False，不写第二份权限或恢复状态。插件继承代理、其它 always、普通/群、非 full 管理员及受限子代理保留原确认；参数、可见性、owner/path、执行权威及插件 deny 仍走原门。

本片只处理新调用；旧 pending 缺本次有效 child/main 边界，不按父 agent.full 猜权或清票据。父 capability_request 的正式状态为 OPEN/GRANTED/GAP/CLOSED，与 human tool approval 的 APPROVED 分属原有协议。日志权限仅定位源码，未读取真实日志或改挂载。未发布、未部署、未连接真实 MCP/飞书，实际运行链由根后续统一验收。

参考先查本机 Codex 合同索引 `codex_contract_code_files.xlsx` sheet2 行 41/43/49 的权限模块，再沿源码 `codex-rs/core/src/tools/orchestrator.rs:142-168` 观察原 permission profile 与单一审批执行门。这里只复用已解析权限与执行门分离的模式，不引入其权限框架。

## 管理员在线诊断入口（审计中）

不新增root模式，优先让现有管理员full-access能读取本机服务日志、进程和资源状况，沿原受控审计查询其它owner任务、调度与权限失败。普通/群不继承管理员资格；操作系统账号权限仍适用，诊断权限不隐式变成sudo。

当前`audit_records`已有`topic=requests`与`scope=all_owners`，只读请求状态、错误码、耗时和归属，不读对话或记忆正文；原`admin_controls`可显式管理`cross_owner_audit_allowed`，默认仍关闭。该工具只允许主代理使用。是否应在full模式下减少额外诊断前置、是否缺具体owner筛选与运行/调度诊断投影仍在审计；不能用解除私密存储整目录保护代替诊断入口。自身日志的离线权限回归与线上真实错误分别记录，尚未据此判定用户现场原因。

## 子代理授权粒度与及时续跑（设计核对中）

目录授权允许子代理在父代理现有权限上界内，对指定目录读写、创建文件并正常运行脚本，不反复申请相同目录。它不扩大到目录外、其它owner或宿主凭据，不绕过工具禁用和原网络限制。命令授权绑定目录与获准命令，不能因为命令需要一个工作目录就附赠该目录的任意写工具。

参数变化按是否扩大已批准范围判断，不按字符串是否逐字相同判断。更换查询词、时间窗及已授权目录内输出名等无扩权变化应继续；新命令、新工具、新目录、额外执行动作或越出父权限上界应重新裁决。尚未证明现有仅按可执行程序名的allowlist可以表达此合同；实施前需复用已有命令解析与结构化范围，不能靠自然语言说明或黑白关键词猜测授权。

本次任务内已有有效grant必须持久沿用，申请措辞变化或接收对象重建不能制造重复确认。父级申请通知、决策与批准后续跑必须沿原事件及canonical状态，不能依赖主代理在摘要里记得处理；仍需核对真实执行延迟来源。提示缓存是性能目标，不能作为授权事实。当前为设计与差距核对，未增加配置、协议状态或新的权限账。

## 2026-10-18 会话批准跨展示 sink 复用候选

解决问题：孩子同一canonical run/current attempt内，普通MCP的同工具同参数已选“本会话允许”，重建BackgroundTranscriptSink仍再次发卡。旧许可仅存sink私有set；本片复用既有Agent级ToolApprovalSessionCache，不新建权限表、期限或队列。

缓存scope只由当前原owner/thread/run/claim/attempt、真实registry、原owner模式和canonical执行上下文中的完整有效工具/Skill/读写边界生成。孩子须核同owner、线程及当前attempt；主车道必须有宿主typed RunParams与同owner/thread/task/run/currentattempt的原RuntimeDB链；前台继续核原writer/队列/claim，后台同时核生产LocalRunControl内存绑定与原claim发布投影一致。JSON单独不能签权，缺运行参数或原控制来源不缓存。原write_boundary完整投影及core运行代次共同进入scope。缺字段、读取失败或漂移只关闭缓存，不回退成共享宽scope。键仍为准确工具与参数哈希；只记录用户approved_session，单次批准、拒绝、取消和插件强制门均不提升。等待后再次核scope才记缓存，旧attempt/新权限不得继承。

本片不解释参数变化是否扩权，不把MCP always免审当成已处理重复批准；不用模型文字签权。正式目录/命令两类授权与字段漏失属于另一切片。全盘诊断及跨owner安全投影继续独立审计，未新建root模式。

已以真实临时canonical child/currentattempt及原MCP工具授权复现旧版2次publish→新版1次，另覆盖新attempt、权限改变、过期请求、取消、单次批准、拒绝、插件与失源。独审发现初版main缺参仍产生缓存键，原失败及冻结件保留；修正版新增真实后台claim的缺参、鸭子参数、错task/run/owner、旧attempt、无原control、伪control、未发布/已关闭control及claim投影漂移反例，并保原publisher和前台writer正例。只替换用户审批运输等待，无真实MCP/飞书网络；准确结果见501后继handoff，尚未发布/部署。

## 2026-10-18 能力申请目录上界与既有授权复用候选

旧底座两处已用真实临时SimpleAgent复现：父admin full-access已明确项目工作根，孩子也已持久化该根，能力分区仍只认owner根而拒绝；另一处同task、同工具和绝对目录已经真实GRANTED，仅needed措辞变化仍新建OPEN。

四生产候选只复用既有路径过滤与canonical grant：目录分区允许owner自身及本task已声明、经通用继承过滤的根，最终resolver仍核父当前工作区和工具上界，宽manager workspace不是权限。已GRANTED必须同request/run实际grant尚有效且task未终态、工具/Skill未撤，路径/cwd在真实grant子树且无符号链接逃逸，命令、网络、预算、限制均有实际字段覆盖。请求描述、needed、risk文案不签权；A内输出名变化可在原工具权限内复用，A外不覆盖。

非空命令须真实grant保有完整request_scope.requested_commands及原exe门；缺任一字段不借旧申请文字补权。这里仅保守比较完整命令，不解释参数变化是否扩权。新目录/命令两类灵活授权的完整合同属于后继设计；命令许可不能顺带变任意目录写权。现resolver `_mark_grant`因目录补全写工具、`_persist_grants`丢command/network/budget/constraints/request_scope已交独立P0片；本候选不忽略缺字段来制造通过。

只匹配并返回既有GRANTED，不新增grant、授权状态、队列或配置；lifecycle与结构化结果只消费同canonical规则。过期、撤销、无真实grant即使完全同signature也要重新OPEN。父通知、耐久wake、GRANTED后实际模型/工具继续及失败终态未在此片改动；主代理不能靠自然语言声称“已批准”替代原状态。普通/群、foreign私密、宿主凭据、网络及工具禁用边界保持，真实日志/服务/配置零读写。

本片明确保留原OPEN去重协议：原signature按exe名归一命令，故OPEN中的参数变化可能合入旧pending；本轮不引入新命令执行合同。已GRANTED分支才执行本片完整实际grant覆盖。试收紧OPEN曾触发原工具/parsed同申请去重回归失败，原失败留档后按scope撤回；不能宣称两类授权UX或参数智能授权已实现。

独审边界修正：授权时已冻结的绝对canonical grant根，其当前resolve必须仍等于原持久路径，根或祖先被换成symlink即拒绝旧覆盖，不保存inode或改绑旧许可。墙外目录的声明root与每个target都复用原继承过滤，避免合法宽工作根恰包住agent home时穿透foreign/config；原owner内分区保持。初候选及三条独审失败保留，不能把原聚焦通过等同完整安全验收。

有效工具不能只读task.allowed_tools声明：GRANTED覆盖复用原_runner_allowed_tools纯投影，canonical effective_permissions.disabled_tools撤销普通/MCP工具时拒旧许可。未改变runner执行或新增工具门，enabled正例保留。
