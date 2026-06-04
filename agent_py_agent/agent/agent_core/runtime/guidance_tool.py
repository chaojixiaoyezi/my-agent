
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...common.value_parsing import dedupe_strings, string_list
from ...conversation.store_guidance import normalize_guidance_target_type
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
        requires_idempotency=True,
        description="给正在运行的主代理、子代理、孙代理、会话或任务追加一条软提示；只影响下一轮判断，不推进、不验收、不硬卡。",
        use_cases=[
            "用户在任务运行中补一句要求、纠偏或提醒",
            "父代理想提醒某个下级换来源、补证据、先写草稿或尽快汇报",
            "需要给一个 thread/task/case 留下一条后续醒来可见的提示",
        ],
        avoid_when=["需要真正推进、重跑或恢复子代理时继续用 dispatch_subagents；第一次派工继续用 create_subagents"],
        keywords=["补充提示", "引导", "纠偏", "催一下", "steer", "guidance", "message"],
        parameters={
            "target": "目标对象，可写 {type,id}；type 常见值 agent_run/thread/task/case，未知类型也会按原名保存",
            "target_type": "不使用 target 时可直接写 target_type",
            "target_id": "不使用 target 时可直接写 target_id",
            "run_id": "agent_run 目标别名",
            "run_ids": "多个 agent_run 目标；适合给一批已知子代理同一句补充提示",
            "target_scope": "批量目标；children/direct_children 表示某 run 的直接孩子，descendants/subtree 表示某 run 的整棵下级",
            "thread_id": "thread 目标别名",
            "task_id": "task 目标别名",
            "case_id": "case 目标别名",
            "message": "要给目标下一轮看的补充提示，必须是具体可执行的人话",
            "priority": "软优先级文本，默认 normal",
            "delivery": "投递方式提示，默认 next_turn",
        },
        examples=[
            '{"tool":"send_guidance","target":{"type":"agent_run","id":"child-1"},"message":"换一个数据来源核对，不要重复查同一个页面。"}',
            '{"tool":"send_guidance","target_scope":"children","run_id":"parent-1","message":"按用户补充要求补证据，完成后继续原任务。"}',
            '{"tool":"send_guidance","thread_id":"thread-1","message":"用户补充：最终报告里要把未命中的来源也写清楚。"}',
        ],
    )


def _guidance_requests(agent: object, params: dict[str, object]) -> list[GuidanceToolRequest] | ToolExecutionResult:
    message = str(params.get("message") or "").strip()
    if not message:
        return _guidance_error("缺少 message；send_guidance 只记录具体补充提示。")
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
        return _guidance_error("缺少 target；请提供 target:{type,id}、run_id、thread_id、task_id 或 case_id。")
    if not message:
        return _guidance_error("缺少 message；send_guidance 只记录具体补充提示。")
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
    explicit = string_list(params.get("run_ids") or params.get("target_run_ids") or params.get("agent_run_ids"))
    if explicit:
        return dedupe_strings(explicit)
    scope = str(params.get("target_scope") or "").strip().lower()
    if not scope:
        return []
    resolved = _target_scope_rows(agent, params, scope)
    if isinstance(resolved, ToolExecutionResult):
        return resolved
    rows, anchor = resolved
    if scope in {"children", "direct_children", "child", "direct"}:
        return _direct_child_run_ids(rows, anchor)
    if scope in {"descendants", "subtree", "all_children", "all_descendants"}:
        return _descendant_run_ids(rows, anchor)
    return _guidance_error("未知 target_scope；请使用 children/direct_children 或 descendants/subtree，或直接传 run_ids。")


def _target_scope_rows(agent: object, params: dict[str, object], scope: str) -> tuple[list[object], str] | ToolExecutionResult:
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(type(manager), "kernel_snapshot", None)):
        return _guidance_error("target_scope 需要可读取的子代理树；请先用显式 run_ids，或刷新代理树后再发。")
    anchor = (
        str(params.get("run_id") or params.get("root_id") or "").strip()
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
        return ToolExecutionResult(_TOOL_NAME, False, json.dumps(payload, ensure_ascii=False, indent=2))
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
        target_type = normalize_guidance_target_type(target.get("type") or target.get("target_type"))
        target_id = str(target.get("id") or target.get("target_id") or "").strip()
        if target_type and target_id:
            return target_type, target_id
    alias_pairs = (
        ("run_id", "agent_run"),
        ("agent_run_id", "agent_run"),
        ("thread_id", "thread"),
        ("task_id", "task"),
        ("case_id", "case"),
    )
    for key, target_type in alias_pairs:
        value = str(params.get(key) or "").strip()
        if value:
            return target_type, value
    return (
        normalize_guidance_target_type(params.get("target_type") or params.get("type")),
        str(params.get("target_id") or params.get("id") or "").strip(),
    )


def _guidance_error(message: str) -> ToolExecutionResult:
    payload = {"ok": False, "error": "invalid_guidance_request", "message": message}
    return ToolExecutionResult(_TOOL_NAME, False, json.dumps(payload, ensure_ascii=False, indent=2))
