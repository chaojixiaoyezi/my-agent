# Main Agent Kernel Design

当前主代理内核只认一条主链路：

1. 解析当前用户消息。
2. 读取 owner home 的轻量上下文。
3. 建立 task workspace。
4. 构造 prompt。
5. 调用模型和工具循环。
6. 写 daily memory、run archive、recovery snapshot、task workspace。
7. 在需要时自动 compact 并继续。
8. 最终由 delivery closeout 验收当前 run 产物。

相关代码：

- `agent_py_agent/agent/core.py`
- `agent_py_agent/agent/agent_core/runtime_mixin.py`
- `agent_py_agent/agent/agent_core/runtime/`
- `agent_py_agent/agent/agent_core/delivery_closeout/`
- `agent_py_agent/agent/user_space/`

不要在内核外新增历史路径旁路或空转发层。
