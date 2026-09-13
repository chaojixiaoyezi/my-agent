# GW-03 / 慢模型相关配对基准

这一组脚本是 **A 项（owner 事实缓存快照绑定）与 GW-03（锁内全量解析）** 的长期可复跑基准，
从当时的验收脚本整理而来，只保留结构化夹具，不含任何真实会话内容、个人路径或密钥，也不引入新依赖。

## 证据边界（重要）

- **锁更短 ≠ 整体更快**：scheduler 只读路径的锁持有从几十毫秒降到 ~13µs，但同负载下**总耗时 +10~20%**
  （锁外多一次读 + 一次 stat 复核）。两组数字都要一起看。
- **配对交替才可比**：机器负载会漂移。每个脚本每轮先测 A 再测 B、多轮取中位，**比值只在同一组采样内成立**。
  R273 记录过两组独立采样（第一组定位回退：0.9918ms → 2.5024ms = 2.52x；第二组改造后复测：
  0.6735ms → 0.7405ms = 1.10x）。**跨组把 2.5024 与 0.6735 相除得到的 ~3.72x 不是有效数据。**
- 这些是**纯内存/临时夹具**基准，不是真机用户任务；真机结论一律以部署后的真 TUI 验收为准。

## 复现命令

```bash
# 1) scheduler 只读路径:单树
python3 scripts/bench/measure_scheduler_reads.py --runs 10000 --rounds 5
#    配对 A/B(两个 checkout,例如修复前 worktree 与当前工作树)
python3 scripts/bench/measure_scheduler_reads.py --repo /path/to/before --runs 10000 --rounds 5
python3 scripts/bench/measure_scheduler_reads.py --repo /path/to/after  --runs 10000 --rounds 5

# 2) owner 事实判定:单场景
python3 scripts/bench/bench_owner_fact_kind.py --scenario empty --rounds 60
#    安静 owner(强制 cheap 分支)
FORCE_CHEAP=1 python3 scripts/bench/bench_owner_fact_kind.py --scenario empty --rounds 60
#    配对交替(每轮 A/B 交替,多轮取中位)
python3 scripts/bench/paired_owner_fact_kind.py --repo-a /path/to/before --repo-b /path/to/after --case cheap
python3 scripts/bench/paired_owner_fact_kind.py --repo-a /path/to/before --repo-b /path/to/after --case bind
```

## 判据

| 指标 | 含义 | 期望 |
| --- | --- | --- |
| `hold_median_ms` | 锁临界区持有时长 | 只读路径应与账本大小无关（µs 量级） |
| `parse/call` / `load/call` | 每次调用的全量解析/锁内读盘次数 | 只读路径：解析在锁外发生、锁内 0 次读盘 |
| `total_median_ms` | 调用墙钟 | 与修复前同量级（允许略升，换锁缩短） |
| `stats.cheap` / `stats.hit` / `stats.miss` | 事实缓存走了哪条分支 | 安静 owner 必须落 `cheap`（零签名成本） |

相关记录：`DESIGN_LEDGER.md` 的 R265 / R267 / R273 / R274 条目；`TESTS.md` 的 R273 / R274 小节。
