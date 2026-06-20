# my-agent 企业级规模化设计(10k–100k 并发)· 调查 + 方案 + 资源预计

> 目标:支撑 10,000–100,000 并发企业用户。原则:**能自建就自建、不能的才借库;稳定性第一**。
> 现状(实测):SQLite+WAL(单写)/ 文件锁 fcntl·flock(单机)/ ThreadingHTTPServer(线程每请求,~千级耗尽)/ 无 redis·异步·分布式。**实测承载约低千级并发,离 10 万差 1–2 个数量级。**

---

## 0. 总判断
my-agent 架构的**形是企业级**(Gateway 中心 / 多租户 / 队列 / 审计 / 限流概念),但**基座是单机单进程**。到 10 万并发是**地基级 re-platforming**,Tier 0 是物理墙。
**关键经济学结论(资源预计先行)**:10 万并发下 **LLM API 成本与 provider 限流是绑定约束,不是 Python 基座**——基座是入场券,Tier 3(LLM 层)才是规模经济所在。

---

## 1. 自建 vs 借库 决策表(按你"能自建就自建"原则逐项定夺)

| 维度 | 决策 | 理由(稳定性第一) |
|---|---|---|
| 存储 SQLite↔PostgreSQL 抽象 | **借 SQLAlchemy Core** | 多 dialect 差异(自增/参数风格/UPSERT/事务)难自建得稳;SQLAlchemy 自动处理、claw 已验证。这是"难自建得稳→借库"的正当 case |
| Postgres 驱动 | **借 psycopg(v3)** | 驱动不能自建 |
| 连接池 | **借 pgbouncer**(进程外)或 SQLAlchemy pool | 10 万用户≠10 万连接,池化必需 |
| 异步 HTTP serving | **asyncio(stdlib 自建事件循环)+ 借 uvicorn**(生产 ASGI) | asyncio 是 stdlib;HTTP/1.1 解析/keepalive 的生产级服务器借 uvicorn 更稳 |
| 分布式锁 | **自建**(PG advisory lock 薄封装)/ 备选 Redis | PG advisory lock 是 SQL,薄封装可自建;已上 PG 就不引 Redis |
| 缓存 | **借 Redis**(client 库)| 分布式缓存/计数/去重不能自建;但 client 是薄层 |
| 分布式队列 | **自建**(PG `FOR UPDATE SKIP LOCKED`,学 claw)| 入站队列用 PG 行锁自建,claw 实测 4204/秒;量级真撑不住再上 Redis Streams/MQ |
| 向量 ANN | **借 pgvector**(PG 扩展)| 百万级向量 ANN 索引不能自建得好;pgvector 复用 PG,不引 Milvus 服务 |
| 限流/熔断/背压 | **自建**(my-agent 已有 28 文件基础)| 算法经典,已有基础,自建 |
| 可观测 指标/追踪 | **借 prometheus_client + OpenTelemetry SDK** | 标准协议,SDK 是薄层 |
| 容器/编排 | **借 Docker + K8s** | 不是代码,是部署基础设施 |
| 密钥 | Phase1 文件级 → **借 KMS/Vault 接口**(规模隔离) | 真多租户密钥隔离需 KMS(claw 自己也这么说) |

**新增必需依赖估计**:SQLAlchemy、psycopg、redis、uvicorn、prometheus_client、opentelemetry(+ 可选 pgvector/KMS)。从当前 2 个 → ~8 个。**这是规模化的必要代价**(全是"难自建得稳"的标准件,符合原则)。

---

## 2. 分层路线(按"不做就到不了"排序)

### Tier 0 · 硬阻塞(物理墙)
- **0.1 存储抽象 + PostgreSQL**:引入 `StorageBackend` 抽象(SQLAlchemy Core),SQLite=本地/开发默认、PostgreSQL=规模。**稳定性第一→增量可回退:先建抽象seam不动现有逻辑,逐仓储迁移,SQLite 路径永不破。** 编码 claw 的并发血泪教训(`BEGIN IMMEDIATE`/busy_timeout/WAL 重试)。
- **0.2 异步 HTTP/ASGI**:gateway HTTP 层 ThreadingHTTPServer → asyncio+uvicorn;请求路径异步化。**可回退:保留同步 gateway,新增 ASGI 入口并行灰度。**
- **0.3 分布式锁**:文件锁 → PG advisory lock 薄封装;锁接口抽象,单机回退文件锁。

