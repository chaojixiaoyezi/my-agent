# LLM: reaction 是受理后的非关键反馈，失败不能改变原 ingress/Gateway 回执；日志只保留短引用、HTTP/code/类型。
# 模块用途: 使用官方列出的 Typing 表情提示处理状态，沿原句柄撤销，不随机尝试表情或记录正文/凭据。
from __future__ import annotations

"""飞书原生 typing 反馈:收到消息给该消息贴官方 Typing emoji reaction,
处理完撤掉 reaction 再发回复。这是飞书机器人原生的"正在输入"提示(在用户消息下方显示
"机器人名 + emoji"),区别于发占位卡片。需应用权限 im:message.reactions:write_only。

作为 mixin 混入 FeishuAdapter(依赖宿主的 _get_tenant_access_token / send_message),把 typing
反馈与适配器主体解耦。任何环节失败(无权限/网络/接口变更)都只是跳过 typing、绝不影响正常回复。
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .protocol import OutgoingMessage, provider_message_reference

logger = logging.getLogger(__name__)

_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

# 2026-10-14 官方表情表列出 Typing；应用权限、目标可见性与实际 HTTP 成功仍需逐次核验。
# https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce.md
_PROGRESS_EMOJI = 'Typing'


# LLM: API 响应只用于同一次 reaction；HTTPError 保留数值状态/code，不把响应文字或 Authorization 放日志。
# 函数用途: 执行原表情接口，并让错误响应保留可诊断的状态而不是只有“400”。
def _reaction_request(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read(16384).decode('utf-8', 'replace'))
            result['http_status'] = getattr(resp, 'status', None)
            return result
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read(16384).decode('utf-8', 'replace'))
        except (OSError, ValueError):
            result = {}
        finally:
            exc.close()
        return {'code': result.get('code') if isinstance(result, dict) else None,
                'http_status': exc.code, 'error_type': 'HTTPError'}


# LLM: 展示诊断不是执行状态；只输出有限字段，不转发 API msg 或异常 str，未知数值保持 None。
# 函数用途: 记录同一原消息的贴表情或撤表情结果，避免把没表情误判成任务没受理。
def _reaction_diagnostic(message_id: str, operation: str, status: str, result: dict[str, Any] | None = None, *, error_type: str = '') -> None:
    result = result or {}
    fields = {'message_ref': provider_message_reference(message_id), 'phase': 'reaction',
              'operation': operation, 'status': status, 'emoji_type': _PROGRESS_EMOJI}
    for key in ('http_status', 'code'):
        value = result.get(key)
        fields['provider_code' if key == 'code' else key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    if error_type:
        fields['error_type'] = error_type
    logger.info('feishu_feedback %s', json.dumps(fields, separators=(',', ':')))


# LLM: 原 POST 仅带供应商消息编号和官方 emoji_type；失败数值沿同一次响应返回，调用方不得因此拒收任务。
# 函数用途: 在用户原消息上贴一个表情，不发送新聊天消息。
def _add_reaction(message_id: str, emoji_type: str, token: str) -> dict[str, Any]:
    """给消息贴 emoji reaction(无状态 HTTP helper);返回飞书 API 原始响应(data.reaction_id 用于撤销)。"""
    payload = {"reaction_type": {"emoji_type": emoji_type}}
    req = urllib.request.Request(
        f"{_FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
    )
    return _reaction_request(req)


# LLM: 删除只针对保存的 exact reaction_id；结果不改变业务执行或最终回复回执。
# 函数用途: 撤销当前消息的临时表情，返回同次安全状态供诊断。
def _remove_reaction(message_id: str, reaction_id: str, token: str) -> dict[str, Any]:
    """撤掉消息上的 emoji reaction(无状态 HTTP helper);返回飞书 API 原始响应。"""
    req = urllib.request.Request(
        f"{_FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions/{reaction_id}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    return _reaction_request(req)


# LLM: 只管理现有 reaction 句柄；不新建进度消息/状态机，API 失败绝不能改变 Gateway 已受理事实。
# 类用途: 给飞书原消息提供和清理非关键处理中表情。
class FeishuTypingMixin:
    """混入 FeishuAdapter:用 emoji reaction 做飞书原生 typing 反馈。
    依赖宿主提供 _get_tenant_access_token() 和 send_message()。"""

    # LLM: 只在 manager 已拿到提交回执后调用；官方 emoji 能力与实际权限分开，日志不打印 token/异常正文。
    # 函数用途: 尝试贴 Typing 表情，成功返回原清理句柄，失败仍让回复 watcher 继续。
    def send_progress_placeholder(self, user_id: str, message_id: str = "") -> str:
        """给收到的消息贴"正在处理"emoji,返回 "message_id:reaction_id" 句柄供完成后撤销。
        无 message_id/无权限/任何失败都返回""(typing 跳过,不影响回复)。"""
        if not message_id:
            return ""
        token = self._get_tenant_access_token()  # type: ignore[attr-defined]
        if not token:
            _reaction_diagnostic(message_id, 'add', 'skipped_token_unavailable')
            return ""
        try:
            result = _add_reaction(message_id, _PROGRESS_EMOJI, token)
            if result.get("code") == 0 and result.get('http_status') in {None, 200, 201}:
                reaction_id = str((result.get("data") or {}).get("reaction_id", "") or "")
                _reaction_diagnostic(message_id, 'add', 'succeeded' if reaction_id else 'missing_handle', result)
                return f"{message_id}:{reaction_id}" if reaction_id else ""
            _reaction_diagnostic(message_id, 'add', 'failed', result)
        except Exception as exc:
            _reaction_diagnostic(message_id, 'add', 'failed', error_type=type(exc).__name__)
        return ""

    def finalize_response(self, user_id: str, handle: str, message: OutgoingMessage) -> bool:
        """先撤掉"正在处理"reaction(若贴成功过),再发最终回复:有 reply_to 就引用用户原消息,否则普通发。"""
        if handle and ":" in handle:
            self._remove_progress_reaction(handle)
        reply_to = str((message.metadata or {}).get("reply_to", "") or "")
        if reply_to:
            idempotency_key = str(
                (message.metadata or {}).get("delivery_idempotency_key")
                or ""
            )
            if idempotency_key:
                return self.reply_message(  # type: ignore[attr-defined]
                    reply_to,
                    message.content,
                    idempotency_key,
                )
            return self.reply_message(reply_to, message.content)  # type: ignore[attr-defined]
        return self.send_message(user_id, message)  # type: ignore[attr-defined]

    # LLM: Stop cleanup removes the exact Feishu reaction without posting a replacement chat message.
    # 函数用途: 任务中断时撤销原消息上的处理中 reaction。
    def clear_progress_placeholder(self, user_id: str, handle: str) -> None:
        del user_id
        if handle and ":" in handle:
            self._remove_progress_reaction(handle)

    # LLM: 只撤销原句柄并记录受限状态；失败不阻止最终回复，不能输出供应商错误正文或凭据。
    # 函数用途: 清理临时表情，使失败提示与业务回执保持独立。
    def _remove_progress_reaction(self, handle: str) -> None:
        message_id, _, reaction_id = handle.partition(":")
        if not (message_id and reaction_id):
            return
        token = self._get_tenant_access_token()  # type: ignore[attr-defined]
        if not token:
            _reaction_diagnostic(message_id, 'remove', 'skipped_token_unavailable')
            return
        try:
            result = _remove_reaction(message_id, reaction_id, token)
            ok = result.get('code') == 0 and result.get('http_status') in {None, 200, 201}
            _reaction_diagnostic(message_id, 'remove', 'succeeded' if ok else 'failed', result)
        except Exception as exc:
            _reaction_diagnostic(message_id, 'remove', 'failed', error_type=type(exc).__name__)
