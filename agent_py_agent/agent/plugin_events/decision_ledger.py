# LLM: B5 第 5 段：把每次收紧征询的决定写进 owner 权威 runtime_events（event_type="plugin_gate.decided"），
#   挂在调用所在的 attempt / run 上。只写结构化决定事实，绝不写插件 message 原文或工具参数值——
#   设计第 9 节只允许原因码，B6 的展示与"无法审批"计数都按这些键读。
#   幂等前提：本模块没有幂等键，重复写入只靠"唯一调用点"这个结构保证——tool_runtime_ledger 每次工具调用只调一次
#   append_plugin_gate_decision（每门一条）。以后若新增第二个调用点（重试、补偿或另一条执行路径），必须先引入幂等键
#   （例如按 call_id + gate_id + activation_id 去重），否则同一次征询会被记两遍，B6 的最近 10 条与计数会失真。
# 模块用途: 为工具执行器提供唯一的插件门决定账本写入点，不在征询分支或审批链里另写一份。
from __future__ import annotations

from dataclasses import dataclass

from ..tooling.runtime_contracts import ToolCall

# 设计第 9 节的字段白名单，必须与 B6（runtime_db/repository.py 的 _PLUGIN_GATE_DECISION_FIELDS）逐字一致；
# 多一个或少一个都会让 B6 的投影读不到或读到意外字段。改这里要同步 B6 与设计稿。
PLUGIN_GATE_DECISION_FIELDS = (
    "plugin_id", "version", "activation_id", "gate_id", "tool", "call_id", "operation_id", "args_hash",
    "actor", "outcome", "verdict", "reason_code", "latency_ms", "host_status", "final_status",
)

#: 设计第 9 节 outcome 的封闭集合；不在集合内的值一律按 error 收紧，避免意外 outcome 流出到展示层。
PLUGIN_GATE_OUTCOMES = ("ok", "timeout", "error", "malformed", "unavailable", "revoked")

#: 决定事实在 ActionDecision.evidence 里使用的键；执行器与归档投影共用，避免两边各拼一份字符串。
PLUGIN_GATE_DECISION_EVIDENCE_KEY = "plugin_gate_decisions"

#: 事件类型常量；B6 的查询方法与 `plugin_commands` 展示都按它过滤。
PLUGIN_GATE_DECIDED_EVENT = "plugin_gate.decided"


# LLM: 只接受白名单内的键并做类型收紧（字符串、非负整数），任何多余键直接丢弃——插件回复里的
#   额外字段没有控制权，也不能借这里的 payload 越出 B6 的字段白名单。
# 函数用途: 把一条门决定投影成设计第 9 节规定的精确字段集合。
def plugin_gate_decision_payload(decision: dict) -> dict:
    payload: dict[str, object] = {}
    for key in PLUGIN_GATE_DECISION_FIELDS:
        value = decision.get(key)
        if key == "latency_ms":
            payload[key] = max(0, int(value or 0)) if isinstance(value, (int, float)) else 0
        else:
            payload[key] = str(value) if value is not None else ""
    if payload["outcome"] not in PLUGIN_GATE_OUTCOMES:
        payload["outcome"] = "error"
    return payload


# LLM: 唯一归档形状是tool_result_envelope中的白名单决定键，不读人工顶层条目或handler任意字段；无门返回空。
# 函数用途: 从归档记录里取出本次插件的门决定事实列表。
def plugin_gate_decisions_from_archive(archive_record: dict) -> tuple[dict, ...]:
    envelope = archive_record.get("tool_result_envelope")
    if not isinstance(envelope, dict):
        return ()
    value = envelope.get(PLUGIN_GATE_DECISION_EVIDENCE_KEY)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


# LLM: 事件落库需要的事件行字段随 repo 一起传，避免函数参数膨胀；字段全部来自宿主已归一的调用事实。
# 类用途: 保存一条 plugin_gate.decided 要挂的 attempt/run 身份。
@dataclass(frozen=True)
class GateEventTarget:
    repo: object
    attempt_id: str
    agent_run_id: str
    task_run_id: str


# LLM: 未接入权威库（纯单测 / 无 home 上下文）时静默跳过，与 B4 的 tool_completed 写入同一策略；
#   写账失败不能打断工具循环。只接受已由 plugin_gate_decision_payload 收紧的字段。
# 函数用途: 把一条门决定追加进 owner 权威 runtime_events，挂到本次调用的 attempt / run。
def append_plugin_gate_decision(target: GateEventTarget, payload: dict) -> None:
    repo = target.repo
    if repo is None or not hasattr(repo, "append_event"):
        return
    if not target.attempt_id or not target.agent_run_id:
        return
    try:
        repo.append_event(
            event_type=PLUGIN_GATE_DECIDED_EVENT,
            attempt_id=target.attempt_id,
            agent_run_id=target.agent_run_id,
            task_run_id=target.task_run_id,
            payload=plugin_gate_decision_payload(payload),
        )
    except Exception:  # noqa: BLE001 控制面遥测失败绝不能崩掉真正的工具调用
        return


# LLM: 门决定必须引用本次调用的权威身份（call_id/operation_id/args_hash），不能从插件回复或模型参数推导；
#   工具名与发起者也只取宿主已归一化的调用事实。
# 函数用途: 用宿主调用事实补齐一条门决定里与门无关的公共字段。
def gate_decision_common_facts(call: ToolCall, actor: str, host_status: str, final_status: str) -> dict:
    return {
        "tool": str(call.tool_name or ""),
        "call_id": str(call.call_id or ""),
        "operation_id": str(call.operation_id or ""),
        "args_hash": str(call.args_hash or ""),
        "actor": str(actor or ""),
        "host_status": str(host_status or ""),
        "final_status": str(final_status or ""),
    }
