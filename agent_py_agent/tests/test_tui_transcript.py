from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
from agent_py_agent.cli.chat_parts.tui_view import TuiFrameProvider
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


def test_frozen_detailed_transcript_keeps_live_refresh_warning_and_recovers():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("frozen-refresh-warning")
    state = TuiTranscriptModeState()
    state.enter(runtime.store.snapshot())
    provider = TuiFrameProvider(
        runtime.store,
        lambda width: TuiRenderContext(
            width=width, detailed_transcript=True,
            background_sync_failed=runtime.store.snapshot().background_sync_failed,
        ),
        transcript_state=state,
    )
    normal_footer = provider.frame(100).footer
    runtime.publish_background_sync_status(ok=False)
    assert "状态刷新失败" in fragments_text(provider.frame(100).footer)
    runtime.publish_background_sync_status(ok=True)
    assert provider.frame(100).footer == normal_footer
    assert state.snapshot().active is True


def _provider(
    store: TuiStateStore,
    state: TuiTranscriptModeState,
) -> TuiFrameProvider:
    return TuiFrameProvider(
        store,
        lambda width: TuiRenderContext(
            width=width,
            detailed_transcript=state.snapshot().active,
            show_all=state.snapshot().show_all,
        ),
        transcript_state=state,
    )


def test_transcript_freezes_view_until_exit() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("transcript-freeze", clock=lambda: 1.0)
    store.publish(seq.emit("user_message", "completed", "one", {"text": "alpha"}))
    state = TuiTranscriptModeState()
    provider = _provider(store, state)

    state.enter(store.snapshot())
    store.publish(seq.emit("user_message", "completed", "two", {"text": "beta"}))
    frozen = "\n".join(
        fragments_text(line) for line in provider.frame(80).transcript_lines
    )
    state.exit()
    live = "\n".join(
        fragments_text(line) for line in provider.frame(80).transcript_lines
    )

    assert "alpha" in frozen
    assert "beta" not in frozen
    assert "beta" in live


def test_transcript_incremental_search_highlights_and_navigates() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("transcript-search", clock=lambda: 1.0)
    for index, text in enumerate(("alpha one", "middle", "alpha two")):
        store.publish(
            seq.emit(
                "system_message",
                "completed",
                f"line-{index}",
                {"text": text},
            )
        )
    state = TuiTranscriptModeState()
    provider = _provider(store, state)
    state.enter(store.snapshot())
    state.open_search(anchor_line=0)
    state.update_search_query("alpha")

    frame = provider.frame(80)
    snapshot = state.snapshot()

    assert snapshot.match_count == 2
    assert snapshot.current_match == 1
    assert any(
        "tui-search-current" in style
        for line in frame.transcript_lines
        for style, _text in line
    )
    first_line = state.commit_search()
    second_line = state.navigate()
    previous_line = state.navigate(reverse=True)
    assert first_line is not None
    assert second_line is not None and second_line > first_line
    assert previous_line == first_line


def test_transcript_search_cancel_restores_anchor_and_resize_clears_query() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("transcript-resize", clock=lambda: 1.0)
    store.publish(
        seq.emit("system_message", "completed", "line", {"text": "searchable"})
    )
    state = TuiTranscriptModeState()
    provider = _provider(store, state)
    state.enter(store.snapshot())
    state.open_search(anchor_line=7)
    state.update_search_query("search")
    provider.frame(80)

    assert state.cancel_search() == 7
    state.open_search(anchor_line=0)
    state.update_search_query("search")
    provider.frame(80)
    provider.invalidate()
    provider.frame(79)

    snapshot = state.snapshot()
    assert snapshot.search_open is False
    assert snapshot.search_query == ""
    assert snapshot.match_count == 0


# LLM: 使用真实块缓存与provider，刻意让两模式块条目互相淘汰；前缀布局仍必须分别存活。
# 函数用途: 验证Ctrl+O反复进出不丢两种布局，也不再次发生块缓存miss。
def test_ctrlo_toggle_keeps_normal_and_detailed_prefixes():
    from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiBlockRenderCache

    store = TuiStateStore()
    seq = TuiEventSequencer('ctrlo-toggle', clock=lambda: 1.0)
    for index in range(3):
        store.publish(seq.emit('assistant_completed', 'completed', f'a-{index}', {'text': f'answer {index}'}))
    state = TuiTranscriptModeState()
    provider = _provider(store, state)
    provider.block_cache = TuiBlockRenderCache(max_entries=3)
    normal_frame = provider.frame(80)
    normal_prefix = provider.block_cache._stable_prefix
    state.enter(store.snapshot())
    detailed_frame = provider.frame(80)
    detailed_prefix = provider.block_cache._stable_prefix
    misses = provider.block_cache.stats().misses
    assert misses == 6
    for _ in range(3):
        state.exit()
        assert provider.frame(80) == normal_frame
        assert provider.block_cache._stable_prefix is normal_prefix
        state.enter(store.snapshot())
        assert provider.frame(80) == detailed_frame
        assert provider.block_cache._stable_prefix is detailed_prefix
        assert provider.block_cache.stats().misses == misses


