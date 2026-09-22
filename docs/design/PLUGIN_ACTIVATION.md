# 插件激活与撤销权威

状态：第 4 步本地源码已串通实际启用、普通工具组合和停用，仍未完成重新启用及卸载。安装表继续是唯一当前状态，
原 HostCommand / ToolExecutor / OperationStore 保存执行、幂等历史和未知结果。

## 状态与原子边界

安装表 v3 将旧的固定 `enabled=false` 与空 `activation_id` 替换成一个可选激活记录。
记录绑定原环境计划、安装/配置版本和新的激活身份；`enabled` 是已发布状态的只读投影。
配置值仍只在同一私有安装记录中，不放入目录、工具参数或结果。

- `preparing`：原启用操作领取后、创建候选资源前，CAS 预留本代；尚不发布工具。
- `active`：候选环境和完整 MCP 目录验收后，沿同一身份与安装版本提交发布。
- `revoked`：先持久关闭本代，再清理其精确资源；这一状态本身不证明进程退出。

准备中、已发布以及撤销但未确认清理的记录都禁止改配置或预留另一代。
旧快照须带原激活身份；检查只读原表，缺失、损坏、身份变化或已撤销均不能补成当前代。
同一次启用的准备和发布是原操作内两次安装提交，不是两个宿主请求或两套运行状态机。
原请求重放由原操作账处理；安装表的最后提交只证明该次 CAS，不是永久幂等历史。

新增准备/发布沿原 quota→插件锁顺序；撤销只修改有界既有记录，不领取配额锁。
这样准备阶段占用配额或 owner 空间不足不会阻塞关闭权限。撤销不启动进程、增加包或环境，
仍遵守表大小上限、原插件锁、原子替换和异常读回，不能借此绕过新资源的配额。
锁内只读取和提交事实；环境准备、MCP 请求与资源等待均在锁外。

v1/v2 只有符合旧字段、停用与空激活约束时才能显式迁移；查询不写入。
首次实际修改写 v3，同时保留旧文件摘要与已有迁移来源。损坏的新协议不降级读取旧格式。

## 进程和调用接线

同一持久激活可以对应不同宿主进程内的 MCP 实例；实例永远绑定该代，不能原地换成新版本。
权限视图共用实例，主/子独立 Agent 或独立进程通过同一持久激活事实撤销，不共享 Python 对象。
每个实例及调用仍须登记原资源账的精确身份；不新建 broker、插件执行器或副作用账。
共享连接寿命归 owner/activation，不能伪装成首个业务任务的资源；业务调用继续保留原 run/attempt。

原后台 host 的日志与空 stdin 不能承载 MCP 管道；[显式 stdio 模式](MANAGED_PROCESS_STDIO.md)已有本地实现：
launcher 创建 host 管道，再将端点直接继承给 child，继续原预留/出生身份/交接，不增加转发 broker。
原资源记录 v3 已有固定 activation 引用与同锁精确冻结，普通任务不可访问或裁剪共享资源。
完整停用仍须先提交 revoked 并释放安装锁，再由资源 Store 冻结该代记录并写停止意图，最后锁外逐资源清理。
启动预留、创建进程前和交接均须复查原代；避免安装锁和资源锁反向嵌套。
不能将 Gateway PID 伪装成可以整树清理的 host。固定引用和 MCP 启动/发送已本地接线，完整管理撤销和资源退出证明仍待组合。
当前管理 disable 和 enable/普通调用见下文；显式调用、重新启用及 remove 仍须接通释放/消费边界并实际验收。
撤销事实不能代替 OS 清理证明，不能提供接受外部 `cleaned=true` 即删除激活记录的入口。

参考边界：沿已核对的本地 Codex MCP Closed-before-cleanup 顺序，以及本仓库
`MCP_TRANSPORT_LIFECYCLE.md`、ProcessSessionStore 的原预留/身份/精确停止实现。
`PluginActivationRef` 只携带可信 root/owner 和原 scope，读回沿唯一 Store；它本身不缓存授权。
准备握手显式接受 preparing/active，业务调用仅 active；旧客户端不能将冻结引用刷新成新代。
当前托管 MCP 组件已覆盖鲜活发送准入，完整管理装卸仍未完成，也不代替实际多 TUI 验收。

## 管理停用的组合边界

状态：本地 `/plugins disable <插件>` 已沿原管理权限、HostCommand 与 ToolExecutor 接线；尚未部署或实际 TUI 验收。
实际启用与普通贡献已有本地组件验证，重新启用与卸载未完成；先前假启用组件不算实际包 enable 验收。

