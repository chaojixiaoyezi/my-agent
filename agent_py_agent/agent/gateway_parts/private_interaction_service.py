# LLM: Count0原锁/admin callbacks保持懒调用；C显式同源config只读wrapper共用唯一前缀，typed来源故障不是无效用户卡。
# 模块用途: 为原Agent与冷C校验同一卡片，保G1先于读body；只有授权后表单typed错误返回可改正分类。
# LLM: 跨 owner 卡片入口必须先核对宿主凭据、SDK actor、签名和个人解锁，再初始化引用 owner；引用不是批准。
# 模块用途: 接受原 IM 适配器验证后的私人卡回调，将精确决定交给原工具审批记录。
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..conversation.im_tool_approval import resolve_im_tool_approval
from ..session_lock.service import UnlockService
from ..session_lock.store import SessionLockStore
from ..user_space.interaction_refs import verify_interaction_reference
from ..user_space.owner_resolver import OwnerIdentity
from ..user_space.task_credential_forms import CredentialFormValidationError
from .multiprocess_global_write import GlobalWriteError
from .request_worker import _resolve_request_agent_for_owner, admin_channel_identity_for_request


# LLM: 只读取原个人密码库的结构化状态，不能从卡片颜色、按钮或模型正文判已解锁；告警出站不经此门。
# 函数用途: 检查私人控制是否已首设密码并处于解锁期，不创建聊天、刷新活跃或自动解锁。
# LLM: 原Agent入口保持原Store初始化；冷C显式只读同库，不能取消个人密码门。
# 函数用途: 返回原私人控制的首设或闲置锁原因。
def private_interaction_lock_error(base_agent, actor_id: str) -> str:
    return private_interaction_lock_error_from_config(base_agent.home_paths.root,base_agent.config,actor_id)


# LLM: MAC/actor/expiry先于owner加载；签名群凭据及模型selection取消按当前成员核验，其余模型动作保留私人锁/原actor。
# 函数用途: 对一条已认证 SDK 控制事件核对身份、密码、执行和卡片，再写原批准记录。
# LLM: fields 仅在凭据/模型配置类型接受；模型控制走轻量原 owner store，不创建运行任务，原锁先于写账。
# 函数用途: 私人凭据 fields 只在 task_credential 接受，审批不收秘密；原锁和实际卡核验先于写账。 修改需同步原控制、恢复或权限测试。
# LLM: 默认原同步hook保持；仅真实HTTP显式approval_paths桥到同control receipt，模型/凭据原分支和G1/验签前缀不变。
# 函数用途: 处理原业务Agent的私卡回调。
def resolve_private_interaction(base_agent, body: dict, *, channel: str, actor_id: str, approval_paths=None) -> dict:
    payload, locked = validated_private_interaction(base_agent.home_paths.root, body,
        channel=channel, actor_id=actor_id,
        lock_error=lambda: private_interaction_lock_error(base_agent,actor_id),
        admin_identity=lambda: admin_channel_identity_for_request(base_agent,
            {"channel": channel,"user_id": actor_id,"metadata":{"channel":channel,"channel_chat_type":"p2p"}}))
    if locked is not None:
        return locked
    owner = OwnerIdentity(**payload['owner'])
    reference = body['reference']
    if payload["kind"] == "model_configuration":
        from ..capability.runtime_config_reload import capability_config_for_agent
        from .model_control_cards import model_control_host, submit_model_control
        capability = capability_config_for_agent(base_agent)
        if body["decision"]!="cancel" and capability is not None and not getattr(capability, "im_model_cards_enabled", True):
            raise ValueError("模型卡未启用")
        return submit_model_control(base_agent, model_control_host(base_agent, owner), owner, payload, body, actor_id=actor_id)
    agent = _resolve_request_agent_for_owner(base_agent, owner)
    if payload["kind"] == "feishu_oauth":
        from ..user_space.feishu_oauth import cancel_oauth_card
        return cancel_oauth_card(agent,payload,body,actor_id=actor_id)
    if payload["kind"] == "task_credential":
        from ..user_space.task_credentials import submit_credential_card
        return submit_credential_card(agent, payload, body)
    if "fields" in body:
        raise ValueError("审批不接收表单")
    if approval_paths is not None and body['decision'] in {'approved', 'denied'}:
        from ..conversation.tool_approval_scope import (
            ToolApprovalScope,
            tool_approval_scope_is_current,
        )
        from .private_approval_control import execute_count0_private_approval
        expected = ToolApprovalScope(**payload['scope'], agent_name='', active=False)
        if not tool_approval_scope_is_current(agent, expected):
            return execute_count0_private_approval(agent, approval_paths, payload, body, channel=channel, actor_id=actor_id)
    return resolve_im_tool_approval(agent, payload, reference=reference, message_id=str(body["message_id"]),
        chat_id=str(body["chat_id"]), decision=str(body["decision"]))


