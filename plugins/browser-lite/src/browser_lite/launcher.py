# LLM: 纯标准库：定位本机 Chrome/Chromium、在插件数据目录的专属 profile 下启动无头浏览器、读取调试端口、回收进程。
#   浏览器是插件进程的普通子进程（不 start_new_session），宿主回收插件进程组时一并回收；绝不使用用户日常 Chrome 配置。
# 模块用途: 浏览器可执行文件探测与子进程生命周期管理。

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .errors import UNAVAILABLE_MESSAGE, BrowserError

_REVISION = re.compile(r"chromium-(\d+)")
_PROC_ROOT = Path("/proc")
_HELPER_EXIT_SECONDS = 5.0


# LLM: 只按平台常见安装位置和 ms-playwright 缓存（按修订号从新到旧）列出候选，不下载、不执行探测命令。
# 函数用途: 生成当前平台的浏览器可执行文件候选路径。
def browser_candidates() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        found = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                 Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
        found += _newest(home.glob("Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/*"))
        return found
    if sys.platform.startswith("linux"):
        found = [Path(path) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                 if (path := shutil.which(name))]
        return found + _newest(home.glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    return []


# LLM: 按路径里 chromium-<修订号> 的数字倒序，保证优先使用最新下载的 Chromium。
# 函数用途: 对 ms-playwright 候选按修订号从新到旧排序。
def _newest(paths) -> list[Path]:
    return sorted(paths, key=_revision, reverse=True)


# LLM: 纯函数；路径里没有修订号时排到最后。
# 函数用途: 从 ms-playwright 路径提取 chromium 修订号。
def _revision(path: Path) -> int:
    match = _REVISION.search(str(path))
    return int(match.group(1)) if match else -1


# LLM: 设置了 chrome_path 就只认它（不存在即不可用，不偷偷回退到自动探测）；未设置时取第一个可执行候选。
# 函数用途: 解析本次要使用的浏览器可执行文件，找不到时抛“浏览器不可用”。
def find_browser(chrome_path: str) -> Path:
    candidates = [Path(chrome_path).expanduser()] if chrome_path else browser_candidates()
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise BrowserError("BROWSER_UNAVAILABLE", UNAVAILABLE_MESSAGE)


# LLM: profile 目录固定在插件数据目录下，只由本插件使用；stop 后清空其内容（保留目录本身）。
#   调试端口由 Chrome 随机分配（--remote-debugging-port=0），从 profile 下的 DevToolsActivePort 读取。
# 类用途: 管理一个专属无头浏览器子进程。
class BrowserProcess:
    # LLM: 构造不启动进程；profile 必须是插件数据目录下的绝对路径。
    # 函数用途: 记录可执行文件、profile 目录和启动超时。
    def __init__(self, executable: Path, profile: Path, timeout: float):
        self.executable = executable
        self.profile = profile
        self.timeout = timeout
        self.process: subprocess.Popen | None = None
        self.port = 0

    # LLM: 有副作用：创建 profile 目录、启动子进程。stdin/stdout/stderr 全部接 DEVNULL，绝不污染插件的 MCP 标准输出。
    #   profile 是符号链接时拒绝；启动超时或进程提前退出时回收并抛错。
    # 函数用途: 启动无头浏览器并等待调试端口就绪。
    def start(self) -> None:
        if self.profile.is_symlink():
            raise BrowserError("UNSAFE_PROFILE", "插件 profile 目录是符号链接，已拒绝启动浏览器。")
        self.profile.mkdir(parents=True, exist_ok=True)
        port_file = self.profile / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        arguments = [str(self.executable), f"--user-data-dir={self.profile}", "--headless=new",
                     "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
                     "--disable-extensions", "--disable-background-networking", "--disable-sync",
                     "--disable-component-update", "--use-mock-keychain", "--password-store=basic", "about:blank"]
        try:
            self.process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as exc:
            raise BrowserError("BROWSER_START_FAILED", "浏览器启动失败，请检查 chrome_path 是否为可执行的 Chrome/Chromium。") from exc
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline and self.process.poll() is None:
            lines = port_file.read_text(encoding="utf-8").split("\n") if port_file.is_file() else []
            if len(lines) >= 2 and lines[0].strip().isdigit():
                self.port = int(lines[0])
                return
            time.sleep(0.05)
        self.stop()
        raise BrowserError("BROWSER_START_FAILED", "浏览器启动失败或超时，未拿到调试端口。")

    # LLM: 只读；返回值仅用于结果展示和测试核对回收，不作为身份或权限依据。
    # 函数用途: 返回浏览器子进程 pid，未启动时为 None。
    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    # LLM: 只读进程状态，不发信号。
    # 函数用途: 判断浏览器子进程是否仍在运行。
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    # LLM: 通过本机 HTTP /json/list 找已有 page 目标，没有时 PUT /json/new 新建；只返回 WebSocket 路径。
    # 函数用途: 取得一个页面目标的 CDP WebSocket 路径。
    def page_target(self) -> str:
        targets = self._http("GET", "/json/list")
        page = next((item for item in targets if item.get("type") == "page"), None) if isinstance(targets, list) else None
        if page is None:
            page = self._http("PUT", "/json/new?about:blank")
        url = page.get("webSocketDebuggerUrl") if isinstance(page, dict) else None
        prefix = f"ws://127.0.0.1:{self.port}"
        if not isinstance(url, str) or not url.startswith(prefix + "/devtools/page/"):
            raise BrowserError("BROWSER_START_FAILED", "浏览器没有提供可用的页面调试地址。")
        return url[len(prefix):]

    # LLM: 只访问 127.0.0.1 上本浏览器的调试端口；响应有长度上限。
    # 函数用途: 发一个 CDP HTTP 端点请求并解析 JSON。
    def _http(self, method: str, path: str) -> object:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return json.loads(response.read(1024 * 1024))
        except (OSError, ValueError) as exc:
            raise BrowserError("BROWSER_START_FAILED", "读取浏览器调试端点失败。") from exc
        finally:
            connection.close()

    # LLM: 幂等；有副作用：SIGTERM 后宽限 5 秒再 SIGKILL；主进程退出后再有界等待仍带本 profile 参数的浏览器子进程，
    #   超时只对这些进程 SIGKILL；最后清空 profile 目录内容（保留目录本身，不跟随符号链接）。浏览器不脱离插件进程组。
    # 函数用途: 结束浏览器子进程并删除本次 profile 下的缓存。
    def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        # Linux 上网络服务等子进程会晚于主进程退出并继续写 profile（CI 实测残留 Default/Network Persistent State）。
        _await_profile_helpers(_PROC_ROOT, f"--user-data-dir={self.profile}", _HELPER_EXIT_SECONDS)
        clear_profile(self.profile)


# LLM: 等待只按 argv 识别本 profile 的进程（误匹配只会多等，无害）；/proc 不存在（macOS）时直接返回，不等待。
#   有副作用：到期仍在的进程只在 argv 仍匹配且与插件同一进程组时各发一次 SIGKILL，再最多等 1 秒让它们消失。
# 函数用途: 浏览器主进程退出后，等仍在用本 profile 的子进程退出，避免它们在清理之后又把文件写回去。
def _await_profile_helpers(proc_root: Path, argument: str, timeout: float) -> None:
    if _wait_profile_helpers_gone(proc_root, argument, timeout):
        return
    for pid in _profile_helper_pids(proc_root, argument):
        _kill_profile_helper(proc_root, pid, argument)
    _wait_profile_helpers_gone(proc_root, argument, 1.0)


# LLM: 只读 /proc，每 50 ms 扫一次；返回 False 表示到期时仍有匹配进程。
# 函数用途: 有界等待，直到没有任何进程的 argv 带本 profile 参数。
def _wait_profile_helpers_gone(proc_root: Path, argument: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _profile_helper_pids(proc_root, argument):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


# LLM: 读不到 /proc 或单个进程（已退出、权限不足、内核线程、僵尸）一律跳过，不报错。
# 函数用途: 列出 argv 里带本 profile 参数的进程号。
def _profile_helper_pids(proc_root: Path, argument: str) -> list[int]:
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    token = os.fsencode(argument)
    return [int(name) for name in names if name.isdigit() and _argv_uses_profile(_process_argv(proc_root, name), token)]


# LLM: Linux 上 Chrome 会用 setproctitle 把整条命令行改写成一个以空格拼接的字符串，所以参数须作为以空格为界的
#   完整一段出现（单独一个 argv 元素也满足）；<profile>2、<profile>/x 这类前缀和别的 profile 都不算。
# 函数用途: 判断一个进程的 argv 是否属于本 profile 的浏览器。
def _argv_uses_profile(argv: list[bytes], token: bytes) -> bool:
    bounded = b" " + token + b" "
    return any(bounded in b" " + item + b" " for item in argv)


# LLM: cmdline 以 NUL 分隔；任何读取错误都当作空 argv（不匹配）。
# 函数用途: 读取一个进程的 argv。
def _process_argv(proc_root: Path, name: str) -> list[bytes]:
    try:
        return (proc_root / name / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return []


# LLM: 只对 argv 仍匹配、且 /proc/<pid>/stat 的 pgrp 等于插件 os.getpgrp() 的进程发 SIGKILL；两项都在发信号前现读，
#   防止 pid 在扫描之后被复用。stat 读不到或进程组不同就不杀：若 Linux 上某些子进程不在插件进程组，只等不杀，
#   这是有意的保守取舍——argv 文本可能被 grep 等进程或“本 profile 加空格再加别的”路径碰巧带上，进程组才能证明是本插件拉起的。
# 函数用途: 强制结束一个超时仍在使用本 profile、且确属插件进程组的浏览器子进程。
def _kill_profile_helper(proc_root: Path, pid: int, argument: str) -> None:
    name = str(pid)
    if not _argv_uses_profile(_process_argv(proc_root, name), os.fsencode(argument)):
        return
    if _process_group(proc_root, name) != os.getpgrp():
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


# LLM: stat 里 comm 在括号中，可能含空格和括号，所以从最后一个 ')' 之后按空格切，其后依次是 state、ppid、pgrp；
#   读不到或格式不对返回 None，调用方据此不杀。
# 函数用途: 读取一个进程所属的进程组号。
def _process_group(proc_root: Path, name: str) -> int | None:
    try:
        data = (proc_root / name / "stat").read_bytes()
    except OSError:
        return None
    end = data.rfind(b")")
    fields = data[end + 1:].split() if end >= 0 else []
    try:
        return int(fields[2])
    except (IndexError, ValueError):
        return None


# LLM: 只删 profile 目录里的条目；目录本身是符号链接或不存在时什么也不做。rmtree 不跟随子目录符号链接。
# 函数用途: 清空专属 profile 下的缓存和状态文件。
def clear_profile(profile: Path) -> None:
    if profile.is_symlink() or not profile.is_dir():
        return
    for child in profile.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass
