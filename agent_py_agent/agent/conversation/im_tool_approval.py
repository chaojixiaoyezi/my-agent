# LLM: C取消只复用原Store/审计/transition，不复活执行源；批准仍由原实际scope门负责。
# 模块用途: 将冷C准确取消接入原私人审批账，保留Count0发布/决定/等待合同。
# LLM: IM 是原审批账的运输与决定入口，不拥有第二份批准状态；冻结 actor 和真实卡片身份，决定仍走原 scope/request/claim/attempt。
# 模块用途: 将后台主子审批送到原发起人私聊，并把签名引用核对回原等待记录。
from __future__ import annotations

import math
import time
from dataclasses import asdict, fields

from ..capability.runtime_config_reload import capability_config_for_agent
from ..common.json_io import locked_json_path, read_json_object_report, write_json_file_atomic
from ..contracts.tool_approval import ToolApprovalDecision, approval_deadline
from ..conversation.channels import (
    DeliveryContext,
    project_host_paths_for_channel,
    project_user_reply,
)
from ..user_space.interaction_refs import sign_interaction_reference
from ..user_space.owner_resolver import OwnerIdentity, owner_identity_from_config
from .host_actor import HostActorError, background_host_actor, resolve_host_actor, task_host_actor


# LLM: scope 原 dataclass 的 compare 字段是唯一身份口径，名称和展示 active 不得悄悄变成新授权键。
# 函数用途: 生成签名与回调共用的审批身份，修改显示名不影响原调用，实际停止仍单独核对。
def _scope_identity(scope) -> dict:
    return {field.name: getattr(scope, field.name) for field in fields(scope) if field.compare}


# LLM: 前台专有 actor 与原子任务记录/immutable origin 同源；模型 attrs 不提供私人接收者，群缺身份不回落管理员。
# 函数用途: 解析原主子审批的发起人，冲突或旧伪造身份明确拒绝。
def approval_recipient(agent, scope) -> tuple[str, str]:
    current = getattr(agent, "_current_run_params", None)
    attributes = getattr(current, "task_attributes", None) or {}
    current_root = str(getattr(current, "task_id", "") or "")
    if current_root and current_root != scope.root_task_id:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
    store = agent.conversation_store
    link, error = store.tasks.load_report(scope.root_task_id)
    if error:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    thread_id = str(getattr(link, "thread_id", "") or "")
    if not thread_id:
        root = agent.subagents.load(scope.root_task_id)
        thread_id = str((root.attributes or {}).get("conversation_thread_id") or "")
        actor = task_host_actor(agent, root)
    else:
        actor = background_host_actor(agent, thread_id, scope.root_task_id)
    if attributes.get("conversation_thread_id") and attributes["conversation_thread_id"] != thread_id:
        raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
    if getattr(current, "host_channel_actor", None) is not None:
        active = resolve_host_actor(agent, thread_id)
        if actor is None or active is None:
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
        if (active.owner_id != actor.owner_id or active.source_thread_id != actor.source_thread_id
                or active.binding() != actor.binding()):
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
    if actor is not None:
        if actor.channel != "feishu":
            raise ValueError("审批来源没有可用的私人通道")
        return actor.channel, actor.actor_id
    thread = store.threads.require(thread_id or scope.thread_id)
    home = getattr(agent, "home_paths", None)
    owner = (OwnerIdentity(home.owner_provider, home.owner_kind, home.owner_id)
             if home is not None else owner_identity_from_config(agent.config))
    identities = {(binding.channel, binding.channel_user_id) for binding in thread.channel_bindings
                  if owner.owner_kind == "user" and binding.channel == owner.provider
                  and binding.channel_user_id == owner.owner_id}
    if len(identities) == 1:
        return next(iter(identities))
    if owner.owner_kind != "main" or owner.provider != "local":
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
    from ..capability.channel_message_target import resolve_message_target

    channel, actor, _service, error = resolve_message_target(agent, "feishu")
    if error or not actor:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
    return channel, actor


