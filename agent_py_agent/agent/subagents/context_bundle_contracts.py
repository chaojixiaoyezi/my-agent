
from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

from ..model_visible_refs import current_model_ref, current_model_text
from .context_bundle_file_roots import (
    add_file_root_term,
    file_level_write_root_terms,
    is_contract_file_path,
)
from .context_bundle_internal_roots import internal_root_texts, path_is_internal
from .context_bundle_refs import safe_string_ref, workspace_refs
from .models import SubAgentTask
from .required_file_terms import (
    clean_file_contract_term,
)


class _TaskContractComponents(NamedTuple):
    required_files: list[str]
    product_roots: list[str]
    required_file_refs: list[str]
    forbidden_files: list[str]
    source: str


def output_contract(task: SubAgentTask) -> dict[str, object]:
    components = task_contract_components(task)
    return {
        "product_write_roots": components.product_roots,
        "required_file_refs": components.required_file_refs,
        "final_report_ref": _preferred_final_report_ref(task, components.required_file_refs),
        "agent_run_final_report_ref": safe_string_ref(task, "agent_run_final_report_md") or safe_string_ref(task, "debrief_file"),
        "runner_result_ref": safe_string_ref(task, "runner_result_json"),
        "run_closeout_ref": safe_string_ref(task, "output_json"),
        "declared_output_refs": declared_output_refs(task),
        "required_files": components.required_files,
        "forbidden_files": components.forbidden_files,
        "file_contract_source": components.source,
        "evidence_refs_required": True,
        "tests_ref_style": "refs_only_with_working_dir",
        "artifact_refs_required": True,
    }


def task_packet(task: SubAgentTask) -> dict[str, object]:
    refs = workspace_refs(task)
    components = task_contract_components(task)
    return {
        "schema_version": "subagent_task_packet.v1",
        "run_id": task.id,
        "root_id": task.root_id or task.id,
        "parent_id": task.parent_id,
        "depth": int(task.depth or 0),
        "role": task.role,
        "agent_name": task.agent_name,
        "goal": current_model_text(task.goal),
        "plan": [current_model_text(item) for item in list(task.plan or [])],
        "acceptance_checks": [current_model_text(item) for item in list(task.acceptance_checks or [])],
        "file_contract": {
            "required_files": components.required_files,
            "required_file_refs": components.required_file_refs,
            "declared_output_refs": declared_output_refs(task),
            "forbidden_files": components.forbidden_files,
            "source": components.source,
        },
        "write_contract": {
            "product_write_roots": components.product_roots,
            "required_file_refs": components.required_file_refs,
            "declared_output_refs": declared_output_refs(task),
            "allowed_write_roots": allowed_write_roots(task),
            "forbidden_write_roots": _model_visible_file_terms(task.forbidden_write_roots),
            "locked_files": _model_visible_file_terms(task.locked_files),
        },
        "tool_contract": {
            "allowed_tools": list(task.allowed_tools or []),
            "allowed_skills": list(task.allowed_skills or []),
            "canonical_tool_names": True,
            "path_argument": "path",
            "run_closeout_ref": safe_string_ref(task, "output_json"),
        },
        "workspace_refs": {
            "task_root": refs.get("task_root", ""),
            "agent_work_dir": refs.get("agent_work_dir", ""),
            "context_bundle_json": _context_bundle_json_ref(refs),
            "latest_continue_packet": refs.get("agent_run_latest_continue_packet", ""),
        },
}


def allowed_write_roots(task: SubAgentTask) -> list[str]:
    roots: list[str] = []
    for raw in (
        safe_string_ref(task, "task_workspace_dir"),
        safe_string_ref(task, "agent_run_workspace_dir"),
        *list(task.allowed_write_roots or []),
    ):
        text = str(raw or "").strip()
        text = current_model_ref(text)
        if text and text not in roots:
            roots.append(text)
    return roots


def task_contract_components(task: SubAgentTask) -> _TaskContractComponents:
    required_files = required_file_contract(task)
    product_roots = product_write_roots(task)
    return _TaskContractComponents(
        required_files=required_files,
        product_roots=product_roots,
        required_file_refs=required_product_file_refs(task, required_files, product_roots),
        forbidden_files=forbidden_file_contract(task),
        source=file_contract_source(task),
    )


def required_file_contract(task: SubAgentTask) -> list[str]:
    return _dedupe_file_terms(
        [
            *_structured_required_file_contract(task),
            *file_level_write_root_terms(task),
        ]
    )


def product_write_roots(task: SubAgentTask) -> list[str]:
    internal_roots = internal_root_texts(task)
    roots: list[str] = []
    for raw in getattr(task, "allowed_write_roots", []) or []:
        text = current_model_ref(raw)
        if not text or path_is_internal(text, internal_roots):
            continue
        if text not in roots:
            roots.append(text)
    return roots


def required_product_file_refs(
    task: SubAgentTask,
    required_files: list[str] | None = None,
    roots: list[str] | None = None,
) -> list[str]:
    files = required_files if required_files is not None else required_file_contract(task)
    product_roots = roots if roots is not None else product_write_roots(task)
    refs: list[str] = []
    for filename in files:
        for ref in _required_product_ref_candidates(str(filename or "").strip(), product_roots):
            add_file_root_term(refs, ref)
    return refs


def forbidden_file_contract(task: SubAgentTask) -> list[str]:
    return _structured_forbidden_file_contract(task)


def file_contract_source(task: SubAgentTask) -> str:
    del task
    return "attributes_required_forbidden_fields"


