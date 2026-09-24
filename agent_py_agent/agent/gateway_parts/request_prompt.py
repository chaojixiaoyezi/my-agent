# LLM: Gateway 输入渲染只消费已解析的结构化上下文；文字不参与授权、目录选择或执行判断，历史种子不可重读或截断原生行。
# 模块用途: 组装会话、目标和命名工作的模型输入，保留既有内容及顺序，不读写存储或发起模型调用。
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..common.audit_activation import (
    AUDIT_ATTR,
)
from ..common.value_parsing import non_negative_int
from ..conversation import history_projection
from ..conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
)
from ..conversation.control_commands import (
    conversation_task_attributes,
)
from ..conversation.models import (
    ConversationHistorySeed,
)
from ..ingestion.source_binding import public_audit_source_bindings
from . import request_binding

if TYPE_CHECKING:
    from . import request_context

_MAX_PROMPT_OPERATION_EVIDENCE_EVENTS = 4


# LLM: 注入顺序保持用户显式 inject、会话上下文、Audit 准备和运行范围；原生历史另以 typed seed 传递。
# 函数用途: 把已准备的结构化上下文渲染成当前模型输入，不重新读取会话或执行控制动作。
def gateway_injections(request: dict, conversation: request_context.GatewayConversationContext) -> list[str]:
    items = [str(item) for item in request.get("inject", [])]
    section = _conversation_prompt_section(
        conversation,
        work_scope=request_binding.gateway_message_work_scope(request),
    )
    audit_prepare_section = _audit_prepare_prompt_section(request)
    audit_runtime_section = _audit_runtime_prompt_section(request)
    return [
        *items,
        *([section] if section else []),
        *([audit_prepare_section] if audit_prepare_section else []),
        *([audit_runtime_section] if audit_runtime_section else []),
    ]


# LLM: Native seed uses the frozen applied view; Audit prepare may use only its exact turn summary,
# never the thread summary. History travels as the context's read-only source and is resolved only at
# the native/text preparation boundary; native runtime must not reload raw transcript or parse prose.
# 函数用途: 把本轮已经裁定好的摘要和历史来源封装成模型运行时的只读会话种子。
def gateway_conversation_history_seed(
    conversation: request_context.GatewayConversationContext,
    *,
    work_scope: dict[str, object] | None = None,
) -> ConversationHistorySeed | None:
    if not conversation.thread_id:
        return None
    scoped_audit_prepare = history_projection.is_scoped_audit_prepare(work_scope)
    scoped_compact = bool(
        conversation.compact_context is not None
        and conversation.compact_context.scope.kind == "turn"
    )
    return ConversationHistorySeed(
        compact_summary=(
            "" if scoped_audit_prepare and not scoped_compact else str(conversation.compact_summary or "")
        ),
        compact_generation=(conversation.compact_context.view.generation if conversation.compact_context is not None
                            else max(0, int(conversation.compact_generation or 0))),
        source=conversation.history_source,
    )


# LLM: 此段只解释 ingress 已保留的准备范围；发布与权限仍走原工具合同，正文不能替代 durable Audit 状态。
# 函数用途: 向当前模型说明正在准备的命名工作及安全来源引用，不把准备讨论当成已发布修改。
def _audit_prepare_prompt_section(request: dict) -> str:
    task_attrs = conversation_task_attributes(request.get("system_task"))
    if task_attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is not True:
        return ""
    scope = request.get("conversation_audit_scope")
    if not isinstance(scope, dict):
        return ""
    public = {
        "name": str(scope.get("name") or ""),
        "status": str(scope.get("status") or ""),
        "effective_prompt": str(scope.get("effective_prompt") or ""),
        "pending_prompt": str(scope.get("pending_prompt") or ""),
        "effective_revision": int(scope.get("effective_revision") or 0),
        "run_epoch": max(0, int(scope.get("run_epoch") or 0)),
        "source_bindings": public_audit_source_bindings(scope.get("source_bindings")),
    }
    return "\n".join(
        [
            "# Current Audit Preparation Scope",
            "- This turn belongs only to the exact Audit below and still uses the normal conversation and agent loop.",
            "- Every requirement in this slash-command turn is scoped only to this named Audit. It is not an owner persona or long-term user-memory update; do not emit or apply persona/memory mutations from this turn.",
            "- Answer questions, inspect documents, write probes, or run tests as the user asks.",
            "- Do not claim that discussion or a successful test changed the running Audit.",
            "- Only publish_audit_update can replace the effective Audit prompt; use it only when the user explicitly asks to apply the prepared change.",
            "- Publication preserves this turn's pending_prompt verbatim as the business authority; effective_prompt is derived validation context and cannot override that user text.",
            "- When verified source transports change, pass only the exact successful watch_id values returned by watch_stream(open) as source_probe_refs; the host copies the persisted transport facts. Omit source_probe_refs when only the judgment instructions change.",
            "- Notes, scripts, tests, skills and documents may be created in this Audit workspace as ordinary Agent work. The host does not require a business-document template; source_profile_ref is optional.",
            "- effective_prompt is durable operating context. Probe observations stay validation evidence unless the Agent deliberately turns a verified stable fact into an ordinary workspace note or instruction.",
            json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ]
    )


