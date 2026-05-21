# LLM: file_write_session_models stores typed bundles shared by the public tool and service.
# 模块用途: 定义大文件写入 session 的路径、上下文和 chunk 请求参数包。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MAX_SESSION_CHUNK_CHARS = 128_000
SESSION_ROOT_NAME = ".agent_file_write_sessions"


# LLM: FileWriteSessionPaths keeps every on-disk session path derived from one validated session id.
# 类用途: 保存 session 目录、manifest、temp 文件和 chunk 目录路径。
@dataclass(frozen=True)
class FileWriteSessionPaths:
    session_dir: Path
    manifest_path: Path
    temp_path: Path
    chunks_dir: Path


# LLM: FileWriteSessionServiceContext injects path-boundary callbacks from the public filesystem tool.
# 类用途: 保存写入 session 服务需要的工作区、chunk 上限、路径解析和展示函数。
@dataclass(frozen=True)
class FileWriteSessionServiceContext:
    workspace_root: Path
    max_chunk_chars: int
    resolve_path: Callable[[str | Path], Path]
    display_path: Callable[[Path], str]


# LLM: ExistingChunkRequest bundles retry lookup facts without positional helper parameters.
# 类用途: 保存判断 chunk 是否已存在所需的 manifest、路径、下标和内容身份。
@dataclass(frozen=True)
class ExistingChunkRequest:
    manifest: dict[str, Any]
    paths: FileWriteSessionPaths
    chunk_index: int
    content: str
    content_hash: str


# LLM: ChunkWriteRequest bundles one staged chunk write and its manifest identity.
# 类用途: 保存写入单个 chunk 文件并更新 manifest 所需的结构化字段。
@dataclass(frozen=True)
class ChunkWriteRequest:
    paths: FileWriteSessionPaths
    manifest: dict[str, Any]
    chunk_index: int
    content: str
    content_hash: str


# LLM: InitialManifestRequest bundles begin-time target facts for manifest creation.
# 类用途: 保存创建 session manifest 需要的上下文、session_id、原始路径、目标路径和内部路径。
@dataclass(frozen=True)
class InitialManifestRequest:
    context: FileWriteSessionServiceContext
    session_id: str
    raw_path: str
    target: Path
    paths: FileWriteSessionPaths
    runtime_scope: dict[str, str] | None = None
