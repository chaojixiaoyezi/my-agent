# LLM: PERF-R3-W2 回归：TUI 展示窗口由 tui_transcript_max_blocks 决定（主页与子代理详情页一致），渲染缓存随之有界；
#   完整历史仍在会话存储，不在本测试范围。
# 模块用途: 固定配置三处接线、主页/子代理 runtime 的窗口来源，以及窗口外旧块不再被保留或渲染。
from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import packaged_config_path
from agent_py_agent.cli.chat_parts.tui import _root_tui_runtime
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiBlockRenderCache,
    TuiRenderContext,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_identity_window import TuiIdentityWindow
from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices
from agent_py_agent.cli.chat_parts.tui_view import make_tui_transcript_view


# 函数用途: 用一个只带配置的替身构造主会话展示 runtime。
def _runtime(window):
    return _root_tui_runtime(SimpleNamespace(current_session_id="sess-w2",
                                             agent=SimpleNamespace(config=SimpleNamespace(tui_transcript_max_blocks=window))))


# 函数用途: 经真实后台消息页路径向 runtime 写入 count 条冻结块。
def _load(runtime, count):
    notices = [{"schema_version": "background_message.v1", "display_kind": "assistant_response", "thread_id": "thread-w2",
                "message_id": f"m{i:05d}", "reason": "subagent_runner_finished", "content": f"#{i} body", "summary": "s",
                "created_at": 100.0 + i} for i in range(count)]

    # 类用途: 按游标分页返回预置后台消息的 Gateway 替身。
    class _Agent:
        def request_background_notices(self, session_id, *, after, event_after, event_stream_id):
            start = int(after or 0)
            page = notices[start:start + 200]
            return {"ok": True, "binding_state": "confirmed", "thread_id": "thread-w2", "cursor": start + len(page),
                    "notices": page, "transcript_events": [], "event_cursor": 0, "event_stream_id": "s0",
                    "active_task_count": 0, "agent_permission_requests": []}

    seen, cursor = TuiIdentityWindow(), [0]
    for _ in range(count // 200 + 2):
        _consume_background_notices(_Agent(), runtime.session_id, runtime, [None], seen, cursor)


def test_config_key_is_wired_with_bounded_default():
    assert AgentConfig().tui_transcript_max_blocks == 500
    text = packaged_config_path().read_text(encoding="utf-8")
    assert "\ntui_transcript_max_blocks: 500\n" in text
    spec = parameter_registry()["tui_transcript_max_blocks"]
    assert spec.description


def test_root_and_agent_view_runtimes_keep_only_the_configured_window():
    root = _runtime(40)
    assert root.store.reducer.max_stable_blocks == 40
    child = TuiAgentNavigationState(root, max_cached_runtimes=2)._runtime_for_locked("run-1")
    assert child.store.reducer.max_stable_blocks == 40
    _load(root, 300)
    stable = root.store.snapshot().stable_blocks
    assert len(stable) == 40 and stable[-1].text.startswith("#299")


def test_render_cache_only_holds_the_window():
    runtime = _runtime(25)
    _load(runtime, 400)
    cache = TuiBlockRenderCache()
    render_tui_snapshot(runtime.store.snapshot(), TuiRenderContext(width=100), cache=cache)
    assert len(cache._entries) <= 25


def _assert_cache_retains_snapshot(cache, snapshot):
    """条目和字符记账都只能引用当前投影中的块，不能靠缩小 LRU 预算伪造回收。"""
    retained = {block.block_id for block in (*snapshot.stable_blocks, *snapshot.active_blocks)}
    assert {key[0] for key in cache._entries} <= retained
    assert cache._entry_chars.keys() == cache._entries.keys()
    assert cache._chars == sum(cache._entry_chars.values())


def _fd_count():
    """只在宿主提供描述符目录时采样；目录读取结束即关闭，不要求 Windows 具备 POSIX 接口。"""
    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    return None


def test_default_window_releases_departed_entries_in_transcript_and_root_overlay():
    """真实 runtime 到两个 view provider 连续回放，并验证子页切换不会迁走根审批浮层。"""
    root = _runtime(AgentConfig().tui_transcript_max_blocks)
    context = TuiRenderContext(width=80, now=1.0)
    view = make_tui_transcript_view(root.store, lambda width: replace(context, width=width))
    fd_before = _fd_count()
    for start in range(0, 750, 50):
        for index in range(start, start + 50):
            root._publish("assistant_completed", "completed", f"reply-{index}",
                          {"text": f"普通回复 {index}\n\n```python\nprint({index})\n```"})
        snapshot = root.store.snapshot()
        assert len(snapshot.stable_blocks) == min(start + 50, 500)
        for width, detailed in ((40, False), (100, True)):
            context = replace(context, width=width, detailed_transcript=detailed)
            expected = render_tui_snapshot(snapshot, context)
            for provider in (view.provider, view.overlay_provider):
                assert provider.frame(width) == expected
                _assert_cache_retains_snapshot(provider.block_cache, snapshot)
                assert provider.block_cache.max_entries == 20_000
                assert provider.block_cache.max_chars == 24_000_000

    navigation = TuiAgentNavigationState(root, max_cached_runtimes=2)
    child = navigation._runtime_for_locked("child-r1")
    child._publish("assistant_completed", "completed", "child-reply", {"text": "子代理回复"})
    root_snapshot, child_snapshot = root.store.snapshot(), child.store.snapshot()
    view.set_state_store(child.store)
    assert view.provider.state_store is child.store
    assert view.overlay_provider.state_store is root.store
    assert view.provider.frame(context.width) == render_tui_snapshot(child_snapshot, context)
    assert view.overlay_provider.frame(context.width) == render_tui_snapshot(root_snapshot, context)
    _assert_cache_retains_snapshot(view.provider._agent_block_cache, child_snapshot)
    _assert_cache_retains_snapshot(view.provider.block_cache, root_snapshot)
    _assert_cache_retains_snapshot(view.overlay_provider.block_cache, root_snapshot)
    view.set_state_store(root.store)
    assert view.provider.frame(context.width) == render_tui_snapshot(root_snapshot, context)
    _assert_cache_retains_snapshot(view.provider.block_cache, root_snapshot)
    assert root.store.snapshot() is root_snapshot and child.store.snapshot() is child_snapshot
    if fd_before is not None:
        fd_delta = _fd_count() - fd_before
        assert fd_delta == 0


@pytest.mark.parametrize("width,detailed", [(40, False), (100, True)])
def test_freezing_one_of_two_active_blocks_keeps_500_cache_hits(width, detailed):
    """500 稳定块和两个活动块换代时，只重排新冻结块，继续复用其余 500 个条目。"""
    runtime = _runtime(AgentConfig().tui_transcript_max_blocks)
    for index in range(500):
        runtime._publish("assistant_completed", "completed", f"stable-{index}", {"text": f"稳定回复 {index}"})
    runtime._publish("assistant_started", "started", "reply")
    runtime._publish("assistant_delta", "delta", "reply", {"text": "正在回复"})
    runtime._publish("thinking_started", "started", "thinking")
    runtime._publish("thinking_delta", "delta", "thinking", {"text": "持续思考"})
    context = TuiRenderContext(width=width, detailed_transcript=detailed, now=1.0)
    cache = TuiBlockRenderCache()
    before = runtime.store.snapshot()
    assert len(before.stable_blocks) == 500 and len(before.active_blocks) == 2
    assert render_tui_snapshot(before, context, cache=cache) == render_tui_snapshot(before, context)
    initial = cache.stats()
    runtime._publish("assistant_completed", "completed", "reply", {"text": "最终回复"})
    snapshot = runtime.store.snapshot()
    assert len(snapshot.stable_blocks) == 500 and len(snapshot.active_blocks) == 1
    assert render_tui_snapshot(snapshot, context, cache=cache) == render_tui_snapshot(snapshot, context)
    stats = cache.stats()
    assert stats.misses - initial.misses == 1
    assert stats.hits - initial.hits == 500
    _assert_cache_retains_snapshot(cache, snapshot)


def test_live_bookkeeping_retires_on_stable_change_and_idle_retention_does_not_scan():
    """活动块单独移除可延迟回收；稳定窗口换代后必须同步清条目和两份流式记账。"""
    runtime = _runtime(2)
    runtime._publish("assistant_completed", "completed", "seed", {"text": "初始回复"})
    runtime._publish("assistant_started", "started", "departed")
    runtime._publish("assistant_delta", "delta", "departed", {"text": "待冻结正文"})
    runtime._publish("thinking_started", "started", "kept")
    runtime._publish("thinking_delta", "delta", "kept", {"text": "仍在思考"})
    context, cache = TuiRenderContext(width=80, now=1.0), TuiBlockRenderCache()
    snapshot = runtime.store.snapshot()
    render_tui_snapshot(snapshot, context, cache=cache)
    assert cache._live_last_key.keys() == cache._live_last_at.keys() == {"departed", "kept"}

    class NoScanEntries(OrderedDict):
        def __iter__(self):
            raise AssertionError("同一稳定来源不得重新遍历缓存")

    cache._entries = NoScanEntries(cache._entries)
    active_only = replace(snapshot, active_blocks=tuple(block for block in snapshot.active_blocks if block.block_id == "kept"))
    cache.retain_snapshot(active_only)
    assert cache._retained_stable_blocks is snapshot.stable_blocks
    assert "departed" in cache._live_last_key
    cache._entries = OrderedDict(cache._entries.items())

    runtime._publish("assistant_completed", "completed", "departed", {"text": "已完成正文"})
    frozen = runtime.store.snapshot()
    render_tui_snapshot(frozen, context, cache=cache)
    assert "departed" in cache._live_last_key and "kept" in cache._live_last_key
    for index in range(2):
        runtime._publish("assistant_completed", "completed", f"next-{index}", {"text": f"后续回复 {index}"})
    changed = runtime.store.snapshot()
    assert render_tui_snapshot(changed, context, cache=cache) == render_tui_snapshot(changed, context)
    assert cache._live_last_key.keys() == cache._live_last_at.keys() == {"kept"}
    assert cache._retained_stable_blocks is changed.stable_blocks
    _assert_cache_retains_snapshot(cache, changed)


def test_returning_from_a_child_page_reuses_the_root_window_render():
    """根页窗口在往返子代理详情页后不得整窗重排；两份缓存各自只持本页窗口内的块。"""
    root = _runtime(AgentConfig().tui_transcript_max_blocks)
    for index in range(520):
        root._publish("assistant_completed", "completed", f"reply-{index}", {"text": f"回复 {index}\n\n```python\nprint({index})\n```"})
    view = make_tui_transcript_view(root.store, lambda width: TuiRenderContext(width=width, now=1.0))
    provider = view.provider
    provider.frame(80)
    child = TuiAgentNavigationState(root, max_cached_runtimes=2)._runtime_for_locked("child-nav")
    child._publish("assistant_completed", "completed", "child-reply", {"text": "子代理回复"})
    for _ in range(2):
        view.set_state_store(child.store)
        assert provider.frame(80) == render_tui_snapshot(child.store.snapshot(), TuiRenderContext(width=80, now=1.0))
        misses = provider.block_cache.stats().misses
        view.set_state_store(root.store)
        assert provider.frame(80) == render_tui_snapshot(root.store.snapshot(), TuiRenderContext(width=80, now=1.0))
        assert provider.block_cache.stats().misses == misses
    _assert_cache_retains_snapshot(provider.block_cache, root.store.snapshot())
    _assert_cache_retains_snapshot(provider._agent_block_cache, child.store.snapshot())
    assert len(provider.block_cache._entries) == 500
