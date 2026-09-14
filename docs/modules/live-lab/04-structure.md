# Live Lab Structure

`main_agent_limit_cases._assert_storm_report/_assert_interrupt_honesty` 依赖显式传入的 lab 测试实例；
F821/F811 不再在仓库全局关闭。`test_r223_audit_regressions.py` 是底层故障注入，不是 TUI 测试替身。
真实模型验收记录 tmux、单 Gateway、request ID、工具轮次、终态和实际产物；不以模型说“完成”作为质量分数。

Live Lab 现在只作为真实链路验证辅助，不作为主架构来源。

保留目标：

- 能启动真实主代理测试。
- 能保存 prompt、日志、输出和总结。
- 不替代主代理 runtime、gateway 或 subagent 的生产逻辑。
- 不解析 stdout 自然语言 marker 当验收事实；case 自身应检查结构化响应、文件、ledger 或 summary。
- `main_agent_compact_stress.py` 可以生成大文本、章节和 checkpoint fixture，但这些字符串只用于测试覆盖，不能复制到生产 compact/status 判断里。
- `main_agent_compact_stress.py` 的临时 tool policy 只写当前运行时真实消费的字段；不保留已删除的
  `pin_versions` 或其他仅用于样例外观的假配置。
- `reporter.py` 在 `--real-llm` 下必须完成一次有界真实生成并验证非空响应，不能用环境变量存在代替模型可用性。
- `main_agent_complex_case.py` 的 `tool-recovery` case 同时核对最终产物和受保护缺失输入没有被创建。
- `log_analysis_replay.py`/`log_analysis_replay_stages.py` 已随 log_analysis 模块删除（2026-06-26）；Live Lab 不再含安全日志离线 replay case。
