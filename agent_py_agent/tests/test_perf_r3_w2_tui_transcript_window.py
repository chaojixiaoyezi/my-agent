# LLM: PERF-R3-W2 回归：TUI 展示窗口由 tui_transcript_max_blocks 决定（主页与子代理详情页一致），渲染缓存随之有界；
#   完整历史仍在会话存储，不在本测试范围。
# 模块用途: 固定配置三处接线、主页/子代理 runtime 的窗口来源，以及窗口外旧块不再被保留或渲染。
from __future__ import annotations

from types import SimpleNamespace

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
