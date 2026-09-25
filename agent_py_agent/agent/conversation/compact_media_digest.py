# LLM: 随图摘要（B 路径）的唯一发送实现：不再把整段压缩范围连图一起发，而是把范围里含媒体的回合按摘要预算打包成若干
#   "看图小请求"（每请求：该请求文字估算 + 图块数 × input_media_token_reserve ≤ 摘要预算；请求数 ≤
#   compact_vision_digest_max_requests），每次只让视觉模型写图中要点；要点文字交给随后的普通文字摘要请求，图块本身不再进
#   大范围请求，因此阈值自动压缩、手动 /compact 走同一条路。任一小请求 typed 失败（COMPACT_VISION_SUMMARY_FAILED）或空回复
#   即停止，剩余图块按归档引用；一次都没成功则整体回落 A 并保留该失败码。只读结构化媒体事实、回合身份与 provider 缓存面，
#   不读正文做判定；不写盘、不改 canonical 行。估算与发送都经 compact_request_budget 模块属性调用，便于同一处替身覆盖。
#   改动须同步 compact.py::_media_digest_for_summary / _effective_media_decision 与 test_compact_media_digest.py。
# 模块用途: 含图历史压缩的"先看图、后总结"两步法，让随图摘要不再受整段范围大小限制。
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from . import compact_request_budget as budget_module
from .auxiliary_model_call import AuxiliaryModelCallRequest
from .compact_guard import (
    CompactInterruptCheck,
    ConversationCompactError,
    raise_if_compact_interrupted,
)
from .compact_media_policy import (
    COMPACT_VISION_SUMMARY_FAILED,
    MEDIA_POLICY_VISION_SUMMARY,
    MEDIA_REASON_DIGEST_PARTIAL,
    CompactMediaDecision,
    MediaArchiveFacts,
    archived_refs_fallback,
    media_archive_facts,
    media_token_reserve,
)
from .compact_provider_surface import (
    ConversationCompactProviderSurface,
    conversation_compact_provider_prompt,
    conversation_compact_provider_source,
)
from .native_history import _row_turn_identity

# LLM: 看图小请求在调用账本里的用途标识；与 conversation_compact_summary 分开，便于按用途统计。
MEDIA_DIGEST_PURPOSE = "conversation_compact_media_digest"
# 内部原因：含图回合因请求次数上限没被总结（最终 checkpoint 统一记 vision_digest_partial）。
_SKIP_REASON_CAP = "vision_digest_cap"
# LLM: 看图小请求的软指令：只描述图中要点，不总结对话、不调工具，输出语言跟随用户；文案不参与任何机器判定。
_MEDIA_DIGEST_INSTRUCTION = "\n".join([
    "The provider messages immediately before this request are a few turns from one conversation that contain",
    "image attachments. This is a vision pass only. For each image, in order, write the content that matters for the",
    "conversation: values, labels, text visible in the image, structure, and any conclusion drawn from it in the",
    "assistant reply that follows. Number the notes in the order the images appear. Do not summarize the rest of the",
    "conversation, do not call tools, and return only the notes in the user's language.",
])


# LLM: 看图小请求复用摘要调用的缓存面与运行身份；interrupt_check 与文字摘要同一停止语义。不持有历史，不拥有提交权。
# 类用途: 一次压缩里发看图小请求所需的上下文。
@dataclass(frozen=True)
class MediaDigestContext:
    surface: ConversationCompactProviderSurface
    compact_generation: int = 0
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    thread_id: str = ""
    interrupt_check: CompactInterruptCheck | None = None


# 类用途: 一次看图小请求覆盖的行与其媒体事实（图块数、sha、字节）。
@dataclass(frozen=True)
class MediaDigestChunk:
    rows: tuple[Any, ...]
    facts: MediaArchiveFacts


# LLM: skipped_blocks 是没被任何请求覆盖的图块数，skipped_reason 只取首个结构化原因（summary_budget_exceeded / vision_digest_cap）。
# 类用途: 按预算与次数上限打包后的看图小请求计划。
@dataclass(frozen=True)
class MediaDigestPlan:
    chunks: tuple[MediaDigestChunk, ...]
    skipped_blocks: int = 0
    skipped_reason: str = ""


# LLM: text 是各请求要点按块标签拼接的文字；failure_code 非空表示在某次请求 typed 失败/空回复后停止，之前成功的仍计入。
# 类用途: 看图小请求的执行结果。
@dataclass(frozen=True)
class MediaDigestOutcome:
    text: str = ""
    summarized_blocks: int = 0
    requests: int = 0
    failure_code: str = ""


