# LLM: Write safety belongs to structured scopes and execution tools, not prose preflight guesses.
# 模块用途: 保留 create_subagents 调用边界；实际写入边界由工具执行层的 allowed roots 和 RunScope 检查。

from __future__ import annotations

WRITE_SUBAGENT_TOOLS = {"write_file", "append_file", "replace_in_file"}


# LLM: external_write_target_error intentionally ignores plain task text.
# 函数用途: 不再从 goal 普通文本推断写入目标；越界写入由结构化写入根和文件工具统一拦截。
def external_write_target_error(agent, goal: str, allowed_tools: list[str]) -> str:
    del agent, goal, allowed_tools
    return ""
