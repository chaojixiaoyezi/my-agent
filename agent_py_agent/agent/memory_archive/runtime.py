"""LLM: runtime facade — thin re-export from runtime/ subpackage.

给人看的解释：
这个文件是 runtime 的入口，只做转发，不做任何逻辑。
所有实现在 runtime/ 子目录下：turn_archiver.py（主入口和ArchiveRunTurnResult）、event_builders.py（事件构造和辅助函数）。
这样原来从 `memory_archive.runtime` 导入的地方仍然能用。
"""

from .runtime.turn_archiver import ArchiveRunTurnResult, archive_run_turn

__all__ = [
    "ArchiveRunTurnResult",
    "archive_run_turn",
]
