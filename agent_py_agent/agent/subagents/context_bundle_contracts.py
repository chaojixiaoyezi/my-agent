
# LLM: 文件合同只表达显式输出和继承权限；路径与工具共享 cwd，不再把相对引用移入内部 output。
# 模块用途: 给子代理投影身份、文件引用和读写边界，不代写或搬运交付物。
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

from ..model_visible_refs import (
    clean_path_contract_ref,
    current_model_ref,
    current_model_text,
    has_placeholder_path_segment,
    is_non_model_visible_locator_root,
)
from .context_bundle_refs import safe_string_ref, workspace_refs
from .models import SubAgentTask
from .services.output_alignment import (
    OutputAnchoring,
    anchor_refs_for_execution,
    anchored_output_refs,
)

_SAFE_FILE_SUFFIX_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,63}$")


class _TaskContractComponents(NamedTuple):
    required_files: list[str]
    product_roots: list[str]
    required_file_refs: list[str]
    forbidden_files: list[str]
    source: str


# LLM: The child-visible output contract contains only parent/user-declared
# product targets. Host-owned closeout, runner-result, and final-report paths
# must stay out; the host creates those projections after the final response.
# 函数用途: 生成子代理真正需要执行的业务产物合同，不把内部收口文件伪装成待写交付物。
def output_contract(task: SubAgentTask) -> dict[str, object]:
    components = task_contract_components(task)
    # 相对声明只补可信 cwd，不能因为权限不够而换一个地址冒充目标。
    anchoring = _model_visible_output_anchoring(task)
    anchored_required = _merged_anchored_required(task, components.required_file_refs, anchoring)
    return {
        "product_write_roots": components.product_roots,
        "required_file_refs": anchored_required,
        "declared_output_refs": declared_output_refs(task),
        "write_contract_warnings": anchoring.warnings,
        "required_files": components.required_files,
        "forbidden_files": components.forbidden_files,
        "file_contract_source": components.source,
    }


# LLM: The task packet mirrors only executable task/tool/write facts. Runtime
# closeout refs are host state and cannot be model-authored or treated as tools.
# 函数用途: 生成子代理可读取的任务包，列清身份、目标、工具和实际写入范围。
def task_packet(task: SubAgentTask) -> dict[str, object]:
    refs = workspace_refs(task)
    components = task_contract_components(task)
    anchoring = _model_visible_output_anchoring(task)
    anchored_required = _merged_anchored_required(task, components.required_file_refs, anchoring)
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
        "file_contract": {
            "required_files": components.required_files,
            "required_file_refs": anchored_required,
            "declared_output_refs": declared_output_refs(task),
            "forbidden_files": components.forbidden_files,
            "source": components.source,
        },
        "write_contract": {
            "product_write_roots": components.product_roots,
            "required_file_refs": anchored_required,
            "declared_output_refs": declared_output_refs(task),
            "write_contract_warnings": anchoring.warnings,
            "allowed_write_roots": allowed_write_roots(task),
            "forbidden_write_roots": _model_visible_file_terms(task.forbidden_write_roots),
            "locked_files": _model_visible_file_terms(task.locked_files),
        },
        "tool_contract": {
            "allowed_tools": list(task.allowed_tools or []),
            "allowed_skills": list(task.allowed_skills or []),
            "canonical_tool_names": True,
            "path_argument": "path",
        },
        "workspace_refs": {
            "owner_workspace_dir": refs.get("owner_workspace_dir", ""),
            "task_root": refs.get("task_root", ""),
            "agent_work_dir": refs.get("agent_work_dir", ""),
            "context_bundle_json": _context_bundle_json_ref(refs),
        },
    }


# LLM: 执行合同的文件引用必须共用工具 cwd，不从权限根列表复制出多个同名目标。
# 函数用途: 合出子代理执行视角的完整目标 refs 列表。
def _merged_anchored_required(
    task: SubAgentTask,
    required_file_refs: list[str],
    anchoring: OutputAnchoring,
) -> list[str]:
    merged = anchor_refs_for_execution(task, required_file_refs)
    for ref in anchoring.anchored_refs:
        if ref and ref not in merged:
            merged.append(ref)
    return merged


# LLM: 保留宿主运行根与已继承写根；output_files/output_refs 不能授予新目录权限。
# 函数用途: 投影现有写权限，不按交付意图新增授权。
def allowed_write_roots(task: SubAgentTask) -> list[str]:
    roots: list[str] = []
    for raw in (
        safe_string_ref(task, "task_workspace_dir"),
        safe_string_ref(task, "agent_run_workspace_dir"),
        *list(task.allowed_write_roots or []),
    ):
        text = _model_visible_write_root(task, raw)
        if text and text not in roots:
            roots.append(text)
    return roots


def _model_visible_write_root(task: SubAgentTask, value: object) -> str:
    text = clean_path_contract_ref(value)
    if not text or is_non_model_visible_locator_root(task, text):
        return ""
    return text


