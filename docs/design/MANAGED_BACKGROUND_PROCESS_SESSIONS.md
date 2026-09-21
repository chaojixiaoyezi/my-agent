# 受管后台进程会话

状态：安装版仍为已发布 v1。源码已完成 v2 启动预留、明确交接、权威查询和单 session 清理，尚未发布；整任务停止接线与实际复验仍待完成。
历史后台 session 主链和 `network_status` 只读投影已部署 `.7` 唯一 Gateway；fresh r53 验收了跨 runner
存活/停止，fresh r10 验收了稳定 session handle、真实 listener PID 和外部探针边界，fresh r27 验收了
启动观察期内的立即退出不会再被报告成“已启动”。

## 解决问题

`run_command(run_in_background=true)` 原先把 bwrap 直接创建在当前 agent runner 下。Linux
沙箱保留了正确的 `--die-with-parent` 安全参数，但子代理 runner 是一次性进程：child 自然完成时，
bwrap 把自己和 HTTP 服务一同退出。界面和最终回复仍拿着启动瞬间的 PID，因而会把已经死亡的服务报告成
“正在运行”。

这不是植物大战僵尸、HTTP 服务或子代理专项问题。底层缺的是后台命令的稳定会话 owner：后台命令必须
比一次模型工作片活得久，同时仍能被所属用户会话查询、等待和停止。

## 对照依据

- 会话运行时 `会话运行时-rs/core/src/session/session.rs` 把 `UnifiedExecProcessManager` 放在 Session 上；
  `session/handlers.rs` 只在整个 Session shutdown 时统一终止。即模型 turn 或 child 工作片结束，
  exec session 仍由长期 Session 服务持有。
- 终端交互 `src/tasks/LocalShellTask/killShellTasks.ts` 和
  `src/tools/AgentTool/runAgent.ts` 采用另一种明确语义：worker 结束时主动清理它拥有的 shell task。
- 本项目已有 `process_session(list/status/wait/stop)`，而用户要求服务在 child 完成后继续供主代理与用户
  使用，因此选 会话运行时 的 Session 所有权语义；不采用“child 一结束就杀服务”的 终端交互 语义。

## 源码唯一主链（v2，未发布）

```text
run_command(run_in_background=true)
  -> exact approval / capability / path / sandbox gates
  -> ShellTool 构造已经校验的 argv 并冻结访问/执行身份
  -> 原 operation 的只读权限复查 + Store 启动预留
  -> detached managed host 在同一 Store 绑定自己
       -> 创建标记先提交，锁内 Popen 并绑定精确 child 实例
       -> bwrap --die-with-parent --new-session / 原平台 argv
  -> 有界启动观察（0.5 秒）与最后权限/取消复查
       -> 仍运行或已自然退出：launcher 确认同一 session 交接
       -> 失败/取消：冻结停止意图，只清理原实例；未确认保持 unknown
  -> process_session list/status/wait/network_status/stop
  -> host 保存真实业务终态；只有未被停止的自然 exited 发送原完成通知
```

关键点：不删除 `bwrap --die-with-parent`。以前它指向短命 agent runner；现在它指向专门的 managed host。
这样 runner 自然退出不会误杀服务，而 host 被停止或异常退出时，bwrap 仍会收掉沙箱内全部后代。

## 组件职责

### `background_process_launch.py`

- 冻结原访问范围、执行归属和 launcher 出生标识；记录进入同一个受保护 Store 后才创建 host。
- 启动 spec 只含 argv、session 和日志上限，放在 Store 内的私有一次性交接目录；环境只传给进程，不写磁盘。
- 准入复查复用原 operation 的 task/run/attempt 权限，不再审批、扣预算或重新 claim。
- 有界观察后，在同一个 Store 锁内重读身份与停止事实，并在交接提交前最后检查取消。
- 交接成功后旧回合 token 不再拥有该后台程序；持久异常恢复同一句柄，不重复 Popen。

### `background_process_host.py`

