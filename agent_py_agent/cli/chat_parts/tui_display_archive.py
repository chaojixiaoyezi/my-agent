# LLM: 只读展示归档的网络操作不得在UI线程执行，不得直接读取宿主路径或产生模型请求。
# 模块用途: 按用户翻到的当前页异步请求Gateway，失败由用户重试，不轮询、不自动拉取全文。

from __future__ import annotations

import threading


# LLM: 每个TUI一条在途读取，引用只交给当前已认证Gateway client；owner权限由服务端裁决。
# 类用途: 让巨量历史工具输出按需读取，不阻塞输入、停止或其他窗口。
class TuiDisplayArchiveReader:
    # LLM: 只持当前app和session，不建立新会话、线程或执行任务。
    # 函数用途: 绑定当前TUI的原文分页状态与Gateway客户端。
    def __init__(self, application, params, state) -> None:
        self.application, self.params, self.state = application, params, state
        self._lock = threading.Lock()
        self._loading = False

    # LLM: render只触发读取意图，重复重绘不会再发同一请求，耗时网络转到worker。
    # 函数用途: 异步读取一个当前页，立即把控制权交回键盘和滚轮。
    def request(self, reference: dict, page_index: int) -> None:
        with self._lock:
            if self._loading or self.params.stop_event.is_set():
                return
            self._loading = True
        threading.Thread(target=self._read, args=(dict(reference), page_index),
                         name="tui-display-page", daemon=True).start()

    # LLM: 请求只调用鉴权client方法；退出时不回调，异常不泄露地址、路径或密钥。
    # 函数用途: 读取后将结果安排回UI线程，失败保留预览。
    def _read(self, reference: dict, page_index: int) -> None:
        try:
            payload = self.params.agent.request_display_page(
                session_id=self.params.current_session_id, reference=reference, page_index=page_index,
            )
        except Exception:  # noqa: BLE001 公开展示读取不能击穿TUI
            payload = {"ok": False}
        if not isinstance(payload, dict):
            payload = {"ok": False}
        loop = self.application.loop
        if loop is not None and not self.params.stop_event.is_set():
            try:
                loop.call_soon_threadsafe(self._accept, reference, page_index, payload)
            except RuntimeError:
                pass

    # LLM: UI提交完成后才清在途标志，避免网络完成与重绘之间重复发送同一页请求。
    # 函数用途: 原子结束本次显示读取并触发下一页按需重绘，不执行模型或工具。
    def _accept(self, reference: dict, page_index: int, payload: dict) -> None:
        self.state.accept_complete_page(reference, page_index, payload)
        with self._lock:
            self._loading = False
        self.application.invalidate()


# LLM: 接线不触发预取；只有Ctrl+E翻到归档页后才读当前页。
# 函数用途: 为主/子统一的详细视图安装按需Gateway分页读取。
def wire_tui_display_archive(application, params, view) -> None:
    reader = TuiDisplayArchiveReader(application, params, view.transcript_state)
    view.transcript_state.request_complete_page = reader.request
