# Live Lab Progress

当前 Live Lab 已包含 `compact-stress` 真实压测入口，用来验证主代理在约 3MB 长文本、200K 上下文窗口、50% 自动 compact 条件下，能否多次 compact 后继续同一任务并产出完整报告。需要更长档位时可用 `MY_AGENT_COMPACT_STRESS_SIZE_MB=10` 或 `50` 单独跑。
该 suite 默认把 `tool_read_max_chars` 设为 100000，让 200K 上下文窗口下的长文本读取以较大页推进，避免测试人为制造过多模型往返。
2026-06-07 起，compact stress 的固定事实 token 和章节标记只存在于 Live Lab fixture 里，用来验证长文本覆盖和 compact 续接；生产 runtime 不用这些自然语言 marker 做状态判断。

运行示例：

```bash
python3 scripts/live_agent_lab.py --suite compact-stress --real-llm --timeout 900
```

该 suite 会检查最终报告覆盖关键事实，并统计本轮运行目录里的 compact apply ledger；compact 次数不足会直接失败。

2026-06-05 的真实对照测试暴露了几个主链路要求：

- 默认 chat/cli/gateway 主链路不跑 delivery materializer。真实 compact 压测要依赖工具读取游标、coverage ledger、task work 草稿和外部校验，不把普通自然语言需求变成隐藏 `target_coverage_contract` 硬门。
- runtime fact / compact work state 必须保留已存在的显式 `target_coverage` 摘要；否则多轮 compact 后会只剩目标产物路径和自然语言 offset 提醒，覆盖合同会在恢复链里变弱。
- 长文本任务不应强迫模型纯顺序翻页。`search_text`/grep 可以作为章节定位器和范围索引，但 required `full_source_read` 的通过条件仍是已归档 `read_file`/`read_artifact` 窗口或 coverage ledger 证明。
- Live Lab 删除未使用的 stdout 文本 marker 断言。真实测试通过/失败只看命令 exit code、summary JSON、结构化产物和持久化状态，不再用模型自然语言回复里的固定短语判断子代理链路是否通过。
