# LLM: API JSON collection materializes sourced tabular checkpoints from structured HTTP request specs.
# 模块用途: 把一组 JSON API 响应映射成带 source_refs/claims/field_source_ids 的表格 checkpoint。

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _required_path
from ._filesystem_read import FileSystemTool
from .api_json_collection_builder import build_checkpoint, validate_checkpoint
from .api_json_collection_request import collection_request
from .models import ToolExecutionResult, ToolSpec

_DEFAULT_TIMEOUT = 15


# LLM: ApiJsonCollectionTool turns remote JSON pages into auditable local source_data checkpoints.
# 类用途: 为来源型表格任务提供批量 API->sheets 工具，避免模型手写超长 JSON 或伪造行级证据。
class ApiJsonCollectionTool(FileSystemTool):
    # LLM: __init__ declares the stable API collection schema for tool discovery.
    # 函数用途: 初始化 api_json_collection 工具规格和网络超时。
    def __init__(self, workspace_root: Path, workspace_roots: list[Path] | None = None, *, timeout: int = _DEFAULT_TIMEOUT):
        super().__init__(workspace_root, workspace_roots)
        self.timeout = max(1, int(timeout or _DEFAULT_TIMEOUT))
        self.spec = _tool_spec()

    # LLM: execute fetches declared URLs and writes one structured checkpoint under workspace roots.
    # 函数用途: 执行批量抓取、字段映射、来源绑定和 JSON 原子写入。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            target = self.resolve_path(_required_path(params.get("path")))
            request = collection_request(params)
            request = self._bind_artifact_requests(request)
            checkpoint = build_checkpoint(request, timeout=self.timeout)
            validate_checkpoint(checkpoint, request=request)
        except ValueError as exc:
            return ToolExecutionResult("api_json_collection", False, str(exc), error_code=_error_code(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target, checkpoint)
        payload = _result_payload(self.display_path(target), checkpoint)
        return ToolExecutionResult(
            "api_json_collection",
            True,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            result_envelope={"output": payload, **payload},
        )

    # LLM: Artifact-backed requests are resolved once at the filesystem boundary.
    # 函数用途: 把 source_artifacts 的 artifact_ref 固定到工作区内路径，builder 只消费已校验结构字段。
    def _bind_artifact_requests(self, request: dict[str, Any]) -> dict[str, Any]:
        bound = dict(request)
        specs: list[dict[str, Any]] = []
        for spec in request["requests"]:
            item = dict(spec)
            if item.get("artifact_ref"):
                artifact_path = self.resolve_path(str(item["artifact_ref"]))
                item["artifact_path"] = str(artifact_path)
                item["artifact_ref"] = self.display_path(artifact_path)
            specs.append(item)
        bound["requests"] = specs
        return bound


# LLM: _tool_spec keeps catalog metadata out of runtime execution logic.
# 函数用途: 返回 api_json_collection 的工具说明和参数合同。
def _tool_spec() -> ToolSpec:
    return ToolSpec(
        name="api_json_collection",
        category="api",
        effect="mutating",
        requires_idempotency=True,
        description="按 requests 列表抓取 JSON API，并生成带来源证据的 sheets/source_data.json。",
        use_cases=[
            "需要从多个 API 分组生成 xlsx/source_data.json",
            "每个表格行都需要 source_refs、claims 或 field_source_ids 证据",
        ],
        avoid_when=["只是写少量人工整理 JSON 时，用 write_structured_json 更直接"],
        keywords=["api", "json", "sheets", "source_refs", "claims", "field_source_ids", "xlsx", "来源证据", "批量抓取"],
        parameters={
            "path": "要写出的 JSON checkpoint 路径",
            "requests": "数组，每项包含 name/source_id/url，可覆盖 item_path/limit",
            "request_ranges": "数组，用 start_date/end_date/step_days/url_template 批量生成 requests",
            "source_artifacts": "数组，每项包含 artifact_ref/name/source_id，可把已归档 JSON 工具结果转成 checkpoint",
            "request_delay_seconds": "批量请求间隔；超过 10 个请求默认自动加间隔以避开常见限流",
            "item_path": "响应 JSON 中数组位置，默认 items",
            "limit_per_request": "每个请求最多取多少条，默认 10",
            "columns": "输出表头数组",
            "fields": "输出字段映射；值可为 JSON path，或 {path/value/template/default/default_template}",
            "evidence_fields": "需要绑定 field_source_ids 和 claims 的字段",
            "completion_evidence": "完成范围、方法、抓取时间等机器字段",
        },
        parameter_details={
            "requests": "最多 64 个；每个 url 只支持 http/https GET；source_id 会写入 source_refs。",
            "request_ranges": "url_template 可用 {start}/{end}/{index}，适合周/月分组等大量同形请求。",
            "source_artifacts": "artifact_ref 必须位于工作区允许根内；支持直接 JSON 或 fetch_url tool_output_artifact.content 中的 JSON 正文。",
            "request_delay_seconds": "可显式设为 0..60 秒；未设置且请求数大于 10 时默认 6.5 秒。",
            "fields": "字符串表示 item 内路径；default_template 可在 path 为空时用 item 字段补非空值。",
            "evidence_fields": "缺省为 fields 的全部字段；每行会写 field_source_ids[field]=[source_id]。",
        },
        examples=[_range_example()],
    )


def _range_example() -> str:
    return (
        '{"tool":"api_json_collection","path":"outputs/source_data.json",'
        '"request_ranges":[{"start_date":"2026-01-01","end_date":"2026-02-28","step_days":7,'
        '"name_template":"2026-W{index:02d}","source_id_template":"src-w{index:02d}",'
        '"url_template":"https://api.example/items?from={start}&to={end}"}],'
        '"fields":{"项目名":"full_name","地址":"html_url","上升 star 数":"stargazers_count",'
        '"中文解释":{"path":"description","default_template":"{full_name} repository; language={language}"},'
        '"推荐理由":{"template":"stars={stargazers_count}; topics={topics}"},'
        '"技术栈":{"path":"language","default":"unknown"},'
        '"生态":{"template":"topics={topics}"},'
        '"应用方向":{"template":"topics={topics}"}},'
        '"evidence_fields":["项目名","地址","上升 star 数","中文解释","推荐理由"]}'
    )


def _result_payload(artifact_ref: str, checkpoint: dict[str, object]) -> dict[str, object]:
    sheets = checkpoint.get("sheets") if isinstance(checkpoint.get("sheets"), list) else []
    return {
        "artifact_ref": artifact_ref,
        "claim_count": len(checkpoint.get("claims", [])) if isinstance(checkpoint.get("claims"), list) else 0,
        "row_count": sum(len(sheet.get("rows", [])) for sheet in sheets if isinstance(sheet, dict)),
        "sheet_count": len(sheets),
    }


def _atomic_write_json(path: Path, value: object) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)


def _error_code(exc: ValueError) -> str:
    prefix = str(exc).split(":", 1)[0].strip().upper()
    if prefix.startswith(("API_", "TOOL_INVALID_ARGUMENTS", "PATH_")):
        return prefix
    return "TOOL_INVALID_ARGUMENTS"


__all__ = ["ApiJsonCollectionTool"]
