# Tier 5 部署:K8s 零停机 + 队列深度自动扩缩

面向 10k–100k 并发的容器化部署。两层架构(异步入站 + 持久化队列 + 无状态 worker)落到 K8s 上的形态。

## 拓扑

```
飞书/IM ──▶ Service(my-agent-ingress) ──▶ [入站 Pod ×N] ──enqueue──▶ PostgreSQL(队列+数据)
                                          (ASGI,按 CPU/HPA 扩缩)         ▲
                                                                         │ SKIP-LOCKED 领取
                                          [worker Pod ×M] ◀──────────────┘
                                          (无状态,按队列深度 KEDA 扩缩)
```

- **入站层**(`ingress.yaml`):`asgi_entry`,只 verify→decrypt→enqueue→立即 ack,**不内联跑 LLM**。按 CPU HPA 扩缩(HTTP 校验是 CPU-bound)。
- **worker 层**(`worker.yaml`):`worker_entry`,消费队列跑 Redis 共享准入 + OTel + RLS 状态写入 + `scale_downstream` 真实 Agent/飞书回复。按**队列待处理积压**用 KEDA 扩缩；启动和 readiness 都必须通过 bwrap 真隔离自检。
- **迁移层**(`migration.yaml`):独立 migration role 先跑 expand migrations；应用 Pod 只读验版本，待迁移即不启动。
- **长守层**(`monitor.yaml`):恢复正式 owner watch，并按真实 wall-clock 追加连续 proof；不调用 `scripts/watch_harness`。
- 两层**分开扩缩**:入站随连接/RPS,worker 随积压——互不绑架。

## 为什么 my-agent 能做到三家都没做的

研究核验 claw / 长期助手 / 通道运行时 后,以下都落在我们 Tier 0/1 地基(PG、Redis、分布式锁、持久化队列、ASGI、worker 池)的延长线上:

| 能力 | claw | 长期助手 | 通道运行时 | my-agent |
|---|---|---|---|---|
| 真 RollingUpdate 多副本零停机 | 无 K8s | 无 K8s | Recreate+单副本 | ✅ worker 无状态(状态在 PG)→ 可放心多副本滚动 |
| `maxUnavailable:0` + readyz 就绪门 + preStop | — | — | 无 readyz 门/preStop | ✅ 新就绪前不减旧、退出先摘流量 |
| 队列深度自动扩缩(KEDA) | 无 | 无 | 无 | ✅ 直读队列表积压,IO/LLM 负载的正确弹性 |
| PodDisruptionBudget | 无 | 无 | 无 | ✅ 防节点维护一次干掉全副本 |
| tini PID 1 + 优雅退出 drain | 应用直作 PID1(漏收僵尸) | tini✅ | tini✅ | ✅ tini + SIGTERM→停领新活→在途跑完 |

## 零停机滚动的机制(应用侧 + 编排侧两道闸)

1. K8s 滚动:`maxUnavailable:0` 保证旧 Pod 在新 Pod `/readyz` 就绪前不下线 → 容量不掉。
2. 优雅退出:Pod 收 SIGTERM → `graceful.install_sigterm_drain` 翻 draining → `/readyz` 转 503 → Service 摘 endpoint → 新流量停。
3. `preStop: sleep 5` 等 endpoint 摘除传播;`terminationGracePeriodSeconds`(入站 60s / worker 120s)给在途请求/LLM turn 跑完。
4. worker 的在途消息靠**心跳续租**(`queue_worker._Heartbeat`)不被 `recover_stale` 误回收。

## 构建与部署

生产执行节点要求：

- Linux 容器；镜像通过系统包内置 bubblewrap，wheel 里的 vendor binary 仅作同架构离线兜底。
- Kubernetes 1.36+，节点/文件系统/CRI 支持 Pod user namespace；worker 使用 `hostUsers:false`。
- bwrap probe 必须真实验证 namespace、owner 写入和隔离外文件不可见。失败 Pod 不 ready，worker 自身也在领取消息前退出。
- 不允许为通过 probe 打开 `privileged` 或宿主级 `SYS_ADMIN`；不能满足时应更换支持的节点/runtime。
- Docker 单机入口使用 `deploy/seccomp-bwrap.json`：它固定自 Moby 官方默认 profile，只额外放行 bwrap 创建内层 user/mount/PID namespace 所需调用；没有使用 `seccomp=unconfined`。
- K8s worker 使用 `Localhost` seccomp；节点供应链必须先把同一文件安装到 `/var/lib/kubelet/seccomp/my-agent/seccomp-bwrap.json`（可由节点镜像或 Security Profiles Operator 分发）。文件缺失时 Pod 应 `CreateContainerError`，不允许退回 RuntimeDefault。

```bash
# .dockerignore 只把生产源码和依赖清单送入 builder，本地 data/memory/logs 不进入 context。
docker build -f deploy/Dockerfile -t my-agent:latest .
# 在最终 runtime/security context 中验 bwrap；必须退出 0。
docker run --rm --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --security-opt "seccomp=$(pwd)/deploy/seccomp-bwrap.json" my-agent:latest \
  python -m agent_py_agent.agent.tooling.sandbox --quiet
# 需先建 Secret（仓库不提供默认生产密码）：
# my-agent-db: url=受限 app role URL, migration_url=DDL/migration role URL
# my-agent-redis: url；my-agent-tenant: id
# my-agent-feishu: encrypt_key, app_id, app_secret
# 还需先把 deploy/seccomp-bwrap.json 分发到每个 execution node 的
# /var/lib/kubelet/seccomp/my-agent/seccomp-bwrap.json。
kubectl apply -f deploy/k8s/config.yaml -f deploy/k8s/storage.yaml
kubectl delete job my-agent-migrate --ignore-not-found
kubectl apply -f deploy/k8s/migration.yaml
kubectl wait --for=condition=complete job/my-agent-migrate --timeout=10m
kubectl apply -f deploy/k8s/ingress.yaml -f deploy/k8s/worker.yaml -f deploy/k8s/monitor.yaml
# worker KEDA ScaledObject 需集群已装 KEDA。
```

正式 scale 配置把 `WORKER_HANDLER` 固定为统一准入门、`WORKER_DOWNSTREAM` 固定为内置真实 Agent；
两者任一缺失都会阻断启动。LLM 配额经 env(`LLM_TENANT_RPS` / `LLM_TENANT_TOKEN_BUDGET` /
`LLM_TENANT_USD_BUDGET` / `LLM_MAX_INFLIGHT`)并由 Redis 跨副本共享。

当前 Agent 的 owner memory/task/artifact 仍是文件事实源，所以 worker 挂 RWX `/data`；这不是“全状态
已迁 PostgreSQL”。目标 10 万用户仍需验证 StorageClass 小文件规模、快照/恢复、热点 owner 与分片。

单机用户不需要手工执行上述命令；根目录 `install.sh` 默认完成 build、probe 和透明 CLI 包装。