# LLM: 真实函数被透传，计数只作为“不再扫描/高亮”的证据，不替换搜索结果或布局。
# 函数用途: 为Ctrl+O测试记录函数调用。
def _ctrlo_calls(monkeypatch, module, name):
    original = getattr(module, name)
    calls = []

    # LLM: 保留原参数、返回值及异常，不模拟功能成功。
    # 函数用途: 收集一次实际调用并执行原函数。
    def count(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, count)
    return calls


# LLM: raw frame只提供可见格式化行，测试必须直接保留传入的安全tuple身份，不经过provider缓存掩盖复制。
# 函数用途: 创建简短的真实渲染帧用于搜索状态验证。
def _ctrlo_frame():
    from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderFrame
    from agent_py_agent.cli.chat_parts.tui_safe_lines import SafeFormattedLines

    return TuiRenderFrame(SafeFormattedLines(((('', 'alpha one'),), (('', 'alpha two'),))), (), (), (), (), ())


@pytest.mark.parametrize('query', ['', 'not-found'])
# LLM: 空搜索不扫描，零命中不高亮；同一lines的零命中也须memo，避免滚动时全量重扫。
# 函数用途: 验证无高亮时复用原行组身份和搜索调用次数。
def test_ctrlo_no_matches_reuses_lines_without_highlighting(monkeypatch, query):
    from agent_py_agent.cli.chat_parts import tui_transcript as module

    state = TuiTranscriptModeState()
    state.enter(TuiStateStore().snapshot())
    frame = _ctrlo_frame()
    find = _ctrlo_calls(monkeypatch, module, '_find_transcript_matches')
    highlight = _ctrlo_calls(monkeypatch, module, '_highlight_transcript_lines')
    if query:
        state.open_search(0)
        state.update_search_query(query)
    for _ in range(3):
        decorated = state.decorate_frame(frame, width=80)
        assert decorated.transcript_lines is frame.transcript_lines
    assert len(find) == int(bool(query))
    assert highlight == []


