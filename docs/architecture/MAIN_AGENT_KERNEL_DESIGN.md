# Main Agent Kernel Design

当前主代理内核只认一条主链路：

1. 解析当前用户消息。
2. 读取 owner home 的轻量上下文。
3. 建立 task workspace。
4. 构造 prompt。
5. 调用模型和工具循环。
6. 写 daily memory、run archive、recovery snapshot、task workspace。
7. 在需要时自动 compact 并继续。
8. 模型基于当前对话和真实工具事实给出自然最终回复，当前回合结束。

普通任务不经过独立验收器，也不要求调用提交工具。`/goal` 只在用户显式启用后给同一 thread
叠加持久目标状态；active 目标在安全条件满足时继续下一回合，直到模型用 `update_goal` 标记
`complete` 或 `blocked`，或进入暂停/用量/预算限制状态。

相关代码：

- `agent_py_agent/agent/core.py`
- `agent_py_agent/agent/agent_core/runtime_mixin.py`
- `agent_py_agent/agent/agent_core/runtime/`
- `agent_py_agent/agent/conversation/goal_runtime.py`
- `agent_py_agent/agent/conversation/goal_tools.py`
- `agent_py_agent/agent/user_space/`

不要在内核外新增历史路径旁路或空转发层。
