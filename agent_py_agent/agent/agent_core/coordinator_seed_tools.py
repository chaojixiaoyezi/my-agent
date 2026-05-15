# LLM: Coordinator seed tool policy preserves inherited grants and adds orchestration tools.
# 模块用途: 给显式 root/coordinator seed 补齐协调工具，同时不吞掉父级已经决定给它的工具。

from __future__ import annotations

from ..subagents.role_templates import COORDINATOR_TOOLS


# LLM: explicit_root_allowed_tools merges root orchestration grants with caller-provided tools.
# 函数用途: 显式 root/coordinator 创建入口保留父级工具，再补齐内置协调工具；未显式传工具时继续交给角色模板自动判断。
def explicit_root_allowed_tools(allowed_tools: list[str] | None) -> list[str] | None:
    if allowed_tools is None:
        return None
    return list(dict.fromkeys([*allowed_tools, *COORDINATOR_TOOLS]))