# LLM: 显示正文只经原公开投影，按钮只带宿主签名引用及固定决定；未包含原参数或秘密。
# 函数用途: 构造本次调用的允许、拒绝与独立取消审批卡，点击后仍需验证当前执行与私人卡片来源。
def approval_card(reference: str, request, scope) -> tuple[dict, str]:
    summary = project_host_paths_for_channel(project_user_reply(request.description).content, "feishu")[:240]
    text = f"{scope.agent_name}申请执行 {request.tool_name}\n{summary}\n请在本次确认截止前处理，超时将拒绝当前操作。"
    buttons = [{"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": color,
                "value": {"kind": "my_agent_tool_approval", "reference": reference, "decision": decision}}
               for label, color, decision in (("允许本次", "primary", "approved"), ("拒绝", "default", "denied"), ("取消", "default", "cancel"))]
    return {"config": {"wide_screen_mode": True}, "elements": [
        {"tag": "div", "text": {"tag": "plain_text", "content": text}}, {"tag": "action", "actions": buttons}]}, text


# LLM: transport 写在原 pending 记录里，外发前先冻结引用/幂等键；回执不等于批准，取消或换代不能写回旧记录。
# 函数用途: 发布一张精确私人审批卡，没有可用通道时保留原 TUI/Web 消费路径。
def publish_im_tool_approval(agent, handle) -> None:
    from .agent_tool_approval import _record_matches, _transition_path

    config = capability_config_for_agent(agent)
    if config is not None and not config.im_tool_approvals_enabled:
        return
    service = getattr(agent, "delivery_service", None)
    if service is None or not handle.scope_valid():
        return
    channel, actor = approval_recipient(agent, handle.scope)
    with locked_json_path(_transition_path(handle.path)):
        report = read_json_object_report(handle.path, context="tool_approval.im.prepare")
        row = report.payload
        if report.load_error or not _record_matches(row, handle.scope, handle.request) or row.get("status") != "pending":
            return
        transport = row.get("im_control")
        if not isinstance(transport, dict):
            expiry = approval_deadline(handle.request, row.get("expires_at"))
            payload = {"kind": "tool_approval", "owner": asdict(owner_identity_from_config(agent.config)),
                       "channel": channel, "actor_id": actor, "scope": _scope_identity(handle.scope),
                       "permission_id": handle.request.permission_id, "expires_at": expiry}
            reference = sign_interaction_reference(agent.home_paths.owner_home_dir, payload)
            transport = {"schema": "im_tool_approval.v1", "reference": reference, "channel": channel,
                         "actor_id": actor, "expires_at": expiry, "delivery_status": "dispatching"}
            write_json_file_atomic(handle.path, {**row, "im_control": transport})
        if transport.get("delivery_status") == "sent":
            return
    card, summary = approval_card(transport["reference"], handle.request, handle.scope)
    context = DeliveryContext(channel=channel, target=actor, mode="reply", thread_id=handle.scope.thread_id,
        task_id=handle.scope.run_id, idempotency_key=f"tool-approval:{handle.scope.run_id}:{handle.request.permission_id}")
    receipt = service.deliver_control_card(context, card, summary=summary)
    with locked_json_path(_transition_path(handle.path)):
        report = read_json_object_report(handle.path, context="tool_approval.im.receipt")
        if report.load_error or not _record_matches(report.payload, handle.scope, handle.request) or not handle.scope_valid():
            return
        if report.payload.get("im_control") != transport:
            return
        transport = {**transport, "delivery_status": "sent" if receipt.ok else "failed",
                     "message_id": receipt.message_id, "chat_id": receipt.chat_id, "error_code": receipt.error_code}
        write_json_file_atomic(handle.path, {**report.payload, "im_control": transport})


