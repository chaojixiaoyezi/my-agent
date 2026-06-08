
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from ....common.value_parsing import sequence_strings
from ...recovery_actions import RecoveryAction
from ..models import GateDecision, GateFinding
from .content_extractors import (
    DocumentContentFacts,
    DocumentSection,
    extract_document_content_facts,
    meaningful_char_count,
)

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