### Tier 1 · 水平扩展使能
- **1.1 无状态 worker**(12-factor:状态全进 PG/Redis,本地零状态)
- **1.2 分布式入站队列**(PG SKIP LOCKED,claim/lease/lane/去重/死信跨实例)
- **1.3 Redis 缓存层**(session/限流计数/去重热数据)
- **1.4 负载均衡 + 健康检查 + 优雅停机**(滚动部署)

### Tier 2 · 规模化记忆/检索
- **2.1 向量库 → pgvector ANN**(我 Phase 2 暴力 cosine 接口已留好,平滑换后端)
- **2.2 多租户硬隔离 + 资源配额**(命名空间/配额/防噪声邻居)

### Tier 3 · LLM 层(规模经济核心)
- **3.1 LLM 请求队列/批处理/每租户限流/连接池/退避**
- **3.2 每租户 token 预算 + 成本控制 + 计量计费**

### Tier 4 · 可观测/可运维
- **4.1 指标(Prometheus)+ 分布式追踪(OTel)+ 日志聚合**
- **4.2 自动伸缩** · **4.3 密钥 KMS/Vault**

### Tier 5 · 部署
- **5.1 容器化 + K8s** · **5.2 零停机 DB 迁移**

---

## 2.5 实现状态(enterprise-features 分支,均真测 + 代码体量闸 strict_scope=0)

| Tier | 件 | 状态 | 提交 | 测试(真测) |
|---|---|---|---|---|
| 0.1 | 存储抽象 SQLite↔PG + PG 池调优 | ✅ | `08a21bc9`/`7b6c5531` | 6,真 PG |
| 0.2 | 异步 ASGI 入站层 | ✅ | `648ea784` | 10,TestClient |
| 0.3 | 分布式锁 PG advisory | ✅ | `edfe2bb4` | 4,真 PG |
| 1.1 | worker 池(心跳续租) | ✅ | `aba6301f` | 4,并发 |
| 1.2 | 持久化入站队列 SKIP-LOCKED | ✅ | `30c071e9` | 7,真 PG+SQLite |
| 1-飞书 | 加密回调 AES 解密 | ✅ | `ba16ae3f` | 6 |
| — | 两层架构端到端集成 | ✅ | `5af95c81` | 2,真链路 |
| 2.1 | pgvector ANN 向量库 + 多租户 | ✅ | `2c8f4e80` | 4,真 pgvector+HNSW |
| 2.2 | PostgreSQL RLS 行级硬隔离 | ✅ | `5b2ced85` | 1,真 PG+非超级用户 |
| 3.1/3.2 | LLM 准入:限流+token 预算+并发 | ✅ | `7b894514` | 11,注入时钟 |
| 4.1 | 自建 Prometheus 指标 | ✅ | `91fe502f` | 7 |
| 4.2 | 分布式追踪 W3C traceparent | ✅ | `338b9794` | 7,真队列传播 |
| 5.1 | K8s 零停机 + KEDA 队列扩缩 + 优雅退出 | ✅ | `4ccc9095` | 14 |

**做了三家(claw/长期助手/通道运行时)集体没做的差异化点**(研究子代理逐行核验):pgvector HNSW ANN、PostgreSQL RLS、队列版 traceparent 传播(否则 worker span 是孤儿)、KEDA 队列深度扩缩、真 RollingUpdate 多副本零停机(worker 无状态解掉 通道运行时 单副本约束)、PodDisruptionBudget。

**待外部依赖落地(非代码缺口)**:① Redis 共享态限流器(跨副本一致限流/全局并发;本地无 Redis,单实例内已是权威)② Tier 5.2 在线 expand-contract 零停机 DB 迁移(需多副本灰度环境验证)③ 接 Jaeger/Tempo 做 trace export(自建 traceparent 已是 OTel 兼容格式,借 opentelemetry-sdk 即可)。

---

## 3. 资源预计(10 万并发"在线"用户)
**关键假设**:并发"在线"≠并发"在跑";活跃发请求约 ~10% → **~1 万在飞请求/任务**;agent 任务 **LLM-bound**(每轮数秒,等 LLM)→ 并发由在飞 LLM 调用主导,是 I/O-bound(异步擅长)。

