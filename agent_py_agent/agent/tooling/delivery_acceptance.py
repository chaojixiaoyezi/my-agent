# LLM: submit_for_acceptance is a generic handoff signal from model work to machine closeout.
# 模块用途: 提供通用“提交系统验收”工具；工具本身不验收、不放行，只让运行循环进入验收门。

from __future__ import annotations

import json
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec


class SubmitForAcceptanceTool(BaseTool):
    """A model-visible, task-agnostic way to submit the current work for acceptance."""

    spec = ToolSpec(
        name="submit_for_acceptance",
        category="delivery",
        description="通用交付提交工具：当你认为用户要求的工作已经完成时，用它请求系统验收。",
        use_cases=[
            "已经生成或更新完用户要求的交付物，需要系统检查是否合格",
            "已按返工单修复产物，需要重新提交验收",
        ],
        avoid_when=[
            "还在搜索、读取、分析、写草稿或没有写出目标产物时不要调用",
        ],
        keywords=["submit", "acceptance", "final", "done", "验收", "提交", "交付", "完成"],
        parameters={"note": "可选。简短说明你认为可以验收的内容；系统不会把 note 当作通过依据。"},
        parameter_details={
            "note": "可选字符串。只作为交接说明，真正验收只读取产物、工具记录、合同和运行事实。",
        },
        examples=[
            '{"tool": "submit_for_acceptance", "note": "主要产物已经写入 outputs/，请系统验收。"}'
        ],
        effect="read_only",
        default_mode="real",
        requires_idempotency=False,
        requires_approval=False,
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        note = str(params.get("note") or "").strip()
        payload = {
            "submission": "acceptance_requested",
            "note": note,
            "message_zh": "已提交系统验收；是否通过以后续机器验收结果为准。",
        }
        return ToolExecutionResult(
            self.spec.name,
            True,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            result_envelope=payload,
        )


__all__ = ["SubmitForAcceptanceTool"]
