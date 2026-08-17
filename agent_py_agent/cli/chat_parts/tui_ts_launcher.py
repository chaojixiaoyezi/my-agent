"""TS TUI launcher（P4 入口集成，2026-08-17）。

`my-agent` 交互入口优先拉起 TypeScript TUI（frontend/tui，长期助手 形态）：
node 可用 + TUI 产物（dist/entry.js）存在 → spawn node 进程（连 gateway
HTTP，仅回环本机）；否则返回 False，由调用方回退现有 Python TUI / plain。
无 node 或产物缺失时绝不阻塞入口（唯一回退语义，群复核 2525/2528）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _tui_dist_entry() -> Path | None:
    """TUI 产物入口：env MY_AGENT_TUI_DIST 优先，否则包内相对路径。

    部署约定：frontend/tui/dist/entry.js 随部署分发（与 wheel 解耦的独立
    前端产物）；env 覆盖用于本地开发指向源码构建产物。
    """
    override = os.environ.get("MY_AGENT_TUI_DIST", "").strip()
    if override:
        entry = Path(override)
        return entry if entry.is_file() else None
    # 相对本文件：agent_py_agent/cli/chat_parts/ → 仓库 frontend/tui/dist
    candidates = [
        Path(__file__).resolve().parents[3] / "frontend" / "tui" / "dist" / "entry.js",
        Path(__file__).resolve().parents[2] / "tui" / "dist" / "entry.js",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def ts_tui_available() -> bool:
    """node 可用 + TUI 产物存在？（检测纯读，不启动）"""
    return shutil.which("node") is not None and _tui_dist_entry() is not None


def try_launch_ts_tui(
    *,
    gateway_base_url: str,
    session_id: str,
    model: str,
) -> bool:
    """拉起 TS TUI（阻塞直到 TUI 退出）。返回是否成功启动（False = 不可用）。"""
    entry = _tui_dist_entry()
    if entry is None:
        return False
    node = shutil.which("node")
    if node is None:
        return False
    if not sys.stdin.isatty():
        return False  # 非交互终端（管道/重定向）不启动 TUI
    cmd = [
        node,
        str(entry),
        "--base-url", gateway_base_url,
        "--session-id", session_id,
        "--model", model,
    ]
    try:
        # TUI 独占 stdin（禁双 renderer 抢终端）；退出码透传
        return subprocess.call(cmd) == 0
    except (OSError, subprocess.SubprocessError):
        return False
