# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""parent planner structured-output templates kept outside planner orchestration."""

PARENT_PLANNER_RESULT_TEMPLATE = (
    "## Required Output\n\n"
    "最后必须输出一个机器可解析结果块，格式如下。结果块里只能放裸 JSON object，"
    "不要使用 Markdown 代码围栏。\n\n"
    "[PARENT_PLANNER_RESULT]\n"
    "{\n"
    '  "decision": "DISPATCH",\n'
    '  "summary": "本轮父代理判断摘要",\n'
    '  "should_dispatch": true,\n'
    '  "runner_instruction": "给本轮 runner 的额外指令，可为空",\n'
    '  "suggested_max_runners": 1,\n'
    '  "actions": [\n'
    '    {"action": "execute_runner|review_acceptance|route_capability|takeover|report_blocker", "run_id": "", "priority": 1, "reason": ""}\n'
    "  ],\n"
    '  "blockers": [],\n'
    '  "risks": [],\n'
    '  "notes": []\n'
    "}\n"
    "[/PARENT_PLANNER_RESULT]\n"
)
