
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
    missing = [section for section in required_sections if section not in headings]
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


def markdown_headings(text: str) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append(match.group(1).strip())
    return headings


def positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


__all__ = [
    "csv_contract_findings",
    "json_contract_findings",
    "markdown_section_findings",
    "text_size_findings",
]
