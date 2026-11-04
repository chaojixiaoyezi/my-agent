# LLM: 只用宿主原 REST 认证读取当前群；不接模型群号/凭据，不订阅人类事件，不写游标或水位。
# 模块用途: 当前群平台可见 chat 历史的有界分页、精确筛选及诚实覆盖事实；不承诺包含全部话题回复。
from __future__ import annotations

import base64
import binascii
import json
import time
from urllib.parse import urlencode

from ..capability.runtime_config_reload import (
    capability_config_for_agent,
    deployment_capability_config,
)
from ..user_space.group_authorization import group_chat_id
from ..user_space.owner_policy import resolve_effective_owner_policy
from .feishu_provider_facts import (
    FeishuProviderFactError,
    _provider,
    _read,
    group_authority_snapshot,
)


# LLM: 上界集中为类属性，不新增模块级散落配置；现有授权 reader 保持自身的超时/成员完整性预算。
# 类用途: 限制一次查询的页数、数据量、正文长度、窗口和新历史读取预算。
class HistoryLimits:
    MAX_PAGES = 5
    PAGE_SIZE = 50
    MAX_MESSAGES = 200
    MAX_TEXT_CHARS = 2000
    BUDGET_SECONDS = 8.0
    MAX_WINDOW_SECONDS = 604800
    MAX_SENDER_IDS = 32
    MAX_CURSOR_CHARS = 65536
    MAX_PROVIDER_CURSOR_CHARS = 2048
    MAX_BODY_CHARS = 65536


# LLM: 只接受固定机器码；异常链不投影平台正文、URL、身份或秘密。
# 类用途: 向工具边界报告参数、开关、网络或授权失败，失败不返回已读取的群消息。
class ChannelMessageHistoryError(ValueError):
    CODES = frozenset({"TOOL_INVALID_ARGUMENTS", "CHANNEL_MESSAGES_DISABLED", "CHANNEL_MESSAGES_GROUP_REQUIRED",
        "CHANNEL_MESSAGES_SCOPE_UNVERIFIED", "NETWORK_DISABLED", "CHANNEL_MESSAGES_UNAVAILABLE",
        "FEISHU_PROVIDER_AUTH_UNAVAILABLE", "FEISHU_PROVIDER_UNAVAILABLE", "FEISHU_PROVIDER_RESPONSE_INVALID",
        "FEISHU_PROVIDER_SCOPE_REQUIRED", "FEISHU_PROVIDER_HTTP_REJECTED", "FEISHU_PROVIDER_READ_REJECTED",
        "FEISHU_PROVIDER_TRANSPORT_UNAVAILABLE", "FEISHU_BOT_GROUP_MEMBERSHIP_REQUIRED",
        "FEISHU_GROUP_OWNER_UNVERIFIED", "FEISHU_GROUP_TENANT_UNVERIFIED", "FEISHU_GROUP_MEMBERS_INCOMPLETE"})

    # LLM: code 来自本模块或原 provider 的净化结果，不能把自由异常正文带到输出。
    # 函数用途: 保存安全错误码。
    def __init__(self, code):
        self.error_code = code if code in self.CODES else "CHANNEL_MESSAGES_UNAVAILABLE"
        super().__init__(self.error_code)


# LLM: 只校验数据形状，不做网络、配置写入或凭据发现；bool 不作为整数。
# 函数用途: 为直接调用和工具参数门共用严格查询校验。
def validate_history_query(*, start_time, end_time, sender_ids=(), cursor='', limit=50):
    bounds = []
    for value in (start_time, end_time):
        if type(value) is str and value.isascii() and value.isdigit() and len(value) <= 12:
            value = int(value)
        if type(value) is not int or not 0 <= value <= 253402300799:
            raise ChannelMessageHistoryError("TOOL_INVALID_ARGUMENTS")
        bounds.append(value)
    start, end = bounds
    if not 0 < end - start <= HistoryLimits.MAX_WINDOW_SECONDS:
        raise ChannelMessageHistoryError("TOOL_INVALID_ARGUMENTS")
    if (type(limit) is not int or not 1 <= limit <= HistoryLimits.MAX_MESSAGES
            or not isinstance(sender_ids, (list, tuple)) or len(sender_ids) > HistoryLimits.MAX_SENDER_IDS
            or any(not _identifier(value) for value in sender_ids)
            or type(cursor) is not str or len(cursor) > HistoryLimits.MAX_CURSOR_CHARS):
        raise ChannelMessageHistoryError("TOOL_INVALID_ARGUMENTS")
    return start, end, tuple(sorted(set(sender_ids))), cursor, limit


