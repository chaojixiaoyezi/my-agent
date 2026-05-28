# LLM: Read-only orchestration status tools; do not trigger dispatch from this module.
# 模块用途: 提供 subagent board 和 agent tree 查询工具，供主代理按需查看状态。

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..subagents.models import SubAgentBoardOptions
from ..tools import BaseTool, ToolExecutionResult
from .agent_tree_status import agent_tree_status_payload
from .orchestration_board_payload import (
    board_actionable_run_ids,
    board_aggregation_readiness,
    board_completion_status,
    board_kernel_snapshot_payload,
)
from .orchestration_board_tool_payload import (
    board_artifact_id_preview,
    board_child_result_index,
    board_items_for_payload,
    board_payload_item,
    board_ref_preview,
)
from .orchestration_tool_specs import (
    build_inspect_agent_tree_spec,
    build_subagent_board_spec,
)
from .parameters import _positive_int

if TYPE_CHECKING:
    from ..core import SimpleAgent


class SubagentBoardTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_subagent_board_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        limit = _positive_int(params.get("limit"), default=10)
        board = self.agent.subagents.write_board(
            options=SubAgentBoardOptions(recent_limit=max(1, limit)),
        )
        items = board_items_for_payload(self.agent, board.items, params, limit)
        payload = {
            "summary": board.summary,
            "completion_status": board_completion_status(items),
            "aggregation_readiness": board_aggregation_readiness(items),
            "kernel_snapshot": board_kernel_snapshot_payload(self.agent, items),
            "returned": len(items),
            "actionable_run_ids": board_actionable_run_ids(items),
            "child_result_index": board_child_result_index(items),
            "deliverable_artifact_ids": board_artifact_id_preview(items),
            "deliverable_artifact_refs": board_ref_preview(items, "artifact_refs"),
            "deliverable_evidence_refs": board_ref_preview(items, "evidence_refs"),
            "subagent_workspace": str(self.agent.subagents.workspace),
            "items": [board_payload_item(item) for item in items],
            "board_json": str(self.agent.subagents.workspace / "subagent_board.json"),
            "board_md": str(self.agent.subagents.workspace / "SUBAGENT_BOARD.md"),
        }
        return ToolExecutionResult(
            "subagent_board",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )


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
