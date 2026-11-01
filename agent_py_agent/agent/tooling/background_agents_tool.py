"""原生/插件F3共用的窄适配器；认证actor及能力闸仍由真实宿主入口提供。

本模块不注册第二工具执行器、不运行模型。W13/W14把已验证请求交到这里；W4的通用任务/
执行树查询不在本适配器猜造workflow、运行关系或可见性。
"""
from __future__ import annotations

from ..conversation.background_agents import BackgroundAgentService, cancel_business_execution
from ..conversation.session_messaging import (
    control_business_subagent,
    queue_owned_background_message,
)
from ..plugin_host.business_action_specs import business_action_spec
from ..plugin_host.contracts import HostAPIError
from ..plugin_host.schemas import validate_object


def execute_background_action(host, action: str, request: dict, *, actor,
                              idempotency_key: str = "", expected_revision: int | None = None) -> dict:
    spec = business_action_spec(action)
    params = validate_object(request, spec.params_schema)
    if spec.requires_idempotency and not idempotency_key:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    if spec.expected_revision_required and type(expected_revision) is not int:
        raise ValueError("EXPECTED_REVISION_REQUIRED")
    service = BackgroundAgentService(host)
    service._actor(actor)
    if action == "controllers.get":
        result = service.get_controller(actor=actor)
    elif action == "controllers.ensure":
        result = service.ensure_controller(actor=actor)
    elif action in {"background_agents.create", "tasks.submit", "tasks.assign", "tasks.follow_up"}:
        method = {"background_agents.create": service.create, "tasks.submit": service.submit,
                  "tasks.assign": service.assign, "tasks.follow_up": service.follow_up}[action]
        result = method(params, actor=actor, idempotency_key=idempotency_key)
    elif action == "background_agents.list":
        result = service.list(params, actor=actor)
    elif action == "background_agents.get":
        result = service.get(params["agent_id"], actor=actor)
    elif action == "background_agents.update":
        result = service.update(params["agent_id"], {key: value for key, value in params.items() if key != "agent_id"},
            actor=actor, expected_revision=expected_revision, idempotency_key=idempotency_key)
    elif action in {"background_agents.suspend", "background_agents.resume", "background_agents.restore", "background_agents.retire"}:
        result = service.transition(params["agent_id"], action.rsplit(".", 1)[1], actor=actor,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
            disposition=params.get("disposition", "drain"), handoff_agent_id=params.get("handoff_agent_id"))
    elif action == "background_agents.model.get":
        result = service.get_model(params["agent_id"], actor=actor)
    elif action == "background_agents.model.set":
        result = service.set_model(params["agent_id"], params["model_profile_id"], actor=actor,
                                   expected_revision=expected_revision, idempotency_key=idempotency_key)
    elif action == "background_agents.messages.send":
        target = service._thread(params["agent_id"], actor)
        if params.get("reply_to_message_id"):
            message, errors = host.conversation_store.messages.by_id_report(target.thread_id, params["reply_to_message_id"])
            if errors or message is None or message.thread_id != target.thread_id:
                raise ValueError("MESSAGE_REFERENCE_MISMATCH")
        result = queue_owned_background_message(host, actor=actor, target=target, text=params["text"],
            input_kind=params["input_kind"], operation_key=service._operation_key(actor, idempotency_key))
    elif action == "tasks.cancel":
        result = service.cancel_task(params["task_id"], actor=actor, include_descendants=params["include_descendants"])
    elif action in {"subagents.messages.send", "subagents.cancel"}:
        result = control_business_subagent(host, service, action, params, actor=actor, idempotency_key=idempotency_key)
    elif action == "executions.cancel":
        result = cancel_business_execution(host, service, params, actor=actor)
    else:
        raise HostAPIError("UNSUPPORTED_ACTION")
    return spec.validate_result(result)
