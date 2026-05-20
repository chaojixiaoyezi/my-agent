# LLM: Structured artifact contract helpers validate declared fields for text, JSON, and CSV artifacts.
# 模块用途: 承载 Markdown/JSON/CSV 的通用合同字段检查，避免 artifact_acceptance.py 继续膨胀。

from __future__ import annotations

import re
from pathlib import Path

from .artifact_acceptance_models import ArtifactFinding


# LLM: json_contract_findings checks top-level fields requested by generic artifact contracts.
# 函数用途: 根据 required_fields 验证 JSON 对象字段存在；数组报告不做顶层字段验收。
def json_contract_findings(path: Path, value: object, contract: dict[str, object]) -> list[ArtifactFinding]:
    required_fields = string_list(contract.get("required_fields"))
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


# LLM: csv_contract_findings checks table headers requested by generic artifact contracts.
# 函数用途: 根据 required_columns 验证 CSV 表头包含必需列。
def csv_contract_findings(
    path: Path,
    rows: list[list[str]],
    contract: dict[str, object],
) -> list[ArtifactFinding]:
    required_columns = string_list(contract.get("required_columns"))
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


# LLM: text_size_findings applies min_size to text-like artifacts.
# 函数用途: 读取 min_size 结构字段，检查文本产物长度是否达到合同下限。
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


# LLM: markdown_section_findings checks declared Markdown heading sections exactly.
# 函数用途: 根据 required_sections 校验 Markdown 标题，不从正文自然语言推断章节是否存在。
def markdown_section_findings(path: Path, text: str, contract: dict[str, object]) -> list[ArtifactFinding]:
    required_sections = string_list(contract.get("required_sections"))
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


# LLM: markdown_headings extracts ATX heading labels from Markdown syntax only.
# 函数用途: 解析 #/## 标题行，保持章节判断是格式事实而不是语义猜测。
def markdown_headings(text: str) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append(match.group(1).strip())
    return headings


# LLM: string_list normalizes list-like contract fields without parsing prose.
# 函数用途: 把 required_fields/required_columns/required_sections 转成非空字符串列表。
def string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value for text in [str(item or "").strip()] if text]


# LLM: positive_int parses optional numeric contract limits.
# 函数用途: 将 min_size 等结构字段转为非负整数，非法值按未设置处理。
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
