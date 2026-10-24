# Owner 工具延迟优化（c4）

日期：2026-10-23。基线：`e1ab9441f3d78fa778de83155281e49abc272975`。

## 范围与不变量

本改动实现四条局部优化，不改变工具授权、审批绑定、逐调用归档、事务提交或 durable operation 的责任边界。未修改 artifact/reader/self_map/shell、operation store、coordinator、ledger 和 archive record。派单明确的文件白名单优先于通用 TESTS.md 收尾约定，本文件承载专项测试说明。

### A. 进程内同 owner 有界调度

- 新配置 `max_parallel_tool_calls_per_owner: int = 0`；0 保留旧段线程池/串行入口及原动态 compact 查询观察点。配置规范化沿原非负整数入口，任务参数不能提高 owner 额度。
- 正额度时，键为 `(os.getpid(), Path(owner_root).resolve())`。根只取宿主 `home_paths.owner_home_dir`，旧宿主兼容取可信 `agent.root`。
- 每个 owner 最多 cap 个 worker，等待队列最多 `2 * cap` 个已接受工作项。生产者在 submit 处背压；不为排队项另起等待许可的线程。弱注册表不永久持有调度器，队空 worker 退出。
- FIFO 定义为已接受项的领取顺序，不保证并行完成顺序。背压者按单调票号入队；等待异常退出登记废票，连续废票被跳过，不能卡死后继。
- 同根宿主暂有不同正配置时，忙碌期采用较小值；超额既有调用不被强杀，但旧 worker 完成后不再领取超额新项。空闲时可采用新值。降额瞬间旧积压允许自然排空；容量限制用于后续入队。
- 每工作项独立 `copy_context()`，实际领取后检查关闭准入/取消/compact；已取消排队项不调用 handler。`started` 在真正启动点发出。中断回调沿原 CancellationToken；不会把取消伪装成成功。
- 结果按原调用顺序收集。首项异常仍等待本批所有已提交项清理，再传播原顺序首错；提交中途出错也等待已经提交的部分，不等待其它轮的全部 owner 工作。
- 嵌套许可用 ContextVar 保存调度器与实际 Thread 对象，包含 PID/规范 owner 键。只有同 owner、同原线程才内联，不靠线程名识别；复制 Context 到另一线程不能借许可。嵌套宿主根不一致明确拒绝，不能借父 owner 额度。内联保留原分段与顺序屏障，子调用仍重入原 ToolExecutor/权限与归档入口。
- 审批等待在工具许可之外；批准后原 actor/model_call/binding 重新进入单工作项边界，仍受启动屏障约束。

### B. live manifest 线性字节裁剪

裁剪算法独立于 `ToolRegistry`，没有新增该类方法。每次完整 manifest、实时 self_context、权限结论、归档 ref 仍按原入口重新生成，不缓存目录权限。

计量统一使用：

```python
json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
```

保持原处理顺序：原值短路 → 所有名卡用途截为 18 字符 → 8 字符 → tools 尾删 → unavailable_tools 尾删 → 原 core → 全部 state 详情缩短 → usage → discovery → tool_states 尾删。字符截断与 UTF-8/JSON 转义字节计量分开。

- 数组删一项扣该项编码长度，以及删除后仍非空时的一个逗号。
- 省略计数加一时补十进制位数差，覆盖 9→10、99→100。
- 字典字段按原单字段 JSON 大小稳定降序删除，同分仍按插入顺序；quarter 门槛仅在原观察点检查一次。每字段只编码一次，排序是 O(N log N)，**序列化总工作量线性**，不再尾删一次就重编码全表。
- 返回前用真实完整 JSON 编码核对累计值；精确整数计量无需逐项全量试删。全部按旧字段与顺序裁剪，不改变真实观察事实。

兼容边界：如果调用者给出异常巨大的不可删固定 core 字段，或把预算降到固定信封本身以下，旧实现也可能无法满足预算；本改动保留该语义，不靠删权威 counts 等字段来宣称“任意输入恒不超限”。正常目录预算由产品常量给出，差分测试同时核对正常预算上限及极端输入旧结果的逐字节等价。

### C. 请求局部 single-flight

`ToolLoopExecuteParams._context_estimate_memo` 使用独立 default_factory，`repr=False, compare=False`；锁和 Future 不进入 `live_archive_state`。旧参数替身通过互斥初始化同一私有 holder，不支持赋值时保守不缓存。

沿用原 `_context_estimate_key` 完整结构键，未改消息内容、schema、媒体预留、guidance、会话/运行身份、估算器身份等失效规则。只缓存纯计量结果，不缓存 compact 裁决或供应商校准。

