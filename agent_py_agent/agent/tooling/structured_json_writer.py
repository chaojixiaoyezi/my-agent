# LLM: Structured JSON writer materializes machine data checkpoints without hand-escaped JSON strings.
# 模块用途: 提供 write_structured_json 工具，让模型传 rows/sheets/data，系统负责稳定序列化 JSON。

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _required_path, _text_param
from ._filesystem_read import FileSystemTool
from .models import ToolExecutionResult, ToolSpec
from .structured_json_collection_patch import (
    collection_item_updated_value,
    has_collection_item_updates,
)
from .structured_json_generation import generated_rows_checkpoint, has_generated_rows
from .structured_json_merge import merge_json_objects


# LLM: StructuredJsonTool writes non-empty JSON checkpoints from structured params.
# 类用途: 让资料整理、表格、索引类任务先落地有效 JSON，再交给后续 builder。
class StructuredJsonTool(FileSystemTool):
    # LLM: __init__ declares rows/sheets/data parameter shapes for retrieval and prompt rendering.
    # 函数用途: 初始化 write_structured_json 工具规格。
    def __init__(self, workspace_root: Path, workspace_roots: list[Path] | None = None):
        super().__init__(workspace_root, workspace_roots)
        self.spec = _structured_json_tool_spec()

    # LLM: execute validates structured shape before writing a JSON checkpoint.
    # 函数用途: 生成有效 JSON 文件；空数据或越界路径会返回稳定错误码。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            raw_path = _required_path(params.get("path"))
            target = self.resolve_path(raw_path)
            value = (
                collection_item_updated_value(target, params)
                if has_collection_item_updates(params)
                else _json_value_from_params(params)
            )
            if _merge_existing(params) and not has_collection_item_updates(params):
                value = _merged_json_value(target, value)
            _validate_non_empty_checkpoint(value)
        except ValueError as exc:
            return ToolExecutionResult("write_structured_json", False, str(exc), error_code=_error_code(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target, value)
        payload = {
            "artifact_ref": self.display_path(target),
            "row_count": _row_count(value),
            "top_level": type(value).__name__,
        }
        return ToolExecutionResult(
            "write_structured_json",
            True,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            result_envelope={"output": payload, **payload},
        )


# LLM: _structured_json_tool_spec keeps the constructor small and the tool schema auditable.
# 函数用途: 返回 write_structured_json 的工具说明、参数和示例。
def _structured_json_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="write_structured_json",
        category="artifact",
        effect="mutating",
        requires_idempotency=True,
        description="把结构化 data/rows/sheets 写成工作区内 JSON checkpoint。",
        use_cases=[
            "需要写 source_data.json、source_index.json、claims.json 等机器可读阶段文件",
            "避免把大型 JSON 当字符串手写导致引号、逗号或截断错误",
        ],
        avoid_when=["只写普通 Markdown/HTML 文本时用 write_file 或 file_write_session"],
        keywords=["json", "source_data", "source_index", "rows", "sheets", "checkpoint", "结构化数据", "写 JSON"],
        parameters=_structured_json_parameters(),
        parameter_details=_structured_json_parameter_details(),
        examples=[
            '{"tool": "write_structured_json", "path": "outputs/report/source_data.json", "sheets": [{"name": "榜单", "rows": [{"项目名": "demo"}]}]}',
            '{"tool": "write_structured_json", "path": "outputs/docs/source_index.json", "data": [{"title": "paper", "url": "https://example.com"}]}',
            '{"tool": "write_structured_json", "path": "outputs/report/source_data.json", "generated_rows": {"count": 1000, "columns": ["订单ID"], "fields": {"订单ID": {"format": "ORD-{index:04d}", "start": 1}}, "sheets": {"count": 3, "prefix": "数据"}}}',
        ],
    )


# LLM: _structured_json_parameters lists public tool fields from one place.
# 函数用途: 返回工具参数摘要，便于检索和提示渲染。
def _structured_json_parameters() -> dict[str, str]:
    return {
        "path": "要写出的 JSON 路径",
        "data": "可选，任意 dict/list JSON 数据",
        "rows": "可选，对象数组；会写成 {'rows': rows}",
        "sheets": "可选，工作表数组；会写成 {'sheets': sheets}",
        "generated_rows": "可选，按 count/fields/columns/sheets 生成大表 rows，不需要手写每一行",
        "collection_item_updates": "可选，按 item_index/field_path/value 更新已有集合 JSON 行",
        "name": "可选，rows 模式下的表名",
        "columns": "可选，rows 模式下的列名数组",
    }


