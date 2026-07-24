
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...common.value_parsing import dedupe_strings, string_list
from ...conversation.models import normalize_guidance_target_type
from ...runtime_errors import runtime_error_report
from ...subagents.kernel import SubagentKernelQuery
from ...tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..runner.context import current_subagent_run_id

if TYPE_CHECKING:
    from ...core import SimpleAgent

_TOOL_NAME = "send_guidance"


@dataclass(frozen=True)
class GuidanceToolRequest:
    target_type: str
    target_id: str
    message: str
    sender: str
    priority: str
    delivery: str
    metadata: dict[str, Any]


class SendGuidanceTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_send_guidance_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        requests = _guidance_requests(self.agent, params)
        if isinstance(requests, ToolExecutionResult):
            return requests
        entries = [
            self.agent.conversation_store.append_guidance(
                {
                    "target_type": request.target_type,
                    "target_id": request.target_id,
                    "message": request.message,
                    "sender": request.sender,
                    "priority": request.priority,
                    "delivery": request.delivery,
                    "metadata": request.metadata,
                }
            )
            for request in requests
        ]
        entry = entries[0]
        payload = {
            "ok": True,
            "guidance_id": entry.guidance_id,
            "target": {"type": entry.target_type, "id": entry.target_id},
            "targets": [{"type": item.target_type, "id": item.target_id, "guidance_id": item.guidance_id} for item in entries],
            "delivery": entry.delivery,
            "message": "已写入软提示；目标代理下一轮会读取，不会被强制停止或硬阻断。",
        }
        return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


def build_send_guidance_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="orchestration",
        effect="mutating",
        idempotency_scope="operation",
        description="给正在运行的主代理、子代理、孙代理、会话或任务追加一条软提示；只影响下一轮判断，不推进、不验收、不硬卡。",
        use_cases=[
            "用户在任务运行中补一句要求、纠偏或提醒",
            "父代理想提醒某个下级换来源、补证据、先写草稿或尽快汇报",
            "需要给一个 thread/task/case 留下一条后续醒来可见的提示",
        ],
        avoid_when=["需要真正推进、重跑或恢复子代理时继续用 dispatch_subagents；第一次派工继续用 create_subagents"],
        keywords=["补充提示", "引导", "纠偏", "催一下", "steer", "guidance", "message"],
        parameters={
            "target": "目标对象，必须写 {type,id}；type 只接受 agent_run/thread/task/case",
            "target_type": "不使用 target 时可直接写 target_type，只接受 agent_run/thread/task/case",
            "target_id": "不使用 target 时可直接写 target_id",
            "run_ids": "多个 agent_run 目标；适合给一批已知子代理同一句补充提示",
            "target_scope": "批量目标；children 表示 root_id 的直接孩子，descendants 表示 root_id 的整棵下级",
            "root_id": "target_scope 的根 run_id",
            "message": "要给目标下一轮看的补充提示，必须是具体可执行的人话",
            "priority": "软优先级文本，默认 normal",
            "delivery": "投递方式提示，默认 next_turn",
        },
        parameter_schema={
            "target": {"type": "object"},
            "target_type": {"type": "string", "enum": ["agent_run", "thread", "task", "case"]},
            "target_id": {"type": "string"},
            "run_ids": {"type": "array", "items": {"type": "string"}},
            "target_scope": {"type": "string", "enum": ["children", "descendants"]},
            "root_id": {"type": "string"},
            "message": {"type": "string"},
            "priority": {"type": "string"},
            "delivery": {"type": "string"},
        },
        required_parameters=["message"],
        examples=[
            '{"tool":"send_guidance","target":{"type":"agent_run","id":"child-1"},"message":"换一个数据来源核对，不要重复查同一个页面。"}',
            '{"tool":"send_guidance","target_scope":"children","root_id":"parent-1","message":"按用户补充要求补证据，完成后继续原任务。"}',
            '{"tool":"send_guidance","target":{"type":"thread","id":"thread-1"},"message":"用户补充：最终报告里要把未命中的来源也写清楚。"}',
        ],
    )


def _guidance_requests(agent: object, params: dict[str, object]) -> list[GuidanceToolRequest] | ToolExecutionResult:
    message = str(params.get("message") or "").strip()
    if not message:
        return _guidance_error("缺少 message；send_guidance 只记录具体补充提示。", error_code="TOOL_PARAMETER_REQUIRED")
    run_ids = _target_run_ids(agent, params)
    if isinstance(run_ids, ToolExecutionResult):
        return run_ids
    if run_ids:
        sender = str(params.get("sender") or current_subagent_run_id(agent) or "main_agent").strip()
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        return [
            GuidanceToolRequest(
                target_type="agent_run",
                target_id=run_id,
                message=message,
                sender=sender,
                priority=str(params.get("priority") or "normal").strip() or "normal",
                delivery=str(params.get("delivery") or "next_turn").strip() or "next_turn",
                metadata={**metadata, "target_scope": str(params.get("target_scope") or "").strip()},
            )
            for run_id in run_ids
        ]
    request = _guidance_request(agent, params)
    if isinstance(request, ToolExecutionResult):
        return request
    return [request]


