## 根整合状态（2026-10-17）

91八路径与108单文件补修已由根按准确前像合入，109已独立关闭105唯一P2。parent_directory、versions_lock仅C退出取当前词法异常并尝试原FD close一次；open_file/transfer/remove_private沿原父目录透传同IO bool，False原调用形状保持。已有native primary不被secondary OSError替换，普通错误仍原优先级；不重关、不补偿改盘、不存权限。112仅改实际包来源测试，严格核canonical根、固定stem、实际.py/.pyc与spec一致，非source-only。完整组合/真实/发行仍待验，下文保留91设计及原验证边界。

# Persona 当前来源本地 IO（91 候选）

状态：以冻结77为唯一完整前置的八路径候选，待独立评审及根组合；不表示服务发行或真实 Count2 已验。

USER/SOUL 仍走原 PersonaRepository、群 lease、同原 quota admission、target 锁、版本 CAS、32 操作批次和 Promotion 权限。仅显式 coordinator_io=True 选择原 execute_global_write(require_scope=True)，来源只由本次 borrow 的原 global_write_commit_scope(source.perform) 运输。实例只增加 IO bool，不保存来源/callback/authority、异常槽或第二状态。原 _ACTIVE_SNAPSHOTS 只记原快照FD/创建责任，没有新增 ContextVar。

C Persona 必须传原同一个 coordinator_io quota；缺依赖在任何 IO 前拒绝，不另建默认 quota。默认False沿原None policy factory、构造调用形状与恢复算法。owner_resources.build_owner_persona 仅默认False透传策略；coordinator_client_memory 的唯一额外改动为原同一 builder 调用显式True，其余77构造次序/资源/helper不变。

目录链创建、准确锁FD出生、stage写/fsync、正文FD verify→replace→verify、snapshot排他link/登记责任、账本捕获/发布/published/清理及原恢复区改盘，分别进入原固定本地门。flock等待、JSON/文本编码、配额扫描、内容校验和读取准备留外。temp清理先门外listdir，再逐个准确私有unlink/rmdir叶。原 source_descriptor、replace_descriptor_text、withdraw_created_source 算法复用，不修改 agents_source_files/rollback。

账本C stage在门外调用同一个 encode_version_records，分别提交原前像、原encoded append和fsync，不把任意旧append callback放进Root；默认追加器仍按原合同调用。固定发布叶保原完整ledger前像核验、capture、排他link、立即published事实、私有清理和目录刷盘；该本地事务读取完整ledger，随字节/记录数增长。snapshot/body完整写同样随字节增长，单次本地IO不可抢占，不能称恒定短门。测试记录1000旧记录及原始bytes/实际门持时，只是样本，不作上界。

正文/快照/账本顺序和原条件回滚保持。实际canonical common/source或物理外层异常沿真实catch同对象bare raise；native不被PersonaLedgerPublishedError、AgentsContractError、runtime健康投影或普通snapshot错误汇总改类。账本已发布的普通错误仍原PersonaLedgerPublishedError并保正文/版本；native已发生则跳过后继补偿、unlink等写，保原恢复材料，不进行自动恢复。

persona_body_writer 将 C prepared 单独放在词法 ExitStack 中，原读/写source_descriptor仍只close，不注入无关账本异常。当前native primary只存本词法局部，交给prepared自身退出；失源只关已持FD。prepared descriptor/control 的必要close始终尝试；其secondary OSError不能覆盖已有native primary。正常退出时私有unlink若失源，传播真实拒绝并仍关FD。原snapshot事务仅C退出close策略也保native primary，reset原token后即使首个FD有secondary close错误仍尝试关其余原FD；不改原创建归属或ContextVar。temporary清理也保native/physical primary并关闭原目录FD；普通IO仍按原ignore_cleanup_errors/恢复合同处理。

只读 load/list/current/hash/history/tail/配额读取也有原锁出生，显式C不能漏门或洗成空历史；缺正文load仍返回原missing，不额外初始化。runtime_snapshot真实native先透传，普通健康投影保持。

普通测试使用原临时文件、flock、真实领域对象与原scope；fake仅外层同步门。实际HTTP/work/G1/source用例验证正文之后和ledger发布之后失源、首native对象、lifetime/pin退出及FD关闭；不调用真实网络/模型/平台或native/D24/P6探针。每个USER/SOUL现有34/缺失31叶逐点撤权，错误后不再尝试写；普通/批量/CAS/rollback/外部换身/短写/已发布错误及跨线程/退出scope均覆盖。

建议下一步：非作者审准确八路径、默认AST、实际native/收尾与发布后事实，再由根串行组合C4并执行最终验收；可并行只读，不能并写共享builder/JSONL或把普通项外推真实发行。

## 108 最小close优先级修复（候选待独立复核）

105独审用真实global_write_commit_scope自然退出后的persona_io(require_scope=True)发现：parent_directory与versions_lock原finally的secondary OSError会盖掉实际GlobalWriteCommitUnavailable。108只改persona_storage_io一生产路径，两finally在显式C时把本词法sys.exception()传给固定_single FD close叶；原os.close只尝试一次，只有已有原_PERSONA_NATIVE_ERRORS tuple实际native/physical primary时才让secondary OSError不抢outer/cause/context，不重关、不重开、不补偿写。

open_file、transfer、remove_private显式C沿原parent_directory透传同bool，以保护真实叶调用方；False保持原kwargs形状/原close优先级。普通primary或无primary时，close错误仍原样胜出。不新增slot、ContextVar、authority或统一资源清理管理器，原source files/rollback及其它91生产文件不改。

105原2F反例同字节在108转2P；新增原FD各关一次、八实际类型outer/cause/context、默认False/ordinary优先及三实际C caller自然scope退出等窄回归，叠91原1372矩阵验。最终数/map以仓外108 REPORT/XML为准，仍待Astra109审，不等于合根/真实发行。
