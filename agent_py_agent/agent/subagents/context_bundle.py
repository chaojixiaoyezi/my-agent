
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..model_visible_refs import current_model_ref, current_model_text
from .context_bundle_contracts import (
    output_contract,
    render_output_contract_lines,
    render_task_packet_lines,
    task_packet,
)
from .context_bundle_refs import lineage, safe_string_ref, workspace_refs
from .context_bundle_semantic import semantic_context_missing_fields
from .context_bundle_sources import source_refs
from .controlled_exec_gateway import controlled_exec_grant_refs
from .models import SubAgentTask
from .protocol import build_task_envelope
from .protocol_preflight import run_tool_preflight

REQUIRED_CONTEXT_BUNDLE_FIELDS = (
    "goal",
    "acceptance_checks",
    "output_contract",
    "permissions",
    "constraints",
    "workspace_refs",
)


@dataclass(frozen=True)
class ContextBundleV1:
    schema_version: str
    run_id: str
    root_id: str
    parent_id: str
    depth: int
    role: str
    agent_name: str
    goal: str
    thought: str
    plan: list[str] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    permissions: dict[str, object] = field(default_factory=dict)
    constraints: dict[str, object] = field(default_factory=dict)
    workspace_refs: dict[str, str] = field(default_factory=dict)
    output_contract: dict[str, object] = field(default_factory=dict)
    lineage: dict[str, object] = field(default_factory=dict)
    context_packs: list[dict[str, object]] = field(default_factory=list)
    task_packet: dict[str, object] = field(default_factory=dict)
    task_envelope: dict[str, object] = field(default_factory=dict)
    tool_preflight: dict[str, object] = field(default_factory=dict)
    collaboration: dict[str, object] = field(default_factory=dict)
    source_refs: dict[str, list[str]] = field(default_factory=dict)
    reserved: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextGateReport:
    ok: bool
    missing_fields: list[str] = field(default_factory=list)
    blocking_reason: str = ""
    reserved: dict[str, object] = field(default_factory=dict)


def build_context_bundle(task: SubAgentTask) -> ContextBundleV1:
    envelope = build_task_envelope(task)
    return ContextBundleV1(
        schema_version="subagent_context_bundle.v1",
        run_id=task.id,
        root_id=task.root_id or task.id,
        parent_id=task.parent_id,
        depth=int(task.depth or 0),
        role=task.role,
        agent_name=task.agent_name,
        goal=current_model_text(task.goal),
        thought=current_model_text(task.thought),
        plan=[current_model_text(item) for item in list(task.plan or [])],
        acceptance_checks=list(task.acceptance_checks or []),
        permissions=_permissions(task),
        constraints=_constraints(task),
        workspace_refs=workspace_refs(task),
        output_contract=output_contract(task),
        lineage=lineage(task),
        context_packs=list(task.context_packs or []),
        task_packet=task_packet(task),
        task_envelope=envelope.to_dict(),
        tool_preflight=_tool_preflight(task, envelope),
        source_refs=source_refs(),
        reserved=_reserved(task),
    )


def validate_context_bundle(bundle: ContextBundleV1) -> ContextGateReport:
    missing = [
        field_name
        for field_name in REQUIRED_CONTEXT_BUNDLE_FIELDS
        if _is_missing(getattr(bundle, field_name))
    ]
    semantic_missing = [] if missing else semantic_context_missing_fields(bundle)
    missing.extend(semantic_missing)
    blocking_reason = ""
    if missing:
        blocking_reason = "missing_required_context_fields" if not semantic_missing else "semantic_context_mismatch"
    return ContextGateReport(
        ok=not missing,
        missing_fields=missing,
        blocking_reason=blocking_reason,
    )


