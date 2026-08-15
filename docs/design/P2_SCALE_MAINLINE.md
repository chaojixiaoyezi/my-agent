# P2 规模化主链

更新时间：2026-07-10。状态以 `docs/PRODUCT_FACTS.md` 为准。本文说明正式 scale profile、
当前验证和仍未完成的规模证明；不得把本机真依赖 smoke 写成十万用户容量结论。

## 正式入口与失败语义

`MY_AGENT_DEPLOYMENT_MODE=scale` 是唯一规模部署开关。正式进程只有四类：

```text
migrate Job ──expand migrations──▶ PostgreSQL
                                      ▲
Feishu ──▶ ASGI ingress ──PG queue──▶ worker ──▶ owner-scoped Agent ──▶ Feishu reply
                 │                     │
                 └── Redis readiness   ├── Redis admission/leases
                                       ├── PG RLS runtime state
                                       ├── PG/RLS owner manifest + versioned S3 objects
                                       └── OTLP traces

continuous-monitor ──resume real watches + append elapsed-time proof──▶ monitor RWO state
```

scale 模式缺任一必需事实都会在领取消息前失败：PostgreSQL 应用 URL、独立 migration URL、
非超级用户且无 `BYPASSRLS` 的应用角色、Redis、OTLP endpoint、真实 handler/downstream、租户 ID、
飞书回复凭据、S3 owner store/bucket、Pod 本地 cache/workspace。禁止回退 SQLite、进程内准入、
stub handler、空下游、RWX owner 事实源或 `default` 租户。

## 数据与迁移

- migration Job 使用 `DATABASE_MIGRATION_URL`；应用 Pod 只使用 `DATABASE_URL`，只读检查版本和
  物理 schema，不在副本启动时偷偷跑 DDL。
- expand 迁移先创建兼容列；PostgreSQL claim 索引使用 `CREATE INDEX CONCURRENTLY`；会话 advisory
  lock 串行迁移，非事务 DDL 必须幂等。
- `tenant_runtime_state` 同时 `ENABLE/FORCE ROW LEVEL SECURITY`，撤销 PUBLIC 权限，只授权
  `DATABASE_APP_ROLE`。worker 只写 trace/status/token 等结构元数据，不写 prompt/消息正文。
- worker owner memory/task/artifact 由 PostgreSQL RLS 清单 + 版本化 S3 对象持久，Pod 本地目录只作
  单次执行缓存；对象提交失败时不回复消息。monitor 的 watch/proof 仍是单副本 RWO 状态卷，后续需
  迁独立 watch ledger。详细边界见 `P2_SCALE_ROLLOUT_DR_OWNER_STORE.md`。

## Redis 与 OTel

Redis Lua 在服务端原子维护每租户令牌桶、token/USD 窗口预算和全局并发有序集合。并发槽带租约和
续租线程，worker 崩溃后自动过期，不留下死槽。ASGI readiness 同时探 PG queue 与 Redis。

自建 W3C `traceparent` 继续负责入站→队列→worker 传播；OpenTelemetry SDK 把相同 trace id 导出到
标准 OTLP/HTTP。ASGI 另接 FastAPI instrumentation；scale 模式没有 exporter endpoint 或 SDK 依赖
即拒绝启动。

## 真实连续监控证据

`continuous_monitor_entry` 不是 `scripts/watch_harness` 的加速器。它从正式 owner home 发现已经由
用户开启的 watch，按当前 owner 私网授权恢复 harvester，并周期写 `continuous-watch.ndjson`。
proof 只按真实 wall-clock、连续采样间隔、保证档、最近真实源查询时间和异构来源签名判定：默认至少
24 小时、3 路健康来源、2 种异构签名。进程重启后的证据仍从 append-only 文件重算，不能用循环次数
或历史成功伪造健康。

GitHub API、PyPI、npm registry 三路真实公开来源已于 `2026-07-10T08:55:42Z` 开始计时，并由
LaunchAgent 崩溃重启；未满 24 小时前产品事实仍必须写“长期证明未完成”。

## 本轮真实验证

- PostgreSQL：本机真 PG 执行迁移 1–10，独立 `NOSUPERUSER NOBYPASSRLS` 应用角色写入 acme/globex，
  acme 上下文不带 tenant WHERE 直查仍只能得到 acme 行。
- Owner store：真 PostgreSQL + MinIO S3 API 上传/恢复 memory/artifact，两文件 hash/大小一致，bucket
  versioning 开启，globex 对 owner manifest 直查为 0 行。
- Redis：临时 Redis 7 容器，两个独立客户端共同消耗同一令牌桶/预算；一个客户端持并发槽时另一个
  超时，释放后可取得。
- OTel：本地真实 OTLP HTTP collector 收到 protobuf 请求，解析到 `p2.real.otlp` span，trace id 与
  自建 W3C 上下文一致。
- 数据库迁移、ASGI、worker、Redis、OTel、RLS、部署清单与持续证据均有 focused pytest；最终发布
  仍必须跑全量门禁。

## 尚未完成，不能外推

1. 本机没有 Kubernetes context；canary channel/Gateway API/分析门和灾备 Job 已落地，但没有目标集群
   流量灰度、节点 bwrap/seccomp profile、Redis/PG 故障切换或隔离恢复演练。
2. 没有 10 万用户容量压测、真实 provider 配额模型、SLO/告警燃尽和成本上限实测。
3. worker owner 主体已退出 RWX，但还没有证明 10 万 owner 的对象数量、上传带宽、热点 owner 锁等待、
   大文件 multipart、对象 GC 和跨区恢复；monitor watch ledger 仍在单副本 RWO。
4. 默认 24 小时真实异构来源 proof 正在运行，尚未到时；当前状态必须是 `duration_too_short`。
5. PostgreSQL migration role、应用 role、Redis、OTel collector、S3 bucket/IAM 与 monitor RWO 均需部署方提供；
   仓库不会创建带默认密码的生产 Secret。

## 参考核对

- 先读 `通道运行时_contract_code_files.xlsx`、`长期助手_contract_code_files.xlsx`、
  `langgraph_contract_code_files.xlsx`、`agentscope_contract_code_files.xlsx` 定位合同边界。
- 通道运行时 `extensions/diagnostics-otel/src/service.ts`：标准 OTLP exporter、batch processor、环境变量
  endpoint 和 collector smoke；本实现复用“真实 exporter + collector 验证”模式。
- AgentScope `src/agentscope/app/storage/_redis_storage.py` 与 tracing middleware：共享状态交给 Redis、
  tracing 在应用边界装配；本实现不复制其存储 API。
- LangGraph checkpoint-postgres `setup()`/`MIGRATIONS`：使用前显式迁移并记录版本；本实现进一步把
  migration Job 与应用只读 readiness 分开。
- Claw-Code `rust/crates/runtime/src/worker_boot.rs`：启动状态必须有可检查证据；本实现把缺 scale 依赖
  统一变为进程启动失败。
