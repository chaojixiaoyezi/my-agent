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
writer 已启动后未取得有效完整响应，可能收到部分帧，错误继续 UNKNOWN；不能按错误码或文案把所有取消/超时都认作未执行。
目录发布前复查 active，但内存目录仍是投影，真正执行必须再次准入；静态 tools/list 匹配已本地组合，完整装卸和实际多 TUI 尚未验收。

## 参考与验收

### 完整工具失败与未知结果（本地修复，待发布验收）

实际 TUI158 的链接读取留下 `effect_outcome_unknown:TOOL_EXECUTION_FAILED`，原账未保存业务正文，
因此不能据此断言现场已经返回某个具体路径错误。确定性组件验证另行复现：完整合法的
`CallToolResult.isError=true` 未设置执行结局，被原协调器改记 UNKNOWN，错误正文不可读且逻辑资源锁未释放。
代理现将这种完整响应标记 `effect_outcome=failed`，由原操作账保存失败结果、释放锁并支持只读重放。
这证明本次调用已明确返回失败，不证明零副作用或外部变更已回滚，不自动重放同一请求，也不回写旧 UNKNOWN。
超时、断连、非法响应、代理异常和清理未知沿原路径保持；发送前的明确拒绝仍为 `not_started`。

参考 [MCP 工具错误协议](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/server/tools.mdx)：
工具执行错误作为结果交回调用方，供其理解失败；与未取得结果的传输问题分开。
本地 Codex 同版本 `core/src/tools/handlers/mcp_resource.rs` 也按 `is_error` 区分工具失败与传输异常；
只参考协议边界，未复制实现，未执行参考项目测试。
回归见 `test_mcp_operation_outcomes.py`：原执行器、临时 RuntimeDB、同请求重放、后续独立操作及未知对照。

核对本地 Codex 源码版本 `578c1b2230288104041e880a86d0f7f3a5ca6e47`：
`rmcp-client/src/rmcp_client.rs` 的 shutdown 先设置 Closed，再独立清理；
HTTP 会话恢复在连接前与发布前复查 Closed。这里只采用状态/清理顺序，没有复制实现、
引入依赖，未执行 Codex 测试，也不把其 HTTP 恢复当作本项目 stdio 的进程归属证明。

开发验收使用确定性交错与临时 MCP 进程，覆盖两层排队、启动中 stop、握手后 stop、
旧连接延迟工作、未知清理禁止重连、同组子进程退出、管道关闭和共享权限视图。
真实插件装卸仍须经官方模型和多路实际 TUI，进度见 [测试记录](../../TESTS.md)。

## W3：能力包按需连接与展示缓存

- 有 MCP 运行声明的能力包在构造时只从已安装的声明或固定目录生成 `PackMCPProxyTool`；
  门面不保存 transport。默认关闭的开关由 core 注入 RegistryParams，不读第二份配置。
  prepare 只同步激活、回收空闲连接和投影门面，不为能力包启动或重连进程。
- 首次调用先鲜活复核激活，再在每客户端锁内完成一次启动、目录发现与发布。声明目录完整匹配，
  固定目录使用与启用时相同的归一化函数做字节相等比较；漂移精确清理原连接并永久拒绝，须重新启用。
  普通启动失败只对该激活按原 MCP 公式退避，不能拖慢不调用它的运行准备。
- 在途名额在 ensure 之前预留，避免连接返回与计数递增之间被空闲回收。请求实际交给固定连接代理，
  不向能力包发送工作区上下文；原业务错误与已发送结果未知不改写成未执行。
- 空闲回收使用 `disconnect(transport=原句柄)` 的精确退出回执，确认后清空已验代理，下次调用可重连。
  这里的“停止空闲进程”不是 `MCPStdioClient.stop()` 的永久关闭；绝不清除永久关闭事件来复活客户端。
  未确认清理仍保留原句柄，由原重连门拒绝替代进程。没有新后台线程，回收只发生在运行边界。
- 展示和审批预检共用最多 1024 条的进程内 FIFO 正缓存，绑定 root/owner/plugin/activation 及原表完整 stat 指纹。
  注册层可复用前后指纹一致的一次完整解码结果预热同表各激活；新写入两秒窗口内不读取或写入该缓存。
  指纹变化、错误或未命中重新完整复核；缓存不是执行权，启动和发送仍走鲜活 `require()` 与原托管准入。
- `package:` 门面默认收起；折叠提示只计真实数量，不列包工具名字、插件简介或默认工具索引。
  显式关闭默认收起时照常全量展示。目录工具仍能查到完整授权工具。
  包 get 的 `capability_package_tools` typed 信封、选包和孩子入口只追加本轮展示名，按冻结工具快照裁剪，
  不解析正文、不授予权限、不增加持久加载账本；原 32 项活跃展示上限保持。

能力包握手上限为 10 秒。**D9 仍开放**：握手目前仍持来源进程边界锁；此上限只限制单次握手等待，
不代表完整启动/发现总耗时或持锁墙钟严格小于 10 秒，后续须把握手移出来源边界。
本机组件/计数测试与真实 stdio 必须分别记账；真实用例沿仓库 `sandbox_capability` 门，环境缺能力的 skip
不等于 stdio、平台沙箱或实际 TUI 已验证。W1 基线的固定目录测试只按集成任务许可适配旧摘要转换，
生产启用链仍由 W2 和集成版真实验收负责。
