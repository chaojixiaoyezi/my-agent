# LLM: Build a compact, source-traceable handoff bundle for subagent runner prompts.
# 模块用途: 从已有 SubAgentTask 事实生成子代理实时工单包，并在派发前检查关键字段是否齐全。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .controlled_exec_gateway import controlled_exec_grant_refs
from .models import SubAgentTask
from .required_file_terms import forbidden_file_terms_from_text, required_file_terms_from_text

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
        workspace_refs=_workspace_refs(task),
        output_contract=_output_contract(task),
        lineage=_lineage(task),
        context_packs=list(task.context_packs or []),
        source_refs=_source_refs(),
        reserved={},
    )


# LLM: validate_context_bundle is the first context gate; later gates can add richer policy checks.
# 函数用途: 检查子代理工单包是否具备可派发的最小字段，返回只读报告供 runner 和 CLI 使用。
def validate_context_bundle(bundle: ContextBundleV1) -> ContextGateReport:
    missing = [
        field_name
        for field_name in REQUIRED_CONTEXT_BUNDLE_FIELDS
        if _is_missing(getattr(bundle, field_name))
    ]
    return ContextGateReport(
        ok=not missing,
        missing_fields=missing,
        blocking_reason="" if not missing else "missing_required_context_fields",
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
    lines.extend(_render_output_contract_lines(bundle.output_contract))
    lines.extend(["", "## Lineage", ""])
    lines.extend(f"- {key}: {value or 'none'}" for key, value in bundle.lineage.items())
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
        "failure_handoff_ref": _safe_string_ref(task, "failure_handoff_json"),
        "takeover_readiness_ref": _safe_string_ref(task, "takeover_readiness_json"),
    }


# LLM: _workspace_refs gives models paths to inspect or write without loading large file bodies.
# 函数用途: 生成旧工单目录和新 runtime workspace 的关键文件引用。
def _workspace_refs(task: SubAgentTask) -> dict[str, str]:
    return {
        "task_dir": _safe_string_ref(task, "task_dir"),
        "task_workspace": _safe_string_ref(task, "task_workspace_dir"),
        "agent_run_workspace": _safe_string_ref(task, "agent_run_workspace_dir"),
        "shared_blackboard": _safe_string_ref(task, "task_workspace_shared_blackboard"),
        "shared_messages": _safe_string_ref(task, "task_workspace_shared_messages_jsonl"),
        "agent_run_inbox": _safe_string_ref(task, "agent_run_inbox_dir"),
        "agent_run_outbox": _safe_string_ref(task, "agent_run_outbox_dir"),
        "artifacts_dir": _safe_string_ref(task, "agent_run_artifacts_dir") or _safe_string_ref(task, "output_dir"),
        "execution_context_json": _safe_string_ref(task, "execution_context_json"),
        "execution_context_file": _safe_string_ref(task, "execution_context_file"),
    }


# LLM: _output_contract tells the runner where durable reports and machine output must land.
# 函数用途: 约定子代理最终报告、结构化输出、证据、测试和产物引用，避免只返回自然语言。
def _output_contract(task: SubAgentTask) -> dict[str, object]:
    return {
        "final_report_ref": _safe_string_ref(task, "agent_run_final_report_md") or _safe_string_ref(task, "debrief_file"),
        "runner_result_ref": _safe_string_ref(task, "runner_result_json"),
        "output_json_ref": _safe_string_ref(task, "output_json"),
        "required_files": _required_file_contract(task),
        "forbidden_files": _forbidden_file_contract(task),
        "file_contract_source": "task_text_positive_negative_extraction",
        "evidence_refs_required": True,
        "tests_ref_style": "refs_only_with_working_dir",
        "artifact_refs_required": True,
    }