# LLM: ID 仅按原不透明字符串精确匹配，不从昵称、角色或正文推断身份。
# 函数用途: 限制不透明字段长度并拒绝空白/控制字符。
def _identifier(value):
    return type(value) is str and 0 < len(value) <= 256 and value == value.strip() and not any(ord(x) < 32 for x in value)


# LLM: 此门只读本 owner 和共享部署开关；模型局部开关不能打开部署关闭项，网络拒绝先于授权 HTTP。
# 函数用途: 绑定当前飞书群，不接受私人或其他供应商 owner。
def current_history_chat(agent):
    home = getattr(agent, "home_paths", None)
    if getattr(home, "owner_kind", "") != "group" or getattr(home, "owner_provider", "") != "feishu":
        raise ChannelMessageHistoryError("CHANNEL_MESSAGES_GROUP_REQUIRED")
    try:
        runtime, deployment = capability_config_for_agent(agent), deployment_capability_config(home)
        if (getattr(runtime, "feishu_chat_history_enabled", False) is not True
                or getattr(deployment, "feishu_chat_history_enabled", False) is not True):
            raise ChannelMessageHistoryError("CHANNEL_MESSAGES_DISABLED")
        if not resolve_effective_owner_policy(home).network_enabled:
            raise ChannelMessageHistoryError("NETWORK_DISABLED")
        return group_chat_id(home)
    except ChannelMessageHistoryError:
        raise
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise ChannelMessageHistoryError("CHANNEL_MESSAGES_UNAVAILABLE") from exc


# LLM: 每页前及发表前重取实时完整群证明；证明失败时抛错丢弃私有结果，不降为成功的部分授权。
# 函数用途: 核验同一应用、租户、原群与机器人仍在群内。
def _authority(agent, chat_id):
    if current_history_chat(agent) != chat_id:
        raise ChannelMessageHistoryError("CHANNEL_MESSAGES_SCOPE_UNVERIFIED")
    try:
        proof = group_authority_snapshot(agent, chat_id)
        fields = ("app_id", "tenant_key", "owner_id")
        if (proof.get("chat_id") != chat_id or proof.get("members_complete") is not True
                or proof.get("bot_is_member") is not True or proof.get("owner_id_type") != "open_id"
                or any(not _identifier(proof.get(field)) for field in fields)):
            raise ChannelMessageHistoryError("CHANNEL_MESSAGES_SCOPE_UNVERIFIED")
        return tuple(proof[field] for field in fields)
    except FeishuProviderFactError as exc:
        raise ChannelMessageHistoryError(exc.reason_code) from exc
    except ChannelMessageHistoryError:
        raise
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise ChannelMessageHistoryError("CHANNEL_MESSAGES_UNAVAILABLE") from exc


# LLM: 游标只承载同查询、同应用/租户/群的供应商分页位置，不是授权；所有恢复仍经过实时群门。
# 函数用途: 生成不带认证秘密、不会跳过未消费页的续读位置。
def _encode_cursor(binding, token):
    raw = json.dumps({"v": 1, "query": binding, "page_token": token}, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


# LLM: 输入游标不可信；严格比对查询与实时权威，深层 JSON 的递归异常也转为安全参数失败。
# 函数用途: 解出供应商页码，不让游标改变容器、过滤或时间窗口。
def _decode_cursor(cursor, binding):
    if not cursor:
        return ""
    try:
        data = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
        token = data["page_token"]
        if (not isinstance(data, dict) or set(data) != {"v", "query", "page_token"} or type(data["v"]) is not int
                or data["v"] != 1 or data["query"] != binding or type(token) is not str
                or len(token) > HistoryLimits.MAX_PROVIDER_CURSOR_CHARS):
            raise ValueError("invalid history cursor")
        return token
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, RecursionError) as exc:
        raise ChannelMessageHistoryError("TOOL_INVALID_ARGUMENTS") from exc


