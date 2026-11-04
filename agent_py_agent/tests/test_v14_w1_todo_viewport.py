# LLM: 只用内存 runtime 和真实 prompt_toolkit 控件验证 Todo 展示，不请求模型或写会话数据。
# 模块用途: 回归任务视口、偏移边界、resize、缓存和独立滚动键接线。
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import DummyInput
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from agent_py_agent.cli.chat_parts import tui_keybindings, tui_ui_setup
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiBlockRenderCache,
    TuiRenderContext,
    _todo_window_items,
    tui_render_context_key,
)
from agent_py_agent.cli.chat_parts.tui_interaction import TuiInteractionState
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
from agent_py_agent.cli.chat_parts.tui_view import make_tui_transcript_view


# LLM: 数量与标题是测试数据，不应成为产品中的特殊分支。
# 函数用途: 建立原顺序稳定的当前任务清单。
def _items(count=43):
    return [{"id": f"item-{i}", "title": f"任务{i:02d}", "status": "pending"}
            for i in range(1, count + 1)]


# LLM: 输出接口只报告尺寸，不替换 renderer、provider、控件或键绑定实现。
# 类用途: 为 headless 控件测试提供可变终端尺寸。
class _ResizableOutput(DummyOutput):
    # LLM: 默认尺寸是本测试的起点，resize 由各测试显式改变。
    # 函数用途: 初始化终端尺寸。
    def __init__(self):
        self.rows = 24
        self.columns = 160

    # LLM: 使用真实 Output.get_size 合同，不依赖系统终端。
    # 函数用途: 返回当前测试尺寸。
    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


# LLM: 复用真实 runtime -> context -> provider -> Todo control -> Window 链路。
# 函数用途: 建立无网、无磁盘的测试应用和输入状态。
def _ui(count=43):
    runtime = TuiRuntime("todo-viewport")
    runtime.publish_task_progress_snapshot(_items(count))
    interaction = TuiInteractionState()
    transcript = TuiTranscriptModeState()
    config = SimpleNamespace(agent_name="my-agent", model_name="")
    params = SimpleNamespace(tui_runtime=runtime, agent_navigation=None,
                             agent=SimpleNamespace(config=config, root=""))
    context_factory = tui_ui_setup._make_render_context_factory(params, interaction, transcript)
    view = make_tui_transcript_view(runtime.store, context_factory, transcript_state=transcript)
    todo = tui_ui_setup._make_todo_window(view)
    area = TextArea(multiline=True)
    output = _ResizableOutput()
    app = Application(layout=Layout(HSplit([view.window, todo, area]), focused_element=area),
                      input=DummyInput(), output=output)
    keyparams = SimpleNamespace(
        transcript_area=view, transcript_follow_ref=[True], input_area=area, interaction_state=interaction,
        transcript_state=transcript, tui_runtime=runtime, use_gateway=False,
        agent_navigation=None, transcript_scroll_lines=10,
        history_search_area=TextArea(), exit_armed_at_ref=[0.0], eof_armed_at_ref=[0.0],
        escape_armed_at_ref=[0.0], escape_armed_text_ref=[""])
    app.key_bindings = tui_keybindings._tui_create_keybindings(keyparams)
    return SimpleNamespace(runtime=runtime, interaction=interaction, transcript=transcript,
                           view=view, todo=todo, area=area, output=output, app=app)


# LLM: 从实际注册表和 filter 选择生效动作，不能给被测实现补接线。
# 函数用途: 执行一个实际可用键并返回是否命中。
def _press(ui, key):
    bindings = ui.app.key_bindings.get_bindings_for_keys((key,))
    active = [binding for binding in bindings if binding.filter()]
    if not active:
        return False
    active[-1].handler(SimpleNamespace(app=ui.app, arg=1))
    return True


def test_real_control_fixture_is_valid_before_viewport_change():
    ui = _ui(2)
    with set_app(ui.app):
        lines = ui.view.provider.frame(160).todo_lines
        assert len(lines) == 3
        assert ui.todo.preferred_height(160, 24).preferred == 3
        assert ui.todo.content.create_content(160, 3).line_count == 3
        assert _press(ui, Keys.ControlT)
        assert ui.interaction.snapshot().todos_expanded is True


