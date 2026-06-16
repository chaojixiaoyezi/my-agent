
from __future__ import annotations

"""浏览器会话管理 —— 给模型补"需要 JS 渲染/点击/填表"的页面自动化能力。

短板6 子项:web_fetch 只能纯 HTTP 抓取,处理不了 SPA/动态站/需要登录点击的页面。
这里用 playwright(headless Chromium)补一个最小可用但完整的浏览器后端。

学标杆 长期助手 tools/browser_tool.py 的设计,裁剪适配 my-agent:

学到什么(对齐 长期助手):
  - **accessibility tree 快照(ariaSnapshot)**:把页面渲染成"角色+名字+ref"的文本树,
    让没有视觉能力的模型也能理解页面结构、知道哪些元素能点/能填。长期助手 用 agent-browser
    的 ariaSnapshot;my-agent 用 playwright 的 `locator.aria_snapshot()`(同一思路,YAML 风格)。
  - **ref 选择器**:快照里给可交互元素标 `[ref=eN]`,click/type 用 ref 定位,不用模型猜
    CSS/XPath。长期助手 用 `@e1/@e2`;my-agent 用 `e1/e2`(注入 data-agent-ref 属性,
    点击时按属性选择器解析,稳定且不依赖 playwright 私有 API)。
  - **按 session/task 隔离会话**:每个 session_id 一个独立 browser context(独立 cookie/storage),
    互不串。对标 长期助手 按 task_id 隔离。
  - **惰性启动 + 资源回收**:第一次用浏览器工具才起 Chromium(没用就不起),进程内单实例复用;
    可显式 close 释放,atexit 兜底全清。

适配差异(为什么不照搬 长期助手):
  - 长期助手 走 agent-browser CLI(子进程 + Unix socket daemon)+ 多云后端(Browserbase/BrowserUse);
    my-agent 直接用进程内 playwright sync API,零子进程/零 daemon/零云依赖,CI 友好、最小依赖。
  - SSRF 不自己写,直接复用 my-agent 现有 network_safety gate(和 web_fetch 同一把锁):
    导航前 evaluate_network_safety_gate 挡内网/loopback/云 metadata,默认拒私网。
  - 不做录屏/隐身/lightpanda/CDP supervisor 那些云常驻场景能力,先覆盖
    navigate/snapshot/click/type/close 最小完整集。

异常兜底:playwright 没装/Chromium 没下载 → TOOL_UNAVAILABLE(带安装指引,不崩);
导航失败/超时 → NETWORK_REQUEST_FAILED / TOOL_TIMEOUT;无效 ref/selector → TOOL_INVALID_ARGUMENTS;
页面崩溃 → 捕获成结构化错误而非让异常冒泡。
"""

import atexit
import threading
from dataclasses import dataclass, field
from typing import Any

# 默认浏览器动作超时(毫秒)。导航给足时间(动态站慢),其余动作短一些。
DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_ACTION_TIMEOUT_MS = 15_000
# a11y 快照给模型的最大字符数,超了截断(避免把 prompt 撑爆)。
SNAPSHOT_MAX_CHARS = 8_000
# 一页最多标注多少个可交互元素的 ref(防超大页生成天量 ref)。
MAX_REFS_PER_PAGE = 400

# 可交互元素选择器:链接/按钮/输入/下拉/可编辑区 + 常见 ARIA role。
# 用于注入 data-agent-ref,让 click/type 能按 ref 精确定位。
_INTERACTIVE_SELECTOR = (
    "a, button, input:not([type=hidden]), textarea, select, "
    "[role=button], [role=link], [role=textbox], [role=checkbox], "
    "[role=radio], [role=tab], [role=menuitem], [contenteditable=true]"
)

# 注入 ref + 收集 ref→元数据的 JS。返回 [{ref, role, name, tag}]。
# 只标可见(有 client rect)的元素,避免给隐藏元素发 ref 误导模型。
_INJECT_REFS_JS = """(maxRefs) => {
  const sel = %s;
  const els = Array.from(document.querySelectorAll(sel));
  const out = [];
  let i = 0;
  for (const el of els) {
    if (i >= maxRefs) break;
    const rects = el.getClientRects();
    const visible = rects.length > 0 || el.offsetParent !== null;
    if (!visible) continue;
    i++;
    const ref = 'e' + i;
    el.setAttribute('data-agent-ref', ref);
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    let name = (el.getAttribute('aria-label') || el.innerText || el.value ||
                el.getAttribute('placeholder') || el.getAttribute('title') || '').trim();
    name = name.replace(/\\s+/g, ' ').slice(0, 80);
    out.push({ref: ref, role: role, name: name, tag: el.tagName.toLowerCase()});
  }
  return out;
}""" % repr(_INTERACTIVE_SELECTOR)