# LLM: 只解析受限正文，不抓附件、图片或话题子树；不支持的正文明确标注不完整，不能据此宣称读到全部内容。
# 函数用途: 提取文本/富文本中已有文字，并给出正文截断事实。
def _text(row):
    raw = row.get("body", {}).get("content") if isinstance(row.get("body"), dict) else None
    if type(raw) is not str or len(raw) > HistoryLimits.MAX_BODY_CHARS:
        return "", False, "invalid_body"
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return "", False, "invalid_body"
    if row.get("msg_type") == "text" and isinstance(data, dict) and type(data.get("text")) is str:
        text = data["text"]
    elif row.get("msg_type") == "post" and isinstance(data, dict):
        stack, values, count = [data], [], 0
        while stack and count < 500:
            value = stack.pop()
            count += 1
            if isinstance(value, dict):
                for field in ("title", "text"):
                    if type(value.get(field)) is str:
                        values.append(value[field])
                stack.extend(reversed([item for item in value.values() if isinstance(item, (dict, list))]))
            elif isinstance(value, list):
                stack.extend(reversed(value))
        text = "\n".join(values)
        if stack:
            return text[:HistoryLimits.MAX_TEXT_CHARS], True, "body_budget"
    else:
        return "", False, "unsupported_body"
    return text[:HistoryLimits.MAX_TEXT_CHARS], len(text) > HistoryLimits.MAX_TEXT_CHARS, ""


# LLM: 容器、时间、sender 均再做本地精确检查；provider sender_type/id_type 原样保留，不把 bot open_id 当 app_id。
# 函数用途: 将一条平台消息变成冻结字段，删除消息和窗口外消息不进入正文结果。
def _message(row, chat_id, start, end, sender_ids):
    if not isinstance(row, dict):
        return None, False, "invalid_message"
    if row.get("chat_id") != chat_id:
        return None, False, "foreign_chat"
    if row.get("deleted") is True:
        return None, False, ""
    sender, created = row.get("sender"), row.get("create_time")
    if (not _identifier(row.get("message_id")) or not isinstance(sender, dict)
            or type(created) not in (str, int) or not str(created).isascii() or not str(created).isdigit()
            or len(str(created)) > 15 or not _identifier(row.get("msg_type"))
            or any(type(sender.get(field, "")) is not str or len(sender.get(field, "")) > 256
                   for field in ("id", "id_type", "sender_type"))):
        return None, False, "invalid_message"
    timestamp = int(created)
    if not start * 1000 <= timestamp < end * 1000 or (sender_ids and sender.get("id") not in sender_ids):
        return None, False, ""
    text, truncated, reason = _text(row)
    item = {"message_id": row["message_id"], "create_time": timestamp, "sender_type": sender.get("sender_type", ""),
        "sender_id": sender.get("id", ""), "sender_id_type": sender.get("id_type", ""), "msg_type": row["msg_type"],
        "text": text, "parent_id": row.get("parent_id", ""), "thread_id": row.get("thread_id", "")}
    if any(type(item[field]) is not str or len(item[field]) > 256 for field in ("parent_id", "thread_id")):
        return None, False, "invalid_message"
    return item, truncated, reason


