# LLM: Context bundle contracts hold task_packet and deliverable file contract helpers.
# 模块用途: 从 SubAgentTask 生成产物合同、结构化 task_packet 和 Markdown 展示行。

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

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


# LLM: output_contract tells the runner where durable reports and machine output must land.
# 函数用途: 约定子代理最终报告、结构化输出、证据、测试和产物引用，避免只返回自然语言。
def output_contract(task: SubAgentTask) -> dict[str, object]:
    components = task_contract_components(task)
    return {
        "product_write_roots": components.product_roots,
        "required_file_refs": components.required_file_refs,
        "final_report_ref": _preferred_final_report_ref(task, components.required_file_refs),
        "agent_run_final_report_ref": safe_string_ref(task, "agent_run_final_report_md") or safe_string_ref(task, "debrief_file"),
        "runner_result_ref": safe_string_ref(task, "runner_result_json"),
        "output_json_ref": safe_string_ref(task, "output_json"),
        "declared_output_refs": declared_output_refs(task),
        "required_files": components.required_files,
        "forbidden_files": components.forbidden_files,
        "file_contract_source": components.source,
        "evidence_refs_required": True,
        "tests_ref_style": "refs_only_with_working_dir",
        "artifact_refs_required": True,
    }


# LLM: task_packet is the compact typed handoff child runners should trust before prose.
# 函数用途: 生成子代理/接管代理优先读取的结构化任务包，避免从自然语言摘要里猜路径。
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
        "goal": task.goal,
        "plan": list(task.plan or []),
        "acceptance_checks": list(task.acceptance_checks or []),
        "file_contract": {
            "required_files": components.required_files,
            "required_file_refs": components.required_file_refs,
            "declared_output_refs": declared_output_refs(task),
            "forbidden_files": components.forbidden_files,
            "source": components.source,
        },
        "write_contract": {
            # LLM: Write contracts expose generic roots only; concrete writes use write_file/apply_patch.
            "product_write_roots": components.product_roots,
            "required_file_refs": components.required_file_refs,
            "declared_output_refs": declared_output_refs(task),
            "allowed_write_roots": allowed_write_roots(task),
            "forbidden_write_roots": list(task.forbidden_write_roots or []),
            "locked_files": list(task.locked_files or []),
        },
        "tool_contract": {
            "allowed_tools": list(task.allowed_tools or []),
            "allowed_skills": list(task.allowed_skills or []),
            "canonical_tool_names": True,
            "path_argument": "path",
            "output_json_ref": safe_string_ref(task, "output_json"),
        },
        "workspace_refs": {
            "task_dir": refs.get("task_dir", ""),
            "agent_run_workspace": refs.get("agent_run_workspace", ""),
            "context_bundle_json": _context_bundle_json_ref(refs),
            "latest_continue_packet": refs.get("agent_run_latest_continue_packet", ""),
        },
        "reserved": {},
}


# LLM: allowed_write_roots keeps task packets aligned with the current runtime workspace.
# 函数用途: task_packet 里的可写目录优先展示当前 task/agent workspace，再展示显式产物目录。
def allowed_write_roots(task: SubAgentTask) -> list[str]:
    roots: list[str] = []
    for raw in (
        safe_string_ref(task, "task_workspace_dir"),
        safe_string_ref(task, "agent_run_workspace_dir"),
        *list(task.allowed_write_roots or []),
    ):
        text = str(raw or "").strip()
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: task_contract_components is the single source for task_packet/output_contract shared file facts.
# 函数用途: 集中计算 required/product/forbidden 文件合同，避免两个出口出现细微分叉。
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


# LLM: required_file_contract extracts exact deliverable filenames from task attributes and write roots.
# 函数用途: 从 task.attributes.required_files 和文件级写入根生成必需文件清单；不读取 goal/acceptance 文本。
def required_file_contract(task: SubAgentTask) -> list[str]:
    return _dedupe_file_terms(
        [
            *_structured_required_file_contract(task),
            *file_level_write_root_terms(task),
        ]
    )


