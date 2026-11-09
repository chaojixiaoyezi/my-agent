# R8-09 业务观察来源分页与用量范围索引

状态：本地实现和定向验收，尚未代表最终集成、跨平台或运行服务切换。

## 解决问题

执行/工具列表先 fetchall 再返回切片，深页和稀疏授权仍会扫描全部历史。用量按时间筛选前读取全部线程及每线程全部事件，持续运行后查询 CPU/RSS 随历史累积。

## 执行与工具来源

- 原 SQLite rowid 是稳定源位置。列表先按 `rowid > after ORDER BY rowid LIMIT limit+1` 读取最多101个原位置，再只对本页位置 join，SQL 保留 owner 过滤，返回前逐条核 actor。
- 授权/业务滤参稀疏时允许空页加续游标，不能为补满展示页而扫描后续全部历史。执行/工具点读沿原 run/attempt/operation 索引并最多两条，保留重复/未知与准确 current attempt 判据。
- 游标绑定 owner、actor、动作、全部滤参与主库/WAL 版本；忽略只读打开产生的空 WAL，不忽略实际提交。来源变化或跨滤参使用返回 CURSOR_EXPIRED。
- ObservationPage 新增 source_paged/next_position；仅宿主明确登记支持源分页的 reader 接收 ObservationSourcePage。旧自定义 reader 的参数及旧分页合同保持。

## 用量事件与来源

- 原 UsageIndex.events 增加 created_at 时间索引和缺失时间 partial 索引；原 install 提交时间定位，旧数据库按128行有界回填。活动/冷段/时间回填共用每次128事件预算，last_work.events_read 给行为验证。
- 时间窗口 SQL 只访问请求区间，fetchmany(128) 后逐条回查 canonical 原行及身份、原行摘要与 created_at；未追平/坏账/源码版本变化返回 unknown，不回退全历史读取。
- 普通 usage.get 流式沿原决策计数算法聚合，窗口超过128事件仍完整可用；每源位置、成员授权与原行都可重读验证。请求缓存最多1024项，源版本缓存最多128项，决策会话明细用固定10项 heap，内存不保全部窗口正文。
- usage.batch 的既有显式 events 输出仍按请求窗口物化，保持内部调用方合同；不读取窗口外历史。
- 精确 agent/task/run 通过 canonical 身份定位线程。owner/space 范围使用 UsageSourceIndex 的时间/来源索引，查询不再 threads.list_report(limit=0)，不创建索引、不迁移、不构造 Agent。

## 可重建来源投影

`<conversation_root>/.model_usage_sources/` 保存 sources.sqlite3 及 pending/ 私有 intent。原 JSONL/分段继续拥有事件与费用事实；投影只保存 thread_id、时间界、已核版本和覆盖，不保存权限或第二份正文账。

- 正式追加前先发表唯一 intent（仅 thread_id）；不等待来源 SQLite 锁。原事件/索引提交后，来源事务核 source revision CAS，成功提交才删除 intent。DB/锁 busy 时原 canonical 追加可以完成，intent 保留，查询不能把中间窗口视为空或完整。
- 维护在原 append guard 下重核/恢复遗留 intent，再推进来源投影；活跃写者或 metadata busy 都非阻塞返回 partial。
- 原生目录 cookie 复用既有 directory paging 实现，持久化在本 SQLite meta；重启或超过64个 owner 轮转不重扫前缀。初次库存只按固定目录步骤推进，已登记正式写者同步当前 epoch，忙 owner 不反复丢弃已知前缀。
- 库存覆盖与坏源分开。未完成/未修好的源不能当空；按 repair_after 轮转有界修复，单个坏源不饿死其它源。
- 旧投影记录按 epoch 索引逐批删除，绝不在结束一片时删除整个旧库存。未发表、五分钟前的本域临时 intent 文件也按小批清理，不把废弃临时文件积累成永久不可读。
- 整个投影/目录被删除后显式维护可重建；缺失时只读查询给 unknown，不静默创建。规范 workspace/runtime 及旧 conversations/sessions 家族均由宿主状态路径规则保护。

维护入口：

```python
store.model_usage.prepare_window_sources(limit=4, deadline=time.monotonic() + 0.025)
# {coverage: complete|partial|unknown, sources_examined: <= limit,
#  cursor: brief projection progress|None, errors: structured list}
```

完整 cookie 在来源 SQLite 中，不塞入维护 JSON。旧源每一步最多128事件；deadline 到达即交还续点。root 在现有 owner storage lane 接真实触发，不依赖未来用户追加才修复旧库存。

## 本地证据与边界

新增真实来源行为测试包含执行/工具深页、稀疏授权、滤参/版本失效、300事件窗口、旧库存续修、66 owner 轮转、跨进程 pending 交错、stale source CAS、busy 不延迟 canonical 追加、真实 Gateway HTTP 往返与宿主路径保护。

历史执行从100增加至4000，末尾3项的 SQLite VM 步数均为815；历史用量从100增加至4000，请求300条窗口都只回读300条 canonical 事件。最终工具步数与完整测试数见仓外交接/JUnit，不以返回数量冒充源成本证据。

建议下一步：root 集成后把私有 `_pair_scan_page` import 换为新公共 `user_space.directory_paging.directory_page`，接原维护lane并跑组合严格gate/跨平台CI。可由其他agent只读复核来源 CAS/intent 生命周期；不要并写同一 reader 或 source writer。10 TUI 长历史资源实测由root在冻结组合源统一执行，本片没有新增真实用户负载或访问真实个人数据。

显式 `usage.batch` 已保留所选窗口数据，现一次 `pairs` 消费同时收集事件与成员授权事实，消除逐来源的第二次事件读取；所有来源消费后再次核对原捕获 revision，不能把新 revision 贴到旧窗口。最终逐成员授权保持原入口。普通 `usage.get` 仍以固定内存重流末核成员，未改变其合同。
