"""显示窗口离场后缓存不得继续强持历史正文；完整输出保持原合同。"""

import gc
import weakref
from dataclasses import replace

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
