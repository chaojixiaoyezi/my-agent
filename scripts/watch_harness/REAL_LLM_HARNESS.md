# 真-LLM 盯守自测台交接说明(real_llm_latency_harness.py)

给测试方接管用。判读**全部真调 MiniMax-M2.7**(禁止模拟判读),量的是"真事发生 → 真模型判完 → 上报"的端到端延迟。这台专门补上旧 `latency_and_overload_harness.py` 照不出的真机判读瓶颈——旧台把判读模拟成"瞬间+完美读答案键",所以只量到管道延迟(~1.4s),量不到真模型每条判读几秒~几十秒+排队(真机 7–18 分的真来源)。

## 怎么跑

```bash
# Mac 环境里有 AGENT_API_KEY;缺失直接退出码 2,绝不退化成模拟判读
AGENT_API_KEY=... python3 scripts/watch_harness/real_llm_latency_harness.py
```

常用参数(默认已调到"一次 ~10–25 分钟、~120–200 次模型调用"):

| 参数 | 默认 | 说明 |
|---|---|---|
| `--sources N` | 5 | 盯守源数。**这是场景实例数不是上限**——判读工数按积压动态定,不写死源数/工数 |
| `--latency-secs S` | 24 | 场景① 活流喂料时长 |
| `--rate R` | 3 | 场景① 每源每秒事件数 |
| `--overload-backlog N` | 200 | 场景② 每源存量洪峰 |
| `--decoys-per-hit D` | 8 | 问题流里 decoy:hit 比(调大=真事更稀、更省钱) |
| `--scenarios 1,2,3` | 全跑 | 只跑指定场景 |
| `--serve-only` | — | 只把 N 个真源起在固定端口(灌存量)不判读,给你 curl 勘查环境 |

## 运行时间/可靠性(重要:真 M2.7 判读很慢)

真 M2.7 是推理模型,认真判一批 ~50 条候选**一次调用要 45–52 秒**(它逐条读正文语义)。所以:

- **一次完整 5 源三场景跑 ≈ 15–25 分钟**,长跑期间**容易被系统在高负载时 kill**(后台进程)。
- **建议分场景单跑**(`--scenarios 1` / `--scenarios 3`)、或减源数(`--sources 3`),每段短、不易被杀。
- 判读工判空**不早退**(feed_done 门控):活流喂料在爬坡时判读工必须一直判到喂完+判空才收工,否则爬坡前见空源早退会漏判(已修的 race)。若你看到某臂 `model_batches=0`,那是该段**被 kill 或 wall-time 不够**(单批 45–52s,几批就几分钟),不是判读管道坏——分场景重跑即可。
- 不间断实测(单跑场景①,2 源)确认修复臂**交付 11/11 真事、中位延迟 60s vs 控制臂 91s**,误报仅 **6**(补了判读纪律后精度 ~62%,远好于缺纪律时的 37–47%);场景③ 重启 **5/5** 全恢复。

## 用哪个模型 + 端点

- `POST https://api.minimaxi.com/anthropic/v1/messages`,`model=MiniMax-M2.7`
- header:`x-api-key=$AGENT_API_KEY` + `anthropic-version: 2023-06-01`(anthropic 兼容端点)
- 每次 pull 出一批候选调一次模型。一次完整跑(3 场景 × 控制/修复两臂)约 **120–200 次调用**,批大小 8–48、每调用 ~5–30s,墙钟约 **10–25 分钟**(真机判读慢正是本台要暴露的)。调用计数见 `run_manifest.json` 的 `model_calls_total`。

## 三个场景各测什么 / 预期输出

| 场景 | 测什么 | 预期(修复臂) | 结果文件 |
|---|---|---|---|
| ① 逐条上报延迟 | 控制=**一个判读工串行扛所有源**(真机头号抱怨的复现) vs 修复=**每源并行判读工**(积压深再按 judge_fanout 加分片工) | 中位/尾延迟**秒级~分钟级**(≤180s/≤600s),且**显著低于控制臂** | `s1_control.json` / `s1_fixed.json` |
| ② 过载不乱报 | 存量洪峰拉爆 | 批量≤quota、**不整批盖章**(rubber_stamp=0)、overload 块在场、精度不崩;控制臂(无背压单工怼整批)精度崩 | `s2_fixed.json` / `s2_control.json` |
| ③ 重启恢复 | 停 harvester+清进程内注册表(模拟网关重启),重开 | **全部 N 源**都被重新驱动续判(每源都有新判读产出) | `scenario3_restart.json` |

终端每场景打印 `[判定]` 与末尾 `VERDICT`。退出码 0=全过。

## 它起了什么 / 监听什么端口(接管环境清单)

**都是本进程内的 daemon 线程/服务,harness 进程退出即随之结束**(见下"durable vs 短暂"):

- **真 HTTP 源服务**:`content_source_simulator` 的中性料(status 恒 200、真假只在 `response.message` 正文语义、真事稀),监听 `127.0.0.1:8951 .. 8951+N-1`。每个 arm/场景跑完会关掉自己的源服务腾出端口给下一 arm(所以跑动中的一刻只有当前 arm 的 N 个端口在听)。
- **harvester 线程**:每源一个进程内后台摄取线程,把源 drain 进 spool。arm 跑完随之停。
- **判读工**:本台起的 **Python 线程**(不是真子代理);修复臂按 pull 回执里的 `judge_fanout.recommended_workers` 用 `shard_index`/`shard_count` 起**多个并行判读工**判同一路 spool——这正是产品里 `create_subagents` 一源多判读工要驱动的机制,这里用线程忠实驱动它,**判读仍真调 M2.7**。

