# LLM: runner 提示采集原上下文后交给纯格式化器；容量投影必须复用冻结材料，不借渲染读取或准备子代理运行。
# 模块用途: 用原 canonical 上下文生成子代理系统说明和任务提示，保持角色、工具与共享分工边界一致。
from __future__ import annotations

"""LLM: 从 canonical 执行上下文构造 runner 提示；当前角色快照、工具授权与共享分工说明须一致。

模块用途: runner 真正调用模型前，把执行上下文压成明确任务；模型按普通 assistant 回合自然结束，
宿主不再要求或修复额外的机器结果块。
"""

import json
from dataclasses import dataclass

from ...settings.config import DEFAULT_DELEGATED_EXECUTION_PERSISTENCE
from ...subagents import SubAgentExecutionContext
from ...subagents.context_bundle import context_gate_prompt_lines
from ...subagents.role_templates import (
    RECORD_LESSON_TOOL,
    role_template_index_text,
    role_template_snapshot_for_role,
    template_for_role_identity,
)
from ...tooling.content_transport_policy import filesystem_text_mutation_rule
from ..orchestration.coordinator_policy import coordinator_execution_policy_lines
from .prompt_context_summary import runner_context_summary_payload

# 子代理默认 thought / plan 模板的权威位置；create_policy 派工链路只引用不复制。
SUBAGENT_DEFAULT_THOUGHT = "根据父代理派工执行，并保留真实结果与引用。"
SUBAGENT_DEFAULT_PLAN: tuple[str, ...] = ("理解目标", "执行任务", "核对真实结果", "交回结果和引用")


# LLM: 只保存宿主已采集的字符串；读取 refs 存在性和角色模板只发生在 prepare，不在纯 render 重读。
# 类用途: 冻结子代理任务提示的完整动态材料，供真实启动和容量投影共用同一格式。
@dataclass(frozen=True)
class SubagentRunnerPromptInput:
    extra: str
    execution_contract: str
    context_gate: str
    context_json: str
    audit_source_prompt: str | None = None


# LLM: 主子共用的执行纪律不能要求协调者亲手重写已委派成果；身份、文件交接与消息消费仍读结构化事实。
# 函数用途: 生成子代理模型的系统说明，要求真实交付和回复插话，不把本层负责误写成每个文件都要亲手实现。
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
        "持续执行纪律：在当前回合能继续推进时，必须继续使用现有工具，直到任务真正完成或出现真实阻塞；"
        "不要以“现在开始/接下来会/马上写入/随后验证”这类未来动作结束回复。"
        "如果最终回复里还存在你承诺要做的动作，就先执行该动作，再向父级交回结果。\n"
        "完成纪律：明确要求的产物必须实际存在；按本层分工由你或受委派下级通过真实工具产出。"
        "下级交回文件时先核对引用和所需内容，不要为了证明完成而亲手重写一份。"
        "自己负责的未完成部分继续做；依赖下级的部分等待真实交接后再接入。"
        "确实做不到就如实说明缺口，不用“进行中/下一步再写”冒充完成。\n"
        "持续任务中的 coverage、游标、积压、签收、工具结果和 source_ref 是运行事实；"
        "不能把候选排序、结构频次或工具提示当成业务结论，也不能把部分覆盖说成完整覆盖。"
        "怎样分析、是否委派、是否复核和怎样交付，由你结合本轮用户目标、可用工具及真实结果"
        "自主决定，不要编造固定流程。\n"
        "运行中如果收到新的真实用户消息，它与普通聊天输入同等优先：先在普通 assistant 回复中"
        "直接回答或确认用户，再继续工具和原任务（除非用户要求停止或改向）；不要只在 thinking"
        "里回应，也不要把用户消息当成无须回复的内部便签。\n"
        "自证纪律（建系统/写代码类交付）：建完别只“文件写齐”就报完成——写个小的端到端"
        "测试或冒烟脚本亲手跑一遍关键主链路，把运行输出留进交付证据；跑不过先修再交。\n"
        "数据纪律（分析/统计/排名类交付）：聚合前先识别明显异常记录（缺字段/重复/数量级"
        "离谱的极端值），剔除或单列后再算汇总排名，报告注明剔除口径与条数；"
        "对脏数据直接求和排序会把结论带偏。"
    )


