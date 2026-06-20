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
- **worker 层**(`worker.yaml`):`worker_entry`,消费队列跑 `worker_handler`(Tier 3 准入 + Tier 4 追踪 + 下游 agent)。按**队列待处理积压**用 KEDA 扩缩。
- 两层**分开扩缩**:入站随连接/RPS,worker 随积压——互不绑架。

## 为什么 my-agent 能做到三家都没做的

研究核验 claw / 长期助手 / 通道运行时 后,以下都落在我们 Tier 0/1 地基(PG、分布式锁、持久化队列、ASGI、worker 池)的延长线上:

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

```bash
docker build -f deploy/Dockerfile -t my-agent:latest .
# 需先建 Secret:my-agent-db(key=url 为 DATABASE_URL)、my-agent-feishu(key=encrypt_key)
kubectl apply -f deploy/k8s/ingress.yaml
kubectl apply -f deploy/k8s/worker.yaml   # 需集群已装 KEDA(https://keda.sh)
```

真实 agent 主循环经 `WORKER_HANDLER='module:func'` 注入 worker(见 `worker_handler.set_downstream`);默认 stub 先把链路跑通。LLM 配额经 env(`LLM_TENANT_RPS` / `LLM_TENANT_TOKEN_BUDGET` / `LLM_MAX_INFLIGHT`)。
