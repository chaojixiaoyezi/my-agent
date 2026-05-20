# LLM: staged_checkpoint_acceptance centralizes generic staged checkpoint validation for long-running artifact flows.
# 模块用途: 统一校验阶段产物是否缺失、为空、JSON 截断或无数据，避免真实任务和普通任务各自维护一套判断。

from __future__ import annotations

import json
from pathlib import Path

from .evidence_contract import (
    EvidenceContractRequest,
    evaluate_evidence_contract,
)
from .staged_checkpoint_evidence_payloads import claims, source_refs, string_list


# LLM: staged_checkpoint_findings inspects only machine-declared checkpoint refs and emits stable findings.
# 函数用途: 根据 staging_contract.checkpoint_refs 检查阶段文件状态，只返回结构化 finding，不读取提示词或自然语言摘要。
def staged_checkpoint_findings(
    items: list[dict[str, object]],
    task_workspace: Path,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    preferred_paths = {str(item.get("preferred_path") or item.get("path") or "") for item in items}
    for item in items:
        for ref_text in _staging_refs_for_item(item, preferred_paths):
            findings.extend(one_staged_checkpoint_findings(ref_text, task_workspace))
    return findings


# LLM: one_staged_checkpoint_findings validates one checkpoint file with generic shape-aware rules.
# 函数用途: 单独检查一个阶段文件，区分缺失、空文件、JSON 非法、JSON 无有效数据等通用错误。
def one_staged_checkpoint_findings(ref: str, task_workspace: Path) -> list[dict[str, object]]:
    path = artifact_path(ref, task_workspace)
    if not path.exists():
        return [_finding("STAGED_ARTIFACT_MISSING", ref, path, {"message": "Staged checkpoint does not exist."})]
    if path.is_file() and path.stat().st_size <= 0:
        return [_finding("STAGED_ARTIFACT_EMPTY", ref, path, {"message": "Staged checkpoint is empty."})]
    if path.suffix.lower() != ".json":
        return []
    status = json_checkpoint_status(path)
    if status["code"] == "STAGED_JSON_INVALID":
        return [
            _finding(
                "STAGED_JSON_INVALID",
                ref,
                path,
                {
                    "message": "Staged JSON checkpoint is invalid or truncated.",
                    "parse_error": status.get("parse_error", ""),
                },
            )
        ]
    if status["code"] == "STAGED_JSON_NO_ROWS":
        return [
            _finding(
                "STAGED_JSON_NO_ROWS",
                ref,
                path,
                {"message": "Staged JSON checkpoint has no data rows."},
            )
        ]
    return []


# LLM: staged_json_evidence_findings validates source/claim refs for factual staged data.
# 函数用途: 对 source_data.json 这类阶段文件执行通用证据合同，不读取自然语言说明。
def staged_json_evidence_findings(
    ref: str,
    task_workspace: Path,
    evidence_contract: dict[str, object] | None,
) -> list[dict[str, object]]:
    if not evidence_contract:
        return []
    path = artifact_path(ref, task_workspace)
    if not path.exists() or path.suffix.lower() != ".json":
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(value, dict):
        return []
    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=source_refs(value.get("source_refs")),
            claims=claims(value.get("claims")),
            required_fields=string_list(evidence_contract.get("required_fields")),
            require_verified=bool(evidence_contract.get("require_verified", True)),
        )
    )
    return [
        {
            "code": str(item.get("code") or "EVIDENCE_CONTRACT_FAILED"),
            "severity": str(item.get("severity") or "hard"),
            "stage_ref": ref,
            "location": str(path),
            "message": str(item.get("message") or "Evidence contract failed."),
            **{key: val for key, val in item.items() if key not in {"code", "severity", "message"}},
        }
        for item in report.findings
    ]


# LLM: artifact_path resolves one workspace-relative checkpoint ref without accepting prose-derived paths.
# 函数用途: 把阶段 ref 解析到任务工作区里的绝对路径，保持和主验收一致的路径语义。
def artifact_path(ref: str, task_workspace: Path) -> Path:
    preferred = Path(str(ref or ""))
    if preferred.is_absolute():
        return preferred
    return (task_workspace / preferred).resolve()


