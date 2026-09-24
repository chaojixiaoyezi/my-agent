# LLM: 纯标准库：定位本机 Chrome/Chromium（探测逻辑与 browser-lite 相同）、以 --app 独立应用窗口打开工作台、
#   或交给系统默认浏览器。应用窗口是插件进程的普通子进程（不 start_new_session），使用插件数据目录下的专属 profile，
#   绝不碰用户日常 Chrome 配置；stdin/stdout/stderr 全接 DEVNULL，不污染 MCP 标准输出。
# 模块用途: 桌面窗口（应用模式浏览器子进程）与默认浏览器打开的平台适配。

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REVISION = re.compile(r"chromium-(\d+)")


# LLM: 只按平台常见安装位置和 ms-playwright 缓存（按修订号从新到旧）列出候选，不下载、不执行探测命令。
# 函数用途: 生成当前平台的浏览器可执行文件候选路径。
def browser_candidates() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        found = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                 Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
        return found + _newest(home.glob("Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/*"))
    if sys.platform.startswith("linux"):
        found = [Path(path) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                 if (path := shutil.which(name))]
        return found + _newest(home.glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    return []


# LLM: 纯函数；路径里没有 chromium-<修订号> 时排到最后。
# 函数用途: 对 ms-playwright 候选按修订号从新到旧排序。
def _newest(paths) -> list[Path]:
    return sorted(paths, key=lambda path: int(m.group(1)) if (m := _REVISION.search(str(path))) else -1, reverse=True)


# LLM: 设置了 chrome_path 就只认它（不存在即视为找不到，不偷偷换成自动探测的浏览器）；未设置时取第一个可执行候选。
# 函数用途: 解析桌面窗口要用的浏览器可执行文件，找不到返回 None。
def find_browser(chrome_path: str) -> Path | None:
    candidates = [Path(chrome_path).expanduser()] if chrome_path else browser_candidates()
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


# LLM: 副作用：创建 profile 目录并启动子进程；profile 是符号链接时拒绝（返回 None），启动失败也返回 None。
#   链接作为单个 --app= 参数传入，不经 shell。
# 函数用途: 以独立应用窗口（无地址栏）打开工作台，返回窗口进程。
def launch_app_window(executable: Path, url: str, profile: Path) -> subprocess.Popen | None:
    if profile.is_symlink():
        return None
    try:
        profile.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen([str(executable), f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                                 "--no-default-browser-check", "--window-size=1100,760"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True)
    except OSError:
        return None


# LLM: 幂等；先 SIGTERM 等 5 秒，不退再 SIGKILL，并回收僵尸进程。返回是否确实结束了一个仍在运行的进程。
# 函数用途: 关闭由 desktop 启动的应用窗口进程。
def close_window(process: subprocess.Popen | None) -> bool:
    if process is None or process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    return True


# LLM: 只调用系统默认打开程序（macOS open / Linux xdg-open），链接作为单个参数传入；失败或无桌面时返回 False。
#   子进程不另开会话，超时 10 秒。副作用：在用户桌面打开浏览器。
# 函数用途: 把完整链接交给默认浏览器，用户不需要看到令牌。
def open_in_browser(url: str) -> bool:
    opener = "open" if sys.platform == "darwin" else "xdg-open" if sys.platform.startswith("linux") else ""
    program = shutil.which(opener) if opener else None
    if program is None or (sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))):
        return False
    try:
        return subprocess.run([program, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
