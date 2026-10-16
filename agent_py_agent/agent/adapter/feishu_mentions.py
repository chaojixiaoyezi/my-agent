# LLM: 群聊触发只消费供应商结构化 mention/post at 与可信 bot open_id；未知身份不得作为稳定未提及完成去重。
# 模块用途: 共用webhook/WS提及裁决与命令寻址，将明确群监控授权的真实目标提及规范为准确ID，普通正文和@全员不授予权限。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass

from .feishu_media import extract_feishu_content, post_content_block


# LLM: allowed 是直接提及事实，verified 区分稳定不触发与身份/结构尚不可核验，不授予 owner/admin 权限。
# 类用途: 为入站回执保留匹配结果和安全原因，不依靠 UI 名称或正文判断。
@dataclass(frozen=True)
class FeishuMentionDecision:
    allowed: bool
    verified: bool
    reason_code: str
    mentions_count: int = 0
    post_at_count: int = 0


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
    keys = {row["key"] for row in normalize_mentions(message.get("mentions"))
            if row["open_id"] == bot_open_id and row["key"] and row["key"] not in {"@_all", "@all"}}
    if kind == "text":
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
            rows = _post_without_leading_bot_nodes(locale["content"], bot_open_id, keys)
            text, _media = extract_feishu_content("post", {**locale, "content": rows})
            locale["content"] = _post_without_trailing_bot_nodes(rows, bot_open_id, keys) if _is_control_text(text) else rows
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


# LLM: 只删除尾部可信 bot ID 或其结构化 key 对应的 at；不按显示名、正文或其它用户裁剪。
# 函数用途: 在显式控制富文本中去除后置机器人提及，不用昵称或转换后的文本替换内容。
def _post_without_trailing_bot_nodes(content: list, bot_open_id: str, bot_keys: set[str]) -> list:
    trailing = True
    rows = []
    for row in reversed(content):
        nodes = []
        for node in reversed(row if isinstance(row, list) else []):
            if trailing and isinstance(node, dict):
                if node.get("tag") == "at" and node.get("user_id") in {bot_open_id, *bot_keys}:
                    continue
                if node.get("tag") == "text" and not str(node.get("text") or "").strip():
                    continue
            nodes.append(node)
            trailing = False
        rows.append(list(reversed(nodes)))
    return list(reversed(rows))


# LLM: 头部 at 只按精确 bot ID 或可信 mention key 移除；其余正文节点和原分行必须保留。
# 函数用途: 将 post 行逐个规范化，避免命令转换入口深嵌套，同时保留命令参数中的其它人。
def _post_without_leading_bot_nodes(content: list, bot_open_id: str, bot_keys: set[str]) -> list:
    leading = True
    rows = []
    for row in content:
        nodes = []
        for node in row if isinstance(row, list) else []:
            if leading and isinstance(node, dict) and node.get("tag") == "at" and node.get("user_id") in {bot_open_id, *bot_keys}:
                continue
            nodes.append(node)
            if not (isinstance(node, dict) and node.get("tag") == "text" and not str(node.get("text") or "").strip()):
                leading = False
        rows.append(nodes)
    return rows


# LLM: SDK/字典同归一 key/open_id；字符串 id 仅接受显式 id_type=open_id，不从昵称或 ID 前缀猜身份。
# 函数用途: 保留飞书 mention 的结构字段，供两个入站路径使用同一判断。
def normalize_mentions(value: object) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    rows = []
    for item in value:
        identity = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        key = item.get("key", "") if isinstance(item, dict) else getattr(item, "key", "")
        open_id = identity.get("open_id", "") if isinstance(identity, dict) else getattr(identity, "open_id", "")
        id_type = item.get("id_type", "") if isinstance(item, dict) else getattr(item, "id_type", "")
        if isinstance(identity, str) and id_type == "open_id":
            open_id = identity
        if not open_id and isinstance(item, dict):
            open_id = item.get("open_id", "")
        rows.append({"key": str(key or ""), "open_id": str(open_id or "")})
    return rows


