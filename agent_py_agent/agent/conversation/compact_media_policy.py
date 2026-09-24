# LLM: 媒体压缩策略的唯一判定与投影模块：只读 agent.config.compact_media_policy 与结构化块字段，不读正文、不发请求、不改 canonical 行。
# 片 A 只落 off / archived_refs（auto 暂等价于 archived_refs）；视觉能力事实与 vision_summary 路径后续在本模块接入，调用方不得自行判定。
# 模块用途: 决定含图历史压缩时走归档引用还是保持后缀保护，并把媒体块投影成可重新附上的引用文字块。
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..backends.request_content import is_local_media_block
from .native_history import canonical_native_messages_from_metadata

MEDIA_POLICY_OFF = "off"
MEDIA_POLICY_ARCHIVED_REFS = "archived_refs"
MEDIA_POLICY_AUTO = "auto"
MEDIA_POLICIES = frozenset({MEDIA_POLICY_OFF, MEDIA_POLICY_ARCHIVED_REFS, MEDIA_POLICY_AUTO})


# LLM: 配置值只认三个枚举，非法值按配置错误 fail closed，不静默回退默认；缺字段（旧测试替身）按默认 auto。
# 函数用途: 读取并校验用户配置的媒体压缩策略。
def configured_media_policy(agent: object) -> str:
    raw = getattr(getattr(agent, "config", None), "compact_media_policy", MEDIA_POLICY_AUTO)
    value = str(raw or MEDIA_POLICY_AUTO).strip().lower()
    if value not in MEDIA_POLICIES:
        raise ValueError(f"compact_media_policy 只能是 off、archived_refs 或 auto，当前为 {value!r}")
    return value


# LLM: policy 只会是 off 或 archived_refs（B 接入后增加 vision_summary）；fact_source 记录本次决定的依据，进 checkpoint 供审计。
# 类用途: 一次压缩对媒体块采取的策略及其结构化依据。
@dataclass(frozen=True)
class CompactMediaDecision:
    policy: str
    fact_source: str


# LLM: auto 在视觉事实接入前一律落归档引用并如实记 vision_fact_unavailable；不探针、不读模型自述。
# 函数用途: 解析本次压缩的媒体策略。
def resolve_compact_media_policy(agent: object) -> CompactMediaDecision:
    configured = configured_media_policy(agent)
    if configured == MEDIA_POLICY_OFF:
        return CompactMediaDecision(MEDIA_POLICY_OFF, "policy_forced")
    if configured == MEDIA_POLICY_ARCHIVED_REFS:
        return CompactMediaDecision(MEDIA_POLICY_ARCHIVED_REFS, "policy_forced")
    return CompactMediaDecision(MEDIA_POLICY_ARCHIVED_REFS, "vision_fact_unavailable")


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


# LLM: refs 为完整 sha256、按首次出现去重；blocks 计每个媒体块一次。只读 metadata 的结构化 envelope，不读正文。
# 类用途: 一次压缩里将被归档的媒体块数量与内容地址，写进 checkpoint。
@dataclass(frozen=True)
class MediaArchiveFacts:
    blocks: int = 0
    refs: tuple[str, ...] = ()


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
# 函数用途: 统计一段将被压缩的行里有多少媒体块及其内容地址。
def media_archive_facts(rows: Iterable[object]) -> MediaArchiveFacts:
    blocks, refs = 0, []
    for block in _archived_media_blocks(rows):
        blocks += 1
        sha = str(block["source"].get("sha256") or "")
        if sha and sha not in refs:
            refs.append(sha)
    return MediaArchiveFacts(blocks, tuple(refs))