def test_unbounded_output_remains_byte_identical():
    ui = _ui(7)
    block = next(b for b in ui.runtime.store.snapshot().active_blocks if b.role == "todo")
    cache = TuiBlockRenderCache()
    context = TuiRenderContext(width=160, todos_expanded=True)
    original = cache.render(block, context)
    explicit = cache.render(block, replace(context, todo_viewport_rows=None, todo_scroll_offset=10**9))
    assert explicit == original
    assert tuple(fragments_text(line) for line in explicit) == (
        "📋 任务清单 · 完成 0/7（Ctrl+T 收起）",
        *(f"  □ 任务{i:02d}" for i in range(1, 8)),
    )


@pytest.mark.parametrize("offset,start", [(-10**9, 0), (0, 0), (1, 1), (10**9, 36)])
def test_window_clamps_offsets_and_reserves_title(offset, start):
    items = _items()
    before = [dict(item) for item in items]
    visible, hidden = _todo_window_items(items, expanded=True, viewport_rows=8, scroll_offset=offset)
    assert visible == items[start:start + 7]
    assert hidden == 36
    assert items == before


@pytest.mark.parametrize("rows", [0, 1, 2, 3, 8, 44, 100])
def test_expanded_render_is_bounded_with_extreme_offset(rows):
    ui = _ui()
    block = next(b for b in ui.runtime.store.snapshot().active_blocks if b.role == "todo")
    context = TuiRenderContext(width=160, todos_expanded=True,
                               todo_viewport_rows=rows, todo_scroll_offset=10**9)
    lines = TuiBlockRenderCache().render(block, context)
    assert 1 <= len(lines) <= max(1, rows)
    if rows > 1:
        assert fragments_text(lines[-1]).endswith("任务43")


def test_cache_keys_include_rows_and_offset():
    ui = _ui()
    snapshot = ui.runtime.store.snapshot()
    block = next(b for b in snapshot.active_blocks if b.role == "todo")
    cache = TuiBlockRenderCache()
    contexts = [TuiRenderContext(width=160, todos_expanded=True, todo_viewport_rows=rows,
                                 todo_scroll_offset=offset)
                for rows, offset in [(8, 0), (8, 1), (4, 1)]]
    frames = [cache.render(block, context) for context in contexts]
    assert len({tui_render_context_key(snapshot, context) for context in contexts}) == 3
    assert frames[0] != frames[1] != frames[2]
    assert [len(frame) for frame in frames] == [8, 8, 4]
    assert "显示 1-7/43（Shift+↑↓ 滚动 · Ctrl+T 收起）" in fragments_text(frames[0][0])
    assert "显示 2-8/43" in fragments_text(frames[1][0])


def test_interaction_clamps_resizes_and_toggle_resets():
    state = TuiInteractionState()
    assert state.snapshot().todo_scroll_offset == 0
    state.toggle_todos()
    assert state.scroll_todos(10**9, max_offset=36) == 36
    assert state.scroll_todos(-1) == 35
    assert state.scroll_todos(0, max_offset=3) == 3
    assert state.scroll_todos(-10**9) == 0
    state.scroll_todos(2)
    assert state.snapshot().todo_scroll_offset == 2
    assert state.toggle_todos() is False
    assert state.snapshot().todo_scroll_offset == 0
    assert state.toggle_todos() is True
    assert state.snapshot().todo_scroll_offset == 0