# LLM: product_write_roots separates user deliverable roots from run-private report roots for handoff contracts.
# 函数用途: 从 allowed_write_roots 里筛出真实用户产物根，避免内部 agent-run final_report 被当成业务交付物。
def product_write_roots(task: SubAgentTask) -> list[str]:
    internal_roots = internal_root_texts(task)
    roots: list[str] = []
    for raw in getattr(task, "allowed_write_roots", []) or []:
        text = str(raw or "").strip()
        if not text or path_is_internal(text, internal_roots):
            continue
        if text not in roots:
            roots.append(text)
    return roots


# LLM: required_product_file_refs gives the runner exact product paths for required deliverables.
# 函数用途: 将 `final_report.md` 这类相对产物名绑定到 product_write_roots，减少模型把文件写进内部 run workspace。
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


# LLM: forbidden_file_contract extracts structured forbidden filenames from task attributes only.
# 函数用途: 从 task.attributes.forbidden_files 生成禁止文件清单，明确反例不能创建。
def forbidden_file_contract(task: SubAgentTask) -> list[str]:
    return _structured_forbidden_file_contract(task)


# LLM: file_contract_source labels bundle file contracts as task-attribute based.
# 函数用途: 给下游调试/验收说明 file_contract 的来源；不再存在 task text extraction 模式。
def file_contract_source(task: SubAgentTask) -> str:
    del task
    return "attributes_required_forbidden_fields"


# LLM: _structured_required_file_contract reads required files from task attributes only.
# 函数用途: 只读取 attributes.required_files / required_file_refs，不读取自然语言或中文继承标签。
def _structured_required_file_contract(task: SubAgentTask) -> list[str]:
    attrs = _task_attributes(task)
    return _dedupe_file_terms([
        *_file_contract_list(attrs.get("required_files")),
        *_file_contract_list(attrs.get("required_file_refs")),
        *_path_like_output_contract_list(attrs.get("output_files")),
        *_path_like_output_contract_list(attrs.get("output_refs")),
    ])


# LLM: declared_output_refs exposes parent-declared deliverables without parsing prose.
# 函数用途: 把 create/schedule 里的 output_files/output_refs/artifact_refs 传给 runner；不从 goal 自然语言推导。
def declared_output_refs(task: SubAgentTask) -> list[str]:
    attrs = _task_attributes(task)
    return _dedupe_file_terms([
        *_file_contract_list(attrs.get("output_files")),
        *_file_contract_list(attrs.get("output_refs")),
        *_file_contract_list(attrs.get("artifact_refs")),
    ])


# LLM: output_refs may be logical field names, so only path-like values become file requirements.
# 函数用途: 允许模型/外部系统用 output_refs 表示结果键；只有绝对路径、带目录或具体文件名才进入硬文件合同。
def _path_like_output_contract_list(value: object) -> list[str]:
    return [item for item in _file_contract_list(value) if _looks_like_output_path(item)]


def _looks_like_output_path(value: object) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if not text or "://" in text:
        return False
    path = Path(text)
    return path.is_absolute() or "/" in text or is_contract_file_path(path)


# LLM: _structured_forbidden_file_contract reads forbidden files from task attributes only.
# 函数用途: 只读取 attributes.forbidden_files，不读取 goal/thought/acceptance_checks 文本。
def _structured_forbidden_file_contract(task: SubAgentTask) -> list[str]:
    return _dedupe_file_terms(_file_contract_list(_task_attributes(task).get("forbidden_files")))


# LLM: render_output_contract_lines makes machine file contracts visible in handoff markdown.
# 函数用途: 渲染 context bundle 的产物合同，方便人和接管代理快速看到 required/forbidden 清单。
def render_output_contract_lines(contract: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for key, value in contract.items():
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value) if value else "none"
        else:
            rendered = str(value) if value not in (None, "") else "none"
        lines.append(f"- {key}: {rendered}")
    return lines


