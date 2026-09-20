# LLM: 此模块只提交桌面文本事件，不决定窗口目标或任务成功；MCP 入口沿既有权限与取消边界调用。
# 修改时同步 Computer Use 协议与文本输入回归，禁止读取或改写用户剪贴板。
# 模块用途: 用系统和既有键盘库的公开接口输入文本，避免静默丢弃 Unicode 或过快键入。
from __future__ import annotations

import sys
import time

_KEY_INTERVAL_SECONDS = 0.05


# LLM: 参数是显式工具协议；先完成字符验证，再清空或输入。只返回提交事实，应用状态必须另行观察。
# macOS 的 Quartz 随既有 PyAutoGUI 依赖安装；其他平台不导入它，不把不支持的字符当成功。
# 函数用途: 向当前焦点控件输入文字，可显式替换原值；会产生真实键盘事件，不使用剪贴板。
def type_desktop_text(text: str, clear_existing: bool = False) -> dict[str, object]:
    import pyautogui

    text.encode("utf-16-le")
    if sys.platform != "darwin" and any(char.lower() not in pyautogui.KEYBOARD_KEYS for char in text):
        raise ValueError("当前平台的键盘执行器不支持这些字符；未清空或输入任何内容。")
    pyautogui.failSafeCheck()
    if clear_existing:
        pyautogui.hotkey("command" if sys.platform == "darwin" else "ctrl", "a")
        pyautogui.press("backspace")
    if sys.platform == "darwin":
        _type_macos_unicode(text, pyautogui)
    else:
        pyautogui.write(text, interval=_KEY_INTERVAL_SECONDS)
    return {"submitted_characters": len(text), "clear_existing": clear_existing, "application_verified": False}


# LLM: Quartz 使用 UTF-16 单元数，不能用 Python 字符数处理非 BMP 字符。每字保留 fail-safe 和节奏，
# 不检测页面语义、不宣称应用接受；修改时核对代理对、键盘按下/释放和失败传播。
# 函数用途: 在 macOS 提交 Unicode 键盘事件，支持中文及其他字符，不依赖当前输入法或剪贴板。
def _type_macos_unicode(text: str, keyboard: object) -> None:
    import Quartz

    for char in text:
        keyboard.failSafeCheck()
        units = len(char.encode("utf-16-le")) // 2
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            if event is None:
                raise RuntimeError("系统未能创建键盘事件，文本输入可能未完成。")
            Quartz.CGEventKeyboardSetUnicodeString(event, units, char)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(_KEY_INTERVAL_SECONDS)