# LLM: 身份相同且query不变才复用坐标；n/N仍改变当前命中，等值新容器或新query必须重新扫描。
# 函数用途: 验证搜索memo、导航以及内容版本失效。
def test_ctrlo_search_memo_preserves_navigation_and_rechecks_new_lines(monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_transcript as module

    state = TuiTranscriptModeState()
    state.enter(TuiStateStore().snapshot())
    frame = _ctrlo_frame()
    calls = _ctrlo_calls(monkeypatch, module, '_find_transcript_matches')
    state.open_search(0)
    state.update_search_query('alpha')
    state.decorate_frame(frame, width=80)
    assert state.commit_search() == 0
    assert state.navigate() == 1
    decorated = state.decorate_frame(frame, width=80)
    assert state.current_match_line() == 1
    assert any('tui-search-current' in style for style, _text in decorated.transcript_lines[1])
    assert len(calls) == 1
    changed = replace(frame, transcript_lines=(*frame.transcript_lines,))
    state.decorate_frame(changed, width=80)
    assert len(calls) == 2
    state.open_search(1)
    state.update_search_query('two')
    state.decorate_frame(changed, width=80)
    assert state.current_match_line() == 1 and len(calls) == 3
    assert state.cancel_search() == 1
    state.decorate_frame(changed, width=80)
    assert state.snapshot().search_query == 'alpha' and len(calls) == 4
    state.decorate_frame(changed, width=79)
    assert state.snapshot().search_query == ''
    assert len(calls) == 4
    state.exit()
    assert state._matches_lines is None
    state.enter(TuiStateStore().snapshot())
    assert state._matches_lines is None


# LLM: 必须穿过真实provider失效/重渲染入口；只在同一个人工frame上测n/N会漏掉命中被重置的回归。
# 函数用途: 验证搜索提交和导航触发重绘后仍停在选中的命中，稳定正文不被重复扫描。
@pytest.mark.parametrize('active_tail', [False, True])
def test_ctrlo_provider_redraw_preserves_search_navigation(monkeypatch, active_tail):
    from agent_py_agent.cli.chat_parts import tui_transcript as module

    store = TuiStateStore()
    seq = TuiEventSequencer('ctrlo-search-redraw', clock=lambda: 1.0)
    for index in range(2):
        store.publish(seq.emit('system_message', 'completed', f's-{index}', {'text': f'alpha {index}'}))
    if active_tail:
        store.publish(seq.emit('assistant_started', 'started', 'active'))
        store.publish(seq.emit('assistant_delta', 'delta', 'active', {'text': 'alpha live'}))
    state = TuiTranscriptModeState()
    provider = _provider(store, state)
    calls = _ctrlo_calls(monkeypatch, module, '_find_transcript_matches')
    state.enter(store.snapshot())
    state.open_search(0)
    state.update_search_query('alpha')
    provider.frame(80)
    state.commit_search()
    selected = state.navigate()
    provider.frame(80)
    assert selected is not None and selected > 0
    assert state.current_match_line() == selected
    if not active_tail:
        assert len(calls) == 1
    assert state.navigate(reverse=True) == 0
    provider.frame(80)
    assert state.current_match_line() == 0


# LLM: 使用真实prompt_toolkit Application和按键管道，DummyOutput仅替代终端设备；不连接Gateway、不提交模型任务。
# 函数用途: 验证Ctrl+O反复切换及搜索n/N在实际按键与重绘通路中保持缓存和正确导航。
def test_ctrlo_real_key_pipeline_keeps_prefixes_and_search_position(tmp_path):
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_ui_setup import (
        _assemble_tui_application,
        _make_normal_tui_body,
        _make_transcript_tui_body,
        _prepare_tui_app_parts,
    )
    from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

    # LLM: 有界等待实际应用状态，不用一次固定sleep冒充输入已处理。
    # 函数用途: 等待按键与显示状态到位，超时保留真实失败。
    async def ready(predicate):
        # LLM: 只读当前测试应用状态，不重发按键或吞掉失败。
        # 函数用途: 让事件循环处理现有输入直到观察条件成立。
        async def poll():
            while not predicate():
                await asyncio.sleep(.01)
        await asyncio.wait_for(poll(), timeout=5)

    # LLM: 全流程只使用临时目录、内存typed事件和按键管道，退出必须回收Application任务。
    # 函数用途: 运行切换、搜索和反向导航并读回最终状态。
    async def scenario():
        runtime = TuiRuntime('ctrlo-real-keys')
        seq = TuiEventSequencer('ctrlo-real-keys')
        for index in range(2):
            runtime.store.publish(seq.emit('assistant_completed', 'completed', f'a-{index}', {'text': f'alpha {index}'}))
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            params = _app_params(tmp_path, runtime)
            parts = _prepare_tui_app_parts(params)
            app = _assemble_tui_application(params, parts, _make_normal_tui_body(parts), _make_transcript_tui_body(parts))
            task = asyncio.create_task(app.run_async())
            provider = parts.transcript_view.provider
            cache = provider.block_cache
            try:
                await ready(lambda: app.is_running and cache._stable_prefix is not None)
                normal = cache._stable_prefix
                pipe.send_bytes(b'\x0f')
                await ready(lambda: cache._stable_prefix.context_key[1] is True)
                detailed = cache._stable_prefix
                misses = cache.stats().misses
                pipe.send_bytes(b'\x0f')
                await ready(lambda: not parts.transcript_state.snapshot().active)
                provider.frame(provider.last_width)
                assert cache._stable_prefix is normal
                pipe.send_bytes(b'\x0f')
                await ready(lambda: parts.transcript_state.snapshot().active)
                provider.frame(provider.last_width)
                assert cache._stable_prefix is detailed
                assert cache.stats().misses == misses
                pipe.send_bytes(b'\x1b[H')
                await ready(lambda: parts.transcript_view.modal_control.current_line() == 0)
                pipe.send_text('/alpha')
                await ready(lambda: parts.transcript_state.snapshot().search_query == 'alpha'
                            and parts.transcript_state.snapshot().match_count == 2)
                pipe.send_bytes(b'\r')
                await ready(lambda: not parts.transcript_state.snapshot().search_open)
                assert parts.transcript_state.snapshot().current_match == 1
                pipe.send_text('n')
                await ready(lambda: parts.transcript_state.snapshot().current_match == 2)
                provider.frame(provider.last_width)
                assert parts.transcript_state.snapshot().current_match == 2
                pipe.send_text('N')
                await ready(lambda: parts.transcript_state.snapshot().current_match == 1)
                assert parts.input_area.text == '' and params.jobs.empty()
            finally:
                if app.is_running:
                    app.exit(result=0)
                await asyncio.wait_for(task, timeout=5)
    asyncio.run(scenario())
