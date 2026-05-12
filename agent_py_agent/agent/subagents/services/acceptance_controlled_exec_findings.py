# LLM: Controlled-exec acceptance findings stay separate so generic acceptance stays small.
# 模块用途: 校验要求受控 shell 的任务是否真的用过 controlled_exec，并具备 stdout/audit/trash 证据引用。

from __future__ import annotations

import json
import re
from pathlib import Path

from ..reports import AcceptanceReviewFinding

_REF_SCAN_NAMES = frozenset({
    "controlled_exec_refs.json",
    "CONTROLLED_EXEC_E2E_SUMMARY.md",
    "controlled_exec_test_results.md",
})
_MAX_REF_FILE_BYTES = 65536
_MAX_REF_FILES = 24
_TOOL_OUTPUT_REF_PATTERN = re.compile(
    r"(/[^\"'\s<>]+/memory_archive/artifacts/tool_outputs/controlled_exec-[^\"'\s<>]+\.json)"
)


# LLM: controlled_exec_contract_finding blocks fake shell completion when goal required the gateway.
# 函数用途: 目标要求 controlled_exec 时，验收必须看到真实工具使用和 stdout/audit/trash refs。
def controlled_exec_contract_finding(task, output: dict, created_at: float) -> AcceptanceReviewFinding:
    if not _controlled_exec_contract_required(task):
        return AcceptanceReviewFinding(
            name="controlled_exec_contract_satisfied",
            ok=True,
            severity="P1",
            message="当前任务未声明 controlled_exec 硬验收合同。",
            evidence_path=task.output_json,
            created_at=created_at,
        )
    tools_ok = _controlled_exec_tool_proven(task, output)
    refs = _controlled_exec_ref_evidence(task, output)
    refs_ok = all(term in refs.text for term in _controlled_exec_required_refs())
    return AcceptanceReviewFinding(
        name="controlled_exec_contract_satisfied",
        ok=tools_ok and refs_ok,
        severity="P0",
        message=_controlled_exec_contract_message(tools_ok and refs_ok),
        evidence_path=refs.evidence_path or task.output_json,
        created_at=created_at,
    )


# LLM: _controlled_exec_contract_required reads goal/check text only, not model self-claims.
# 函数用途: 判断任务是否要求受控 shell 合同；普通写文件任务不受影响。
def _controlled_exec_contract_required(task) -> bool:
    text = " ".join([str(getattr(task, "goal", "") or ""), *[str(item) for item in task.acceptance_checks]])
    return "controlled_exec" in text.lower()


# LLM: _actual_tool_names merges persisted and output-side tool facts for acceptance checks.
# 函数用途: 汇总 task.used_tools、output.used_tools 和 structured_output.actual_tools，避免只信任一处。
def _actual_tool_names(task, output: dict) -> set[str]:
    tools = set(_string_list(getattr(task, "used_tools", [])))
    tools.update(_string_list(output.get("used_tools", [])))
    structured = output.get("structured_output")
    if isinstance(structured, dict):
        tools.update(_string_list(structured.get("actual_tools", [])))
    return {item.lower() for item in tools}


# LLM: _controlled_exec_tool_proven accepts delegated child execution while still requiring persisted tool facts.
# 函数用途: 判断 controlled_exec 是否由当前任务或其真实子树执行过；coordinator 可以靠下级 leaf 的真实 used_tools 通过验收，但不能只靠口头声明。
def _controlled_exec_tool_proven(task, output: dict) -> bool:
    if "controlled_exec" in _actual_tool_names(task, output):
        return True
    return _descendant_used_controlled_exec(task)


# LLM: _descendant_used_controlled_exec scans only persisted child_ids below the current task.
# 函数用途: 沿 task.child_ids 最多读取少量子任务 task.json/output.json，确认下级是否真实记录过 controlled_exec。
def _descendant_used_controlled_exec(task) -> bool:
    queue = _string_list(getattr(task, "child_ids", []))
    workspace = _task_workspace(task)
    if not queue or workspace is None:
        return False
    seen: set[str] = set()
    scanned = 0
    while queue and scanned < _MAX_REF_FILES:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        record = _read_child_record(workspace, run_id)
        if not record:
            continue
        scanned += 1
        if _record_used_controlled_exec(record):
            return True
        queue.extend(child_id for child_id in _string_list(record.get("child_ids", [])) if child_id not in seen)
    return False


# LLM: _task_workspace derives the legacy subagent workspace from task_dir without touching manager state.
# 函数用途: 根据当前任务目录定位 sibling child task.json；路径异常时返回 None，验收保持保守失败。
def _task_workspace(task) -> Path | None:
    try:
        task_dir = Path(str(getattr(task, "task_dir", "") or "")).expanduser()
    except OSError:
        return None
    if not str(task_dir):
        return None
    return task_dir.parent