# LLM: 原真实启动入口保留上下文读取顺序；唯一格式化器也供准备前容量使用，不能另造简化 runner prompt。
# 函数用途: 读取完整 runner 提示材料后渲染，输出与原子代理启动提示逐字一致。
def _build_subagent_runner_prompt(
    context: SubAgentExecutionContext,
    instruction: str = "",
) -> str:
    return render_subagent_runner_prompt(prepare_subagent_runner_prompt(context, instruction))


# LLM: 准备层可能读取 refs 和角色目录；只接受原执行上下文，不创建任务、不消费邮箱、不准备或探测后端。
# 函数用途: 冻结启动提示的上下文 JSON、角色纪律及输入合同，纯投影从此值对象继续。
def prepare_subagent_runner_prompt(
    context: SubAgentExecutionContext, instruction: str = "",
) -> SubagentRunnerPromptInput:
    if _audit_source_runtime_profile(context):
        return SubagentRunnerPromptInput(
            "", "", "", "", audit_source_prompt=_build_audit_source_runner_prompt(context, instruction),
        )
    payload = json.dumps(runner_context_summary_payload(context), ensure_ascii=False, indent=2)
    extra = instruction.strip() or "按执行上下文完成任务；如果能力不足，如实说明缺少什么。"
    execution_contract = "\n".join(_runner_execution_contract_lines(context))
    context_gate = "\n".join(context_gate_prompt_lines(context.context_bundle))
    return SubagentRunnerPromptInput(extra, execution_contract, context_gate, payload)


# LLM: 纯格式化不读取上下文、目录或文件；普通路径与原 audit 专用格式分别保持既有字节，不改变运行合同。
# 函数用途: 从冻结字符串恢复完整子代理任务提示，可重复用于真实请求和创建前容量检查。
def render_subagent_runner_prompt(prepared: SubagentRunnerPromptInput) -> str:
    if prepared.audit_source_prompt is not None:
        return prepared.audit_source_prompt
    return (
        "# SubAgent Runner Task\n\n"
        "你是一个被父代理授权的子代理，只能依据下面的执行上下文工作。\n"
        "不要使用上下文之外的 skill/tool，也不要编造已经完成的动作。\n\n"
        "## Extra Instruction\n\n"
        f"{prepared.extra}\n\n"
        "## Runner Contract\n\n"
        f"{prepared.execution_contract}\n\n"
        "## Context Bundle Gate\n\n"
        f"{prepared.context_gate}\n\n"
        "## Execution Context JSON\n\n"
        "下面是瘦身后的执行摘要；完整上下文请按 refs 读取，不要让模型一次吞完整大 JSON。\n\n"
        "```json\n"
        f"{prepared.context_json}\n"
        "```\n\n"
        "## Required Output\n\n"
        "像普通协作者一样给出简洁最终回复：说明完成了什么、重要文件或结果在哪里、"
        "实际运行了哪些检查，以及是否存在真实阻塞。回复中的数字、统计和核对结论必须来自本轮实际执行的"
        "工具输出，并说明由哪一步得到；没有用工具计算或核对过的内容明确标为未核对，不要心算后报告通过。"
        "不要输出 SUBAGENT_RESULT、"
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
        + "## Runtime Context\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```\n\n"
        "## Bounded Turn Result\n\n"
        "当本工作片暂时没有更多可安全处理的记录，像普通协作者一样简洁说明"
        "本轮处理了什么和仍在等待什么，然后结束本轮；来源工作者的持续状态由宿主管理。\n"
    )


# LLM: 条款只作软引导，按结构化上下文（工作区、授权工具、角色）条件渲染；不得重新要求状态 JSON 或结果块。
# 函数用途: 生成子代理任务提示里 Runner Contract 一节的全部条款。
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
        *_lesson_ledger_lines(context),
    ]
    lines.extend(_targeted_request_lines(context))
    lines.extend(_controlled_exec_contract_lines(context))
    lines.extend(_current_role_template_lines(context))
    lines.extend(_root_execution_contract_lines(context))
    if _is_coordinator_context(context):
        lines.extend(_coordinator_execution_contract_lines())
    return lines


