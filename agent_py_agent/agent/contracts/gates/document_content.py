
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from ...common.value_parsing import sequence_strings
from ..recovery import RecoveryAction
from .models import GateDecision, GateFinding

_DEFAULT_PLACEHOLDER_TOKENS = ("__FILL", "__TODO__", "PLACEHOLDER", "TODO", "TBD", "TO_BE_FILLED")
_FINDING_MESSAGES = {
    "DOCUMENT_SECTION_TOO_THIN": "Document section content is thinner than the declared contract.",
    "DOCUMENT_PLACEHOLDER_RATIO_EXCEEDED": "Document contains too much placeholder content.",
}


def evaluate_document_content_quality_gate(
    *,
    artifact_ref: str,
    workspace_root: Path | str | None = None,
    contract: dict[str, Any] | None = None,
) -> GateDecision:
    path = _document_path(artifact_ref, workspace_root)
    if path is None:
        return GateDecision.repair(
            "document_content_quality",
            [_finding("DOCUMENT_PATH_OUTSIDE_WORKSPACE", artifact_ref=artifact_ref)],
            recommended_action=RecoveryAction.REPAIR_DOCUMENT_ARTIFACT.value,
        )
    findings = document_content_quality_findings(path, workspace_root=workspace_root, contract=contract or {})
    if findings:
        return GateDecision.repair(
            "document_content_quality",
            findings,
            recommended_action=RecoveryAction.REPAIR_DOCUMENT_ARTIFACT.value,
            evidence={"artifact_ref": str(path), "finding_count": len(findings)},
        )
    return GateDecision.allow("document_content_quality", evidence={"artifact_ref": str(path)})


def document_content_quality_findings(
    path: Path,
    *,
    workspace_root: Path | str | None = None,
    contract: dict[str, Any] | None = None,
) -> list[GateFinding]:
    if _outside_workspace(path, workspace_root):
        return [_finding("DOCUMENT_PATH_OUTSIDE_WORKSPACE", artifact_ref=str(path))]
    if not path.exists():
        return [_finding("DOCUMENT_ARTIFACT_MISSING", artifact_ref=str(path))]
    contract = contract or {}
    try:
        facts = extract_document_content_facts(path)
    except OSError as exc:
        return [_finding("DOCUMENT_READ_FAILED", artifact_ref=str(path), details={"current_state": {"error": str(exc)}})]
    except (BadZipFile, ParseError) as exc:
        return [_finding("DOCUMENT_FORMAT_INVALID", artifact_ref=str(path), details={"current_state": {"error": str(exc)}})]
    return [
        *_required_section_findings(path, facts, contract),
        *_placeholder_findings(path, facts, contract),
        *_content_unit_findings(path, facts, contract),
        *_text_layer_findings(path, facts, contract),
    ]


def _required_section_findings(path: Path, facts: DocumentContentFacts, contract: dict[str, Any]) -> list[GateFinding]:
    required = sequence_strings(contract.get("required_sections"))
    min_chars = _int_value(contract.get("min_chars_per_section"))
    findings: list[GateFinding] = []
    sections = {_normalized_markdown_heading(section.heading): section for section in facts.sections}
    for name in required:
        section = sections.get(_normalized_markdown_heading(name))
        if section is None:
            findings.append(
                _finding(
                    "DOCUMENT_SECTION_MISSING",
                    artifact_ref=str(path),
                    details={
                        "location": {"section": name},
                        "current_state": {"present": False},
                        "required_state": {"section": name},
                    },
                )
            )
            continue
        chars = meaningful_char_count(section.body)
        if min_chars and chars < min_chars:
            findings.append(
                _finding(
                    "DOCUMENT_SECTION_TOO_THIN",
                    artifact_ref=str(path),
                    details={
                        "location": {
                            "section": section.heading,
                            "start_line": section.start_line,
                            "end_line": section.end_line,
                        },
                        "current_state": {"text_chars": chars},
                        "required_state": {"min_chars_per_section": min_chars},
                    },
                )
            )
    return findings


def _placeholder_findings(path: Path, facts: DocumentContentFacts, contract: dict[str, Any]) -> list[GateFinding]:
    max_ratio = _float_value(contract.get("max_placeholder_ratio"), default=0.0)
    if max_ratio <= 0:
        return []
    ratio, hits = _placeholder_ratio(facts.text, _placeholder_tokens(contract))
    if ratio <= max_ratio:
        return []
    return [
        _finding(
            "DOCUMENT_PLACEHOLDER_RATIO_EXCEEDED",
            artifact_ref=str(path),
            details={
                "current_state": {"placeholder_ratio": ratio, "placeholder_hits": hits[:10]},
                "required_state": {"max_placeholder_ratio": max_ratio},
            },
        )
    ]


