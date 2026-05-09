# LLM: Runner context helpers keep active subagent state centralized for nested orchestration.
# 模块用途: 读取当前正在执行的 subagent runner 上下文，避免各工具自行约定字段名。

from __future__ import annotations


# LLM: current_subagent_run_id returns the active runner id set by subagent_run_flow.
# 函数用途: 给层级调度和 runner 内 dispatch 使用当前节点 ID；空字符串表示当前不是 subagent runner。
def current_subagent_run_id(agent) -> str:
    return str(getattr(agent, "_current_subagent_run_id", "") or "").strip()
