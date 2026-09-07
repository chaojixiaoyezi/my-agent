# LLM: 本模块只协调显示分页，沿现有 authenticated history API；不调用模型、不写会话、不接管任何代理。
# 模块用途: 用户上翻时异步加载一页旧消息，保证输入不阻塞、页面不串、失败可以再次上翻重试。

from __future__ import annotations

import threading

from .history import chat_history_max_turns, load_gateway_chat_history


# LLM: 一个控制器绑定当前 app 的 root session；child 自己的事件流和 cursor 继续由导航管理，不使用 root 分页。
# 类用途: 在后台读取旧页，并把同一页的显示更新送回 UI 线程。
class TuiHistoryPager:
    # LLM: 只保存本客户端对象和进程内请求锁，无额外存储/轮询器/任务执行权。
    # 函数用途: 绑定当前会话、视口和停止信号。
    def __init__(self, application, params, view) -> None:
        self.application, self.params, self.view = application, params, view
        self.runtime = params.tui_runtime
        self._lock = threading.Lock()
        self._loading = False

    # LLM: 单次上翻最多一个在途读取；无更早数据、子页、退出或重复按键不发网络请求。
    # 函数用途: 响应滚轮/PgUp/Home，并立即返回给输入事件循环。
    def request(self, home: bool = False) -> None:
        with self._lock:
            before = self.runtime.history_before_cursor
            if (self._loading or before <= 0 or self.params.stop_event.is_set()
                    or self.view.provider.state_store is not self.runtime.store):
                return
            self._loading = True
        self.runtime.set_notice("正在读取更早的历史…", duration_seconds=15)
        threading.Thread(target=self._read, args=(before, home), name="tui-history-page", daemon=True).start()

    # LLM: worker 只读一页；网络/解析失败交回 UI，不能重置游标或自动无限重试；退出后丢弃迟到结果。
    # 函数用途: 执行一次后台读取，完成后安排同页面的显示更新。
    def _read(self, before: int, home: bool) -> None:
        try:
            page = load_gateway_chat_history(
                self.params.agent, self.params.current_session_id,
                max_turns=chat_history_max_turns(self.params.agent.config), before_message_cursor=before,
            )
        except Exception:  # noqa: BLE001 展示错误不得影响客户端或泄露服务端私有路径
            page = None
        if self.params.stop_event.is_set() or self.application.loop is None:
            with self._lock:
                self._loading = False
            return
        try:
            self.application.loop.call_soon_threadsafe(self._apply, before, home, page)
        except RuntimeError:
            with self._lock:
                self._loading = False

    # LLM: UI 线程更新只接受严格前移的同一旧游标；当前已换 child 或关闭时不消费响应，不重置 live cursor。
    # 函数用途: 将成功的旧页放回前部，保留阅读锚点及 Ctrl+O 冻结内容，失败保留原文。
    def _apply(self, before: int, home: bool, page) -> None:
        try:
            if (self.params.stop_event.is_set() or self.view.provider.state_store is not self.runtime.store
                    or self.runtime.history_before_cursor != before):
                return
            if (page is None or page.load_errors or page.display_events is None
                    or not 0 <= page.before_message_cursor < before):
                self.runtime.set_notice("更早历史读取失败；现有内容保留，再次上翻可重试", duration_seconds=6)
                return
            controls = (self.view.control, self.view.modal_control)
            anchors = [control.history_anchor() for control in controls]
            homes = [home and control.current_line() == 0 for control in controls]
            ids = self.runtime.prepend_history_page(
                page.display_events or (), expected_before=before, next_before=page.before_message_cursor,
            )
            self.view.transcript_state.prepend_history(self.runtime.store.snapshot(), ids)
            for control, anchor, at_home in zip(controls, anchors, homes):
                control.restore_history_anchor(anchor, home=at_home, block_ids=ids)
            self.runtime.set_notice(
                "已补入更早历史，继续上翻可再加载" if page.before_message_cursor else "已到会话最早记录",
                duration_seconds=3,
            )
        finally:
            with self._lock:
                self._loading = False
            self.application.invalidate()


# LLM: 普通/详情滚动共用同一个 pager；仅建立客户端回调，不在安装时读取历史或增加模型请求。
# 函数用途: 为当前应用接通按需旧页加载。
def wire_tui_history_pager(application, params, view) -> None:
    pager = TuiHistoryPager(application, params, view)
    view.control.set_older_history_callback(pager.request)
    view.modal_control.set_older_history_callback(pager.request)
