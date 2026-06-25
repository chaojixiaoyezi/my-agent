from __future__ import annotations

"""飞书占位卡片 typing 反馈:收到消息先发"🤔 正在思考"卡片,处理完用 PATCH 原地更新成答案。

飞书机器人没有微信/Telegram 那种原生"正在输入"信令,这是业界通行的"卡片占位 + 原地更新"方案。
作为 mixin 混入 FeishuAdapter(依赖宿主的 _get_tenant_access_token / send_message),把卡片反馈
与适配器主体解耦——也让 FeishuAdapter 类不必为这组方法继续膨胀。任何环节失败都降级发新消息、绝不丢结果。
"""

import json
import logging
import urllib.request
from typing import Any

from .protocol import OutgoingMessage

logger = logging.getLogger(__name__)

_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


def _thinking_card() -> dict[str, Any]:
    """处理中占位卡片(lark_md);完成后由 _patch_card 原地更新成答案。"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "🤔 正在思考，请稍候…"}}],
    }


def _answer_card(text: str) -> dict[str, Any]:
    """最终答案卡片(lark_md);原地替换占位卡片内容。"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text or "（无内容）"}}],
    }


def _post_card(user_id: str, card: dict[str, Any], token: str) -> dict[str, Any]:
    """发交互卡片(无状态 HTTP helper);返回飞书 API 原始响应。"""
    payload = {"receive_id": user_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
    req = urllib.request.Request(
        f"{_FEISHU_API_BASE}/im/v1/messages?receive_id_type=open_id",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _patch_card(message_id: str, card: dict[str, Any], token: str) -> dict[str, Any]:
    """原地更新已发卡片(无状态 HTTP helper);返回飞书 API 原始响应。"""
    payload = {"content": json.dumps(card, ensure_ascii=False)}
    req = urllib.request.Request(
        f"{_FEISHU_API_BASE}/im/v1/messages/{message_id}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class FeishuCardFeedbackMixin:
    """混入 FeishuAdapter:提供"思考中"占位卡片 + 完成后原地更新(typing 反馈)。
    依赖宿主提供 _get_tenant_access_token() 和 send_message()。"""

    def send_progress_placeholder(self, user_id: str) -> str:
        """发"正在思考"占位卡片,返回 message_id 供完成后原地更新。任何失败返回""(降级:完成后发新消息)。"""
        token = self._get_tenant_access_token()  # type: ignore[attr-defined]
        if not token:
            return ""
        try:
            result = _post_card(user_id, _thinking_card(), token)
            if result.get("code") == 0:
                return str((result.get("data") or {}).get("message_id", "") or "")
            logger.warning(f"飞书占位卡片发送失败(降级为完成后发结果): {result.get('msg')}")
        except Exception as exc:
            logger.warning(f"飞书占位卡片异常(降级): {exc}")
        return ""

    def finalize_response(self, user_id: str, handle: str, message: OutgoingMessage) -> bool:
        """有占位句柄就 PATCH 把占位卡片原地更新成答案;无句柄或更新失败都降级发新消息(绝不丢结果)。"""
        if not handle:
            return self.send_message(user_id, message)  # type: ignore[attr-defined]
        token = self._get_tenant_access_token()  # type: ignore[attr-defined]
        if not token:
            return self.send_message(user_id, message)  # type: ignore[attr-defined]
        try:
            result = _patch_card(handle, _answer_card(message.content), token)
            if result.get("code") == 0:
                return True
            logger.warning(f"飞书卡片更新失败(降级为发新消息): {result.get('msg')}")
        except Exception as exc:
            logger.warning(f"飞书卡片更新异常(降级): {exc}")
        return self.send_message(user_id, message)  # type: ignore[attr-defined]