# LLM: _structured_json_parameter_details documents generation rules without changing runtime facts.
# 函数用途: 返回工具参数细节，帮助模型选择 rows/sheets/generated_rows。
def _structured_json_parameter_details() -> dict[str, str]:
    return {
        "path": "相对工作区的 .json 输出路径；父目录会自动创建。",
        "data": "dict/list；适合 source_index 这类数组或对象；dict 可和 rows/sheets 同次提交写元数据。",
        "rows": "非空数组；适合单表 source_data。",
        "sheets": "非空数组，每个 sheet 需要非空 rows。",
        "generated_rows": "对象；count 为行数，columns 为列名，fields 支持 value/cycle/number/format/multiply，sheets.count 可把生成行拆成多个同形 sheet。",
        "collection_item_updates": "对象数组；每项用 item_index、field_path、value 更新 rows/list/groups 中的已有条目。",
        "items_path": "collection_item_updates 可选字段；集合行所在对象路径，默认 rows。",
        "groups_path": "collection_item_updates 可选字段；分组数组所在对象路径，分组内按 items_path 定位 rows。",
        "merge_existing": "可选布尔值；为 true 时 upsert JSON，对 rows/sheets 做追加合并，适合大表分批写入。",
        "name": "rows 模式下写入 JSON 的 name 字段。",
        "columns": "rows 模式下写入 JSON 的 columns 字段。",
    }


# LLM: _json_value_from_params accepts common checkpoint shapes without parsing prose.
# 函数用途: 按 data、sheets、rows 优先级构造 JSON 顶层对象。
def _json_value_from_params(params: dict[str, Any]) -> object:
    metadata = _top_level_metadata(params)
    if "data" not in params:
        return _merge_metadata_with_shape(metadata, _shape_value_from_params(params))
    value = _data_value(params.get("data"))
    if not isinstance(value, (dict, list)):
        raise ValueError("TOOL_INVALID_ARGUMENTS: data 必须是对象或数组")
    if metadata:
        if not isinstance(value, dict):
            raise ValueError("TOOL_INVALID_ARGUMENTS: 顶层 metadata 只能和对象 data 同时使用")
        value = _merge_inline_shape(metadata, value)
    if _has_shape_payload(params):
        return _data_with_shape_value(value, params)
    if _should_use_data_value(value, params):
        return value
    return _merge_metadata_with_shape(metadata, _shape_value_from_params(params))


# LLM: _data_value normalizes model-adapter JSON-string payloads into machine data.
# 函数用途: 兼容模型把嵌套 JSON 参数串化的情况，只解析 JSON 对象/数组，不把普通文本当事实。
def _data_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, (dict, list)) else value


# LLM: _shape_value_from_params keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _shape_value_from_params(params: dict[str, Any]) -> object:
    if has_generated_rows(params):
        return generated_rows_checkpoint(params.get("generated_rows"))
    if isinstance(params.get("generated_rows"), list):
        return {"rows": params["generated_rows"]}
    if isinstance(params.get("sheets"), list):
        return {"sheets": params["sheets"]}
    if isinstance(params.get("rows"), list):
        payload: dict[str, object] = {"rows": params["rows"]}
        if params.get("columns") is not None:
            payload["columns"] = params.get("columns")
        name = _text_param(params.get("name", ""), name="name", max_chars=120, allow_empty=True, strip=True)
        if name:
            payload["name"] = name
        return payload
    raise ValueError("TOOL_INVALID_ARGUMENTS: 需要 data、rows 或 sheets")


