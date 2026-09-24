# LLM: TUI 端插件面板只保存本地显示偏好和 Gateway 已校验的展示结果；不导入插件代码、不执行插件、不写宿主状态。
#   轮询线程只在有面板打开时工作，失败指数退避；宿主报告不可用（停用/卸载/换代）即移除面板并停止轮询。
#   面板区域高度有上限，渲染异常只隐藏面板，不能影响核心输入。修改时同步 PLUGIN_DISPLAY.md 与 test_tui_plugin_panels。
# 模块用途: 在输入框上方显示已打开的插件面板，并随运行状态刷新。
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

MAX_VISIBLE_PANELS = 2
POLL_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 10.0
PANEL_BODY_LINES = 6
_STATE_LABEL = {"loading": "加载中", "refreshing": "", "ready": "", "error": "出错", "unavailable": "不可用"}


# LLM: 视图只含宿主返回并已由核心校验的字段；title 为空时回退到插件/面板编号，不猜测插件内容。
# 类用途: 一个已打开面板的最新显示内容。
@dataclass(frozen=True)
class PanelView:
    plugin_id: str
    panel_id: str
    title: str
    state: str
    body: tuple[str, ...] = ()
    error: str = ""


# LLM: 状态只在本进程内存中；toggle 是用户显式操作，宿主不可用时由轮询结果移除，不从文字推断插件状态。
# 类用途: 管理已打开的插件面板、后台刷新和渲染所需的快照。
class PluginPanelBoard:
    # LLM: fetch 必须是 PluginCommandClient.panels 这类只读请求；invalidate 只触发重绘。
    # 函数用途: 创建面板板，后台线程惰性启动。
    def __init__(self, fetch: Callable[[tuple[tuple[str, str], ...]], dict], invalidate: Callable[[], None], *,
                 clock: Callable[[], float] = time.monotonic, start_thread: bool = True,
                 stop_event: threading.Event | None = None) -> None:
        self._fetch = fetch
        self._invalidate = invalidate
        self._clock = clock
        self._start_thread = start_thread
        self._condition = threading.Condition()
        self._visible: list[tuple[str, str]] = []
        self._views: dict[tuple[str, str], PanelView] = {}
        self._closed = False
        self._thread: threading.Thread | None = None
        self._backoff = POLL_SECONDS
        self._stop_event = stop_event

    # LLM: 打开数量有上限，超限不静默挤掉已开面板；返回值供 TUI 给出明确提示。
    # 函数用途: 打开或关闭一个面板，返回 "opened" / "closed" / "limit"。
    def toggle(self, plugin_id: str, panel_id: str, title: str = "") -> str:
        key = (plugin_id, panel_id)
        with self._condition:
            if key in self._visible:
                self._visible.remove(key)
                self._views.pop(key, None)
                outcome = "closed"
            elif len(self._visible) >= MAX_VISIBLE_PANELS:
                return "limit"
            else:
                self._visible.append(key)
                self._views[key] = PanelView(plugin_id, panel_id, title or f"{plugin_id}/{panel_id}", "loading")
                self._backoff = POLL_SECONDS
                outcome = "opened"
                self._ensure_thread()
            self._condition.notify_all()
        self._invalidate()
        return outcome

    # 函数用途: 返回当前是否有打开的面板（布局过滤条件用）。
    def has_visible(self) -> bool:
        with self._condition:
            return bool(self._visible)

    # 函数用途: 返回已打开面板的显示快照，按打开顺序排列。
    def views(self) -> tuple[PanelView, ...]:
        with self._condition:
            return tuple(self._views[key] for key in self._visible if key in self._views)

    # LLM: 一次只读请求；空结果视为传输失败并退避，不改变可见性。宿主报告 unavailable 的面板被移除。
    # 函数用途: 查询一次并更新面板内容（后台线程与测试共用）。
    def poll_once(self) -> bool:
        with self._condition:
            requested = tuple(self._visible)
        if not requested:
            return True
        try:
            result = self._fetch(requested)
        except Exception:  # noqa: BLE001 面板请求失败只退避，不能影响输入线程
            result = {}
        panels = result.get("panels") if isinstance(result, dict) else None
        if not isinstance(panels, list):
            with self._condition:
                self._backoff = min(MAX_BACKOFF_SECONDS, self._backoff * 2)
            return False
        changed = False
        with self._condition:
            self._backoff = POLL_SECONDS
            for row in panels:
                if not isinstance(row, dict):
                    continue
                key = (str(row.get("plugin_id") or ""), str(row.get("panel_id") or ""))
                if key not in self._visible:
                    continue
                if row.get("state") == "unavailable":
                    self._visible.remove(key)
                    self._views.pop(key, None)
                    changed = True
                    continue
                current = self._views.get(key)
                view = PanelView(key[0], key[1], str(row.get("title") or (current.title if current else "")),
                                 str(row.get("state") or ""), _body(row.get("display"), current),
                                 str(row.get("error") or ""))
                if view != current:
                    self._views[key] = view
                    changed = True
        if changed:
            self._invalidate()
        return True

    # LLM: 关闭后线程退出，不再发任何请求；可重复调用。
    # 函数用途: TUI 退出时停止面板刷新。
    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._visible.clear()
            self._views.clear()
            self._condition.notify_all()

    # LLM: 守护线程；没有面板时在条件变量上等待，不空转。
    # 函数用途: 需要时启动后台刷新线程。
    def _ensure_thread(self) -> None:
        if not self._start_thread or self._closed or (self._thread is not None and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._loop, name="tui-plugin-panels", daemon=True)
        self._thread.start()

    # LLM: TUI 的 stop_event 与 close 都能让循环退出；没有面板时每秒醒来只检查退出，不发请求。
    # 函数用途: 后台循环：有面板就按间隔刷新，没有就等待，关闭或 TUI 退出即结束。
    def _loop(self) -> None:
        while True:
            with self._condition:
                while not self._stopped() and not self._visible:
                    self._condition.wait(timeout=1.0)
                if self._stopped():
                    return
            self.poll_once()
            with self._condition:
                if self._stopped():
                    return
                self._condition.wait(timeout=self._backoff)

    # 函数用途: 判断面板刷新是否应停止（显式关闭或 TUI 已退出）。
    def _stopped(self) -> bool:
        return self._closed or (self._stop_event is not None and self._stop_event.is_set())


