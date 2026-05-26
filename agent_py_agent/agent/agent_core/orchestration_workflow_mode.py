# LLM: Workflow mode normalization is shared by create and dispatch orchestration tools.
# 模块用途: 统一处理 orchestration 工具里的 workflow mode 字符串，避免各工具类各自解释配置。

from __future__ import annotations


# LLM: tool_workflow_mode only honors explicit workflow requests from the model/tool call.
# 函数用途: workflow 不再从全局配置静默套到普通子代理；只有本次工具参数明确写 plan/auto 才启用。
def tool_workflow_mode(explicit_mode: object, config_mode: object) -> str:
    del config_mode
    if isinstance(explicit_mode, str):
        normalized = explicit_mode.strip().lower()
        if normalized in {"off", "plan", "auto"}:
            return normalized
        if normalized:
            return "off"
    return "off"
