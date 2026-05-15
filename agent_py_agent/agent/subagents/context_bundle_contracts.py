# LLM: Context bundle contracts hold task_packet and deliverable file contract helpers.
# 模块用途: 从 SubAgentTask 生成产物合同、结构化 task_packet 和 Markdown 展示行。

from __future__ import annotations

from pathlib import Path

from .context_bundle_refs import safe_string_ref, workspace_refs
from .models import SubAgentTask
from .required_file_terms import forbidden_file_terms_from_text, required_file_terms_from_text

_CONTRACT_FILE_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".css",
    ".html",
    ".htm",
}


# LLM: output_contract tells the runner where durable reports and machine output must land.
# 函数用途: 约定子代理最终报告、结构化输出、证据、测试和产物引用，避免只返回自然语言。
def output_contract(task: SubAgentTask) -> dict[str, object]:
    return {
        "final_report_ref": safe_string_ref(task, "agent_run_final_report_md") or safe_string_ref(task, "debrief_file"),
        "runner_result_ref": safe_string_ref(task, "runner_result_json"),
        "output_json_ref": safe_string_ref(task, "output_json"),
        "required_files": required_file_contract(task),
        "forbidden_files": forbidden_file_contract(task),
        "file_contract_source": "task_text_positive_negative_extraction",
        "evidence_refs_required": True,
        "tests_ref_style": "refs_only_with_working_dir",
        "artifact_refs_required": True,
    }


# LLM: task_packet is the compact typed handoff child runners should trust before prose.
# 函数用途: 生成子代理/接管代理优先读取的结构化任务包，避免从自然语言摘要里猜路径。
def task_packet(task: SubAgentTask) -> dict[str, object]:
    refs = workspace_refs(task)
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
            "required_files": required_file_contract(task),
            "forbidden_files": forbidden_file_contract(task),
            "source": "task_text_positive_negative_extraction",
        },
        "write_contract": {
            "allowed_write_roots": list(task.allowed_write_roots or []),
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


# LLM: required_file_contract extracts exact deliverable filenames from task text without reading artifacts.
# 函数用途: 从任务文本和文件级写入根生成必需文件清单；不读取产物正文。
def required_file_contract(task: SubAgentTask) -> list[str]:
    return _dedupe_file_terms(
        [
            *(
                term
                for text in _file_contract_texts(task)
                for term in required_file_terms_from_text(text, extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html")
            ),
            *_file_level_write_root_terms(task),
        ]
    )


# LLM: forbidden_file_contract extracts negative filename examples so descendants do not treat them as outputs.
# 函数用途: 从任务文本里生成禁止文件清单，明确反例不能创建。
def forbidden_file_contract(task: SubAgentTask) -> list[str]:
    return _dedupe_file_terms(
        term
        for text in _file_contract_texts(task)
        for term in forbidden_file_terms_from_text(text, extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html")
    )


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
        f"- forbidden_files: {_compact_list(file_contract.get('forbidden_files'))}",
        f"- allowed_write_roots: {_compact_list(write_contract.get('allowed_write_roots'))}",
        f"- allowed_tools: {_compact_list(tool_contract.get('allowed_tools'))}",
    ]


# LLM: _context_bundle_json_ref derives the standard context bundle path from agent_run_workspace.
# 函数用途: 只从 refs 构造路径字符串，不访问文件系统。
def _context_bundle_json_ref(refs: dict[str, str]) -> str:
    workspace = refs.get("agent_run_workspace", "")
    return str(Path(workspace) / "context_bundle.json") if workspace else ""


# LLM: _file_contract_texts keeps contract extraction bounded to lightweight persisted task facts.
# 函数用途: 收集可用于文件契约的短文本字段，不读取 output/artifact 正文。
def _file_contract_texts(task: SubAgentTask) -> list[str]:
    values: list[object] = [task.goal, task.thought, getattr(task, "description", ""), *(task.acceptance_checks or [])]
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


# LLM: _file_level_write_root_terms turns explicit product file grants into required file contracts.
# 函数用途: 真实 E2E 里父级常只传 `/.../index.html` 写入根；这里补出 `index.html`，避免 Context Gate 误挡。
def _file_level_write_root_terms(task: SubAgentTask) -> list[str]:
    terms: list[str] = []
    task_dir = Path(str(getattr(task, "task_dir", "") or ""))
    for raw in getattr(task, "allowed_write_roots", []) or []:
        path = Path(str(raw or "").strip().replace("\\", "/"))
        if not _is_contract_file_path(path) or _is_internal_task_file(path, task_dir):
            continue
        _append_file_root_term(terms, path.name)
        if len(path.parts) >= 2:
            _append_file_root_term(terms, "/".join(path.parts[-2:]))
    return terms


# LLM: _is_contract_file_path keeps directory roots out of required_files.
# 函数用途: 只把带受支持后缀的具体文件路径加入文件合同。
def _is_contract_file_path(path: Path) -> bool:
    return bool(path.name and path.suffix.lower() in _CONTRACT_FILE_SUFFIXES)


# LLM: _is_internal_task_file filters run-private output/checkpoint files from product contracts.
# 函数用途: 子代理自己的 task_dir/output.json 不是用户产物，不能因为可写就进入 required_files。
def _is_internal_task_file(path: Path, task_dir: Path) -> bool:
    if not str(task_dir):
        return False
    try:
        return path.resolve().is_relative_to(task_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


# LLM: _append_file_root_term preserves basename and scoped relative forms without duplicates.
# 函数用途: 让 `index.html` 和 `dir/index.html` 都能匹配不同自然语言写法。
def _append_file_root_term(terms: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in terms:
        terms.append(text)


# LLM: _compact_list renders short packet arrays for handoff markdown.
# 函数用途: 把列表值压成一行；空值显示 none。
def _compact_list(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(str(item) for item in value)
