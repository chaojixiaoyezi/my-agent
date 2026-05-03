# ARCHITECTURE EXEMPTIONS

LLM: No exemption is permanent; update this whenever code-size debt changes.

给人看的解释：
下面是历史大文件/大类的临时豁免。新增代码不能借这些豁免继续膨胀。

| Item | Current Size | Why Not Fully Split Now | Risk | Split Plan | Owner | Cleanup Round | Expires |
| --- | ---: | --- | --- | --- | --- | --- | --- |
| `agent_py_agent/cli/chat.py` | 989 lines | 已抽 history/rendering/slash commands，但 TUI/fallback worker 仍需更细测试保护 | 交互 streaming 回归 | 抽 `tui.py`、`fallback.py`、`gateway_client.py` | architecture owner | next 2 rounds | 2026-06-30 |
| `agent_py_agent/agent/settings/config.py` | 751 lines | 配置兼容面广，需先建立分域 config 测试 | 旧配置默认值回归 | 抽 model/memory/gateway/subagent/adapter config | architecture owner | next 3 rounds | 2026-07-15 |
| `agent_py_agent/agent/agent_core/dispatch_mixin.py` | 889 lines | 调度涉及 gateway/subagent/acceptance 多路径 | 调度顺序和审计日志回归 | 抽 dispatch/planner/runner gate services | architecture owner | next 3 rounds | 2026-07-15 |
| `agent_py_agent/agent/subagents/manager_patch.py` | 794 lines | patch apply 有写边界和审计风险 | patch 写入边界回归 | 抽 patch review/apply service | architecture owner | next 2 rounds | 2026-06-30 |
| `agent_py_agent/agent/memory_archive/query.py` | 839 lines | 查询过滤路径多，CLI 兼容敏感 | 恢复和搜索结果变化 | 抽 filter/query service/result model | architecture owner | next 2 rounds | 2026-06-30 |
| `agent_py_agent/agent/log_analysis/tools.py` | 666 lines | log_analysis 长期要插件化，需先定义 extension API | 扩展入口污染 core | 抽 plugin registration 和 handlers | architecture owner | next 3 rounds | 2026-07-15 |
| `agent_py_agent/agent/gateway_parts/runtime.py` | 551 lines | gateway runtime 涉及 queue/worker/recovery | gateway lease/chunk 回归 | 抽 request worker 和 response renderer | architecture owner | next 2 rounds | 2026-06-30 |
