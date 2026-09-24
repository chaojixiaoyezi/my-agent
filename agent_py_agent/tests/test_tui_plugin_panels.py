from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.agent import plugin_commands
from agent_py_agent.cli.chat_parts import tui_plugin_commands
from agent_py_agent.cli.chat_parts.tui_plugin_commands import (
    PluginInputBinding,
    toggle_plugin_panel,
)
from agent_py_agent.cli.chat_parts.tui_plugin_panels import (
    MAX_BACKOFF_SECONDS,
    PANEL_BODY_LINES,
    POLL_SECONDS,
    PluginPanelBoard,
    render_plugin_panels,
)


class _Fetch:
    def __init__(self):
        self.calls = []
        self.response = {"ok": True, "panels": []}

    def __call__(self, requested):
        self.calls.append(requested)
        return self.response if not callable(self.response) else self.response(requested)


def _board():
    fetch = _Fetch()
    redraws = []
    board = PluginPanelBoard(fetch, lambda: redraws.append(1), start_thread=False)
    return board, fetch, redraws


def test_toggle_opens_closes_and_limits_visible_panels():
    board, fetch, redraws = _board()
    assert board.toggle("a", "p", "A") == "opened" and board.has_visible()
    assert board.views()[0].state == "loading" and board.views()[0].title == "A"
    assert board.toggle("b", "p") == "opened"
    assert board.toggle("c", "p") == "limit"
    assert board.toggle("a", "p") == "closed"
    assert [view.plugin_id for view in board.views()] == ["b"]
    assert fetch.calls == [] and len(redraws) == 3


def test_poll_formats_each_kind_and_redraws_only_on_change():
    board, fetch, redraws = _board()
    board.toggle("a", "t")
    board.toggle("b", "s")
    fetch.response = {"ok": True, "panels": [
        {"plugin_id": "a", "panel_id": "t", "title": "表", "state": "ready",
         "display": {"kind": "table", "columns": ["名", "值"], "rows": [["x", "1"]], "truncated": True}},
        {"plugin_id": "b", "panel_id": "s", "title": "状态", "state": "ready",
         "display": {"kind": "status", "fields": [{"label": "状态", "value": "工作中"}], "truncated": False}},
    ]}
    before = len(redraws)
    assert board.poll_once()
    table, status = board.views()
    assert table.body == ("名  值", "x  1", "…（内容已截断）")
    assert status.body == ("状态：工作中",)
    assert len(redraws) == before + 1
    board.poll_once()
    assert len(redraws) == before + 1


def test_unavailable_panel_is_removed_and_polling_stops():
    board, fetch, _ = _board()
    board.toggle("a", "p")
    fetch.response = {"ok": True, "panels": [{"plugin_id": "a", "panel_id": "p", "state": "unavailable"}]}
    board.poll_once()
    assert not board.has_visible() and board.views() == ()
    calls = len(fetch.calls)
    board.poll_once()
    assert len(fetch.calls) == calls


def test_transport_failure_backs_off_without_hiding_panel():
    board, fetch, _ = _board()
    board.toggle("a", "p")
    fetch.response = {}
    for _ in range(6):
        assert board.poll_once() is False
    assert board._backoff == MAX_BACKOFF_SECONDS and board.has_visible()
    fetch.response = {"ok": True, "panels": []}
    board.poll_once()
    assert board._backoff == POLL_SECONDS


def test_error_state_keeps_previous_body_and_shows_error():
    board, fetch, _ = _board()
    board.toggle("a", "p", "活动")
    fetch.response = {"ok": True, "panels": [{"plugin_id": "a", "panel_id": "p", "state": "ready",
                                              "display": {"kind": "text", "lines": ["一"], "truncated": False}}]}
    board.poll_once()
    fetch.response = {"ok": True, "panels": [{"plugin_id": "a", "panel_id": "p", "state": "error",
                                              "error": "插件响应超时"}]}
    board.poll_once()
    view = board.views()[0]
    assert view.body == ("一",) and view.state == "error"
    rendered = "".join(text for _style, text in render_plugin_panels(board.views()))
    assert "插件响应超时" in rendered and "（出错）" in rendered


def test_render_caps_body_lines():
    board, fetch, _ = _board()
    board.toggle("a", "p", "长")
    fetch.response = {"ok": True, "panels": [{"plugin_id": "a", "panel_id": "p", "state": "ready",
                                              "display": {"kind": "text", "lines": [str(i) for i in range(20)],
                                                          "truncated": False}}]}
    board.poll_once()
    lines = "".join(text for _s, text in render_plugin_panels(board.views())).splitlines()
    assert len(lines) == 1 + PANEL_BODY_LINES and "还有" in lines[-1]


def test_close_and_stop_event_end_background_thread():
    stop = threading.Event()
    fetch = _Fetch()
    board = PluginPanelBoard(fetch, lambda: None, stop_event=stop)
    board.toggle("a", "p")
    thread = board._thread
    assert thread is not None
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    board.close()
    assert board.views() == ()


class _Runtime:
    def __init__(self):
        self.notices = []

    def set_notice(self, text):
        self.notices.append(text)


_CATALOG = SimpleNamespace(plugins=(), management_actions=())


def _intercept(monkeypatch, *, kind="display", enabled=True, catalog=_CATALOG):
    runtime = _Runtime()
    monkeypatch.setattr("agent_py_agent.cli.chat_parts.tui_actions._required_tui_runtime", lambda _p: runtime)
    parsed = SimpleNamespace(
        plugin=SimpleNamespace(plugin_id="activity-line", enabled=enabled),
        action=SimpleNamespace(kind=kind, target="line", summary="打开或关闭活动面板"),
        help_requested=False,
    )
    monkeypatch.setattr(plugin_commands, "parse_plugin_command", lambda *_a, **_k: parsed)
    board, fetch, _ = _board()
    client = SimpleNamespace(snapshot=lambda: catalog)
    binding = PluginInputBinding(client, panels=board)
    handled = toggle_plugin_panel(object(), "/plugins@activity-line show", binding)
    return handled, board, fetch, runtime


def test_display_action_toggles_locally_without_host_request(monkeypatch):
    handled, board, fetch, runtime = _intercept(monkeypatch)
    assert handled and board.has_visible() and fetch.calls == []
    assert runtime.notices == ["已打开插件面板 activity-line/line。"]


def test_tool_action_and_missing_catalog_fall_back_to_host(monkeypatch):
    assert _intercept(monkeypatch, kind="tool")[0] is False
    assert _intercept(monkeypatch, catalog=None)[0] is False


def test_disabled_plugin_panel_gets_clear_notice(monkeypatch):
    handled, board, _fetch, runtime = _intercept(monkeypatch, enabled=False)
    assert handled and not board.has_visible()
    assert "未启用" in runtime.notices[0]


def test_submit_uses_panel_interception_before_background_command(monkeypatch):
    calls = []
    monkeypatch.setattr(tui_plugin_commands, "toggle_plugin_panel", lambda *a: calls.append(a) or True)
    binding = PluginInputBinding(SimpleNamespace(snapshot=lambda: None))
    assert tui_plugin_commands.submit_plugin_command(object(), object(), "/plugins@x show", binding, "") is True
    assert len(calls) == 1