# LLM: 只有本轮 allowed_tools 真含 record_lesson 时才渲染这一条软引导；它是可选提示，不是完成门，
#   也不要求任何结构化结果块。宿主只从 lesson 账本读经验，从不解析这里或最终回复的文字。
# 函数用途: 提示子代理可以（非必须）用 record_lesson 记下以后同类任务可复用的做法。
def _lesson_ledger_lines(context: SubAgentExecutionContext) -> list[str]:
    if RECORD_LESSON_TOOL not in (context.allowed_tools or []):
        return []
    return [
        "- 本次工作若形成以后同类任务可复用的具体做法，可以调用 record_lesson 记一条（可选；只记可复用做法，"
        "不记本任务的事实结论，结论照常写在最终回复里）。"
    ]


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
        "`work/...`、`output/...`、`tasks/...` 也是 cwd 内的普通目录，不表示内部任务区。",
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


# LLM: Required product refs are the only model-authored delivery paths. Host
# closeout/report files are created after the final response and stay unnamed in
# the child prompt so they cannot be mistaken for a second deliverable.
# 函数用途: 告诉子代理真正要写哪些用户业务文件，并明确内部交接由宿主自动收口。
def required_product_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    contract = bundle.get("output_contract") if isinstance(bundle.get("output_contract"), dict) else {}
    refs = [str(item) for item in contract.get("required_file_refs") or [] if str(item).strip()]
    if not refs:
        return []
    return [
        "- 用户要求的业务产物必须写到 output_contract.required_file_refs 中的精确路径；"
        "最终回复也要明确列出这些业务产物路径。",
        "- 系统内部交接记录由宿主在最终回复后自动生成；不要为交差另写内部状态或交接文件。",
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
        "如果 controlled_exec_grants.delete_policy.mode=task_trash，已有 controlled_exec grant 时直接调用 controlled_exec apply=true 执行删除命令，工具会改走 task_trash 并在 payload.trash 里返回 moved/manifest_ref，"
        "不要为了裸 rm 再提交 capability_request；只有 trash.moved=true 且有 trash.manifest_ref 才能把删除验收写成 PASS。",
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


# LLM: 协调层只需可选角色索引与统一纪律；其它角色正文不属于当前身份，不能混入当前执行约束。
# 函数用途: 保留选下级角色的简短目录，避免把执行者、测试者等整份行为同时灌给协调者。
def _coordinator_execution_contract_lines() -> list[str]:
    lines = ["可用角色模板索引："]
    lines.extend(f"  {line}" for line in role_template_index_text().splitlines())
    lines.extend(coordinator_execution_policy_lines())
    return lines


# LLM: 每个角色（含 coordinator）只读取自己的创建时行为快照；重启不换成当前目录，不增加权限或状态。
# 函数用途: 给子代理补入自己的角色说明，避免协调者吃下其它角色全文或丢失自定义角色行为。
def _current_role_template_lines(context: SubAgentExecutionContext) -> list[str]:
    snapshot = context.role_template if isinstance(context.role_template, dict) else {}
    prompt = str(snapshot.get("prompt_zh") or "").strip()
    role_name = str(snapshot.get("name_zh") or context.role or "当前角色").strip()
    if not prompt:
        template = template_for_role_identity(str(context.role or ""))
        if template is not None:
            prompt = template.prompt_zh
            role_name = template.name_zh or role_name
    if not prompt:
        return []
    return [f"- 当前角色行为（{role_name}）：{prompt}"]


def _is_coordinator_context(context: SubAgentExecutionContext) -> bool:
    tools = set(context.allowed_tools or [])
    snapshot = context.role_template or role_template_snapshot_for_role(str(context.role or ""))
    return bool(snapshot.get("can_spawn_children")) and "create_subagents" in tools
