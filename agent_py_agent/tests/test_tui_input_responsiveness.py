from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document

from agent_py_agent.cli.chat_parts.tui_input import TuiHistoryAutoSuggest, TuiInputCompleter


# LLM: 控制慢辅助读取的释放，不用真实网络或任务正文；UI tick 必须能在读取结束前运行。
# 函数用途: 检查输入辅助任务等待外部读取时，事件循环是否还能处理后续键盘事件。
async def _assert_ui_can_tick(factory, started, release):
    timer = threading.Timer(0.35, release.set)
    timer.start()
    task = asyncio.create_task(factory())
    try:
        await asyncio.sleep(0.025)
        assert started.is_set(), '辅助读取应已开始，避免空跑通过'
        assert not release.is_set(), '输入事件循环被同步辅助读取阻塞'
    finally:
        release.set()
        await task
        timer.cancel()


def test_history_suggestion_does_not_block_input_event_loop():
    started, release = threading.Event(), threading.Event()

    def get_strings():
        started.set()
        assert release.wait(2)
        return ['abcdef']

    suggest = TuiHistoryAutoSuggest()
    buffer = SimpleNamespace(history=SimpleNamespace(get_strings=get_strings))
    asyncio.run(_assert_ui_can_tick(
        lambda: suggest.get_suggestion_async(buffer, Document('abc')), started, release,
    ))


def test_path_completion_does_not_block_input_event_loop(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    completer = TuiInputCompleter(tmp_path)

    def completions(document, event):
        started.set()
        assert release.wait(2)
        yield Completion('example')

    monkeypatch.setattr(completer, 'get_completions', completions)

    async def collect():
        return [item async for item in completer.get_completions_async(Document('@e'), CompleteEvent())]

    asyncio.run(_assert_ui_can_tick(collect, started, release))