# LLM: _top_level_metadata keeps source/evidence facts structured when adapters flatten tool args.
# 函数用途: 提取顶层 completion_evidence/source_refs/claims，避免 rows/sheets 模式静默丢验收元数据。
def _top_level_metadata(params: dict[str, Any]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in ("completion_evidence", "source_refs", "claims"):
        value = params.get(key)
        if isinstance(value, (dict, list)) and bool(value):
            metadata[key] = value
    return metadata


# LLM: _merge_metadata_with_shape combines machine metadata with rows/sheets checkpoints.
# 函数用途: 顶层 metadata 和 shape 冲突时返回参数错误，避免覆盖机器事实。
def _merge_metadata_with_shape(metadata: dict[str, object], shape: object) -> object:
    if not metadata:
        return shape
    if not isinstance(shape, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: 顶层 metadata 只能和对象 checkpoint 同时使用")
    return _merge_inline_shape(metadata, shape)


# LLM: _data_with_shape_value merges metadata data with sibling rows/sheets without silent loss.
# 函数用途: 允许 data 写 completion_evidence/source_refs，同时由 rows/sheets 写真实数据；冲突时返回参数错误。
def _data_with_shape_value(data: object, params: dict[str, Any]) -> object:
    if not data:
        return _shape_value_from_params(params)
    if not isinstance(data, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: data 数组不能和 rows/sheets 同时使用")
    shape = _shape_value_from_params(params)
    if not isinstance(shape, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: rows/sheets 形状无效")
    return _merge_inline_shape(data, shape)


# LLM: _merge_inline_shape 是 agent_py_agent/agent/tooling/structured_json_writer.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 merge inline shape 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _merge_inline_shape(data: dict[str, object], shape: dict[str, object]) -> dict[str, object]:
    merged = dict(data)
    for key, value in shape.items():
        if key in merged and merged[key] != value:
            raise ValueError(f"TOOL_INVALID_ARGUMENTS: data 与 rows/sheets 同时声明了 {key}")
        merged[key] = value
    return merged


# LLM: _has_shape_payload keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _has_shape_payload(params: dict[str, Any]) -> bool:
    return (
        has_generated_rows(params)
        or isinstance(params.get("generated_rows"), list)
        or isinstance(params.get("sheets"), list)
        or isinstance(params.get("rows"), list)
    )


# LLM: _should_use_data_value keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _should_use_data_value(value: object, params: dict[str, Any]) -> bool:
    return bool(value) or not _has_shape_payload(params)


# LLM: _merge_existing keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _merge_existing(params: dict[str, Any]) -> bool:
    value = params.get("merge_existing")
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


# LLM: _merged_json_value keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _merged_json_value(target: Path, value: object) -> object:
    if not target.exists():
        return value
    existing = _read_existing_json(target)
    if not isinstance(existing, dict) or not isinstance(value, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: merge_existing 只支持对象合并")
    return merge_json_objects(existing, value)


# LLM: _read_existing_json keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _read_existing_json(target: Path) -> object:
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("STAGED_JSON_INVALID: 目标 JSON 无法读取或解析") from exc


# LLM: _validate_non_empty_checkpoint blocks skeleton-only data from becoming authoritative.
# 函数用途: 对 list、rows、sheets 三种通用结构执行非空检查。
def _validate_non_empty_checkpoint(value: object) -> None:
    if isinstance(value, list):
        if value:
            return
        raise ValueError("STAGED_JSON_NO_ROWS: data 数组不能为空")
    if not isinstance(value, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: JSON 顶层必须是对象或数组")
    rows = value.get("rows")
    if isinstance(rows, list):
        if rows:
            return
        raise ValueError("STAGED_JSON_NO_ROWS: rows 不能为空")
    sheets = value.get("sheets")
    if isinstance(sheets, list):
        if any(isinstance(sheet, dict) and isinstance(sheet.get("rows"), list) and sheet["rows"] for sheet in sheets):
            return
        raise ValueError("STAGED_JSON_NO_ROWS: sheets 必须包含非空 rows")
    if value:
        return
    raise ValueError("STAGED_JSON_NO_ROWS: JSON 对象不能为空")


# LLM: _atomic_write_json avoids half-written checkpoints after process interruption.
# 函数用途: 临时文件写入后原子替换目标 JSON。
def _atomic_write_json(path: Path, value: object) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)


# LLM: _row_count reports useful checkpoint size without affecting validation.
# 函数用途: 统计 list/rows/sheets 里的结构化行数。
def _row_count(value: object) -> int:
    if isinstance(value, list):
        return len(value)
    if not isinstance(value, dict):
        return 0
    rows = value.get("rows")
    if isinstance(rows, list):
        return len(rows)
    sheets = value.get("sheets")
    if isinstance(sheets, list):
        return sum(
            len(sheet.get("rows", []))
            for sheet in sheets
            if isinstance(sheet, dict) and isinstance(sheet.get("rows"), list)
        )
    return 1 if value else 0


# LLM: _error_code maps writer validation failures to shared taxonomy codes.
# 函数用途: 从 ValueError 前缀提取机器错误码。
def _error_code(exc: ValueError) -> str:
    prefix = str(exc).split(":", 1)[0].strip().upper()
    if prefix.startswith("STAGED_JSON_") or prefix in {"TOOL_INVALID_ARGUMENTS", "PATH_OUTSIDE_WORKSPACE"}:
        return prefix
    if "路径超出" in str(exc) or "outside" in str(exc).lower():
        return "PATH_OUTSIDE_WORKSPACE"
    return "TOOL_INVALID_ARGUMENTS"


__all__ = ["StructuredJsonTool"]