def render_context_bundle_markdown(bundle: ContextBundleV1, gate: ContextGateReport) -> str:
    lines = [
        "# SUBAGENT CONTEXT BUNDLE",
        "",
        f"- schema_version: {bundle.schema_version}",
        f"- run_id: {bundle.run_id}",
        f"- root_id: {bundle.root_id}",
        f"- parent_id: {bundle.parent_id or 'none'}",
        f"- depth: {bundle.depth}",
        f"- role: {bundle.role}",
        f"- agent_name: {bundle.agent_name}",
        "",
        "## Goal",
        "",
        bundle.goal or "未设置",
        "",
        "## Plan",
        "",
    ]
    lines.extend(f"- {item}" for item in bundle.plan or ["未设置"])
    lines.extend(["", "## Acceptance Checks", ""])
    lines.extend(f"- [ ] {item}" for item in bundle.acceptance_checks or ["未设置"])
    lines.extend(["", "## Workspace Refs", ""])
    lines.extend(f"- {key}: {value or 'none'}" for key, value in bundle.workspace_refs.items())
    lines.extend(["", "## Output Contract", ""])
    lines.extend(render_output_contract_lines(bundle.output_contract))
    lines.extend(["", "## Lineage", ""])
    lines.extend(f"- {key}: {value or 'none'}" for key, value in bundle.lineage.items())
    lines.extend(["", "## Task Envelope", ""])
    lines.extend(render_task_envelope_lines(bundle.task_envelope))
    lines.extend(["", "## Tool Preflight", ""])
    lines.extend(render_tool_preflight_lines(bundle.tool_preflight))
    lines.extend(["", "## Collaboration", ""])
    lines.extend(render_collaboration_lines(bundle.collaboration))
    lines.extend(["", "## Task Packet", ""])
    lines.extend(render_task_packet_lines(bundle.task_packet))
    lines.extend(["", "## Context Gate", ""])
    lines.append(f"- ok: {gate.ok}")
    lines.append(f"- blocking_reason: {gate.blocking_reason or 'none'}")
    lines.append(f"- missing_fields: {', '.join(gate.missing_fields) if gate.missing_fields else 'none'}")
    return "\n".join(lines) + "\n"


def context_gate_prompt_lines(context_bundle: dict[str, object]) -> list[str]:
    gate = context_bundle.get("gate") if isinstance(context_bundle, dict) else {}
    if not isinstance(gate, dict):
        gate = {}
    missing = [str(item) for item in gate.get("missing_fields") or [] if str(item).strip()]
    if gate.get("ok") is True:
        return [
            "- Context Gate: PASS",
            f"- context_bundle_json: {context_bundle.get('context_bundle_json', 'context_bundle.json')}",
            *_protocol_prompt_lines(context_bundle),
            "- 优先按 context_bundle.task_packet 的 role、goal、file_contract、write_contract 和 tool_contract 执行；"
            "不要从摘要里重新猜路径或工具名。",
            "- 先按 context bundle 做一次自检，再执行任务。",
        ]
    return [
        "- Context Gate: BLOCKED",
        f"- blocking_reason: {gate.get('blocking_reason') or 'missing_required_context_fields'}",
        f"- missing_fields: {', '.join(missing) if missing else 'unknown'}",
        "- 不要继续执行业务实现；请在结果块中返回 BLOCKED，并说明需要父代理补齐哪些字段。",
    ]


def render_task_envelope_lines(envelope: dict[str, object]) -> list[str]:
    address = envelope.get("address") if isinstance(envelope.get("address"), dict) else {}
    acceptance = envelope.get("acceptance") if isinstance(envelope.get("acceptance"), dict) else {}
    return [
        f"- schema_version: {envelope.get('schema_version') or 'none'}",
        f"- run_id: {address.get('run_id') or 'none'}",
        f"- lineage: {_compact_prompt_list(address.get('lineage'))}",
        f"- workspace_ref: {address.get('workspace_ref') or 'none'}",
        f"- acceptance_checks: {_compact_prompt_list(acceptance.get('checks'))}",
    ]


def render_tool_preflight_lines(preflight: dict[str, object]) -> list[str]:
    issues = preflight.get("issues") if isinstance(preflight.get("issues"), list) else []
    issue_codes = [str(item.get("code") or "") for item in issues if isinstance(item, dict)]
    return [
        f"- ok: {bool(preflight.get('ok'))}",
        f"- issue_codes: {_compact_prompt_list(issue_codes)}",
        f"- effective_tools: {_compact_prompt_list(preflight.get('effective_tools'))}",
    ]


