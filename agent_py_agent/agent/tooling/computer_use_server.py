"""为开源 Computer Control MCP 补齐滚动动作并启动同一个 stdio server。"""

# ruff: noqa: UP045

# LLM: This is a deliberately thin extension point around the pinned upstream MCP server. Keep
# screenshot, OCR, window, mouse, and keyboard mechanics upstream; only bridge an upstream tool
# gap when the underlying mature library already owns the OS implementation.
# 模块用途: 直接启动打包进底座的开源桌面执行器，并用其既有 PyAutoGUI 依赖补齐滚轮工具。

from __future__ import annotations

from typing import Optional

import pyautogui
from computer_control_mcp.core import main as run_upstream_server
from computer_control_mcp.core import mcp


# LLM: Positive clicks scroll up and negative clicks scroll down, matching PyAutoGUI's public
# contract. Pinned MCP 1.13 crashes while inspecting PEP 604 optional parameters, so keep typing
# Optional here. Raising preserves MCP's structured isError path; never convert failures to prose.
# 函数用途: 在指定屏幕坐标执行竖向滚轮；不填坐标时在当前鼠标位置滚动。
@mcp.tool()
def scroll_screen(
    clicks: int,
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> dict[str, object]:
    pyautogui.scroll(int(clicks), x=x, y=y)
    return {
        "ok": True,
        "clicks": int(clicks),
        "x": x,
        "y": y,
    }


if __name__ == "__main__":
    run_upstream_server()
