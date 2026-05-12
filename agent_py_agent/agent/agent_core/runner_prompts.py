# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""builds subagent runner execution prompts and structured-output repair audit text.

runner 真正调用模型前，需要把执行上下文压成明确任务；模型输出不合格式时，还要生成一次'只修格式'的补救 prompt。
这些 prompt 模板都放这里，避免主流程函数越来越长。
"""

import json
from dataclasses import asdict

from ..subagent import SubAgentExecutionContext
from ..subagents.context_bundle import context_gate_prompt_lines
from ..subagents.role_templates import role_template_detail_text, role_template_index_text

_SUBAGENT_RESULT_TEMPLATE = (
    "[SUBAGENT_RESULT]\n"
    "{\n"
    '  "status": "AWAITING_ACCEPTANCE",\n'
    '  "summary": "本轮完成或卡住的摘要",\n'
    '  "used_tools": [],\n'
    '  "used_skills": [],\n'
    '  "evidence": [\n'
    '    {"kind": "command", "summary": "验证摘要", "command": "", "path": "", "url": "", "ok": true}\n'
    "  ],\n"
    '  "evidence_packets": [\n'
    '    {"id": "evpkt-run-id-short", "claim": "可验收声明", "checked_scope": "检查范围", "evidence_refs": ["runner_result.json"], "artifact_refs": ["产物路径或output.json"], "confidence": 0.9}\n'
    "  ],\n"
    '  "capability_requests": [\n'
    '    {"problem": "缺少什么", "needed_capability": "能力名", "capability_type": "shell|tool|skill|mcp|network|generic", "expected_output": "希望得到什么", "requested_tools": [], "requested_skills": [], "requested_mcp_tools": [], "requested_commands": ["python3"], "cwd_scope": [], "path_scope": ["任务内需要访问的目录"], "network_scope": [], "output_budget": {"stdout_bytes": 65536, "stderr_bytes": 32768}, "risk_level": "low|medium|high", "tried": [], "evidence": [], "constraints": {}, "fallback_attempted": [], "escalation_target": "parent", "reserved": {}}\n'
    "  ],\n"
    '  "artifacts": [\n'
    '    {"path": "产物路径", "kind": "file|report|log", "summary": "产物说明"}\n'
    "  ],\n"
    '  "tests": [\n'
    '    {"name": "测试名称", "validation_method": "command", "command": "python3 -m pytest -q", "working_dir": "运行目录", "ok": true, "summary": "测试结果摘要"}\n'
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

_SUBAGENT_REPAIR_RESULT_TEMPLATE = (
    "[SUBAGENT_RESULT]\n"
    "{\n"
    '  "status": "AWAITING_ACCEPTANCE",\n'
    '  "summary": "本轮完成或卡住的摘要",\n'
    '  "used_tools": [],\n'
    '  "used_skills": [],\n'
    '  "evidence": [\n'
    '    {"kind": "artifact", "summary": "已检查的产物或报告", "path": "产物路径或报告路径", "ok": true}\n'
    "  ],\n"
    '  "evidence_packets": [\n'
    '    {"id": "evpkt-repair-run-id-short", "claim": "可验收声明", "checked_scope": "修复整理范围", "evidence_refs": ["报告或output.json路径"], "artifact_refs": ["产物路径"], "confidence": 0.8}\n'
    "  ],\n"
    '  "capability_requests": [],\n'
    '  "artifacts": [],\n'
    '  "tests": [],\n'
    '  "patches": [],\n'
    '  "lessons": [],\n'
    '  "next_actions": [],\n'
    '  "blocked_reason": "",\n'
    '  "failure_type": ""\n'
    "}\n"
    "[/SUBAGENT_RESULT]"
)


# LLM: _build_subagent_runner_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理执行器提示词所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _build_subagent_runner_prompt(
    context: SubAgentExecutionContext,
    instruction: str = "",
) -> str:

    payload = json.dumps(asdict(context), ensure_ascii=False, indent=2)
    extra = instruction.strip() or "按执行上下文完成任务；如果能力不足，说明需要上抛的 capability_request。"
    execution_contract = "\n".join(_runner_execution_contract_lines(context))
    context_gate = "\n".join(context_gate_prompt_lines(context.context_bundle))
    return (
        "# SubAgent Runner Task\n\n"
        "你是一个被父代理授权的子代理，只能依据下面的执行上下文工作。\n"
        "不要使用上下文之外的 skill/tool，不要假完成；没有验收证据时只能标记等待验收或上抛能力请求。\n\n"
        "## Extra Instruction\n\n"
        f"{extra}\n\n"
        "## Runner Contract\n\n"
        f"{execution_contract}\n\n"
        "## Context Bundle Gate\n\n"
        f"{context_gate}\n\n"
        "## Execution Context JSON\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Required Output\n\n"
        "- 说明完成了什么或卡在哪里。\n"
        "- 列出使用过的授权工具或 skill。\n"
        "- 给出可验收证据；成功时 evidence_packets 必须有 artifact_refs 或 evidence_refs，不能只写普通 evidence。\n"
        "- 结果块要短：evidence/artifacts/tests/lessons 每类只保留最关键的 1-5 条，长报告写文件后引用路径。\n"
        "- 最后必须输出一个机器可解析结果块，格式如下：\n\n"
        "注意：结果块里面只能放裸 JSON object，不要使用 ```json 或任何 Markdown 代码围栏。\n"
        "在最终结果块之前，不要把 [SUBAGENT_RESULT] 或 [/SUBAGENT_RESULT] 当作普通说明文字重复引用。\n\n"
        f"{_SUBAGENT_RESULT_TEMPLATE}"
    )


# LLM: _runner_execution_contract_lines keeps model-facing runner rules precise without bloating the prompt builder.
# 函数用途: 根据 runner 上下文生成执行边界说明，尤其说明叶子节点测试命令应交给父级验收器执行。
def _runner_execution_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    lines = [
        "- 只把真正阻止你产出文件、报告或证据的缺口写成 capability_request。",
        "- 如果你没有 shell/command/terminal 工具，不要因为不能自己运行 pytest 就提交 capability_request。",
        "- 没有命令执行工具时，应写出可验收产物和测试文件，并在 tests/next_actions 中给父级验收器推荐命令。",
        "- 推荐给父级验收器的命令必须是安全、具体、可复制的；不要假装你已经执行过它。",
        "- tests 里的命令必须能被父级 TestExecutor 安全执行：不要写 cd ... &&，把目录写在 \"working_dir\" 字段里。",
        "- 验证文件内容时优先写 validation_method=\"content_check\"、file_path、content_pattern 或 content_equals、match_mode=\"exact\"，不要写 cat 文件命令。",
        "- 写代码和测试后，必须逐条对照验收条件做静态自检，确保实现、测试、README 三者互相一致。",
        "- 写 Python 测试时必须保证从 working_dir 运行能导入被测模块；优先把测试文件和模块放同一目录，或显式处理 import path。",
        "- 生成长 CSS/JS/HTML 或大段代码时，不要一次性把完整 content 塞进 write_file；"
        "先用 write_file 写短骨架，再用 append_file 分块追加；正常分块时单次 content 建议 1500-2000 字符。"
        "如果出现工具调用解析失败，再降到不超过 800 字符，并且每轮只输出 1 个写入工具调用，"
        "闭合 [/TOOL_CALL] 后再继续下一块。",
        "- write_file 和 append_file 会在授权 allowed_write_roots 内自动创建父目录；不要因为目标目录尚未创建就标记 BLOCKED。",
        "- 如果最终结果需要列很多 artifacts 或证据，优先用 write_file 写 execution_context.output_json 的短 JSON；"
        "系统会自动把它包成 SUBAGENT_RESULT 收口，避免对话里的长结果块被截断。",
        "- output.json 是内部收口文件名；只能写 execution_context.output_json，"
        "不要在 product_write_roots、deliverables 或用户产物目录里创建 output.json。",
        "- 需要新工具、skill、MCP、网络或 shell 命令时，写 capability_request；"
        "必须说明 requested_tools/requested_skills/requested_mcp_tools/requested_commands 和 path_scope/output_budget。",
        "- 如果 Tool Catalog 里有 capability_request 工具，缺能力时必须先调用该工具记录正式申请；"
        "不要在产物目录写 capability_request.json，也不要改 execution_context.json 伪造 pending_requests。",
        "- capability_request 工具返回 OPEN 后，最终结果块写 status=PENDING_CAPABILITY_REQUEST 或 BLOCKED，"
        "不要继续假装能力已经授权或命令已经执行。",
    ]
    if context.controlled_exec_grants:
        lines.append(
            "- controlled_exec 只能使用 controlled_exec_grants 里的父级 grant；"
            "不要在工具参数里自填 command_allowlist/path_scope/network_scope。"
        )
        lines.append(
            "- 目标要求 controlled_exec 真实执行时，dry_run/allowed plan 不算完成；"
            "必须用 apply=true，并在最终 refs 中写出执行 payload 里的 stdout_ref、audit_ref。"
            "复杂 python 片段优先用 argv 数组，例如 command=[\"python3\",\"-c\",\"print('x' * 2000)\"]，避免 shell 引号歧义。"
        )
        lines.append(
            "- rm/rmdir/unlink 不会进入 command_allowlist，这是安全设计；"
            "如果 controlled_exec_grants.delete_policy.mode=task_trash，已有 controlled_exec grant 时直接调用 controlled_exec apply=true 执行删除命令，工具会改走 task_trash 并返回 trash_manifest_ref，"
            "不要为了裸 rm 再提交 capability_request；只有 moved=true 且有 trash_manifest_ref 才能把删除验收写成 PASS。"
        )
    lines.extend(_current_role_template_lines(context))
    if "leaf" in str(context.role or "").lower():
        lines.append("- 叶子节点重点是交付产物和测试文件；父级验收器负责运行命令、判定通过和触发 rescue。")
    if _is_coordinator_context(context):
        lines.extend(_coordinator_execution_contract_lines())
    return lines


# LLM: _coordinator_execution_contract_lines keeps delegation policy readable and under size limits.
# 函数用途: 生成 coordinator/lead 专属执行规则，包括模板选择、权限继承、产物委派和调度失败处理。
def _coordinator_execution_contract_lines() -> list[str]:
    lines = ["可用角色模板索引："]
    lines.extend(f"  {line}" for line in role_template_index_text().splitlines())
    lines.append("模板详情：")
    lines.extend(f"  {line}" for line in role_template_detail_text().splitlines())
    lines.extend(
        [
            "- coordinator/lead 节点可以在自己的 task_dir 写计划、证据和协调报告；"
            "也可以继承产物写入根用于检查、接管和救援；业务代码、页面、文档正文等最终产物仍应优先交给 worker/writer。",
            "- 上层权限应覆盖下层；如果父级给了产物写入根，你可以用它检查、修复或接管，"
            "但不要因为有权限就绕过 worker/writer 直接替它们完成整块业务产物；"
            "如果直接写业务产物被工具层拒绝，立刻创建救援 worker/writer/leaf_worker。",
            "- 正确动作是调用 schedule_child_subagents 创建 worker/writer/leaf_worker，"
            "把目标路径、文件名、验收条件原样传给下一层，然后用 dispatch_subagents 推进直接 child。",
            "- 如果缺口只属于未来 child/leaf 的执行能力，例如 leaf 才需要 controlled_exec、shell、网络或某个 skill，"
            "coordinator/lead 不要替后代提前提交 capability_request 后停止；先创建并 dispatch 对应 child，"
            "由真正需要该能力的 runner 正式申请，父级再 route grant 并继续推进。",
            "- 给 child 写 goal 时，不要要求它在产物目录写 output.json；"
            "如需结构化汇报，只能要求它写自己的 execution_context.output_json。",
            "- coordinator/lead 可以继续创建 coordinator/child_coordinator/grandchild_coordinator 作为下一层领导节点；"
            "需要多层协作时不要误以为只能创建 worker；父级要求 4 层链路时，深度未到孙孙层前先创建下一层 coordinator。",
            "- 如果父级目标或验收条件点名需要 tester、bug_finder、acceptor、reviewer、找错、测试或验收角色，"
            "必须创建真实 child run，并把 role/agent_name 写成对应角色；只在 goal、summary 或 evidence 里提到这些词不算角色覆盖。",
            '- schedule_child_subagents 的参数必须放在顶层，例如 {"tool":"schedule_child_subagents","apply":true,"children":[...]}；'
            '不要包成 {"orchestration": {...}}，长目标请分多次调用，每次 1-2 个 child。',
            "- 不要让 worker/writer 代写 coordinator 自己的协调证据；需要共享时引用 artifact_refs/evidence_refs。",
            "- 创建 child/leaf 时必须原样传递父级指定的文件名、目录和验收条件，不要把 solution.py 改成别的模块名。",
            "- 同一次 schedule_child_subagents 不要混建 coordinator 和 leaf_worker；如返回 mixed_coordinator_leaf_children，先只创建下一层 coordinator。",
            "- 如果 schedule_child_subagents 返回 domain_mismatch 或 forbidden_child_scope，必须修正 child 领域后重试，不能宣称完成。",
            "- 创建 leaf 后使用 dispatch_subagents(apply=true, execute_runners=true) 推进直接 child，并汇总 leaf 的产物 refs。",
            "- 多个 child 同轮 dispatch 时不要写子任务专属 runner_instruction；需要专属补充就按单个 run_id 分多次 dispatch。",
            "- dispatch_subagents 返回 child test_failed 或 followup_action=plan_rescue 时，不要宣称完成；先汇报失败 refs 或安排修复。",
            "- 少数下属需要不同纠偏、路径修正或需求变更时，用 subagent_message mode=direct scope=descendants 发给具体 run_id；"
            "大量下属需要同一通知时，用 mode=broadcast scope=descendants 写 scoped shared board。"
            "平级讨论只能用 mode=direct scope=peers，不能广播到兄弟分支的子孙。"
        ]
    )
    return lines


# LLM: _current_role_template_lines gives each runner its own role prompt without loading all templates.
# 函数用途: 只展开当前角色的模板详情；普通 worker/tester 等能拿到专属提示，leaf_worker 等无模板角色保持精简。
def _current_role_template_lines(context: SubAgentExecutionContext) -> list[str]:
    detail = role_template_detail_text(roles=[str(context.role or "")]).strip()
    if not detail or _is_coordinator_context(context):
        return []
    return ["当前角色模板详情：", *[f"  {line}" for line in detail.splitlines()]]


# LLM: _is_coordinator_context identifies runner roles that should delegate file writing to leaves.
# 函数用途: 判断当前 runner 是否是层级协调节点；用于给模型注入更强的派叶子规则。
def _is_coordinator_context(context: SubAgentExecutionContext) -> bool:
    role_text = f"{context.role} {context.agent_name}".lower()
    tools = set(context.allowed_tools or [])
    return (
        ("coordinator" in role_text or "lead" in role_text)
        and "schedule_child_subagents" in tools
    )


# LLM: _build_subagent_runner_repair_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理执行器repair提示词所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _build_subagent_runner_repair_prompt(
    context: SubAgentExecutionContext,
    *,
    original_prompt: str,
    original_response: str,
    parse_error: str = "",
) -> str:

    payload = json.dumps(asdict(context), ensure_ascii=False, indent=2)
    problem = parse_error.strip() or "上一轮回复缺少 [SUBAGENT_RESULT] 结果块。"
    prompt_tail = _clip_repair_text(original_prompt, 6000)
    response_tail = _clip_repair_text(original_response, 12000)
    return (
        "# SubAgent Runner Output Repair\n\n"
        "上一轮子代理已经完成了一次执行，但父代理没有拿到可解析的机器结果块。\n"
        "你现在只做格式修复：不要调用工具，不要新增事实，不要虚构证据；"
        "只能根据执行上下文、上一轮最终 prompt 里的工具结果、以及上一轮回复来整理结果。\n"
        "如果上一轮确实没有可验收证据，就把 status 写成 BLOCKED，并在 blocked_reason 里说明缺什么。\n\n"
        "输出必须很短：summary 不超过 300 字；evidence/artifacts/tests/lessons 各不超过 5 条；"
        "不要复述长报告、表格或源码。成功时必须给 evidence_packets，且每个 packet 至少包含 "
        "artifact_refs 或 evidence_refs 之一。\n\n"
        "必须只输出下面这种结果块，不要输出解释文字、Markdown 代码围栏或额外前后缀：\n\n"
        f"{_SUBAGENT_REPAIR_RESULT_TEMPLATE}\n\n"
        "## Parse Problem\n\n"
        f"{problem}\n\n"
        "## Execution Context JSON\n\n"
        f"{payload}\n\n"
        "## Previous Final Prompt Tail\n\n"
        f"{prompt_tail}\n\n"
        "## Previous Model Response Tail\n\n"
        f"{response_tail}\n"
    )


# LLM: _clip_repair_text keeps repair prompts bounded so the repair answer has room to close JSON.
# 函数用途: 裁剪结构化修复 prompt 中的长上下文，只保留尾部最可能包含工具结果和最终报告的片段。
def _clip_repair_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"[... clipped {len(text) - limit} chars ...]\n{text[-limit:]}"


# LLM: _append_runner_repair_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入执行器repair提示词的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _append_runner_repair_prompt(original_prompt: str, repair_prompt: str) -> str:

    return (
        f"{original_prompt}\n\n"
        "---\n\n"
        "# Structured Output Repair Prompt\n\n"
        f"{repair_prompt}"
    )


# LLM: _append_runner_repair_response 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入执行器repair响应的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _append_runner_repair_response(original_response: str, repair_response: str) -> str:

    return (
        f"{original_response}\n\n"
        "---\n\n"
        "# Structured Output Repair Response\n\n"
        f"{repair_response}"
    )


# LLM: _append_runner_repair_failure 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入执行器repair失败的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _append_runner_repair_failure(original_response: str, exc: Exception) -> str:

    return (
        f"{original_response}\n\n"
        "---\n\n"
        "# Structured Output Repair Failure\n\n"
        f"{type(exc).__name__}: {exc}"
    )
