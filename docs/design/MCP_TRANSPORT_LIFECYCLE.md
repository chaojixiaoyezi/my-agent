# MCP 连接生命周期

状态：第 4 步本地开发中的通用连接边界，尚未发布部署。它解决旧连接队列跨重连发送、
显式关闭后被注册层恢复，以及旧超时误清理新进程的问题。完整插件激活仍按
[插件生命周期](PLUGIN_LIFECYCLE.md) 和唯一安装表接线；本模块不新建插件状态库。

## 所有权与状态

- `MCPStdioClient` 持有唯一当前连接、启动串行锁和永久关闭事件。
- `MCPTransport` 一次绑定一个 Popen 和启动时的出生标识；请求锁、写锁、响应箱、读线程、
  stderr 和清理回执均属于该连接。新连接不复用旧事件、ID 队列或诊断。
- `start()` 返回本次连接；`reconnect()` 仅在原进程清理确认后替换连接。
- `disconnect(transport=原句柄)` 清理临时失败连接，不终结客户端重试资格，不能猜当前连接。
- `stop()` 先永久关闭并撤销原连接准入，再在生命周期锁外清理；以后不能 start/reconnect。
- 启动进程尚未返回时收到 stop，回执为 `launch_pending`、清理未确认；启动返回后必须清理
  该候选，不能发布 ready。不能用“当时还没有 Popen”宣称资源已经退出。

## 调用、发布与锁

一次调用进入客户端时冻结 transport，之后在该连接请求锁和写锁之后再次检查关闭、取消和期限。
发送线程的启动与撤销共用短准入锁；先取得发送准入的请求属于在途，可能已产生外部副作用。
写线程独占复制后的 fd，直到自身退出才关闭，避免 fd 被重连复用。半帧超时只废弃原连接。

所有 tools/list 分页绑定同一连接。注册层先构建候选目录，再经 `publish_tools(原连接, 回调)`
在短状态/准入锁内核对身份和运行状态，回调只替换内存目录，不能请求网络或获取 prepare 锁。
已有普通 MCP 的非法声明跳过策略保持；插件由专属组合层完整匹配静态清单，任一坏项拒绝整包贡献。
它仍复用本连接发布门，见 [启用组合](PLUGIN_ACTIVATION.md#启用与新运行的贡献组合)。

普通注册失败使用精确 disconnect；启动未返回句柄时由 start 自行收尾，外层不能猜 current。
永久关闭不进入退避。权限视图共用的客户端列表就地清空，旧快照通过同一客户端拒绝调用。
关闭注册表先设置各视图共享的关闭标记，再冻结客户端并 stop，打断发现后取得 prepare 锁清共享引用。
登记新客户端后也必须复查关闭标记；不能让首次清理快照后加入的连接逃过关闭，不能持 prepare 锁等长请求后才撤销。

## 进程清理与限制

清理复用 `process_registry.terminate_process_tree` 和原出生身份，返回原 `ProcessTerminationReceipt`。
状态查询不调用 Popen.poll/wait 提前回收组长；连接读线程通过 EOF 判定断连。
原树快照完成后才回收进程，使自然退出但尚未回收的独立组长仍可用于核对同组后代。
已被外部回收、出生身份不可读取或原清理无法确认时保留未知，不启动替代进程，也不改报成功。
清理回执只证明本次可观察并核对的进程树；没有额外 OS 隔离的任意脱离/隐藏进程不获保证。
读线程在自己的 finally 关闭流，收尾有界等待且不等待自身；明确关闭成功后移除退出回调引用。

## 固定激活与原托管进程

本地内部接线中，`MCPStdioClient` 可由可信宿主绑定 `PluginActivationRef`，整个客户端只能沿原代启动和重连。
插件使用原 BackgroundLaunch/host/ProcessSessionStore，普通已配置 MCP 保留原进程树路径；插件启动失败不回退裸 Popen。
stdio 的 stdout/stderr 各交给一个 UTF-8 TextIOWrapper，之后沿原有字符上限、脱敏和 reader 关闭归属；stdin 仍发送原始字节。
Transport 固定真实托管 Popen、原 record/store 与出生身份，不能将共享 Gateway PID 标成可清理的插件 host。
清理直接返回原 `ProcessSessionCleanup`；提交/redo 异常继续携带原报告，UNKNOWN 禁止替代。
启动后、Transport 接管前的失败同样回收原句柄和管道；该阶段清理未确认时保留异常，stop 不能改报 not_started。

launcher 与独立 host 从可信 root/owner/scope 还原同一安装表，不从工作目录或首个任务猜用户。
预留、Popen、host 创建 child 和交接前在原资源锁内复查；只有 preparing/active 可以启动，revoked 永久拒绝。
撤销组合顺序为：原安装 CAS 提交 revoked 并释放安装锁，再原资源 Store 冻结该代，最后锁外精确清理。
管理 disable 已本地组合该顺序；不能用单个 client.stop 替代 owner 范围的持久撤销和资源证明。

发送在 request/write 队列后取得原资源锁，再取本地 admission_lock，复查原 session、激活和本次 executor 权限。
等待资源锁可响应原调用取消/期限和连接关闭，不持 admission_lock 排队；锁内不等待协议响应。
原目录锁仅在等待/准入前调用 wait_check；取得锁后的 redo/提交保持完整，取消不截断事务。
initialize、initialized 和 tools/list 允许同代 preparing；业务调用只接受 active，普通未知方法不能借准备状态执行。
原代理冻结发现 transport，重连后需发布新代理，旧 Schema 不会自动指向新连接。
固定连接选择、请求/写队列中未创建 writer 的拒绝只结束本次调用，以结构化 `effect_outcome=not_started` 进入原操作账。
writer 已启动则可能收到部分帧，错误继续 UNKNOWN；不能按错误码或文案把所有取消/超时都认作未执行。
目录发布前复查 active，但内存目录仍是投影，真正执行必须再次准入；静态 tools/list 匹配已本地组合，完整装卸和实际多 TUI 尚未验收。

## 参考与验收

核对本地 Codex 源码版本 `578c1b2230288104041e880a86d0f7f3a5ca6e47`：
`rmcp-client/src/rmcp_client.rs` 的 shutdown 先设置 Closed，再独立清理；
HTTP 会话恢复在连接前与发布前复查 Closed。这里只采用状态/清理顺序，没有复制实现、
引入依赖，未执行 Codex 测试，也不把其 HTTP 恢复当作本项目 stdio 的进程归属证明。

开发验收使用确定性交错与临时 MCP 进程，覆盖两层排队、启动中 stop、握手后 stop、
旧连接延迟工作、未知清理禁止重连、同组子进程退出、管道关闭和共享权限视图。
真实插件装卸仍须经官方模型和多路实际 TUI，进度见 [测试记录](../../TESTS.md)。
