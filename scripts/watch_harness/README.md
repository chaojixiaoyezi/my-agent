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
