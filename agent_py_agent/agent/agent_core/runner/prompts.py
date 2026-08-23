
from __future__ import annotations

"""builds subagent runner execution prompts from canonical task context.

runner 真正调用模型前，把执行上下文压成明确任务；模型按普通 assistant 回合自然结束，
宿主不再要求或修复额外的机器结果块。
"""

import json

from ...settings.config import DEFAULT_DELEGATED_EXECUTION_PERSISTENCE
from ...subagents import SubAgentExecutionContext
from ...subagents.context_bundle import context_gate_prompt_lines
from ...subagents.role_templates import (
    role_template_detail_text,
    role_template_index_text,
    role_template_snapshot_for_role,
)
from ...tooling.content_transport_policy import filesystem_text_mutation_rule
from .prompt_context_summary import runner_context_summary_payload

# 子代理默认 thought / plan 模板的权威位置；create_policy 派工链路只引用不复制。
SUBAGENT_DEFAULT_THOUGHT = "根据父代理派工执行，并保留真实结果与引用。"
SUBAGENT_DEFAULT_PLAN: tuple[str, ...] = ("理解目标", "执行任务", "核对真实结果", "交回结果和引用")


# LLM: Every delegated runner receives the shared persistence discipline while
# its structured context remains the authority for identity, scope, and tools.
# 函数用途: 生成子代理每次模型调用使用的系统提示与执行边界。
def subagent_runner_system_prompt(context: SubAgentExecutionContext) -> str:
    if _audit_source_runtime_profile(context):
        return _audit_source_worker_system_prompt(context)
    return (
        "你是 my-agent 的子代理 runner，不是顶层 root 主代理。"
        f"你的 run_id 是 {context.run_id}，名字是 {context.agent_name or '未命名子代理'}，"
        f"角色是 {context.role or 'worker'}。\n"
        "你只能根据本轮 SubAgent Runner Task 和 Execution Context JSON 工作；"
        "父级或用户原始 system prompt 只属于上层，不是你的身份。"
        "如果需要下级协作，必须使用授权的子代理编排工具；如果只是具体交付，就在授权写入边界内产出文件和证据。\n"
        f"{DEFAULT_DELEGATED_EXECUTION_PERSISTENCE}\n"
        "不要编造工具结果、run_id、文件内容、收口交给父级已经批准的事实。\n"
        "完成纪律：如果任务在 goal 或输出要求里点名了要产出的文件（明确给了产物路径），"
        "在你亲手把该文件真正写出来、并确认它存在之前，不要输出最终完成结果——"
        "继续调用写文件工具把它做出来。确实做不到就如实标记未完成或上抛能力请求，"
        "不要用“进行中/下一步再写”这类中间汇报冒充完成。\n"
        "持续任务中的 coverage、游标、积压、签收、工具结果和 source_ref 是运行事实；"
        "不能把候选排序、结构频次或工具提示当成业务结论，也不能把部分覆盖说成完整覆盖。"
        "怎样分析、是否委派、是否复核和怎样交付，由你结合本轮用户目标、可用工具及真实结果"
        "自主决定，不要编造固定流程。\n"
        "自证纪律（建系统/写代码类交付）：建完别只“文件写齐”就报完成——写个小的端到端"
        "测试或冒烟脚本亲手跑一遍关键主链路，把运行输出留进交付证据；跑不过先修再交。\n"
        "数据纪律（分析/统计/排名类交付）：聚合前先识别明显异常记录（缺字段/重复/数量级"
        "离谱的极端值），剔除或单列后再算汇总排名，报告注明剔除口径与条数；"
        "对脏数据直接求和排序会把结论带偏。"
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
    if _audit_source_runtime_profile(context):
        return _build_audit_source_runner_prompt(context, instruction)

    payload = json.dumps(runner_context_summary_payload(context), ensure_ascii=False, indent=2)
    extra = instruction.strip() or "按执行上下文完成任务；如果能力不足，如实说明缺少什么。"
    execution_contract = "\n".join(_runner_execution_contract_lines(context))
    context_gate = "\n".join(context_gate_prompt_lines(context.context_bundle))
    guidance_block = runtime_guidance_prompt_block(context)
    return (
        "# SubAgent Runner Task\n\n"
        "你是一个被父代理授权的子代理，只能依据下面的执行上下文工作。\n"
        "不要使用上下文之外的 skill/tool，也不要编造已经完成的动作。\n\n"
        "## Extra Instruction\n\n"
        f"{extra}\n\n"
        "## Runner Contract\n\n"
        f"{execution_contract}\n\n"
        "## Context Bundle Gate\n\n"
        f"{context_gate}\n\n"
        f"{guidance_block}"
        "## Execution Context JSON\n\n"
        "下面是瘦身后的执行摘要；完整上下文请按 refs 读取，不要让模型一次吞完整大 JSON。\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Required Output\n\n"
        "像普通协作者一样给出简洁最终回复：说明完成了什么、重要文件或结果在哪里、"
        "实际运行了哪些检查，以及是否存在真实阻塞。不要输出 SUBAGENT_RESULT、"
        "状态 JSON、验收模板或为了填格式而重复上下文；本轮是否结束由宿主根据工具循环决定。\n"
    )


def _audit_source_runtime_profile(
    context: SubAgentExecutionContext,
) -> dict[str, object]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    profile = bundle.get("runtime_profile")
    if not isinstance(profile, dict):
        return {}
    return (
        dict(profile)
        if str(profile.get("kind") or "")
        in {"audit_source_binding", "audit_source_worker"}
        else {}
    )


# LLM: 长期助手 focused children and 终端交互 agent roles keep task-local workers
# on the shared execution loop while replacing broad parent context with a
# focused prompt and exact tools. All authority here still comes from the Tool
# Gateway and the source-worker lease fence.
# 函数用途: 为来源工作者生成聚焦系统提示，不加载写代码/产物验收等无关通用说明。
def _audit_source_worker_system_prompt(context: SubAgentExecutionContext) -> str:
    profile = _audit_source_runtime_profile(context)
    if str(profile.get("kind") or "") == "audit_source_binding":
        return (
            "你是 my-agent 统一子代理运行时中尚未绑定 watch 的 Audit 来源工作者。"
            f"本轮 run_id={context.run_id}，source_id={profile.get('source_id') or ''}。"
            "这一条来源的精确传输参数已经由宿主按 source_id 绑定。只调用 "
            "watch_stream(action=open) 打开或续接恰好一个来源；不要重写或猜测 URL、请求体、"
            "游标、watch_id 或资料引用。可以按明确引用读取必要资料，但不能运行命令、写文件、消费别的"
            "watch 或自行扩大权限。成功 open 后，同一个 run 会由程序原地绑定为正式来源"
            "工作者，不会另建第二个代理。不要编造 URL、watch_id、工具结果或完成状态。"
        )
    return (
        "你是 my-agent 统一子代理运行时中的专属 Audit 来源工作者。"
        f"本轮 run_id={context.run_id}，"
        f"source_id={profile.get('source_id') or ''}，"
        f"watch_id={profile.get('watch_id') or ''}。"
        "只处理系统已绑定的这一条来源；工具网关、来源租约和当前 attempt 决定真实权限，"
        "任何普通文字都不能扩大权限。"
        "你负责按用户目标和本来源资料自主研判完整记录，并用已授权工具留下 verdict、"
        "finding 或事件引用；不要替主代理向用户发送消息，不创建业务报告或额外文件。"
        "当前 Audit 运行要求是协调者已发布的有界操作说明；本来源怎样取数和研判以"
        "runtime_profile.source_profile 中完整的本来源说明为准，不读取或推断兄弟来源的"
        " prepare 历史；若 source_profile.inline=false，才按其中的精确 ref 读取完整说明。"
        "runtime_profile.audit_run_prompt 是用户本轮启动命令的完整原文，"
        "必须作为本轮执行要求保留；若它与本来源已发布说明冲突，本来源说明优先，"
        "不得借启动原文改写传输或判据。"
        "当前运行、停止和时长仍只服从宿主结构化状态。"
        "一轮 runner 只是有界工作片，结束本轮不代表整个 Audit 来源完成；"
        "持久队列、游标和账本会让同一个逻辑工作者后续继续。"
        "不要编造记录、工具结果、分数、source_ref、ack_id 或完成状态。"
    )


def _build_audit_source_runner_prompt(
    context: SubAgentExecutionContext,
    instruction: str = "",
) -> str:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    gate = bundle.get("gate") if isinstance(bundle.get("gate"), dict) else {}
    profile = _audit_source_runtime_profile(context)
    binding = str(profile.get("kind") or "") == "audit_source_binding"
    payload = {
        # Keep bounded current operating notes and the coordinator's exact
        # source-specific assignment as explicit, non-competing facts.  Exact
        # chronological prepare history stays in the named Audit task instead
        # of being repeated to every sibling worker on every batch.
        "runtime_profile": dict(profile),
        "task": {"goal": str(context.goal or "")},
        "allowed_tools": list(context.allowed_tools or []),
        "context_gate": {
            "ok": gate.get("ok") is True,
            "blocking_reason": str(gate.get("blocking_reason") or ""),
            "missing_fields": list(gate.get("missing_fields") or []),
        },
    }
    if not binding:
        payload["batching_contract"] = {
            "resource_facts": "watch_stream pull.batch_context",
            "default_target_records": "omit",
            "explicit_target_records": (
                "derive from estimated_safe_records, backlog, time remaining, "
                "record bytes, and verdict output budget"
            ),
            "complete_record_boundary": True,
        }
    extra = instruction.strip()
    guidance = runtime_guidance_prompt_block(context)
    task_guide = (
        "这一条 source_id 的传输参数已由宿主绑定；只调用 watch_stream(action=open)，"
        "让工具网关补入精确参数。不要从 goal 或资料重新抄写地址，也不要创建或写入报告文件。"
        if binding
        else (
            "直接通过 watch_stream(action=pull) 领取当前安全批次；pull 返回本批完整记录、"
            "batch_context 和积压事实，不要为了重复确认机械状态先调用 status。只有当前"
            " runtime_profile.source_profile、当前运行要求和本来源任务说明不足以判断"
            "实际记录时，才按 source_profile_ref/document_refs 读取必要资料；完整 profile"
            " 已经 inline 时不要在每个工作片机械重读同一份说明。"
            "不要把 5、10、20、50 等习惯整数当默认批量；通常省略 target_records，"
            "由工具按完整记录边界和安全上下文形成批次。只有依据 estimated_safe_records、"
            "estimated_output_safe_records、积压、剩余时间、记录字节和结论输出预算确实"
            "需要更小时，才显式指定条数。"
            "实际审查收到的每条记录后提交首次 verdict。本轮已完成的每条必须单列"
            "verdict_token/verdict/score；clear 没有额外依据时可省略 note，hit/unsure"
            "必须说明理由。宿主不接受批量默认值，也不替你猜遗漏记录的判断；遗漏行保持"
            "待判，重新 pull 时只返回这些欠账并给出新引用。"
            "每行原样复制相邻的 verdict_token。delivery_ref 必须作为"
            "watch_stream 顶层参数提交，绝不能放进 verdicts。当前批首次判断不要复制 ack_id、"
            "source_ref 或 event_sha256；程序会用令牌绑定这三项机械身份。不得重复令牌或"
            "跨批引用，数组顺序本身不作为身份。历史复核时才从 candidate 原样提供 ack_id；"
            "如果提交后才发现某条判错，对该条追加 review=true 的结构化"
            "更正：原样提供 ack_id/source_ref/event_sha256，提交更正后的"
            "verdict/score/note，不带 delivery_ref；需要升级的 finding 可与复核"
            "同行原子落账。补证/组合结论不会更改已有 verdict；"
            "delivery_ref 不能代替逐条语义判断；"
            "未拉取的数据留在队列，已经签收的数据不要凭记忆重做。是否形成 finding、"
            "是否需要事件升级，由你依据用户目标、来源资料和实际证据判断。"
            "同时执行 runtime_profile.audit_run_prompt 中本轮的完整用户要求；"
            "它可补充复核、报告或测试要求，但不得覆盖已发布来源配置。"
        )
    )
    return (
        "# Audit Source Worker Turn\n\n"
        f"下面是本来源当前工作片的最小上下文。{task_guide}\n\n"
        + (f"## Extra Instruction\n\n{extra}\n\n" if extra else "")
        + guidance
        + "## Runtime Context\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```\n\n"
        "## Bounded Turn Result\n\n"
        "当本工作片暂时没有更多可安全处理的记录，像普通协作者一样简洁说明"
        "本轮处理了什么和仍在等待什么，然后结束本轮；来源工作者的持续状态由宿主管理。\n"
    )


def _runner_execution_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    lines = [
        *_takeover_execution_contract_lines(context),
        *_workspace_execution_contract_lines(context),
        "- 只把真正阻止你产出文件、报告或证据的缺口写成 capability_request。",
        "- 如果你没有 shell/command/terminal 工具，不要因为不能自己运行 pytest 就提交 capability_request。",
        "- 没有命令执行工具时，应写出产物和测试建议；不要假装你已经执行过命令。",
        "- 写代码和测试后，按任务风险运行必要检查并如实报告结果；检查失败就继续修复，"
        "但宿主不会替你执行或裁定一套额外的机器验收。",
        "- 如果用户要求按钮、链接或图片不能失效，不要用 href=\"#\"、空锚点或不存在的 #id 假装可点击；"
        "页面内跳转必须指向真实存在的元素 id，按钮必须有真实交互或真实本地目标。",
        "- 生成普通报告或中等长度文本时，优先用 write_file 的 content 字段完整写入。"
        "生成长 CSS/JS/HTML、大段代码或长报告时，第一块用 mode=\"overwrite\"，后续使用 mode=\"append\" 分段写入。"
        f"{filesystem_text_mutation_rule()}。PDF、XLSX、图片等二进制产物可用授权命令/脚本生成，"
        "再用 write_file.data_base64 写入。",
        "- write_file 会自动创建父目录；不要因为目标目录尚未创建就标记 BLOCKED。"
        "普通输出路径按 workspace_root/path_access_mode 解析，只有危险目录或显式禁止路径才会被拒绝。",
        *read_ref_context_lines(context),
        *required_product_contract_lines(context),
        "- 需要当前工具目录之外的新工具、skill、MCP、网络或运行权限时，写 capability_request；"
        "说清楚 problem、needed_capability、capability_type、requested_tools/requested_skills/requested_mcp_tools 和 expected_output 即可，"
        "不要把父级授权细节、grant、path_scope、output_budget 当成普通任务步骤。",
        "- 如果 Tool Catalog 里有 capability_request 工具，缺能力时必须先调用该工具记录正式申请；"
        "不要在产物目录写 capability_request.json，也不要改 execution_context.json 伪造 pending_requests。",
        "- capability_request 工具返回 OPEN 后，停止猜测并在最终回复里如实说明等待哪项能力，"
        "不要继续假装能力已经授权或命令已经执行。",
    ]
    lines.extend(_targeted_request_lines(context))
    lines.extend(_controlled_exec_contract_lines(context))
    lines.extend(_current_role_template_lines(context))
    lines.extend(_root_execution_contract_lines(context))
    if _is_coordinator_context(context):
        lines.extend(_coordinator_execution_contract_lines())
    return lines


# LLM: Prompt wording mirrors the typed execution_cwd consumed by tools. Task
# state refs remain visible for recovery but must never be described as cwd.
# 函数用途: 用简短条款区分真实项目目录与宿主管理的任务状态目录。
def _workspace_execution_contract_lines(
    context: SubAgentExecutionContext,
) -> list[str]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    refs = bundle.get("workspace_refs")
    if not isinstance(refs, dict):
        return []
    boundary = context.write_boundary if isinstance(context.write_boundary, dict) else {}
    execution_cwd = str(
        boundary.get("execution_cwd") or refs.get("owner_workspace_dir") or ""
    ).strip()
    task_root = str(refs.get("task_root") or context.task_dir or "").strip()
    if not execution_cwd:
        return []
    return [
        f"- Current working directory (cwd): {execution_cwd}",
        f"- Internal task state root（仅宿主管理的 work/output）: {task_root or 'none'}",
        "- 普通相对目录和文件始终从 cwd 解析；不要把 cwd 拼到内部 task state root 下面。"
        "只有父级明确给出 `work/...` 或 `output/...` 命名空间时，才使用内部任务区。",
    ]


def _takeover_execution_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    takeover = bundle.get("takeover")
    if not isinstance(takeover, dict) or not takeover:
        return []
    return [
        "- 这是 takeover run：context_bundle.takeover 是来源 run 的结构化权威 handoff；先按其中 current_step、latest_summary、blockers 接续。",
        "- takeover.refs 是指针而非启动前置条件；不要用通用 read_file/list_files 读取受管状态面。仅当嵌入摘要不足时读取权限范围内的具体产物正文。",
    ]


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
        "最终回复也要明确列出这些业务产物路径。",
        "- agent_run_final_report_ref 是系统内部交接报告，不是用户要求的业务产物；"
        "除非它同时出现在 required_file_refs 里，否则不能把它当作交付文件。",
        f"- required_file_refs: {', '.join(refs[:8])}",
    ]