# LLM: _required_file_contract extracts exact deliverable filenames from task text without reading artifacts.
# 函数用途: 从 goal/thought/description/acceptance_checks 生成必需文件清单，作为结构化下发合同。
def _required_file_contract(task: SubAgentTask) -> list[str]:
    return _dedupe_file_terms(
        term
        for text in _file_contract_texts(task)
        for term in required_file_terms_from_text(text, extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html")
    )


# LLM: _forbidden_file_contract extracts negative filename examples so descendants do not treat them as outputs.
# 函数用途: 从任务文本里生成禁止文件清单，明确 product.html/legacy.html 这类反例不能创建。
def _forbidden_file_contract(task: SubAgentTask) -> list[str]:
    return _dedupe_file_terms(
        term
        for text in _file_contract_texts(task)
        for term in forbidden_file_terms_from_text(text, extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html")
    )


# LLM: _file_contract_texts keeps contract extraction bounded to lightweight persisted task facts.
# 函数用途: 收集可用于文件契约的短文本字段，不读取 output/artifact 正文，避免 token 和 IO 膨胀。
def _file_contract_texts(task: SubAgentTask) -> list[str]:
    values: list[object] = [
        task.goal,
        task.thought,
        getattr(task, "description", ""),
        *(task.acceptance_checks or []),
    ]
    return [str(value or "") for value in values if str(value or "").strip()]


# LLM: _dedupe_file_terms preserves user-mentioned order for required/forbidden contract lists.
# 函数用途: 对结构化文件清单去重，避免同一文件从 goal 和验收条件重复出现。
def _dedupe_file_terms(values) -> list[str]:
    terms: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in terms:
            terms.append(text)
    return terms


# LLM: _render_output_contract_lines makes machine file contracts visible in handoff markdown.
# 函数用途: 渲染 context bundle 的产物合同，方便人和接管代理快速看到 required/forbidden 文件清单。
def _render_output_contract_lines(contract: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for key, value in contract.items():
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value) if value else "none"
        else:
            rendered = str(value) if value not in (None, "") else "none"
        lines.append(f"- {key}: {rendered}")
    return lines


# LLM: _lineage gives nested runners parent/root refs without expanding ancestor files into prompt text.
# 函数用途: 记录当前子代理在任务树中的位置，以及直接父级 context bundle 的可读路径。
def _lineage(task: SubAgentTask) -> dict[str, object]:
    parent_id = str(task.parent_id or "")
    task_dir_ref = _safe_string_ref(task, "task_dir")
    task_workspace_ref = _safe_string_ref(task, "task_workspace_dir")
    agent_run_ref = _safe_string_ref(task, "agent_run_workspace_dir")
    task_dir = Path(task_dir_ref) if task_dir_ref else Path("")
    task_workspace = Path(task_workspace_ref) if task_workspace_ref else Path("")
    parent_legacy_ref = str(task_dir.parent / parent_id / "context_bundle.json") if parent_id and task_dir_ref else ""
    parent_agent_ref = str(task_workspace / "agents" / parent_id / "context_bundle.json") if parent_id and task_workspace_ref else ""
    return {
        "root_id": task.root_id or task.id,
        "parent_id": parent_id,
        "depth": int(task.depth or 0),
        "own_context_bundle_ref": str(Path(agent_run_ref) / "context_bundle.json") if agent_run_ref else "",
        "own_legacy_context_bundle_ref": str(Path(task_dir_ref) / "context_bundle.json") if task_dir_ref else "",
        "parent_context_bundle_ref": parent_agent_ref,
        "parent_legacy_context_bundle_ref": parent_legacy_ref,
        "inheritance_manifest_ref": _safe_string_ref(task, "inheritance_manifest_json"),
    }


# LLM: _safe_string_ref protects JSON context bundles from mocks or missing optional path refs.
# 函数用途: 读取可选路径字段；只有字符串和 Path 会进入 bundle，MagicMock/None 等测试占位值会归一成空串。
def _safe_string_ref(task: SubAgentTask, field_name: str) -> str:
    value = getattr(task, field_name, "")
    if isinstance(value, Path):
        return str(value)
    return value if isinstance(value, str) else ""


# LLM: _source_refs makes every major bundle field traceable to existing task facts.
# 函数用途: 给大模型和调试人员标明关键字段从哪里来，后续可扩展到 ledger/compact/task refs。
def _source_refs() -> dict[str, list[str]]:
    return {
        "goal": ["task.goal"],
        "thought": ["task.thought"],
        "plan": ["task.plan"],
        "acceptance_checks": ["task.acceptance_checks"],
        "permissions": ["task.allowed_tools", "task.allowed_skills", "task.capability_grants"],
        "constraints": ["task.allowed_write_roots", "task.forbidden_write_roots", "task.locked_files"],
        "workspace_refs": ["task.task_dir", "task.task_workspace_dir", "task.agent_run_workspace_dir"],
        "output_contract": [
            "task.output_json",
            "task.runner_result_json",
            "task.agent_run_final_report_md",
            "task.goal",
            "task.thought",
            "task.acceptance_checks",
        ],
        "lineage": ["task.root_id", "task.parent_id", "task.depth", "task.inheritance_manifest_json"],
        "context_packs": ["task.context_packs"],
    }


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
