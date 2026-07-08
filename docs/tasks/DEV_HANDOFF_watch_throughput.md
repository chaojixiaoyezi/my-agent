# 交接:盯守判读吞吐 / 过载不乱报 / 重启全驱动(接 49d1a09b 之后一棒)

> 领域无关的通用数据研判能力问题,全程中性视角。本文给测试方独立复验用。
> 详细根因+对照证据见 `evidence/watch_throughput_backpressure/`。

## 一句话

R4 把"候选不丢/停前不弃/重启部分复活"修好了,但真机暴露**头号问题 = 判读吞吐**:
passthrough 五源把整条流(各 ~5000)倒进 spool、消费追不上 → 低速率真事**报不出**(延迟极高)、
高速率**过载 rubber-stamp 狂报垃圾**(115 误报=一批候选整车当命中倒出)。P2 重启只恢复 2/5 源。
**本棒:背压钳住 spool + 记录配速到一批 + 过载如实标注 + 重启补全非终态卡死源。** 全结构化、
零关键词、遵铁律(只调速/如实告知,绝不用结构规则替模型对"正文分不开真假"的事做去留)。

## 改了什么(4 处,keystone=背压)

1. **背压**(`harvester`,`config.spool_backpressure_factor=8`):content_mode(passthrough/冷启动
   全量直通)未读积压达 `8×judge_quota` → 收割本拍不 drain(游标不动、只续租约、`backpressure_skips++`),
   消费推进读游标即释放。append-only 源=延迟无损;滚动缓冲源=如实记 gap。**只对 content_mode 生效**
   (结构化判据源候选稀疏、不洪泛、不背压)。
2. **记录配速**(`harvester.content_batch_size`):content_mode 每条 spool 记录 ≤ 一批(48),
   per-cycle drain 也钳到 room——模型每 pull 不再被怼 500 条整片(治 rubber-stamp)。
3. **过载如实标注**(`watch_payloads.attach_overload_note`):未判积压 ≥ 一个判读口粮 → pull 载荷挂
   `overload` 块(积压 N/背压中/"别为追进度批量乱报、宁可如实留积压、要提速去增派子代理或学判据收窄")。
4. **重启全驱动**(`watch_lane_sweep._lane_stuck_nonterminal`):非终态(PENDING/PLANNING/BLOCKED)
   puller + 消费停摆 + 不活 + 复活拉不起来(attempt 上限/能力闸排除)→ 建接管;可复活/在岗的不抢。

churn 自查(R3~54%→R4~36%):R4 收口逻辑无 bug,只是终点定在"积压清零",而 R4 缺背压→积压
永不清零→闸永远 fire。背压补上后积压可清零、闸到终点、churn 收敛。**不需要回退 R4。**

## 怎么复验(你会换一批措辞的料)

- **料**:`scripts/watch_harness/content_source_simulator.py`(中性:status 恒 200、真/假只在
  response.message 语义、真事稀、since=0 存量;换措辞只改 `_HIT_PHRASES/_DECOY_PHRASES`)。
- **隔离自测(本机跑过,rc=0)**:
  - `latency_and_overload_harness.py`:①合理速率 15/15 上报、中位延迟 **1.4s**、零误报;
    ②过载修复后 FP=0/批量≤48/spool 有界(352)/overload 在场,**对照(修复前)FP=1888/批量 500/spool 1500**。
  - `restart_lane_drive_harness.py`:五源(终态+两种非终态卡死)全驱动,可复活/在岗不抢;
    **对照(旧只认终态)L1/L2 无接管=真机 2/5 复现**。
  - R4 台复跑未破:`consumption_pressure_harness`(积压钳到 ~384、清零、零孤儿)、`restart_recovery_harness`。
- **真机怎么摆**:5 源 passthrough、给足消费时间冻结后再对账。看三样:①逐条上报延迟(合理速率
  应秒~分钟级,不该十几分钟/报不出);②过载别狂报(判不过来时 pull 载荷 `overload` 块在场、
  子代理该如实标"积压 N 未判"而非批量倒候选 id);③进程整体重启后**所有**在盯源都续跑(不是一部分)。
- **注意**:判读准确率(P4 边界案例方差)非本棒目标——harness 用完美读者隔离管道;真机判读方差
  仍靠"存疑倾向报"兜。任何硬编码固定串的解法当场失灵——本棒零硬编码。

## 守约
- 未碰测试方 192.168.1.9 / 192.0.2.10(只读查看盘上证据,严禁改动),自起隔离环境自测。
- 本地绿 ≠ 真机过:待你换措辞料独立复验。
