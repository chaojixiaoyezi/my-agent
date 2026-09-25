# LLM: 媒体压缩策略的唯一判定与投影模块：只读 agent.config、结构化块字段、线程结构化字段和视觉能力事实模块，不读正文、
#   不改 canonical 行。策略值 off / archived_refs / vision_summary；auto 配置下由事实选择：强制恢复、同代次 B 曾失败、
#   视频块、字节预算、摘要预算任一不满足都落归档引用并写 reason。视觉能力事实只来自档案声明（model_input_modalities）
#   或 backends.vision_capability 的结构化探针，不读模型自述。调用方不得自行判定。随图摘要的实际发送（按预算打包的看图小请求）
#   在 compact_media_digest.py；本模块只做决定、准入与投影。
# 模块用途: 决定含图历史压缩时走归档引用还是随图摘要，并把媒体块投影成可重新附上的引用文字块。
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from ..backends.errors import ProviderConfigurationError, ProviderRecoverableError
from ..backends.request_content import is_local_media_block
from .native_history import canonical_native_messages_from_metadata

logger = logging.getLogger(__name__)

MEDIA_POLICY_OFF = "off"
MEDIA_POLICY_ARCHIVED_REFS = "archived_refs"
MEDIA_POLICY_AUTO = "auto"
MEDIA_POLICY_VISION_SUMMARY = "vision_summary"
MEDIA_POLICIES = frozenset({MEDIA_POLICY_OFF, MEDIA_POLICY_ARCHIVED_REFS, MEDIA_POLICY_AUTO})
# B 路径的 typed 失败码：写进线程 compact_vision_failed_generation，同代次下一次压缩据此选 A；不计入熔断。
COMPACT_VISION_SUMMARY_FAILED = "COMPACT_VISION_SUMMARY_FAILED"
# B 部分成功的结构化原因：范围内只有一部分图块被看图小请求总结（预算或次数上限所限），其余按归档引用；不是失败。
MEDIA_REASON_DIGEST_PARTIAL = "vision_digest_partial"


# LLM: 配置值只认三个枚举，非法值按配置错误 fail closed，不静默回退默认；缺字段（旧测试替身）按默认 auto。
# 函数用途: 读取并校验用户配置的媒体压缩策略。
def configured_media_policy(agent: object) -> str:
    raw = getattr(getattr(agent, "config", None), "compact_media_policy", MEDIA_POLICY_AUTO)
    value = str(raw or MEDIA_POLICY_AUTO).strip().lower()
    if value not in MEDIA_POLICIES:
        raise ValueError(f"compact_media_policy 只能是 off、archived_refs 或 auto，当前为 {value!r}")
    return value


# LLM: policy ∈ off / archived_refs / vision_summary；fact_source 记录视觉能力事实来源（declared / probe_* / policy_forced /
#   vision_fact_pending / vision_fact_unavailable）；reason 记录 auto 下没走 B 的结构化原因（forced_recovery、
#   COMPACT_VISION_SUMMARY_FAILED、video_present、media_bytes_exceeded、summary_budget_exceeded、legacy_prompt）。
#   vision_candidate=True 表示"范围内若确有媒体块，再解析视觉事实（声明或探针）"；纯文字压缩因此永远不发探针。
#   summarized_blocks 记录本次实际由看图小请求总结的图块数：B 全部成功时等于范围内图块数，部分成功时小于它（reason 为
#   vision_digest_partial），A 路径为 0。
# 类用途: 一次压缩对媒体块采取的策略及其结构化依据。
@dataclass(frozen=True)
class CompactMediaDecision:
    policy: str
    fact_source: str
    reason: str = ""
    vision_candidate: bool = False
    summarized_blocks: int = 0


# LLM: supported 只可能来自声明含 image 或探针 probe_supported；fact_source 沿用决策枚举。
# 类用途: 视觉能力事实的判定结果。
@dataclass(frozen=True)
class VisionFact:
    supported: bool
    fact_source: str