def task_contract_components(task: SubAgentTask) -> _TaskContractComponents:
    required_files = required_file_contract(task)
    product_roots = product_write_roots(task)
    return _TaskContractComponents(
        required_files=required_files,
        product_roots=product_roots,
        required_file_refs=required_product_file_refs(task, required_files),
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
        text = _model_visible_write_root(task, raw)
        if not text or path_is_internal(text, internal_roots):
            continue
        if text not in roots:
            roots.append(text)
    return roots


# LLM: 所有声明文件只按 canonical cwd 解析，不从权限根列表推导多个交付目标。
# 函数用途: 把明确文件要求转成唯一真实路径，避免多写根产生多个假交付目标。
def required_product_file_refs(
    task: SubAgentTask,
    required_files: list[str] | None = None,
) -> list[str]:
    files = required_files if required_files is not None else required_file_contract(task)
    return anchor_refs_for_execution(task, files)


def forbidden_file_contract(task: SubAgentTask) -> list[str]:
    return _structured_forbidden_file_contract(task)


def file_contract_source(task: SubAgentTask) -> str:
    del task
    return "attributes_required_forbidden_fields"


def _structured_required_file_contract(task: SubAgentTask) -> list[str]:
    attrs = _task_attributes(task)
    output_files = [] if _has_legacy_system_default_output_ref(task) else _path_like_output_contract_list(
        attrs.get("output_files")
    )
    output_refs = [] if _has_legacy_system_default_output_ref(task) else _path_like_output_contract_list(
        attrs.get("output_refs")
    )
    return _dedupe_file_terms([
        *_file_contract_list(attrs.get("required_files")),
        *_file_contract_list(attrs.get("required_file_refs")),
        *output_files,
        *output_refs,
    ])


def declared_output_refs(task: SubAgentTask) -> list[str]:
    attrs = _task_attributes(task)
    legacy_default = _has_legacy_system_default_output_ref(task)
    return _dedupe_file_terms([
        *([] if legacy_default else _file_contract_list(attrs.get("output_files"))),
        *([] if legacy_default else _file_contract_list(attrs.get("output_refs"))),
        *_file_contract_list(attrs.get("artifact_refs")),
    ])


# LLM: 本修复以前的 durable child 可能带 system_default_output_ref；该路径只是
#   旧运行时内部报告槽，不是用户/父代理声明的业务产物。旧记录继续可恢复，但不得再
#   进入 runner 的文件合同、交付映射或完成通知。新建任务不再生成这个字段。
# 函数用途: 判断一个历史子任务的 output_files 是否只是旧版系统默认报告槽。
def _has_legacy_system_default_output_ref(task: SubAgentTask) -> bool:
    return _task_attributes(task).get("system_default_output_ref") is True


# LLM: 模型可见的产物锚定必须排除旧版内部默认报告槽；真实显式 output_files 仍走
#   output_alignment 的同一权威锚定。返回空投影，不修改 durable task。
# 函数用途: 给 runner 合同计算真正由用户或父代理声明的产物落点。
def _model_visible_output_anchoring(task: SubAgentTask) -> OutputAnchoring:
    if _has_legacy_system_default_output_ref(task):
        return OutputAnchoring()
    return anchored_output_refs(task)


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
    return [term] if term and not has_placeholder_path_segment(term) else []


def clean_file_contract_term(value: object) -> str:
    text = str(value or "").strip().strip("`'\".,;:，。；：、").replace("\\", "/")
    return text[2:] if text.startswith("./") else text


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


def _compact_list(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(str(item) for item in value)


def file_level_write_root_terms(task: SubAgentTask) -> list[str]:
    terms: list[str] = []
    task_dir = Path(str(getattr(task, "task_dir", "") or ""))
    for raw in getattr(task, "allowed_write_roots", []) or []:
        path = Path(str(raw or "").strip().replace("\\", "/"))
        if not is_contract_file_path(path) or _is_internal_task_file(path, task_dir):
            continue
        add_file_root_term(terms, path.name)
        if len(path.parts) >= 2:
            add_file_root_term(terms, "/".join(path.parts[-2:]))
    return terms


def is_contract_file_path(path: Path) -> bool:
    return bool(path.name and _SAFE_FILE_SUFFIX_RE.fullmatch(path.suffix.lower()))


def add_file_root_term(terms: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in terms:
        terms.append(text)


def internal_root_texts(task: object) -> set[str]:
    fields = (
        "task_dir",
        "data_dir",
        "output_dir",
        "tests_dir",
        "reports_dir",
        "logs_dir",
        "scratch_dir",
        "task_workspace_dir",
        "agent_run_workspace_dir",
        "agent_run_artifacts_dir",
    )
    return {_resolved_path_text(getattr(task, field, "")) for field in fields if _path_text(getattr(task, field, ""))}


def path_is_internal(path: str, internal_roots: set[str]) -> bool:
    resolved = _resolved_path_text(path)
    return any(resolved == root or resolved.startswith(f"{root}/") for root in internal_roots if root)


def _is_internal_task_file(path: Path, task_dir: Path) -> bool:
    if not str(task_dir):
        return False
    try:
        return path.resolve().is_relative_to(task_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _path_text(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    return value if isinstance(value, str) else ""


def _resolved_path_text(value: object) -> str:
    text = _path_text(value)
    return str(Path(text).expanduser().resolve(strict=False)) if text else ""


def _model_visible_file_terms(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return _dedupe_file_terms(value)


def _model_visible_file_term(value: object) -> str:
    text = str(value or "").strip()
    if has_placeholder_path_segment(text):
        return ""
    return current_model_ref(text) if text else ""
