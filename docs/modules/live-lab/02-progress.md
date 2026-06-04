# Live Lab Progress

当前 Live Lab 已包含 `compact-stress` 真实压测入口，用来验证主代理在约 10MB 长文本、200K 上下文窗口、50% 自动 compact 条件下，能否多次 compact 后继续同一任务并产出完整报告。

运行示例：

```bash
python3 scripts/live_agent_lab.py --suite compact-stress --real-llm --timeout 900
```

该 suite 会检查最终报告覆盖关键事实，并统计本轮运行目录里的 compact apply ledger；compact 次数不足会直接失败。