def _structured_required_file_contract(task: SubAgentTask) -> list[str]:
    attrs = _task_attributes(task)
    return _dedupe_file_terms([
        *_file_contract_list(attrs.get("required_files")),
        *_file_contract_list(attrs.get("required_file_refs")),
        *_path_like_output_contract_list(attrs.get("output_files")),
        *_path_like_output_contract_list(attrs.get("output_refs")),
    ])


def declared_output_refs(task: SubAgentTask) -> list[str]:
    attrs = _task_attributes(task)
    return _dedupe_file_terms([
        *_file_contract_list(attrs.get("output_files")),
        *_file_contract_list(attrs.get("output_refs")),
        *_file_contract_list(attrs.get("artifact_refs")),
    ])


def _path_like_output_contract_list(value: object) -> list[str]:
    return [item for item in _file_contract_list(value) if _looks_like_output_path(item)]


def _looks_like_output_path(value: object) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if not text or "://" in text:
        return False
    path = Path(text)
    return path.is_absolute() or "/" in text or is_contract_file_path(path)


def _structured_forbidden_file_contract(task: SubAgentTask) -> list[str]:
    return _dedupe_file_terms(_file_contract_list(_task_attributes(task).get("forbidden_files")))


def render_output_contract_lines(contract: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for key, value in contract.items():
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value) if value else "none"
        else:
            rendered = str(value) if value not in (None, "") else "none"
        lines.append(f"- {key}: {rendered}")
    return lines


def render_task_packet_lines(packet: dict[str, object]) -> list[str]:
    file_contract = packet.get("file_contract") if isinstance(packet.get("file_contract"), dict) else {}
    write_contract = packet.get("write_contract") if isinstance(packet.get("write_contract"), dict) else {}
    tool_contract = packet.get("tool_contract") if isinstance(packet.get("tool_contract"), dict) else {}
    return [
        f"- schema_version: {packet.get('schema_version') or 'none'}",
        f"- run_id: {packet.get('run_id') or 'none'}",
        f"- role: {packet.get('role') or 'none'}",
        f"- required_files: {_compact_list(file_contract.get('required_files'))}",
        f"- required_file_refs: {_compact_list(file_contract.get('required_file_refs'))}",
        f"- declared_output_refs: {_compact_list(file_contract.get('declared_output_refs'))}",
        f"- forbidden_files: {_compact_list(file_contract.get('forbidden_files'))}",
        f"- product_write_roots: {_compact_list(write_contract.get('product_write_roots'))}",
        f"- allowed_write_roots: {_compact_list(write_contract.get('allowed_write_roots'))}",
        f"- allowed_tools: {_compact_list(tool_contract.get('allowed_tools'))}",
    ]


def _context_bundle_json_ref(refs: dict[str, str]) -> str:
    workspace = refs.get("agent_work_dir", "") or refs.get("agent_run_workspace", "")
    return str(Path(workspace) / "context_bundle.json") if workspace else ""


def _task_attributes(task: SubAgentTask) -> dict[str, object]:
    attrs = getattr(task, "attributes", {})
    return attrs if isinstance(attrs, dict) else {}


def _file_contract_list(value: object) -> list[str]:
    if value is None:
        return []
    structured = _structured_file_ref_value(value)
    if structured is not value:
        return _file_contract_list(structured)
    if isinstance(value, dict):
        refs: list[str] = []
        for field in ("path", "file", "file_path", "output_path", "artifact_path", "ref", "href"):
            refs.extend(_file_contract_list(value.get(field)))
        return _dedupe_file_terms(refs)
    if isinstance(value, list | tuple):
        return _dedupe_file_terms([term for item in value for term in _file_contract_list(item)])
    term = clean_file_contract_term(value)
    return [term] if term else []


def _structured_file_ref_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return value
    return parsed if isinstance(parsed, dict | list | tuple) else value


def _dedupe_file_terms(values) -> list[str]:
    terms: list[str] = []
    for value in values:
        text = _model_visible_file_term(value)
        if text and text not in terms:
            terms.append(text)
    return terms


def _preferred_final_report_ref(task: SubAgentTask, required_refs: list[str]) -> str:
    for ref in required_refs:
        if Path(str(ref)).name == "final_report.md":
            return str(ref)
    return safe_string_ref(task, "agent_run_final_report_md") or safe_string_ref(task, "debrief_file")


def _resolve_required_file_ref(root: str, file_path: Path) -> str:
    root_path = Path(str(root or "").strip())
    if not str(root_path):
        return ""
    if is_contract_file_path(root_path):
        normalized_file = file_path.as_posix()
        normalized_root = root_path.as_posix()
        return str(root_path) if root_path.name == file_path.name or normalized_root.endswith("/" + normalized_file) else ""
    return str(root_path / _file_path_with_product_root_stripped(root_path, file_path))


def _file_path_with_product_root_stripped(root_path: Path, file_path: Path) -> Path:
    file_parts = file_path.parts
    root_parts = root_path.parts
    max_size = min(len(file_parts) - 1, len(root_parts))
    for size in range(max_size, 0, -1):
        if file_parts[:size] == root_parts[-size:]:
            return Path(*file_parts[size:])
    return file_path


def _required_product_ref_candidates(file_text: str, product_roots: list[str]) -> list[str]:
    if not file_text:
        return []
    file_path = Path(file_text)
    if file_path.is_absolute():
        return [str(file_path)]
    return [
        resolved
        for root in product_roots
        if (resolved := _resolve_required_file_ref(root, file_path))
    ]


def _compact_list(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(str(item) for item in value)


def _model_visible_file_terms(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return _dedupe_file_terms(value)


def _model_visible_file_term(value: object) -> str:
    text = str(value or "").strip()
    return current_model_ref(text) if text else ""
