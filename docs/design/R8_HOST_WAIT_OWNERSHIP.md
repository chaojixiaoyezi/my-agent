# 原宿主等待与确认义务的前置边界

状态：已集成到补修分支；只保护原Popen退出状态和wait返回值，不启用操作确认阶段。完整目标见[R8_OPERATION_CONFIRMATION_DRAFT.md](R8_OPERATION_CONFIRMATION_DRAFT.md)。旧c9、UNKNOWN、失败父回合及取消子回合不迁移、不重开。

## 已落地的小片

`subagents.process_control.is_pid_alive`改为只观察。旧实现会对任意传入PID调用`waitpid(WNOHANG)`；若它恰好是本进程拥有的已退出child，会抢走原Popen的退出状态。随后CPython的Popen等待遇ECHILD可能缓存0。自有exit(7)及Event控制的“观察先于原reaper”等价反例在旧实现下均返回0，新实现保留真实7。

Linux保留原`/proc`僵尸观察；Darwin复用原`process_registry._process_is_zombie`，活进程读内核，必要时只点查该PID。Windows 查询同一原生进程的 SYNCHRONIZE 句柄，以零等待检查是否已退出，不向 PID 发送信号；打不开、等待或关闭失败保持未知。对外只接受布尔值的守卫将未知作为仍可能存活处理，不能据此签发退出证明。

`background.dispatch._reap_background_process`仍由原宿主持原Popen执行wait，现在显式返回该次等待结果。调用者不再在等待异常后从可变`process.returncode`补日志退出回执或结算未启动attempt；非严格int结果也不强制转换成退出码。原异常诊断保留，未知不补成功。

没有安装新的Popen.wait/poll替换、周期线程、待清理队列或task属性，没有变更operation状态、资源锁或权限。真实wait返回值仍不能单独升级成持久operation proof。

## 同类调用与回收拥有者

- `orchestration/background/dispatch`唯一实际Popen出生点，正常启动与启动窗口即退两条分支均登记同一原`_start_background_process_reaper`；原reaper持句柄直到wait返回。进程内启动没有子进程等待义务。
- `subagents/shell_gateway_execution._run_subprocess_with_budget`自行持Popen；正常、取消、超时均沿原wait返回并收集输出，不依赖判活观察偷reap。
- `background_start_record_stale`、`cancellation_hosts.cleanup_runner_stop`、`capability_auto_sweep`只观察原派工宿主；`startup_recovery`也可能观察另一进程或已换父宿主，不能成为其wait拥有者。非本进程child仍由其原父/OS拥有等待责任。
- Gateway 判活、调度租约、运行库 holder、适配器与心跳在 Windows 复用同一个只读原语；原生进程等待与出生身份比较仍各归其原拥有者。
- 同族原生waitpid点仍在`multiprocess_native_host_observation`、`multiprocess_native_host_session`及`multiprocess_drain_darwin_waitid`；它们由原Popen/宿主生命周期锁及直接child来源调用，不从本片新授权。

既有spawn成功后到reaper登记前的异常窗口、Thread.start失败的退出责任仍需按正式源单独评审；本片没有用判活或诊断掩盖它们，也不宣称所有异常启动均有完整持久退出证明。

## 下一片可复用事实与尚缺合同

原`OriginalWorkerExitObservation`可在同一原Popen锁内核WNOWAIT、准确zombie birth/直接父与实际reap，拒绝ECHILD及缓存返回码。Darwin已有公开sysctl的zombie出生适配。它只证明直接process实例，不证明共享宿主、后代命令或PTY资源全集。简单把它安装到所有旧宿主并在unknown后丢句柄，会引入新的回收责任缺口；无周期轮询的阻塞等待API候选仍未启用。

原`ManagedOperationStore.record_tool_operation_dispatch`在`BEGIN IMMEDIATE`内按owner/run/attempt/holder/generation、workspace epoch及活跃执行权检查更新原`outcome_json.component_receipts`；这是可复用的唯一操作账位置。当前合并只冻结component_id、message_key及首次dispatch时间，任意target/resource字段仍可覆盖，不能直接充作不可变授权绑定。现有finish只接受终态或UNKNOWN，尚没有确认交接CAS。

锁序必须保原短事务：creation guard内固定目标，原operation组件提交结束后才允许停止效果；DB事务不能跨OS等待。现有资源freeze本身会发stop，不能拿它冒充副作用前纯读快照；资源创建与完整集合冻结的并发门也须明确，不能漏掉刚启动的资源。

原owner维护`_cleanup_owner_runtime`经`commit_transition`进入已有RuntimeRepository。现有status、run/status索引可定位原活跃操作，但没有已版本化确认义务的partial索引/专用keyset口；也没有允许无活跃attempt的proof-only CAS。不能扫历史UNKNOWN或读取task属性猜pending。

租约过期当前会把原操作标UNKNOWN、释放资源锁并标DIRTY；未来确认变体必须先改变责任转交合同，不能只留RUNNING字段就允许旧租约自然释放。原历史retention只瘦身已终态操作且run已终态的到期结果；RUNNING/UNKNOWN不在该瘦身资格内，但确认完的证据何时可退休仍需原绑定引用/保留合同，不能依赖日志TTL。

现有`TOOL_OPERATION_IN_FLIGHT`仅表达等待原操作，普通handler返回仍立即结算；没有等待依赖→有界维护→精确原活跃父轮唤醒的完整消费者。`runner_session.cancelled/ended_at`、旧stop diagnostic、日志`.exit.json`及PID消失均不补这个缺口。

建议下一步：先独立评审副作用前不可变绑定、proof-only CAS、持久保留及unknown回收责任，再一次接通确认阶段和活跃父等待。可以并行只读核宿主/PTY证明源，不能把本片算作旧c9自愈闭环。
