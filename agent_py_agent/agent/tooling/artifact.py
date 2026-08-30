from __future__ import annotations

"""tool implementation for explicit externalized artifact reads."""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    estimate_tool_output_artifact_size,
    read_tool_output_artifact,
)
from ..settings.defaults import default_config_int
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)


@dataclass(frozen=True)
class ArtifactReadBudgetRequest:
    run_id: str
    requested_chars: int
    now: float | None = None


class ArtifactReadBudget:
    def __init__(self, *, window_seconds: int, max_chars: int) -> None:
        self.window_seconds = max(0, int(window_seconds or 0))
        self.max_chars = max(0, int(max_chars or 0))
        self._events_by_run: dict[str, list[tuple[float, int]]] = {}

    def preflight(self, request: ArtifactReadBudgetRequest) -> str:
        run_id = str(request.run_id or "").strip()
        if not run_id or self.window_seconds <= 0 or self.max_chars <= 0:
            return ""
        requested = max(0, int(request.requested_chars))
        if requested <= 0:
            return ""
        now = float(time.monotonic() if request.now is None else request.now)
        events = self._fresh_events(run_id, now)
        used = sum(chars for _, chars in events)
        if used + requested > self.max_chars:
            return _budget_error(
                run_id,
                self.max_chars,
                self.window_seconds,
                f"已用 {used} 字符，本次请求 {requested} 字符",
            )
        return ""

    def commit(self, run_id: str, chars: int, *, now: float | None = None) -> None:
        scoped_run = str(run_id or "").strip()
        if not scoped_run or self.window_seconds <= 0 or self.max_chars <= 0:
            return
        timestamp = float(time.monotonic() if now is None else now)
        events = self._fresh_events(scoped_run, timestamp)
        events.append((timestamp, max(0, int(chars))))
        self._events_by_run[scoped_run] = events

    def _fresh_events(self, run_id: str, now: float) -> list[tuple[float, int]]:
        events = [
            item
            for item in self._events_by_run.get(run_id, [])
            if item[0] > now - self.window_seconds
        ]
        self._events_by_run[run_id] = events
        return events


# LLM: This is the sole model-facing reader for archived tool output. Host paths and run identity
# stay inside the reader/audit layer; the model receives only a logical ref, content window, and
# an exact continuation call when more suffix content exists.
# 类用途: 让模型按稳定引用分段读取大工具输出，不暴露内部文件路径或运行身份。
class ReadArtifactTool(BaseTool):
    model_spec = ToolModelSpec(
        name="read_artifact",
        description="显式读取已外置 tool-output artifact 的正文切片；不能读取任意文件路径。",
        input_schema={
            "type": "object",
            "properties": {
                "artifact_ref": {
                    "type": "string",
                    "description": "工具结果给出的逻辑 artifact_ref（优先 scoped_call_id）；不要填写或猜内部文件路径。",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "mode=slice 时从正文第几个字符开始读取，默认 0；head/tail/search 会忽略它。",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "最多读取多少字符；0 表示读取全部。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["slice", "head", "tail", "search"],
                    "description": "读取模式，默认 slice；tail 直接返回真实末尾且不再需要向后续读。",
                },
                "query": {"type": "string", "description": "mode=search 时要搜索的关键词。"},
            },
            "required": ["artifact_ref"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="memory",
            use_cases=(
                "memory-resume 只给出 artifact path/hash/size 后，需要显式查看正文片段",
                "接管或救援时核对大工具输出的具体内容",
            ),
            avoid_when=(
                "只是要看普通工作区文件时使用 read_file",
                "artifact ref 没有来自 tool output index、manifest 或恢复包",
            ),
            keywords=("artifact", "tool_output", "externalized", "read_artifact", "checkpoint"),
            examples=(
                '{"tool": "read_artifact", "artifact_ref": "run-123:2-1", "offset": 0, "max_chars": 4000}',
                '{"tool": "read_artifact", "artifact_ref": "run-123:2-1", "mode": "tail", "max_chars": 1000}',
            ),
        ),
    )

    def __init__(
        self,
        root: Path,
        *,
        artifact_read_budget_window_seconds: int | None = None,
        artifact_read_budget_max_chars: int | None = None,
        default_read_chars: int | None = None,
    ):
        self.root = Path(root)
        self.default_read_chars = _config_int(
            "memory_artifact_default_read_chars", default_read_chars
        )
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("read_only"),
            concurrency_policy=ConcurrencyPolicy("parallel_safe"),
            resource_scopes=ResourceScopePolicy(
                parameter_names=("artifact_ref",),
                parameter_kinds={"artifact_ref": "logical"},
            ),
            output_policy=OutputPolicy(trust="external_data"),
            input_policy=ToolInputPolicy(
                internal_parameters=(
                    "__artifact_read_scope_mode",
                    "run_id",
                    "task_id",
                    "request_id",
                ),
                safe_parameter_defaults=(
                    ("offset", 0),
                    ("mode", "slice"),
                    ("max_chars", self.default_read_chars),
                ),
                trusted_parameter_bindings=(
                    (
                        "run_id",
                        TrustedParameterBinding(
                            source_refs=("run_scope.run_id",),
                            authority="host_authoritative",
                        ),
                    ),
                    (
                        "task_id",
                        TrustedParameterBinding(
                            source_refs=("run_scope.task_id",),
                            authority="host_authoritative",
                        ),
                    ),
                    (
                        "request_id",
                        TrustedParameterBinding(
                            source_refs=("run_scope.request_id",),
                            authority="host_authoritative",
                        ),
                    ),
                ),
            ),
        )
        self.read_budget = ArtifactReadBudget(
            window_seconds=_config_int(
                "tool_artifact_read_budget_window_seconds",
                artifact_read_budget_window_seconds,
            ),
            max_chars=_config_int(
                "tool_artifact_read_budget_max_chars", artifact_read_budget_max_chars
            ),
        )

    # LLM: Execute against the host-only reader, then remove filesystem/scope fields before the
    # output enters ToolResult. Keep the full payload available only to direct CLI/audit callers.
    # 函数用途: 读取一段归档正文并只返回模型需要的逻辑引用、窗口、续读提示和内容。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        request = _read_request_from_params(
            self.root, params, default_read_chars=self.default_read_chars
        )
        budget_error = self.read_budget.preflight(
            ArtifactReadBudgetRequest(
                run_id=request.run_id,
                requested_chars=_budget_requested_chars(request),
            )
        )
        if budget_error:
            return ToolHandlerOutcome(
                self.model_spec.name, False, budget_error, error_code="QUOTA_EXCEEDED"
            )
        payload = read_tool_output_artifact(request)
        ok = payload.get("ok") is True
        if ok:
            self.read_budget.commit(request.run_id, int(payload.get("content_chars") or 0))
        model_payload = _model_visible_read_payload(payload)
        return ToolHandlerOutcome(
            self.model_spec.name,
            ok,
            json.dumps(model_payload, ensure_ascii=False),
            error_code=(
                "" if ok else str(payload.get("error_code") or "TOOL_EXECUTION_FAILED").upper()
            ),
        )