# LLM: 一张已发出的有效私人卡就是本请求的等待能力，不续租全 root；expiry 和原执行检查仍能终止等待。
# 函数用途: 保留没有在线 TUI 的精确审批，避免 1.5 秒发现窗把刚送出的卡删除。
def im_approval_wait_live(row, now: float) -> bool:
    transport = row.get("im_control") if isinstance(row, dict) else None
    try:
        expiry = float(transport.get("expires_at") or 0) if isinstance(transport, dict) else 0
    except (ValueError, TypeError):
        return False
    return bool(isinstance(transport, dict) and transport.get("schema") == "im_tool_approval.v1"
        and transport.get("delivery_status") == "sent" and transport.get("message_id") and transport.get("chat_id")
        and math.isfinite(expiry) and expiry > now)


# LLM: 引用已在初始化 owner 前验证 MAC/actor/expiry；此处核对真实卡片、原 scope/请求以及本次明确决定，再写原批准记录。
# 函数用途: 将原发起人在私人卡上的选择提交给原主子调用，不接受转发卡、他人点击或旧执行代次。
# LLM: 可选C提交器只包原scope/transition/audit的真实RMW；Count0省略参数保原调用，MAC不能替代实际writer/claim/attempt。
# 函数用途: 已发原私卡决定仍交原审批门，C本机实际Agent可显式非阻塞提交。
def resolve_im_tool_approval(agent, payload: dict, *, reference: str, message_id: str, chat_id: str, decision: str,
                             commit_transition=None, blocking=True) -> dict:
    from .agent_tool_approval import _approval_path, _approval_request, resolve_agent_tool_approval
    from .tool_approval_scope import tool_approval_scope

    if decision == 'cancel':
        return _cancel_approval_card(agent,payload,reference,message_id,chat_id)
    scope = tool_approval_scope(agent, str((payload.get("scope") or {}).get("run_id") or ""))
    if not scope.active or _scope_identity(scope) != payload.get("scope") or decision not in {"approved", "denied"}:
        raise ValueError("原执行已改变或决定无效")
    path = _approval_path(agent, scope, str(payload.get("permission_id") or ""))
    report = read_json_object_report(path, context="tool_approval.im.decide")
    row = report.payload or {}
    transport = row.get("im_control") or {}
    if (report.load_error or transport.get("reference") != reference or transport.get("message_id") != message_id
            or transport.get("chat_id") != chat_id or not im_approval_wait_live(row, time.time())):
        raise ValueError("卡片不是原私人请求或已失效")
    request = _approval_request(row.get("request"))
    return resolve_agent_tool_approval(agent, run_id=scope.run_id, request_value=request,
        decision_value=ToolApprovalDecision(request.permission_id, decision), interaction_actor=payload,
        **({} if commit_transition is None and blocking else {'commit_transition': commit_transition, 'blocking': blocking}))


# LLM: 前台共用原审批账和等待线程；actual typed未知先穿透，普通失败仍unavailable，不以卡片授执行权。
# 函数用途: 将普通用户/群聊的前台工具确认接到同一私人卡和执行代次检查。
def request_foreground_im_permission(agent, value, *, cancellation_token=None):
    from types import SimpleNamespace

    from .agent_tool_approval import AgentToolApprovalSinkMixin
    from .tool_approval_scope import tool_approval_scope
    current = getattr(agent, '_current_run_params', None)
    task_id = str(getattr(current, 'task_id', '') or '')
    try:
        scope = tool_approval_scope(agent, task_id)
        sink = SimpleNamespace(agent=agent, task_id=task_id, thread_id=scope.thread_id, approval_interactive=True)
        return AgentToolApprovalSinkMixin.request_permission(sink, value, cancellation_token=cancellation_token)
    except (ValueError, RuntimeError, OSError) as error:
        from ..gateway_parts.private_approval_control import raise_private_approval_transport_error
        raise_private_approval_transport_error(error)
        from ..contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
        request = ToolApprovalRequest.from_mapping(value)
        return ToolApprovalDecision(request.permission_id, 'unavailable').to_dict()


