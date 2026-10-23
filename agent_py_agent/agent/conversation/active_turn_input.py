# LLM: 插话携带包沿原 Compact 协议保存结构化媒体引用；纯归一化不授予文件读取权，入口与出站仍负责 owner/字节校验。
# 模块用途: 保留运行中用户输入的正文、身份和附件，使续跑不丢图也不重复消费。
from __future__ import annotations

"""Structured current-turn user input carried across compact continuations.

``/btw`` is a real user turn delivered while one durable task is already running.
The model-visible text lives in the provider-neutral ``UserTurn`` IR.  This small
packet is the continuation carrier: it keeps the same user turn available when
compact creates a fresh tool loop without parsing prompt text or replaying an
already-delivered guidance ledger entry.

This protocol belongs to the provider-neutral conversation layer so Gateway and
Agent Core share it without either runtime layer importing the other.
"""

from collections.abc import Iterable
from typing import Any

# LLM: 仅处理宿主已领取的 guidance；返回跨 Compact 的原输入投影，不确认消费、不执行媒体读取。
# 函数用途: 从原插话条目生成带稳定身份的携带包。
def packet_from_guidance(entries: Iterable[Any], text: str) -> dict[str, object] | None:
    content = str(text or "")
    entries = list(entries)
    input_ids = [
        value
        for entry in entries
        if (value := str(getattr(entry, "guidance_id", "") or "").strip())
    ]
    if not content.strip() or not input_ids:
        return None
    return {
        "schema_version": "active-turn-user-input.v1",
        "input_ids": input_ids,
        "text": content,
        **_packet_media({"input_media": [ref for entry in entries
            for ref in (getattr(entry, "metadata", None) or {}).get("input_media", [])]}),
    }


def append_active_turn_user_input(params: object, packet: dict[str, object] | None) -> bool:
    if packet is None:
        return False
    target = getattr(params, "active_turn_user_inputs", None)
    if not isinstance(target, list):
        return False
    merged = merge_active_turn_user_inputs(target, [packet])
    if len(merged) == len(target):
        return False
    target[:] = merged
    return True


# LLM: 同身份重放必须完整相等，媒体、正文或身份冲突不能静默吞掉；不同身份即使同文同图也分别保留。
# 函数用途: 按原输入身份幂等合并携带包并拒绝内容冲突。
def merge_active_turn_user_inputs(
    existing: Iterable[dict[str, object]] | None,
    incoming: Iterable[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: dict[tuple[str, ...], dict] = {}
    for collection in (existing, incoming):
        for raw in collection or []:
            packet = _normalized_packet(raw)
            if packet is None:
                continue
            key = tuple(packet["input_ids"])
            if key in seen:
                if seen[key] != packet:
                    raise ValueError("插话输入身份与媒体或正文冲突")
                continue
            seen[key] = packet
            merged.append(packet)
    return merged


def active_turn_user_input_texts(
    packets: Iterable[dict[str, object]] | None,
) -> list[str]:
    return [
        str(packet["text"])
        for raw in packets or []
        if (packet := _normalized_packet(raw)) is not None
    ]


# LLM: Unconsumed packets must return to the canonical guidance mailbox before a compact restart.
# Filtering uses structured guidance ids and never compares user text.
# 函数用途: 从跨轮携带包中移除尚未被模型接收、需要重新认领的补充消息。
def exclude_active_turn_user_input_ids(
    packets: Iterable[dict[str, object]] | None,
    excluded_ids: Iterable[str],
) -> list[dict[str, object]]:
    excluded = {str(item or "").strip() for item in excluded_ids if str(item or "").strip()}
    if not excluded:
        return merge_active_turn_user_inputs(packets)
    return [
        packet
        for raw in packets or []
        if (packet := _normalized_packet(raw)) is not None
        and not excluded.intersection(str(item) for item in packet["input_ids"])
    ]


# LLM: 旧纯文本形状保持不变；媒体只复制原结构化引用，格式损坏明确失败，不把坏图片静默降为文字。
# 函数用途: 规范携带包并保留可恢复的附件引用。
def _normalized_packet(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    if str(raw.get("schema_version") or "") != "active-turn-user-input.v1":
        return None
    text = str(raw.get("text") or "")
    raw_ids = raw.get("input_ids")
    if not text.strip() or not isinstance(raw_ids, list):
        return None
    input_ids: list[str] = []
    seen_ids: set[str] = set()
    for raw_id in raw_ids:
        input_id = str(raw_id or "").strip()
        if input_id and input_id not in seen_ids:
            input_ids.append(input_id)
            seen_ids.add(input_id)
    if not input_ids:
        return None
    return {
        "schema_version": "active-turn-user-input.v1",
        "input_ids": input_ids,
        "text": text,
        **_packet_media(raw),
    }


# LLM: HostInputMediaRef 只做不可变字段校验，不读文件、不把路径当授权；调用方保留真实 owner 和输入归属。
# 函数用途: 复制有结构校验的附件列表，无附件时兼容原纯文本包。
def _packet_media(raw: dict) -> dict:
    from dataclasses import asdict

    from .host_message_context import HostInputMediaRef

    media = raw.get("input_media", [])
    if not isinstance(media, (list, tuple)):
        raise ValueError("插话媒体携带包格式无效")
    return {"input_media": [asdict(HostInputMediaRef(**ref)) for ref in media]} if media else {}


__all__ = [
    "active_turn_user_input_texts",
    "append_active_turn_user_input",
    "exclude_active_turn_user_input_ids",
    "merge_active_turn_user_inputs",
    "packet_from_guidance",
]
