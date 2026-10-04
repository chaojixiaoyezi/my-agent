# LLM: B5 执行器决定阶段适配，只收紧 ActionPolicy 原裁决，不执行 handler、改参数或替用户授权。
# 模块用途: 将共用池征询的结构化事实接入唯一工具执行器，后续审批和账本沿原链扩展。
from __future__ import annotations

import json
from dataclasses import replace

from ..contracts.tool_approval import ToolApprovalRequest
from ..plugin_events.decision_ledger import (
    PLUGIN_GATE_DECISION_EVIDENCE_KEY,
    gate_decision_common_facts,
)
from ..plugin_events.tool_gate import GateCall, GateReview, PluginToolGate, clean_gate_message
from .action_policy import ActionDecision


# LLM: 来源只读宿主请求；host deny/host_command零征询；精确批准只取原write_boundary，决定证据取全部review而非仅ask集合。
# 函数用途: 在审批前以最严插件结果收紧宿主裁决，并保留原沙箱、资源与授权证据。
def tighten_plugin_decision(request: object, call: object, host: ActionDecision) -> ActionDecision:
    if host.status == "deny" or request.call_origin == "host_command" or request.plugin_gate_reviewer is None:
        return host
    context = request.trusted_run_context or {}
    actor = "subagent" if request.runtime_snapshot.owner_type == "subagent" else "main"
    if context.get("actor") == "decision":
        actor = "decision"
    gate_call = GateCall(call, host.resolved_effect, actor, context.get("interactive") is True,
                         _approved_gate_references(request, call))
    try:
        reviews = request.plugin_gate_reviewer(gate_call)
    except Exception:  # noqa: BLE001 读安装/取得共用池失败宁严，不把异常或路径回显给模型
        evidence = {**host.evidence, "plugin_gate_unavailable": True,
                    "plugin_gate_ref": _gate_reference(call, [])}
        return _confirmation_decision(host, context, evidence)
    merged = PluginToolGate.merge(host.status, reviews)
    requirements = [_requirement(item) for item in merged.requirements]
    evidence = {**host.evidence, "plugin_requirements": requirements}
    approved = [_requirement(item) for item in reviews if item.approval_applied]
    # B5 第 5 段：把每门的决定事实（含 outcome/延迟/主因）附在原 evidence 上；执行器归档后由
    # persist_tool_runtime_ledger 写 plugin_gate.decided。这里只放结构化决定，message 永不进入。
    # 精确批准命中的门（approval_applied=True）没有真的征询插件，绝不能当作决定记账（第 4 段契约）。
    # 只有最终仍需插件确认的ask才投影无法审批；混合门已有deny或宿主ask无插件要求时，保留真实最严终态。
    if reviews:
        final_status = ("PLUGIN_GATE_APPROVAL_UNAVAILABLE" if merged.status == "ask" and merged.requirements
                        and context.get("interactive") is False else merged.status)
        evidence[PLUGIN_GATE_DECISION_EVIDENCE_KEY] = _decision_ledger_entries(gate_call, host.status, final_status, reviews)
    if merged.primary is None:
        if approved and merged.status == "allow":
            evidence.update({"approval_applied": True, "plugin_gate_ref": _gate_reference(call, approved)})
        return replace(host, evidence=evidence) if reviews else host
    if merged.status == "ask":
        evidence["plugin_gate_ref"] = _gate_reference(call, requirements)
        return _confirmation_decision(host, context, evidence)
    reason = "PLUGIN_GATE_DENIED" if merged.status == "deny" else "APPROVAL_REQUIRED"
    return replace(host, status=merged.status, reason_codes=(reason, *host.reason_codes), evidence=evidence)


# LLM: 只读原宿主批准列表；write_boundary键名、类型或形状改变时回空引用，安全退化为“不跳门、重新征询”，不是批准或另建兜底账。
# 函数用途: 从本次write_boundary取得可以继续精确核验的插件引用。
def _approved_gate_references(request: object, call: object) -> tuple[str, ...]:
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    actions = boundary.get("approved_actions")
    if not isinstance(actions, (list, tuple)):
        return ()
    return tuple(ref for action in actions if (ref := _approved_gate_reference(action, call)))


# LLM: 外层原批准行必须APPROVED、有approval_id且与tool/run/operation/key/hash五字段全等；缺字段不默认为批准。
#   外层校验用户批准的宿主执行归属（tool/run），内层解码插件引用再比operation/call/key/hash四字段及门目标；
#   call_id属于插件引用而不在原批准行，tool/run属于宿主批准行而不交插件决定，两层有意分开且都必须成立。
# 函数用途: 拒绝未批准、跨调用或坏类型的宿主批准行。
def _approved_gate_reference(action: object, call: object) -> str:
    if not isinstance(action, dict) or action.get("status") != "APPROVED":
        return ""
    if not isinstance(action.get("approval_id"), str) or not action["approval_id"]:
        return ""
    if not all(action.get(key) == getattr(call, key) for key in
               ("tool_name", "run_id", "operation_id", "idempotency_key", "args_hash")):
        return ""
    ref = action.get("plugin_gate_ref")
    return ref if isinstance(ref, str) else ""


