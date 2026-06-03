
from __future__ import annotations

"""compatibility facade for low-level file IO moved to `agent.io`.

带锁 JSONL 追加等底层 I/O 已经放进 `agent.io`。
这个文件只负责旧导入兼容。
"""

from .io import append_jsonl, append_line_locked

__all__ = ["append_jsonl", "append_line_locked"]
