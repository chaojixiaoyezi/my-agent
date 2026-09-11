# LLM: 本模块只串行投影显式用户选区，不读取系统剪贴板或会话；每 app 至多一个在途和一个最新待处理选区。
# 模块用途: 避免较慢的旧复制覆盖新复制，并按实际通道结果生成准确回执；退出后不启动剩余复制。

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass


# LLM: native/tmux 的 None 表示未尝试，False 表示失败；OSC sent 仅说明写控制序列，不证明终端授权或接收。
# 类用途: 保存一次复制的分通道结果，不把应用内剪贴板、tmux 缓冲和系统剪贴板混为一谈。
@dataclass(frozen=True)
class ClipboardResult:
    chars: int
    native: bool | None
    tmux: bool | None
    osc_sent: bool

    # LLM: 只有 native=True 可以声称系统写入成功；tmux buffer 成功与 OSC 写出均保留外层系统未确认边界。
    # 函数用途: 把确定的复制结果转成中文提示，不泄露选区正文。
    def notice(self) -> str:
        if self.native is True:
            return f"已复制 {self.chars} 个字符到系统剪贴板"
        if self.tmux is True:
            return f"已复制 {self.chars} 个字符到 tmux 缓冲；系统剪贴板未确认"
        if self.osc_sent:
            return f"已发送 {self.chars} 个字符的终端复制请求；系统剪贴板未确认"
        return f"已保留 {self.chars} 个字符到应用剪贴板；系统复制未成功，可用 Ctrl+V"


# LLM: job 只在本 app 复制生命周期内持有文本和 UI 回调；被更新的 pending job 即刻释放，不能无界保存选区。
# 类用途: 将选区、通道选择和当前页面回执绑在一起，避免切换子代理后提示串页。
@dataclass(frozen=True)
class _ClipboardJob:
    generation: int
    text: str
    native: bool
    tmux: bool
    terminal_copy: Callable[[], bool]
    dispatch: Callable[[Callable[[], None]], None]
    completed: Callable[[ClipboardResult], None]


# LLM: 复制进程由注入的有超时 helper 执行；单 worker 保证副作用顺序，generation 同时防止迟到 UI 回执覆盖新选区。
# 类用途: 给一个 TUI 应用维护有界串行复制，保留最新意图且不阻塞键盘或鼠标输入。
class ClipboardProjector:
    # LLM: 两个 helper 必须返回实际 bool 结果且自行限制阻塞时间；构造本身不启动线程或写剪贴板。
    # 函数用途: 绑定本机和 tmux 的复制方法，初始化关闭状态和最新任务槽。
    def __init__(self, native_copy: Callable[[str], bool], tmux_copy: Callable[[str], bool]) -> None:
        self._native_copy, self._tmux_copy = native_copy, tmux_copy
        self._lock = threading.Lock()
        self._pending: _ClipboardJob | None = None
        self._generation = 0
        self._closed = False
        self._worker: threading.Thread | None = None

    # LLM: 同 app 每次提交只替换一个 pending 槽；已经在写的旧请求不能取消操作系统副作用，但必定先于最新写入完成。
    # 函数用途: 接收新的复制意图，空闲时启动一个短生命周期 worker，忙碌时合并中间选区。
    def submit(self, text: str, *, native: bool, tmux: bool, terminal_copy: Callable[[], bool],
               dispatch: Callable[[Callable[[], None]], None],
               completed: Callable[[ClipboardResult], None]) -> None:
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            self._pending = _ClipboardJob(self._generation, text, native, tmux, terminal_copy, dispatch, completed)
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="my-agent-tui-clipboard", daemon=True)
                self._worker.start()

    # LLM: close 不阻塞 UI，也不清除任何实际剪贴板；丢弃 pending 并让有超时的在途 helper 自行收尾，禁止后续通道和回执。
    # 函数用途: TUI 退出后停止复制调度并释放尚未处理的文字和页面回调。
    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = None

    # LLM: 此判断只是同一 UI 操作代次，不是用户/代理身份或持久状态；失效任务不得继续投影第二通道。
    # 函数用途: 判断复制是否仍是当前应用最新且未关闭的意图。
    def _current(self, generation: int) -> bool:
        with self._lock:
            return not self._closed and generation == self._generation

    # LLM: worker 顺序执行 helper；每次取完最新槽后释放锁再执行外部命令，不能阻塞 UI 提交或关闭。
    # 函数用途: 按序处理复制；旧任务结束后直接处理最新选区，中间任务不排长队。
    def _run(self) -> None:
        while True:
            with self._lock:
                job, self._pending = self._pending, None
                if job is None or self._closed:
                    self._worker = None
                    return
            native = self._copy(self._native_copy, job.text) if job.native else None
            if not self._current(job.generation):
                continue
            tmux = self._copy(self._tmux_copy, job.text) if job.tmux else None
            result = ClipboardResult(len(job.text), native, tmux, False)
            if self._current(job.generation):
                try:
                    job.dispatch(lambda job=job, result=result: self._deliver(job, result))
                except RuntimeError:
                    self.close()

    # LLM: UI dispatch 后再次校验代次，OSC 同样只由最新任务发布；旧 tmux 不能在新 OSC 后完成并回滚外层剪贴板。
    # 函数用途: 在界面线程发送当前终端复制序列，并向发起页面汇报分通道结果；关闭后不再写终端。
    def _deliver(self, job: _ClipboardJob, result: ClipboardResult) -> None:
        if self._current(job.generation):
            try:
                sent = job.terminal_copy() is True
            except Exception:  # noqa: BLE001 终端已关闭或发送失败不影响应用/native/tmux 已有结果
                sent = False
            job.completed(ClipboardResult(result.chars, result.native, result.tmux, sent))

    # LLM: 一个 clipboard helper 的异常只能标记该通道失败，不得终止队列、清空选区或跳过另一个可用通道。
    # 函数用途: 把平台复制异常归一为失败，避免后台线程悄悄退出后永远不再处理复制。
    @staticmethod
    def _copy(copy: Callable[[str], bool], text: str) -> bool:
        try:
            return copy(text) is True
        except Exception:  # noqa: BLE001 系统复制失败不应破坏 TUI，也不输出私有选区
            return False
