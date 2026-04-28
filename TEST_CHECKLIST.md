# TEST_CHECKLIST

## [已测]
- [x] F01 Python3 标准库优先 — py_compile PASS，无运行依赖。
- [x] F02 CLI 入口 — `python3 -m agent_py_agent --help` PASS。
- [x] F03 JSONL 记忆 — remember/search PASS。
- [x] F04 外置配置中文说明 — `config/agent_config.yaml`。
- [x] F05 subagent 思维/计划/结果 — spawn-subagents PASS，生成 task.json/thought.md。
- [x] F06 动态 prompt 注入 — run --inject PASS。
- [x] F07 工程结构 — config/memory/prompt/backend/subagent/core 分层。
- [x] F08 Web/API 扩展边界 — extensions_dir/backend 接口预留。
- [x] F09 测试覆盖真实入口和错误入口 — --help/run/remember/search/spawn/unknown-command。

## [未测]
无。