# LLM: Never copy artifact_path/request_id/run_id/task_id from the host reader into model output.
# Continuation must reuse the canonical logical ref and only the public slice parameters.
# 函数用途: 把完整读取事实裁成模型可安全照抄的分页结果。
def _model_visible_read_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "ok",
        "kind",
        "tool",
        "call_id",
        "sha256",
        "size_bytes",
        "content_hash_verified",
        "reads_artifact_body",
        "read_mode",
        "content_offset",
        "content_max_chars",
        "content_chars",
        "total_chars",
        "window_start",
        "window_end",
        "has_more_before",
        "has_more_after",
        "next_offset",
        "truncated",
        "search_query",
        "match_count",
        "search_truncated",
        "content",
        "error_code",
        "message",
    )
    projected = {key: payload[key] for key in allowed if key in payload}
    logical_ref = str(
        payload.get("canonical_artifact_ref")
        or payload.get("artifact_ref")
        or ""
    ).strip()
    if logical_ref:
        projected["artifact_ref"] = logical_ref
    if payload.get("ok") is not True:
        return projected
    has_more_after = payload.get("has_more_after") is True
    if has_more_after and logical_ref:
        projected["next_read"] = {
            "artifact_ref": logical_ref,
            "mode": "slice",
            "offset": int(payload.get("next_offset") or 0),
            "max_chars": int(payload.get("content_max_chars") or 0),
        }
    else:
        projected["at_end"] = payload.get("read_mode") != "search"
    return projected


def _read_request_from_params(
    root: Path,
    params: dict[str, Any],
    *,
    default_read_chars: int,
) -> ReadToolOutputArtifactRequest:
    return ReadToolOutputArtifactRequest(
        root=_artifact_read_root(root, params),
        artifact_ref=str(params.get("artifact_ref") or ""),
        offset=int(params.get("offset", 0) or 0),
        max_chars=int(params.get("max_chars", default_read_chars) or 0),
        mode=str(params.get("mode") or "slice"),
        query=str(params.get("query") or ""),
        run_id=str(params.get("run_id") or ""),
        task_id=str(params.get("task_id") or ""),
        request_id=str(params.get("request_id") or ""),
        scope_mode=str(params.get("__artifact_read_scope_mode") or ""),
    )


def _artifact_read_root(root: Path, params: dict[str, Any]) -> Path:
    artifact_root = str(
        params.get("__artifact_read_root") or params.get("__task_work_dir") or ""
    ).strip()
    if not artifact_root:
        return root
    try:
        return Path(artifact_root).expanduser().resolve(strict=False)
    except OSError:
        return root


def _config_int(key: str, value: int | None) -> int:
    if value is not None:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            pass
    return default_config_int(key, minimum=0)


def _budget_requested_chars(request: ReadToolOutputArtifactRequest) -> int:
    requested = max(0, int(request.max_chars or 0))
    if requested > 0:
        return requested
    estimated = estimate_tool_output_artifact_size(request)
    return 0 if estimated is None else estimated


def _budget_error(run_id: str, max_chars: int, window_seconds: int, detail: str) -> str:
    return (
        f"artifact 读取预算已达到：run_id={run_id} 最近 {window_seconds} 秒最多读取 {max_chars} 字符。"
        f"原因：{detail}。请改用 mode=search/head/tail 或更小 max_chars 读取窄片段；"
        "如果仍缺证据，请向父级上报需要继续读取或提高预算。"
    )