# LLM: 每门一条，字段与设计第 9 节对齐；取全部真实review而非仅待确认requirements，deny/allow/撤销也不能丢。
#   host_status取宿主原裁决，final_status由调用方投影不可交互终态；不读message，不在此写库。
#   approval_applied=True 的门是宿主按精确批准跳过的、没有真的征询插件（第 4 段契约），
#   绝不写决定账本——否则 B6 的"最近 10 次收紧决定"会混进从未发生的征询。
# 函数用途: 为一次调用生成所有门的决定账本条目（多门每门一条），排除精确批准跳过门。
def _decision_ledger_entries(gate_call: GateCall, host_status: str, final_status: str, reviews: tuple[GateReview, ...]) -> list[dict]:
    common = gate_decision_common_facts(gate_call.call, gate_call.actor, host_status, final_status)
    return [{**common, "plugin_id": item.target.plugin_id, "version": item.target.version,
             "activation_id": item.target.activation_id, "gate_id": item.target.declaration.id,
             "outcome": item.outcome, "verdict": item.reply.verdict, "reason_code": item.reply.reason_code,
             "latency_ms": item.latency_ms}
            for item in reviews if not item.approval_applied]


# LLM: 四调用字段加每门激活/门身份序列化为稳定 JSON；消息不入引用，原 permission_id 算法不受影响。
# 函数用途: 给原审批链生成本次插件确认的精确身份摘要，不创建批准账。
def _gate_reference(call: object, requirements: list[dict]) -> str:
    identity = {key: getattr(call, key) for key in ("operation_id", "call_id", "idempotency_key", "args_hash")}
    identity["gates"] = [{key: value for key, value in item.items() if key != "message"} for item in requirements]
    return json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# LLM: 无交互是宿主结构化事实，不从模型参数猜；旧调用方缺字段仍保留待审批，生产入口必须显式提供。
# 函数用途: 将插件要求确认变成 ask 或明确的无人审批拒绝，保持宿主限制。
def _confirmation_decision(host: ActionDecision, context: dict, evidence: dict) -> ActionDecision:
    unavailable = context.get("interactive") is False
    code = "PLUGIN_GATE_APPROVAL_UNAVAILABLE" if unavailable else "APPROVAL_REQUIRED"
    return replace(host, status="deny" if unavailable else "ask", reason_codes=(code, *host.reason_codes), evidence=evidence)


# LLM: 旧 request 已算 grant_key/permission_id，引用只能后附；仅本次/拒绝，消息复用 decode_reply 同一清洗。
# 函数用途: 将执行器生成的引用和安全前缀附到原审批请求，不写缓存或长期授权。
def plugin_gate_approval_request(execution: object, request: ToolApprovalRequest) -> ToolApprovalRequest:
    reference = execution.decision.evidence.get("plugin_gate_ref")
    if not reference:
        return request
    once = next(item for item in request.options if item["decision"] == "approved")
    deny = next(item for item in request.options if item["decision"] == "denied")
    return replace(request, binding={**request.binding, "plugin_gate_ref": reference},
                   description=_confirmation_prefix(execution.decision) + request.description,
                   options=({**once, "label": "仅本次"}, deny))


# LLM: 不从展示消息产生控制；插件编号、原因和消息均作有界清洗，前缀放最前保证 IM 前200字可见。
# 函数用途: 为插件确认生成与协议解码同源的单行前缀。
def _confirmation_prefix(decision: ActionDecision) -> str:
    requirements = decision.evidence.get("plugin_requirements") or []
    if not requirements:
        return "[插件门要求确认：PLUGIN_GATE_UNAVAILABLE] "
    item = requirements[0]
    plugin = clean_gate_message(item.get("plugin_id"))[:40]
    reason = clean_gate_message(item.get("reason_code"))[:40]
    message = clean_gate_message(item.get("message"))
    return f"[插件 {plugin} 要求确认：{reason} {message}] "


# LLM: 拒绝/取消沿原错误码与宿主决定；仅把同源清洗的插件原因放最前，不解析反馈、消息或改变授权。
# 函数用途: 让原拒绝回执说清是哪个插件要求确认。
def plugin_gate_rejection_message(execution: object, message: str) -> str:
    return _confirmation_prefix(execution.decision) + message if execution.decision.evidence.get("plugin_gate_ref") else message


# LLM: consumer 不存在、失联或返回 unavailable 都不产生批准；只替换原执行回执，handler 和操作账不进入。
# 函数用途: 让模型明确看到插件要求确认但当前无法审批的失败。
def plugin_gate_approval_unavailable(execution: object) -> object:
    from .runtime_contracts import ToolFailureFacts, ToolResult

    if not execution.decision.evidence.get("plugin_gate_ref"):
        return execution
    decision = replace(execution.decision, status="deny", reason_codes=("PLUGIN_GATE_APPROVAL_UNAVAILABLE",))
    result = ToolResult.failed(execution.call, _confirmation_prefix(decision) + "这里无法审批，所以没有执行。",
        error_code="PLUGIN_GATE_APPROVAL_UNAVAILABLE", failure_stage="authorization",
        facts=ToolFailureFacts(metadata={**execution.result.metadata, "action_decision": decision.to_dict()}))
    return replace(execution, decision=decision, result=result)


# LLM: 仅展示当次经过严格协议校验的消息；激活身份供后续精确审批引用，不从模型输出恢复授权。
# 函数用途: 将要求确认的单门事实投影给原审批链。
def _requirement(review: GateReview) -> dict:
    return {"plugin_id": review.target.plugin_id, "version": review.target.version,
            "activation_id": review.target.activation_id, "gate_id": review.target.declaration.id,
            "reason_code": review.reply.reason_code, "message": review.reply.message}
