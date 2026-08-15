# Main Agent Contract Testing

当前测试策略：

- 普通合同和工具边界用 focused pytest 覆盖。
- 真实 LLM 测试只用于最终验收主链路。
- 用户 prompt 保持普通中文，不塞内部字段名。
- 质量问题放到 evidence、repair、closeout，不提前变成无关硬门。

常用命令：

```bash
python3 -m pytest agent_py_agent/tests/test_runtime_gate_integration.py agent_py_agent/tests/test_main_agent_delivery_closeout.py -q
python3 -m pytest agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_dispatch_subagents_tool.py -q
```

参考项目映射：

- agentscope-main: AgentScope
- 终端应用: 模型助手 Code
- claw-code-main: Claw Code
- 会话运行时-main: 会话运行时
- 终端交互-main: 终端交互
- 长期助手-agent-main: 长期助手
- langchain-master: LangChain
- langgraph-main: LangGraph
- openai-agents-python-main: OpenAI Agents SDK
- openclaude-main: 代理运行时
- 通道运行时-main: 通道运行时
- openhuman-main: OpenHuman
- my-agent-architecture-review-20260519-clean: my-agent-architecture-review
- my-agent-feature-card-message-runtime: my-agent-feature-card-message-runtime
