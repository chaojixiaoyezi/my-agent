# LLM: 本模块准备原input receipt的数据；准备不等于入队，不授予C/W或执行权限。
# 模块用途: 固定插件消息的幂等身份及原回执载荷。
from dataclasses import asdict, replace
from hashlib import sha256

from ..plugin_host.contracts import decode_call
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json, thaw
from . import input_delivery_service as inputs
from .input_delivery_service import GatewayInputPreparation
from .plugin_request_source import require_plugin_source


# LLM: 仅生成宿主规范输入数据，调用者仍须通过真实来源/线程/工厂检查。
# 函数用途: 构造原GatewayInputPreparation。
def build_message_preparation(call, binding):
    envelope = {"schema": call.schema, "action": call.action, "payload": thaw(call.payload),
                "idempotency_key": call.idempotency_key}
    if call.as_user_id is not None:
        envelope["as_user"] = {"user_id": call.as_user_id}
    call = decode_call(canonical_json(envelope).encode("utf-8"))
    if call.action != "conversations.messages.send":
        raise HostAPIError("INVALID_SCHEMA")
    payload = thaw(call.payload)
    if payload["text"].lstrip().startswith("/"):
        raise HostAPIError("CONTROL_COMMAND_NOT_ALLOWED", 403)
    owner = asdict(binding.effective_owner)
    scope = {"schema": "plugin_message_input.v1", "installation_ref": binding.installation_ref,
             "effective_owner": owner, "action": call.action}
    request_id = "plugin-msg-" + sha256(canonical_json({**scope, "key": call.idempotency_key}).encode("utf-8")).hexdigest()
    digest = sha256(canonical_json({**scope, "payload": payload}).encode("utf-8")).hexdigest()
    prepared = {"id": request_id, "kind": "ask", "goal": payload["text"], "source": "plugin",
        "host_request_owner.v1": owner,
        "metadata": {"origin": "plugin", "client_input_digest": digest,
            "plugin_provenance": {"installation_ref": binding.installation_ref,
                "package_sha256": binding.package_sha256, "activation_id": binding.activation_id,
                "grant_revision": binding.grant_revision, "thread_id": payload["thread_id"],
                "kind": "chat", "tool_policy": payload["tool_policy"]}}}
    return GatewayInputPreparation(request_id, digest, request_id, request_id, prepared)


# LLM: 原T内重核来源并复用原receipt/queue；准备回调只在首次输入时执行，冲突不覆盖原输入。
# 函数用途: 通过原函数式输入服务幂等受理插件消息。
def submit_message(authorized, context, execution, paths, prepare_request):
    require_plugin_source(authorized)
    request = build_message_preparation(authorized.call, authorized.binding)

    # LLM: 宿主必须把零工具或C策略落实到原执行接缝，缺接缝不能裸排到main；回调不接受客户端身份。
    # 函数用途: 在原首次准备窗口内核当前来源并准备执行绑定。
    def prepare(payload):
        require_plugin_source(authorized)
        prepared = prepare_request(authorized, context, execution, payload)
        if not isinstance(prepared, dict):
            raise HostAPIError("HOST_NOT_READY", 503)
        return prepared

    request = replace(request, prepare_request=prepare)
    with inputs.gateway_input_transition(paths, request.request_id):
        require_plugin_source(authorized)
        try:
            receipt, _ = inputs.load_or_prepare_gateway_input_request_locked(paths, request)
        except ValueError as error:
            if str(error) == "gateway client message id was reused with different input":
                raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409) from None
            raise
        require_plugin_source(authorized)
        if receipt.state in {"pending", "queued"}:
            inputs.queue_gateway_input_locked(paths, receipt)
    return request.request_id


# LLM: 控制输入签入原op回执；op锁→原T，锁内重核当前scope/revision，仅标记受理，不伪造资源停止。
# 函数用途: 幂等取消一条同安装同owner的准确原请求。
def cancel_message(domain, authorized, context, paths, store):
    import time

    from ..plugin_host.contracts import DomainResult
    from ..plugin_host.requests import _read_execution, _read_receipt
    from . import control_operation_service as controls
    from .control_service import GatewayControlScope
    from .io import write_json_file_atomic

    payload = thaw(authorized.call.payload)
    scope_id = sha256(canonical_json([authorized.binding.installation_ref, asdict(context.identity),
        authorized.call.action, authorized.call.idempotency_key]).encode()).hexdigest()
    scope = GatewayControlScope(user_id=context.owner_home.owner_id, channel="plugin",
        conversation_id=authorized.binding.installation_ref, resolved_owner=context.identity,
        metadata={"message_id": scope_id, "expected_turn_id": payload["request_id"],
                  "target_control_message_id": sha256(canonical_json(payload).encode()).hexdigest()})
    text = "/stop"
    operation_id, fingerprint = controls.gateway_control_operation_identity(scope, text)
    with controls.gateway_control_operation_transition(paths, operation_id):
        require_plugin_source(authorized)
        prior = controls.read_gateway_control_operation(paths, operation_id)
        if prior is not None:
            if prior.input_digest != fingerprint:
                raise HostAPIError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409)
            if prior.state == "completed":
                return DomainResult(202, prior.result["plugin_request"])
            if prior.state != "prepared":
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
        with inputs.gateway_input_transition(paths, payload["request_id"]):
            require_plugin_source(authorized)
            facts = domain.facts(authorized, context, paths, store)
            if facts["terminal"] is not None:
                return DomainResult(200, facts)
            if facts["revision"] != payload["expected_revision"]:
                raise HostAPIError("REVISION_CONFLICT", 409)
            receipt, _ = _read_receipt(paths, payload["request_id"], context.identity, authorized.binding.installation_ref)
            row, folder = _read_execution(paths, receipt)
            if folder not in {"processing", "pending"}:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
            if folder == "processing" and domain.ports.cancel_request is None:
                raise HostAPIError("HOST_NOT_READY", 503)
            operation = prior or controls._new_control_operation_receipt(scope, "stop", text, operation_id, fingerprint)
            op_path = controls.gateway_control_operation_path(paths, operation_id)
            write_json_file_atomic(op_path, replace(operation, state="executing", updated_at=time.time()).to_dict())
            frozen = dict(row)
            row.update(cancel_requested=True, cancel_requested_at=time.time(), control_status="stopping", turn_phase="closing")
            require_plugin_source(authorized)
            write_json_file_atomic(paths.processing / (payload["request_id"] + ".json") if folder == "processing"
                                   else paths.inbox / (payload["request_id"] + ".json"), row)
            facts = domain.facts(authorized, context, paths, store)
        if folder == "processing":
            domain.ports.cancel_request(authorized, context, frozen)
        operation = replace(operation, state="completed", updated_at=time.time(),
            result={"kind": "stop", "ok": True, "message": "Cancellation accepted; resource stop unverified.",
                    "request_id": payload["request_id"], "delivery_status": "accepted", "plugin_request": facts})
        write_json_file_atomic(op_path, operation.to_dict())
        return DomainResult(202, facts)
