from __future__ import annotations

import threading

from agent_py_agent.cli.chat_parts.tui_clipboard import ClipboardProjector


def test_copy_serializes_writes_and_coalesces_pending_selection() -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    writes, notices = [], []

    def native(text):
        if text == "earlier":
            started.set()
            assert release.wait(2)
        writes.append(("native", text))
        return True

    def tmux(text):
        writes.append(("tmux", text))
        return True

    projector = ClipboardProjector(native, tmux)
    projector.submit("earlier", native=True, tmux=True, terminal_copy=lambda: False,
                     dispatch=lambda callback: callback(), completed=notices.append)
    assert started.wait(1)
    projector.submit("intermediate", native=True, tmux=True, terminal_copy=lambda: False,
                     dispatch=lambda callback: callback(), completed=notices.append)
    projector.submit("latest", native=True, tmux=True, terminal_copy=lambda: False,
                     dispatch=lambda callback: callback(),
                     completed=lambda result: (notices.append(result), finished.set()))
    assert writes == []
    release.set()
    assert finished.wait(2)
    assert writes == [("native", "earlier"), ("native", "latest"), ("tmux", "latest")]
    assert len(notices) == 1
    assert notices[0].native is True and notices[0].tmux is True
    assert notices[0].chars == len("latest")


def test_close_discards_pending_and_suppresses_receipt() -> None:
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    writes, receipts = [], []

    def native(text):
        started.set()
        assert release.wait(2)
        writes.append(text)
        stopped.set()
        return True

    projector = ClipboardProjector(native, lambda text: writes.append(text))
    for text in ("inflight", "pending"):
        projector.submit(text, native=True, tmux=True, terminal_copy=lambda: True,
                         dispatch=lambda callback: callback(), completed=receipts.append)
        assert started.wait(1)
    projector.close()
    worker = projector._worker
    release.set()
    assert stopped.wait(1)
    worker.join(1)
    assert not worker.is_alive()
    assert writes == ["inflight"]
    assert receipts == []


def test_late_ui_receipt_cannot_override_new_copy() -> None:
    first_ready, second_ready = threading.Event(), threading.Event()
    callbacks, receipts, terminal_writes = [], [], []
    projector = ClipboardProjector(lambda text: True, lambda text: True)

    def dispatch(callback):
        callbacks.append(callback)
        (first_ready if len(callbacks) == 1 else second_ready).set()

    for text, ready in (("first", first_ready), ("second", second_ready)):
        projector.submit(text, native=True, tmux=False,
                         terminal_copy=lambda text=text: terminal_writes.append(text) is None,
                         dispatch=dispatch, completed=receipts.append)
        assert ready.wait(1)
    for callback in callbacks:
        callback()
    assert len(receipts) == 1 and receipts[0].chars == len("second")
    assert terminal_writes == ["second"]


def test_channel_failures_are_independent_and_terminal_send_is_not_ack() -> None:
    finished = threading.Event()
    receipts = []
    projector = ClipboardProjector(lambda text: False, lambda text: True)
    projector.submit("文字", native=True, tmux=True, terminal_copy=lambda: True,
                     dispatch=lambda callback: callback(),
                     completed=lambda result: (receipts.append(result), finished.set()))
    assert finished.wait(1)
    result = receipts[0]
    assert result.native is False and result.tmux is True and result.osc_sent is True
    assert "系统剪贴板未确认" in result.notice()
    assert "tmux" in result.notice()


def test_keyboard_copy_pipeline_keeps_latest_external_text_and_honest_notice(monkeypatch) -> None:
    import base64
    from types import SimpleNamespace

    from prompt_toolkit.clipboard import InMemoryClipboard

    from agent_py_agent.cli.chat_parts import tui_keybindings as keys

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    writes, raw, notices = [], [], []
    app = SimpleNamespace(clipboard=InMemoryClipboard(),
                          output=SimpleNamespace(write_raw=raw.append, flush=lambda: None))

    def native(text):
        if text == "旧复制":
            started.set()
            assert release.wait(2)
        writes.append(("native", text))
        return False

    def notify(notice):
        notices.append(notice)
        if "系统剪贴板未确认" in notice:
            finished.set()

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setenv("TMUX", "isolated-no-command-executed")
    monkeypatch.setattr(keys, "_copy_native_clipboard", native)
    monkeypatch.setattr(keys, "_load_tmux_clipboard_buffer",
                        lambda text: writes.append(("tmux", text)) is None)
    keys._write_selection_clipboard(app, "旧复制", notify=notify)
    assert started.wait(1)
    keys._write_selection_clipboard(app, "最新复制", notify=notify)
    assert raw == [] and all("正在复制" in text for text in notices)
    release.set()
    assert finished.wait(2)
    assert writes == [("native", "旧复制"), ("native", "最新复制"), ("tmux", "最新复制")]
    assert app.clipboard.get_data().text == "最新复制"
    assert len(raw) == 1 and base64.b64encode("最新复制".encode()).decode() in raw[0]
    assert "tmux 缓冲" in notices[-1] and "系统剪贴板未确认" in notices[-1]


def test_application_future_close_cancels_pending_copy_and_terminal_write(monkeypatch) -> None:
    from concurrent.futures import Future
    from types import SimpleNamespace

    from prompt_toolkit.clipboard import InMemoryClipboard

    from agent_py_agent.cli.chat_parts import tui_keybindings as keys

    started, release = threading.Event(), threading.Event()
    raw, writes = [], []
    future = Future()
    app = SimpleNamespace(clipboard=InMemoryClipboard(), future=future,
                          output=SimpleNamespace(write_raw=raw.append, flush=lambda: None))

    def native(text):
        started.set()
        assert release.wait(2)
        writes.append(text)
        return True

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(keys, "_copy_native_clipboard", native)
    keys._write_selection_clipboard(app, "已经开始")
    assert started.wait(1)
    keys._write_selection_clipboard(app, "应被丢弃")
    worker = app._my_agent_clipboard_projector._worker
    future.set_result(0)
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert writes == ["已经开始"] and raw == []


def test_terminal_failure_does_not_erase_native_success() -> None:
    finished = threading.Event()
    results = []

    def terminal():
        raise OSError("terminal closed")

    projector = ClipboardProjector(lambda text: True, lambda text: False)
    projector.submit("复制", native=True, tmux=False, terminal_copy=terminal,
                     dispatch=lambda callback: callback(),
                     completed=lambda result: (results.append(result), finished.set()))
    assert finished.wait(1)
    assert results[0].native is True and results[0].osc_sent is False
    assert "到系统剪贴板" in results[0].notice()