1. 从同次目录/安装快照冻结原代，在唯一安装表先 CAS 为 revoked；释放安装锁。
2. 按 plan 保存的 operation ID 与可信 owner 反查原 HostCommandBinding；核验首次完整运行链、启用工具及原资源声明。
3. 沿原取消入口关闭该准备 attempt；DB 事务结束后冻结其完整 execution_scope，包含终态准备命令，以核对 host 是否真正退出。
4. 按原 owner/plugin/activation 冻结共享 MCP 资源；两类清单各自在原 Store 锁内形成，实际清理均在锁外。
5. 原清理回执进入同一管理操作，查询/原请求重送不重新选择当前代。无激活的停用也在原安装锁内核对同一版本。

坏准备引用不授权停止其他任务；已撤销的准确共享资源仍尝试清理。坏记录/redo、提交错误或任一退出未知均保留。
当前普通 task stop 的默认终态过滤不变；仅明确管理清理传 `include_terminal=True`。
静态目录读原 enabled/activation_id，不能把已发布插件一直画成停用，也不能用目录缓存授予执行权。

本片保留 revoked 激活及全部 retained records；`cleanup_confirmed` 只表示本次两类已选资源退出得到确认。
仍待实现：资源证据核验后的 release CAS，以及原 ToolExecutor 结果持久化/读回后的精确 consume。
不得先删记录再提交 release，安装提交 UNKNOWN 时保留全部证据；消费失败只留下历史，不反向改写已确认提交。
原 operation 的 UNKNOWN 分支不保存新结果，尤其不能在 handler 返回前抢先删除资源记录。
移除候选环境还须读取原 executor 的真实退出事实；attempt cancelled 只关闭创建权限，不证明原 Python handler 已结束。

## 启用与新运行的贡献组合

状态：本地实现及组件验证已接通，完整装卸和实际多 TUI 尚未验收。

管理 enable 沿原 HostCommand/ToolExecutor：固定环境计划先进入 claim，领取后预留 preparing、准备离线环境，
实际启动候选 MCP 并完整读取分页目录。名称集合、说明和规范输入 schema 必须全部匹配静态包；不跳过坏条目。
候选连接只用于启用验收，精确退出得到确认后才提交同一代 active；长期业务连接由原 Registry 的新运行边界按需建立，
每条新连接重新检查完整目录。这样冷管理入口不创建 Agent，也不留下无归属的临时连接；active 表示已启用版本，
当前进程的连接可用性另外由原 MCP 事实表示，不能用 active 伪造当前服务健康。

私有设置只以 `MY_AGENT_PLUGIN_SETTINGS` JSON 环境变量交给隔离模块，不进入命令参数、模型目录或管理结果。
宿主以该代环境的 Python 和 `-I -m` 启动静态入口，沿原安全环境基线；包不能指定任意 Shell 命令。
包的 requested_effect 仍不是授权，首期工具一律沿原 dangerous/审批策略，不凭只读自述降低风险。

可信 owner 从 core 唯一组合入口传入 Registry；构造、目录读取和 availability 不启动插件。
连接继续保存在原 MCP 客户端集合，已验代理绑定同一连接及激活；每个权限视图在 prepare_for_run 后重建自己的插件投影。
新代只能生成新代理，旧 handler 不原位换绑，实际发送仍读原激活与当前调用权限。功能开关关闭时不接入插件贡献。
注册表永久关闭先设共享标记再冻结并关闭连接，迟到登记在加入后复查；未成功登记的候选也关闭自己的退出回调。
构造新连接前读取原资源账，同代未知或未确认停止不能靠新 Registry 绕过；不将其他正常运行实例误当需要重启的旧进程。

完整 session 清理证明只附加到原 termination.cleanup，原自然退出事实和 child 回执保持；已确认事实单调保留。
保存前故障继续 UNKNOWN，已提交 redo 的恢复沿原 Store；没有原生树回执的 unknown 不能只靠 PID 消失变成功。

本片核对本地 Codex `578c1b22` 的 `codex-mcp/src/connection_manager/tool_catalog.rs` 的连接视图/目录组合，
以及本仓库 Registry、owner pool 和 worker 的实际入口；未运行 Codex 测试，也不复制其实现。
非本地 owner 的自动后台派工当前仍按原策略留在进程内；本片不声称新增远程 owner 的独立 CLI 身份传递。
