from __future__ import annotations

"""LLM: builds subagent runner execution prompts and structured-output repair audit text.

给人看的解释：
runner 真正调用模型前，需要把执行上下文压成明确任务；模型输出不合格式时，还要生成一次'只修格式'的补救 prompt。
这些 prompt 模板都放这里，避免主流程函数越来越长。
"""

import json
from dataclasses import asdict

from ..subagent import SubAgentExecutionContext


def _build_subagent_runner_prompt(
    context: SubAgentExecutionContext,
    instruction: str = "",
) -> str:

    payload = json.dumps(asdict(context), ensure_ascii=False, indent=2)
    extra = instruction.strip() or "按执行上下文完成任务；如果能力不足，说明需要上抛的 capability_request。"
    return (
        "# SubAgent Runner Task\n\n"
        "你是一个被父代理授权的子代理，只能依据下面的执行上下文工作。\n"
        "不要使用上下文之外的 skill/tool，不要假完成；没有验收证据时只能标记等待验收或上抛能力请求。\n\n"
        "## Extra Instruction\n\n"
        f"{extra}\n\n"
        "## Execution Context JSON\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Required Output\n\n"
        "- 说明完成了什么或卡在哪里。\n"
        "- 列出使用过的授权工具或 skill。\n"
        "- 给出可验收证据；如果没有证据，明确写出还需要什么能力或工具。\n"
        "- 最后必须输出一个机器可解析结果块，格式如下：\n\n"
        "注意：结果块里面只能放裸 JSON object，不要使用 ```json 或任何 Markdown 代码围栏。\n"
        "在最终结果块之前，不要把 [SUBAGENT_RESULT] 或 [/SUBAGENT_RESULT] 当作普通说明文字重复引用。\n\n"
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
        '  "summary": "本轮完成或卡住的摘要",\n'
        '  "used_tools": [],\n'
        '  "used_skills": [],\n'
        '  "evidence": [\n'
        '    {"kind": "command", "summary": "验证摘要", "command": "", "path": "", "url": "", "ok": true}\n'
        "  ],\n"
        '  "capability_requests": [\n'
        '    {"problem": "缺少什么", "needed_capability": "能力名", "expected_output": "希望得到什么", "tried": [], "evidence": [], "constraints": {}}\n'
        "  ],\n"
        '  "artifacts": [\n'
        '    {"path": "产物路径", "kind": "file|report|log", "summary": "产物说明"}\n'
        "  ],\n"
        '  "tests": [\n'
        '    {"name": "测试名称", "command": "运行命令", "ok": true, "summary": "测试结果摘要"}\n'
        "  ],\n"
        '  "patches": [\n'
        '    {"path": "改动文件", "status": "applied|planned|blocked", "summary": "改了什么或准备改什么"}\n'
        "  ],\n"
        '  "lessons": ["可沉淀经验，适合未来变成 skill 或规则"],\n'
        '  "next_actions": ["建议父代理下一步动作"],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]\n"
    )


def _build_subagent_runner_repair_prompt(
    context: SubAgentExecutionContext,
    *,
    original_prompt: str,
    original_response: str,
    parse_error: str = "",
) -> str:

    payload = json.dumps(asdict(context), ensure_ascii=False, indent=2)
    problem = parse_error.strip() or "上一轮回复缺少 [SUBAGENT_RESULT] 结果块。"
    return (
        "# SubAgent Runner Output Repair\n\n"
        "上一轮子代理已经完成了一次执行，但父代理没有拿到可解析的机器结果块。\n"
        "你现在只做格式修复：不要调用工具，不要新增事实，不要虚构证据；"
        "只能根据执行上下文、上一轮最终 prompt 里的工具结果、以及上一轮回复来整理结果。\n"
        "如果上一轮确实没有可验收证据，就把 status 写成 BLOCKED，并在 blocked_reason 里说明缺什么。\n\n"
        "必须只输出下面这种结果块，不要输出解释文字、Markdown 代码围栏或额外前后缀：\n\n"
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
        '  "summary": "本轮完成或卡住的摘要",\n'
        '  "used_tools": [],\n'
        '  "used_skills": [],\n'
        '  "evidence": [],\n'
        '  "capability_requests": [],\n'
        '  "artifacts": [],\n'
        '  "tests": [],\n'
        '  "patches": [],\n'
        '  "lessons": [],\n'
        '  "next_actions": [],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]\n\n"
        "## Parse Problem\n\n"
        f"{problem}\n\n"
        "## Execution Context JSON\n\n"
        f"{payload}\n\n"
        "## Previous Final Prompt\n\n"
        f"{original_prompt}\n\n"
        "## Previous Model Response\n\n"
        f"{original_response}\n"
    )


def _append_runner_repair_prompt(original_prompt: str, repair_prompt: str) -> str:

    return (
        f"{original_prompt}\n\n"
        "---\n\n"
        "# Structured Output Repair Prompt\n\n"
        f"{repair_prompt}"
    )


def _append_runner_repair_response(original_response: str, repair_response: str) -> str:

    return (
        f"{original_response}\n\n"
        "---\n\n"
        "# Structured Output Repair Response\n\n"
        f"{repair_response}"
    )


def _append_runner_repair_failure(original_response: str, exc: Exception) -> str:

    return (
        f"{original_response}\n\n"
        "---\n\n"
        "# Structured Output Repair Failure\n\n"
        f"{type(exc).__name__}: {exc}"
    )
