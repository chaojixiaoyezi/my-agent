# REFACTORING BACKLOG

LLM: Keep this backlog concrete and executable.

给人看的解释：
这里列出最应该继续拆的对象，每项都包含问题、目标结构、步骤、风险和验收命令。

## 1. `agent_py_agent/cli/chat.py`

- 当前问题：仍超过 CLI/TUI 硬目标，包含 TUI loop、fallback loop、streaming、状态展示和 session 收尾。
- 目标结构：`chat_parts/tui.py`、`fallback.py`、`session_state.py`、`gateway_client.py`、`input_loop.py`。
- 拆分步骤：先抽 fallback worker，再抽 TUI worker，再把 gateway/local response streaming 合并为 response client。
- 风险：交互行为、streaming 输出和 prompt_toolkit 兼容容易回归。
- 验收命令：`python3 -m pytest agent_py_agent/tests/test_cli_chat.py agent_py_agent/tests/test_chat_parts.py -q`。

## 2. `agent_py_agent/agent/settings/config.py`

- 当前问题：配置字段集中，容易成为万能配置对象。
- 目标结构：`model_config.py`、`memory_config.py`、`gateway_config.py`、`subagent_config.py`、`adapter_config.py`、`normalize.py`。
- 拆分步骤：先抽纯 dataclass 分域视图，再抽 normalize helper，最后让底层模块只接收需要的 config。
- 风险：配置兼容和默认值最容易破坏旧用户。
- 验收命令：`python3 -m pytest agent_py_agent/tests -q -k "config or packaging"`。

## 3. `agent_py_agent/agent/agent_core/dispatch_mixin.py`

- 当前问题：调度 orchestration 仍偏大。
- 目标结构：dispatch service、planner service、runner gate、acceptance gate。
- 拆分步骤：抽纯决策函数，抽 dispatch step result，再服务化 runner 推进。
- 风险：父代理调度状态和审计日志顺序不能变。
- 验收命令：`python3 -m pytest agent_py_agent/tests -q -k "dispatch or gateway"`。

## 4. `agent_py_agent/agent/subagents/manager_patch.py`

- 当前问题：patch review 和 apply 逻辑仍在 mixin。
- 目标结构：`SubAgentPatchService`、patch repository、patch renderer。
- 拆分步骤：先抽 review service，再抽 apply service，最后 manager 只委托。
- 风险：文件写入边界和 patch 审计不能被绕过。
- 验收命令：`python3 -m pytest agent_py_agent/tests -q -k "patch or subagent"`。

## 5. `agent_py_agent/agent/memory_archive/query.py`

- 当前问题：查询、过滤、解析、展示边界偏宽。
- 目标结构：query model、archive query service、filter policy、rendering adapter。
- 拆分步骤：先抽 filter predicate，再抽 query result dataclass。
- 风险：CLI 搜索兼容和恢复路径不能变。
- 验收命令：`python3 -m pytest agent_py_agent/tests -q -k "memory_archive"`。

## 6. `agent_py_agent/agent/log_analysis/tools.py`

- 当前问题：扩展工具入口仍偏大。
- 目标结构：extension plugin、tool registration、tool handlers。
- 拆分步骤：先定义 LogAnalysisPlugin，再把工具注册从 handler 中分离。
- 风险：日志分析命令和测试夹具较多，需小步迁移。
- 验收命令：`python3 -m pytest agent_py_agent/tests -q -k "log_analysis"`。

## 7. `agent_py_agent/agent/gateway_parts/runtime.py`

- 当前问题：runtime worker、请求处理、输出展示仍同文件。
- 目标结构：gateway queue service、request worker、response renderer、audit service。
- 拆分步骤：先抽 request execution helper，再抽 queue iteration。
- 风险：gateway 恢复、lease、chunk streaming 不能回归。
- 验收命令：`python3 -m pytest agent_py_agent/tests -q -k "gateway"`。
