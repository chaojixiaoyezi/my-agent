# P2 规模化第二轮：灰度、灾备、Owner 对象事实源与 24 小时 proof

更新时间：2026-07-10。本文记录本轮已经落地的第一段主链和仍受外部环境阻塞的部分；
状态承诺以 `docs/PRODUCT_FACTS.md` 为准。

## 当前环境事实

- 本机 `kubectl` 可用，但 `kubectl config current-context` 为空，context 列表也为空。因此仓库可以
  完成清单渲染、策略门和本地真依赖验证，不能宣称已经在目标 Kubernetes 集群完成灰度或灾备演练。
- 目标集群接入后必须补齐：Gateway API/KEDA/CSI 能力、节点 Localhost seccomp profile、正式镜像
  digest、PG/Redis/OTel/S3 Secret、对象 bucket versioning 与服务端加密。

## Owner 主体数据：RWX 退役主链

规模 worker 的不可绕过边界现在是：

```text
claim release_channel message
  -> PG advisory lock(tenant + owner)
  -> PG/RLS owner_file_manifest
  -> versioned S3 objects restore
  -> Pod-local emptyDir execution
  -> upload content-addressed objects
  -> atomically replace manifest
  -> Feishu reply
```

- `scale/worker` 必须显式配置 `MY_AGENT_OWNER_STORE=s3` 和 bucket；没有对象存储时启动配置直接失败，
  不回退 RWX。
- bucket readiness 要求 versioning=Enabled。对象以 tenant hash + SHA-256 内容寻址；恢复时复算大小和
  hash，路径越界、符号链接、恢复校验失败、快照过程中仍写入都会 fail-closed。
- 清单表强制 PostgreSQL `ENABLE/FORCE RLS`；同一 owner 跨 Pod 用 session advisory lock 串行。
  对象全部成功上传后才在单事务替换清单；清单提交后才回复飞书，进程崩溃不会覆盖上一份完整快照。
- worker 的 `/var/lib/my-agent` 与 workspace 都是 `emptyDir` 缓存，不再挂 owner RWX PVC。monitor 的
  watch/proof 目前仍用单副本 RWO 状态卷；它不是 worker owner 主体事实源，但后续仍应迁成独立数据库
  watch ledger，避免长期监控依赖单卷。

本轮真依赖验证使用 PostgreSQL 16 + 受限 `NOSUPERUSER NOBYPASSRLS` app role + MinIO S3 API：
迁移 1–10、两文件上传/恢复、bucket versioning 和 globex 跨租户直查 0 行均通过。裸 MinIO 未配置
KMS，因此本地 smoke 不发送 SSE header；正式 scale 配置仍只接受 `AES256` 或 `aws:kms`。

## 灰度链

`release_channel` 是队列表的结构化列，不从用户消息正文推断。stable/canary ingress 各自入对应通道，
stable/canary worker 只领取同通道消息；同一 lane 的 claimed 排他仍跨通道生效。

`canary-route.yaml` 使用 Gateway API backend weight，初始 canary=0。推荐阶段为
`1 -> 5 -> 10 -> 25 -> 50`；每阶段先重建 `canary-analysis.yaml`，要求 canary `/readyz` 正常、
本窗口无 failed 消息、无超过 15 分钟的 pending/claimed。任一门失败立即把 canary 权重改回 0，
保留 canary worker 排空已入队通道后再缩容，禁止把 canary 积压留成无人消费队列。

当前没有目标 context，所以只完成代码、清单和本地解析/测试；没有执行真实流量权重变更。

## 灾备链

- 对象层：正式 bucket 必须启用 versioning，owner manifest 只引用内容寻址对象；生产还应配置 bucket
  replication、lifecycle 和跨区域副本，这些属于目标云账户设置，仓库不能伪造。
- 数据库层：`dr-backup.yaml` 每小时以 migration role 生成 custom-format `pg_dump`，再上传同一灾备
  bucket；它只是托管 PG PITR/WAL 归档的补充。
- 演练层：`dr-restore.yaml` 只允许恢复到独立 `DATABASE_RESTORE_URL`，拒绝它等于生产 URL，并要求
  实际数据库名与显式 `DR_EXPECTED_DATABASE` 一致且以 `my_agent_dr_` 开头；恢复后检查 migration
  version。目标集群仍需实际执行一次恢复、RLS 隔离和 owner 对象重放验收。

## 24 小时真实异构来源 proof

本轮建立三路公开、真实、不同 origin 的 JSON 快照来源：GitHub API、PyPI 和 npm registry，全部使用
正式 `watch_stream` poll 模式、`audit_guarantee=true`、窗口 86400 秒。证据从
`2026-07-10T08:55:42Z` 起写入 `~/.my-agent/scale_evidence/continuous-watch.ndjson`。

真实运行先发现并修复了三处会让 proof 失真的生产问题：

1. owner 发现使用 `owners/**` 深递归，在真实 task/artifact 树上长时间空转；现只遍历规范 owner 层级。
2. `list_states()` 把 `.harvester.json` 当主 watch 快照，重复计数来源；现只接受规范 watch 文件名。
3. 健康只看历史 `pulls>0`，死源也可能永久健康；现要求最近真实源查询时间不超过新鲜度上限。

进程已注册为用户 LaunchAgent `com.my-agent.continuous-monitor`，崩溃自动重启，30 秒采样，默认门仍是
86400 秒、3 路健康保证源、至少 2 个异构签名、最大证据间隔 120 秒、最大源陈旧 900 秒。当前运行中，
尚未满 24 小时，必须保持 `proven=false / duration_too_short`，不能提前改成通过。

## 参考核对

- LangGraph checkpoint-postgres `langgraph/store/postgres/base.py`：namespace + key 的唯一事实表、显式
  migration 和并发索引；本实现映射为 tenant/owner/path 清单与独立 migration role。
- AgentScope `src/agentscope/app/storage/_base.py`、`_redis_storage.py`：先定义 owner/user 作用域的正式
  存储边界，再由后端实现；本实现把不可绕过边界放在 scale worker 执行前后，而非 prompt。
- 通道运行时 `src/config/backup-rotation.ts`：备份写入顺序、权限和保留是显式运维语义；本实现采用
  “对象先成功、清单后原子提交”，并把数据库备份与隔离恢复分开。

这些参考用于确定 chokepoint、命名空间、迁移和恢复顺序；没有复制其本地存储实现。