# LLM: 先看档案声明（非空即权威，含 image 才支持），再看后端探针；缺后端或探针遇网络/额度/认证错误时按不支持、
#   fact_source 记 vision_fact_unavailable / probe_unavailable 并只记异常类型，不缓存、不抛给压缩主链。
# 函数用途: 取当前模型能否看图的结构化事实；可能触发一次进程内缓存的真实探针请求。
def vision_capability_fact(agent: object) -> VisionFact:
    declared = [str(item or "").strip().lower() for item in
                (getattr(getattr(agent, "config", None), "model_input_modalities", None) or ())]
    declared = [item for item in declared if item]
    if declared:
        return VisionFact("image" in declared, "declared")
    backend = getattr(agent, "backend", None)
    if backend is None:
        return VisionFact(False, "vision_fact_unavailable")
    from ..backends.vision_capability import resolve_vision_capability

    try:
        capability = resolve_vision_capability(backend)
    except (ProviderRecoverableError, ProviderConfigurationError) as exc:
        logger.warning("视觉能力探针暂不可用，本次按归档引用：%s", type(exc).__name__)
        return VisionFact(False, "probe_unavailable")
    return VisionFact(capability.supported, capability.fact_source)


# LLM: 请求前的结构化决定（不发请求）：off/archived_refs 直接落；auto 下 forced 恢复一律 A；线程同代次记录过 B 失败一律 A；
#   其余先给 archived_refs + vision_candidate=True，由候选构造在确认范围内有媒体块后调用 resolve_vision_candidate 解析事实。
#   候选 B 还要经 vision_summary_admission 过字节/预算门，那一步在摘要请求构造处执行。
# 函数用途: 解析本次压缩的媒体策略骨架，纯文字范围永远不触发探针。
def resolve_compact_media_policy(agent: object, *, forced: bool = False, thread: object | None = None) -> CompactMediaDecision:
    configured = configured_media_policy(agent)
    if configured == MEDIA_POLICY_OFF:
        return CompactMediaDecision(MEDIA_POLICY_OFF, "policy_forced")
    if configured == MEDIA_POLICY_ARCHIVED_REFS:
        return CompactMediaDecision(MEDIA_POLICY_ARCHIVED_REFS, "policy_forced")
    if forced:
        return CompactMediaDecision(MEDIA_POLICY_ARCHIVED_REFS, "policy_forced", "forced_recovery")
    if thread is not None and vision_summary_failed_this_generation(thread):
        return CompactMediaDecision(MEDIA_POLICY_ARCHIVED_REFS, "policy_forced", COMPACT_VISION_SUMMARY_FAILED)
    return CompactMediaDecision(MEDIA_POLICY_ARCHIVED_REFS, "vision_fact_pending", vision_candidate=True)


# LLM: 只在 vision_candidate 且范围内确有媒体块时调用；这里才读声明或发（缓存的）探针。支持 → vision_summary，否则 archived_refs，
#   fact_source 换成真实来源，vision_candidate 归零，避免同一候选重复解析。
# 函数用途: 把"待定"的媒体决定按视觉能力事实落成 B 候选或 A。
def resolve_vision_candidate(agent: object, decision: CompactMediaDecision) -> CompactMediaDecision:
    if not decision.vision_candidate:
        return decision
    fact = vision_capability_fact(agent)
    policy = MEDIA_POLICY_VISION_SUMMARY if fact.supported else MEDIA_POLICY_ARCHIVED_REFS
    return replace(decision, policy=policy, fact_source=fact.fact_source, vision_candidate=False)


# LLM: 只读线程两个结构化整数字段；代次推进（压缩成功）后自然失效，缺字段的旧线程视为没有失败记录。
# 函数用途: 判断本代次是否已经有一次随图摘要 typed 失败。
def vision_summary_failed_this_generation(thread: object) -> bool:
    # 代次 0 是合法值，不能用 `or -1` 归一化；只接受整数字段。
    value = getattr(thread, "compact_vision_failed_generation", -1)
    failed = int(value) if isinstance(value, int) and not isinstance(value, bool) else -1
    generation = getattr(thread, "compact_generation", 0)
    return failed >= 0 and isinstance(generation, int) and failed == int(generation)


# LLM: 把 B 候选改成 A 并记结构化原因；fact_source 保留原事实来源，说明"支持视觉但本次没随图"。
# 函数用途: 生成回落到归档引用的决定。
def archived_refs_fallback(decision: CompactMediaDecision, reason: str) -> CompactMediaDecision:
    return replace(decision, policy=MEDIA_POLICY_ARCHIVED_REFS, reason=reason)


# LLM: 引用只含块类型、sha256 前 12 位、文件名与字节数，不含 owner 目录路径；文案是给摘要模型的软引导，不参与任何机器判定。
# 函数用途: 把一个 canonical 媒体块换成可重新附上的归档引用文字块。
def archived_media_ref_block(block: dict[str, Any]) -> dict[str, Any]:
    source = block["source"]
    sha = str(source.get("sha256") or "")
    name = str(source.get("name") or sha[:12])
    return {"type": "text", "text": (
        f"[附件引用 {block.get('type')} sha256:{sha[:12]} 名称:{name} 大小:{int(source.get('size_bytes') or 0)} 字节；"
        "已归档，未随本次摘要发送；需要重看时请重新添加同一附件]"
    )}


