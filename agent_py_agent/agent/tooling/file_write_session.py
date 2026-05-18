# LLM: file_write_session is a thin public tool wrapper; staged write mechanics live in the service module.
# 模块用途: 暴露大文件分块写入工具规格，并把 begin/append/finish/abort 委托给服务层。

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._filesystem_read import FileSystemTool
from .file_write_session_service import (
    DEFAULT_MAX_SESSION_CHUNK_CHARS,
    FileWriteSessionService,
    FileWriteSessionServiceContext,
)
from .models import ToolExecutionResult, ToolSpec


# LLM: FileWriteSessionTool keeps model-facing metadata separate from staged file state.
# 类用途: 提供 file_write_session 工具入口；目录边界由 FileSystemTool 管，写入流程由 service 管。
class FileWriteSessionTool(FileSystemTool):
    # LLM: __init__ wires workspace callbacks into the service without doing file side effects.
    # 函数用途: 初始化工具规格、chunk 上限和可复用 session 服务。
    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        *,
        max_chunk_chars: int = DEFAULT_MAX_SESSION_CHUNK_CHARS,
    ):
        super().__init__(workspace_root, workspace_roots)
        self.max_chunk_chars = max(1, int(max_chunk_chars))
        self.spec = _build_spec(self.max_chunk_chars)
        self._service = FileWriteSessionService(
            FileWriteSessionServiceContext(
                workspace_root=self.workspace_root,
                max_chunk_chars=self.max_chunk_chars,
                resolve_path=self.resolve_path,
                display_path=self.display_path,
            )
        )

    # LLM: execute preserves the public Tool interface while delegating all action semantics.
    # 函数用途: 执行 file_write_session 的 begin/append/finish/abort 请求并返回结构化结果。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        return self._service.execute(params)


# LLM: _build_spec is isolated so tool metadata growth never bloats FileWriteSessionTool.
# 函数用途: 生成给模型看的工具说明、参数说明和示例。
def _build_spec(max_chunk_chars: int) -> ToolSpec:
    return ToolSpec(
        name="file_write_session",
        category="filesystem",
        description="用 begin/append/finish/abort 分块写入大文本文件，finish 时原子提交。",
        use_cases=[
            "要写入超过 write_file 推荐 inline 尺寸的大文件",
            "需要可重试、可幂等追加 chunk 的生成文件流程",
        ],
        avoid_when=[
            "小文件或一次性短内容仍优先 write_file",
            "只是在已有文件末尾追加少量文本时优先 append_file",
        ],
        keywords=[
            "large file",
            "chunk",
            "session",
            "manifest",
            "大文件",
            "分块写入",
        ],
        parameters={
            "action": "必填，begin、append、finish 或 abort",
            "target_path": "begin 时必填，最终提交的工作区内目标文件路径",
            "session_id": "append、finish、abort 时必填，由 begin 返回",
            "chunk_index": "append 时必填，从 0 开始的整数；相同 index+content 可安全重试",
            "content": "append 时必填，单 chunk 文本内容",
        },
        parameter_details={
            "action": "begin 创建 session；append 写入一个 chunk；finish 检查 chunk 连续后原子提交；abort 删除临时状态。",
            "target_path": "结构化记录在 manifest.target_path 中，必须位于允许工作区内。",
            "session_id": "begin 返回的稳定 id；不是路径，不能自行拼接。",
            "chunk_index": "同一个 chunk_index 只能对应同一份内容；重复提交同内容会返回 duplicate=true。",
            "content": f"单个 chunk 最多 {max_chunk_chars} 字符；更大的内容要拆成多个 append。",
        },
        examples=[
            '{"tool": "file_write_session", "action": "begin", "target_path": "dist/big.txt"}',
            '{"tool": "file_write_session", "action": "append", "session_id": "...", "chunk_index": 0, "content": "..."}',
            '{"tool": "file_write_session", "action": "finish", "session_id": "..."}',
        ],
    )
