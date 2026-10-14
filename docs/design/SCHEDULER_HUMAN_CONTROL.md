# 监控的人类控制与未来通知接管

2026-10-14：R07/R08 本地实现，真实 IM 收信和发行由主线统一验收。

## 确定性入口

`@bot /schedule delete job01`（也支持完整 `job_id`）直接进入原控制命令分发，不请求模型、不索要管理员审批。工具 `schedule action=delete` 与命令共用 `scheduler/control.py`；模型工具仍服从原工具策略，显式 ask 策略不会因为命令入口而被覆盖。

- `delete` 关闭未来计划，并请求取消当前所有规则、主任务和子孙；不删执行历史、工作区或产物。
- `cancel` 只请求取消当前已有规则执行，未来计划保留。
- `execution` 查看准确 run/task、规则、执行/等待、输入依赖数量和取消状态。
- `recover` 重新核对这个监控的死亡领取与等待收口，不启动新业务、不建立替代任务。租约到期不能证明进程死亡，存活或身份不明的 runner 保持原状态。
- `takeover` 仅允许已绑定管理员的私人 IM 入口，显式接管原本机监控**后续触发**的通知到当前私聊。

所有写控制使用原 job 版本 CAS。job01 是展示别名，不能提供跨 owner 权限；存在多个匹配时必须使用完整 ID。重复删除复用原 tombstone，并重试尚未确认的取消；旧版本竞争不得覆盖新状态。

## 所有权和原身份

权限只读取认证 owner/入口 actor 或原任务冻结 actor，与原 creator 精确匹配。共享群上下文、引用作者、管理员群成员、昵称和普通文案均不能改变操作人。群 B 即使知道 A 的 ID，也不能控制 A 的监控。私人管理员可以查看本人群监控，但不会因此取得其他群成员的创建身份。

没有 creator 证明的旧任务拒绝删除/接管；只允许已有显式结构化迁移流程处理。原本机任务由明确 `{local:true}` 创建事实认定，不以“来源看着像 TUI”升级未知任务。

## 取消与恢复

原 job 事务先写 deleted 或当前取消状态、取消 queued/claimed 并归档准确 run 历史；原 tombstone/job 保留尚待清理的 run ID。running/waiting 只写持久取消请求，继续保留 claim、PID 出生指纹和原运行状态。原控制服务消费准确 TaskLink，在 task/creation 锁内关闭 RuntimeDB 执行权、必要输入、冻结主资源与整棵子树，然后锁外清理。不能按线程名字、任务标题杀宿主，也不清锁来伪装完成。

回执区分 `cancel_requested` 与 `stopped`。解析器排除坏 run 不代表不存在执行；原账本读取不完整时保持 cancel_requested，保留结构化错误码，不能猜 PID/task 或用空列表宣称当前已停止。主/子执行中断已请求、PTY 异步回收、资源未知或原 claim 仍存活时保持取消请求。waiting 只有资源确认且原执行 claim 不再存活时才沿原收口写 cancelled；running 由原执行者收口或死亡证明恢复，不能提前宣称退出。Gateway 重启按原 job/run/history 重试持久取消，不复活被删计划，不重新执行业务。

## 通知接管

`notification_binding` 是原 job 的带版本元数据，包含原 owner、已认证当前私人 source_thread_id、原三字段 origin binding、管理员绑定的 bound_at 代次及控制 request_id；旧版本保存在同一 job 审计字段。工具参数没有任意 recipient，也不能直接写该元数据。

接管不改 creator、source_thread_id、原执行 thread/model/workspace 或活动 run。每次预留 run 冻结当时的 notification_binding；接管前已经预留、正在执行或欠发的运行沿原通知目标。以后触发才使用当前私人目标，仍沿原后台路由和投递服务，一次只有原 runner。原群监控继续优先使用原 channel_conversation_id；管理员在群中不能调用接管。

投递前重查准确私人线程、owner、原通道绑定和管理员 bound_at。注销、重绑或线程重置使旧通知引用失效；不借新管理员，也不自动寻找其它收件人。接管仅改变通知，不代表活进程迁移。

## 验证边界

`test_scheduler_control_lifecycle.py` 用真实隔离 SchedulerRepository、ThreadStore、HostActor 和原取消接口覆盖确定性入口、负向权限、CAS/重复删除、followup/isolated 冻结、重启、queued/claimed/running/waiting、live claim、孩子/输入及清理失败。沿现有 scheduler actor/security/runtime/registry 用例检验兼容。无真实租户外发、生产监控修改或默认 Gateway 重启。
