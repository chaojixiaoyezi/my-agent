# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

"""Public dataclasses and hook registry for memory snapshot writing."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 CompressionHook 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 CompressionHook 的接口契约，让调用方依赖方法签名而非具体实现。
class CompressionHook(Protocol):
    """Protocol for external systems that need to run before compression."""

    # LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 __call__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 call 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
    def __call__(self, *, session_id: str, turn_id: str, archive_level: int) -> None:
        """Called before compression proceeds. Raise to block compression."""
        ...


_compression_hooks: list[CompressionHook] = []


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 CompressionSnapshotInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CompressionSnapshotInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class CompressionSnapshotInput:
    """Input bundle for write_compression_snapshot and on_before_compression."""
    session_id: str
    turn_id: str
    role: str
    content: str
    archive_level: int = 3
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "compression"
    backend: str = ""
    tool_calls: Iterable[Mapping[str, Any]] | None = None
    content_paths: Iterable[str] | None = None
    task_refs: Iterable[str] | None = None
    next_actions: Iterable[str] | None = None
    created_at: str | None = None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 RecoverySnapshotInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 RecoverySnapshotInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class RecoverySnapshotInput:
    """Input bundle for write_recovery_snapshot."""
    session_id: str
    user_prompt: str
    response_text: str
    backend: str
    source: str
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    status: str = "ok"
    error_code: str = ""
    tool_calls: Iterable[Mapping[str, Any]] | None = None
    task_refs: Iterable[str] | None = None
    content_paths: Iterable[str] | None = None
    next_actions: Iterable[str] | None = None
    archive_level: int = 3
    created_at: str | None = None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 RecoverySnapshotResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 RecoverySnapshotResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class RecoverySnapshotResult:
    """Result returned after a best-effort recovery snapshot write."""
    ok: bool
    snapshot_id: str = ""
    path: str = ""
    token_estimate: int = 0
    error: str = ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 CompressionHookResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CompressionHookResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class CompressionHookResult:
    """Result of the pre-compression hook that must succeed before compression proceeds."""
    snapshot_id: str
    hook_path: str
    snapshot_file_path: str
    token_estimate: int
    archive_level: int
