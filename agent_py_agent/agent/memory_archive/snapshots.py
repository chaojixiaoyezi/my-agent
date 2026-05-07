# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""lightweight recovery snapshot builder for run/gateway/subagent completion points.

新手说明:
这个文件专门负责写"恢复锚点"。
它不保存大段工具输出，只保存用户意图、助手动作、工具摘要、任务/请求 ID、恢复路径和 token 估算。
"""

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import CompressionSnapshot, utc_now_iso
from .snapshots import (
    CompressionHook,
    CompressionHookResult,
    CompressionSnapshotInput,
    RecoverySnapshotInput,
    RecoverySnapshotResult,
    clear_compression_hooks,
    on_before_compression,
    register_compression_hook,
    write_compression_snapshot,
    write_recovery_snapshot,
)
from .snapshots._helpers import (
    SNAPSHOT_PREVIEW_LIMITS,
    _compression_hooks,
    _content_hash,
    _dedupe_texts,
    _normalize_archive_level,
    _participants,
    _preview,
    _snapshot_id,
    _stable_json,
    _tool_snapshot,
)

__all__ = [
    "CompressionHook",
    "CompressionHookResult",
    "CompressionSnapshotInput",
    "RecoverySnapshotInput",
    "RecoverySnapshotResult",
    "SNAPSHOT_PREVIEW_LIMITS",
    "_compression_hooks",
    "_content_hash",
    "_dedupe_texts",
    "_normalize_archive_level",
    "_participants",
    "_preview",
    "_snapshot_id",
    "_stable_json",
    "_tool_snapshot",
    "clear_compression_hooks",
    "on_before_compression",
    "register_compression_hook",
    "write_compression_snapshot",
    "write_recovery_snapshot",
]