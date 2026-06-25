from __future__ import annotations

"""飞书原生 typing 反馈:收到消息给该消息贴一个 emoji reaction(OnIt"正在处理",≈🙇),
处理完撤掉 reaction 再发回复。这是飞书机器人原生的"正在输入"提示(在用户消息下方显示
"机器人名 + emoji"),区别于发占位卡片。需应用权限 im:message.reactions:write_only。

作为 mixin 混入 FeishuAdapter(依赖宿主的 _get_tenant_access_token / send_message),把 typing
反馈与适配器主体解耦。任何环节失败(无权限/网络/接口变更)都只是跳过 typing、绝不影响正常回复。
"""

import json
import logging
import urllib.request
from typing import Any

from .protocol import OutgoingMessage

logger = logging.getLogger(__name__)

_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
_PROGRESS_EMOJI = "OnIt"  # 飞书"正在处理/在做了"表情(≈🙇);收到贴它、完成撤掉


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
            result = _add_reaction(message_id, _PROGRESS_EMOJI, token)
            if result.get("code") == 0:
                reaction_id = str((result.get("data") or {}).get("reaction_id", "") or "")
                return f"{message_id}:{reaction_id}" if reaction_id else ""
            logger.warning(f"飞书 reaction 贴失败(typing 跳过,不影响回复): {result.get('msg')}")
        except Exception as exc:
            logger.warning(f"飞书 reaction 异常(typing 跳过): {exc}")
        return ""

    def finalize_response(self, user_id: str, handle: str, message: OutgoingMessage) -> bool:
        """先撤掉"正在处理"reaction(若贴成功过),再发最终回复。撤销失败不影响回复。"""
        if handle and ":" in handle:
            self._remove_progress_reaction(handle)
        return self.send_message(user_id, message)  # type: ignore[attr-defined]

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
