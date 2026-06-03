
from __future__ import annotations

import json
from typing import Any

from ..contracts.tool_manifest_contract import tool_manifest_payload
from .models import BaseTool, ToolExecutionResult, ToolSpec


class ListToolsTool(BaseTool):

    def __init__(self, registry: Any):
        self.registry = registry
        self.spec = ToolSpec(
            name="list_tools",
            category="system",
            effect="read_only",
            description="列出当前执行上下文可见的工具清单。",
            use_cases=[
                "不确定当前有哪些工具时，先查询机器可读工具清单",
                "需要确认 run_command、write_file、apply_patch 等工具是否可用",
            ],
            avoid_when=[
                "已经知道要用哪个工具时，直接调用目标工具",
            ],
            keywords=["list_tools", "tools", "工具清单", "tool manifest", "available tools"],
            parameters={},
            examples=['{"tool": "list_tools"}'],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        specs = self.registry.specs(include_orchestration=True)
        payload = tool_manifest_payload(specs, owner_type="main_agent")
        payload["tool_failure_taxonomy"] = payload["failure_taxonomy"]
        return ToolExecutionResult(
            "list_tools",
            True,
            json.dumps(payload, ensure_ascii=False),
            result_envelope={"tool_output_policy": {"preserve_prompt_output": True}},
        )


__all__ = ["ListToolsTool"]