# LLM: 只按结构化回合身份（conversation_request_id 等）把连续行归为一个回合，没有身份的行各成一组；只保留含媒体块的回合，
#   顺序不变。只读 metadata 里的 canonical 信封，不读正文。
# 函数用途: 找出压缩范围里哪些回合带图，供看图小请求打包。
def media_turn_groups(rows: Any) -> tuple[tuple[Any, ...], ...]:
    groups: list[list[Any]] = []
    previous = ""
    for row in rows:
        identity = _row_turn_identity(row)
        if groups and identity and identity == previous:
            groups[-1].append(row)
        else:
            groups.append([row])
        previous = identity
    return tuple(tuple(group) for group in groups if media_archive_facts(group).blocks > 0)


# 函数用途: 构造一次看图小请求（同一缓存面前缀、工具与系统指令，指令换成看图要点），不含历史。
def _digest_request(agent: object, context: MediaDigestContext) -> AuxiliaryModelCallRequest:
    surface = context.surface
    return AuxiliaryModelCallRequest(
        agent=agent, prompt=conversation_compact_provider_prompt(surface, _MEDIA_DIGEST_INSTRUCTION), messages=None,
        tools=list(surface.tools) if surface.tools is not None else None, system_instruction=surface.system_instruction,
        request_id=context.request_id, run_id=context.run_id, task_id=context.task_id, purpose=MEDIA_DIGEST_PURPOSE,
        thread_id=context.thread_id,
    )


# 函数用途: 为一组含图回合建立可重放的 provider 消息来源（不带旧摘要），图块保留给运输层展开。
def _chunk_source(context: MediaDigestContext, rows: Any):
    return conversation_compact_provider_source(
        "", context.compact_generation, tuple(rows), volatile_sections=context.surface.volatile_sections,
    )


# LLM: 与 compact_request_budget._request_tokens 同口径的文字估算，再加该请求图块数 × 预留；只是准入估算，不是计费。
# 函数用途: 估算一次看图小请求（这些行）的完整输入大小。
def digest_request_tokens(agent: object, context: MediaDigestContext, rows: Any) -> int:
    rows = tuple(rows)
    text = budget_module.compact_request_tokens(_digest_request(agent, context), _chunk_source(context, rows))
    return int(text) + media_archive_facts(rows).blocks * media_token_reserve(agent)


# LLM: 供准入用：范围内没有含图回合返回 0；否则返回最大的单回合看图请求估算，准入据此判断"至少能发一次"。
# 函数用途: 算压缩范围里最大的一次看图小请求有多大。
def largest_digest_request_tokens(agent: object, context: MediaDigestContext, rows: Any) -> int:
    return max((digest_request_tokens(agent, context, group) for group in media_turn_groups(rows)), default=0)


# LLM: 贪心打包：能并进当前请求就并入；否则在次数上限内另起一个请求；单回合自己都装不下按预算原因跳过；超过上限按次数原因跳过。
#   估算走 digest_request_tokens，同一预算口径。
# 函数用途: 把含图回合分成若干能装进摘要预算的看图小请求。
def plan_media_digest(
    agent: object, context: MediaDigestContext, groups: tuple[tuple[Any, ...], ...], *, budget: int, max_requests: int,
) -> MediaDigestPlan:
    limit = max(1, int(max_requests))
    chunks: list[list[Any]] = []
    skipped_blocks, skipped_reason = 0, ""
    for group in groups:
        rows = list(group)
        if chunks and digest_request_tokens(agent, context, chunks[-1] + rows) <= budget:
            chunks[-1].extend(rows)
            continue
        if len(chunks) >= limit:
            skipped_blocks += media_archive_facts(rows).blocks
            skipped_reason = skipped_reason or _SKIP_REASON_CAP
        elif digest_request_tokens(agent, context, rows) <= budget:
            chunks.append(rows)
        else:
            skipped_blocks += media_archive_facts(rows).blocks
            skipped_reason = skipped_reason or "summary_budget_exceeded"
    return MediaDigestPlan(
        tuple(MediaDigestChunk(tuple(rows), media_archive_facts(rows)) for rows in chunks), skipped_blocks, skipped_reason,
    )


