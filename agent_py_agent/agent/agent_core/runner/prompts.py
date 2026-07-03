
from __future__ import annotations

"""builds subagent runner execution prompts and structured-output repair audit text.

runner 真正调用模型前，需要把执行上下文压成明确任务；模型输出不合格式时，还要生成一次'只修格式'的补救 prompt。
这些 prompt 模板都放这里，避免主流程函数越来越长。
"""

import json

from ...subagents import SubAgentExecutionContext
from ...subagents.context_bundle import context_gate_prompt_lines
from ...subagents.role_templates import (
    role_template_detail_text,
    role_template_index_text,
    role_template_snapshot_for_role,
)
from ..subagent import compact_continuation as subagent_compact_continuation
from .prompt_context_summary import runner_context_summary_payload

SUBAGENT_RESULT_TEMPLATE = (
    "[SUBAGENT_RESULT]\n"
    "{\n"
    '  "status": "DONE",\n'
    '  "summary": "本轮完成或卡住的摘要",\n'
    '  "used_tools": [],\n'
    '  "used_skills": [],\n'
    '  "evidence": [\n'
    '    {"kind": "command", "summary": "验证摘要", "command": "", "path": "", "url": "", "ok": true}\n'
    "  ],\n"
    '  "evidence_packets": [\n'
    '    {"id": "evpkt-run-id-short", "claim": "可验收声明", "checked_scope": "检查范围", "evidence_refs": ["runner_result.json"], "artifact_refs": ["产物路径或output.json"], "confidence": 0.9}\n'
    "  ],\n"
    '  "coverage_records": [\n'
    '    {"covered_run_id": "失败或损坏的run_id", "covered_by_run_id": "已DONE/VERIFIED的覆盖run_id", "reason": "为什么覆盖同一范围", "artifact_refs": ["覆盖者产物路径"], "evidence_refs": ["覆盖者证据路径"]}\n'
    "  ],\n"
    '  "capability_requests": [\n'
    '    {"problem": "缺少什么", "needed_capability": "能力名", "capability_type": "shell|tool|skill|mcp|network|generic", "expected_output": "希望得到什么", "requested_tools": [], "requested_skills": [], "requested_mcp_tools": [], "requested_commands": ["python3"], "cwd_scope": [], "path_scope": ["任务内需要访问的目录"], "network_scope": [], "output_budget": {"stdout_bytes": 65536, "stderr_bytes": 32768}, "risk_level": "low|medium|high", "tried": [], "evidence": [], "constraints": {}, "alternatives_attempted": [], "escalation_target": "parent"}\n'
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
    '  "lessons": ["<把你这次任务真正踩到的坑/学到的可复用经验写成一句话；没有就给空数组 []，不要照抄本提示>"],\n'
    '  "next_actions": ["建议父代理下一步动作"],\n'
    '  "blocked_reason": "",\n'
    '  "failure_type": ""\n'
    "}\n"
    "[/SUBAGENT_RESULT]\n"
)