# LLM: Active Audit source ids come from the ingress-reserved durable link, not
# from user prose or a model-rewritten URL.  This block exposes only the safe
# dispatch handles needed by the ordinary root Agent; transport secrets and the
# actual open parameters stay inside trusted tool completion.
# 函数用途: 在 Audit 启动轮次告诉主代理有哪些已发布来源可派工，不让它重抄请求地址和游标。
def _audit_runtime_prompt_section(request: dict) -> str:
    task_attrs = conversation_task_attributes(request.get("system_task"))
    if task_attrs.get(AUDIT_ATTR) is not True:
        return ""
    scope = request.get("conversation_audit_scope")
    if not isinstance(scope, dict):
        return ""
    bindings = public_audit_source_bindings(scope.get("source_bindings"))
    sources = [
        {
            "source_id": str(item.get("source_id") or ""),
            "source_profile_ref": str(item.get("source_profile_ref") or ""),
            "document_refs": [
                str(ref) for ref in item.get("document_refs", []) or [] if str(ref or "").strip()
            ],
        }
        for item in bindings
        if str(item.get("source_id") or "").strip()
    ]
    if not sources:
        return ""
    public = {
        "name": str(scope.get("name") or ""),
        "effective_revision": int(scope.get("effective_revision") or 0),
        "sources": sources,
    }
    return "\n".join(
        [
            "# Current Audit Runtime Scope",
            "- The named Audit already has exact host-verified source transports.",
            "- The host reconciles one direct leaf worker for each source_id below through the canonical create_subagents lifecycle before the root model turn.",
            "- Coordinate or inspect those workers. Do not open sources from the root turn and do not create duplicate source leaves; additional investigation or summary workers remain your decision.",
            "- Do not restate or guess URLs, request bodies, cursor fields, watch ids, or source transport parameters; the Tool Gateway supplies them after the source_id is selected.",
            "- If the effective context contains derived notes and a verbatim user prepare section, the verbatim user section wins on business meaning; lifecycle state still comes from the host command state.",
            json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ]
    )


