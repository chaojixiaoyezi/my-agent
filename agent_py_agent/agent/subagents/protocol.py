# LLM: Subagent protocol objects are the external task handoff contract, separate from manager internals.
# 模块用途: 定义 TaskAddress、TaskEnvelope 和协议校验结果，减少父子代理靠自然语言猜地址、工具和验收。

from __future__ import annotations

"""Subagent protocol contracts.

给人看的解释：
`CreateRunParams` 适合代码内部调用，但父子代理之间更需要稳定“线协议”。
这里的 TaskAddress / TaskEnvelope 是第一版协议包：先把地址、工具、写入、验收和上下文 refs
整理成机器字段，后续 dispatch、recovery、QA 都可以读同一种结构。
"""

from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field

from .models import SubAgentTask


# LLM: ProtocolIssue is the shared machine-readable error shape for address/envelope/tool contract checks.
# 类用途: 表示协议边界发现的结构化问题，让父级知道是哪类错误、哪个字段出了问题。
@dataclass(frozen=True)
class ProtocolIssue:

    kind: str
    code: str
    field: str = ""
    message: str = ""
    severity: str = "error"
    reserved: dict[str, object] = dataclass_field(default_factory=dict)

    # LLM: to_dict keeps issue serialization stable across reports and events.
    # 函数用途: 把协议问题转成 JSON 友好字段，方便写入事件日志或报告。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: ProtocolValidationReport carries all protocol issues without throwing in normal control flow.
# 类用途: 汇总 TaskEnvelope 或 ToolPreflight 的校验结果，调用方可决定阻断、降级或请求能力。
@dataclass(frozen=True)
class ProtocolValidationReport:

    ok: bool
    issues: list[ProtocolIssue] = dataclass_field(default_factory=list)
    reserved: dict[str, object] = dataclass_field(default_factory=dict)

    # LLM: to_dict keeps validation reports machine-readable.
    # 函数用途: 转成 JSON 友好结构，保留所有 issue 而不是只返回第一条错误。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "reserved": dict(self.reserved),
        }


# LLM: TaskAddress is the canonical address for one subagent run in a task tree.
# 类用途: 固定 run/root/parent/depth/lineage/attempt/workspace 字段，避免恢复和消息路由靠字符串猜。
@dataclass(frozen=True)
class TaskAddress:

    schema_version: str
    run_id: str
    root_id: str
    parent_id: str = ""
    depth: int = 0
    lineage: list[str] = dataclass_field(default_factory=list)
    attempt_id: str = ""
    workspace_ref: str = ""
    reserved: dict[str, object] = dataclass_field(default_factory=dict)

    # LLM: to_dict is the stable wire shape for task addresses.
    # 函数用途: 输出给 envelope、kernel、recovery 和 message payload 复用。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: TaskEnvelope is the first stable handoff package between parent and child agents.
# 类用途: 保存地址、目标、角色、工具合同、写入合同、验收合同和上下文引用。
@dataclass(frozen=True)
class TaskEnvelope:

    schema_version: str
    address: TaskAddress
    goal: str
    role: str
    display_name: str = ""
    plan: list[str] = dataclass_field(default_factory=list)
    tool_contract: dict[str, object] = dataclass_field(default_factory=dict)
    write_contract: dict[str, object] = dataclass_field(default_factory=dict)
    acceptance: dict[str, object] = dataclass_field(default_factory=dict)
    context_refs: dict[str, object] = dataclass_field(default_factory=dict)
    audit: dict[str, object] = dataclass_field(default_factory=dict)
    reserved: dict[str, object] = dataclass_field(default_factory=dict)

    # LLM: to_dict keeps nested address typed while exposing a plain JSON payload.
    # 函数用途: 生成外部协议包 JSON 形状，调用方不用知道内部 dataclass。
    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["address"] = self.address.to_dict()
        return payload


# LLM: build_task_address creates a canonical address from persisted task facts.
# 函数用途: 从 SubAgentTask 和可选全量任务列表生成 lineage/workspace 引用。
def build_task_address(task: SubAgentTask, *, all_tasks: list[SubAgentTask] | None = None) -> TaskAddress:
    run_id = _task_text(task, "id")
    return TaskAddress(
        schema_version="subagent_task_address.v1",
        run_id=run_id,
        root_id=_task_text(task, "root_id") or run_id,
        parent_id=_task_text(task, "parent_id"),
        depth=_task_int(task, "depth"),
        lineage=_lineage_for_task(task, all_tasks or []),
        attempt_id=_task_text(task, "runner_active_attempt_id") or _attempt_from_task(task),
        workspace_ref=_workspace_ref(task),
        reserved={"session_id": _task_text(task, "subagent_session_id"), "thread_id": _task_text(task, "agent_thread_id")},
    )


