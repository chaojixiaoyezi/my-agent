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

# 上游 computer-control-mcp 工具的精简中文说明静态覆盖表；仅覆盖说明文本，保留原函数签名、Schema 与未知工具默认说明。
_UPSTREAM_TOOL_DESCRIPTIONS: dict[str, str] = {
    "click_screen": (
        "在屏幕绝对坐标 (x, y) 执行单次鼠标左键点击。"
        "若已启用 observe_window，优先用 click_candidate 按复核候选点击；点击后请观察目标窗口确认状态。"
    ),
    "get_screen_size": "只读获取当前主屏幕像素分辨率（宽与高），供核对屏幕坐标范围。",
    "take_screenshot": (
        "截取整屏或匹配标题窗口的图像：title_pattern 留空截整屏，填写时默认按模糊匹配（threshold 默认 10）或"
        "设 use_regex=true 按正则匹配；save_to_downloads 默认保持 false；use_wgc 仅用于 Windows 捕获。"
        "常规窗口只读采样优先用 observe_window。"
    ),
    "take_screenshot_with_ocr": (
        "截屏并返回带屏幕绝对坐标的 OCR 识别框列表 JSON（四点坐标、文本与置信度）；"
        "title_pattern、use_regex、threshold、scale_percent_for_ocr、save_to_downloads 语义同 take_screenshot。"
        "若已启用 observe_window，优先用 observe_window 获取可复核候选。"
    ),
    "move_mouse": "将鼠标指针移动到屏幕绝对坐标 (x, y)，不触发按下或点击。",
    "mouse_down": (
        "按住指定鼠标按键不放（button 可选 'left'、'right'、'middle'，默认 'left'）。"
        "注意：系统不会自动松开按键，必须在同一操作序列中配对调用相同 button 的 mouse_up 释放；"
        "普通单次点击请用 click_screen，拖拽请用 drag_mouse。"
    ),
    "mouse_up": (
        "释放先前由 mouse_down 按住的鼠标按键（button 可选 'left'、'right'、'middle'，默认 'left'，"
        "须与同序列中的 mouse_down 配对一致）。"
    ),
    "drag_mouse": (
        "从屏幕坐标 (from_x, from_y) 按住拖拽至 (to_x, to_y)，duration 为拖拽秒数（默认 0.5），"
        "内部自动完成按下与释放；拖拽后请观察目标窗口确认结果。"
    ),
    "key_down": (
        "按住单个键盘按键不放（key 如 'shift'、'ctrl'、'alt'）。"
        "注意：系统不会自动松开按键，必须在同一操作序列中配对调用相同 key 的 key_up 释放，"
        "防止修饰键残留影响后续桌面输入；普通按键或组合键请优先用 press_keys。"
    ),
    "key_up": "释放先前由 key_down 按住的键盘按键（key 须与同序列中的 key_down 配对一致）。",
    "press_keys": (
        "发送自动按下并释放的键盘按键：keys 支持单键字符串（如 'enter'）、顺序按键列表（如 ['a', 'b']）"
        "或组合键嵌套列表（如 [['ctrl', 'c']]）。输入普通文本请用 type_text 或 type_into_candidate；"
        "提交后请观察目标窗口确认效果。"
    ),
    "list_windows": "只读列出当前系统打开的窗口列表，供核对窗口标题后再调用 observe_window 或 activate_window。",
    "wait_milliseconds": "等待指定毫秒数（milliseconds，整数），供界面动画或加载过渡后再重新观察。",
    "activate_window": (
        "按标题匹配将窗口激活至前台：title_pattern 默认模糊匹配（threshold 范围 0-100，默认 60），"
        "use_regex=true 时按正则匹配。只读采样无需先激活窗口，可直接调用 observe_window。"
    ),
}


# LLM: 只在完整档调用；上游 import 与 pyautogui 都在函数体里，避免只看档进程加载执行器依赖。
# 函数用途: 装载上游依赖，注册除 type_text 外的原函数。
def _load_upstream():
    import pyautogui
    from computer_control_mcp import core as upstream

    from .computer_text_input import type_desktop_text

    return pyautogui, upstream, type_desktop_text


# LLM: 只在 server 启动时读取上游公开目录并注册原函数；未知函数缺失时失败，不猜工具或降级。
# 函数用途: 保留上游工具目录及签名，已知工具覆盖精简中文说明，文本输入由本适配器的唯一实现接管。
async def register_upstream_tools(pyautogui, upstream, type_desktop_text) -> None:
    for tool in await upstream.mcp.list_tools():
        if tool.name != "type_text":
            description = _UPSTREAM_TOOL_DESCRIPTIONS.get(tool.name, tool.description)
            mcp.add_tool(getattr(upstream, tool.name), name=tool.name, description=description)
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
        """在屏幕坐标 (x, y) 执行竖向滚轮（clicks 正数向上滚、负数向下滚；x、y 留空时在当前鼠标位置滚动）。滚动后请观察窗口确认可见内容变化。"""
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
