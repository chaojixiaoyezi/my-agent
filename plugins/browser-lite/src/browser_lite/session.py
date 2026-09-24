# LLM: 一个插件进程内至多一个浏览器子进程和一个页面连接；工具调用与空闲回收线程用同一把锁串行。
#   崩溃或断连后整体关闭，下次 open 重新启动；空闲超过 idle_close_seconds 自动关闭。关闭总会清空 profile 缓存。
# 模块用途: 管理浏览器会话的启动、复用、空闲回收和关闭。

from __future__ import annotations

import threading
import time
from pathlib import Path

from .access import UrlGuard
from .cdp import PageConnection
from .errors import BrowserError
from .launcher import BrowserProcess, clear_profile, find_browser
from .page import click_element, fill_element, open_page, read_elements

_FATAL = {"PAGE_CRASHED", "BROWSER_DISCONNECTED"}


# LLM: 设置是已校验的插件设置；data_dir 为 None 表示宿主没给数据目录，此时不能启动浏览器（不回退 cwd）。
#   空闲线程是守护线程，只在拿到锁时关闭浏览器，不输出任何内容到标准输出。
# 类用途: 插件进程内唯一的浏览器会话。
class BrowserSession:
    # LLM: 有副作用：启动空闲回收守护线程。
    # 函数用途: 初始化会话状态并开始空闲检查。
    def __init__(self, settings: dict, data_dir: Path | None):
        self.settings = settings
        self.profile = data_dir / "profile" if data_dir is not None else None
        self.lock = threading.Lock()
        self.browser: BrowserProcess | None = None
        self.page: PageConnection | None = None
        self.last_used = time.monotonic()
        self._stopped = threading.Event()
        threading.Thread(target=self._idle_loop, name="browser-lite-idle", daemon=True).start()

    # LLM: open 会按需启动浏览器；其它动作要求已有页面。致命错误（崩溃、断连）后关闭浏览器再抛出。
    # 函数用途: 在锁内执行一个页面动作。
    def run(self, name: str, arguments: dict, guard: UrlGuard) -> dict:
        with self.lock:
            self.last_used = time.monotonic()
            try:
                page = self._ensure_page() if name == "open" else self._require_page()
                page.guard = guard.reason
                if name == "open":
                    return {**open_page(page, guard, arguments["url"]), "browser_pid": self.browser.pid}
                if name == "read":
                    return read_elements(page, guard, arguments.get("selector"))
                if name == "click":
                    return click_element(page, guard, arguments["selector"])
                return fill_element(page, guard, arguments["selector"], arguments["value"])
            except BrowserError as exc:
                if exc.code in _FATAL:
                    self._shutdown()
                raise
            finally:
                if self.page is not None:
                    self.page.guard = None
                self.last_used = time.monotonic()

    # LLM: 有副作用：结束浏览器并清空 profile 缓存（即使浏览器没在运行也清理）。
    # 函数用途: 响应 close 工具。
    def close_browser(self) -> dict:
        with self.lock:
            running = self.browser is not None and self.browser.alive()
            self._shutdown()
            if self.profile is not None:
                clear_profile(self.profile)
        return {"closed": running, "message": "浏览器已关闭，profile 缓存已清理。" if running else "浏览器未在运行，profile 缓存已清理。"}

    # LLM: 进程退出路径调用；停止空闲线程并关闭浏览器。等锁有上限，避免退出被卡死的调用拖住（浏览器仍随进程组回收）。
    # 函数用途: 插件退出时关闭浏览器。
    def shutdown(self) -> None:
        self._stopped.set()
        if self.lock.acquire(timeout=10):
            try:
                self._shutdown()
            finally:
                self.lock.release()

    # LLM: 调用方必须持锁。已有页面且浏览器存活时复用；否则清理残留后重新启动并连接页面，启动失败回收已启动的进程。
    # 函数用途: 取得可用的页面连接，必要时启动浏览器。
    def _ensure_page(self) -> PageConnection:
        if self.page is not None and self.browser is not None and self.browser.alive():
            return self.page
        self._shutdown()
        if self.profile is None:
            raise BrowserError("MISSING_DATA_DIR", "宿主未提供插件数据目录，无法创建专属浏览器 profile。")
        timeout = float(self.settings["command_timeout_seconds"])
        browser = BrowserProcess(find_browser(self.settings["chrome_path"]), self.profile, timeout)
        browser.start()
        try:
            self.page = PageConnection.open(browser.port, browser.page_target(), timeout)
        except BaseException:
            browser.stop()
            raise
        self.browser = browser
        return self.page

    # LLM: 调用方必须持锁；浏览器已退出时先清理再报错。
    # 函数用途: 要求已有打开的页面。
    def _require_page(self) -> PageConnection:
        if self.page is None or self.browser is None or not self.browser.alive():
            self._shutdown()
            raise BrowserError("NO_PAGE", "还没有打开页面（或浏览器已被空闲回收），请先用 open 打开地址。")
        return self.page

    # LLM: 调用方必须持锁；幂等。先断 CDP 连接再结束进程，进程 stop 会清空 profile 缓存。
    # 函数用途: 关闭页面连接和浏览器子进程。
    def _shutdown(self) -> None:
        page, browser, self.page, self.browser = self.page, self.browser, None, None
        if page is not None:
            page.close()
        if browser is not None:
            browser.stop()

    # LLM: 每秒检查一次；拿不到锁（有调用在进行）就跳过本轮。
    # 函数用途: 空闲超时后自动关闭浏览器。
    def _idle_loop(self) -> None:
        idle = float(self.settings["idle_close_seconds"])
        while not self._stopped.wait(1.0):
            if not self.lock.acquire(blocking=False):
                continue
            try:
                if self.browser is not None and time.monotonic() - self.last_used >= idle:
                    self._shutdown()
            finally:
                self.lock.release()
