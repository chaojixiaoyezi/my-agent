# LLM: Subagent typed protocol envelopes for result and fan-out events.
# 模块用途: 定义子代理结果、层级创建结果和结构化 refs 展开逻辑。

from __future__ import annotations

"""Subagent protocol envelope layer."""

from dataclasses import asdict, dataclass, field
from typing import Any

from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    ArtifactRef,
    EvidenceRef,
    PathRef,
    RunScope,
    _default_operation_id,
    _dict_list,
    _dict_or_empty,
    _now_iso,
    _string_list,
)


# LLM: SubagentResultEnvelope is the typed form of a child run final report.
# 类用途: 保存子代理状态、真实工具、证据 ref 和产物 ref；summary 只用于展示。
@dataclass(frozen=True)
class SubagentResultEnvelope:
    result_id: str
    run_id: str
    status: str
    summary: str = ""
    actual_tools: list[str] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    path_refs: list[PathRef] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    failure_type: str = ""
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "subagent_result"
    created_at: str = field(default_factory=_now_iso)
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: __post_init__ assigns a replay-safe operation key for child result ingestion.
    # 函数用途: 旧子代理结果没有 operation_id 时，按 result_id 自动补齐。
    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.result_id))

    # LLM: to_dict serializes nested refs explicitly for storage and parent review.
    # 函数用途: 把子代理结果 envelope 转成 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        payload["artifact_refs"] = [item.to_dict() for item in self.artifact_refs]
        payload["evidence_refs"] = [item.to_dict() for item in self.evidence_refs]
        payload["path_refs"] = [item.to_dict() for item in self.path_refs]
        return payload

    # LLM: from_dict restores typed child results without trusting display summaries.
    # 函数用途: 从 JSON 字典恢复 SubagentResultEnvelope。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SubagentResultEnvelope:
        return cls(
            result_id=str(payload.get("result_id") or payload.get("id") or ""),
            run_id=str(payload.get("run_id") or ""),
            status=str(payload.get("status") or ""),
            summary=str(payload.get("summary") or ""),
            actual_tools=_string_list(payload.get("actual_tools")),
            artifact_refs=[ArtifactRef.from_dict(item) for item in _dict_list(payload.get("artifact_refs"))],
            evidence_refs=[EvidenceRef.from_dict(item) for item in _dict_list(payload.get("evidence_refs"))],
            path_refs=[PathRef.from_dict(item) for item in _dict_list(payload.get("path_refs"))],
            tests=_dict_list(payload.get("tests")),
            next_actions=_string_list(payload.get("next_actions")),
            blocked_reason=str(payload.get("blocked_reason") or ""),
            failure_type=str(payload.get("failure_type") or ""),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "subagent_result"),
            created_at=str(payload.get("created_at") or _now_iso()),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


# LLM: SubagentScheduleEnvelope is the typed handoff for every subagent fan-out layer.
# 类用途: 统一 create_subagents 和 schedule_child_subagents 的创建结果，让多层 refs 不靠自然语言传递。
@dataclass(frozen=True)
class SubagentScheduleEnvelope:
    schedule_id: str
    tool: str
    created_run_ids: list[str] = field(default_factory=list)
    parent_run_id: str = ""
    root_id: str = ""
    planned_count: int = 0
    dry_run: bool = False
    blocked: bool = False
    reason: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "subagent_schedule"
    created_at: str = field(default_factory=_now_iso)
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: __post_init__ gives every fan-out event a stable idempotency key.
    # 函数用途: 让 create/schedule 子代理事件能被恢复逻辑去重。
    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.schedule_id))

    # LLM: to_dict keeps subagent fan-out results machine-readable for parent recovery.
    # 函数用途: 把 SubagentScheduleEnvelope 转成 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        return payload

    # LLM: from_dict restores schedule refs without parsing tool output prose.
    # 函数用途: 从 JSON 字典恢复 SubagentScheduleEnvelope。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SubagentScheduleEnvelope:
        return cls(
            schedule_id=str(payload.get("schedule_id") or payload.get("id") or ""),
            tool=str(payload.get("tool") or ""),
            created_run_ids=_string_list(payload.get("created_run_ids")),
            parent_run_id=str(payload.get("parent_run_id") or ""),
            root_id=str(payload.get("root_id") or ""),
            planned_count=int(payload.get("planned_count") or 0),
            dry_run=bool(payload.get("dry_run")),
            blocked=bool(payload.get("blocked")),
            reason=str(payload.get("reason") or ""),
            items=_dict_list(payload.get("items")),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "subagent_schedule"),
            created_at=str(payload.get("created_at") or _now_iso()),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