def render_collaboration_lines(collaboration: dict[str, object]) -> list[str]:
    if not collaboration:
        return ["- targeted_request_count: 0"]
    requests = collaboration.get("targeted_requests")
    if not isinstance(requests, list):
        requests = []
    lines = [f"- targeted_request_count: {len(requests)}"]
    for request in requests[:5]:
        if not isinstance(request, dict):
            continue
        lines.append(
            "- targeted_request: "
            f"case_id={request.get('case_id') or 'none'}; "
            f"request_id={request.get('request_id') or 'none'}; "
            f"request_ref={request.get('request_ref') or 'none'}"
        )
    return lines


def _protocol_prompt_lines(context_bundle: dict[str, object]) -> list[str]:
    envelope = context_bundle.get("task_envelope") if isinstance(context_bundle.get("task_envelope"), dict) else {}
    preflight = context_bundle.get("tool_preflight") if isinstance(context_bundle.get("tool_preflight"), dict) else {}
    issues = preflight.get("issues") if isinstance(preflight.get("issues"), list) else []
    issue_codes = [str(item.get("code") or "") for item in issues if isinstance(item, dict)]
    status = "PASS" if preflight.get("ok") is True else "ISSUE"
    return [
        f"- TaskEnvelope: {envelope.get('schema_version') or 'missing'}",
        "- 优先按 context_bundle.task_envelope 的 address、tool_contract、write_contract、acceptance 执行；"
        "自然语言 summary 只作为说明。",
        f"- Tool Preflight: {status}",
        f"- Tool Preflight issue_codes: {_compact_prompt_list(issue_codes)}",
    ]


def _tool_preflight(task: SubAgentTask, envelope) -> dict[str, object]:
    return run_tool_preflight(
        envelope,
        available_tools=_preflight_available_tools(task),
    ).to_dict()


def _preflight_available_tools(task: SubAgentTask) -> list[str]:
    return [str(item) for item in list(task.allowed_tools or []) if str(item).strip()]


def _compact_prompt_list(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(str(item) for item in value if str(item).strip()) or "none"


def _permissions(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_tools": list(task.allowed_tools or []),
        "allowed_skills": list(task.allowed_skills or []),
        "capability_grants": [grant.id for grant in task.capability_grants],
        "controlled_exec_grants": controlled_exec_grant_refs(list(task.capability_grants or [])),
    }


def _constraints(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_write_roots": _allowed_write_roots(task),
        "forbidden_write_roots": _path_terms(task.forbidden_write_roots),
        "locked_files": _path_terms(task.locked_files),
        "failure_handoff_ref": safe_string_ref(task, "failure_handoff_json"),
        "takeover_readiness_ref": safe_string_ref(task, "takeover_readiness_json"),
    }


def _allowed_write_roots(task: SubAgentTask) -> list[str]:
    roots: list[str] = []
    for raw in (
        safe_string_ref(task, "task_workspace_dir"),
        safe_string_ref(task, "agent_run_workspace_dir"),
        *list(task.allowed_write_roots or []),
    ):
        text = current_model_ref(raw)
        if text and text not in roots:
            roots.append(text)
    return roots


def _path_terms(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    terms: list[str] = []
    for item in value:
        text = current_model_ref(item)
        if text and text not in terms:
            terms.append(text)
    return terms


def _reserved(task: SubAgentTask) -> dict[str, object]:
    attributes = getattr(task, "attributes", {})
    attributes = attributes if isinstance(attributes, dict) else {}
    preflight = attributes.get("runner_recovery_preflight")
    reserved: dict[str, object] = _file_contract_reserved(attributes)
    conversation = _conversation_reserved(attributes)
    if conversation:
        reserved["conversation"] = conversation
    if isinstance(preflight, dict):
        reserved["runner_recovery_preflight"] = dict(preflight)
    return reserved


def _conversation_reserved(attributes: dict[str, object]) -> dict[str, str]:
    thread_id = str(attributes.get("conversation_thread_id") or "").strip()
    task_id = str(attributes.get("conversation_task_id") or "").strip()
    payload = {}
    if thread_id:
        payload["thread_id"] = thread_id
    if task_id:
        payload["root_task_id"] = task_id
    return payload


def _file_contract_reserved(attributes: dict[str, object]) -> dict[str, object]:
    required = attributes.get("required_files")
    if not isinstance(required, list):
        return {}
    return {"expected_required_files": [str(item).strip() for item in required if str(item or "").strip()]}

def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not bool(value)
    if isinstance(value, Path):
        return not str(value)
    return False
