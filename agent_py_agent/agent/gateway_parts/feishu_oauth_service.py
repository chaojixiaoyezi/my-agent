# LLM: 浏览器回调无宿主凭据但必须先验证MAC/state，再交换token并验证真实本人open_id；不记录query或正文。
# 模块用途: 处理准确部署路径的飞书本人登录回调，恢复原输入任务。
from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..user_space.feishu_oauth import _assert_actor, complete_oauth
from ..user_space.interaction_refs import verify_interaction_reference
from ..user_space.owner_resolver import OwnerIdentity
from .request_worker import _resolve_request_agent_for_owner


# LLM: 未验证payload只提供MAC核对actor，不能初始化owner或发HTTP；完整验签先于所有副作用。
# 函数用途: 从浏览器state核验原本人授权请求。
def oauth_callback_payload(base_agent, state):
    if not isinstance(state, str) or len(state) > 8192:
        raise ValueError("授权引用无效")
    prefix, encoded, _ = state.split(".")
    if prefix != "ir1":
        raise ValueError("授权引用无效")
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    hint = json.loads(raw)
    actor = hint.get("actor_id") if isinstance(hint, dict) else None
    if not isinstance(actor, str) or not actor:
        raise ValueError("授权身份无效")
    payload = verify_interaction_reference(Path(base_agent.home_paths.root), state, channel="feishu", actor_id=actor)
    if payload.get("kind") != "feishu_oauth":
        raise ValueError("授权引用类型无效")
    return payload


# LLM: callback精确匹配固定路径，参数只消费state/code；供应商错误和授权材料不进入错误日志。
# 函数用途: 验证签名与当前本人/管理员绑定后交给原 OAuth 状态机，浏览器只获固定回执，不泄露 token 或供应商正文。
def handle_feishu_oauth_callback(handler, server):
    try:
        if server is None or server.agent is None:
            raise ValueError("服务不可用")
        parts = urlsplit(handler.path)
        if parts.path != "/feishu/oauth/callback" or len(handler.path) > 16384:
            raise ValueError("回调路径无效")
        values = parse_qs(parts.query, keep_blank_values=True)
        if set(values) != {"state", "code"} or any(len(v) != 1 for v in values.values()):
            raise ValueError("回调参数无效")
        state, code = values["state"][0], values["code"][0]
        if not code or len(code) > 8192:
            raise ValueError("授权码无效")
        payload = oauth_callback_payload(server.agent, state)
        owner = OwnerIdentity(**payload["owner"])
        if owner.owner_kind == "group":
            raise ValueError("群不能保存本人授权")
        agent = _resolve_request_agent_for_owner(server.agent, owner)
        _assert_actor(agent, payload["actor_id"])
        result = complete_oauth(agent, payload, state, code)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        handler._send_json(403, {"ok": False, "error_code": "FEISHU_USER_AUTH_REQUIRED",
                                 "message": "授权未保存；请在私人助手会话重新发起本人授权。"})
        return
    handler._send_json(200, {**result, "message": "本人授权已安全保存，原任务将恢复。"})