# LLM: json_checkpoint_status separates invalid JSON from valid-but-empty structured data.
# 函数用途: 返回阶段 JSON 的结构状态，避免把被截断的 JSON 误判成“只是没有数据”。
def json_checkpoint_status(path: Path, required_columns: list[str] | None = None) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"code": "STAGED_JSON_INVALID", "parse_error": str(exc)}
    except json.JSONDecodeError as exc:
        return {"code": "STAGED_JSON_INVALID", "parse_error": str(exc)}
    if not _contains_nonempty_list(value):
        return {"code": "STAGED_JSON_NO_ROWS"}
    if shape_issue := _tabular_json_shape_issue(value, required_columns=required_columns):
        return shape_issue
    return {"code": "OK"}


# LLM: _staging_refs_for_item extracts checkpoint refs while filtering the final artifact path itself.
# 函数用途: 从单个 artifact 的 staging_contract 里取 checkpoint_refs，避免把最终产物路径重复当阶段文件检查。
def _staging_refs_for_item(item: dict[str, object], preferred_paths: set[str]) -> list[str]:
    contract = item.get("validation_contract")
    staging = contract.get("staging_contract") if isinstance(contract, dict) else None
    refs = staging.get("checkpoint_refs") if isinstance(staging, dict) else None
    if not isinstance(refs, list):
        return []
    return [
        ref_text
        for ref in refs
        if (ref_text := str(ref)).strip() and ref_text not in preferred_paths
    ]


