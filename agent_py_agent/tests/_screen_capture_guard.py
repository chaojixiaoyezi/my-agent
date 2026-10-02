"""测试会话的真实屏幕防线（J16 片 E）：任何测试都不能碰 Mac 的真实屏幕、窗口列表或屏幕录制授权。

两层：
1. 本进程：conftest 在会话级把两个桌面后端唯一的真实库加载入口（`computer_use_macos.load_real_macos_frameworks`、
   `computer_use_x11.load_real_x11_libraries`）换成直接抛 `RealScreenAccessForbidden` 的版本；X11 那个只在 Linux 车道容器里不换
   （pyautogui 在 Mac 上也能点真屏幕）。它继承 BaseException：观察核心截图外层的 `except Exception` 吞不掉它，
   不会被误报成 capture_failed，测试当场失败。注入假库的后端根本不走这两个入口。
2. 子进程：适配器子进程经 MCP stdio 拉起时只继承白名单环境变量，conftest 的替换管不到它。Mac 上能走到真桌面后端的
   唯一入口是"打开屏幕观察 + 真的拉起 MCP 子进程"，所以递归扫描 tests/ 下所有 .py（含子目录和辅助模块，只排除本文件）：
   同一个文件里两者都出现，就必须带 Linux 车道跳过标记 MY_AGENT_XVFB_LANE（只在车道容器里跑，Mac 上自动跳过）。
"""
from __future__ import annotations

from pathlib import Path

# 打开屏幕观察的入口（环境标记名、写入函数、主配置开关）
OBSERVATION_SWITCH_MARKERS = ("OBSERVATION_ENV_FLAG", "MY_AGENT_COMPUTER_USE_OBSERVATION", "with_computer_use_observation",
                              "computer_use_observation_enabled")
# 真的拉起 MCP 适配器子进程的入口
MCP_SPAWN_MARKERS = ("register_mcp_servers", "MCPTransport", "stdio_client", "computer_use_server")
# Linux 车道跳过标记（只有车道容器里设为 1）
LANE_SKIP_MARKER = "MY_AGENT_XVFB_LANE"


# 类用途: 测试里碰到了真实屏幕入口；继承 BaseException，不会被产品代码的 except Exception 吞掉。
class RealScreenAccessForbidden(BaseException):
    pass


# 函数用途: 替换真实框架加载入口的版本：一被调用就失败，不 import 任何系统框架。
def forbidden_real_macos_frameworks():
    raise RealScreenAccessForbidden("测试不能加载真实的 macOS 屏幕框架（Quartz / ScreenCaptureKit / mss / pyautogui）；请注入假框架")


# 函数用途: 替换 X11 真实桌面库加载入口的版本（Linux 车道容器之外）：一被调用就失败，不 import 任何桌面库。
def forbidden_real_x11_libraries():
    raise RealScreenAccessForbidden("测试不能加载真实的 X11 桌面库（python-xlib Display / mss / pyautogui）；请注入假库")


# 函数用途: 一段测试源码是否既打开屏幕观察、又真的拉起 MCP 子进程，却没带车道跳过标记。
def needs_lane_marker(source: str) -> bool:
    switches = any(marker in source for marker in OBSERVATION_SWITCH_MARKERS)
    spawns = any(marker in source for marker in MCP_SPAWN_MARKERS)
    return switches and spawns and LANE_SKIP_MARKER not in source


# 函数用途: 递归扫描测试目录下所有 .py（子目录、辅助模块都算，辅助模块也可能拉起子进程），列出违规文件的相对路径；只排除本文件。
def lane_marker_violations(tests_dir: Path) -> list[str]:
    return sorted(path.relative_to(tests_dir).as_posix() for path in tests_dir.rglob("*.py")
                  if path.resolve() != Path(__file__).resolve() and needs_lane_marker(path.read_text(encoding="utf-8")))


__all__ = [
    "LANE_SKIP_MARKER", "MCP_SPAWN_MARKERS", "OBSERVATION_SWITCH_MARKERS", "RealScreenAccessForbidden",
    "forbidden_real_macos_frameworks", "forbidden_real_x11_libraries", "lane_marker_violations", "needs_lane_marker",
]
