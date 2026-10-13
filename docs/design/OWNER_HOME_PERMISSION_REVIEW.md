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

### 6.1 自动委派后台链路与主子审批（R06/R07，待实施）

基于原 [IM 自动委派策略](IM_TASK_EXECUTION_POLICY.md#4-当天自动委派) 补齐接线与验证，不新增执行模式、权限开关或另一套任务系统：

1. **后台切换保留正确身份和工作范围。** IM 接单后，来源用户、owner、执行 thread/task/run、主 claim 与孩子 attempt 都沿原结构化引用传递。后台主代理与孩子在各自已授权的业务目录中正常读写；换 thread、排队后启动、唤醒或恢复不能丢失写根、误套宿主保护，或把普通用户变成管理员。后台主代理汇总时读取孩子产物也沿原获准 refs/路径。
2. **主代理能处理孩子的能力申请。** 使用原 capability request 和父级裁决工具，按准确 parent/root 唤醒后台主代理处理；前台接单回合已结束、用户正聊别的任务或没有 TUI 在线，都不能导致申请无人接收。父级只在自己可授予的上界内决定，批准/拒绝能回到原孩子，重复通知不反复创建或执行。
3. **需要用户决定的工具审批送达 IM。** 主代理自身和孩子都复用原具体工具审批账，沿已有 IM 控制/卡片和可信绑定提供入口。用户能辨认哪个任务、哪个执行者、什么操作需要决定；回调复核原用户/owner、请求及当前运行轮，不能让同群其他人或模型代批。父级授予工具能力不等于用户批准具体调用。
4. **只有 IM 也能完成等待与恢复。** 原审批桥主要对接 TUI/Web consumer，必须核对后台 main claim、当前选中任务限制和 consumer 租约的接缝，再扩展原入口；不能只补一张通知卡。等待一项审批不堵住来源聊天和其它独立任务。未送达、未解锁、拒绝、取消或失效分别沿原状态/控制结果处理，不静默丢请求、不显示已批准却永久等待；真正的用户批准仍需通过原身份与个人锁门，已有合法告警出站不受影响。
5. **模式和生命周期不混淆。** 非 full-access 按有效 ask/auto 等策略执行；合法 full-access 沿原实际规则减少询问，不额外人为增加普通文件操作审批。`/delegate disable` 和零点重置只改变新任务，已受理工作仍沿原任务和权限链执行；撤权、取消、main claim/child attempt 换代及 Gateway 恢复继续重新校验，旧批准不能跨到另一轮。已发生副作用沿原操作账去重，不能为续跑重新派一份任务。

这里的“主代理审批”既包含主代理对孩子能力申请的裁决，也包含主代理自身向用户申请具体操作批准；实施与验收必须分别记录，不能用其中一条通过代替另一条。

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
