# 受管后台进程会话

状态：后台 session 主链 `8f50d19` 已部署 `.7` 唯一 Gateway并通过 fresh r53 MiniMax-M2.7 真机验收；
`network_status` 只读可达性投影已在当前 worktree 实现，待本地严格 gate、部署与独立客户端探针验收。

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

## 唯一主链

```text
run_command(run_in_background=true)
  -> exact approval / capability / path / sandbox gates
  -> ShellTool 构造已经校验的 argv
  -> detached managed host
       -> bwrap --die-with-parent --new-session
            -> 用户命令及其后代
  -> managed_process_session.v1 受保护记录
  -> process_session list/status/wait/network_status/stop
```

关键点：不删除 `bwrap --die-with-parent`。以前它指向短命 agent runner；现在它指向专门的 managed host。
这样 runner 自然退出不会误杀服务，而 host 被停止或异常退出时，bwrap 仍会收掉沙箱内全部后代。

## 组件职责

### `background_process_host.py`

- 每条显式后台命令创建一个独立 OS session；不依赖 Gateway 或 child runner 的 Python 线程继续存活。
- 只执行 ShellTool 已构造的 argv，`shell=False`，不重新解析模型字符串。
- 启动文件只用于一次性交接，读取后立即删除；状态文件只记录 running/exited、child PID、退出码和时间。
- stdout/stderr 继续写工作区 `.background_jobs/*.log`。
- host 自己持续执行日志上限；即原 runner 与其内存 watchdog 已退出，也不会失去日志保护。

### `process_session_store.py`

- `managed_process_session.v1` 是跨主代理、子代理和 Gateway 进程的唯一持久事实。
- owner-scoped 部署把记录放在 owner sandbox 的同级受保护目录，不放进模型可写的 owner home。
- 每个 session 一个 JSON；文件锁串行更新，原子替换，目录 `0700`、记录 `0600`。
- session id、host PID、PID 出生指纹和 owner/conversation scope 创建后不可变。
- 生命周期只允许 `running -> exited|killed`；并发旧缓存不能把终态写回 running，明确 killed 优先。
- 坏记录返回结构化 load error，不把半条 PID 记录变成可控制进程。

### `process_registry.py`

- 内存表只作当前进程缓存，不再充当跨 runner 权威。
- 查询时从 exact store 水合；必须同时匹配 owner、conversation 和 store root。
- 有 Popen 句柄时直接 poll；跨进程时用 `PID + birth token` 核对，避免 PID 复用后误杀无关进程。
- host 真正退出后才读取状态文件补命令 exit code；状态文件从不提供授权、PID 或 scope。
- stop 对 host 与已快照后代做 TERM、宽限、KILL，随后持久化 killed。

### `process_network_status.py`

- 只接受已经通过 owner/conversation scope 校验的 exact managed session；不扫描或控制无关进程。
- Linux `/proc` 下用 session 进程树的 socket inode 识别该树真正持有的 TCP listener，区分 loopback 与
  non-loopback；端口参数只筛选观测结果，不授予访问权。
- firewalld 可用时只执行固定的只读 `--state` / `--query-port`，报告“显式放行”“未显式放行”或“未知”；
  service rule、nftables、云安全组、路由、NAT 和上游网络未被观察时不得推断。
- non-loopback listener 只证明服务接受非回环地址，不证明另一台机器能连接。结果固定要求独立外部探针，
  工具不自动开放端口、不重启服务、不发网络请求。

## 权威记录

`managed_process_session.v1` 至少包含：

- `session_id`
- `pid`：managed host PID，也是 stop 的进程树根
- `child_pid`：实际 bwrap/命令根 PID，只作观测
- `pid_birth_token`
- `owner_id + conversation_id + owner_home`
- `command + cwd + output_file + host_state_file`
- `status + exit_code + started_at + finished_at`

模型只能提供 `session_id` 和 action。store root 与访问 scope 都由 ToolRegistry/ToolExecutor 注入，不能由
模型覆盖。错误 scope 与不存在继续统一返回 `PROCESS_NOT_FOUND`。

`network_status` 返回 `managed_process_network_status.v1`，包含精确 session、可选端口、进程树 listener、
绑定范围、主机防火墙显式规则和 `external_reachability=unverified_external_probe_required`。只有来自另一
机器/目标网络的真实请求才可把“局域网可达”写成已验证；本工具本身永远不生成该成功结论。

## 失败和回收

- host 未完成有界启动握手：返回 `COMMAND_FAILED`，回收该 host 树。
- 受保护记录无法落盘：先回收 host，再返回失败；绝不留下“已经运行但无人能管”的服务。
- 无法取得 PID 出生指纹：持久会话启动失败并回收，不能退化为只凭 PID 管理。
- 日志超过上限：host 终止真实命令树并留下 `reason=log_limit_exceeded`。
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
   没有显式 port rule 时保持保守状态，且任何结果都要求外部探针。

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