# LLM: _read_child_record keeps descendant evidence bounded to the child run's small JSON files.
# 函数用途: 读取子任务持久化记录；只按 child_id 精确打开 task.json/output.json，不做 glob 扫描。
def _read_child_record(workspace: Path, run_id: str) -> dict:
    child_dir = workspace / run_id
    record = _read_json(child_dir / "task.json")
    output = _read_json(child_dir / "output.json")
    if isinstance(output, dict):
        record.setdefault("output", output)
    return record


# LLM: _record_used_controlled_exec recognizes tool facts from task.json and output.json only.
# 函数用途: 在子任务持久化记录里查 used_tools/structured_output.actual_tools，避免相信普通 summary 文本。
def _record_used_controlled_exec(record: dict) -> bool:
    tools = set(_string_list(record.get("used_tools", [])))
    output = record.get("output")
    if isinstance(output, dict):
        tools.update(_string_list(output.get("used_tools", [])))
        structured = output.get("structured_output")
        if isinstance(structured, dict):
            tools.update(_string_list(structured.get("actual_tools", [])))
    return "controlled_exec" in {item.lower() for item in tools}


# LLM: _read_json is intentionally tiny and failure-closed for acceptance scanning.
# 函数用途: 安全读取小 JSON；不存在、过大或解析失败都返回空 dict。
def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > _MAX_REF_FILE_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# LLM: _RefEvidence keeps bounded ref-scan text with the best evidence path for audit.
# 类用途: 保存 controlled_exec 验收时找到的小型 refs 文本和来源路径；不承载大工具输出正文。
class _RefEvidence:
    text: str
    evidence_path: str

    # LLM: _RefEvidence.__init__ stores only bounded evidence text and the first useful ref path.
    # 函数用途: 初始化 controlled_exec 验收证据容器，不读取或扩展任何外部文件。
    def __init__(self, text: str, evidence_path: str = "") -> None:
        self.text = text
        self.evidence_path = evidence_path


# LLM: _controlled_exec_ref_evidence checks output plus bounded refs files instead of trusting summaries only.
# 函数用途: 汇总 output.json、task_dir 和 allowed_write_roots 下的小型 refs/summary 文件，验证 stdout/audit/trash 证据字段。
def _controlled_exec_ref_evidence(task, output: dict) -> _RefEvidence:
    chunks = [_output_contract_text(output)]
    evidence_path = ""
    artifact_refs = _controlled_exec_artifact_ref_paths(chunks[0])
    file_evidence = _collect_ref_file_evidence(task)
    chunks.extend(file_evidence.chunks)
    artifact_refs.extend(file_evidence.artifact_refs)
    evidence_path = file_evidence.evidence_path
    artifact_evidence = _collect_artifact_ref_evidence(artifact_refs)
    chunks.extend(artifact_evidence.chunks)
    evidence_path = evidence_path or artifact_evidence.evidence_path
    return _RefEvidence("\n".join(chunks), evidence_path)


# LLM: _CollectedRefEvidence bundles bounded chunks, artifact refs, and first evidence path.
# 类用途: 在 controlled_exec refs 扫描阶段传递小型证据包，避免函数参数膨胀。
class _CollectedRefEvidence:
    chunks: list[str]
    artifact_refs: list[str]
    evidence_path: str

    # LLM: _CollectedRefEvidence.__init__ packages one bounded scan result for later merging.
    # 函数用途: 初始化 refs 文件扫描结果，保存短文本块、artifact refs 和首个证据路径。
    def __init__(self, chunks: list[str], artifact_refs: list[str], evidence_path: str = "") -> None:
        self.chunks = chunks
        self.artifact_refs = artifact_refs
        self.evidence_path = evidence_path


# LLM: _collect_ref_file_evidence reads only known small refs files in task-approved roots.
# 函数用途: 收集 controlled_exec_refs/summary 文件中的短证据文本和二级 artifact 引用。
def _collect_ref_file_evidence(task) -> _CollectedRefEvidence:
    chunks: list[str] = []
    artifact_refs: list[str] = []
    evidence_path = ""
    for path in _controlled_exec_ref_files(task):
        text = _read_small_text(path)
        if not text:
            continue
        lowered = text.lower()
        chunks.append(lowered)
        artifact_refs.extend(_controlled_exec_artifact_ref_paths(text))
        if not evidence_path:
            evidence_path = str(path)
    return _CollectedRefEvidence(chunks, artifact_refs, evidence_path)