# LLM: SubagentDispatchEnvelope is the typed form of dispatch_subagents tool output.
# 类用途: 保存 dispatch 一轮调度的 refs、可操作 run ids 和摘要；父级不需要解析自然语言记录。
@dataclass(frozen=True)
class SubagentDispatchEnvelope:
    dispatch_id: str
    tool: str = "dispatch_subagents"
    dry_run: bool = False
    summary: dict[str, Any] = field(default_factory=dict)
    actionable_run_ids: list[str] = field(default_factory=list)
    recovery_run_ids: list[str] = field(default_factory=list)
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


# LLM: path_refs_from_subagent_refs flattens structured artifact/evidence refs for resolvers.
# 函数用途: 从 ArtifactRef/EvidenceRef 生成去重 PathRef；不解析 summary，避免自然语言误触发读取。
def path_refs_from_subagent_refs(
    *,
    artifact_refs: list[ArtifactRef],
    evidence_refs: list[EvidenceRef],
    owner_run_id: str = "",
) -> list[PathRef]:
    refs: list[PathRef] = []
    seen: set[str] = set()

    # LLM: add closes over owner/seen so path-ref creation avoids long parameter lists.
    # 函数用途: 追加非空路径引用并按 path 去重，保持读取顺序稳定。
    def add(path: str, kind: str, source: str, owner: str = "") -> None:
        clean = str(path or "").strip()
        if not clean or clean in seen:
            return
        seen.add(clean)
        refs.append(PathRef(path=clean, kind=kind or "file", owner_run_id=owner, source=source))

    for item in artifact_refs:
        add(item.path, item.kind, "artifact_ref", item.owner_run_id or owner_run_id)
    for item in evidence_refs:
        for ref in item.artifact_refs:
            add(ref, "artifact", f"evidence:{item.evidence_id}:artifact_refs", owner_run_id)
        for ref in item.evidence_refs:
            add(ref, "evidence", f"evidence:{item.evidence_id}:evidence_refs", owner_run_id)
    return refs


# LLM: subagent_schedule_envelope_from_payload normalizes top-level and nested spawn outputs.
# 函数用途: 把 create_subagents/schedule_child_subagents 的旧 JSON 响应转成统一 SubagentScheduleEnvelope。
def subagent_schedule_envelope_from_payload(
    payload: dict[str, Any],
    *,
    tool: str,
    scope: RunScope | None = None,
) -> SubagentScheduleEnvelope:
    created_run_ids = _schedule_created_run_ids(payload)
    parent_run_id = str(payload.get("parent_run_id") or "")
    root_id = str(payload.get("root_id") or "")
    return SubagentScheduleEnvelope(
        schedule_id=_schedule_envelope_id(tool, created_run_ids, parent_run_id, root_id),
        tool=tool,
        created_run_ids=created_run_ids,
        parent_run_id=parent_run_id,
        root_id=root_id,
        planned_count=int(payload.get("planned_count") or len(created_run_ids)),
        dry_run=bool(payload.get("dry_run")),
        blocked=bool(payload.get("blocked")),
        reason=str(payload.get("reason") or ""),
        items=_dict_list(payload.get("items") or payload.get("tasks")),
        scope=scope or RunScope(run_id=parent_run_id, root_task_id=root_id),
        reserved={"source": "subagent_orchestration_tool"},
    )


# LLM: subagent_dispatch_envelope_from_payload normalizes dispatch_subagents output for parent agents.
# 函数用途: 从 dispatch payload 提取摘要、可继续调度 run ids 和报告 refs，避免父级从 message 字段猜。
def subagent_dispatch_envelope_from_payload(
    payload: dict[str, Any],
    *,
    scope: RunScope | None = None,
) -> SubagentDispatchEnvelope:
    actionable = _dispatch_actionable_run_ids(payload)
    recovery = _dispatch_recovery_run_ids(payload)
    return SubagentDispatchEnvelope(
        dispatch_id=_dispatch_envelope_id(payload),
        dry_run=bool(payload.get("dry_run")),
        summary=_dispatch_summary(payload.get("summary")),
        actionable_run_ids=actionable,
        recovery_run_ids=recovery,
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


# LLM: _schedule_created_run_ids accepts both create_subagents ids and child scheduling ids.
# 函数用途: 从旧 payload 的 created_run_ids/ids/tasks 里提取创建出来的 run id。
def _schedule_created_run_ids(payload: dict[str, Any]) -> list[str]:
    ids = _string_list(payload.get("created_run_ids") or payload.get("ids"))
    if ids:
        return ids
    return [
        str(item.get("id") or item.get("run_id"))
        for item in _dict_list(payload.get("tasks") or payload.get("items"))
        if str(item.get("id") or item.get("run_id") or "").strip()
    ]


# LLM: _schedule_envelope_id gives schedule events stable ids without reading task files.
# 函数用途: 基于工具名、父级/root 和第一个子 run id 生成可追踪 schedule_id。
def _schedule_envelope_id(tool: str, run_ids: list[str], parent_run_id: str, root_id: str) -> str:
    anchor = run_ids[0] if run_ids else parent_run_id or root_id or "unknown"
    return f"{tool}:{anchor}"