# LLM: _contains_nonempty_list checks structured data shape recursively without depending on prose content.
# 函数用途: 判断 JSON 里是否存在非空 list，适配 sheets/rows/items/projects/top10 等常见机器字段结构。
def _contains_nonempty_list(value: object) -> bool:
    if isinstance(value, list):
        return bool(value) and any(_list_item_has_data(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_nonempty_list(item) for item in value.values())
    return False


# LLM: _tabular_json_shape_issue validates generic sheets/rows checkpoint structure before builder tools consume it.
# 函数用途: 对机器声明的表格 JSON 结构做通用检查：sheet 身份唯一、rows 非空、columns 与 row 字段一致。
def _tabular_json_shape_issue(value: object, *, required_columns: list[str] | None = None) -> dict[str, str]:
    required = required_columns or []
    if not required and (not isinstance(value, dict) or "sheets" not in value):
        return {}
    sheets = _tabular_sheet_candidates(value, required_columns=required)
    if not sheets:
        return {"code": "STAGED_JSON_NO_ROWS"}
    seen_names: set[str] = set()
    for index, sheet in enumerate(sheets):
        if not isinstance(sheet, dict):
            return _shape_issue("sheet", index)
        name = str(sheet.get("name") or "").strip()
        if not name:
            return _shape_issue("sheet.name", index)
        if name in seen_names:
            return {"code": "STAGED_JSON_DUPLICATE_SHEET_NAMES", "sheet_name": name}
        seen_names.add(name)
        rows = sheet.get("rows")
        if not isinstance(rows, list) or not any(_list_item_has_data(row) for row in rows):
            return {"code": "STAGED_JSON_NO_ROWS", "sheet_name": name}
        if columns_issue := _sheet_columns_issue(sheet, rows, index, required_columns=required):
            return columns_issue
    return {}


# LLM: _tabular_sheet_candidates normalizes flexible JSON table shapes into sheet-like records for shape validation.
# 函数用途: 把 rows/sheets/键值映射等表格 JSON 结构统一成 sheet 候选列表，供后续列和行校验复用。
def _tabular_sheet_candidates(value: object, *, required_columns: list[str]) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [{"name": "Sheet1", "rows": value}] if required_columns else []
    if not isinstance(value, dict):
        return []
    sheets = value.get("sheets")
    if isinstance(sheets, list):
        return [item for item in sheets if isinstance(item, dict)]
    if not required_columns:
        return []
    rows = value.get("rows")
    if isinstance(rows, list):
        return [{"name": str(value.get("name") or "Sheet1"), "columns": value.get("columns"), "rows": rows}]
    return [
        {"name": str(key), "rows": item}
        for key, item in value.items()
        if isinstance(item, list) and item
    ]


# LLM: _sheet_columns_issue keeps workbook source rows aligned with their declared machine columns.
# 函数用途: 如果 sheet 声明了 columns，则校验 columns 是非空列表且每行包含这些字段，避免 builder 生成错表。
def _sheet_columns_issue(
    sheet: dict[str, object],
    rows: list[object],
    index: int,
    *,
    required_columns: list[str],
) -> dict[str, str]:
    columns = sheet.get("columns")
    if columns is None and not required_columns:
        return {}
    if columns is not None and (not isinstance(columns, list) or not columns):
        return _shape_issue("sheet.columns", index)
    normalized_columns = [str(column).strip() for column in columns] if isinstance(columns, list) else _row_columns(rows)
    if any(not column for column in normalized_columns):
        return _shape_issue("sheet.columns", index)
    if required_columns:
        missing_required = [column for column in required_columns if column not in normalized_columns]
        if missing_required:
            return {
                "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
                "sheet_index": str(index),
                "missing_columns": ",".join(missing_required),
            }
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            return _shape_issue("sheet.rows", index, row_index=row_index)
        missing = [column for column in normalized_columns if column not in row]
        if missing:
            return {
                "code": "STAGED_JSON_TABLE_SHAPE_INVALID",
                "sheet_index": str(index),
                "row_index": str(row_index),
                "missing_columns": ",".join(missing),
            }
    return {}


# LLM: _row_columns derives table fields from structured rows when a sheet omits explicit columns.
# 函数用途: 对未声明 columns 的表格 JSON，按 row 字段补出机器列名供 required_columns 校验。
def _row_columns(rows: list[object]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        columns.extend(str(key) for key in row if str(key) not in columns)
    return columns


# LLM: _shape_issue returns stable table-shape status without embedding task-specific acceptance prose.
# 函数用途: 统一表格 JSON 结构错误的机器字段，供 closeout 和提交前校验复用。
def _shape_issue(field: str, index: int, *, row_index: int | None = None) -> dict[str, str]:
    issue = {
        "code": "STAGED_JSON_TABLE_SHAPE_INVALID",
        "field": field,
        "sheet_index": str(index),
    }
    if row_index is not None:
        issue["row_index"] = str(row_index)
    return issue


# LLM: _list_item_has_data treats list entries as meaningful only when they carry real row/body payload, not just skeleton metadata.
# 函数用途: 区分“只有阶段骨架的列表项”和“已经有真实数据的列表项”，避免空 sheet/meta 列表被误判成就绪。
def _list_item_has_data(item: object) -> bool:
    if isinstance(item, list):
        return bool(item) and any(_list_item_has_data(child) for child in item)
    if isinstance(item, dict):
        nested_values = [value for value in item.values() if isinstance(value, (list, dict))]
        if nested_values:
            return any(_contains_nonempty_list(value) for value in nested_values)
        return any(_scalar_has_data(value) for value in item.values())
    return _scalar_has_data(item)


# LLM: _scalar_has_data keeps row detection structural while ignoring empty placeholders.
# 函数用途: 判断一个标量值是否算真实内容；空字符串和 None 不算，数字/布尔/非空字符串算。
def _scalar_has_data(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


# LLM: _finding keeps staged checkpoint findings compact and machine-readable.
# 函数用途: 统一生成阶段文件 finding，必要时带上额外字段，例如 parse_error。
def _finding(code: str, ref: str, path: Path, detail: dict[str, object] | None = None) -> dict[str, object]:
    payload = detail or {}
    return {
        "code": code,
        "severity": "hard",
        "stage_ref": ref,
        "location": str(path),
        "message": str(payload.get("message") or "Staged checkpoint failed."),
        **{key: value for key, value in payload.items() if key != "message"},
    }


__all__ = [
    "artifact_path",
    "json_checkpoint_status",
    "one_staged_checkpoint_findings",
    "staged_json_evidence_findings",
    "staged_checkpoint_findings",
]
