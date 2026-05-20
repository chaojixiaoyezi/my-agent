# LLM: Runtime tool manifest exposes current tool availability as structured data.
# 模块用途: 提供 list_tools 工具，避免模型靠自然语言记忆猜当前有哪些工具。

from __future__ import annotations

import json
from typing import Any

from ..contracts.tool_manifest_contract import tool_manifest_payload
from .models import BaseTool, ToolExecutionResult, ToolSpec


# LLM: ListToolsTool returns the registry's visible ToolManifest.
# 类用途: 让模型在不确定工具能力时查询结构化工具清单，减少“以为没有工具”的幻觉。
class ListToolsTool(BaseTool):

    # LLM: ListToolsTool.__init__ keeps a live registry reference for runtime manifests.
    # 函数用途: 初始化 list_tools 工具规格和注册表引用。
    def __init__(self, registry: Any):
        self.registry = registry
        self.spec = ToolSpec(
            name="list_tools",
            category="system",
            description="列出当前执行上下文可见的工具清单。",
            use_cases=[
                "不确定当前有哪些工具时，先查询机器可读工具清单",
                "需要确认 run_command、data_to_workbook 等工具是否可用",
            ],
            avoid_when=[
                "已经知道要用哪个工具时，直接调用目标工具",
            ],
            keywords=["list_tools", "tools", "工具清单", "tool manifest", "available tools"],
            parameters={},
            examples=['{"tool": "list_tools"}'],
        )

    # LLM: ListToolsTool.execute serializes visible specs without reading prompt text.
    # 函数用途: 返回 name/category/parameters/description 等结构化工具事实。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        specs = self.registry.specs(include_orchestration=True)
        payload = tool_manifest_payload(specs, owner_type="main_agent")
        payload["tool_failure_taxonomy"] = payload["failure_taxonomy"]
        return ToolExecutionResult("list_tools", True, json.dumps(payload, ensure_ascii=False))


__all__ = ["ListToolsTool"]
