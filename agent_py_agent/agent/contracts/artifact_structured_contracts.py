
from __future__ import annotations

import re
from pathlib import Path

from ..common.value_parsing import sequence_strings
from .artifact_acceptance_models import ArtifactFinding


def json_contract_findings(path: Path, value: object, contract: dict[str, object]) -> list[ArtifactFinding]:
    required_fields = sequence_strings(contract.get("required_fields"))
    if not required_fields or not isinstance(value, dict):
        return []
    missing = [field for field in required_fields if field not in value]
    if not missing:
        return []
    return [
        ArtifactFinding(
            code="JSON_REQUIRED_FIELDS_MISSING",
            severity="hard",
            message="JSON artifact is missing required fields.",
            location=str(path),
            value=",".join(missing),
        )
    ]


def csv_contract_findings(
    path: Path,
    rows: list[list[str]],
    contract: dict[str, object],
) -> list[ArtifactFinding]:
    required_columns = sequence_strings(contract.get("required_columns"))
    if not required_columns:
        return []
    header = [cell.strip() for cell in rows[0]]
    missing = [column for column in required_columns if column not in header]
    if not missing:
        return []
    return [
        ArtifactFinding(
            code="CSV_REQUIRED_COLUMNS_MISSING",
            severity="hard",
            message="CSV artifact is missing required columns.",
            location=str(path),
            value=",".join(missing),
        )
    ]


def text_size_findings(path: Path, text: str, contract: dict[str, object]) -> list[ArtifactFinding]:
    min_size = positive_int(contract.get("min_size"))
    if min_size <= 0 or len(text.encode("utf-8")) >= min_size:
        return []
    return [
        ArtifactFinding(
            code="ARTIFACT_TOO_SMALL",
            severity="hard",
            message="Artifact is smaller than required min_size.",
            location=str(path),
            value=str(len(text.encode("utf-8"))),
        )
    ]


def markdown_section_findings(path: Path, text: str, contract: dict[str, object]) -> list[ArtifactFinding]:
    required_sections = sequence_strings(contract.get("required_sections"))
    if not required_sections:
        return []
    headings = set(markdown_headings(text))
    normalized_headings = {_normalized_markdown_heading(section) for section in headings}
    missing = [
        section
        for section in required_sections
        if _normalized_markdown_heading(section) not in normalized_headings
    ]
    if not missing:
        return []
    return [
        ArtifactFinding(
            code="MARKDOWN_REQUIRED_SECTION_MISSING",
            severity="hard",
            message="Markdown artifact is missing required sections.",
            location=str(path),
            value=",".join(missing),
        )
    ]