SUBAGENT_REPAIR_RESULT_TEMPLATE = (
    "[SUBAGENT_RESULT]\n"
    "{\n"
    '  "status": "DONE",\n'
    '  "summary": "本轮完成或卡住的摘要",\n'
    '  "used_tools": [],\n'
    '  "used_skills": [],\n'
    '  "evidence": [\n'
    '    {"kind": "artifact", "summary": "已检查的产物或报告", "path": "产物路径或报告路径", "ok": true}\n'
    "  ],\n"
    '  "evidence_packets": [\n'
    '    {"id": "evpkt-repair-run-id-short", "claim": "可验收声明", "checked_scope": "修复整理范围", "evidence_refs": ["报告或output.json路径"], "artifact_refs": ["产物路径"], "confidence": 0.8}\n'
    "  ],\n"
    '  "coverage_records": [],\n'
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

_REQUIRED_OUTPUT_GUIDE = (
    "- 说明完成了什么或卡在哪里。\n"
    "- 列出使用过的授权工具或 skill。\n"
    "- 给出可验收证据；成功时 evidence_packets 必须有 artifact_refs 或 evidence_refs，不能只写普通 evidence。\n"
    "- 如果你认为某个失败、损坏或超时的 child run 已由另一个已完成 run 覆盖，必须写 coverage_records；"
    "只在 summary 里说“已覆盖”不会被最终收口认可。\n"
    "- 只填你这次真用到的字段，用不到的留空数组 [] 或省略；不要为了填满模板而编造内容，长报告写文件后引用路径。\n"
    "- 最后必须输出一个机器可解析结果块（裸 JSON，不要 ```json 或 Markdown 围栏），**绝对不能交空块**。\n"
    "  最小必填：大多数任务只要 status、summary、artifacts（你真写出来的产物文件路径）这三项就够。完成时照这个最小示例填即可：\n\n"
    "[SUBAGENT_RESULT]\n"
    '{"status": "DONE", "summary": "一句话说清你完成了什么", "artifacts": [{"path": "你写出的文件绝对路径", "kind": "file", "summary": "结果文件"}]}\n'
    "[/SUBAGENT_RESULT]\n\n"
    "  没干完/卡住时也必须如实填、同样不能交空块：status 填 BLOCKED，blocked_reason 一句话说清卡在哪、缺什么。\n"
    "  在最终结果块之前，不要把 [SUBAGENT_RESULT] 或 [/SUBAGENT_RESULT] 当普通说明文字引用。\n"
    "- 下面是【全字段参考】，需要某个字段时才照它填；简单任务别被这个大模板吓到，按上面“最小必填”填就对了：\n\n"
)


COLLABORATION_CONTROL_PLANE_TOOLS = (
    "inspect_collaboration",
    "raise_collaboration",
    "submit_collaboration_result",
    "update_collaboration",
)

COLLABORATION_TOOL_HINTS = (
    ("inspect_collaboration", "- inspect_collaboration：先读取已有 case/request 状态，避免重复开 case 或重复提交同一份证据。"),
    (
        "inspect_collaboration",
        "- inspect_collaboration：如果你知道自己被要求协作、但不知道 case_id/request_id，"
        "先用它按自己的 agent_id/agent_name/role 查询待响应请求；不要因为缺 case_id 就新开重复 case。",
    ),
    (
        "raise_collaboration",
        "- raise_collaboration：如果你发现需要其他代理、其他数据源或上级共同补证据，"
        "优先用这个单步工具打开 case 并发出 request；不要只在 output.json 里写 collaboration_required。"
        "它接受 observed_facts/query_hints/response_contract 等开放世界字段，不要求业务专项格式。",
    ),
    (
        "raise_collaboration",
        "- raise_collaboration：发现需要多代理共同研判、补证据、换数据源或跟踪阻塞时，"
        "打开通用协作 case，写清 title、summary、entities 和 required_capabilities。",
    ),
    (
        "raise_collaboration",
        "- raise_collaboration：需要其他代理补证据时发起请求，写清 question、target_agent_ids 或 required_capabilities；"
        "如果发现的是可被多方查证的线索，把 observed_facts、query_intent、query_hints、response_contract 和 context_refs 一起交出去。"
        "query_hints 是软提示，响应代理可自行拆分或改写。请求引用形如 collaboration://request/<id>。",
    ),
    (
        "submit_collaboration_result",
        "- submit_collaboration_result：回应协作请求时提交 refs-first 证据，优先给 evidence_refs/artifact_refs、matched、confidence 和简短 summary，"
        "不要把长正文塞进消息。",
    ),
    (
        "update_collaboration",
        "- update_collaboration：完成、阻塞或需要返工时更新 request 状态，把 actor_agent_id、summary 和必要的 request 引用写清楚。",
    ),
    (
        "update_collaboration",
        "- update_collaboration：原目标没有证据、不可用或更合适的来源已出现时，用结构化 target_agent_ids 改派；"
        "不要只在 summary 里说已经协作或已经转派。",
    ),
)


# 子代理默认 thought / plan 模板的权威位置；create_policy 派工链路只引用不复制。
SUBAGENT_DEFAULT_THOUGHT = "根据父代理派工执行，并保留可验收证据。"
SUBAGENT_DEFAULT_PLAN: tuple[str, ...] = ("理解目标", "执行任务", "产出证据", "交回真实结果和证据")


def subagent_runner_system_prompt(context: SubAgentExecutionContext) -> str:
    return (
        "你是 my-agent 的子代理 runner，不是顶层 root 主代理。"
        f"你的 run_id 是 {context.run_id}，名字是 {context.agent_name or '未命名子代理'}，"
        f"角色是 {context.role or 'worker'}。\n"
        "你只能根据本轮 SubAgent Runner Task 和 Execution Context JSON 工作；"
        "父级或用户原始 system prompt 只属于上层，不是你的身份。"
        "如果需要下级协作，必须使用授权的子代理编排工具；如果只是具体交付，就在授权写入边界内产出文件和证据。\n"
        "不要编造工具结果、run_id、文件内容、收口交给父级已经批准的事实。\n"
        "完成纪律：如果任务在 goal 或输出要求里点名了要产出的文件（明确给了产物路径），"
        "在你亲手把该文件真正写出来、并确认它存在之前，不要输出最终完成结果——"
        "继续调用写文件工具把它做出来。确实做不到就如实标记未完成或上抛能力请求，"
        "不要用“进行中/下一步再写”这类中间汇报冒充完成。\n"
        "判读纪律（监控/筛查/排查类任务）：采集拉数可以写脚本代劳，但【每条候选是否命中必须"
        "你自己按判据看数据定性】——脚本里的关键字/字段过滤只是初筛不算判断；脚本报 0 命中"
        "≠真没有，先亲自抽样读几条原始数据核实过滤逻辑没漏（字段名/类型/嵌套层级都要对上）"
        "再采信脚本结论。\n"
        "盯守纪律（goal 要求持续监控/盯满某时长的任务）：盯满要求的时长才算完成，只做基线"
        "采样或只跑一小段就交报告不算；时长内要持续消费到数据流当前末尾（游标跟上进度），"
        "跟不上就如实写明覆盖了哪段、漏了哪段，不要把部分覆盖说成全程监控。"
        "盯高吞吐游标源（每秒几十/上百条，逐条读根本读不过来）优先用 watch_stream 工具："
        "open 打开后循环 pull（带 max_wait_seconds=30~55）——它在代码层消费全量数据流做"
        "结构化初筛，把稀有候选按批给你研判（每条你仍须亲自看触发端+结果端两头定性），"
        "游标自动跟到流末尾、断点可续；不要用自己写死判据的脚本顶替逐条研判。\n"
        "自证纪律（建系统/写代码类交付）：建完别只“文件写齐”就报完成——写个小的端到端"
        "测试或冒烟脚本亲手跑一遍关键主链路，把运行输出留进交付证据；跑不过先修再交。\n"
        "数据纪律（分析/统计/排名类交付）：聚合前先识别明显异常记录（缺字段/重复/数量级"
        "离谱的极端值），剔除或单列后再算汇总排名，报告注明剔除口径与条数；"
        "对脏数据直接求和排序会把结论带偏。"
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


def runtime_guidance_prompt_block(context: SubAgentExecutionContext) -> str:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    guidance = bundle.get("runtime_guidance")
    if not isinstance(guidance, list) or not guidance:
        return ""
    lines = [
        "## GUIDANCE_DELIVERED\n\n",
        "以下是运行中追加给你的补充提示，只作为普通补充消息进入上下文。"
        "运行时不会把这些文字解释成新的硬门，也不会自动替换当前用户消息。\n",
    ]
    for index, item in enumerate(guidance[:20], start=1):
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        priority = str(item.get("priority") or "normal").strip() or "normal"
        sender = str(item.get("sender") or "").strip()
        guidance_id = str(item.get("guidance_id") or "").strip()
        target_type = str(item.get("target_type") or "").strip()
        target_id = str(item.get("target_id") or "").strip()
        sender_text = f"; sender={sender}" if sender else ""
        lines.append(
            f"{index}. guidance_id={guidance_id}; target={target_type}:{target_id}; priority={priority}{sender_text}: {message}\n"
        )
    return "".join(lines) + "\n"


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
    guidance_block = runtime_guidance_prompt_block(context)
    return (
        "# SubAgent Runner Task\n\n"
        "你是一个被父代理授权的子代理，只能依据下面的执行上下文工作。\n"
        "不要使用上下文之外的 skill/tool，不要假完成；没有验收证据时只能标记等待收口或上抛能力请求。\n\n"
        "## Extra Instruction\n\n"
        f"{extra}\n\n"
        "## Runner Contract\n\n"
        f"{execution_contract}\n\n"
        "## Context Bundle Gate\n\n"
        f"{context_gate}\n\n"
        f"{guidance_block}"
        f"{compact_block}"
        "## Execution Context JSON\n\n"
        "下面是瘦身后的执行摘要；完整上下文请按 refs 读取，不要让模型一次吞完整大 JSON。\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Required Output\n\n"
        f"{_REQUIRED_OUTPUT_GUIDE}"
        f"{SUBAGENT_RESULT_TEMPLATE}"
    )


def _runner_execution_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    lines = [
        "- 只把真正阻止你产出文件、报告或证据的缺口写成 capability_request。",
        "- 如果你没有 shell/command/terminal 工具，不要因为不能自己运行 pytest 就提交 capability_request。",
        "- 没有命令执行工具时，应写出产物和测试建议；不要假装你已经执行过命令。",
        "- tests 里的命令必须安全、具体、可复制：不要写 cd ... &&，把目录写在 \"working_dir\" 字段里。",
        "- 验证文件内容时优先写 validation_method=\"content_check\"、file_path、content_pattern 或 content_equals、match_mode=\"exact\"，不要写 cat 文件命令。",
        "- 写代码和测试后，必须逐条对照验收条件做静态自检，确保实现、测试、README 三者互相一致。",
        "- 写 Python 测试时必须保证从 working_dir 运行能导入被测模块；优先把测试文件和模块放同一目录，或显式处理 import path。",
        "- 如果用户要求按钮、链接或图片不能失效，不要用 href=\"#\"、空锚点或不存在的 #id 假装可点击；"
        "页面内跳转必须指向真实存在的元素 id，按钮必须有真实交互或真实本地目标。",
        "- 生成普通报告或中等长度文本时，优先用 write_file 的 content 字段完整写入。"
        "生成长 CSS/JS/HTML、大段代码或长报告时，可以用 "
        "独立成行的 [WRITE_FILE_RAW path=\"...\"]...[/WRITE_FILE_RAW] 原文块（独立原文块，不要写进任何工具调用的参数里，也不要把 WRITE_FILE_RAW 写进 JSON 的 tool 字段）。"
        "局部修改已有文件用 apply_patch。PDF、XLSX、图片等二进制产物可用授权命令/脚本生成，再用 write_file.data_base64 写入。",
        "- write_file 会自动创建父目录；不要因为目标目录尚未创建就标记 BLOCKED。"
        "普通输出路径按 workspace_root/path_access_mode 解析，只有危险目录或显式禁止路径才会被拒绝。",
        *read_ref_context_lines(context),
        *required_product_contract_lines(context),
        "- 如果最终结果需要列很多 artifacts 或证据，优先用 write_file 写 execution_context.output_json 的短 JSON；"
        "系统会自动把它包成 SUBAGENT_RESULT 收口，避免对话里的长结果块被截断。",
        "- output.json 是内部收口文件名；只能写 execution_context.output_json，"
        "不要在 product_write_roots、deliverables 或用户产物目录里创建 output.json。",
        "- 需要当前工具目录之外的新工具、skill、MCP、网络或运行权限时，写 capability_request；"
        "说清楚 problem、needed_capability、capability_type、requested_tools/requested_skills/requested_mcp_tools 和 expected_output 即可，"
        "不要把父级授权细节、grant、path_scope、output_budget 当成普通任务步骤。",
        "- 如果 Tool Catalog 里有 capability_request 工具，缺能力时必须先调用该工具记录正式申请；"
        "不要在产物目录写 capability_request.json，也不要改 execution_context.json 伪造 pending_requests。",
        "- capability_request 工具返回 OPEN 后，最终结果块写 status=PENDING_CAPABILITY_REQUEST 或 BLOCKED，"
        "不要继续假装能力已经授权或命令已经执行。",
    ]
    lines.extend(_collaboration_control_plane_lines(context))
    lines.extend(_controlled_exec_contract_lines(context))
    lines.extend(_current_role_template_lines(context))
    lines.extend(_root_execution_contract_lines(context))
    if _is_coordinator_context(context):
        lines.extend(_coordinator_execution_contract_lines())
    return lines


def read_ref_context_lines(context: SubAgentExecutionContext) -> list[str]:
    payload = runner_context_summary_payload(context)
    refs = payload.get("read_refs") if isinstance(payload.get("read_refs"), dict) else {}
    declared = [str(item) for item in refs.get("required_read_paths") or [] if str(item).strip()]
    resolved = [str(item) for item in refs.get("resolved_read_paths") or [] if str(item).strip()]
    if not declared and not resolved:
        return []
    lines = [
        "- read_refs 是父级给你的可读线索和授权范围，不是启动前置门。"
        "能读就读；某条路径不存在时，记录限制、尝试其他线索或向父级说明，不要因为单条线索缺失直接卡死。",
    ]
    if declared:
        lines.append(f"- declared_read_paths: {', '.join(declared[:8])}")
    if resolved:
        lines.append(f"- resolved_read_paths: {', '.join(resolved[:8])}")
    return lines


def required_product_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    contract = bundle.get("output_contract") if isinstance(bundle.get("output_contract"), dict) else {}
    refs = [str(item) for item in contract.get("required_file_refs") or [] if str(item).strip()]
    if not refs:
        return []
    return [
        "- 用户要求的业务产物必须写到 output_contract.required_file_refs 中的精确路径；"
        "最终 SUBAGENT_RESULT 的 artifacts/evidence_packets.artifact_refs 也必须引用这些业务产物路径。",
        "- agent_run_final_report_ref 是系统内部交接报告，不是用户要求的业务产物；"
        "除非它同时出现在 required_file_refs 里，否则不能把它当作交付文件。",
        f"- required_file_refs: {', '.join(refs[:8])}",
    ]


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
        "处理顺序是 inspect_collaboration -> submit_collaboration_result -> update_collaboration。"
        "除非发现全新问题，不要另开 raise_collaboration。"
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
        lines.append(
            "- 响应建议：response_contract 只是请求方给你的整理参考；你可以按实际查询结果提交命中、未命中、证据引用、查询范围和限制说明。"
        )
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
    return lines


def _collaboration_closeout_lines() -> list[str]:
    return ["- 协作结果要落到账本或产物：最终 evidence_packets/next_actions 中引用 collaboration://case/<id>、collaboration://request/<id> 或真实 artifact/evidence refs，方便上级从 tree/case 状态继续看和调度。"]


def _targeted_collaboration_requests(context: SubAgentExecutionContext) -> list[dict[str, object]]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    collaboration = bundle.get("collaboration")
    if not isinstance(collaboration, dict):
        return []
    requests = collaboration.get("targeted_requests")
    if not isinstance(requests, list):
        return []
    return [dict(item) for item in requests if isinstance(item, dict)]


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


def _root_execution_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    if not _is_root_context(context):
        return []
    return [
        "- root 不走 capability_request；root 当前不应缺能力。"
        "遇到任务内普通缺口时使用现有工具、创建/调度下级或直接说明暂不支持；不要写 OPEN 能力申请。"
    ]


def _is_root_context(context: SubAgentExecutionContext) -> bool:
    parent_id = str(context.parent_id or "").strip()
    root_id = str(context.root_id or "").strip()
    return not parent_id and (not root_id or root_id == context.run_id or context.depth == 0)


def _coordinator_execution_contract_lines() -> list[str]:
    lines = ["可用角色模板索引："]
    lines.extend(f"  {line}" for line in role_template_index_text().splitlines())
    lines.append("模板详情：")
    lines.extend(f"  {line}" for line in role_template_detail_text().splitlines())
    lines.extend(coordinator_execution_policy_lines())
    return lines


def coordinator_execution_policy_lines() -> list[str]:
    return [
        "- coordinator/lead 节点拥有完整基础读写能力：可以写自己的计划、证据、协调报告，也可以在授权产物根里检查、修复或接管。",
        "- coordinator/lead 的第一目标是让团队动起来：先读取最小必要材料来理解目标、目录、评分和质量边界，"
        "不要在派工前把所有正文、数据表、长报告都自己读完。能拆给 child 的研究、实现、测试和汇总，先创建并 dispatch child。",
        "- child 完成前，coordinator/lead 只跟踪状态、refs、summary、blockers 和必要的路径纠偏；"
        "child 完成后，再按 artifact_refs/evidence_refs 读取必要证据做汇总。不要把所有 child 正文一次性吞回自己的上下文。",
        "- 派工是为了把活做好，不是硬流程。任务小、用户要求你亲自检查/修复、或下级卡住时，你可以直接完成；"
        "任务大、可并行或需要多人视角时，优先创建 worker/tester 等 child。",
        "- 创建 child 时，把目标路径、文件名、质量要求原样传给下一层，然后用 dispatch_subagents 推进直接 child。",
        "- 如果缺口只属于未来 child/leaf 的执行能力，例如 leaf 才需要 controlled_exec、shell、网络或某个 skill，"
        "coordinator/lead 不要替后代提前提交 capability_request 后停止；先创建并 dispatch 对应 child，"
        "由真正需要该能力的 runner 正式申请，父级再 route grant 并继续推进。",
        "- 给 child 写 goal 时，不要要求它在产物目录写 output.json；"
        "如需结构化汇报，只能要求它写自己的 execution_context.output_json。",
        "- coordinator 可以继续创建 coordinator 作为下一层领导节点；"
        "需要多层协作时不要误以为只能创建 worker；父级要求多层链路时，深度未到目标层前先创建下一层 coordinator。",
        "- 如果父级目标或质量要求点名需要 tester、bug_finder、reviewer、找错或测试角色，"
        "必须创建真实 child run，并把 role/agent_name 写成对应角色；只在 goal、summary 或 evidence 里提到这些词不算角色覆盖。",
        "- 当生产 child 已完成，但父级合同仍缺 tester/bug_finder 时，"
        "不要直接输出最终 SUBAGENT_RESULT；先调用 schedule_child_subagents 获取或执行 quality_advice，"
        "再由你按 ready refs、风险和 scope 选择 QA 数量、顺序和是否需要 repair。",
        '- schedule_child_subagents 的参数必须放在顶层，例如 {"tool":"schedule_child_subagents","dry_run":false,"children":[...]}；'
        "不要包二级参数对象，长目标请分多次调用，每次 1-2 个 child。",
        "- 不要让 worker/writer 代写 coordinator 自己的协调证据；需要共享时引用 artifact_refs/evidence_refs。",
        "- 创建 child/leaf 时必须原样传递父级指定的文件名、目录和质量要求，不要把 solution.py 改成别的模块名。",
        "- 同一次 schedule_child_subagents 可以混建 coordinator、worker 或 tester；调度层只返回创建、复用和待 dispatch 的状态，是否继续拆分或修正由你根据 tree/refs 判断。",
        "- 创建 worker 后使用 dispatch_subagents(dry_run=false, run_ids=[...]) 推进直接 child，并汇总 worker 的产物 refs。",
        "- 多个 child 同轮 dispatch 时不要写子任务专属 runner_instruction；需要专属补充就按单个 run_id 分多次 dispatch。",
        "- dispatch_subagents 返回 child test_failed 或 followup_action=plan_rescue 时，不要宣称完成；先汇报失败 refs 或安排修复。",
        "- dispatch_subagents 返回 direct_children.qa_repair_advice 或 needs_repair_wave 时，不要直接报完成；"
        "先按失败 QA refs 创建 scoped repair worker，修复后再让 tester 复测。",
        "- 少数下属需要不同纠偏、路径修正或需求变更时，优先用 send_guidance 点名具体 run_id；"
        "dispatch_subagents 只在需要立刻推进、恢复或重跑时使用。"
        "平级讨论要走允许的定向通道，不能广播到兄弟分支的子孙。",
    ]


def _current_role_template_lines(context: SubAgentExecutionContext) -> list[str]:
    del context
    return []


def _is_coordinator_context(context: SubAgentExecutionContext) -> bool:
    tools = set(context.allowed_tools or [])
    snapshot = context.role_template or role_template_snapshot_for_role(str(context.role or ""))
    return bool(snapshot.get("can_spawn_children")) and "schedule_child_subagents" in tools


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


def _clip_repair_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"[... clipped {len(text) - limit} chars ...]\n{text[-limit:]}"
