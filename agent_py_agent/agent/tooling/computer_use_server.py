"""组合开源 Computer Control 工具及本地输入适配，启动唯一 stdio server。"""

# ruff: noqa: UP045

# LLM: 通过 FastMCP 公开接口复用上游函数，截图/OCR/窗口/鼠标继续由上游实现；仅替换文本输入
# 并补滚轮。不能修改 SDK 私有工具表、保留两条 type_text 路径或启动第二个服务。单一运行路径：底层 Server 收发 stdio，
# tools/list 与普通 tools/call 都交给 FastMCP；屏幕观察工具（J16 片 B，只在宿主写了环境标记时注册）由
# computer_use_observation_tools 的接管层处理，不碰 FastMCP 私有属性。
#   档位（结构化环境标记，不是缺依赖兜底）：只看档只装配观察工具、进程内绝不 import pyautogui 与
#   computer_control_mcp；完整档维持原上游装配。因此上游 import 全部放进函数体，模块顶层不得出现它们。
# 模块用途: 组合开源桌面工具与可靠文本输入，复用原工具名、权限和 stdio 连接。

import asyncio
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from .computer_use_observation_tools import build_adapter_server, observe_only_enabled

mcp = FastMCP("my-agent Computer Use")


# LLM: 只在完整档调用；上游 import 与 pyautogui 都在函数体里，避免只看档进程加载执行器依赖。
# 函数用途: 装载上游依赖，注册除 type_text 外的原函数。
def _load_upstream():
    import pyautogui
    from computer_control_mcp import core as upstream

    from .computer_text_input import type_desktop_text

    return pyautogui, upstream, type_desktop_text


# LLM: 只在 server 启动时读取上游公开目录并注册原函数；未知函数缺失时失败，不猜工具或降级。
# 函数用途: 保留上游工具目录及说明，文本输入由本适配器的唯一实现接管。
async def register_upstream_tools(pyautogui, upstream, type_desktop_text) -> None:
    for tool in await upstream.mcp.list_tools():
        if tool.name != "type_text":
            mcp.add_tool(getattr(upstream, tool.name), name=tool.name, description=tool.description)
    _register_local_inputs(pyautogui, type_desktop_text)


# LLM: 工具名沿用 type_text，clear_existing 是显式替换参数；结果不是应用验收证据，必须后置观察。
# 函数用途: 注册本适配器自己的文本输入与滚轮两个工具（只在完整档）。
def _register_local_inputs(pyautogui, type_desktop_text) -> None:
    @mcp.tool()
    def type_text(text: str, clear_existing: bool = False) -> dict[str, object]:
        """输入文本；clear_existing=true 清空当前控件后输入。提交后必须读取目标应用核对实际内容。"""
        return type_desktop_text(text, clear_existing)

    # LLM: Positive clicks scroll up and negative clicks scroll down, matching PyAutoGUI's public
    # contract. Pinned MCP 1.13 crashes while inspecting PEP 604 optional parameters, so keep typing
    # Optional here. Raising preserves MCP's structured isError path; never convert failures to prose.
    # 函数用途: 在指定屏幕坐标执行竖向滚轮；不填坐标时在当前鼠标位置滚动。
    @mcp.tool()
    def scroll_screen(clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> dict[str, object]:
        pyautogui.scroll(int(clicks), x=x, y=y)
        return {"ok": True, "clicks": int(clicks), "x": x, "y": y}


# 函数用途: 观察开关开着时才会被调用：按平台选的桌面后端（computer_use_backends.select_backend）只在这里构造。
def _screen_observer():
    from .computer_use_backends import select_backend
    from .screen_observation import ScreenObserver

    return ScreenObserver(select_backend())


# LLM: 装配在 build_adapter_server（可单测：按环境标记决定是否注册观察工具），这里按档位决定是否装载上游。不另起第二个服务。
# 函数用途: 适配器进程的唯一启动入口。
async def serve() -> None:
    observe_only = observe_only_enabled(os.environ)
    if not observe_only:
        pyautogui, upstream, type_desktop_text = _load_upstream()
        pyautogui.FAILSAFE = True
        await register_upstream_tools(pyautogui, upstream, type_desktop_text)
    low = build_adapter_server(mcp, os.environ, _screen_observer)
    async with stdio_server() as (read_stream, write_stream):
        await low.run(read_stream, write_stream, low.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve())
