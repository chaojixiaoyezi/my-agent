from __future__ import annotations

"""飞书原生 typing 反馈:收到消息给该消息贴一个 emoji reaction(OnIt"正在处理",≈🙇),
处理完撤掉 reaction 再发回复。这是飞书机器人原生的"正在输入"提示(在用户消息下方显示
"机器人名 + emoji"),区别于发占位卡片。需应用权限 im:message.reactions:write_only。

作为 mixin 混入 FeishuAdapter(依赖宿主的 _get_tenant_access_token / send_message),把 typing
反馈与适配器主体解耦。任何环节失败(无权限/网络/接口变更)都只是跳过 typing、绝不影响正常回复。
"""

import json
import logging
import random
import urllib.request
from typing import Any

from .protocol import OutgoingMessage

logger = logging.getLogger(__name__)

_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

# 飞书"处理中+正向情绪+友好动作"表情(emoji_type),每次随机一个:让 typing 提示有变化、显得 agent 心情不同。
# emoji_type 区分大小写;无效的那次贴失败会降级(不影响回复),真机验证后保留有效集。
_PROGRESS_EMOJIS = (
    "Typing", "OnIt", "Thinking", "OneSecond",          # 处理中/输入中
    "SMILE", "LAUGH", "LOL", "BLUSH", "WINK", "PROUD",   # 笑/俏皮/得意
    "JOYFUL", "WOW", "LOVE", "YEAH", "WITTY", "SMART",   # 欢乐/惊喜/机智
    "THUMBSUP", "FINGERHEART", "APPLAUSE", "CLAP",        # 点赞/比心/鼓掌
    "MUSCLE", "OK", "DONE", "HIGHFIVE", "SALUTE", "HUG",  # 加油/手势/拥抱
)


def _add_reaction(message_id: str, emoji_type: str, token: str) -> dict[str, Any]:
    """给消息贴 emoji reaction(无状态 HTTP helper);返回飞书 API 原始响应(data.reaction_id 用于撤销)。"""
    payload = {"reaction_type": {"emoji_type": emoji_type}}
    req = urllib.request.Request(
        f"{_FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _remove_reaction(message_id: str, reaction_id: str, token: str) -> dict[str, Any]:
    """撤掉消息上的 emoji reaction(无状态 HTTP helper);返回飞书 API 原始响应。"""
    req = urllib.request.Request(
        f"{_FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions/{reaction_id}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class FeishuTypingMixin:
    """混入 FeishuAdapter:用 emoji reaction 做飞书原生 typing 反馈。
    依赖宿主提供 _get_tenant_access_token() 和 send_message()。"""

    def send_progress_placeholder(self, user_id: str, message_id: str = "") -> str:
        """给收到的消息贴"正在处理"emoji,返回 "message_id:reaction_id" 句柄供完成后撤销。
        无 message_id/无权限/任何失败都返回""(typing 跳过,不影响回复)。"""
        if not message_id:
            return ""
        token = self._get_tenant_access_token()  # type: ignore[attr-defined]
        if not token:
            return ""
        try:
            result = _add_reaction(message_id, random.choice(_PROGRESS_EMOJIS), token)
            if result.get("code") == 0:
                reaction_id = str((result.get("data") or {}).get("reaction_id", "") or "")
                return f"{message_id}:{reaction_id}" if reaction_id else ""
            logger.warning(f"飞书 reaction 贴失败(typing 跳过,不影响回复): {result.get('msg')}")
        except Exception as exc:
            logger.warning(f"飞书 reaction 异常(typing 跳过): {exc}")
        return ""

    def finalize_response(self, user_id: str, handle: str, message: OutgoingMessage) -> bool:
        """先撤掉"正在处理"reaction(若贴成功过),再发最终回复:有 reply_to 就引用用户原消息,否则普通发。"""
        if handle and ":" in handle:
            self._remove_progress_reaction(handle)
        reply_to = str((message.metadata or {}).get("reply_to", "") or "")
        if reply_to:
            return self.reply_message(reply_to, message.content)  # type: ignore[attr-defined]
        return self.send_message(user_id, message)  # type: ignore[attr-defined]

    # LLM: Stop cleanup removes the exact Feishu reaction without posting a replacement chat message.
    # 函数用途: 任务中断时撤销原消息上的处理中 reaction。
    def clear_progress_placeholder(self, user_id: str, handle: str) -> None:
        del user_id
        if handle and ":" in handle:
            self._remove_progress_reaction(handle)

    def _remove_progress_reaction(self, handle: str) -> None:
        message_id, _, reaction_id = handle.partition(":")
        if not (message_id and reaction_id):
            return
        token = self._get_tenant_access_token()  # type: ignore[attr-defined]
        if not token:
            return
        try:
            _remove_reaction(message_id, reaction_id, token)
        except Exception as exc:
            logger.warning(f"飞书 reaction 撤销异常(不影响回复): {exc}")
