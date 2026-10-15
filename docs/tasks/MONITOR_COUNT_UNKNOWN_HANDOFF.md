# 监控活动计数未知交接

这是状态投影P2，**不是权限漏洞**。真实隔离SimpleAgent与原SchedulerRepository中插入损坏活动run，原`monitor_counts`忽略`_valid_runs`错误，回报执行/排队/等待全零且无诊断。

小片仅改`gateway_parts/schedule_control.py`、`conversation/control_commands.py`，新增`tests/test_monitor_counts_degraded.py`。以`/tmp/monitor-counts-snapshot`根最新文件为基准交`/tmp/monitor-counts-unknown.patch`，不含旧工作树其它变更。

现在保留原`load_error_codes`；`job_counts_known`保留监控列表是否完整，`run_counts_known=false`时三项规则计数为null。用户显示“执行 未知，排队 未知，等待 未知”，正常监控数量继续显示。状态故障不作为问答/管理授权门，不改变任何任务、成员查询或控制行为。恢复原账本后计数自然恢复为已知，不保存第二状态。

验证：真实本机/群owner的坏run文件回归、旧不完整投影兼容显示、原schedule control测试；6项通过。定向Ruff通过。根接手后追加总严格gate；没有操作真实owner或飞书。

建议下一步：根应用小片后复验`/status`，并同步总文档/文件树；可并行只读复核成员分页与transfer源目标权限，不要扩大代清理动作或让计数错误阻塞控制主链。

## 空job与孤儿追加

追加增量`/tmp/monitor-empty-runs.patch`以根已应用前片后的`/tmp/monitor-empty-snapshot`为基准。计数明确解析当前scope唯一owner仓库，不再靠job列表发现仓库；即使无有效job，也读取同仓库原run诊断。合法活动run找不到同代有效job时返回`SCHEDULER_RUN_JOB_UNAVAILABLE`与`orphan_run_count`，三项规则计数显示未知；已删除但仍保留的合法job不会被当作孤儿。只读，不修复、停止或启动任何记录，不跨owner聚合。

准确测试文件为`agent_py_agent/tests/test_monitor_counts_degraded.py`。追加空job的坏run/有效孤儿/健康空账，本机与群两种owner；连原schedule control共12项通过。Ruff、strict size、diff check通过。

建议下一步：根应用追加片并跑相同两个测试文件；transfer到齐后可并行开展源/目标权限只读审查，本片不改变转移或监控控制授权。
