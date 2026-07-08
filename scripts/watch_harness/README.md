# 监控研判编队测试台

本目录两套工具:**正式测试台**(多源异构+结果端判真,能力一的长期压测资产)和 **M1 最小版**(单文件蹲守)。

## 判读吞吐 / 过载 / 重启 隔离自测(2026-07-08 新增,接 49d1a09b 之后)

领域无关的通用数据研判自测(中性料 `content_source_simulator.py`,不需真机、不需真调模型;
判读按真机行为建模"批量≤capacity 精读、>capacity 判力淹→整批 rubber-stamp")。均自带修复前对照。

- `latency_and_overload_harness.py`:①合理速率逐条上报延迟(应秒级)+ ②过载不乱报
  (修复后每 pull 批量≤48/FP=0/spool 有界/overload 标注在场;对照修复前批量 500/FP 暴涨/spool 无界)。
- `restart_lane_drive_harness.py`:进程重启后**所有**在盯源重新驱动(终态+非终态卡死都接管;
  可复活/在岗的不抢);对照旧只认终态 → 复现"5 源只 2 源恢复"的夹缝。
- `consumption_pressure_harness.py` / `restart_recovery_harness.py`:R4 台(背压后复跑未破)。

```bash
python3 scripts/watch_harness/latency_and_overload_harness.py   # 场景①②(约 40s)
python3 scripts/watch_harness/restart_lane_drive_harness.py     # P2 全驱动 + 对照
```
交接见 `docs/tasks/DEV_HANDOFF_watch_throughput.md`,根因/证据见 `docs/tasks/evidence/watch_throughput_backpressure/`。

## 正式测试台(对应交接文档「可复用长期测试台」节,2026-07-02 新增)

- `multi_source_simulator.py`:一进程起 N 个 HTTP 源(默认 5 源 8901-8905、每源 ~100 条/秒),
  schema 各不相同、每源 `GET /pull?since=<游标>&limit=<n>` 游标续读;**所有事件都带唯一 ID**
  (id 存在性不泄真假),绝大多数候选"触发端像命中、结果端否定"(迷惑),真命中极稀疏且**只能从
  结果/响应端字段判**;真命中同步写旁路 answer-key。
- `fleet_score.py`:扫会话 jsonl/产物文本里出现的事件 ID,对账 answer-key → 命中/漏报/误报 +
  端到端延迟(消息时间戳优先,mtime 兜底)。

```bash
# 数据源机(与运行机同局域网;先验证运行机可 LAN 直连本机端口):
python3 scripts/watch_harness/multi_source_simulator.py \
    --base-port 8901 --sources 5 --rate 100 --duration 1260 --hits 22 \
    --answer-key /tmp/watch_answer_key.jsonl

# 运行机投任务(要求主代理派编队、各盯一路、结果端判真、命中即上报事件ID):
#   注意:5 个源是【内网地址】,主代理需先经用户确认用 authorize_network_host 授权,
#   子代理的 web_fetch 才过出站闸(N1 接线,详见交接文档 §2/§8)。

# 跑完对账:
python3 scripts/watch_harness/fleet_score.py --answer-key /tmp/watch_answer_key.jsonl \
    <owner_home>/.../conversations/messages/ <owner_home>/tasks/
```

---

# M1 单流蹲守复现工具(最小版)

对应交接文档 `docs/tasks/DEV_HANDOFF_orchestration_reliability.md` 的「复现(M1 最小版)」:
单个增长日志、每秒 1 行、按固定分钟点埋真命中,其余为噪声/迷惑行;跑完用对账脚本
逐行核对"日志真命中 vs 会话上报"(命中率/延迟/漏报/误报/是否提问)。

> **定位说明**:这是 M1 最小版(判据在**输入端**可见:CRITICAL+[INCIDENT] 字样)。
> 能力一(监控研判编队)的正式长期测试台按交接文档「可复用长期测试台」节**另建**——
> 那个要求多源异构 schema、游标 pull 接口、且**真假只能从"结果"端判**(输入端全是迷惑),
> 本目录工具不满足那套要求,别拿它顶替。

## 用法

```bash
# 1. 喂入器:往日志文件每秒追加 1 行,21 分钟,8 个真命中散布在这些分钟点
python3 scripts/watch_harness/watch_feeder.py /path/to/service.log 1260 "2.0,4.5,7.0,9.5,12.0,14.5,17.0,19.0"

# 2. 投任务(隔离 home + 网关常驻,见交接文档「本轮真机复现方法」;
#    注意 config 里 my_agent_home 优先于 MY_AGENT_HOME 环境变量,两处都要改)
curl -X POST http://127.0.0.1:8420/ask -H 'Content-Type: application/json' \
  -H 'X-User-Id: u-m1' -H 'X-Channel: feishu' \
  -d '{"kind":"ask","goal":"<盯守任务全文,判据=CRITICAL 且带 [INCIDENT]>"}'

# 3. 对账:日志真命中 vs 会话逐轮上报
python3 scripts/watch_harness/watch_report_audit.py /path/to/service.log \
  <owner_home>/workspace/runtime/workspaces/ws-*/conversations/messages/thread-*.jsonl
```

2026-07-02 真机基准:修复前 0/8;修复后 7/8 命中(延迟 18-61s≈一个唤醒周期)、0 误报、0 提问、0 子代理。
