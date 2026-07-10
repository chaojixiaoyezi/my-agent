# Live Lab Structure

Live Lab 现在只作为真实链路验证辅助，不作为主架构来源。

保留目标：

- 能启动真实主代理测试。
- 能保存 prompt、日志、输出和总结。
- 不替代主代理 runtime、gateway 或 subagent 的生产逻辑。
- 不解析 stdout 自然语言 marker 当验收事实；case 自身应检查结构化响应、文件、ledger 或 summary。
- `main_agent_compact_stress.py` 可以生成大文本、章节和 checkpoint fixture，但这些字符串只用于测试覆盖，不能复制到生产 compact/status 判断里。
- `reporter.py` 在 `--real-llm` 下必须完成一次有界真实生成并验证非空响应，不能用环境变量存在代替模型可用性。
- `main_agent_complex_case.py` 的 `tool-recovery` case 同时核对最终产物和受保护缺失输入没有被创建。
- `log_analysis_replay.py`/`log_analysis_replay_stages.py` 已随 log_analysis 模块删除（2026-06-26）；Live Lab 不再含安全日志离线 replay case。