# LLM: 只规范显式/group监控控制命令中的准确平台提及，不能按显示名、正文ID前缀或模型参数猜目标；最终群权限/当前成员仍由控制入口复核。
# 函数用途: 把真实@某人的授权参数转为原member:编号，方便用户无需查open_id；不写状态或自行授予权限。
def group_monitor_command_target(text: str, message: dict[str, object], bot_open_id: str) -> str:
    parts = text.split()
    if len(parts) != 3 or parts[:2] != ["/group", "监控"]:
        return text
    target = parts[2]
    candidates: set[str] = set()
    mentions = normalize_mentions(message.get("mentions"))
    for row in mentions:
        if row["key"] == target and row["open_id"] not in {"", bot_open_id, "all", "@all", "@_all"}:
            candidates.add(row["open_id"])
    nodes, valid = _post_at_targets(message)
    if valid:
        for node in nodes:
            if target != "@" + node or node in {"", bot_open_id, "all", "@all", "@_all"}:
                continue
            mapped = {row["open_id"] for row in mentions if row["key"] == node and row["open_id"]}
            if mapped:
                candidates.update(mapped)
            elif not node.startswith("@"):
                candidates.add(node)
    if len(candidates) != 1:
        return text
    return "/group 监控 member:" + candidates.pop()


# LLM: 原 bool API 只投影唯一结构化裁决；调用方要决定重投时必须读取 bot_mention_decision.verified。
# 函数用途: 检查群聊消息是否明确单独 @了此应用机器人。
def directly_mentions_bot(message: dict[str, object], bot_open_id: str) -> bool:
    return bot_mention_decision(message, bot_open_id).allowed


# LLM: 顶层 ID 与 post at 同属可信事件结构；按 key 关联，任一精确匹配可触发，未解析字段不能伪装稳定未提及。
# 函数用途: 区分直接 @机器人、稳定无 @和暂时无法核验，换行与补文字不参与授权。
def bot_mention_decision(message: dict[str, object], bot_open_id: str) -> FeishuMentionDecision:
    mentions = normalize_mentions(message.get("mentions"))
    targets, valid = _post_at_targets(message)
    counts = {"mentions_count": len(mentions), "post_at_count": len(targets)}
    if not bot_open_id or bot_open_id in {'all', '@all', '@_all'}:
        return FeishuMentionDecision(False, False, "FEISHU_BOT_IDENTITY_UNAVAILABLE", **counts)
    keys = {row['key'] for row in mentions if row['open_id'] == bot_open_id and row['key'] not in {'@_all', '@all'}}
    if any(row['open_id'] == bot_open_id and row['key'] not in {'@_all', '@all'} for row in mentions) or any(
        target == bot_open_id or target in keys for target in targets
    ):
        return FeishuMentionDecision(True, True, "", **counts)
    malformed = message.get('mentions') is not None and not isinstance(message.get('mentions'), (list, tuple))
    incomplete = any(not row['open_id'] and row['key'] not in {'@_all', '@all'} for row in mentions)
    if malformed or incomplete or not valid or any(not target.strip() or target.startswith('@_user') for target in targets):
        return FeishuMentionDecision(False, False, "FEISHU_MENTION_UNVERIFIED", **counts)
    return FeishuMentionDecision(False, True, "FEISHU_GROUP_NOT_ADDRESSED", **counts)


# LLM: 仅读取 canonical post 包裹里的 at.user_id，不解析昵称/正文；格式未知返回不可核验而不是稳定忽略。
# 函数用途: 保留富文本里的结构化目标，供顶层 mentions 补全与群门共同核验。
def _post_at_targets(message: dict[str, object]) -> tuple[list[str], bool]:
    if str(message.get("message_type") or message.get("msg_type") or "") != "post":
        return [], True
    try:
        value = message.get("content")
        post = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return [], False
    if not isinstance(post, dict):
        return [], False
    locale = post_content_block(post)
    if not isinstance(locale.get('content'), list):
        return [], False
    targets = []
    for line in locale.get("content", []) if isinstance(locale.get("content"), list) else []:
        for node in line if isinstance(line, list) else []:
            if isinstance(node, dict) and node.get("tag") == "at":
                target = node.get('user_id')
                targets.append(target if isinstance(target, str) else '')
    return targets, True