# LLM: Count0继续原Agent审计/Store/阻塞锁；取消不读取当前新代次授予权限，真实C使用同一领域leaf。
# 函数用途: 保留原私人审批取消入口及虚调用资源。
def _cancel_approval_card(agent, payload, reference, message_id, chat_id):
    from functools import partial

    from ..user_space.interaction_actor_audit import record_interaction_actor

    audit = partial(record_interaction_actor, agent, payload, decision='cancel', actor=payload['actor_id'])
    return cancel_im_tool_approval(agent.conversation_store, payload,
                                  {'reference': reference, 'message_id': message_id, 'chat_id': chat_id}, audit=audit)


# LLM: 仅退休已MAC验证的原等待，过期/失活仍可关闭；Store不授active执行权，C必须提供同来源短RMW，审计失败保真实部分事实。
# 函数用途: 在原transition锁内幂等取消准确卡片，不停止任务、不覆写其它决定或清P3。
def cancel_im_tool_approval(store, payload, body, *, audit, commit_transition=None, blocking=True):
    from ..common.opaque_id import validate_opaque_id
    from .agent_tool_approval import approval_path_for_store
    from .tool_approval_scope import ToolApprovalScope

    identity = dict(payload['scope'])
    validate_opaque_id(identity['run_id'], kind='run_id')
    validate_opaque_id(identity['root_task_id'], kind='root_task_id')
    scope = ToolApprovalScope(**identity, agent_name='', active=False)
    path = approval_path_for_store(store, scope, str(payload.get('permission_id') or ''))

    # LLM: 来源窗口必须包住原transition锁和行锁；不能仅先检查后在C门外提交，默认Count0仍直接执行。
    # 函数用途: 原锁内复核卡片并完成这一次取消。
    def cancel():
        return _cancel_approval_at_path(path, scope, body, audit, blocking)

    return cancel() if commit_transition is None else commit_transition(cancel)


# LLM: 完整原schema/scope/request及运输ID在同一锁内核对；首次审计后才写取消，已取消原facts重放不再审计。
# 函数用途: 更新唯一原审批行，所有其它任务、输入和卡片保持原状态。
def _cancel_approval_at_path(path, scope, body, audit, blocking):
    from ..user_space.interaction_actor_audit import interaction_actor_text
    from .agent_tool_approval import _approval_request, _record_matches, _transition_path

    with locked_json_path(_transition_path(path), **({} if blocking else {"blocking": False})):
        report = read_json_object_report(path, context='tool_approval.im.cancel')
        row = report.payload or {}
        if report.load_error:
            raise ValueError('审批记录不可读')
        if not row:
            return {'ok': False, 'error_code': 'IM_INTERACTION_ALREADY_HANDLED'}
        request = _approval_request(row.get('request'))
        transport = row.get('im_control') or {}
        if (not _record_matches(row, scope, request) or transport.get('reference') != body['reference']
                or transport.get('message_id') != body['message_id'] or transport.get('chat_id') != body['chat_id']):
            raise ValueError('卡片不是原审批请求')
        if row.get('status') != 'pending':
            if (row.get('decision') or {}).get('decision') != 'cancelled':
                return {'ok': False, 'error_code': 'IM_INTERACTION_ALREADY_HANDLED'}
            facts = row.get('decision_by')
            if not facts:
                return {'ok': False, 'error_code': 'IM_INTERACTION_ALREADY_HANDLED'}
        else:
            facts = audit()
            row = {**row, 'status': 'decided', 'decision': ToolApprovalDecision(request.permission_id, 'cancelled').to_dict(), 'decision_by': facts}
            write_json_file_atomic(path, row, **({} if blocking else {"blocking": False}))
    return {'ok': True, 'decision': 'cancelled', 'clicked_by': facts,
            'message': interaction_actor_text(facts) + ' 已取消这次确认；任务和其他卡片保持原状态。'}
