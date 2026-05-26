# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""builds subagent runner execution prompts and structured-output repair audit text.

runner 真正调用模型前，需要把执行上下文压成明确任务；模型输出不合格式时，还要生成一次'只修格式'的补救 prompt。
这些 prompt 模板都放这里，避免主流程函数越来越长。
"""

import json

from ..subagent import SubAgentExecutionContext
from ..subagents.context_bundle import context_gate_prompt_lines
from ..subagents.role_templates import role_template_detail_text, role_template_index_text
from . import subagent_compact_continuation
from .runner_prompt_context_summary import runner_context_summary_payload
from .runner_prompt_contract_lines import (
    read_ref_context_lines,
    required_product_contract_lines,
)
from .runner_prompt_coordinator_policy import coordinator_execution_policy_lines
from .runner_prompt_templates import (
    COLLABORATION_CONTROL_PLANE_TOOLS,
    COLLABORATION_TOOL_HINTS,
    SUBAGENT_REPAIR_RESULT_TEMPLATE,
    SUBAGENT_RESULT_TEMPLATE,
)
from .runner_repair_log import (
    _append_runner_repair_failure,
    _append_runner_repair_prompt,
    _append_runner_repair_response,
)


# LLM: _build_subagent_runner_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建子代理执行器提示词所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _build_subagent_runner_prompt(
    context: SubAgentExecutionContext,
    instruction: str = "",
) -> str:

    payload = json.dumps(runner_context_summary_payload(context), ensure_ascii=False, indent=2)
    extra = instruction.strip() or "按执行上下文完成任务；如果能力不足，说明需要上抛的 capability_request。"
    execution_contract = "\n".join(_runner_execution_contract_lines(context))
    context_gate = "\n".join(context_gate_prompt_lines(context.context_bundle))
    compact_continuation = subagent_compact_continuation.build_subagent_compact_continuation_section(
        subagent_compact_continuation.SubagentCompactContinuationRequest(context=context)
    )
    compact_block = f"{compact_continuation}\n\n" if compact_continuation else ""
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
        f"{compact_block}"
        "## Execution Context JSON\n\n"
        "下面是瘦身后的执行摘要；完整上下文请按 refs 读取，不要让模型一次吞完整大 JSON。\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Required Output\n\n"
        "- 说明完成了什么或卡在哪里。\n"
        "- 列出使用过的授权工具或 skill。\n"
        "- 给出可验收证据；成功时 evidence_packets 必须有 artifact_refs 或 evidence_refs，不能只写普通 evidence。\n"
        "- 如果你认为某个失败、损坏或超时的 child run 已由另一个已完成 run 覆盖，必须写 coverage_records；"
        "只在 summary 里说“已覆盖”不会被父级验收或最终收口认可。\n"
        "- 结果块要短：evidence/artifacts/tests/lessons 每类只保留最关键的 1-5 条，长报告写文件后引用路径。\n"
        "- 最后必须输出一个机器可解析结果块，格式如下：\n\n"
        "注意：结果块里面只能放裸 JSON object，不要使用 ```json 或任何 Markdown 代码围栏。\n"
        "在最终结果块之前，不要把 [SUBAGENT_RESULT] 或 [/SUBAGENT_RESULT] 当作普通说明文字重复引用。\n\n"
        f"{SUBAGENT_RESULT_TEMPLATE}"
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
        "- 如果用户要求按钮、链接或图片不能失效，不要用 href=\"#\"、空锚点或不存在的 #id 假装可点击；"
        "页面内跳转必须指向真实存在的元素 id，按钮必须有真实交互或真实本地目标。",
        "- 生成长 CSS/JS/HTML 或大段代码时，不要一次性把完整 content 塞进 write_file；"
        "先用 write_file 写短骨架，再用 append_file 分块追加；正常分块时单次 content 建议 1500-2000 字符。"
        "写 HTML 时，最后一块才写 </body></html>；一旦文件已经闭合，不要再 append 正文，"
        "需要补中间内容就用 replace_in_file 插到 </body> 前。"
        "如果出现工具调用解析失败，再降到不超过 800 字符，并且每轮只输出 1 个写入工具调用，"
        "闭合 [/TOOL_CALL] 后再继续下一块。",
        "- write_file 和 append_file 会在授权 allowed_write_roots 内自动创建父目录；不要因为目标目录尚未创建就标记 BLOCKED。",
        *read_ref_context_lines(context),
        *required_product_contract_lines(context),
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
    lines.extend(_collaboration_control_plane_lines(context))
    lines.extend(_controlled_exec_contract_lines(context))
    lines.extend(_current_role_template_lines(context))
    if "leaf" in str(context.role or "").lower():
        lines.append("- 叶子节点重点是交付产物和测试文件；父级验收器负责运行命令、判定通过和触发 rescue。")
    lines.extend(_root_execution_contract_lines(context))
    if _is_coordinator_context(context):
        lines.extend(_coordinator_execution_contract_lines())
    return lines


# LLM: Collaboration guidance is rendered only from granted tools, not from task-specific templates.
# 函数用途: 子代理已获得协作工具时，给出通用 case/request/evidence 控制面入口；未授权时不污染普通任务 prompt。
def _collaboration_control_plane_lines(context: SubAgentExecutionContext) -> list[str]:
    tools = set(context.allowed_tools or [])
    granted = [tool for tool in COLLABORATION_CONTROL_PLANE_TOOLS if tool in tools]
    if not granted:
        return []
    return [
        *_collaboration_intro_lines(),
        *_targeted_request_lines(context),
        *_collaboration_tool_lines(tools),
        *_collaboration_closeout_lines(),
    ]


def _collaboration_intro_lines() -> list[str]:
    return [
        "- 协作控制面：你已获得部分多代理协作工具。"
        "当任务需要兄弟代理、上级代理或其他数据源共同补证据/换来源时，"
        "不要只在自然语言报告里描述协作，应该用已授权工具留下结构化 case、request 或 evidence 引用。"
    ]


def _targeted_request_lines(context: SubAgentExecutionContext) -> list[str]:
    requests = _targeted_collaboration_requests(context)
    if not requests:
        return []
    lines = [
        "- 点名给你的协作请求：优先复用已有 case/request；"
        "处理顺序是 case_status -> submit_evidence -> update_collaboration_request。"
        "除非发现全新问题，不要另开 open_case。"
    ]
    for request in requests[:3]:
        lines.extend(_single_targeted_request_lines(request))
    return lines


def _single_targeted_request_lines(request: dict[str, object]) -> list[str]:
    lines = [_request_ref_line(request)]
    if request.get("observed_facts"):
        lines.append("- 线索事实：observed_facts 是开放世界线索包；kind/label/value 由请求方定义，你要按自己的数据源判断如何查询，不要把 kind 当封闭枚举。")
    if request.get("query_hints"):
        lines.append("- 查询提示：query_hints 是软提示；可以完整查、拆分查、改写查、扩大/缩小范围或换来源，提交 evidence 时尽量说明 queried_scopes、used_query_hints、limitations。")
    if request.get("response_contract"):
        lines.append("- 响应形状：优先按 response_contract 返回 matched、evidence_refs、queried_scopes、limitations；查不到也要提交 matched=false 的证据包和 miss_reason。")
    return lines


def _request_ref_line(request: dict[str, object]) -> str:
    return (
        "- 点名请求详情："
        f"case_id={request.get('case_id') or ''}; "
        f"request_id={request.get('request_id') or ''}; "
        f"case_ref={request.get('case_ref') or ''}; "
        f"request_ref={request.get('request_ref') or ''}; "
        f"question={request.get('question') or ''}"
    )


def _collaboration_tool_lines(tools: set[str]) -> list[str]:
    lines: list[str] = []
    for name, message in COLLABORATION_TOOL_HINTS:
        if name in tools:
            lines.append(message)
    if {"open_case", "request_collaboration"}.issubset(tools) and "raise_collaboration_event" not in tools:
        lines.append("- 如果 open_case 后还需要其他代理回应、补证据或确认同一实体，建议继续调用 request_collaboration 留下明确 request；只记录事件时可以停在 open_case。")
    return lines


def _collaboration_closeout_lines() -> list[str]:
    return ["- 协作结果要落到账本或产物：最终 evidence_packets/next_actions 中引用 collaboration://case/<id>、collaboration://request/<id> 或真实 artifact/evidence refs，方便上级从 tree/case 状态继续看和调度。"]


# LLM: _targeted_collaboration_requests reads machine-provided request refs without inferring from the goal.
# 函数用途: 从 context_bundle.collaboration 中取出点名当前 runner 的协作请求，限制 prompt 展开量。
def _targeted_collaboration_requests(context: SubAgentExecutionContext) -> list[dict[str, object]]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    collaboration = bundle.get("collaboration")
    if not isinstance(collaboration, dict):
        return []
    requests = collaboration.get("targeted_requests")
    if not isinstance(requests, list):
        return []
    return [dict(item) for item in requests if isinstance(item, dict)]


# LLM: _controlled_exec_contract_lines isolates grant-specific runner guidance from the base prompt builder.
# 函数用途: 当前 runner 拿到 controlled_exec grant 时，追加真实执行、refs 和 task trash 规则。
def _controlled_exec_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    if not context.controlled_exec_grants:
        return []
    return [
        "- controlled_exec 只能使用 controlled_exec_grants 里的父级 grant；"
        "不要在工具参数里自填 command_allowlist/path_scope/network_scope。",
        "- 目标要求 controlled_exec 真实执行时，dry_run/allowed plan 不算完成；"
        "必须用 apply=true，并在最终 refs 中写出执行 payload 里的 stdout_ref、audit_ref。"
        "复杂 python 片段优先用 argv 数组，例如 command=[\"python3\",\"-c\",\"print('x' * 2000)\"]，避免 shell 引号歧义。",
        "- rm/rmdir/unlink 不会进入 command_allowlist，这是安全设计；"
        "如果 controlled_exec_grants.delete_policy.mode=task_trash，已有 controlled_exec grant 时直接调用 controlled_exec apply=true 执行删除命令，工具会改走 task_trash 并返回 trash_manifest_ref，"
        "不要为了裸 rm 再提交 capability_request；只有 moved=true 且有 trash_manifest_ref 才能把删除验收写成 PASS。",
    ]


# LLM: _root_execution_contract_lines keeps root-only policy text out of the base contract body.
# 函数用途: 只在 root runner prompt 中追加能力决策规则，普通子代理不读取这段。
def _root_execution_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    if not _is_root_context(context):
        return []
    return [
        "- root 不走 capability_request；root 当前不应缺能力。"
        "遇到任务内普通缺口时使用现有工具、创建/调度下级或直接说明暂不支持；不要写 OPEN 能力申请。"
    ]


# LLM: _is_root_context keeps root-only instructions out of ordinary child prompts.
# 函数用途: 判断当前 runner 是否为 root，避免普通子代理拿到 root 专用能力规则。
def _is_root_context(context: SubAgentExecutionContext) -> bool:
    parent_id = str(context.parent_id or "").strip()
    root_id = str(context.root_id or "").strip()
    return not parent_id and (not root_id or root_id == context.run_id or context.depth == 0)


# LLM: _coordinator_execution_contract_lines keeps delegation guidance readable without disabling the agent.
# 函数用途: 生成 coordinator/lead 专属执行规则，包括模板选择、权限继承、派工建议和调度失败处理；角色偏好不等于能力剥夺。
def _coordinator_execution_contract_lines() -> list[str]:
    lines = ["可用角色模板索引："]
    lines.extend(f"  {line}" for line in role_template_index_text().splitlines())
    lines.append("模板详情：")
    lines.extend(f"  {line}" for line in role_template_detail_text().splitlines())
    lines.extend(coordinator_execution_policy_lines())
    return lines


# LLM: _current_role_template_lines gives each runner its own role prompt without loading all templates.
# 函数用途: 只展开当前角色的模板详情；普通 worker/tester 等能拿到专属提示，leaf_worker 等无模板角色保持精简。
def _current_role_template_lines(context: SubAgentExecutionContext) -> list[str]:
    if _is_leaf_worker_context(context):
        return []
    detail = role_template_detail_text(roles=[str(context.role or "")]).strip()
    if not detail or _is_coordinator_context(context):
        return []
    return ["当前角色模板详情：", *[f"  {line}" for line in detail.splitlines()]]


# LLM: _is_leaf_worker_context keeps concrete leaves slim while still letting contracts grant worker tools.
# 函数用途: leaf_worker 只需要边界、工具和验收条件，不加载完整 worker 模板，避免大批叶子节点 prompt 变厚。
def _is_leaf_worker_context(context: SubAgentExecutionContext) -> bool:
    return "leaf_worker" in str(context.role or "").lower()


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

    payload = json.dumps(runner_context_summary_payload(context), ensure_ascii=False, indent=2)
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
        f"{SUBAGENT_REPAIR_RESULT_TEMPLATE}\n\n"
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
