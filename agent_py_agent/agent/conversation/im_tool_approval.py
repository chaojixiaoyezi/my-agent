# LLM: IM 是原审批账的运输与决定入口，不拥有第二份批准状态；冻结 actor 和真实卡片身份，决定仍走原 scope/request/claim/attempt。
# 模块用途: 将后台主子审批送到原发起人私聊，并把签名引用核对回原等待记录。
from __future__ import annotations

import math
import time
from dataclasses import asdict, fields

from ..capability.runtime_config_reload import capability_config_for_agent
from ..common.json_io import locked_json_path, read_json_object_report, write_json_file_atomic
from ..contracts.tool_approval import ToolApprovalDecision
from ..conversation.channels import (
    DeliveryContext,
    project_host_paths_for_channel,
    project_user_reply,
)
from ..user_space.interaction_refs import sign_interaction_reference
from ..user_space.owner_resolver import owner_identity_from_config
from .execution_threads import EXECUTION_ORIGIN_KEY, execution_origin_thread

_CARD_WAIT_SECONDS = 1800.0  # 卡片本次批准有效期 30 分钟；任务/claim/attempt 失效时提前结束。


# LLM: scope 原 dataclass 的 compare 字段是唯一身份口径，名称和展示 active 不得悄悄变成新授权键。
# 函数用途: 生成签名与回调共用的审批身份，修改显示名不影响原调用，实际停止仍单独核对。
def _scope_identity(scope) -> dict:
    return {field.name: getattr(scope, field.name) for field in fields(scope) if field.compare}


# LLM: 原根任务与 host origin 是来源事实；没有精确 actor 的群不猜最新发言者。本机管理员只可取自己唯一绑定身份。
# 函数用途: 为本次主子审批解析原发起人的私人接收者，不能指定任意群成员。
def approval_recipient(agent, scope) -> tuple[str, str]:
    store = agent.conversation_store
    link = store.tasks.load(scope.root_task_id)
    thread_id = str(getattr(link, "thread_id", "") or "")
    if not thread_id:
        root = agent.subagents.load(scope.root_task_id)
        thread_id = str((root.attributes or {}).get("conversation_thread_id") or "")
    thread = store.threads.require(thread_id or scope.thread_id)
    reference = thread.metadata.get(EXECUTION_ORIGIN_KEY)
    if reference is not None:
        execution_origin_thread(store.threads.require, thread)
        binding = reference["binding"]
        if binding["channel"] != "feishu" or not binding["actor_id"]:
            raise ValueError("审批来源没有可用的私人通道")
        return "feishu", binding["actor_id"]
    owner = owner_identity_from_config(agent.config)
    identities = {(binding.channel, binding.channel_user_id) for binding in thread.channel_bindings
                  if owner.owner_kind == "user" and binding.channel == owner.provider
                  and binding.channel_user_id == owner.owner_id}
    if len(identities) == 1:
        return next(iter(identities))
    from ..capability.channel_message_target import resolve_message_target

    channel, actor, _service, error = resolve_message_target(agent, "feishu")
    if error or not actor:
        raise ValueError("审批来源没有唯一私人接收者")
    return channel, actor


# LLM: 显示正文只经原公开投影，按钮只带宿主签名引用及固定决定；未包含原参数或秘密。
# 函数用途: 构造一张本次调用的两选项审批卡，点击后仍需验证当前执行与私人卡片来源。
def approval_card(reference: str, request, scope) -> tuple[dict, str]:
    summary = project_host_paths_for_channel(project_user_reply(request.description).content, "feishu")[:240]
    text = f"{scope.agent_name}申请执行 {request.tool_name}\n{summary}\n本次确认 30 分钟内有效。"
    buttons = [{"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": color,
                "value": {"kind": "my_agent_tool_approval", "reference": reference, "decision": decision}}
               for label, color, decision in (("允许本次", "primary", "approved"), ("拒绝", "default", "denied"))]
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
            expiry = time.time() + _CARD_WAIT_SECONDS
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
def resolve_im_tool_approval(agent, payload: dict, *, reference: str, message_id: str, chat_id: str, decision: str) -> dict:
    from .agent_tool_approval import _approval_path, _approval_request, resolve_agent_tool_approval
    from .tool_approval_scope import tool_approval_scope

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
        decision_value=ToolApprovalDecision(request.permission_id, decision))
