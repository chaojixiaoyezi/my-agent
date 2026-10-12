# LLM: 群聊触发只看飞书结构化 mention 与探针取得的 bot open_id；不得按正文、昵称或 @all 文案匹配。
# 模块用途: 共用 webhook/WS 的直接 @机器人判据，并保留结构化 post 内的 at 节点。
from __future__ import annotations

import json


# LLM: SDK 对象和 webhook 字典同归一为 key/open_id；不使用展示 name 授权，异常列表视为没有直接 mention。
# 函数用途: 保留飞书 mention 的结构字段，供两个入站路径使用同一判断。
def normalize_mentions(value: object) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    rows = []
    for item in value:
        identity = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        key = item.get("key", "") if isinstance(item, dict) else getattr(item, "key", "")
        open_id = identity.get("open_id", "") if isinstance(identity, dict) else getattr(identity, "open_id", "")
        rows.append({"key": str(key or ""), "open_id": str(open_id or "")})
    return rows


# LLM: bot 身份未知时群聊不触发；广播节点无论是否错误携带 bot ID 均不算直接 mention；私聊由上层放行。
# 函数用途: 检查群聊消息是否明确单独 @了此应用机器人。
def directly_mentions_bot(message: dict[str, object], bot_open_id: str) -> bool:
    if not bot_open_id:
        return False
    mentions = normalize_mentions(message.get("mentions"))
    if mentions:
        return any(row["key"] not in {"@_all", "@all"} and row["open_id"] not in {"all", "@all"}
                   and row["open_id"] == bot_open_id for row in mentions)
    if str(message.get("message_type") or message.get("msg_type") or "") != "post":
        return False
    try:
        value = message.get("content")
        post = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return False
    if not isinstance(post, dict):
        return False
    for locale in post.values():
        if not isinstance(locale, dict):
            continue
        for line in locale.get("content", []) if isinstance(locale.get("content"), list) else []:
            for node in line if isinstance(line, list) else []:
                if isinstance(node, dict) and node.get("tag") == "at" and node.get("user_id") == bot_open_id:
                    return True
    return False