# LLM: render_task_packet_lines keeps the packet readable without dumping nested JSON into Markdown.
# 函数用途: 在 CONTEXT_BUNDLE.md 展示任务包关键字段，让接管代理快速确认结构化合同。
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


# LLM: _context_bundle_json_ref derives the standard context bundle path from agent_run_workspace.
# 函数用途: 只从 refs 构造路径字符串，不访问文件系统。
def _context_bundle_json_ref(refs: dict[str, str]) -> str:
    workspace = refs.get("agent_run_workspace", "")
    return str(Path(workspace) / "context_bundle.json") if workspace else ""


# LLM: _task_attributes normalizes task attributes for file-contract reads.
# 函数用途: 读取 task.attributes 字典；缺失或类型不对时返回空，不做文本兜底。
def _task_attributes(task: SubAgentTask) -> dict[str, object]:
    attrs = getattr(task, "attributes", {})
    return attrs if isinstance(attrs, dict) else {}


# LLM: _file_contract_list normalizes explicit file refs without parsing prose.
# 函数用途: 支持 list/tuple 或单字符串；不会按逗号、顿号或自然语言拆分。
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


# LLM: _structured_file_ref_value accepts serialized ref carriers without making prose a contract.
# 函数用途: 兼容旧任务把 {"output_path": "..."} 误存成字符串的情况，只按结构化对象读取路径字段。
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


# LLM: _dedupe_file_terms preserves user-mentioned order for required/forbidden contract lists.
# 函数用途: 对结构化文件清单去重，避免同一文件从 goal 和验收条件重复出现。
def _dedupe_file_terms(values) -> list[str]:
    terms: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in terms:
            terms.append(text)
    return terms


# LLM: _preferred_final_report_ref keeps the legacy key useful while separating internal reports.
# 函数用途: 如果用户明确要求 final_report.md，优先返回 product root 下的真实交付路径；否则保持旧内部报告引用。
def _preferred_final_report_ref(task: SubAgentTask, required_refs: list[str]) -> str:
    for ref in required_refs:
        if Path(str(ref)).name == "final_report.md":
            return str(ref)
    return safe_string_ref(task, "agent_run_final_report_md") or safe_string_ref(task, "debrief_file")


# LLM: _resolve_required_file_ref maps a required relative filename to one authorized product root.
# 函数用途: 目录 root 直接拼接文件；具体文件 root 只有同名/同后缀匹配时才作为精确交付路径。
def _resolve_required_file_ref(root: str, file_path: Path) -> str:
    root_path = Path(str(root or "").strip())
    if not str(root_path):
        return ""
    if is_contract_file_path(root_path):
        normalized_file = file_path.as_posix()
        normalized_root = root_path.as_posix()
        return str(root_path) if root_path.name == file_path.name or normalized_root.endswith("/" + normalized_file) else ""
    return str(root_path / _file_path_with_product_root_stripped(root_path, file_path))


# LLM: _file_path_with_product_root_stripped avoids duplicating the product root basename.
# 函数用途: 当模型/任务文本写出 `site/index.html` 且 product root 已经是 `.../site` 时，生成 `.../site/index.html` 而不是 `.../site/site/index.html`。
def _file_path_with_product_root_stripped(root_path: Path, file_path: Path) -> Path:
    file_parts = file_path.parts
    root_parts = root_path.parts
    max_size = min(len(file_parts) - 1, len(root_parts))
    for size in range(max_size, 0, -1):
        if file_parts[:size] == root_parts[-size:]:
            return Path(*file_parts[size:])
    return file_path


# LLM: _required_product_ref_candidates keeps path resolution flat and literal.
# 函数用途: 将一个 required 文件名解析为候选业务产物路径；绝对路径原样返回，相对路径只绑定到 product root。
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


# LLM: _compact_list renders short packet arrays for handoff markdown.
# 函数用途: 把列表值压成一行；空值显示 none。
def _compact_list(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(str(item) for item in value)