def _targeted_request_lines(context: SubAgentExecutionContext) -> list[str]:
    requests = _targeted_collaboration_requests(context)
    if not requests:
        return []
    lines = [
        "- 点名给你的协作请求：在最终回复的证据/结论里原样引用 case_ref/request_ref 回应；"
        "不要把请求内容重述成新问题或另开重复 case。"
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


# LLM: coordinator 的提示只描述统一 create 和事件驱动父子关系；不要恢复旧
# schedule/dispatch/inspect 轮询词汇或让协调者代替 worker 自写全部产物。
# 函数用途: 生成协调角色每轮都会看到的执行边界说明。
def coordinator_execution_policy_lines() -> list[str]:
    return [
        "- coordinator/lead 节点拥有完整基础读写能力，但只用于自己的计划、证据、已有产物整合、测试和协调报告；"
        "已经委派给 child 的实际实现不由 coordinator 亲自补写。",
        "- coordinator/lead 的第一目标是让团队动起来：先读取最小必要材料来理解目标、目录、评分和质量边界，"
        "不要在派工前把所有正文、数据表、长报告都自己读完。能拆给 child 的研究、实现和测试，先创建 child；创建后它会自动运行。",
        "- 一旦让 child 替你完成工作，你的角色就变为协调者：child 运行期间不要同时做它的实际工作；"
        "child 完成后按 artifact_refs/evidence_refs 读取必要结果，只整合已有产物、运行用户允许的测试并汇报。"
        "如果仍缺功能且当前还能推进，点名 guidance 或创建职责精确的 replacement child，继续到目标完整解决；"
        "诚实列出未完成项不能代替继续工作，也不要由 coordinator 静默接管实现。",
        "- 是否派工由目标规模、可并行性和用户要求决定；简单任务可以一开始就直接做。"
        "但创建失败、容量不足、child 失败或结束都不会自动撤销已经形成的协调角色边界。",
        "- 创建 child 时，把目标路径、文件名和质量要求原样传给下一层；不需要额外推进。",
        "- 如果缺口只属于未来 child/leaf 的执行能力，例如 leaf 才需要 controlled_exec、shell、网络或某个 skill，"
        "coordinator/lead 不要替后代提前提交 capability_request 后停止；先创建对应 child，"
        "由真正需要该能力的 runner 正式申请，父级再 route grant 并继续推进。",
        "- 给 child 写 goal 时，不要要求它在产物目录写 output.json；"
        "如需结构化汇报，只能要求它写自己的 execution_context.output_json。",
        "- coordinator 可以继续创建 coordinator 作为下一层领导节点；"
        "需要多层协作时不要误以为只能创建 worker；父级要求多层链路时，深度未到目标层前先创建下一层 coordinator。",
        "- 只创建父级任务确实需要的 child；父级明确点名 tester、reviewer 等角色时才创建对应 run，"
        "不要为了凑角色或验收格式自动扩容。",
        '- 下一层仍使用统一的 create_subagents，例如 {"tool":"create_subagents","goal":"整批目标","items":[{"goal":"子任务"}]}；'
        "长目标可分多次创建，每个 child 的 goal 必须自包含。",
        "- 不要让 worker/writer 代写 coordinator 自己的协调证据；需要共享时引用 artifact_refs/evidence_refs。",
        "- 创建 child/leaf 时必须原样传递父级指定的文件名、目录和质量要求，不要把 solution.py 改成别的模块名。",
        "- 同一次 create_subagents 可以混建 coordinator、worker 或 tester；创建回执只说明是否已记录并交给运行时，进展、阻塞或完成由宿主事件送回直接父级。",
        "- 下级失败或阻塞时先读取真实 refs 和原因；不要自动创建整批 repair/QA 子代理。",
        "- 少数下属需要不同纠偏、路径修正或需求变更时，优先用 send_guidance 点名具体 run_id；"
        "平级讨论要走允许的定向通道，不能广播到兄弟分支的子孙。",
    ]


def _current_role_template_lines(context: SubAgentExecutionContext) -> list[str]:
    del context
    return []


def _is_coordinator_context(context: SubAgentExecutionContext) -> bool:
    tools = set(context.allowed_tools or [])
    snapshot = context.role_template or role_template_snapshot_for_role(str(context.role or ""))
    return bool(snapshot.get("can_spawn_children")) and "create_subagents" in tools
