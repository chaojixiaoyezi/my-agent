# LLM: 一个插件进程内至多一个浏览器子进程和一个页面连接；工具调用与空闲回收线程用同一把锁串行。
#   崩溃或断连后整体关闭，下次 open 重新启动；空闲超过 idle_close_seconds 自动关闭。关闭总会清空 profile 缓存。
# 模块用途: 管理浏览器会话的启动、复用、空闲回收和关闭。

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from .access import UrlGuard
from .cdp import PageConnection
from .errors import BrowserError
from .launcher import BrowserProcess, clear_profile, find_browser
from .page import (
    DEFAULT_READ_SELECTOR,
    click_candidate,
    click_element,
    fill_candidate,
    fill_element,
    observation_candidates,
    open_page,
    read_elements,
)

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
        # 页面代次：open 与点击后导航都换新代次；观察候选只在铸出它的代次内有效。observed 记该代次里每次 read 用的选择器与总数
        self.generation = 0
        self.observed: dict[str, dict] = {}
        self._stopped = threading.Event()
        threading.Thread(target=self._idle_loop, name="browser-lite-idle", daemon=True).start()

    # LLM: open 会按需启动浏览器；其它动作要求已有页面。致命错误（崩溃、断连）后关闭浏览器再抛出。
    #   observation 是宿主复核过后附在 _meta 里的候选事实（key + 目标代次）；给了它就按候选执行，否则按 selector；两者都没有报参数错误。
    #   read 的结果附本代次的观察候选；open 与点击后导航都推进代次，旧候选随之失效。
    # 函数用途: 在锁内执行一个页面动作。
    def run(self, name: str, arguments: dict, guard: UrlGuard, observation: dict | None = None) -> dict:
        with self.lock:
            self.last_used = time.monotonic()
            try:
                page = self._ensure_page() if name == "open" else self._require_page()
                page.guard = guard.reason
                if name == "open":
                    result = open_page(page, guard, arguments["url"])
                    self._advance_generation()
                    return {**result, "browser_pid": self.browser.pid}
                if name == "read":
                    return self._read_with_observation(page, guard, arguments.get("selector"))
                if observation is not None:
                    selector, index, expected = self._resolve_candidate(observation)
                    if name == "click":
                        result = click_candidate(page, guard, selector, index, expected)
                    else:
                        result = fill_candidate(page, guard, selector, index, expected, arguments["value"])
                else:
                    if not arguments.get("selector"):
                        raise BrowserError("INVALID_ARGUMENTS", "请提供 selector，或填 read 结果里的 candidate_id。")
                    if name == "click":
                        result = click_element(page, guard, arguments["selector"])
                    else:
                        result = fill_element(page, guard, arguments["selector"], arguments["value"])
                if result.get("navigated"):
                    self._advance_generation()
                return result
            except BrowserError as exc:
                if exc.code in _FATAL:
                    self._shutdown()
                raise
            finally:
                if self.page is not None:
                    self.page.guard = None
                self.last_used = time.monotonic()

    # LLM: 只读；观察载荷只由结构化元素字段生成，目标引用固定为 "page"（本插件只有一个页面），代次是本会话的页面代次。
    #   没有元素时不附观察（宿主要求 1 个以上候选）。同代次内每次 read 的选择器与总数记在 observed，供候选解析。
    # 函数用途: 读取元素并附上本代次的观察候选。
    def _read_with_observation(self, page, guard: UrlGuard, selector: str | None) -> dict:
        result = read_elements(page, guard, selector)
        selector_text = selector or DEFAULT_READ_SELECTOR
        selector_hash = hashlib.sha256(selector_text.encode("utf-8")).hexdigest()[:8]
        self.observed[selector_hash] = {"selector": selector_text, "count": int(result.get("count") or 0)}
        candidates = observation_candidates(result.get("items") or [], selector_hash)
        if candidates:
            result["my_agent_observation"] = {"schema": "plugin_observation.v1",
                                              "target": {"ref": "page", "generation": str(self.generation)},
                                              "candidates": candidates}
        return result

    # LLM: 只认宿主 _meta 里的结构化字段：目标必须是本页当前代次（否则 stale），key 必须是本代次某次 read 铸出的“选择器哈希.序号”
    #   （否则 not_found）；两种拒绝都带 my_agent_observation_error 供宿主与模型区分，且不产生副作用。
    # 函数用途: 把候选 key 解析回“同一选择器 + 序号 + 观察时总数”。
    def _resolve_candidate(self, observation: dict) -> tuple[str, int, int]:
        target = observation.get("target") if isinstance(observation, dict) else None
        if (not isinstance(target, dict) or target.get("ref") != "page"
                or str(target.get("generation")) != str(self.generation)):
            raise BrowserError("OBSERVATION_STALE", "页面已经变化，观察候选已过期，请重新 read 后再操作。",
                               my_agent_observation_error={"code": "stale"})
        key = str(observation.get("key") or "")
        selector_hash, _, index = key.partition(".")
        entry = self.observed.get(selector_hash)
        if entry is None or not index.isdigit():
            raise BrowserError("OBSERVATION_NOT_FOUND", "候选不属于本页当前代次的任何一次 read，请重新 read。",
                               my_agent_observation_error={"code": "not_found"})
        return entry["selector"], int(index), int(entry["count"])

    # 函数用途: 页面换代：清空本代次的观察记录。
    def _advance_generation(self) -> None:
        self.generation += 1
        self.observed = {}

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
