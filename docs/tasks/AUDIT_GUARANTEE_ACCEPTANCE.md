# /audit 逐条保证档 —— R7 验收报告(判读工问责)

> 领域无关表述:一个多消费者的 durable 队列,worker 逐条问真模型要结论再签收(ack-on-judge)。
> 治的是 R6 头号崩点——36 判读工 11 空壳、召回 4%:旧签收是 ack-on-next-pull,空转 worker
> 再 pull 一次就把没判的在途批"确认"掉=真命中被静默吃。

## 1. 根因与修法(一句话)

| R6 崩点 | 根因(机制层,非模型) | 本轮修法 |
|---|---|---|
| 11/36 判读工空壳、召回 4% | 签收绑"再 pull 一次"而非"真判完" → 空转工抽干游标不判 | **ack-on-judge**:签收只在逐条结论交齐后推进,空转工推不动任何账 |
| 有损筛在 pull 前吞真事 | 引擎 triage 对必查源也筛 | 保证档**关有损筛**:normal 规则命中也逐条入队(只记账不减负) |
| 判读慢 → 源端滚动缓冲淘汰=真丢 | 抬取按判读积压背压,慢了就不抬,流留源端被淘汰 | 保证档抬取**只按磁盘水位**背压,判读慢=spool 涨(不丢) |
| 重启只 3/5,落后源躺平 | 恢复按"谁在健康跑"驱动、要求"有窗" | 恢复按"**谁有未判积压**"驱动,无窗长守/冷启动路都恢复 |
| 无覆盖证明,用户须重审 | 无对账口径 | **覆盖回执**:入队 X·已判 Y·待判 M·丢弃 0,随时可查,丢弃恒 0 |

## 2. 契约(每条都在机制层成立,不靠信任模型)

1. **全量入队**:该源每条原样入 durable 磁盘队列(现有 spool),关引擎有损抬。
2. **逐条判 + 弹性伸缩**:按积压 fanout(judge_fanout 动态推荐工数,不写死),每条过模型。
3. **判完才签收(ack-on-judge · 头号根)**:游标/ACK 只在"该条真判完 + 记了结论"后推进。
   空壳工推不动游标 → 任何一条不被静默吃。
4. **来不及也不许缺/瞎搞**:落后只表现为延迟(队列涨、诚实挂 pending),绝不丢、绝不盖章。
5. **契约跟数据继承**:保证档标志随 watch 持久化,任何后来消费者(子代理/孙代理/重启新进程)
   冷加载都拿到契约;ack-on-judge 在 spool 层强制,哪层想跳过都推不动游标。
6. **覆盖回执**:丢弃恒 0 是硬约束(>0 亮红=有 bug);待判>0 只表示还在判、不是漏。
7. **数据格式对齐(前置)**:/audit 前 sample+configure 学判据——只为判得准,绝不变有损筛。

## 3. 落点(改了哪)

| 文件 | 改动 |
|---|---|
| `ingestion/harvester.py` | ack_id 令牌 + pending_acks 欠账 + `submit_verdicts`(ack-on-judge)+ `audit_receipt_facts`(回执)+ verdict 台账 + 磁盘水位背压 + 归档轮转 |
| `ingestion/engine.py` | `process(guarantee=True)`:full_read 无条件、normal 规则命中也逐条入队 |
| `ingestion/watch_tool.py` | `action=verdict` 结论签收 + open 认 /audit(参数/任务文本斜杠词元)+ 保证档强制 spool 路 + 回执挂载 |
| `ingestion/watch_state.py` | `audit_guarantee`/`audit_baseline` 字段单调持久化 + 原子写 tmp 名唯一化(并发竞态) |
| `ingestion/watch_payloads.py` | `AUDIT_PULL_GUIDANCE` 契约文本 + `attach_audit_receipt` + fanout 契约下传句 |
| `ingestion/wake_backstop.py` | 恢复按未判积压驱动(去窗口门 + 回落 opened_by 兜冷启动路) |
| `ingestion/config.py` | `max_judge_workers` / `guarantee_spool_max_mb` / `guarantee_min_disk_free_mb` |

## 4. 单测(机制层,全绿)

`agent_py_agent/tests/test_watch_audit_guarantee.py`(22 项):空壳抽不干游标 / 部分销账 /
换人带欠账 / 未知令牌不入账 / 回执对账 / 台账 / 归档 / 磁盘水位 / 棘轮 / 契约冷加载继承 /
inline 不回落。`test_watch_wake_backstop.py` 补:无窗落后源恢复 + 冷启动路回落 opened_by。
全量套件 rc=0 无回归。

## 5. 真机 M2.7 验收 A–G

**判读唯一入口真调 MiniMax-M2.7,禁模拟**(缺 AGENT_API_KEY 直接退出 2)。
台:`scripts/watch_harness/audit_guarantee_harness.py`;产物:`data/audit_guarantee_harness/`。

### 5.1 quick 档(3 源,先验管道)—— 7/7 PASS

| 场景 | 关键数字 | 判定 |
|---|---|---|
| A 无空壳 | 入队60·判60·待判0·丢弃0;空壳工签收 **0**、其分片被真工接判 | ✓ |
| B 不静默吃 | worker 判1批猝死,在途批重投,**2/2 目标记录全被判到** | ✓ |
| C 重启 3/3 | 落后源[2]重启后恢复续判,**3/3 全判完** | ✓ |
| D 回执+召回 | judged=enqueued=80·丢弃0;真模型召回 **67%**(R6=4%) | ✓ |
| E 月级缩比 | 归档已生成、活跃 spool 峰值 **<18KB**(总入队120)、回执 dropped 恒 0 | ✓ |
| F 过载不瞎搞 | 灌后待判如实=40·丢弃0;判一段 **rubber_stamp=0·精度100%** | ✓ |
| G 契约继承 | fanout=2 动态;台账逐条结论 **90=已判90**·丢弃0 | ✓ |