def _guidance_request(agent: object, params: dict[str, object]) -> GuidanceToolRequest | ToolExecutionResult:
    target_type, target_id = _target_from_params(params)
    message = str(params.get("message") or "").strip()
    if not target_type or not target_id:
        return _guidance_error("缺少 target；请提供 target:{type,id}，或 target_type + target_id。", error_code="TOOL_PARAMETER_REQUIRED")
    if not message:
        return _guidance_error("缺少 message；send_guidance 只记录具体补充提示。", error_code="TOOL_PARAMETER_REQUIRED")
    sender = str(params.get("sender") or current_subagent_run_id(agent) or "main_agent").strip()
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    return GuidanceToolRequest(
        target_type=target_type,
        target_id=target_id,
        message=message,
        sender=sender,
        priority=str(params.get("priority") or "normal").strip() or "normal",
        delivery=str(params.get("delivery") or "next_turn").strip() or "next_turn",
        metadata=metadata,
    )


def _target_run_ids(agent: object, params: dict[str, object]) -> list[str] | ToolExecutionResult:
    explicit = string_list(params.get("run_ids"))
    if explicit:
        return dedupe_strings(explicit)
    scope = str(params.get("target_scope") or "").strip()
    if not scope:
        return []
    resolved = _target_scope_rows(agent, params, scope)
    if isinstance(resolved, ToolExecutionResult):
        return resolved
    rows, anchor = resolved
    if scope == "children":
        return _direct_child_run_ids(rows, anchor)
    if scope == "descendants":
        return _descendant_run_ids(rows, anchor)
    return _guidance_error("未知 target_scope；请使用 children 或 descendants，或直接传 run_ids。")


def _target_scope_rows(agent: object, params: dict[str, object], scope: str) -> tuple[list[object], str] | ToolExecutionResult:
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(type(manager), "kernel_snapshot", None)):
        return _guidance_error("target_scope 需要可读取的子代理树；请先用显式 run_ids，或刷新代理树后再发。")
    anchor = (
        str(params.get("root_id") or "").strip()
        or current_subagent_run_id(agent)
        or str(getattr(agent, "_main_agent_run_id", "") or "").strip()
    )
    query = SubagentKernelQuery(run_id=anchor, root_id=str(params.get("root_id") or "").strip(), scope="own_subtree" if anchor else "root_tree")
    try:
        rows = list(manager.kernel_snapshot(query).runs)
    except Exception as exc:
        payload = {
            "ok": False,
            "error": "target_scope_resolution_failed",
            "message": "target_scope 解析失败；软提示没有投递，避免误发给父代理或错误目标。",
            "target_scope": scope,
            "load_error": runtime_error_report(exc, context="send_guidance.target_scope"),
        }
        # kernel_snapshot 运行时异常 → 可恢复执行失败(可重试/改用显式 run_ids)，
        # 不是参数不合法；无码会兜底成 UNKNOWN_ERROR(retryable=False)误导模型放弃。
        return ToolExecutionResult(
            _TOOL_NAME, False, json.dumps(payload, ensure_ascii=False, indent=2), error_code="TOOL_EXECUTION_FAILED"
        )
    return rows, anchor


def _direct_child_run_ids(rows: list[object], anchor: str) -> list[str]:
    return dedupe_strings(
        [
            str(getattr(row, "run_id", "") or "")
            for row in rows
            if str(getattr(row, "run_id", "") or "")
            and (
                str(getattr(row, "parent_run_id", "") or "") == anchor
                or (not anchor and not str(getattr(row, "parent_run_id", "") or ""))
            )
        ]
    )


def _descendant_run_ids(rows: list[object], anchor: str) -> list[str]:
    return dedupe_strings(
        [
            str(getattr(row, "run_id", "") or "")
            for row in rows
            if str(getattr(row, "run_id", "") or "") and str(getattr(row, "run_id", "") or "") != anchor
        ]
    )


def _target_from_params(params: dict[str, object]) -> tuple[str, str]:
    target = params.get("target")
    if isinstance(target, dict):
        target_type = normalize_guidance_target_type(target.get("type"))
        target_id = str(target.get("id") or "").strip()
        if target_type and target_id:
            return target_type, target_id
    return (
        normalize_guidance_target_type(params.get("target_type")),
        str(params.get("target_id") or "").strip(),
    )


def _guidance_error(message: str, *, error_code: str = "TOOL_INVALID_ARGUMENTS") -> ToolExecutionResult:
    # 带准确分类码：缺必填参数(message/target)给 TOOL_PARAMETER_REQUIRED，取值非法给
    # TOOL_INVALID_ARGUMENTS；无码会兜底成 UNKNOWN_ERROR(retryable=False)误导模型放弃。
    payload = {"ok": False, "error": "invalid_guidance_request", "message": message}
    return ToolExecutionResult(_TOOL_NAME, False, json.dumps(payload, ensure_ascii=False, indent=2), error_code=error_code)