def _has_body_beyond_headings(text: str) -> bool:
    """是否含标题之外的正文行。纯标题/大纲/搬运的单标题短笔记(如 "# TODO: 修登录bug"
    整文件就一行,标题即完整内容)不算"末尾空标题"——organize 整理任务实锤:模型把
    原始单标题行文件分类搬运,内容未改,却被 MARKDOWN_TRAILING_EMPTY_HEADING hard 拦,
    误伤完整交付。只有"有正文段落且末尾停在空标题"才是真问题。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not re.match(r"^\s{0,3}#{1,6}\s", line):
            return True
    return False


def markdown_integrity_findings(path: Path, text: str) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    tail = _last_significant_line(text)
    if tail and re.match(r"^\s{0,3}#{1,6}\s+\S.*$", tail) and _has_body_beyond_headings(text):
        findings.append(
            ArtifactFinding(
                code="MARKDOWN_TRAILING_EMPTY_HEADING",
                severity="hard",
                message="Markdown artifact ends immediately after a heading.",
                location=str(path),
                value=tail[:200],
            )
        )
    if _unclosed_code_fence(text):
        findings.append(
            ArtifactFinding(
                code="MARKDOWN_CODE_FENCE_UNCLOSED",
                severity="hard",
                message="Markdown artifact has an unclosed fenced code block.",
                location=str(path),
            )
        )
    if _unfinished_placeholder_marker(text):
        findings.append(
            ArtifactFinding(
                code="MARKDOWN_UNFINISHED_PLACEHOLDER",
                severity="hard",
                message="Markdown artifact is an unfinished placeholder (待完成/待填写 with no substantive body).",
                location=str(path),
            )
        )
    return findings


# 未完成占位符标记:仅匹配 my-agent 系统【兜底生成】的"产物未生成"专用签名
# (agent_run_workspace 在子代理没真产出时写的 Final Report 占位)。刻意不含
# __FILL__/TODO/待填写 等通用占位符——那些属于"文档质量低"范畴,由 document_quality
# 软门按 warning 处理(不阻断),保持"让模型发挥不强行卡"。这里只 hard 拦"整篇就是
# 系统兜底占位、零实质产出"的死交付。
_UNFINISHED_MARKERS = ("待完成后填写",)


def _unfinished_placeholder_marker(text: str) -> bool:
    """产物是"未完成占位"判定:含明确占位短语,且去掉标题/元数据/占位行后实质正文极少。
    双条件避免误判正常长报告里偶尔出现的"待补充"一词。"""
    if not any(marker in text for marker in _UNFINISHED_MARKERS):
        return False
    substantive = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or re.match(r"^#{1,6}\s", stripped):
            continue
        if re.match(r"^[-*]\s*\S+\s*[:：]", stripped):  # 元数据列表项 (- key: value)
            continue
        if any(marker in stripped for marker in _UNFINISHED_MARKERS):
            continue
        substantive += len(stripped)
    return substantive < 80


def markdown_local_reference_findings(
    path: Path,
    text: str,
    *,
    reference_roots: tuple[Path | str, ...] = (),
) -> list[ArtifactFinding]:
    roots = _existing_reference_roots(reference_roots)
    if not roots:
        return []
    findings: list[ArtifactFinding] = []
    seen: set[str] = set()
    for ref, line_no in _markdown_local_reference_candidates(text):
        normalized = ref.replace("\\", "/").strip()
        status = _local_reference_status(normalized, roots)
        if normalized in seen or status is not False:
            continue
        seen.add(normalized)
        findings.append(
            ArtifactFinding(
                code="MARKDOWN_LOCAL_REF_MISSING",
                severity="hard",
                message="Markdown references a concrete local file path that does not exist under the configured reference roots.",
                location=f"{path}:{line_no}",
                value=normalized,
            )
        )
    return findings


def _last_significant_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _unclosed_code_fence(text: str) -> bool:
    fence_count = 0
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fence_count += 1
    return fence_count % 2 == 1


def markdown_headings(text: str) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append(match.group(1).strip())
    return headings


_TREE_ENTRY_RE = re.compile(r"^(?P<prefix>(?:[│| ]{4})*)(?:├──|└──|\\+--|`--)\s+(?P<name>[^#]+?)(?:\s+#.*)?\s*$")
_PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_:/\\.-])(?P<path>[A-Za-z0-9_.@+~-]+(?:[\\/][A-Za-z0-9_.@+~-]+)+)(?![A-Za-z0-9_/\\.-])"
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _markdown_local_reference_candidates(text: str) -> list[tuple[str, int]]:
    candidates: list[tuple[str, int]] = []
    in_fence = False
    tree_stack: dict[int, str] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            tree_stack = {}
            continue
        if in_fence:
            tree_refs = _tree_line_references(line, tree_stack)
            candidates.extend((ref, line_no) for ref in tree_refs)
            continue
        candidates.extend((ref, line_no) for ref in _path_token_references(line))
    return candidates


def _tree_line_references(line: str, stack: dict[int, str]) -> list[str]:
    stripped = line.strip()
    if not stripped:
        return []
    root = _tree_root_name(stripped)
    if root:
        stack.clear()
        stack[0] = root
        return []
    match = _TREE_ENTRY_RE.match(stripped)
    if not match:
        return []
    name = _clean_tree_entry_name(match.group("name"))
    if not name:
        return []
    depth = len(match.group("prefix")) // 4 + (1 if 0 in stack else 0)
    for key in [key for key in stack if key >= depth]:
        stack.pop(key, None)
    if name.endswith("/"):
        stack[depth] = name.rstrip("/")
        return []
    parts = [stack[index] for index in sorted(stack) if index < depth]
    parts.append(name)
    ref = "/".join(part for part in parts if part)
    return [ref] if _concrete_file_reference(ref) else []


def _tree_root_name(stripped: str) -> str:
    if not stripped.endswith("/") or any(token in stripped for token in (" ", "\t", "*", "?", "<", ">", "$")):
        return ""
    root = stripped.rstrip("/")
    return root if root and "/" not in root and "\\" not in root else ""


def _clean_tree_entry_name(value: str) -> str:
    name = value.strip()
    if not name or name in {".", "..", "..."}:
        return ""
    return name


def _path_token_references(line: str) -> list[str]:
    refs: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(line):
        ref = match.group("path").strip("`*_.,:;()[]{}")
        if _concrete_file_reference(ref):
            refs.append(ref)
    return refs


def _concrete_file_reference(ref: str) -> bool:
    text = ref.replace("\\", "/").strip()
    if not text or text.startswith(("http://", "https://", "data:", "#")):
        return False
    if "/" not in text:
        return False
    if any(token in text for token in ("...", "*", "?", "<", ">", "$", "{", "}")):
        return False
    if text.startswith("../") or "/../" in text or text.endswith("/.."):
        return False
    return bool(Path(text).suffix)


def _existing_reference_roots(values: tuple[Path | str, ...]) -> list[Path]:
    roots: list[Path] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        root = Path(text).expanduser().resolve(strict=False)
        if root.exists() and root not in roots:
            roots.append(root)
    return roots


def _local_reference_status(ref: str, roots: list[Path]) -> bool | None:
    if _WINDOWS_ABSOLUTE_RE.match(ref):
        return None
    raw = Path(ref).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve(strict=False)
        if not _path_matches_any_root(resolved, roots):
            return None
        return resolved.exists()
    anchored = False
    for root in roots:
        first_segment = ref.replace("\\", "/").split("/", 1)[0]
        if first_segment and not (root / first_segment).exists():
            continue
        anchored = True
        if (root / ref).resolve(strict=False).exists():
            return True
    return False if anchored else None


def _path_matches_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


_LEADING_MARKDOWN_SECTION_RE = re.compile(
    r"^\s*(?:"
    r"(?:[一二三四五六七八九十百千]+|[IVXLCDM]+)\s*[、.．:：)）\\-]\s*"
    r"|(?:第\s*[一二三四五六七八九十百千0-9]+\s*[章节部篇]?)\s*[、.．:：)）\\-]?\s*"
    r"|(?:\d+(?:\.\d+)*)\s*[、.．:：)）\\-]?\s*"
    r")",
    re.I,
)


def _normalized_markdown_heading(value: str) -> str:
    text = str(value or "").strip()
    previous = ""
    while text and text != previous:
        previous = text
        text = _LEADING_MARKDOWN_SECTION_RE.sub("", text).strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[、,，.．:：;；\\-—_（）()\\[\\]【】]+", "", text)
    return text.casefold()


def positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


__all__ = [
    "csv_contract_findings",
    "json_contract_findings",
    "markdown_local_reference_findings",
    "markdown_integrity_findings",
    "markdown_section_findings",
    "text_size_findings",
]
