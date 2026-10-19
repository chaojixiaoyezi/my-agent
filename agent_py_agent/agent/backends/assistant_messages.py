# LLM: 逐消息来源只附在原text块；阶段不决定续跑，投影前必须核完整当前text一致性，联测拒绝/超时合成/跨协议回放。
# 模块用途: 保存模型可见消息的原编号、顺序和阶段，纯函数区分过程与终答，不执行工具或写历史。
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

MESSAGE_METADATA = "assistant_message"
KNOWN_PHASES = frozenset({"commentary", "final_answer"})


# LLM: 只取协议真实字段，collector缺失output_index保持None；未知phase逐字保留，不能用排序位置补造来源。
# 函数用途: 为一个Responses正文块记录原消息来源，不保存隐藏推理或工具参数。
def response_text_block(item: dict, part: dict, output_index: int | None, content_index: int) -> dict:
    return {"type": "text", "text": str(part.get("text") or part.get("refusal") or ""),
            MESSAGE_METADATA: {"protocol": "responses", "message_id": item.get("id") if isinstance(item.get("id"), str) else None,
                               "output_index": output_index, "content_index": content_index,
                               "phase": item.get("phase") if isinstance(item.get("phase"), str) else None,
                               "source_type": part["type"]}}


# LLM: 这里只清洗元数据，不产生可信宿主来源；执行/投递仍须核原sealed call，未知phase不归一。
# 函数用途: 从原text块复制合法的逐消息来源，非消息元数据返回空。
def message_metadata(block: dict) -> dict:
    raw = block.get(MESSAGE_METADATA)
    if not isinstance(raw, dict) or raw.get("protocol") != "responses":
        return {}
    return {key: deepcopy(value) for key, value in raw.items()
            if (key in {"protocol", "message_id", "phase", "source_type"} and (value is None or isinstance(value, str)))
            or (key in {"output_index", "content_index"} and type(value) is int and value >= 0)}


# LLM: 全体块须与当前受验text相等；缺实际message id/index的Responses块不猜同消息而合并，拒绝/合成退回当前text。
# 函数用途: 按原消息顺序取出可分阶段正文；不适用阶段投影时保留现有回复。
def response_messages(response: object) -> list[dict]:
    text = str(getattr(response, "text", "") or "")
    blocks = [block for block in getattr(response, "assistant_content_blocks", ()) or ()
              if isinstance(block, dict) and block.get("type") == "text"]
    if (not text or str(getattr(response, "runtime_status", "ok") or "ok") != "ok"
            or "".join(str(block.get("text") or "") for block in blocks) != text):
        return [{"text": text, "phase": None, "message_id": None, "output_index": None}] if text else []
    rows = []
    for block in blocks:
        meta = message_metadata(block)
        identity = (meta.get("protocol"), meta.get("message_id"), meta.get("output_index"))
        if rows and rows[-1]["identity"] == identity and identity != ("responses", None, None):
            rows[-1]["text"] += str(block.get("text") or "")
        else:
            rows.append({**meta, "text": str(block.get("text") or ""), "phase": meta.get("phase"),
                         "message_id": meta.get("message_id"), "output_index": meta.get("output_index"), "identity": identity})
    return rows


# LLM: 净化必须覆盖原整次正文；分段净化与整段结果不同时不得丢前文再发送后缀，不改原消息身份或历史。
# 函数用途: 检查逐消息公开投影能否保留原安全上下文，供终答与各显示出口共同保守降级。
def message_projection(rows: list[dict], project: Callable[[str], str]) -> tuple[list[dict], str, bool]:
    projected = [{**row, "text": project(str(row.get("text") or ""))} for row in rows]
    full = project("".join(str(row.get("text") or "") for row in rows))
    return projected, full, full == "".join(row["text"] for row in projected)


# LLM: 只从当前合法且一致的正文选阶段；整段安全上下文不闭合时保留净化整段，commentary-only仍无终答；不控制采样。
# 函数用途: 生成最终用户正文投影，保留模型响应text和原内容块不变。
def final_response_text(response: object) -> str:
    from ..conversation.channels import project_user_reply

    rows, full, separable = message_projection(response_messages(response), lambda text: project_user_reply(text).content)
    finals = [row["text"] for row in rows if row.get("phase") == "final_answer"]
    selected = "".join(finals or [row["text"] for row in rows if row.get("phase") != "commentary"])
    return selected if separable or not selected else full


# LLM: 元数据仅供内部历史；Anthropic等出站须显式剥离，不能让normalizer把新字段原样外发。
# 函数用途: 返回去掉内部消息来源的独立内容块副本。
def without_message_metadata(block: dict) -> dict:
    return {key: deepcopy(value) for key, value in block.items() if key != MESSAGE_METADATA}


# LLM: 结构化工具摘除后只保安全text及来源；thinking签名和其它原生块失效，不得带回旧工具或推理。
# 函数用途: 提供工具历史改写时可保留的正文块副本。
def text_only_blocks(blocks: list[dict]) -> list[dict]:
    return [deepcopy(block) for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