# 函数用途: 给一段要点加上它覆盖的附件 sha 前缀标签，让文字摘要能对上归档引用；标签由结构化事实生成，不解析模型输出。
def _chunk_label(index: int, chunk: MediaDigestChunk) -> str:
    refs = "、".join(f"sha256:{sha[:12]}" for sha in chunk.facts.refs)
    return f"[附件组 {index}｜{chunk.facts.blocks} 个图块｜{refs}]"


# LLM: 顺序发送每个小请求；typed COMPACT_VISION_SUMMARY_FAILED 或空回复/工具调用即停止并记 failure_code，之前成功的保留；
#   其它异常（取消、来源变化等）原样上抛。每次请求前后都检查停止信号。
# 函数用途: 逐个发看图小请求，把每段要点连附件标签拼成给文字摘要的材料。
def generate_media_digest(agent: object, context: MediaDigestContext, plan: MediaDigestPlan) -> MediaDigestOutcome:
    notes: list[str] = []
    summarized = requests = 0
    for index, chunk in enumerate(plan.chunks, start=1):
        raise_if_compact_interrupted(context.interrupt_check)
        requests += 1
        try:
            response = budget_module.generate_bounded_compact_response(
                _digest_request(agent, context), interrupt_check=context.interrupt_check,
                message_source=_chunk_source(context, chunk.rows), vision_summary=True,
                media_reserve_tokens=chunk.facts.blocks * media_token_reserve(agent),
            )
        except ConversationCompactError as exc:
            if exc.code != COMPACT_VISION_SUMMARY_FAILED:
                raise
            return MediaDigestOutcome("\n\n".join(notes), summarized, requests, exc.code)
        text = "" if list(getattr(response, "tool_use_blocks", None) or []) else str(getattr(response, "text", "") or "").strip()
        if not text:
            return MediaDigestOutcome("\n\n".join(notes), summarized, requests, COMPACT_VISION_SUMMARY_FAILED)
        notes.append(f"{_chunk_label(index, chunk)}\n{text}")
        summarized += chunk.facts.blocks
    return MediaDigestOutcome("\n\n".join(notes), summarized, requests, "")


# LLM: 一次都没成功 → 回落 A，reason 取失败码，否则取计划里的跳过原因；有成功 → 保持 vision_summary 并记 summarized_blocks，
#   只要有图块没被覆盖（预算/次数/中途失败）reason 记 vision_digest_partial。fact_source 始终保留原视觉事实来源。
# 函数用途: 把看图小请求的结果合成最终的媒体决定。
def digest_media_decision(
    decision: CompactMediaDecision, plan: MediaDigestPlan, outcome: MediaDigestOutcome,
) -> CompactMediaDecision:
    if outcome.summarized_blocks <= 0:
        return archived_refs_fallback(decision, outcome.failure_code or plan.skipped_reason or "summary_budget_exceeded")
    partial = plan.skipped_blocks > 0 or bool(outcome.failure_code)
    return replace(
        decision, policy=MEDIA_POLICY_VISION_SUMMARY, summarized_blocks=outcome.summarized_blocks,
        reason=MEDIA_REASON_DIGEST_PARTIAL if partial else "",
    )


# LLM: 压缩主链的唯一入口：非 B 决定或范围内没有含图回合直接返回原决定与空串（零请求）；否则计划、发送、合成最终决定。
#   摘要预算读 compact_request_budget.compact_summary_budget（模块属性）。
# 函数用途: 文字摘要之前先把范围里的图看一遍，返回最终媒体决定和要交给文字摘要的图中要点文字。
def summarize_media_turns(
    agent: object, context: MediaDigestContext, rows: Any, decision: CompactMediaDecision, *, max_requests: int,
) -> tuple[CompactMediaDecision, str]:
    groups = media_turn_groups(rows)
    if decision.policy != MEDIA_POLICY_VISION_SUMMARY or not groups:
        return decision, ""
    plan = plan_media_digest(
        agent, context, groups, budget=budget_module.compact_summary_budget(agent), max_requests=max_requests,
    )
    outcome = generate_media_digest(agent, context, plan)
    return digest_media_decision(decision, plan, outcome), outcome.text


__all__ = [
    "MEDIA_DIGEST_PURPOSE",
    "MediaDigestChunk",
    "MediaDigestContext",
    "MediaDigestOutcome",
    "MediaDigestPlan",
    "digest_media_decision",
    "digest_request_tokens",
    "generate_media_digest",
    "largest_digest_request_tokens",
    "media_turn_groups",
    "plan_media_digest",
    "summarize_media_turns",
]