# LLM: 新历史 HTTP 仅在剩余预算内开始；授权复用原 reader 的独立超时，不能据此承诺整个授权链八秒硬截止。
#   每页大小不超过剩余结果/抓取预算，拒绝超量响应，不丢页尾；游标不成为权限证明。create_time 保留毫秒整数。
#   空匹配仍携带私有计数/覆盖/游标，发表前同样无条件复核授权，失败不发布查询事实。
# 函数用途: 读取当前群平台可见的有限 chat 消息，返回实际查询范围、计数、截断与可续读位置。
def list_group_messages(agent: object, chat_id: str, *, start_time: int, end_time: int, sender_ids: tuple[str, ...]=(), cursor: str='', limit: int=50) -> dict[str, object]:
    start, end, senders, cursor, limit = validate_history_query(
        start_time=start_time, end_time=end_time, sender_ids=sender_ids, cursor=cursor, limit=limit)
    deadline = time.monotonic() + HistoryLimits.BUDGET_SECONDS
    authority = _authority(agent, chat_id)
    binding = {"chat_id": chat_id, "start_time": start, "end_time": end, "sender_ids": list(senders),
               "authority": list(authority)}
    token = _decode_cursor(cursor, binding)
    try:
        adapter, credential = _provider(agent)
        if adapter.app_id != authority[0]:
            raise ChannelMessageHistoryError("CHANNEL_MESSAGES_SCOPE_UNVERIFIED")
    except FeishuProviderFactError as exc:
        raise ChannelMessageHistoryError(exc.reason_code) from exc
    items, reasons, seen_ids, seen_tokens = [], [], set(), {token}
    fetched, pages, has_more, text_truncated, provider_error = 0, 0, True, False, None
    for page_number in range(HistoryLimits.MAX_PAGES):
        if time.monotonic() >= deadline:
            reasons.append("time_budget")
            break
        if page_number and _authority(agent, chat_id) != authority:
            raise ChannelMessageHistoryError("CHANNEL_MESSAGES_SCOPE_UNVERIFIED")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reasons.append("time_budget")
            break
        page_size = min(HistoryLimits.PAGE_SIZE, limit - len(items), HistoryLimits.MAX_MESSAGES - fetched)
        if page_size <= 0:
            reasons.append("message_budget")
            break
        query = {"container_id_type": "chat", "container_id": chat_id, "start_time": str(start), "end_time": str(end),
                 "sort_type": "ByCreateTimeAsc", "page_size": page_size}
        if token:
            query["page_token"] = token
        try:
            data = _read(credential, "/im/v1/messages?" + urlencode(query), timeout=min(5.0, remaining), stage="message")
        except FeishuProviderFactError as exc:
            # 平台文档码230027是读取消息的明确权限拒绝，与缺scope、HTTP 401/403同属授权失败：整体失败，不发布已缓冲页。
            if (exc.reason_code == "FEISHU_PROVIDER_SCOPE_REQUIRED" or exc.provider_code in {401, 403}
                    or (exc.reason_code == "FEISHU_PROVIDER_READ_REJECTED" and exc.provider_code == 230027)):
                raise ChannelMessageHistoryError(exc.reason_code) from exc
            provider_error = exc.safe_diagnostics()
            reasons.append("provider_read_failed")
            break
        pages += 1
        rows, more, next_token = data.get("items"), data.get("has_more"), data.get("page_token", "")
        if (not isinstance(rows, list) or len(rows) > page_size or type(more) is not bool
                or type(next_token) is not str or len(next_token) > HistoryLimits.MAX_PROVIDER_CURSOR_CHARS):
            reasons.append("invalid_page")
            break
        fetched += len(rows)
        if more and not next_token:
            reasons.append("missing_cursor")
            break
        if more and next_token in seen_tokens:
            reasons.append("repeated_cursor")
            break
        for row in rows:
            item, shortened, reason = _message(row, chat_id, start, end, senders)
            if reason:
                reasons.append(reason)
            text_truncated |= shortened
            if item is not None and item["message_id"] not in seen_ids:
                items.append(item)
                seen_ids.add(item["message_id"])
        has_more = more
        token = next_token if more else ""
        if not more:
            break
        seen_tokens.add(next_token)
        if len(items) >= limit:
            reasons.append("result_limit")
            break
        if fetched >= HistoryLimits.MAX_MESSAGES:
            reasons.append("message_budget")
            break
    else:
        reasons.append("page_budget")
    if _authority(agent, chat_id) != authority:
        raise ChannelMessageHistoryError("CHANNEL_MESSAGES_SCOPE_UNVERIFIED")
    if text_truncated:
        reasons.append("text_truncated")
    reasons = list(dict.fromkeys(reasons))
    coverage = {"status": "partial" if reasons or has_more else "complete", "reasons": reasons,
                "scope": "provider_visible_chat_messages", "thread_replies_complete": False,
                "body_complete": not any(reason in reasons for reason in ("text_truncated", "unsupported_body", "invalid_body", "body_budget")),
                "budget_scope": "history_requests; authority_uses_existing_reader_bounds"}
    if provider_error is not None:
        coverage["provider_error"] = provider_error
    return {"schema_version": "channel_message_page.v1", "chat_id": chat_id, "items": items,
            "next_cursor": _encode_cursor(binding, token) if has_more else "", "has_more": has_more,
            "coverage": coverage, "fetched_count": fetched, "matched_count": len(items), "pages_read": pages,
            "truncated": bool(reasons or has_more), "window": {"start_time": start, "end_time": end, "unit": "unix_seconds"}}