# LLM: 只投影 user 行 content 列表里的 local_file 媒体块，其它块与 assistant 行逐字保留；返回新 dict，不改传入的消息或 canonical 行。
# 函数用途: 摘要来源重放时把媒体块换成归档引用，供 A 路径的 provider 来源使用。
def project_archived_media_message(message: dict[str, Any]) -> dict[str, Any]:
    if not _user_local_media_blocks(message):
        return message
    return {**message, "content": [archived_media_ref_block(block) if is_local_media_block(block) else block for block in message["content"]]}


# LLM: refs 为完整 sha256、按首次出现去重；blocks 计每个媒体块一次；bytes 为 size_bytes 之和；videos 计视频块。只读结构化 envelope。
# 类用途: 一次压缩范围内媒体块的数量、字节、视频数与内容地址，供准入判定和 checkpoint 使用。
@dataclass(frozen=True)
class MediaArchiveFacts:
    blocks: int = 0
    refs: tuple[str, ...] = ()
    bytes: int = 0
    videos: int = 0


# 函数用途: 取出一条 provider 消息里运输层会展开的媒体块（顶层 user 行的 local_file image/video），其它情况返回空。
def _user_local_media_blocks(message: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return ()
    return tuple(block for block in content if is_local_media_block(block))


# 函数用途: 依次产出一段行里所有将被归档的媒体块；只读 metadata 的结构化 envelope，不读正文。
def _archived_media_blocks(rows: Iterable[object]):
    for row in rows:
        for message in canonical_native_messages_from_metadata(getattr(row, "metadata", None)):
            yield from _user_local_media_blocks(message)


# LLM: 统计只依赖 canonical 信封的结构化字段；rows 可能是惰性快照，调用方负责在只读上下文中传入可迭代对象。
# 函数用途: 统计一段将被压缩的行里有多少媒体块、多少字节、多少视频及其内容地址。
def media_archive_facts(rows: Iterable[object]) -> MediaArchiveFacts:
    blocks, refs, total_bytes, videos = 0, [], 0, 0
    for block in _archived_media_blocks(rows):
        blocks += 1
        total_bytes += int(block["source"].get("size_bytes") or 0)
        videos += 1 if block.get("type") == "video" else 0
        sha = str(block["source"].get("sha256") or "")
        if sha and sha not in refs:
            refs.append(sha)
    return MediaArchiveFacts(blocks, tuple(refs), total_bytes, videos)


# LLM: 请求前的结构化准入：视频只走 A；Σ size_bytes 超过运输层 input_media_max_bytes 走 A（避免 project_input_media 的
#   带路径归档文字进入摘要）；request_tokens 是"最大的一次看图小请求"的完整估算（该请求的文字 + 它的图块数 × 预留），超过
#   摘要预算走 A——不再按整段压缩范围估算，所以阈值自动压缩的大范围不会天然超预算。都过才保持 vision_summary。不发请求。
# 函数用途: 决定一个 B 候选本次能不能发看图小请求，还是整体回落归档引用。
def vision_summary_admission(
    decision: CompactMediaDecision,
    facts: MediaArchiveFacts,
    *,
    media_max_bytes: int,
    request_tokens: int,
    budget: int,
) -> CompactMediaDecision:
    if decision.policy != MEDIA_POLICY_VISION_SUMMARY:
        return decision
    if facts.videos > 0:
        return archived_refs_fallback(decision, "video_present")
    if facts.bytes > max(0, int(media_max_bytes)):
        return archived_refs_fallback(decision, "media_bytes_exceeded")
    if int(request_tokens) > int(budget):
        return archived_refs_fallback(decision, "summary_budget_exceeded")
    return decision


# 函数用途: 按配置取每个图块的摘要预算预留 token 数；缺字段（测试替身）按默认 1600。
def media_token_reserve(agent: object) -> int:
    return max(0, int(getattr(getattr(agent, "config", None), "input_media_token_reserve", 1600) or 0))


# 函数用途: 按配置取一次压缩最多发几次看图小请求；缺字段（测试替身）按默认 4，最小 1。
def vision_digest_max_requests(agent: object) -> int:
    return max(1, int(getattr(getattr(agent, "config", None), "compact_vision_digest_max_requests", 4) or 4))