- 每条显式后台命令由独立 host 持有，child 也建立独立系统进程组；保留原沙箱安全参数。
- 只执行 ShellTool 已验证的 argv，`shell=False`；host 与 child 在原预留记录上各绑定一次。
- child 创建标记在系统调用前提交，创建与绑定共用目录锁；进入创建却未能绑定时只能保留未知。
- 在首次 poll/wait 之前取得 child 出生标识；自然退出也先核对并收回已观察的遗留后代，再保存真实退出码。
- 交接前持续检查 launcher 的原实例，交接后只读取停止意图和日志上限；不需要 launcher 常驻 watchdog。
- stdout/stderr 保持原日志地址；v2 不写另一份 host-state 权威。

### `process_session_store.py` 与 `process_session_cleanup.py`

- v2 沿原 session JSON 地址持久化，版本/身份验证、目录锁和可恢复提交的职责分别由窄模块承接。
- v1 是确实已经发布的数据格式，只保留原访问范围内按 session 读取/管理，不补猜执行归属或升级。
- 访问目录仍在 owner 沙箱之外，目录 `0700`、记录 `0600`；模型不能通过普通参数指定它。
- 多记录停止只提交精确冻结集合；清理入口消费给定记录，不重扫任务、恢复轮或 launcher。
- 终止原语在采集树后复核冻结出生标识，无法确认的实例和后代保留未确认回执。
- 停止意图提交后、发信号后以及终态保存后是不同事实；异常保留已提交、待恢复和实际终止回执。
  被其它事务阻塞恢复不等于本次停止已提交；只有本目标的停止记录才能证明目标提交。

### `process_registry.py`

- 缓存以规范 Store 地址和 session ID 共同索引；每次冷热查询都读取当前权威，并保留全部 v2 字段。
- 只有同目录、同 PID 和同出生标识才能沿用本进程 Popen；查询确认终态时回收该句柄，不用 host 退出码替代 child 退出码。
- host 消失而业务终态没有可靠记录时标为 unknown；starting/unknown 仍待处理，不发完成通知或冒充退出。
- 显式 stop 只清理那个 session；完整任务的权限关闭及冻结清单消费由后续控制层接线负责。
- 旧 v1 host-state 文件仅补充已发布记录的退出观测，不为 v2 提供身份、权限或终态旁路。

### `process_network_status.py`

- 只接受已经通过 owner/conversation scope 校验的 exact managed session；不扫描或控制无关进程。
- Linux `/proc` 下用 session 进程树的 socket inode 识别该树真正持有的 TCP listener，区分 loopback 与
  non-loopback；每条绑定附真实持有 socket 的 `listener_pids`，端口参数只筛选观测结果，不授予访问权。
- listener PID 是宿主内核对 exact managed tree 的权威只读观测；`run_command` 沙箱可能看不到宿主 PID，
  因此沙箱内 `ps/lsof` 无结果不能推翻这条事实。
- firewalld 可用时只执行固定的只读 `--state` / `--query-port`，报告“显式放行”“未显式放行”或“未知”；
  service rule、nftables、云安全组、路由、NAT 和上游网络未被观察时不得推断。
