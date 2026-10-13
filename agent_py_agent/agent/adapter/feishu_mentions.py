# LLM: 群聊触发只看飞书结构化 mention 与探针取得的 bot open_id；不得按正文、昵称或 @all 文案匹配。
# 模块用途: 共用 webhook/WS 的直接 @机器人判据与命令首尾寻址规范化，保留其它用户和命令参数。
from __future__ import annotations

import json
from copy import deepcopy

from .feishu_media import extract_feishu_content, post_content_block


# LLM: 只移除可信 bot ID 对应的寻址 mention；尾部只在显式 slash 控制中处理，正文与其它用户不授予控制权。
# 函数用途: 让机器人前后提及都沿原斜杠入口执行，保留真正参数与普通富文本内容。
def command_content_without_bot_mention(message: dict[str, object], bot_open_id: str) -> object:
    raw = message.get("content")
    if not bot_open_id:
        return raw
    try:
        content = json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(content, dict):
        return raw
    kind = message.get("message_type") or message.get("msg_type") or "text"
    if kind == "text":
        keys = {row["key"] for row in normalize_mentions(message.get("mentions"))
                if row["open_id"] == bot_open_id and row["key"] and row["key"] not in {"@_all", "@all"}}
        text = str(content.get("text") or "").lstrip()
        while (key := next((key for key in sorted(keys, key=len, reverse=True)
                            if text.startswith(key) and (len(text) == len(key) or text[len(key)].isspace() or text[len(key)] == "/")), "")):
            text = text[len(key):].lstrip()
        if _is_control_text(text):
            text = _without_trailing_bot_keys(text, keys)
        content["text"] = text
    elif kind == "post":
        locale = post_content_block(content)
        if isinstance(locale.get("content"), list):
            rows = _post_without_leading_bot_nodes(locale["content"], bot_open_id)
            text, _media = extract_feishu_content("post", {**locale, "content": rows})
            locale["content"] = _post_without_trailing_bot_nodes(rows, bot_open_id) if _is_control_text(text) else rows
    return content


# LLM: 仅复用显式系统协议判据，不按正文关键词猜测指令；未知 slash 仍交原解析器拒绝。
# 函数用途: 判断尾部机器人提及是否属于命令的寻址包裹。
def _is_control_text(text: str) -> bool:
    from ..command_catalog import system_slash_command_name

    return bool(system_slash_command_name(text))


# LLM: key 来自已核验 bot ID 的结构化 mention；只删除独立尾部 token，不裁掉密码/普通参数中的子串。
# 函数用途: 移除斜杠命令结尾的机器人寻址占位，并保留其他提及。
def _without_trailing_bot_keys(text: str, keys: set[str]) -> str:
    text = text.rstrip()
    while key := next((key for key in sorted(keys, key=len, reverse=True)
                      if text.endswith(key) and len(text) > len(key) and text[-len(key)-1].isspace()), ""):
        text = text[:-len(key)].rstrip()
    return text


# LLM: 只删除最后的 bot at 节点及包裹空白，跨行保持原 content；中间节点、代码块和其它用户均不变。
# 函数用途: 在显式控制富文本中去除后置机器人提及，不用昵称或转换后的文本替换内容。
def _post_without_trailing_bot_nodes(content: list, bot_open_id: str) -> list:
    trailing = True
    rows = []
    for row in reversed(content):
        nodes = []
        for node in reversed(row if isinstance(row, list) else []):
            if trailing and isinstance(node, dict):
                if node.get("tag") == "at" and node.get("user_id") == bot_open_id:
                    continue
                if node.get("tag") == "text" and not str(node.get("text") or "").strip():
                    continue
            nodes.append(node)
            trailing = False
        rows.append(list(reversed(nodes)))
    return list(reversed(rows))


# LLM: 只移除头部精确机器人节点；第一段其它实质内容之后保持所有 at/text 节点和原分行。
# 函数用途: 将 post 行逐个规范化，避免命令转换入口深嵌套，同时保留命令参数中的其它人。
def _post_without_leading_bot_nodes(content: list, bot_open_id: str) -> list:
    leading = True
    rows = []
    for row in content:
        nodes = []
        for node in row if isinstance(row, list) else []:
            if leading and isinstance(node, dict) and node.get("tag") == "at" and node.get("user_id") == bot_open_id:
                continue
            nodes.append(node)
            if not (isinstance(node, dict) and node.get("tag") == "text" and not str(node.get("text") or "").strip()):
                leading = False
        rows.append(nodes)
    return rows


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
        if not open_id and isinstance(item, dict):
            open_id = item.get("open_id", "")
        rows.append({"key": str(key or ""), "open_id": str(open_id or "")})
    return rows


# LLM: bot 身份未知时群聊不触发；可信节点沿正文同一包裹层读取，广播仍不算直接 mention；私聊由上层放行。
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
    locale = post_content_block(post)
    for line in locale.get("content", []) if isinstance(locale.get("content"), list) else []:
        for node in line if isinstance(line, list) else []:
            if isinstance(node, dict) and node.get("tag") == "at" and node.get("user_id") == bot_open_id:
                return True
    return False