### 5.2 完整档(5 源)—— 权威数字,7/7 全 PASS(2026-07-08 额度刷新后重跑)

`run_manifest.json`:model_calls=23、wall≈656s、**all_pass=True**。

| 场景 | 权威数字 | 判定 |
|---|---|---|
| A 无空壳 | 入队60·判60·待判0·丢弃0;空壳工签收 **0**、其分片被真工接判 | ✓ |
| B 不静默吃 | 猝死批重投,**2/2 目标记录全被判到**·丢弃0 | ✓ |
| C 重启 **5/5** | 落后源 **[2,3,4]**(3 源)重启后全恢复续判到 judged=enqueued·丢弃0 | ✓ |
| D 回执+召回 | judged=enqueued=80·丢弃0;真模型召回 **100%**(9/9;R6=4%) | ✓ |
| E 月级缩比 | 归档已生成、活跃 spool 峰值 **17.8KB**(远小于总入队)、回执 dropped 恒 0 | ✓ |
| F 过载不瞎搞 | 待判如实涨·丢弃0·**rubber_stamp=0·精度100%** | ✓ |
| G 契约继承 | fanout=2 动态;台账逐条结论 **90=已判90**·丢弃0 | ✓ |

**C 5/5 含 3 落后源(重启前 [2,3,4] 有未判积压)全部恢复续判**——正是"重启恢复必须 5/5 含
落后源"这条硬指标(R6 只 3/5)达成。D 召回 100%、F 精度 100%,与"模型逐条判语义本就准、
缺口在机制不在模型"一致。

> **一段插曲(如实存档):** 首次完整档跑到 C/D 时撞上 MiniMax 额度耗尽,C 得 3/5、D 召回 0,
> 但当时 `judged=enqueued·dropped=0·覆盖闭合`仍成立、A/B/E 照过——失败只落"模型质量"维度、
> 机制维度没塌。额度刷新后重跑即上表 7/7,反证了那次是额度问题非机制回归。
> **本次权威重跑由审阅方 agent(独立会话)在同机交叉执行本 harness 完成**(共享 repo/产物),
> 与实现方 quick 档 7/7 相互独立印证。

### 5.3 H 真产品激活(测试方 1.9 网关实测挖出的缺口 → 已修)

**缺口(测试方真机实测):** 发 `/audit 盯这5个API` 真任务,盯守委派给子代理做,9 分钟后 6 个
watch 的 `audit_guarantee` 全 = None、verdicts=0——**/audit 从没激活、整轮跑默认 triage**(用户
以为开了零丢弃、实际没开=最危险的静默失效)。A–G harness 与队列单测都漏了这条,因为它们
**直接 `set audit_guarantee=True` 绕过了激活层**。

**根因:** 旧激活只查 `_current_user_prompt` 与子代理 `goal` 的 `/audit` 词元,但真路径三条词元路
全失真——子代理 goal 空(/audit 在 runner_prompt 里)、后台唤醒轮 root_user_prompt 被回填成机器
拼 prompt、主代理没显式传 audit 参数。

**修法(结构化确定性激活,不靠模型传参/不靠词元落 goal):** 见 §2 与 `common/audit_activation.py`——
`/audit` 意图在前台创建路一次性盖进 `task_attributes["audit_guarantee"]`,此后跨轮/跨 spawn 树靠该
结构化标志继承(gateway 盖章 → create_subagents 继承 → 子代理 runner 透传 → watch 消费),后台轮
残留边界用 owner 持久棘轮反推兜住;pull/status 常驻档位布尔可观测。

| H 子项(机制维度,单测走真激活路径非绕过) | 覆盖 |
|---|---|
| task_attributes 结构化标志激活(无参数无词元) | ✓ |
| 子代理**空 goal** 仍激活(测试方实锤场景回归) | ✓ |
| 契约沿 spawn 树继承(父保证档→子 task.attributes 自动带) | ✓ |
| gateway 前台盖章 / 后台唤醒轮持久棘轮反推延续 | ✓ |
| 默认档不误开 + 档位一眼可见 | ✓ |

`tests/test_audit_activation.py`(11 项)+ 全量 rc=0。**真网关→主代理→委派子代理端到端 H(带真
M2.7)由测试方 1.9 网关环境复验坐实**——本侧机制维度已确定性覆盖,激活开关已接通。

## 6. 取舍(如实告诉用户)

- 逐条判每条烧一次模型 → 贵。中速流(API 盯守)扛得住;真·洪水速率模型追不上 → 队列
  **涨(不是丢)**、持久滞后。别为追速把有损筛偷偷加回来。
- 两档分开:洪水流量走默认档(triage 有损、看大概);`/audit` 只对"必须一条不漏"的源开。

## 7. 测试环境(留盘交接)

- 产物目录:`data/audit_guarantee_harness/`(run_manifest.json + 逐场景 *_result.json +
  answer_keys/ + owner_*/watch_state/ 快照/spool/游标/verdict台账/归档),跑完不删。
- 复跑:`AGENT_API_KEY=... python scripts/watch_harness/audit_guarantee_harness.py [--quick]`。
- 定性锁死(禁改方向):模型逐条判语义本就准(直喂单条+混批量都对);缺口在判读工机制层,
  **禁加词典/关键词分类器**救召回。