# LLM: build_task_envelope maps SubAgentTask into the protocol package consumed by recovery and QA.
# 函数用途: 将目标、工具、写入、验收和上下文 refs 统一打包，避免下游读散字段。
def build_task_envelope(task: SubAgentTask, *, all_tasks: list[SubAgentTask] | None = None) -> TaskEnvelope:
    return TaskEnvelope(
        schema_version="subagent_task_envelope.v1",
        address=build_task_address(task, all_tasks=all_tasks),
        goal=_task_text(task, "goal"),
        role=_task_text(task, "role"),
        display_name=_task_text(task, "agent_name"),
        plan=_task_list(task, "plan"),
        tool_contract=_tool_contract(task),
        write_contract=_write_contract(task),
        acceptance=_acceptance_contract(task),
        context_refs=_context_refs(task),
        audit={"created_at": _task_float(task, "created_at"), "updated_at": _task_float(task, "updated_at"), "contract_version": "v1"},
    )


# LLM: validate_task_envelope checks the minimal handoff fields before a child starts work.
# 函数用途: 返回结构化协议错误，避免缺目标/验收条件时继续靠默认值跑歪。
def validate_task_envelope(envelope: TaskEnvelope) -> ProtocolValidationReport:
    issues: list[ProtocolIssue] = []
    if not envelope.goal.strip():
        issues.append(_protocol_issue("missing_goal", "goal", "TaskEnvelope.goal is required."))
    if not list(envelope.acceptance.get("checks") or []):
        issues.append(
            _protocol_issue(
                "missing_acceptance_checks",
                "acceptance.checks",
                "TaskEnvelope.acceptance.checks must include at least one parent-visible check.",
            )
        )
    return ProtocolValidationReport(ok=not issues, issues=issues)


# LLM: task_envelope_dict is a compact helper for report payloads that should not expose dataclasses.
# 函数用途: 给 recovery、acceptance、board 等报告直接生成 JSON 友好的 envelope。
def task_envelope_dict(task: SubAgentTask, *, all_tasks: list[SubAgentTask] | None = None) -> dict[str, object]:
    return build_task_envelope(task, all_tasks=all_tasks).to_dict()


# LLM: _protocol_issue keeps protocol error naming consistent.
# 函数用途: 构造 ProtocolError 类型 issue，方便后续事件日志统一筛选。
def _protocol_issue(code: str, field: str, message: str) -> ProtocolIssue:
    return ProtocolIssue(kind="ProtocolError", code=code, field=field, message=message)


# LLM: _task_text reads task-like objects defensively for legacy mocks and persisted tasks.
# 函数用途: 兼容真实 SubAgentTask 和旧测试替身；缺字段时返回空字符串而不是抛异常。
def _task_text(task: object, name: str) -> str:
    return str(getattr(task, name, "") or "")


# LLM: _task_list normalizes list-like task fields without mutating the task object.
# 函数用途: 读取工具、验收、context pack 等列表字段；缺字段或非列表时返回空列表。
def _task_list(task: object, name: str) -> list:
    value = getattr(task, name, [])
    return list(value) if isinstance(value, (list, tuple, set)) else []