# LLM: 宿主结果已由核心校验；这里只排版，不再解释字段含义。没有新内容时保留上一次正文，避免闪烁。
# 函数用途: 把宿主返回的展示描述转成面板正文行。
def _body(display: object, current: PanelView | None) -> tuple[str, ...]:
    if not isinstance(display, dict):
        return current.body if current else ()
    kind = display.get("kind")
    if kind == "text":
        lines = [str(line) for line in display.get("lines") or []]
    elif kind == "table":
        columns = [str(cell) for cell in display.get("columns") or []]
        rows = [[str(cell) for cell in row] for row in display.get("rows") or []]
        widths = [max([len(columns[i])] + [len(row[i]) for row in rows if i < len(row)]) for i in range(len(columns))]
        lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(columns))]
        lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    elif kind == "status":
        lines = [f"{item.get('label', '')}：{item.get('value', '')}" for item in display.get("fields") or []
                 if isinstance(item, dict)]
    else:
        lines = []
    if display.get("truncated"):
        lines.append("…（内容已截断）")
    return tuple(lines)


# LLM: 纯函数；每个面板一行标题加至多 PANEL_BODY_LINES 行正文，总高度有上限，不读时钟或网络。
# 函数用途: 生成面板区域的 prompt_toolkit 格式化片段。
def render_plugin_panels(views: tuple[PanelView, ...]) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    for view in views:
        label = _STATE_LABEL.get(view.state, "")
        suffix = f"（{label}）" if label else ""
        fragments.append(("bold", f"─ {view.title}{suffix}\n"))
        body = list(view.body)
        if view.state == "error" and view.error:
            body = [view.error] + body
        if len(body) > PANEL_BODY_LINES:
            body = body[: PANEL_BODY_LINES - 1] + [f"…还有 {len(body) - PANEL_BODY_LINES + 1} 行"]
        for line in body:
            fragments.append(("", f"  {line}\n"))
    return fragments


# LLM: 高度由内容决定且有上限；渲染异常时返回空片段，面板隐藏而不是让界面报错。
# 函数用途: 创建显示面板区域的窗口。
def make_plugin_panel_window(board: PluginPanelBoard):
    from prompt_toolkit.layout import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension

    # 函数用途: 为窗口提供当前面板片段，出错时隐藏。
    def fragments():
        try:
            return render_plugin_panels(board.views())
        except Exception:  # noqa: BLE001 面板渲染故障不能冻结核心界面
            return []

    max_height = MAX_VISIBLE_PANELS * (PANEL_BODY_LINES + 1)
    return Window(content=FormattedTextControl(fragments), height=Dimension(min=0, max=max_height),
                  dont_extend_height=True, wrap_lines=False)
