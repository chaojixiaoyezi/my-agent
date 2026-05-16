# LLM: Dispatch-specific subagent envelopes stay separate so protocol files remain small.
# 模块用途: 定义 dispatch_subagents 的 typed envelope 和 payload 归一化 helper。

from __future__ import annotations

"""Typed dispatch envelope for subagent orchestration."""

from dataclasses import asdict, dataclass, field
from typing import Any

from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    RunScope,
    _default_operation_id,
    _dict_list,
    _dict_or_empty,
    _now_iso,
    _string_list,
)


# LLM: SubagentDispatchEnvelope is the typed form of dispatch_subagents tool output.
# 类用途: 保存 dispatch 一轮调度的 refs、状态闸门和可操作 run ids；父级不需要解析自然语言记录。
@dataclass(frozen=True)
class SubagentDispatchEnvelope:
    dispatch_id: str
    tool: str = "dispatch_subagents"
    dry_run: bool = False
    summary: dict[str, Any] = field(default_factory=dict)
    actionable_run_ids: list[str] = field(default_factory=list)
    recovery_run_ids: list[str] = field(default_factory=list)
    blocking_run_ids: list[str] = field(default_factory=list)
    unfinished_run_ids: list[str] = field(default_factory=list)
    pending_artifact_refs: list[str] = field(default_factory=list)
    pending_evidence_refs: list[str] = field(default_factory=list)
    deliverable_artifact_refs: list[str] = field(default_factory=list)
    deliverable_evidence_refs: list[str] = field(default_factory=list)
    completion_status: dict[str, Any] = field(default_factory=dict)
    current_turn_run_state: dict[str, Any] = field(default_factory=dict)
    next_action: str = ""
    must_not_report_done: bool = False
    dispatch_json: str = ""
    dispatch_md: str = ""
    record_count: int = 0
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "subagent_dispatch"
    created_at: str = field(default_factory=_now_iso)
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: __post_init__ makes dispatch rounds addressable for resume and audit.
    # 函数用途: 自动补齐 dispatch operation_id，避免父级从报告文本里猜是哪一轮调度。
    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.dispatch_id))

    # LLM: to_dict serializes dispatch refs without expanding runner record bodies.
    # 函数用途: 把 dispatch envelope 转成 JSON 字典，供工具输出、日志和接管流程读取。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        return payload

    # LLM: from_dict restores dispatch refs for tests and future action routers.
    # 函数用途: 从 JSON 字典恢复 SubagentDispatchEnvelope。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SubagentDispatchEnvelope:
        return cls(
            dispatch_id=str(payload.get("dispatch_id") or payload.get("id") or ""),
            tool=str(payload.get("tool") or "dispatch_subagents"),
            dry_run=bool(payload.get("dry_run")),
            summary=dict(payload.get("summary") or {}),
            actionable_run_ids=_string_list(payload.get("actionable_run_ids")),
            recovery_run_ids=_string_list(payload.get("recovery_run_ids")),
            blocking_run_ids=_string_list(payload.get("blocking_run_ids")),
            unfinished_run_ids=_string_list(payload.get("unfinished_run_ids")),
            pending_artifact_refs=_string_list(payload.get("pending_artifact_refs")),
            pending_evidence_refs=_string_list(payload.get("pending_evidence_refs")),
            deliverable_artifact_refs=_string_list(payload.get("deliverable_artifact_refs")),
            deliverable_evidence_refs=_string_list(payload.get("deliverable_evidence_refs")),
            completion_status=_dict_or_empty(payload.get("completion_status")),
            current_turn_run_state=_dict_or_empty(payload.get("current_turn_run_state")),
            next_action=str(payload.get("next_action") or ""),
            must_not_report_done=bool(payload.get("must_not_report_done")),
            dispatch_json=str(payload.get("dispatch_json") or ""),
            dispatch_md=str(payload.get("dispatch_md") or ""),
            record_count=int(payload.get("record_count") or 0),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "subagent_dispatch"),
            created_at=str(payload.get("created_at") or _now_iso()),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


