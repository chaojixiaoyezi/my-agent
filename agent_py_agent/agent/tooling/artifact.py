# LLM: Tool wrapper for explicit artifact body reads; it must not bypass the memory artifact index.
# 模块用途: 给模型提供 `read_artifact` 工具，按已登记 artifact ref 读取正文切片。
from __future__ import annotations

"""tool implementation for explicit externalized artifact reads."""

import json
from pathlib import Path
from typing import Any

from ..memory_archive.artifact_reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from .models import BaseTool, ToolExecutionResult, ToolSpec


# LLM: ReadArtifactTool exposes controlled body reads for externalized tool-output artifacts.
# 类用途: 只读取 memory_archive/artifacts/tool_outputs/index.jsonl 已登记 artifact，支持 offset/max_chars 切片。
class ReadArtifactTool(BaseTool):
    spec = ToolSpec(
        name="read_artifact",
        category="memory",
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
            "max_chars": "最多读取多少字符；0 表示读取全部，默认 4000",
            "run_id": "可选；读取短 call_id 时用于限制当前 runner 作用域，通常由系统自动注入",
            "task_id": "可选；读取短 call_id 时用于限制当前任务作用域，通常由系统自动注入",
            "request_id": "可选；读取短 call_id 时用于限制当前请求作用域，通常由系统自动注入",
        },
        examples=[
            '{"tool": "read_artifact", "artifact_ref": "C:/repo/memory_archive/artifacts/tool_outputs/read_file-call-abc.json", "offset": 0, "max_chars": 4000}',
        ],
    )

    # LLM: ReadArtifactTool.__init__ stores the workspace root used to locate the trusted artifact index.
    # 函数用途: 保存 workspace root；真正的路径边界检查交给 artifact_reader。
    def __init__(self, root: Path):
        self.root = Path(root)

    # LLM: execute delegates to read_tool_output_artifact and returns JSON so callers can inspect metadata.
    # 函数用途: 执行显式 artifact 读取；失败时也返回 JSON 错误，不读取任意未登记文件。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        payload = read_tool_output_artifact(
            ReadToolOutputArtifactRequest(
                root=self.root,
                artifact_ref=str(params.get("artifact_ref") or params.get("path") or ""),
                offset=int(params.get("offset", 0) or 0),
                max_chars=int(params.get("max_chars", 4000) or 0),
                run_id=str(params.get("run_id") or ""),
                task_id=str(params.get("task_id") or ""),
                request_id=str(params.get("request_id") or ""),
            )
        )
        return ToolExecutionResult(self.spec.name, bool(payload.get("ok")), json.dumps(payload, ensure_ascii=False))