- R179 保留 firewall-cmd 原生退出码：只有 `NOT_RUNNING=252` 证明未运行；超时、权限错误、zone 查询失败
  保持 `unknown`。逐 zone/port 的 `port_observations` 与 `explicit_rules` 一起返回；混合放行为
  `partially_allowed`，任一查询未知则总览未知但保留已知行。字段是只读证据，不自动改规则或任务终态。
  退出码依据 [firewall-cmd 官方手册](https://firewalld.org/documentation/man-pages/firewall-cmd.html#exit_codes)。
- non-loopback listener 只证明服务接受非回环地址，不证明另一台机器能连接。结果固定要求独立外部探针，
  工具不自动开放端口、不重启服务、不发网络请求。

## 权威记录

已发布 `managed_process_session.v1` 包含以下旧字段，新源码仍能显式读取：

- `session_id`
- `pid`：managed host PID，也是 stop 的进程树根
- `child_pid`：实际 bwrap/命令根 PID，只作观测
- `pid_birth_token`
- `owner_id + conversation_id + owner_home`
- `command + cwd + output_file + host_state_file`
- `status + exit_code + started_at + finished_at`

新 v2 还明确保存 `revision`、`execution_scope`、launcher/child 出生标识、`reserved_at`、
`child_launch_started`、`handoff_confirmed` 和 `stop_requested`，不从通知或 command 推断这些事实。

模型可见启动/状态结果只提供稳定 `session_id`，不公开上述 host/child 内部 PID。模型只能提供
`session_id` 和 action；store root 与访问 scope 都由 ToolRegistry/ToolExecutor 注入，不能由模型覆盖。
错误 scope 与不存在继续统一返回 `PROCESS_NOT_FOUND`。

`network_status` 返回 `managed_process_network_status.v1`，包含精确 session、可选端口、进程树 listener、
`listener_pids`、宿主观测权威/沙箱可见性、绑定范围、主机防火墙显式规则和
`lan_reachability=unverified_external_probe_required`。只有来自另一
机器/目标网络的真实请求才可把“局域网可达”写成已验证；本工具本身永远不生成该成功结论。

## 失败和回收

### 任务资源停止缺口（待修复）

命令首片安装版的实际 TUI 137 在同一 Goal 多次工作片后执行 `/stop`，当前回合取消、Goal 暂停，
但主代理一次启动的受管后台程序仍持续采样。这与用户的资源停止语义不符；原生控制和精确 PID/出生标识
已留在私有证据中，后续自然结束不抵充明确停止。

当前后台记录的 owner/conversation 是访问边界，不足以表达精确任务的执行归属；完成通知地址也不能
成为是否允许停止的授权来源。修复须从宿主可信运行上下文冻结任务归属，保留跨工作片存活、
独立查询及自然结束行为，并让显式资源停止覆盖该任务已启动和正在启动的资源。
任务、run、attempt 以及启动期间的停止竞态须分别核对；不能扫整个会话或解析 command/输出猜归属，
也不能误伤后续明确恢复后才创建的资源。缺少可靠归属的旧记录不猜测迁移或批量终止。
确定具体接线及成熟参考后才修改实现，修复必须经过合同回归、同版部署和实际多 TUI 再验。

只读定位已确认：Gateway 的显式停止直接回收 PTY，再派发子代理取消；无子代理的主后台进程没有进入停止入口。
后台启动又先 Popen/握手再登记，所以仅扫描已登记资源仍会漏掉启动窗口。短命子代理与 Gateway 不在同一进程，
启动预留、停止冻结和迟到登记必须共享持久权威，不能只复制 PTY 的进程内字典。
启动成功交接后的回合 token 不应长期拥有独立后台进程；交接前取消仍须回收已经启动但尚未交付的资源，
日志上限保护则继续保持。direct/local 的单纯中断也须与 Gateway 一样避免进入子代理资源取消。
原 operation 成功、STABLE 及锁释放描述单次启动操作，不改成长期进程生命周期，也不清理原 UNKNOWN 历史。

参考只读核对本地 Codex `578c1b2` 的中断/后台终端清理入口，以及 Hermes `0a62610` 的停止分派、
显式任务身份和冻结进程集合；不采用全局 kill_all，不照搬参考项目的 Goal 暂停或 turn-timeout 资源语义。
这些源码不证明本项目的跨进程启动竞态已解决。确定方案后须覆盖交接前各阶段取消、跨 attempt/进程停止、
同会话不同任务隔离、停止后恢复的新资源、PID 复用和退出未确认；真实 TUI 原失败单独保留。

修复先分离无副作用的身份合同：`process_scope.py` 统一保存访问身份与执行身份，两种类型仍分别使用。
访问身份保留原 owner/conversation 规则；执行身份只复制可信的 owner/thread/root task/run/attempt，
不能从访问身份的回退值、通知地址或工作目录反推出任务。PTY 直接使用该合同，不保留旧的专用执行身份类。
所有提供的停止身份必须同时匹配；整任务停止不要求当前 attempt，精确 run 停止仍可附加 attempt。
direct/local 控制在精确回合中断成功后，按 `operation=interrupt` 返回，保留未消费的插话和独立资源；
明确 `/stop` 继续清退该请求插话并回收其子代理。当前同步工具仍接收原协作取消信号。
这片身份整理和入口修正不能替代普通后台的持久归属、启动预留、明确交接与停止接线；TUI 137 仍未修复验收。

后续修复采用同一 Store 内的显式 v2 协议，保留原 session JSON 地址，当前正在实现，尚未发布：

- v1 不补猜执行归属，保留原访问权限内按 session 管理；新 v2 冻结执行身份、launcher 出生标识、
  启动预留、host/child 一次绑定、停止意图及 launcher 的明确交接确认。停止意图与交接事实不能被旧缓存清除。
- 新操作共用目录锁，启动准入在锁内复读原调用的取消和权威门，不重复审批或扣减预算。
  host 创建 child 前先记“已进入创建”，随后持锁创建并绑定；外力崩溃发生在 OS 创建与绑定之间时保留未知。
- 多条停止记录先冻结 session 集合，再在原 Store 发布短期 redo 事务文件，作为文件级提交点；
  安装中断后只补写该集合，不重新扫描恢复轮。提交后的安装失败须区分“已提交、待恢复”，不能报告未发生。
  v1 不参加新批量事务；旧二进制不认识目录锁，部署须避免混合版本并发写入。
- 交接前 launcher 死亡、取消或停止使这次启动失效；确认交接后，旧回合 token 不再拥有后台资源。
  终止仍须核对精确进程实例，host 死亡或发出信号都不自动成为 child 已退出；未知状态不得通知完成或被裁剪。
- 锁使用标准库的 [POSIX flock](https://docs.python.org/3/library/fcntl.html#fcntl.flock) 或
  [Windows 字节锁](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)；无 OS 锁的平台明确拒绝。
  原子替换及 redo 只声明进程中断后的恢复，不宣称断电耐久或 Windows 已做实机验收。

当前源码已完成 Store、启动交接及单 session 查询清理，整任务控制仍待接入：

- `process_session_records.py` 统一 v1/v2 校验与不可变身份，v2 的 host/child 只能绑定一次；停止后不能开始 child 创建或确认交接。
- `process_session_lock.py` 持固定目录锁；`process_session_commit.py` 先预检完整批次，再发布 redo、逐条安装及清日志。
  恢复时原内容摘要、精确下一版本、不可变字段和单调事实均须成立，不能用坏日志覆盖新的权威。
- `ProcessSessionStore.transaction()` 为宿主提供有限期锁内检查点；v2 过期写入明确冲突，v1 沿原逐记录锁管理。
  读取、枚举、写入和裁剪都先恢复；事务发布后异常携带固定回执，并使当前锁内视图失效。
- Store 只记录停止意图，不发信号、不重新扫恢复轮、不另建任务取消状态。控制端仍须先关闭原执行轮的真实权限，
  不能把 request closing 或任务投影终态视作 RuntimeDB 权限已经关闭。既有 UNKNOWN 及锁的保留语义不能改写。
- Shell/host 已调用 v2；已删除启动后 `ProcessRegistration/register` 和 launcher watchdog。独立开发进程的故障注入不替代 TUI 137 的安装版复验。

### 现有进程会话行为

- host 未完成有界启动握手：返回原 session 失败/未知事实，并尝试精确清理；未确认不能写成未启动或已停止。
- 受保护记录提交异常：保留提交点与恢复回执，读取不可靠时不再发信号或继续启动；明确暴露 UNKNOWN，不能声称清理成功。
- 记录落盘后在 0.5 秒启动观察期内退出：同一次工具结果返回真实 `exit_code/output_tail`；非零退出为
  `COMMAND_FAILED`，零退出为成功的 `status=exited`，两者都不能叫“后台运行中”。
- 无法取得 PID 出生指纹：持久会话启动失败并回收，不能退化为只凭 PID 管理。
- 日志超过上限：host 终止真实命令树并留下 `reason=log_limit_exceeded`；只有回执确认才记 killed，即使命令处理 TERM 后退出码为 0 也不是自然成功。
- agent runner/Gateway 退出：不自动杀已批准的受管会话；另一个同 scope 进程可重新水合。
- 主机崩溃或重启：不自动重放命令。旧记录只用于还原真实终态，避免重复副作用。

## 非目标

- 不把普通前台命令变成长驻会话。
- 不支持交互式 stdin；交互进程仍由 `terminal_session` 管理。
- 不绕过 exact approval、capability、路径边界或 bwrap。
- 不通过命令正文、模型回复或“服务已启动”文案判断进程状态。
- 不增加自动 restart、systemd 或远程进程调度；需要长期生产守护时应由部署层另行声明。

## 验收

合同回归必须证明：

1. one-shot launcher 退出后，另一进程能用同 scope 查询 running 并 stop。
2. 其他 TUI/owner 即使知道 session id 也看不到日志、不能停止。
3. 权威目录位于 owner sandbox 外，终态不能回退，损坏记录 fail closed。
4. 原 runner 退出后日志上限仍生效。
5. 后台短命命令能保存真实退出码；停止完整进程树不留后代。
6. `network_status` 只返回 exact session 进程树的 listener；loopback 与 non-loopback 不混淆，主机防火墙
   没有显式 port rule 时保持保守状态，listener PID 必须来自 socket inode owner，且任何结果都要求外部探针。
7. 已占用端口、导入错误等立即失败必须在首个 `run_command` 结果中返回非零退出和日志尾部；只有越过
   启动观察期仍存活的命令才返回 `status=started`。观察期必须有界，不能把正常长服务变成阻塞调用。

真机必须使用 `.7` 的一个 Gateway、MiniMax-M2.7 和 fresh TUI：child 经 owner TUI 批准后启动本地 HTTP
服务；child 和 root 工作片自然结束至少十秒后端口仍监听，随后同一用户会话可由 `process_session`
查询/停止。测试者只给一次普通中文任务并观察，不替被测 agent 补服务或产物。

新增可达性验收还必须从测试者所在 Mac 对目标 IP/端口真实发请求，并与 TUI 内的
`process_session(network_status)` 对账。若本机监听成功而 Mac 失败，任务必须报告“服务本机已启动、局域网
未验证/不可达”及已观察到的防火墙事实，不能继续宣称完成；测试者也不能为了让用例通过旁路修改防火墙。

`ma-evidence-r53-child-process-session` 已完成该验收：审批前 8769 关闭，Yes 后 child DONE；child DONE 至少
27 秒、root final 至少 12 秒后 HTTP 仍为 200 且正文含 r53。另一 Python 进程从 owner sandbox 外的
`0600` 记录水合 `running`，错误 conversation 返回 `PROCESS_NOT_FOUND`；精确 stop 后 host 与 bwrap PID
均消失、端口关闭、持久终态为 `killed`。一次性 launch spec 已删除，Gateway 全程只有一个。

`ma-cleanup-process-pid-r10` 补充完成身份验收：模型只用 `bg-...` 作为管理句柄，`network_status` 返回
`listener_pids=[1563961]`，与宿主 `ss` 的 18083 listener 一致；模型没有再用沙箱 `ps/lsof` 否定宿主事实，
最终仍明确局域网未由另一台机器验证。压缩、路径解析和审批复用是独立验收项，不混入本合同的
进程生命周期结论；当前开放问题统一见 `STATUS.md`。

`ma-cleanup-background-r27` 补充完成启动诚实性验收：18478 首次启动越过 0.5 秒观察期，返回
`status=started` 和 `bg-1787946151-9c482822e21d4546`；同一会话第二次用完全相同参数启动时复用精确审批，
但端口冲突在首个工具结果中直接成为 `COMMAND_FAILED/exit_code=1`，MiniMax-M2.7 明确回复
`Address already in use`，没有再说第二个服务已经启动。测试结束后用各自 TUI 的 `process_session stop`
清理 18474--18478，宿主核对已无监听。