# LLM: _task_int reads integer-like task counters for address attempts and depth.
# 函数用途: 将缺失或非法数字字段安全转成 0，保证协议包兼容旧任务记录。
def _task_int(task: object, name: str) -> int:
    try:
        return int(getattr(task, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


# LLM: _task_float reads timestamp-like fields for protocol audit metadata.
# 函数用途: 将缺失或非法时间字段安全转成 0.0，避免旧 mock 阻断生命周期测试。
def _task_float(task: object, name: str) -> float:
    try:
        return float(getattr(task, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: _lineage_for_task walks parent links and falls back safely when historical records are partial.
# 函数用途: 生成 root -> ... -> current 的 run_id 链，缺父级时仍返回当前 run。
def _lineage_for_task(task: SubAgentTask, all_tasks: list[SubAgentTask]) -> list[str]:
    task_id = _task_text(task, "id")
    by_id = {_task_text(item, "id"): item for item in all_tasks if _task_text(item, "id")}
    lineage = [task_id] if task_id else []
    current = task
    seen = {task_id}
    while _task_text(current, "parent_id") and _task_text(current, "parent_id") in by_id and _task_text(current, "parent_id") not in seen:
        parent_id = _task_text(current, "parent_id")
        current = by_id[parent_id]
        lineage.append(_task_text(current, "id"))
        seen.add(parent_id)
    lineage.reverse()
    return lineage


# LLM: _attempt_from_task gives every address a stable attempt string even before a live runner starts.
# 函数用途: 从 runner attempt 计数生成兼容 attempt_id，后续可替换为正式 lease/attempt 表。
def _attempt_from_task(task: SubAgentTask) -> str:
    attempts = _task_int(task, "runner_attempts")
    return f"{_task_text(task, 'id')}:attempt-{attempts}"


# LLM: _workspace_ref chooses the most specific existing workspace-like path for this run.
# 函数用途: 优先返回 agent run workspace，其次 task workspace，最后旧 task_dir。
def _workspace_ref(task: SubAgentTask) -> str:
    return _task_text(task, "agent_run_workspace_dir") or _task_text(task, "task_workspace_dir") or _task_text(task, "task_dir")


# LLM: _tool_contract mirrors kernel tool facts but keeps protocol independent of kernel internals.
# 函数用途: 暴露工具白名单、已用工具、授权和缺口数量，不自动发放新权限。
def _tool_contract(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_tools": _task_list(task, "allowed_tools"),
        "used_tools": _task_list(task, "used_tools"),
        "controlled_exec_grant_ids": [
            grant.id for grant in _task_list(task, "capability_grants") if "controlled_exec" in list(getattr(grant, "tools", []) or [])
        ],
        "open_request_count": len([item for item in _task_list(task, "capability_requests") if _request_is_open(item)]),
        "grant_count": len(_task_list(task, "capability_grants")),
        "gap_count": len(_task_list(task, "capability_gaps")),
    }


# LLM: _write_contract keeps path boundaries in one protocol field.
# 函数用途: 暴露允许写入、禁止写入和锁定文件，供 preflight/recovery/QA 共用。
def _write_contract(task: SubAgentTask) -> dict[str, object]:
    product_roots = _product_write_roots(task)
    return {
        "internal_task_root": _task_text(task, "task_dir"),
        "product_write_roots": product_roots,
        "allowed_write_roots": _task_list(task, "allowed_write_roots"),
        "forbidden_write_roots": _task_list(task, "forbidden_write_roots"),
        "locked_files": _task_list(task, "locked_files"),
    }


# LLM: _product_write_roots separates user deliverable roots from the run's private scratch space.
# 函数用途: 过滤掉子代理自己的内部任务目录，避免把能写日志误判成能写用户产物。
def _product_write_roots(task: SubAgentTask) -> list[str]:
    internal_roots = {
        _normalize_path(_task_text(task, "task_dir")),
        _normalize_path(_task_text(task, "data_dir")),
        _normalize_path(_task_text(task, "output_dir")),
        _normalize_path(_task_text(task, "tests_dir")),
        _normalize_path(_task_text(task, "reports_dir")),
        _normalize_path(_task_text(task, "logs_dir")),
        _normalize_path(_task_text(task, "scratch_dir")),
    }
    task_root = _normalize_path(_task_text(task, "task_dir"))
    result: list[str] = []
    for root in _task_list(task, "allowed_write_roots"):
        normalized = _normalize_path(root)
        if not normalized or normalized in internal_roots:
            continue
        if task_root and normalized.startswith(f"{task_root}/"):
            continue
        result.append(root)
    return result


# LLM: _normalize_path gives protocol checks stable string comparisons without touching the filesystem.
# 函数用途: 规范化路径字符串用于内部目录过滤；不存在的目录也不会报错。
def _normalize_path(value: str) -> str:
    return str(value or "").rstrip("/")


# LLM: _acceptance_contract makes parent acceptance checks a protocol field, not only prompt text.
# 函数用途: 暴露验收条件、不能自验收和 required outputs，为 QA/验收链路提供机器事实。
def _acceptance_contract(task: SubAgentTask) -> dict[str, object]:
    quality_contract = getattr(task, "quality_contract", None)
    return {
        "checks": _task_list(task, "acceptance_checks"),
        "must_not_self_accept": bool(getattr(quality_contract, "cannot_self_accept", True)),
        "required_outputs": _task_list(task, "artifact_refs"),
    }


# LLM: _context_refs exposes small recovery/context refs without loading file bodies.
# 函数用途: 给 runner/recovery/QA 指向 checkpoint、summary、context bundle 和 final report。
def _context_refs(task: SubAgentTask) -> dict[str, object]:
    refs = {
        "checkpoint": _task_text(task, "agent_run_checkpoint_json") or _task_text(task, "checkpoint_ref"),
        "summary": _task_text(task, "agent_run_summary_md"),
        "context_bundle": _first_context_bundle_ref(task),
        "final_report": _task_text(task, "agent_run_final_report_md"),
        "task_dir": _task_text(task, "task_dir"),
    }
    return {key: value for key, value in refs.items() if value}


# LLM: _first_context_bundle_ref preserves compatibility with current context pack shapes.
# 函数用途: 从 context_packs 中找到第一个 bundle JSON 引用；没有则返回空。
def _first_context_bundle_ref(task: SubAgentTask) -> str:
    for item in _task_list(task, "context_packs"):
        ref = _context_pack_ref(item)
        if ref:
            return ref
    return ""


# LLM: _context_pack_ref hides legacy dict-shape checks from the loop above.
# 函数用途: 从兼容 context pack 对象中取 bundle 引用；非 dict 或空值直接返回空。
def _context_pack_ref(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("context_bundle_json") or item.get("ref") or "").strip()


# LLM: _request_is_open normalizes capability request status strings.
# 函数用途: 关闭或已授权的请求不再计入 open_request_count。
def _request_is_open(item: object) -> bool:
    return str(getattr(item, "status", "OPEN") or "OPEN").upper() not in {
        "CLOSED",
        "RESOLVED",
        "REJECTED",
        "APPROVED",
        "GRANTED",
    }


__all__ = [
    "ProtocolIssue",
    "ProtocolValidationReport",
    "TaskAddress",
    "TaskEnvelope",
    "build_task_address",
    "build_task_envelope",
    "task_envelope_dict",
    "validate_task_envelope",
]
