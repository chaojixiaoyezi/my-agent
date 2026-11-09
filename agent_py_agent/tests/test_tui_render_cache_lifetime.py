"""显示窗口离场后缓存不得继续强持历史正文；完整输出保持原合同。"""

import gc
import weakref
from dataclasses import replace

import pytest

from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiBlockRenderCache,
    TuiRenderContext,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_view_model import TuiBlock


def test_departed_prefix_contexts_release_source_and_keep_render_equivalence():
    runtime = TuiRuntime("cache-lifetime")
    runtime._publish("assistant_completed", "completed", "old", {"text": "旧正文" * 1000})
    snapshot = runtime.store.snapshot()
    old = weakref.ref(snapshot.stable_blocks[0])
    cache = TuiBlockRenderCache()
    contexts = [
        TuiRenderContext(width=width, detailed_transcript=detail)
        for width, detail in [(80, False), (80, True), (120, False), (120, True)]
    ]
    for context in contexts:
        render_tui_snapshot(snapshot, context, cache=cache)
    current = replace(
        snapshot,
        stable_blocks=(TuiBlock("new", "message", "assistant", "completed", text="新正文"),),
    )
    del snapshot, runtime
    render_tui_snapshot(current, contexts[0], cache=cache)
    gc.collect()
    assert old() is None
    for context in contexts:
        assert render_tui_snapshot(current, context, cache=cache) == render_tui_snapshot(
            current, context
        )


def test_replaced_block_releases_old_body_even_if_identifier_stays():
    runtime = TuiRuntime("cache-replaced")
    runtime._publish("assistant_completed", "completed", "same", {"text": "旧正文" * 1000})
    snapshot = runtime.store.snapshot()
    old = weakref.ref(snapshot.stable_blocks[0])
    cache = TuiBlockRenderCache()
    normal, detail = (
        TuiRenderContext(width=80),
        TuiRenderContext(width=80, detailed_transcript=True),
    )
    render_tui_snapshot(snapshot, detail, cache=cache)
    current = replace(
        snapshot,
        stable_blocks=(replace(snapshot.stable_blocks[0], text="新正文", updated_seq=999),),
    )
    del snapshot, runtime
    render_tui_snapshot(current, normal, cache=cache)
    gc.collect()
    assert old() is None
    assert render_tui_snapshot(current, detail, cache=cache) == render_tui_snapshot(current, detail)


def test_empty_window_releases_all_layout_aliases():
    runtime = TuiRuntime("cache-empty")
    runtime._publish("assistant_completed", "completed", "old", {"text": "原文"})
    snapshot = runtime.store.snapshot()
    old = weakref.ref(snapshot.stable_blocks[0])
    cache = TuiBlockRenderCache()
    for width in (80, 100):
        render_tui_snapshot(snapshot, TuiRenderContext(width=width), cache=cache)
    current = replace(snapshot, stable_blocks=())
    del snapshot, runtime
    cache.retain_snapshot(current)
    gc.collect()
    assert old() is None
    assert not cache._stable_prefixes and cache._stable_prefix is None
    assert cache.stats().entries == 0


@pytest.mark.parametrize('limited', ['chars', 'entries', 'single_entries'])
def test_current_window_prefix_contexts_share_total_budget_and_keep_output(limited):
    runtime = TuiRuntime('prefix-total-budget')
    for index in range(3):
        runtime._publish('assistant_completed', 'completed', f'body-{index}',
                         {'text': f'正文{index} ' * 30})
    snapshot = runtime.store.snapshot()
    contexts = [TuiRenderContext(width=width, detailed_transcript=detail)
                for width, detail in [(80, False), (120, False), (80, True), (120, True)]]
    sizing = TuiBlockRenderCache()
    prefixes = [sizing.stable_prefix(snapshot.stable_blocks, context) for context in contexts]
    max_chars = 2 * max(prefix.chars for prefix in prefixes) if limited == 'chars' else 24_000_000
    max_entries = (2 if limited == 'entries' else 1) * len(snapshot.stable_blocks) if limited.endswith('entries') else 20_000
    cache = TuiBlockRenderCache(max_chars=max_chars, max_entries=max_entries)
    first_prefix = None
    for context in contexts:
        assert render_tui_snapshot(snapshot, context, cache=cache) == render_tui_snapshot(snapshot, context)
        if first_prefix is None:
            first_prefix = cache._stable_prefix
        assert sum(prefix.chars for prefix in cache._stable_prefixes.values()) <= max_chars
        assert sum(len(prefix.line_ends) for prefix in cache._stable_prefixes.values()) <= max_entries
        assert cache._chars <= max_chars and cache.stats().entries <= max_entries
    retained_count = 1 if limited == 'single_entries' else 2
    assert len(cache._stable_prefixes) == retained_count
    last = cache._stable_prefix
    assert cache.stable_prefix(snapshot.stable_blocks, contexts[-1]) is last
    assert list(cache._stable_prefixes)[-1] == last.context_key
    # 旧context被淘汰仅增加重排成本，回看仍由同一完整源生成全部内容和原锚点。
    assert render_tui_snapshot(snapshot, contexts[0], cache=cache) == render_tui_snapshot(snapshot, contexts[0])
    assert len(cache._stable_prefixes) == retained_count
    assert cache._stable_prefix is not first_prefix
    if limited == 'single_entries':
        active = TuiBlock('live', 'message', 'assistant', 'delta', text='活动正文',
                          created_seq=1000, updated_seq=1000)
        for version in range(2):
            live = replace(snapshot, active_blocks=(replace(active, text=f'活动正文{version}', updated_seq=1000 + version),))
            stable = cache._stable_prefix
            live_context = replace(contexts[0], now=100.0 + version)
            assert render_tui_snapshot(live, live_context, cache=cache) == render_tui_snapshot(live, live_context)
            assert cache._stable_prefix is stable


def test_prefix_budget_hit_does_not_recount_rendered_body(monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_block_renderer as module

    runtime = TuiRuntime('prefix-fast-hit')
    runtime._publish('assistant_completed', 'completed', 'body', {'text': '正文' * 100})
    blocks = runtime.store.snapshot().stable_blocks
    context = TuiRenderContext(width=80)
    cache = TuiBlockRenderCache()
    prefix = cache.stable_prefix(blocks, context)
    def forbidden(*args, **kwargs):
        pytest.fail('same-source prefix hit must not recount or render history')
    monkeypatch.setattr(module, '_rendered_char_count', forbidden)
    monkeypatch.setattr(cache, '_build_stable_prefix', forbidden)
    for _ in range(10):
        assert cache.stable_prefix(blocks, context) is prefix