def _content_unit_findings(path: Path, facts: DocumentContentFacts, contract: dict[str, Any]) -> list[GateFinding]:
    minimum = _int_value(contract.get("min_content_units"))
    if not minimum:
        return []
    units = facts.paragraph_count + facts.table_count + facts.image_count + facts.code_block_count
    if units >= minimum:
        return []
    return [
        _finding(
            "DOCUMENT_CONTENT_UNITS_TOO_FEW",
            artifact_ref=str(path),
            details={
                "current_state": {
                    "content_units": units,
                    "paragraphs": facts.paragraph_count,
                    "tables": facts.table_count,
                    "images": facts.image_count,
                    "code_blocks": facts.code_block_count,
                },
                "required_state": {"min_content_units": minimum},
            },
        )
    ]


def _text_layer_findings(path: Path, facts: DocumentContentFacts, contract: dict[str, Any]) -> list[GateFinding]:
    if path.suffix.lower() != ".pdf" or not bool(contract.get("require_text_layer", False)):
        return []
    if facts.has_text_layer:
        return []
    return [
        _finding(
            "PDF_TEXT_LAYER_MISSING",
            artifact_ref=str(path),
            details={
                "current_state": {"has_text_layer": False, "page_count": facts.page_count},
                "required_state": {"require_text_layer": True},
            },
        )
    ]


def _document_path(ref: str, workspace_root: Path | str | None) -> Path | None:
    path = Path(str(ref or "")).expanduser()
    if not str(path):
        return None
    if path.is_absolute():
        resolved = path.resolve(strict=False)
    elif workspace_root is not None:
        resolved = (Path(workspace_root) / path).resolve(strict=False)
    else:
        resolved = path.resolve(strict=False)
    return None if _outside_workspace(resolved, workspace_root) else resolved


def _outside_workspace(path: Path, workspace_root: Path | str | None) -> bool:
    if workspace_root is None:
        return False
    try:
        path.resolve(strict=False).relative_to(Path(workspace_root).resolve(strict=False))
        return False
    except ValueError:
        return True


def _finding(
    code: str,
    *,
    artifact_ref: str = "",
    details: dict[str, Any] | None = None,
) -> GateFinding:
    details = details or {}
    return GateFinding(
        code,
        severity="P0",
        message=_FINDING_MESSAGES.get(code, ""),
        evidence={
            "priority": 0,
            "artifact_ref": artifact_ref,
            "location": details.get("location") or {},
            "current_state": details.get("current_state") or {},
            "required_state": details.get("required_state") or {},
            "repair_action": "repair_document_content",
            "required_tool_calls": ["read_artifact", "rewrite_artifact"],
            "retryable": True,
        },
    )


def _placeholder_tokens(contract: dict[str, Any]) -> list[str]:
    tokens = [*_DEFAULT_PLACEHOLDER_TOKENS, *sequence_strings(contract.get("placeholder_tokens"))]
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        normalized = token.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
    return unique


def _placeholder_ratio(text: str, tokens: list[str]) -> tuple[float, list[str]]:
    total = max(meaningful_char_count(text), 1)
    hits = [token for token in tokens for _ in re.finditer(re.escape(token), text, re.I)]
    chars = sum(len(item) for item in hits)
    return chars / total, hits


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


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "DocumentContentFacts",
    "DocumentSection",
    "document_content_quality_findings",
    "evaluate_document_content_quality_gate",
    "extract_document_content_facts",
]


# ---- 原 document/content_extractors.py 并入 ----
import re
import zipfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True)
class DocumentSection:
    heading: str
    body: str
    start_line: int = 0
    end_line: int = 0


@dataclass(frozen=True)
class DocumentContentFacts:
    text: str
    sections: tuple[DocumentSection, ...]
    paragraph_count: int = 0
    table_count: int = 0
    image_count: int = 0
    code_block_count: int = 0
    page_count: int = 0
    has_text_layer: bool = True