## 产物留在哪(固定、文档化路径)

根:`<repo>/data/realllm_watch_harness/`

```
data/realllm_watch_harness/
├─ run_manifest.json                这次跑动了什么、留下了什么、模型调了几次(接管总入口)
├─ s1_control.json / s1_fixed.json  场景① 两臂结果(延迟分位数、误报、模型批次数)
├─ s2_fixed.json / s2_control.json  场景② 两臂结果(精度/召回/盖章轮/overload)
├─ scenario3_restart.json           场景③ 每源重启后续判计数
├─ s1_control/ s1_fixed/ s2_fixed/ s2_control/ s3/
│    └─ answer_keys/*.jsonl          每源真 hit 的旁路答案键(判读工够不着,只当延迟/召回裁判)
└─ owner_*/watch_state/             每 owner 的 watch 快照 + spool.ndjson + 读游标
     ├─ ws-*.json                   watch 持久化快照
     ├─ ws-*.spool.ndjson           后台摄取落盘的候选批
     ├─ ws-*.read.json              单消费者读游标
     └─ ws-*.read.sNofM.json        sharded 判读时每个分片的独立读游标(N=分片号 M=分片数)
```

## durable vs 短暂(哪些留、哪些随进程走)

- **durable(留在盘上,交给你处理)**:上面 `data/realllm_watch_harness/` 下所有文件——答案键、watch 快照、spool、分片游标、逐场景 JSON、manifest。**跑完不删不清**。
- **短暂(harness 进程退出即止,无法"留着运行")**:进程内的 HTTP 源服务、harvester 线程、判读工线程。它们是 daemon 线程,进程一退就没了——这是 in-process 设计的必然,不是遗漏。要一个**活的可 curl 的源环境**,用 `--serve-only`(它会把 N 个源起在 8951.. 并灌存量,Ctrl-C 停)。

## 我没有做、交给你做的后续(按硬要求)

- **不停服务、不删数据、不清环境**:整套盘上产物保持现状。
- 要重跑:直接再跑本脚本(幂等,会重建 owner_* 目录并重新真判)。
- 要清:删 `data/realllm_watch_harness/` 整个目录即可(没有起任何常驻系统服务/cron/detached 进程,无残留进程要杀)。
- 换一批措辞复验:改 `content_source_simulator.py` 的 `_HIT_PHRASES`/`_DECOY_PHRASES` 措辞库再跑——任何硬编码固定串的解法当场失灵,本台判读全交真模型语义、不含固定词黑名单。

## 重要:绝对精度取决于【判读 prompt 有没有喂够纪律】,不是吞吐/批判读/模型的事

初版本台判读 prompt 缺了产品 `PULL_GUIDANCE` 里那条关键决策纪律,测出的精度只有 ~37%,一度被误当成"批判读摊薄注意力"或"模型判不动"。**三个真调 M2.7 探针把根因查清了**(都在 scripts/watch_harness/):

- `batch_vs_single_probe.py`:批判读 11% vs 逐条判 13% → **误报不是批判读 artifact**(逐条判一样低)。
- `few_shot_learning_probe.py`(三臂各跑 2 次,极稳):
  - A 裸判(只说"真假在正文"):精度 **15%**、误报 17
  - B 喂样品(加 6 条带答案示范):精度 **15%**、误报 17 —— **few-shot 没用**
  - C 补判读纪律(加"读到否定/未竟语义即使 200 也【一票否决】+ 宁可漏报别把尝试当命中"):精度 **100%**、误报 **0**

**真根因 = 判读 prompt 缺那条决策纪律**。补上后 M2.7 在同料、同批判读下精度 **15%→100%**。这印证军规:禁裸判就下"模型判不动"结论;判不准先问"喂够判读上下文没有"。

本台 `_SYSTEM` 已灌入该纪律,精度贴近产品真实水平(真机盯守子代理本就带完整 `PULL_GUIDANCE` 判)。所以:
- 场景②判定**不压绝对精度**——精度取决于 prompt 纪律喂没喂够(已喂),不是吞吐/背压修复的目标;只压**确定性的过载降级保证**(钳批≤quota、不盖章、overload 如实在场、控制臂喂更大整批)。
- **误报虚高不是产品缺陷、不是模型短板、不是吞吐修复的锅**——是判读上下文问题,喂对纪律即解。

## 硬要求自查(已内建)

- 判读唯一入口 `_judge_batch → _call_minimax` 真调 M2.7;`AGENT_API_KEY` 缺失直接退出码 2、不跑。
- 延迟把真判读耗时算进去:`latency = judged_at(真模型调用返回时刻) − emitted_at(答案键记的真事发生时刻)`,不是只量管道。
- 去留只由模型语义判(passthrough 直通,不用结构规则/固定词替模型拍板);源料真假只在正文措辞、真事稀。
