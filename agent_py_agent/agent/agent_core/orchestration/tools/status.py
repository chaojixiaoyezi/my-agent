
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ....tools import BaseTool, ToolExecutionResult
from ...agent_tree.status import agent_tree_status_payload
from ..tool_specs import (
    build_inspect_agent_tree_spec,
)

if TYPE_CHECKING:
    from ....core import SimpleAgent


class InspectAgentTreeTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_inspect_agent_tree_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        payload = agent_tree_status_payload(self.agent, params)
        return ToolExecutionResult(
            "inspect_agent_tree",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