def test_real_controls_keybindings_and_resize_preserve_input_and_transcript():
    ui = _ui()
    with set_app(ui.app):
        assert not _press(ui, Keys.ShiftDown)
        assert _press(ui, Keys.ControlT)
        first = ui.view.provider.frame(160)
        assert len(first.todo_lines) == 8
        assert ui.todo.preferred_height(160, 24).preferred == 8
        assert ui.todo.content.create_content(160, 8).line_count == 8
        ui.area.text = "未提交草稿"
        ui.view.scroll(-1)
        transcript_position = ui.view.window.vertical_scroll
        assert _press(ui, Keys.ShiftDown)
        second = ui.view.provider.frame(160)
        assert "显示 2-8/43" in fragments_text(second.todo_lines[0])
        assert ui.area.text == "未提交草稿"
        assert ui.view.window.vertical_scroll == transcript_position
        ui.interaction.scroll_todos(10**9)
        assert "显示 37-43/43" in fragments_text(ui.view.provider.frame(160).todo_lines[0])
        ui.output.rows = 12
        shrunk = ui.view.provider.frame(160)
        assert len(shrunk.todo_lines) == 4
        assert ui.todo.preferred_height(160, 12).preferred == 4
        assert "显示 37-39/43" in fragments_text(shrunk.todo_lines[0])
        ui.output.rows = 120
        grown = ui.view.provider.frame(160)
        assert len(grown.todo_lines) == 40
        assert "显示 5-43/43" in fragments_text(grown.todo_lines[0])
        assert ui.interaction.snapshot().todo_scroll_offset == 4
        assert _press(ui, Keys.ShiftUp)
        assert ui.interaction.snapshot().todo_scroll_offset == 3
        assert _press(ui, Keys.ControlT)
        assert ui.interaction.snapshot().todo_scroll_offset == 0
        assert not _press(ui, Keys.ShiftUp)
        assert ui.area.text == "未提交草稿"
        assert next(b for b in ui.runtime.store.snapshot().active_blocks
                    if b.role == "todo").metadata["items"] == _items()


def test_short_list_and_hidden_modes_do_not_capture_shift_keys():
    ui = _ui(2)
    with set_app(ui.app):
        _press(ui, Keys.ControlT)
        assert not _press(ui, Keys.ShiftDown)
        assert ui.todo.preferred_height(160, 24).preferred == 3
        ui.runtime.publish_task_progress_snapshot(_items())
        assert _press(ui, Keys.ShiftDown)
        ui.transcript.enter(ui.runtime.store.snapshot())
        assert not _press(ui, Keys.ShiftDown)
        ui.transcript.exit()
        ui.interaction.start_history_search(ui.interaction.capture_draft("", 0), ["旧提示词"])
        assert not _press(ui, Keys.ShiftDown)


def test_history_and_transcript_keys_keep_original_targets(monkeypatch):
    ui = _ui()
    targets = []
    monkeypatch.setattr(tui_keybindings, "_handle_up_keybinding", lambda _e, _p: targets.append("history-up"))
    monkeypatch.setattr(tui_keybindings, "_handle_down_keybinding", lambda _e, _p: targets.append("history-down"))
    monkeypatch.setattr(type(ui.view), "scroll", lambda self, delta: targets.append(delta))
    with set_app(ui.app):
        assert _press(ui, Keys.ControlT)
        assert _press(ui, Keys.Up)
        assert _press(ui, Keys.Down)
        assert _press(ui, Keys.PageUp)
        assert _press(ui, Keys.PageDown)
        assert targets == ["history-up", "history-down", -10, 10]
        assert ui.interaction.snapshot().todo_scroll_offset == 0


@pytest.mark.parametrize("rows", [1, 8, 12, 24, 120])
def test_terminal_cap_in_context_matches_window_including_title(rows):
    ui = _ui()
    with set_app(ui.app):
        ui.output.rows = rows
        ui.interaction.toggle_todos()
        context = ui.view.provider.context_factory(160)
        frame = ui.view.provider.frame(160)
        cap = max(3, rows // 3)
        assert context.todo_viewport_rows == cap
        assert len(frame.todo_lines) == min(44, cap)
        assert ui.todo.preferred_height(160, rows).preferred == min(44, cap)


def test_resize_and_replaced_items_clamp_the_snapshot_offset():
    ui = _ui()
    with set_app(ui.app):
        ui.interaction.toggle_todos()
        ui.view.provider.frame(160)
        ui.interaction.scroll_todos(10**9)
        ui.runtime.publish_task_progress_snapshot(_items(2))
        frame = ui.view.provider.frame(160)
        assert len(frame.todo_lines) == 3
        assert ui.interaction.snapshot().todo_scroll_offset == 0
        assert not _press(ui, Keys.ShiftDown)


@pytest.mark.parametrize("rows", [1, 8])
def test_collapsed_small_terminal_fits_the_same_cap(rows):
    ui = _ui()
    with set_app(ui.app):
        ui.output.rows = rows
        frame = ui.view.provider.frame(160)
        assert len(frame.todo_lines) <= max(3, rows // 3)
        assert ui.todo.preferred_height(160, rows).preferred == len(frame.todo_lines)