同键冷 miss 仅一个 producer；计算和 waiter 等待均在锁外。失败传播原异常并清 inflight，不缓存失败；每个返回路径都从冻结的 components 项生成独立浅拷贝。不同 key 可以同时计算，旧 key 晚完成只会形成其本身的缓存项，不会以新 key 发布旧值。无法构造安全键时沿原纯计量路径，None key 不合流。拒绝同线程同键自等待。

### D. 单工具 SQLite 连接寿命

每个 ToolWorkItem 执行时无条件进入新的 `sqlite_connection_scope(enabled)`，替换 Context 中继承的 scope，不操作父连接。开关开启时，同项同库多次借用复用线程本地连接；正常/异常结束即退出 scope 关闭本项连接。关闭开关仍走原短连接。

嵌套子调用只继承配置布尔值，不继承父物理连接；即使 cap=1 同线程内联，也给每个子调用建立新的 scope，退出恢复父 scope。原 sqlite_scope 本身核验 PID/Thread/文件身份；existing_only、claim/finish 的事务和耐久提交实现未改。

## 回归与复现

在本工作树根执行；`PYTHONPATH=.` 保证子进程导入本树，不能把旧 editable checkout 误当当前版本。

```bash
PYTHONPATH=. python3 -m pytest \
  agent_py_agent/tests/test_tool_round_execution.py \
  agent_py_agent/tests/test_tool_segment_planning.py \
  agent_py_agent/tests/test_tool_progressive_disclosure.py \
  agent_py_agent/tests/test_model_latency_q2.py \
  agent_py_agent/tests/test_tool_output_headroom_externalize.py \
  agent_py_agent/tests/test_operation_store_robustness.py \
  agent_py_agent/tests/test_tool_operation_managed_gate.py \
  agent_py_agent/tests/test_owner_tool_scheduler.py \
  agent_py_agent/tests/test_live_manifest_linear.py \
  agent_py_agent/tests/test_context_estimate_singleflight.py \
  agent_py_agent/tests/test_worker_sqlite_scope.py \
  -q --tb=short -o addopts=''
```

新增用例要点：

- 两个独立 Agent/工具轮共享 cap=1/2/4 的真实 handler 峰值；FIFO、独立 Context、不同 owner、许可不能跨线程、队满背压、Future/token 取消、废票恢复、动态降额、系统线程启动失败恢复。
- 原执行轮入口：异常等待本批清理、部分提交失败、串行排队后停机/取消/compact 不进入 handler、不提前发 started；真实 tool_program 在 cap=1 内联返回。
- SQLite：串/并行 × cap=0/1 × 开关的真实物理连接；父子隔离且父连接不被关闭；真实嵌套 inline/legacy 每子调用独立复用。
- Manifest：旧算法独立 oracle 的多种预算/种子逐字节对比，中文/emoji/JSON 转义，18/8 恰好预算，9/99 计数边界，核心字典同分/非字典和 state 字符边界。通过统计实际编码总字节而非不稳定耗时阈值，约束线性序列化工作量。
- 计量：16 个真实并发线程在 producer/waiter 都进入后才释放，只计算一次；全部等待者得到原异常，同 key 重试、独立 components 副本、不同 key 并行、None key、重入、私有 holder 初始化，以及指导/media/压缩/身份变化失效。原 Q2 用例继续覆盖中段消息/schema/工具调用追加。

另跑与嵌套接线直接相关的 `test_tool_program.py`。lint 范围为派单十个生产 Python 文件及四个新增测试；尺寸除 strict 基线门禁外，对改变的 AST 定义检查函数 ≤60 行、参数 ≤6、嵌套 ≤3、类 ≤250，并核对双层注释。

## 当前验收事实与限制

Ruff（派单生产范围加四份新增测试）通过；`check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json` 返回 `hard=0, blocked=False`，不是全仓零历史告警。改变的定义单独按派单更紧的尺寸阈值检查，无违规；双层注释检查无缺口。门禁生成的 CODE_SIZE_REPORT.md 已还原，不随功能提交。`git diff --check` 通过。

最终指定套件：**269 passed，1 failed**。唯一失败是 `test_operation_store_robustness.py::test_s248_lock_rejects_hardlink_same_inode`，夹具执行 `os.link(first, second)` 时返回 `PermissionError: [Errno 1] Operation not permitted`，尚未进入被测锁的断言。没有跳过/放宽/修改该测试，也没有以这条结果宣称产品锁逻辑通过；未在真正基线工作树复现，因此不宣称“基线已失败”。需要集成者在允许创建硬链接的 Linux 车道重跑原用例和指定套件。

本地 Event/Barrier/物理 SQLite/JSON 编码工作量测试不是 Linux 2C4G 的延迟测量，不能据此宣称 Linux P50/P95 改善。生产部署、真实远端模型与完整 Linux 性能验收均未验证；没有修改当前宿主配置或部署包。
