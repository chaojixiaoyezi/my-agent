# LLM: Build a compact, source-traceable handoff bundle for subagent runner prompts.
# 模块用途: 从已有 SubAgentTask 事实生成子代理实时工单包，并在派发前检查关键字段是否齐全。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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

REQUIRED_CONTEXT_BUNDLE_FIELDS = (
    "goal",
    "plan",
    "acceptance_checks",
    "output_contract",
    "permissions",
    "constraints",
    "workspace_refs",
)


# LLM: ContextBundleV1 is the runner-facing handoff contract; keep fields stable and source refs explicit.
# 类用途: 保存一次子代理运行需要的目标、约束、权限、产物要求和字段来源，方便模型和接管代理快速理解任务。
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
    source_refs: dict[str, list[str]] = field(default_factory=dict)
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: ContextGateReport records missing handoff fields without mutating task state.
# 类用途: 给派发前检查返回是否可继续、缺哪些字段、阻断原因和后续可扩展信息。
@dataclass(frozen=True)
class ContextGateReport:
    ok: bool
    missing_fields: list[str] = field(default_factory=list)
    blocking_reason: str = ""
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: build_context_bundle converts a persisted task into a compact runner handoff snapshot.
# 函数用途: 根据 SubAgentTask 构造实时 context bundle；只引用路径和摘要，不读取大型 artifact 正文。
def build_context_bundle(task: SubAgentTask) -> ContextBundleV1:
    return ContextBundleV1(
        schema_version="subagent_context_bundle.v1",
        run_id=task.id,
        root_id=task.root_id or task.id,
        parent_id=task.parent_id,
        depth=int(task.depth or 0),
        role=task.role,
        agent_name=task.agent_name,
        goal=task.goal,
        thought=task.thought,
        plan=list(task.plan or []),
        acceptance_checks=list(task.acceptance_checks or []),
        permissions=_permissions(task),
        constraints=_constraints(task),
        workspace_refs=workspace_refs(task),
        output_contract=output_contract(task),
        lineage=lineage(task),
        context_packs=list(task.context_packs or []),
        task_packet=task_packet(task),
        source_refs=source_refs(),
        reserved=_reserved(task),
    )


# LLM: validate_context_bundle checks both presence and semantic consistency of handoff facts.
# 函数用途: 检查子代理工单包是否具备可派发字段，并确认文件合同没有从自然语言目标里缩水。
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


# LLM: render_context_bundle_markdown keeps the persisted handoff readable for humans and future takeover agents.
# 函数用途: 把 context bundle 和 gate 报告渲染成轻量 Markdown，不展开 artifact 正文。
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
    lines.extend(["", "## Task Packet", ""])
    lines.extend(render_task_packet_lines(bundle.task_packet))
    lines.extend(["", "## Context Gate", ""])
    lines.append(f"- ok: {gate.ok}")
    lines.append(f"- blocking_reason: {gate.blocking_reason or 'none'}")
    lines.append(f"- missing_fields: {', '.join(gate.missing_fields) if gate.missing_fields else 'none'}")
    return "\n".join(lines) + "\n"


# LLM: context_gate_prompt_lines gives the runner a short mandatory self-check before doing work.
# 函数用途: 把 gate 结果渲染成 prompt 片段；缺关键字段时要求子代理停止业务实现并返回 BLOCKED。
def context_gate_prompt_lines(context_bundle: dict[str, object]) -> list[str]:
    gate = context_bundle.get("gate") if isinstance(context_bundle, dict) else {}
    if not isinstance(gate, dict):
        gate = {}
    missing = [str(item) for item in gate.get("missing_fields") or [] if str(item).strip()]
    if gate.get("ok") is True:
        return [
            "- Context Gate: PASS",
            f"- context_bundle_json: {context_bundle.get('context_bundle_json', 'context_bundle.json')}",
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


# LLM: _permissions separates tool/skill access and controlled exec grants from task instructions.
# 函数用途: 汇总子代理授权工具、技能和受控 exec grant refs；空列表表示后续策略可自动判断，不代表模型能越权。
def _permissions(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_tools": list(task.allowed_tools or []),
        "allowed_skills": list(task.allowed_skills or []),
        "capability_grants": [grant.id for grant in task.capability_grants],
        "controlled_exec_grants": controlled_exec_grant_refs(list(task.capability_grants or [])),
    }


# LLM: _constraints carries filesystem and inheritance boundaries into the handoff bundle.
# 函数用途: 汇总写入范围、禁止范围、锁定文件和失败/接管提示引用。
def _constraints(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_write_roots": list(task.allowed_write_roots or []),
        "forbidden_write_roots": list(task.forbidden_write_roots or []),
        "locked_files": list(task.locked_files or []),
        "failure_handoff_ref": safe_string_ref(task, "failure_handoff_json"),
        "takeover_readiness_ref": safe_string_ref(task, "takeover_readiness_json"),
    }


# LLM: _reserved carries small future-extensible handoff hints without changing the context bundle schema.
# 函数用途: 将 task.attributes 里的轻量恢复预检信息传给 runner prompt；不复制正文产物或主代理记忆。
def _reserved(task: SubAgentTask) -> dict[str, object]:
    attributes = getattr(task, "attributes", {})
    attributes = attributes if isinstance(attributes, dict) else {}
    preflight = attributes.get("runner_recovery_preflight")
    if not isinstance(preflight, dict):
        return {}
    return {"runner_recovery_preflight": dict(preflight)}

# LLM: _is_missing defines the minimum useful handoff signal for gate checks.
# 函数用途: 判断字符串、列表、字典等字段是否为空；用于缺字段报告。
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