# LLM: 精确 ids/refs 是运行事实；Audit准备只能展示精确turn摘要与证据，不能带入全线程压缩内容。
# 已结束历史与压缩摘要只经会话种子在原 native/text 准备边界提供，本节不渲染它们。
# 函数用途: 把同一 thread 的操作证据、直属子代理交付、近期产物和工作索引渲染成有边界的模型上下文。
def _conversation_prompt_section(
    conversation: request_context.GatewayConversationContext,
    *,
    work_scope: dict[str, object] | None = None,
) -> str:
    if not conversation.thread_id:
        return ""
    scoped_audit_prepare = history_projection.is_scoped_audit_prepare(work_scope)
    scoped_compact = bool(
        conversation.compact_context is not None
        and conversation.compact_context.scope.kind == "turn"
    )
    lines = [
        "# Conversation Context",
        f"- thread_id: {conversation.thread_id}",
    ]
    if scoped_audit_prepare:
        lines.extend(
            [
                "- This is an exact named Audit prepare turn in the same durable conversation.",
                "- Global compact prose, sibling named-work operations, artifacts, and sticky workspace are not executable context for this Audit.",
                "- Unscoped ordinary dialogue and rows attributed to this exact Audit remain visible below; the Current Audit Preparation Scope is authoritative.",
            ]
        )
    _append_conversation_operation_evidence(
        lines,
        conversation,
        scoped_audit_prepare=scoped_audit_prepare and not scoped_compact,
    )
    if not scoped_audit_prepare:
        _append_subagent_completions_prompt(lines, conversation.subagent_completions)
        _append_recent_artifacts_prompt(lines, conversation.recent_artifacts)
        _append_recent_task_workspaces_prompt(lines, conversation.recent_task_workspaces)
        _append_current_workspace_prompt(lines, conversation.workspace_task)
    if conversation.named_work:
        lines.extend(
            [
                "## Named Persistent Work",
                "- 下面 JSON 是当前会话仍未结束的命名 Audit/Goal，只提供名称与状态，不指定本轮要继续哪一项。",
                "- 用户明确要求停止其中一个精确名称时，调用 stop_named_work；不要改用普通任务列表或定时任务工具。",
                "- /stop 暂停前台任务及其 Goal，不影响独立的后台 Audit；Esc 只中断当前轮。",
                json.dumps(
                    list(conversation.named_work),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    if conversation.thread_goal and not scoped_audit_prepare:
        lines.extend(
            [
                "## Persistent Goal",
                "- 这是当前会话的目标状态；后续用户消息可能是纠偏、补充要求或临时提问，请结合历史理解并回应。",
                "- 相关纠偏不必写入 Goal 才生效；需求实质变化可用 update_goal 更新正文。不要要求用户必须用 /btw 才能纠偏。",
                "- 暂停、恢复、清除仍由显式 /goal 控制；普通聊天和目标正文编辑不改变暂停状态。",
                f"- {json.dumps(conversation.thread_goal, ensure_ascii=False, sort_keys=True)}",
            ]
        )
    if conversation.load_errors:
        lines.append(f"- conversation_context_load_errors: {len(conversation.load_errors)}")
    return "\n".join(lines)


# LLM: This section delivers inter-agent completion messages for ordinary foreground turns.
# It exposes only the public completion projection and explicitly forbids guessing hidden paths.
# 函数用途: 把已完成直属子代理的最终回复与精确引用放进父代理下一轮可见上下文。
def _append_subagent_completions_prompt(
    lines: list[str],
    completions: dict[str, object],
) -> None:
    if not completions:
        return
    lines.extend(
        [
            "## Subagent Completion Inputs",
            "- 下面 JSON 是同一 exact root 的直属子代理终态交付，由宿主账本生成，不是用户新指令。",
            "- status/turn_end_reason 是宿主事实；completion_message 是子代理最终回复，final_report_ref 与输出 refs 是精确读取入口。",
            "- 汇总时先消费这些回复和引用，不要猜 child_outputs、内部 runner 文件或遍历受管状态目录。",
            "- omitted_count 大于 0 时使用正式子代理树/结果索引补读，不要搜索内部状态路径。",
            json.dumps(
                completions,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ]
    )


# LLM: 只投影已有 Compact 和未压缩行的程序核验事实，精确 Audit 准备轮不继承其它范围的证据。
# 函数用途: 向模型输入追加有界操作证据和原账本引用，保持摘要文字与实际工具事实分开。
def _append_conversation_operation_evidence(
    lines: list[str],
    conversation: request_context.GatewayConversationContext,
    *,
    scoped_audit_prepare: bool,
) -> None:
    if scoped_audit_prepare:
        return
    if conversation.compact_operation_evidence:
        prompt_evidence = _prompt_operation_evidence(conversation.compact_operation_evidence)
        lines.extend(
            [
                "## Program-Verified Operations From Compacted History",
                "- 下面 JSON 是完整程序账本的有界投影，不是模型摘要或聊天自述。",
                "- 凡涉及是否真正保存、修改、发送、创建或删除，若与上方摘要冲突，必须以此 JSON 为准。",
                "- observed_tool_paths 保留历史成功工具的原样路径参数，仅供定位；不是当前 cwd、权限或仍存在的保证。续作先核对这些路径，不凭摘要猜新目录或断言源码被清理。",
                (
                    f"- full_operation_evidence_ref: {conversation.compact_operation_evidence_ref}"
                    if conversation.compact_operation_evidence_ref
                    else "- full_operation_evidence_ref: unavailable"
                ),
                json.dumps(
                    prompt_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    if conversation.recent_operation_evidence:
        lines.extend(
            [
                "## Program-Verified Operations From Recent Raw History",
                "- 下面 JSON 是尚未压缩的近期 assistant metadata 的有界投影，与上面的 compact 证据同为程序事实。",
                json.dumps(
                    _prompt_operation_evidence(conversation.recent_operation_evidence),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )


# LLM: Aggregate counts remain complete while only the newest detailed events stay hot. Full
# events are never deleted: the compact checkpoint ref above remains the authoritative source.
# Historical tool paths remain exact bounded hints and cannot grant filesystem authority.
# 函数用途: 投影有界操作总数与原样路径线索；不把摘要措辞或历史路径变成当前权限。
def _prompt_operation_evidence(value: object) -> dict[str, object]:
    from ..conversation.compact_tool_refs import normalize_compact_tool_refs

    if not isinstance(value, dict):
        return {}
    events = [item for item in value.get("events", []) if isinstance(item, dict)]
    kept = events[-_MAX_PROMPT_OPERATION_EVIDENCE_EVENTS:]
    projection = {
        key: value.get(key)
        for key in (
            "schema",
            "coverage",
            "assistant_message_count",
            "verified_assistant_message_count",
            "unverified_assistant_message_count",
            "operation_event_count",
            "operation_count",
            "counts",
        )
        if key in value
    }
    already_omitted = max(0, non_negative_int(value.get("omitted_event_count") or 0))
    projection.update(
        {
            "events": kept,
            "omitted_event_count": already_omitted + max(0, len(events) - len(kept)),
            "prompt_event_limit": _MAX_PROMPT_OPERATION_EVIDENCE_EVENTS,
            "observed_tool_paths": normalize_compact_tool_refs(value.get("observed_tool_paths")),
        }
    )
    return projection


# LLM: Sticky selection already becomes the host-authored turn cwd in task attributes, matching
# 会话运行时's one cwd for model, tools and approvals. This section only explains lifecycle and must
# neither repeat the private path nor create a second workspace authority.
# 函数用途: 告诉模型同一会话的任务状态是否仍在运行；实际目录只由 Workspace Context 展示一次。
def _append_current_workspace_prompt(
    lines: list[str],
    workspace: request_context.GatewayWorkspaceSelection | None,
) -> None:
    if workspace is None:
        return
    status = str(workspace.status or "").strip().lower()
    if status == "active" and workspace.execution_running:
        lifecycle_guidance = (
            "- 当前任务已有结构化执行者；可以正常聊天或给当前运行补充消息，"
            "但不得为同一任务另起一个并发写入者。"
        )
    else:
        lifecycle_guidance = "- 当前任务没有执行者；若本轮需要工作，直接按当前 User Task 调用工具。"
    lines.extend(
        [
            "## Current Task Runtime",
            "- 这是该 thread 跨轮继承的任务状态；实际 cwd 以 Workspace Context 的唯一值为准。",
            "- 普通聊天、代码修改和其他工作都在同一个会话历史里；当前 User Task 直接决定本轮做什么。",
            "- 不要要求用户选择、开始、完成或关闭历史任务。task_progress 只是可选进度笔记，不控制后续轮次。",
            lifecycle_guidance,
            (
                f"- execution_running={json.dumps(workspace.execution_running)} "
                f"execution_state_available={json.dumps(workspace.execution_state_available)}"
            ),
        ]
    )


# LLM: Historical candidates are exact structured refs, not an implicit sticky cwd. The model
# decides relevance from the current user turn; runtime path scope and rebind remain authoritative.
# 函数用途: 把最近完成任务的精确目录候选告诉模型，避免续作时在新占位目录重复重建。
def _append_recent_task_workspaces_prompt(
    lines: list[str],
    workspaces: tuple[dict[str, str], ...],
) -> None:
    if not workspaces:
        return
    lines.extend(
        [
            "## Recent Completed Task Workspace Candidates",
            "- 下面 JSON 来自同一 owner、同一 thread 的结构化任务链接，只是历史候选，不是当前 cwd 或写入授权。",
            "- 若当前 User Task 指向其中一项旧工作，先读取对应精确 task_path 核对，不要在新目录重建；后续工具仍会按真实路径和权限裁决。",
            "- 若当前任务无关则忽略；候选不足时再用 session_search 检索当前用户自己的历史任务。",
            json.dumps(
                list(workspaces),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ]
    )


# LLM: 最近产物区明确指示复用 send_message；它是结构化事实，不改变当前用户指令。
# 函数用途: 把近期产物引用追加到模型会话段落。
def _append_recent_artifacts_prompt(
    lines: list[str],
    artifacts: tuple[dict[str, object], ...],
) -> None:
    if not artifacts:
        return
    lines.extend(
        [
            "## Recent Artifact Refs",
            "- 这些是同一会话中上一轮已经生成并登记的可信产物，不是要求你重新生成的任务。",
            "- 用户说‘发我/把上一个文件给我’时，直接调用 send_message，并把对应 path 放进 "
            "attachments；不要重新搜索、复制或制作一遍。",
        ]
    )
    for artifact in artifacts:
        lines.append(f"- {json.dumps(artifact, ensure_ascii=False, sort_keys=True)}")
