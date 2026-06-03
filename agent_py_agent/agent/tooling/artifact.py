from __future__ import annotations

"""tool implementation for explicit externalized artifact reads."""

import json
from pathlib import Path
from typing import Any

from ..memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    estimate_tool_output_artifact_size,
    read_tool_output_artifact,
)
from ..settings.defaults import default_config_int
from .artifact_read_budget import (
    ArtifactReadBudget,
    ArtifactReadBudgetRequest,
)
from .models import BaseTool, ToolExecutionResult, ToolSpec


class ReadArtifactTool(BaseTool):
    spec = ToolSpec(
        name="read_artifact",
        category="memory",
        effect="read_only",
        description="显式读取已外置 tool-output artifact 的正文切片；不能读取任意文件路径。",
        use_cases=[
            "memory-resume 只给出 artifact path/hash/size 后，需要显式查看正文片段",
            "接管或救援时核对大工具输出的具体内容",
        ],
        avoid_when=[
            "只是要看普通工作区文件时使用 read_file",
            "artifact ref 没有来自 tool output index、manifest 或恢复包",
        ],
        keywords=["artifact", "tool_output", "externalized", "read_artifact", "checkpoint"],
        parameters={
            "artifact_ref": "artifact path、sha256、scoped_call_id 或 call_id；必须能在 tool_outputs/index.jsonl 中命中",
            "offset": "从正文第几个字符开始读取，默认 0",
            "max_chars": "最多读取多少字符；0 表示读取全部，默认读取 agent_config.yaml 的 memory_artifact_default_read_chars",
            "mode": "读取模式：slice/head/tail/search；默认 slice",
            "query": "mode=search 时要搜索的关键词",
            "run_id": "可选；读取短 call_id 时用于限制当前 runner 作用域，通常由系统自动注入",
            "task_id": "可选；读取短 call_id 时用于限制当前任务作用域，通常由系统自动注入",
            "request_id": "可选；读取短 call_id 时用于限制当前请求作用域，通常由系统自动注入",
        },
        examples=[
            '{"tool": "read_artifact", "artifact_ref": "C:/repo/memory_archive/artifacts/tool_outputs/read_file-call-abc.json", "offset": 0, "max_chars": 4000}',
        ],
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
        self.default_read_chars = _config_int("memory_artifact_default_read_chars", default_read_chars)
        self.read_budget = ArtifactReadBudget(
            window_seconds=_config_int(
                "tool_artifact_read_budget_window_seconds",
                artifact_read_budget_window_seconds,
            ),
            max_chars=_config_int("tool_artifact_read_budget_max_chars", artifact_read_budget_max_chars),
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        request = _read_request_from_params(self.root, params, default_read_chars=self.default_read_chars)
        budget_error = self.read_budget.preflight(
            ArtifactReadBudgetRequest(
                run_id=request.run_id,
                requested_chars=_budget_requested_chars(request),
            )
        )
        if budget_error:
            return ToolExecutionResult(self.spec.name, False, budget_error)
        payload = read_tool_output_artifact(request)
        if payload.get("ok"):
            self.read_budget.commit(request.run_id, int(payload.get("content_chars") or 0))
        return ToolExecutionResult(self.spec.name, bool(payload.get("ok")), json.dumps(payload, ensure_ascii=False))


def _read_request_from_params(
    root: Path,
    params: dict[str, Any],
    *,
    default_read_chars: int,
) -> ReadToolOutputArtifactRequest:
    return ReadToolOutputArtifactRequest(
        root=root,
        artifact_ref=str(params.get("artifact_ref") or params.get("path") or ""),
        offset=int(params.get("offset", 0) or 0),
        max_chars=int(params.get("max_chars", default_read_chars) or 0),
        mode=str(params.get("mode") or "slice"),
        query=str(params.get("query") or ""),
        run_id=str(params.get("run_id") or ""),
        task_id=str(params.get("task_id") or ""),
        request_id=str(params.get("request_id") or ""),
    )


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
