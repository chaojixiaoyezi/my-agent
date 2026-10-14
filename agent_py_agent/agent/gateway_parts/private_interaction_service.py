# LLM: 跨 owner 卡片入口必须先核对宿主凭据、SDK actor、签名和个人解锁，再初始化引用 owner；引用不是批准。
# 模块用途: 接受原 IM 适配器验证后的私人卡回调，将精确决定交给原工具审批记录。
from __future__ import annotations

from pathlib import Path

from ..conversation.im_tool_approval import resolve_im_tool_approval
from ..session_lock.service import UnlockService
from ..session_lock.store import SessionLockStore
from ..user_space.interaction_refs import verify_interaction_reference
from ..user_space.owner_resolver import OwnerIdentity
from .request_worker import _resolve_request_agent_for_owner, admin_channel_identity_for_request


# LLM: 只读取原个人密码库的结构化状态，不能从卡片颜色、按钮或模型正文判已解锁；告警出站不经此门。
# 函数用途: 检查私人控制是否已首设密码并处于解锁期，不创建聊天、刷新活跃或自动解锁。
def private_interaction_lock_error(base_agent, actor_id: str) -> str:
    config = base_agent.config
    if not config.feishu_session_lock_enabled:
        return ""
    path = Path(base_agent.home_paths.root) / "session_lock" / "private_chat_locks.db"
    if not path.is_file():
        return "IM_PASSWORD_SETUP_REQUIRED"
    service = UnlockService(SessionLockStore(path), idle_limit_seconds=config.feishu_personal_idle_lock_seconds)
    status = service.status(actor_id)
    return "IM_PASSWORD_SETUP_REQUIRED" if status.requires_password_setup else "IM_UNLOCK_REQUIRED" if status.locked else ""


# LLM: MAC/actor/expiry 必须先于目标 owner 加载；local/main 还需当前管理员绑定，群来源只接受原发起人的签名私人请求。
# 函数用途: 对一条已认证 SDK 控制事件核对身份、密码、执行和卡片，再写原批准记录。
# LLM: fields 仅在凭据/模型配置类型接受；模型控制走轻量原 owner store，不创建运行任务，原锁先于写账。
# 函数用途: 私人凭据 fields 只在 task_credential 接受，审批不收秘密；原锁和实际卡核验先于写账。 修改需同步原控制、恢复或权限测试。
def resolve_private_interaction(base_agent, body: dict, *, channel: str, actor_id: str) -> dict:
    if set(body) not in ({"reference", "decision", "message_id", "chat_id"}, {"reference", "decision", "message_id", "chat_id", "fields"}):
        raise ValueError("交互字段无效")
    reference = body["reference"]
    if not isinstance(reference, str):
        raise ValueError("交互引用无效")
    payload = verify_interaction_reference(Path(base_agent.home_paths.root), reference, channel=channel, actor_id=actor_id)
    if payload.get("kind") not in {"tool_approval", "task_credential", "model_configuration"}:
        raise ValueError("交互类型无效")
    error = private_interaction_lock_error(base_agent, actor_id)
    if error:
        return {"ok": False, "error_code": error, "message": "请先在机器人私聊设置或解锁个人密码，再确认原卡片。"}
    owner = OwnerIdentity(**payload["owner"])
    if owner == OwnerIdentity.local_main() and admin_channel_identity_for_request(base_agent,
            {"channel": channel, "user_id": actor_id, "metadata": {"channel": channel, "channel_chat_type": "p2p"}}) is None:
        raise ValueError("管理员绑定已变化")
    if payload["kind"] == "model_configuration":
        from ..capability.runtime_config_reload import capability_config_for_agent
        from .model_control_cards import model_control_host, submit_model_control
        capability = capability_config_for_agent(base_agent)
        if capability is not None and not getattr(capability, "im_model_cards_enabled", True):
            raise ValueError("模型卡未启用")
        return submit_model_control(base_agent, model_control_host(base_agent, owner), owner, payload, body)
    agent = _resolve_request_agent_for_owner(base_agent, owner)
    if payload["kind"] == "task_credential":
        from ..user_space.task_credentials import submit_credential_card
        return submit_credential_card(agent, payload, body)
    if "fields" in body:
        raise ValueError("审批不接收表单")
    return resolve_im_tool_approval(agent, payload, reference=reference, message_id=str(body["message_id"]),
        chat_id=str(body["chat_id"]), decision=str(body["decision"]))


# LLM: 新跨 owner 控制必须有实际宿主凭据，即使 G2a 旧回环兼容仍开；模型不能靠 localhost 或复制签名引用代人批准。
# 函数用途: 校验适配器来源后处理私人卡回调，失败只返回稳定错误码，不暴露原请求或秘密。
def handle_private_interaction(handler, server) -> None:
    middleware = getattr(handler, "_auth_middleware", None)
    if middleware is None or not middleware.has_gateway_credential(dict(handler.headers)):
        handler._send_json(403, {"ok": False, "error_code": "LOCAL_CREDENTIAL_REQUIRED"})
        return
    try:
        if server is None or server.agent is None:
            raise ValueError("服务不可用")
        channel = str(handler.headers.get("X-Channel") or "")
        actor_id = str(handler.headers.get("X-User-Id") or "")
        if not channel or not actor_id:
            raise ValueError("缺少已验证发送者")
        result = resolve_private_interaction(server.agent, handler._read_json(), channel=channel, actor_id=actor_id)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        handler._send_json(403, {"ok": False, "error_code": "IM_INTERACTION_INVALID",
                                 "message": "这张确认卡已失效，或不属于当前私人会话。"})
        return
    handler._send_json(200, result)