# LLM: 跨owner控制仍须实际宿主凭据；仅授权后parser的实际typed错误可返回form，原身份/期限/任务拒绝不降级，须同步adapter固定出口。
# 函数用途: 校验适配器来源后处理控制卡；原授权后的typed表单错误允许改正原卡，身份/版本和宿主故障仍沿原分类，不暴露输入。
def handle_private_interaction(handler, server) -> None:
    middleware = getattr(handler, "_auth_middleware", None)
    if middleware is None or not middleware.has_gateway_credential(dict(handler.headers)):
        handler._send_json(403, {"ok": False, "error_code": "LOCAL_CREDENTIAL_REQUIRED"})
        return
    try:
        if server is None:
            raise ValueError("服务不可用")
        channel = str(handler.headers.get("X-Channel") or "")
        actor_id = str(handler.headers.get("X-User-Id") or "")
        if not channel or not actor_id:
            raise ValueError("缺少已验证发送者")
        body = handler._read_json()
        control = getattr(server, 'control_plane', None)
        if control is not None and control.worker_count > 0:
            from .private_interaction_transaction import execute_coordinator_private_interaction
            result = execute_coordinator_private_interaction(handler,server,body,channel=channel,actor_id=actor_id)
        else:
            if server.agent is None:
                raise ValueError("服务不可用")
            result = resolve_private_interaction(server.agent,body,channel=channel,actor_id=actor_id,approval_paths=server.paths)
    except CredentialFormValidationError as error:
        handler._send_json(400, {'ok':False, 'error_code':error.code, 'error_stage':error.stage,
                                 'error_field':error.field, 'message':error.message})
        return
    except (OSError, RuntimeError, sqlite3.Error, GlobalWriteError):
        handler._send_json(503, {"ok": False, "error_code": "IM_INTERACTION_HOST_UNAVAILABLE",
                                 "error_stage": "execution", "message": "宿主暂时无法处理卡片，请稍后重试。"})
        return
    except (TypeError, ValueError, KeyError):
        handler._send_json(403, {"ok": False, "error_code": "IM_INTERACTION_INVALID",
                                 "error_stage": "interaction", "message": "这张确认卡已失效，或不属于当前私人会话。"})
        return
    handler._send_json(503 if result.get("error_code")=="COORDINATOR_BUSINESS_NOT_READY" else 200, result)


# LLM: 新read_only模式不建目录/迁移，仍用原has_password/last_activity/status判据；坏库保持错误不当成已解锁。
# 函数用途: 在冷C只读原个人锁，Count0默认仍原初始化。
def private_interaction_lock_error_from_config(home_root, config, actor_id, *, read_only=False):
    if not config.feishu_session_lock_enabled:
        return ''
    path = Path(home_root)/'session_lock'/'private_chat_locks.db'
    if not path.is_file():
        return 'IM_PASSWORD_SETUP_REQUIRED'
    service = UnlockService(SessionLockStore(path,read_only=read_only),idle_limit_seconds=config.feishu_personal_idle_lock_seconds)
    status = service.status(actor_id)
    return 'IM_PASSWORD_SETUP_REQUIRED' if status.requires_password_setup else 'IM_UNLOCK_REQUIRED' if status.locked else ''


# LLM: 原MAC/SDK actor/群submitter/chat/个人锁/admin顺序保持；callbacks只由两个原host资源wrapper提供，不从JSON签权。
# 函数用途: 为原Agent和真实C解析同一原卡身份，返回原锁提示或验签结果。
def validated_private_interaction(home_root, body, *, channel, actor_id, lock_error, admin_identity):
    allowed = {"reference", "decision", "message_id", "chat_id", "fields", "submitter"}
    if not set(body) <= allowed or not {"reference", "decision", "message_id", "chat_id"} <= set(body):
        raise ValueError("交互字段无效")
    if "submitter" not in body and set(body) not in ({"reference", "decision", "message_id", "chat_id"}, {"reference", "decision", "message_id", "chat_id", "fields"}):
        raise ValueError("交互字段无效")
    reference = body["reference"]
    if not isinstance(reference, str):
        raise ValueError("交互引用无效")
    payload = verify_interaction_reference(Path(home_root), reference, channel=channel, actor_id=actor_id, submitter=body.get("submitter"), decision=str(body["decision"]))
    if payload.get("kind") not in {"tool_approval", "task_credential", "model_configuration", "feishu_oauth"}:
        raise ValueError("交互类型无效")
    from ..user_space.interaction_refs import interaction_presentation
    presentation = interaction_presentation(payload,str(body["decision"])) or {}
    group_input = presentation.get("kind") == "group"
    if group_input and body["chat_id"] != presentation["chat_id"]:
        raise ValueError("群卡来源不匹配")
    error = "" if group_input or body["decision"]=="cancel" else lock_error()
    if error:
        return payload, {"ok": False, "error_code": error, "message": "请先在机器人私聊设置或解锁个人密码，再确认原卡片。"}
    owner = OwnerIdentity(**payload["owner"])
    if owner == OwnerIdentity.local_main() and admin_identity() is None:
        raise ValueError("管理员绑定已变化")
    return payload, None


# LLM: 显式config来自原C组合根，回调只沿原只读锁及唯一admin读口；与Count0共用同一验签/字段/执行逻辑，不物化Agent。
# 函数用途: 在冷C的每次末核读取当前个人锁及管理员绑定事实。
def validated_private_interaction_from_config(home_root, config, body, *, channel, actor_id):
    from .request_worker import _admin_channel_identity_from_config
    return validated_private_interaction(home_root,body,channel=channel,actor_id=actor_id,
        lock_error=lambda: private_interaction_lock_error_from_config(home_root,config,actor_id,read_only=True),
        admin_identity=lambda: _admin_channel_identity_from_config(config,home_root,
            {"channel":channel,"user_id":actor_id,"metadata":{"channel":channel,"channel_chat_type":"p2p"}}))
