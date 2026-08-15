# CHAT REFACTOR PLAN

当前方向：

- 本地不应成为瓶颈，慢只能慢在模型。
- gateway/chat 要保持流式输出和清爽日志。
- 多个 CLI/chat 界面接同一 gateway 时，请先修 queue、lease、context 和渲染主链路。
- 不再按旧拆分计划推进。