# LLM: _collect_artifact_ref_evidence follows explicit tool-output refs with the same bounded read policy.
# 函数用途: 读取 controlled_exec tool artifact 中的 stdout_ref/audit_ref 等字段，不做目录扫描。
def _collect_artifact_ref_evidence(artifact_refs: list[str]) -> _CollectedRefEvidence:
    chunks: list[str] = []
    evidence_path = ""
    for path in _unique_paths(Path(item) for item in artifact_refs)[:_MAX_REF_FILES]:
        text = _read_small_text(path)
        if not text:
            continue
        chunks.append(text.lower())
        evidence_path = evidence_path or str(path)
    return _CollectedRefEvidence(chunks, [], evidence_path)


# LLM: _output_contract_text keeps direct output checks cheap and deterministic.
# 函数用途: 将 output.json 里的短证据字段压成字符串，用于查找 refs 字段名，不读取大 artifact。
def _output_contract_text(output: dict) -> str:
    return repr(output).lower()


# LLM: _controlled_exec_artifact_ref_paths follows only explicit small tool-output refs, never broad globs.
# 函数用途: 从 output/refs 文本中提取 controlled_exec tool artifact 路径，用于读取 stdout_ref/audit_ref 等二级证据。
def _controlled_exec_artifact_ref_paths(text: str) -> list[str]:
    return list(dict.fromkeys(_TOOL_OUTPUT_REF_PATTERN.findall(str(text or ""))))


# LLM: _controlled_exec_ref_files scans only known small evidence filenames in task-scoped roots.
# 函数用途: 在 task_dir 与 allowed_write_roots 中找 controlled_exec refs/summary 文件，避免扫描大日志或任意文件。
def _controlled_exec_ref_files(task) -> list[Path]:
    paths: list[Path] = []
    for root in _ref_roots(task):
        if not root.exists():
            continue
        paths.extend(_matching_ref_files(root))
        if len(paths) >= _MAX_REF_FILES:
            break
    return _unique_paths(paths[:_MAX_REF_FILES])


# LLM: _ref_roots keeps controlled_exec evidence discovery scoped to task-owned and granted output roots.
# 函数用途: 返回受控扫描根目录，只包含当前任务目录和显式 allowed_write_roots，避免扫整个工作区。
def _ref_roots(task) -> list[Path]:
    roots = [Path(str(getattr(task, "task_dir", "") or ""))]
    allowed = getattr(task, "allowed_write_roots", [])
    if isinstance(allowed, (list, tuple, set)):
        roots.extend(Path(str(item)) for item in allowed if str(item))
    return _unique_paths(root for root in roots if str(root))


# LLM: _matching_ref_files avoids broad content search and only accepts known controlled_exec evidence names.
# 函数用途: 从一个根目录内找固定文件名的 refs/summary 文件，数量由调用方限制。
def _matching_ref_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.name in _REF_SCAN_NAMES else []
    paths: list[Path] = []
    for name in _REF_SCAN_NAMES:
        candidates = [root / name, *root.glob(f"*/{name}"), *root.glob(f"*/*/{name}")]
        paths.extend(path for path in candidates if path.is_file())
    return paths


# LLM: _read_small_text protects acceptance from accidentally loading huge tool outputs.
# 函数用途: 只读取小型 refs 文件；超过上限的文件跳过，避免验收把大日志塞进内存或上下文。
def _read_small_text(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_REF_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# LLM: _unique_paths keeps scan order deterministic while removing duplicates.
# 函数用途: 对候选根/候选文件去重，避免重复读取同一证据文件。
def _unique_paths(paths) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


# LLM: _controlled_exec_required_refs centralizes refs that prove shell gateway behavior.
# 函数用途: controlled_exec 验收必须有 stdout、audit 和 trash 证据引用，防止只口头声明。
def _controlled_exec_required_refs() -> tuple[str, ...]:
    return ("stdout_ref", "audit_ref", "trash_manifest_ref")


# LLM: _controlled_exec_contract_message keeps the finding constructor compact.
# 函数用途: 生成受控 exec 验收 finding 文案，让主函数保持短小。
def _controlled_exec_contract_message(ok: bool) -> str:
    if ok:
        return "controlled_exec 工具和关键 refs 均可追溯。"
    return "任务声明 controlled_exec 验收合同，但缺少真实 controlled_exec 工具记录或 stdout/audit/trash refs。"


# LLM: _string_list keeps this module independent from generic acceptance_findings imports.
# 函数用途: 将工具名字段归一成字符串列表，避免循环引用通用验收模块。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]