| 层 | 估算 | 说明 |
|---|---|---|
| API/HTTP(ASGI) | **8–16 实例 × 4–8 核** | 异步单 worker 顶数千连接;10 万持久连接 + 1 万在飞轻松 |
| Agent worker | **20–50 实例** | LLM-bound,一个 worker 异步持多个在飞 LLM 调用 |
| PostgreSQL | **主 1 + 读副本 2–4 + pgbouncer** | 池化到数百后端连接;写峰 1–5 万/秒→需分区/按租户分片(高端) |
| Redis | **集群 3–6 节点** | 热路径 session/限流/去重 |
| 向量(pgvector) | **随 PG 或独立 PG** | 百万级向量 ANN |
| **基座月成本(粗)** | **~$20k–80k/月** | 计算+DB+Redis+网络+K8s |
| **LLM API 月成本** | **可能 $100k–$1M+/月** | ⭐ **绑定约束**:1 万持续在飞 × token 量,远超基座成本 |

→ **资源预计头号结论**:**基座成本可控,LLM 成本是数量级更大的真约束**。所以 Tier 3(每租户 token 预算/限流/成本控制)不仅是工程、更是**生意能否成立**的关键,优先级应提前。

---

## 4. 三家参考(claw / 长期助手 / 通道运行时)· 研究子代理实读源码结论(均附文件:行号)

**定位**:**claw = 规模化的 my-agent 蓝本**(它已 FastAPI/uvicorn+SQLAlchemy+PG SKIP-LOCKED,`db.py:66,89` 注释自承"学 长期助手 ★4 / 通道运行时 教训")→ 地基 = 移植 claw + 补缺口。长期助手=单机 SQLite 并发调参/优雅停机最佳范本;通道运行时=飞书 CardKit 流式 + readiness/云原生最佳范本。

| 维度 | 最优来源 | my-agent 决策 |
|---|---|---|
| 1 存储 | claw(唯一真双后端) | 借 SQLAlchemy Core;**补 claw 缺的 PG 池调优(pool_size/max_overflow/pre_ping)+ PgBouncer** |
| 2 锁 | claw(PG SKIP-LOCKED+advisory) | 自建 DB 原语;**移植 长期助手 jitter 重试到 SQLite 默认抗 convoy** |
| 3 HTTP | claw(异步接入+DB队列+无状态worker两层) | 借 uvicorn+自建 worker;CPU 密集步骤挪进程(GIL) |
| 4 入站队列 | claw `IngressQueue`(持久+多实例+lease/墓碑/lane) | 自建;移植 通道运行时 timeout 驱逐 |
| 5 飞书 | 通道运行时(CardKit sequence 流式+持久 dedup)+ claw(双传输+AES 解密) | 借 lark_oapi WS+自建逻辑;移植 通道运行时 流式卡片+长期助手 自适应退避 |
| 6 限流/熔断 | claw(熔断+两级背压)**但缺 RPS 限流** | 熔断自建;**借 Redis 共享态令牌桶补 RPS 限流**(进程内限流跨副本失效) |
| 7 可观测 | claw(`/metrics` 手写)+ 通道运行时(W3C trace) | 指标自建;借 OTel SDK 追踪;补 RED 指标+per-session 日志 |
| 8 部署 | 通道运行时(readiness 503+云原生)+长期助手(60s drain) | 自建+借 tini/K8s;补 readiness 探针+用户感知 drain |

**4 件"三家都没有、必须从零建"**(真 re-platforming 工作量)进度:① Redis 共享态限流器 ⏳(待 Redis;单实例内准入已自建 `7b894514`)② 真数据面多租户隔离 ✅(pgvector 租户列 `2c8f4e80` + PG RLS `5b2ced85`)③ K8s/Helm + readiness-gated 滚动 ✅(`4ccc9095`,RollingUpdate maxUnavailable:0 + readyz 门 + preStop + KEDA + PDB)④ 在线 expand-contract 零停机迁移 ⏳(需多副本灰度环境)。**4 件已落 2 件,另 2 件卡在外部基础设施(Redis/灰度集群),非代码缺口。**

**纠正**:claw 宣称的 "4204/秒 exactly-once" 是**单机 SQLite 基准**,非 PG 吞吐。

---

## 5. 执行顺序建议
**Tier 0.1 存储抽象(增量可回退 seam)→ 真机接 PostgreSQL 测 → 0.2 异步 → 0.3 锁 → Tier 1 …**
每步:稳定性第一(可回退、SQLite 路径不破)、真实测试(含真 PostgreSQL)、不破现有全量。

---
*调查来源:claw 源码、SQLAlchemy/Uvicorn 官方与社区最佳实践(见对话 web 调查)、研究子代理三家深析。*
