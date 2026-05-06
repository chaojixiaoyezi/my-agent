from __future__ import annotations

"""LLM: provides a simple file lock for dispatch watch loops.

给人看的解释：
watch 模式不能让两个父代理同时抢同一批子代理。
这个文件用一个带 token 的 lock 文件做最小互斥，避免并发调度互相踩状态。
"""

import json
import os
import time
import uuid
from pathlib import Path


class _DispatchWatchLock:

    def __init__(self, path: Path, *, force: bool = False):
        self.path = path
        self.force = force
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> _DispatchWatchLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.force and self.path.exists():
            self.path.unlink()
        payload = {
            "token": self.token,
            "pid": os.getpid(),
            "created_at": time.time(),
        }
        try:
            with self.path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        except FileExistsError as exc:
            raise RuntimeError(
                f"dispatch watch lock 已存在: {self.path}；确认没有父代理在运行后可使用 --force-lock。"
            ) from exc
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.acquired or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("token") == self.token:
            self.path.unlink()
