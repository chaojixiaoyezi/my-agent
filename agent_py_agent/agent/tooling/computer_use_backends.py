# LLM: Computer Use 适配器里屏幕观察后端的唯一选择入口（J16 片 E）：按 sys.platform 的结构化取值选，darwin → MacBackend，
#   其余 → X11Backend（片 B 起的原行为）。两个后端都在这里惰性 import，构造本身不碰屏幕。只由 computer_use_server._screen_observer
#   在观察开关打开时调用；改动同步 test_computer_use_macos 里的选择用例。
# 模块用途: 让适配器按操作系统用对的桌面后端，新增平台只改这一处。
from __future__ import annotations

import sys


# 函数用途: 按平台名返回观察后端实例（不传则取当前进程的 sys.platform）。
def select_backend(platform_name: str | None = None) -> object:
    if (platform_name or sys.platform) == "darwin":
        from .computer_use_macos import MacBackend

        return MacBackend()
    from .computer_use_x11 import X11Backend

    return X11Backend()


__all__ = ["select_backend"]
