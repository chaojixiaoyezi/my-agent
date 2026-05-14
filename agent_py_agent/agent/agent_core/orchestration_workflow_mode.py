# LLM: Workflow mode normalization is shared by create and dispatch orchestration tools.
# 模块用途: 统一处理 orchestration 工具里的 workflow mode 字符串，避免各工具类各自解释配置。

from __future__ import annotations


# LLM: tool_workflow_mode preserves legacy manual/auto/off semantics for model tools.
# 函数用途: 把显式参数和配置里的 workflow mode 归一化成 off、plan 或 auto，供创建和调度工具复用。
def tool_workflow_mode(explicit_mode: object, config_mode: object) -> str:
    if isinstance(config_mode, str) and config_mode.strip().lower() == "off":
        return "off"
    if isinstance(explicit_mode, str):
        normalized = explicit_mode.strip().lower()
        if normalized in {"off", "plan", "auto"}:
            return normalized
        if normalized:
            return "off"
    if isinstance(config_mode, str):
        normalized = config_mode.strip().lower()
        if normalized == "auto":
            return "auto"
        if normalized == "manual":
            return "plan"
    return "off"