def extract_document_content_facts(path: Path) -> DocumentContentFacts:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return _markdown_facts(path.read_text(encoding="utf-8", errors="replace"))
    if suffix in {".html", ".htm"}:
        return _html_facts(path.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".docx":
        return _docx_facts(path)
    if suffix == ".pdf":
        return _pdf_facts(path)
    return _text_facts(path.read_text(encoding="utf-8", errors="replace"))


def meaningful_char_count(text: str) -> int:
    return sum(1 for char in text if not char.isspace() and char not in "#*_`|-")


def _markdown_facts(text: str) -> DocumentContentFacts:
    lines = text.splitlines()
    heading_rows = [(index + 1, _heading_text(line)) for index, line in enumerate(lines) if _heading_text(line)]
    sections = _sections_from_heading_rows(lines, heading_rows)
    return DocumentContentFacts(
        text=text,
        sections=tuple(sections) or (DocumentSection("document", text, 1, len(lines)),),
        paragraph_count=_paragraph_count(text),
        table_count=sum(1 for line in lines if "|" in line),
        code_block_count=text.count("```") // 2,
    )


def _html_facts(text: str) -> DocumentContentFacts:
    plain = _strip_tags(text)
    heading_rows = [
        (1, unescape(match.group(1)).strip())
        for match in re.finditer(r"<h[1-6][^>]*>(.*?)</h[1-6]>", text, re.I | re.S)
        if match.group(1).strip()
    ]
    sections = [DocumentSection(heading, _html_section_body(text, heading), 1, 1) for _, heading in heading_rows]
    return DocumentContentFacts(
        text=plain,
        sections=tuple(sections) or (DocumentSection("document", plain, 1, 1),),
        paragraph_count=len(re.findall(r"<p\b", text, re.I)) or _paragraph_count(plain),
        table_count=len(re.findall(r"<table\b", text, re.I)),
        image_count=len(re.findall(r"<img\b", text, re.I)),
        code_block_count=len(re.findall(r"<pre\b|<code\b", text, re.I)),
    )


def _docx_facts(path: Path) -> DocumentContentFacts:
    with zipfile.ZipFile(path) as docx:
        xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[tuple[str, str]] = []
    table_count = len(root.findall(".//w:tbl", ns))
    for para in root.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in para.findall(".//w:t", ns)).strip()
        if not text:
            continue
        style = ""
        style_node = para.find(".//w:pStyle", ns)
        if style_node is not None:
            style = str(style_node.attrib.get(f"{{{ns['w']}}}val") or "")
        paragraphs.append((style, text))
    sections = _sections_from_styled_paragraphs(paragraphs)
    plain = "\n\n".join(text for _, text in paragraphs)
    return DocumentContentFacts(
        text=plain,
        sections=tuple(sections) or (DocumentSection("document", plain, 1, len(paragraphs)),),
        paragraph_count=len(paragraphs),
        table_count=table_count,
    )


def _pdf_facts(path: Path) -> DocumentContentFacts:
    data = path.read_bytes()
    page_count = data.count(b"/Type /Page")
    text = _pdf_text(data)
    return DocumentContentFacts(
        text=text,
        sections=(DocumentSection("document", text, 1, max(page_count, 1)),),
        paragraph_count=_paragraph_count(text),
        page_count=page_count,
        has_text_layer=bool(meaningful_char_count(text)),
    )


def _text_facts(text: str) -> DocumentContentFacts:
    return DocumentContentFacts(
        text=text,
        sections=(DocumentSection("document", text, 1, len(text.splitlines())),),
        paragraph_count=_paragraph_count(text),
    )


def _sections_from_heading_rows(lines: list[str], heading_rows: list[tuple[int, str]]) -> list[DocumentSection]:
    return [
        DocumentSection(heading, "\n".join(lines[line_no : next_line - 1]), line_no, next_line - 1)
        for (line_no, heading), next_line in zip(
            heading_rows,
            [*[row[0] for row in heading_rows[1:]], len(lines) + 1],
            strict=False,
        )
    ]


def _sections_from_styled_paragraphs(paragraphs: list[tuple[str, str]]) -> list[DocumentSection]:
    sections: list[DocumentSection] = []
    current_heading = ""
    current_body: list[str] = []
    start = 1
    for index, (style, text) in enumerate(paragraphs, start=1):
        if not _is_heading_style(style):
            current_body.extend([text] if current_heading else [])
            continue
        if current_heading:
            sections.append(DocumentSection(current_heading, "\n".join(current_body), start, index - 1))
        current_heading = text
        current_body = []
        start = index
    if current_heading:
        sections.append(DocumentSection(current_heading, "\n".join(current_body), start, len(paragraphs)))
    return sections


def _is_heading_style(style: str) -> bool:
    return style.lower().startswith("heading")


def _html_section_body(html: str, heading: str) -> str:
    index = html.find(heading)
    if index < 0:
        return ""
    next_heading = re.search(r"<h[1-6][^>]*>", html[index + len(heading) :], re.I)
    end = index + len(heading) + next_heading.start() if next_heading else len(html)
    return _strip_tags(html[index:end])


def _heading_text(line: str) -> str:
    match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
    return match.group(1).strip() if match else ""


def _strip_tags(value: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", value))


def _pdf_text(data: bytes) -> str:
    text = data.decode("latin-1", errors="ignore")
    chunks = re.findall(r"\(([^()]*)\)\s*Tj", text)
    chunks.extend(match.replace("\\)", ")").replace("\\(", "(") for match in re.findall(r"\(([^()]*)\)", text))
    return "\n".join(chunks)


def _paragraph_count(text: str) -> int:
    return len([part for part in re.split(r"\n\s*\n", text.strip()) if meaningful_char_count(part)])
