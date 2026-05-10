# LLM: Coordinator seed tool policy keeps root orchestration grants narrow and reusable.
# 模块用途: 收敛显式 root/coordinator seed 的工具权限，避免模型临时添加 shell/web 工具。

from __future__ import annotations

from ..subagents.role_templates import COORDINATOR_TOOLS


# LLM: explicit_root_allowed_tools ignores model-invented shell/web grants for coordinator seeds.
# 函数用途: 显式 root/coordinator 创建入口只保留内置协调工具包；未显式传工具时继续交给角色模板自动判断。
def explicit_root_allowed_tools(allowed_tools: list[str] | None) -> list[str] | None:
    if allowed_tools is None:
        return None
    return list(COORDINATOR_TOOLS)