class BrowserUnavailableError(RuntimeError):
    """playwright 未安装或 Chromium 二进制缺失。携带给模型的安装指引。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.hint = message


@dataclass
class _Session:
    """一个隔离的浏览器会话:独立 context + 当前活动 page。"""

    session_id: str
    context: Any  # playwright BrowserContext
    page: Any  # playwright Page
    ref_map: dict[str, dict[str, str]] = field(default_factory=dict)


def _import_playwright_sync():
    """惰性导入 playwright sync API。未安装则抛 BrowserUnavailableError(带指引)。"""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BrowserUnavailableError(
            "浏览器自动化需要 playwright,但当前环境未安装。安装:pip install playwright "
            "&& python -m playwright install chromium。无法装时改用 web_fetch 做纯 HTTP 抓取。"
        ) from exc
    return sync_playwright


class BrowserSessionManager:
    """进程内单实例浏览器会话管理器。惰性启动 Chromium,按 session_id 隔离 context。

    线程安全:用一把锁保护浏览器启动和会话字典。playwright sync API 的对象绑定到
    创建它的线程,my-agent 工具调用都在同一执行线程内顺序发生,这里的锁主要防"惰性启动"
    竞态(两个调用同时触发首次启动)和会话字典并发读写。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._playwright: Any = None  # sync_playwright().start() 的句柄(惰性)
        self._browser: Any = None  # 启动的 Chromium 实例(惰性)
        self._sessions: dict[str, _Session] = {}
        self._closed = False
        self._atexit_registered = False

    # ---- 惰性启动 ----------------------------------------------------------

    def _ensure_browser_locked(self) -> None:
        """惰性启动 Chromium。必须持锁调用。已启动则直接返回(零开销)。

        关键约束:没有任何浏览器工具被调用时,这个方法不会被触发 → 不起浏览器。
        启动失败(Chromium 二进制没下载)抛 BrowserUnavailableError(带安装指引)。
        """
        if self._browser is not None:
            return
        if self._closed:
            # 进程退出清理后又被调用(理论上不该发生),重置以便重新起。
            self._closed = False
        sync_playwright = _import_playwright_sync()
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:  # 二进制缺失/启动失败 → 统一成带指引的不可用错误
            self._teardown_playwright_locked()
            raise BrowserUnavailableError(
                f"无法启动 headless Chromium(可能是浏览器二进制未下载):{exc}。"
                "下载:python -m playwright install chromium。无法装时改用 web_fetch。"
            ) from exc
        if not self._atexit_registered:
            atexit.register(self.shutdown)
            self._atexit_registered = True

    def _get_or_create_session_locked(self, session_id: str) -> _Session:
        """取或建一个会话(独立 context + 空白 page)。必须持锁调用。"""
        session = self._sessions.get(session_id)
        if session is not None:
            return session
        self._ensure_browser_locked()
        context = self._browser.new_context()
        context.set_default_timeout(DEFAULT_ACTION_TIMEOUT_MS)
        context.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
        page = context.new_page()
        session = _Session(session_id=session_id, context=context, page=page)
        self._sessions[session_id] = session
        return session

    # ---- a11y 快照(ariaSnapshot 思路) ------------------------------------

    def _build_snapshot_locked(self, session: _Session, *, max_chars: int = SNAPSHOT_MAX_CHARS) -> dict[str, Any]:
        """生成当前页的 a11y 快照(给模型看页面有什么、能点什么),并刷新 ref_map。

        两部分:
          1. aria_snapshot():playwright 的 accessibility tree 文本(YAML 风格,角色+名字),
             对齐 长期助手 ariaSnapshot —— 无需视觉模型就能理解页面结构。
          2. 注入 data-agent-ref 并收集 ref→{role,name,tag},附在快照后面让模型知道
             可点/可填元素的 ref(click/type 用)。
        """
        page = session.page
        # 1. accessibility tree 文本
        try:
            aria_text = page.locator("body").aria_snapshot()
        except Exception:
            aria_text = ""
        # 2. 注入 ref 并收集可交互元素
        try:
            refs = page.evaluate(_INJECT_REFS_JS, MAX_REFS_PER_PAGE)
        except Exception:
            refs = []
        session.ref_map = {item["ref"]: item for item in refs if isinstance(item, dict) and item.get("ref")}
        interactive_lines = [
            f"  [ref={item['ref']}] {item.get('role', '')} \"{item.get('name', '')}\"".rstrip()
            for item in refs
            if isinstance(item, dict)
        ]
        sections = []
        if aria_text.strip():
            sections.append("# accessibility tree\n" + aria_text.strip())
        if interactive_lines:
            sections.append(
                "# interactive elements (用 ref 调 browser_click / browser_type)\n"
                + "\n".join(interactive_lines)
            )
        snapshot = "\n\n".join(sections) if sections else "(页面无可读 accessibility 内容)"
        truncated = False
        if len(snapshot) > max_chars:
            snapshot = snapshot[:max_chars].rstrip() + "\n...[快照已截断,只显示前部]"
            truncated = True
        return {
            "title": _safe_title(page),
            "url": _safe_url(page),
            "snapshot": snapshot,
            "element_count": len(session.ref_map),
            "snapshot_truncated": truncated,
        }

    # ---- 对外动作(供 browser_tools 调用) --------------------------------

    def navigate(self, session_id: str, url: str, *, timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS) -> dict[str, Any]:
        """打开页面,返回标题 + a11y 快照。失败抛带分类的异常,由调用层转 error_code。"""
        with self._lock:
            session = self._get_or_create_session_locked(session_id)
            page = session.page
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            # 给动态内容一点稳定时间(忽略 networkidle 超时,拿到什么算什么)。
            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5_000))
            except Exception:
                pass
            return self._build_snapshot_locked(session)

    def snapshot(self, session_id: str) -> dict[str, Any]:
        """当前页的 a11y 快照。会话不存在/未导航 → 抛 ValueError(由调用层转参数错)。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(
                    "当前没有打开的浏览器页面;先用 browser_navigate 打开一个 URL,再取快照。"
                )
            return self._build_snapshot_locked(session)

    def click(self, session_id: str, ref_or_selector: str) -> dict[str, Any]:
        """点击元素(ref 或 CSS selector)。点完返回新快照(页面可能已变化)。

        用 no_wait_after=True:click 本身不阻塞等"点击触发的导航完成"(否则点提交按钮
        触发的跳转/挂起会把工具卡到超时)。点完自己做一个有界的 load-state 等待来让
        页面稳定,等不到就拿当前状态快照。
        """
        with self._lock:
            session = self._require_session_locked(session_id)
            selector = self._resolve_selector(session, ref_or_selector)
            session.page.click(selector, timeout=DEFAULT_ACTION_TIMEOUT_MS, no_wait_after=True)
            self._settle_after_action_locked(session)
            result = self._build_snapshot_locked(session)
            result["clicked"] = ref_or_selector
            return result

    def _settle_after_action_locked(self, session: _Session) -> None:
        """动作后有界等待页面稳定(domcontentloaded 优先,拿不到就算了)。不抛异常。"""
        for state in ("domcontentloaded", "networkidle"):
            try:
                session.page.wait_for_load_state(state, timeout=3_000)
            except Exception:
                pass

    def type_text(self, session_id: str, ref_or_selector: str, text: str) -> dict[str, Any]:
        """填表单(清空后输入)。返回新快照。"""
        with self._lock:
            session = self._require_session_locked(session_id)
            selector = self._resolve_selector(session, ref_or_selector)
            session.page.fill(selector, text, timeout=DEFAULT_ACTION_TIMEOUT_MS)
            result = self._build_snapshot_locked(session)
            result["typed"] = text
            result["element"] = ref_or_selector
            return result

    def close(self, session_id: str) -> dict[str, Any]:
        """关一个会话释放资源。最后一个会话关掉后,浏览器仍留着(下次复用);
        显式全关用 shutdown。不存在的会话返回 already_closed(幂等,不报错)。"""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return {"session_id": session_id, "status": "already_closed"}
            _safe_close_context(session.context)
            return {"session_id": session_id, "status": "closed"}

    # ---- 内部工具 ----------------------------------------------------------

    def _require_session_locked(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(
                "当前没有打开的浏览器页面;先用 browser_navigate 打开一个 URL。"
            )
        return session

    def _resolve_selector(self, session: _Session, ref_or_selector: str) -> str:
        """把模型给的 ref(eN / @eN / ref=eN)解析成 data-agent-ref 属性选择器;
        否则当作原生 CSS selector 透传。"""
        token = (ref_or_selector or "").strip()
        if not token:
            raise ValueError("ref/selector 不能为空。")
        normalized = token.lstrip("@")
        if normalized.startswith("ref="):
            normalized = normalized[len("ref="):]
        if normalized in session.ref_map:
            return f"[data-agent-ref='{normalized}']"
        # 形如 e12 但当前 ref_map 里没有 → 可能是过期快照的 ref,提示模型重拍快照。
        if normalized and normalized[0] == "e" and normalized[1:].isdigit():
            raise ValueError(
                f"ref '{normalized}' 在当前页面快照里不存在(可能页面已变化或快照过期);"
                "先用 browser_snapshot 重新获取当前页的 ref 再操作。"
            )
        # 当作 CSS selector 透传。
        return token

    def _teardown_playwright_locked(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def shutdown(self) -> None:
        """关闭所有会话 + 浏览器 + playwright。atexit 兜底调用,也可显式调用。幂等。"""
        with self._lock:
            for session in list(self._sessions.values()):
                _safe_close_context(session.context)
            self._sessions.clear()
            self._teardown_playwright_locked()
            self._closed = True

    def active_session_ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())


def _safe_close_context(context: Any) -> None:
    try:
        context.close()
    except Exception:
        pass


def _safe_title(page: Any) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def _safe_url(page: Any) -> str:
    try:
        return page.url
    except Exception:
        return ""


# 进程内单实例。browser_tools 的各工具从这里取会话。惰性:构造它不启动浏览器,
# 第一次 navigate/snapshot/... 才真正起 Chromium。
browser_session_manager = BrowserSessionManager()


__all__ = [
    "BrowserSessionManager",
    "BrowserUnavailableError",
    "browser_session_manager",
    "DEFAULT_NAV_TIMEOUT_MS",
    "DEFAULT_ACTION_TIMEOUT_MS",
    "SNAPSHOT_MAX_CHARS",
]