# LLM: subagent_dispatch_envelope_from_payload normalizes dispatch_subagents output for parent agents.
# 函数用途: 从 dispatch payload 提取状态闸门、可继续调度 run ids 和报告 refs，避免父级从 message 字段猜。
def subagent_dispatch_envelope_from_payload(
    payload: dict[str, Any],
    *,
    scope: RunScope | None = None,
) -> SubagentDispatchEnvelope:
    return SubagentDispatchEnvelope(
        dispatch_id=_dispatch_envelope_id(payload),
        dry_run=bool(payload.get("dry_run")),
        summary=_dispatch_summary(payload.get("summary")),
        actionable_run_ids=_dispatch_actionable_run_ids(payload),
        recovery_run_ids=_dispatch_recovery_run_ids(payload),
        blocking_run_ids=_string_list(payload.get("blocking_run_ids")),
        unfinished_run_ids=_string_list(payload.get("unfinished_run_ids")),
        pending_artifact_refs=_string_list(payload.get("pending_artifact_refs")),
        pending_evidence_refs=_string_list(payload.get("pending_evidence_refs")),
        deliverable_artifact_refs=_string_list(payload.get("deliverable_artifact_refs")),
        deliverable_evidence_refs=_string_list(payload.get("deliverable_evidence_refs")),
        completion_status=_dict_or_empty(payload.get("completion_status")),
        current_turn_run_state=_dict_or_empty(payload.get("current_turn_run_state")),
        next_action=str(payload.get("next_action") or ""),
        must_not_report_done=bool(payload.get("must_not_report_done")),
        dispatch_json=str(payload.get("dispatch_json") or ""),
        dispatch_md=str(payload.get("dispatch_md") or ""),
        record_count=len(_dict_list(payload.get("records"))),
        scope=scope or RunScope(),
        reserved={"source": "dispatch_subagents_tool"},
    )


# LLM: _dispatch_actionable_run_ids reads direct-child hints before falling back to records.
# 函数用途: 提取下一轮可继续推进的 run ids，只使用结构化字段。
def _dispatch_actionable_run_ids(payload: dict[str, Any]) -> list[str]:
    direct = payload.get("direct_children") if isinstance(payload.get("direct_children"), dict) else {}
    ids = _string_list((direct or {}).get("actionable_run_ids"))
    if ids:
        return ids
    return _record_run_ids(payload)


# LLM: _dispatch_summary keeps legacy string summaries from breaking typed dispatch output.
# 函数用途: 把旧 mock/旧报告里的 summary 字符串安全包进字典，真实 dict 原样复制。
def _dispatch_summary(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    return {"text": str(value)}


# LLM: _dispatch_recovery_run_ids extracts failed child ids from direct progress payloads.
# 函数用途: 提取需要恢复/接管的 run ids，供父级优先处理失败分支。
def _dispatch_recovery_run_ids(payload: dict[str, Any]) -> list[str]:
    direct = payload.get("direct_children") if isinstance(payload.get("direct_children"), dict) else {}
    return _string_list((direct or {}).get("recovery_run_ids"))


# LLM: _record_run_ids gives dispatch envelopes a bounded fallback when no direct-child block exists.
# 函数用途: 从 records 列表提取 run_id，保留顺序并去重。
def _record_run_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in _dict_list(payload.get("records")):
        run_id = str(item.get("run_id") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


# LLM: _dispatch_envelope_id creates stable dispatch ids from report refs or first run id.
# 函数用途: 生成一轮 dispatch envelope 的可追踪 id。
def _dispatch_envelope_id(payload: dict[str, Any]) -> str:
    anchor = str(payload.get("dispatch_json") or "").strip()
    if not anchor:
        ids = _dispatch_actionable_run_ids(payload) or _record_run_ids(payload)
        anchor = ids[0] if ids else "unknown"
    return f"dispatch_subagents:{anchor}"
