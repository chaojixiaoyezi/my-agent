# LLM: 仅用临时记录、真实TUI投影和确定性时序复现；不启动进程、消费完成通知或访问真实用户会话。
# 模块用途: 覆盖Wave9后台瞬态观察隔离及独立用户消息顶栏的布局、点击与复制合同。
from __future__ import annotations

import errno
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_process_view as bg
from agent_py_agent.cli.chat_parts import tui_threading
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


# LLM: 只清模块易失缓存，不触碰Store或真实进程；测试之间不能继承缓存代次。
# 函数用途: 为每条回归提供独立后台观察缓存。
@pytest.fixture(autouse=True)
def clear_process_cache():
    bg._clear_background_process_view_cache()
    yield
    bg._clear_background_process_view_cache()


# LLM: 样本只写本测试临时owner，终态无需探活；非法schema保留原归属证据供隔离判定。
# 函数用途: 建立真实后台记录供原只读reader和scope验证器读取。
def write_record(home, name="bg-one", thread="thread-one", *, invalid=False):
    root = home / "workspace/runtime/processes/sessions"
    root.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": name, "status": "exited", "command": name,
               "execution_scope": {"owner_home": str(home), "thread_id": thread}}
    if invalid:
        payload["schema"] = "invalid"
    (root / f"{name}.json").write_text(json.dumps(payload))
    return root


# LLM: 两次元数据观察代表同一次扫描的前后代次；只模拟并发写时序，真实目录遍历与错误生成保持。
# 函数用途: 确定性地让前几次扫描换代，随后稳定或持续换代。
def directory_race(monkeypatch, root, changes):
    calls, delays = [], []

    # LLM: 不访问真实时钟，按完整扫描次数推进目录代次。
    # 函数用途: 注入目录前后stat差异。
    def key(path):
        assert path == root
        attempt, after = divmod(len(calls), 2)
        calls.append(path)
        return (1, 2, 100 + attempt + int(bool(after) and attempt < changes))

    monkeypatch.setattr(bg, "_directory_stat_key", key)
    monkeypatch.setattr(bg, "cache_entry_trustworthy", lambda _mtime: False)
    monkeypatch.setattr(bg.time, "sleep", delays.append)
    return calls, delays


# LLM: 最多三次重试即首次扫描加三次；稳定结果必须重新扫描，不能把旧的不完整快照报成功。
# 函数用途: 复现一次、两次及预算内第三次目录原子换代的恢复。
@pytest.mark.parametrize("changes", [1, 2, 3])
def test_directory_transient_changes_recover_with_bounded_retry(tmp_path, monkeypatch, changes):
    root = tmp_path / "records"
    root.mkdir()
    calls, delays = directory_race(monkeypatch, root, changes)
    assert bg._directory_snapshots(root, tmp_path) == ()
    assert len(calls) == 2 * (changes + 1)
    assert delays == [0.005] * changes


# LLM: 持续换代必须保留真实错误并停止重试，不能无限轮询或返回旧缓存冒充完整。
# 函数用途: 固定初读加三次重试的失败边界和退避次数。
def test_directory_persistent_changes_surface_after_retry_budget(tmp_path, monkeypatch):
    root = tmp_path / "records"
    root.mkdir()
    calls, delays = directory_race(monkeypatch, root, changes=100)
    snapshots = bg._directory_snapshots(root, tmp_path)
    assert len(snapshots) == 1 and snapshots[0].error_code == "DIRECTORY_CHANGED_DURING_READ"
    assert len(calls) == 8 and delays == [0.005] * 3
    assert not bg._DIRECTORY_CACHE


# LLM: 权限错误不是并发换代，不重试或吞掉；沿原异常交给上层失败关闭。
# 函数用途: 防止目录重试掩盖持续权限拒绝。
def test_directory_permission_failure_is_not_retried(tmp_path, monkeypatch):
    calls = []

    # LLM: 仅拒绝本测试的stat入口，保留errno作为真实分类依据。
    # 函数用途: 模拟确定的权限拒绝。
    def denied(_path):
        calls.append(1)
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(bg, "_directory_stat_key", denied)
    with pytest.raises(PermissionError):
        bg._directory_snapshots(tmp_path, tmp_path)
    assert calls == [1]


# LLM: 空会话只能隔离严格确认的其他线程，未知归属坏JSON仍必须保留诊断。
# 函数用途: 复现None/空线程对其他会话坏记录的瞬态误报。
@pytest.mark.parametrize("thread", [None, ""])
def test_empty_thread_isolates_explicit_other_thread_error(tmp_path, thread):
    write_record(tmp_path, "bg-other", "thread-other", invalid=True)
    result = bg.list_background_processes(tmp_path, thread_id=thread)
    assert result["projection_ok"] is True and result["errors"] == []
    assert len(result["unrelated_errors"]) == 1
    assert result["unscoped_error_count"] == 1


# LLM: None在owner列表API仍表示owner范围；新TUI空会话必须在会话入口清空，不能破坏该公开列表合同。
# 函数用途: 保护owner列表与新会话观察的不同范围语义。
def test_owner_level_list_keeps_valid_rows_but_unknown_corruption_fails(tmp_path):
    root = write_record(tmp_path, "bg-other", "thread-other")
    assert bg.list_background_processes(tmp_path)["items"][0]["session_id"] == "bg-other"
    (root / "bg-unknown.json").write_text("{broken")
    result = bg.list_background_processes(tmp_path)
    assert result["projection_ok"] is False
    assert result["errors"][0]["error"] == "JSONDecodeError"


# LLM: errno仅是只读错误分类事实，权限拒绝与原子替换必须可区分，不把异常文本当分类器。
# 函数用途: 保证后台reader到TUI的错误信封不会丢失实际OSError原因。
@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ESTALE, errno.EACCES])
def test_projection_preserves_oserror_errno_for_transient_classification(tmp_path, monkeypatch, error_number):
    write_record(tmp_path)

    # LLM: 只替换文件stat边界，真实快照、owner筛选及错误投影继续执行。
    # 函数用途: 注入带准确errno的文件观察错误。
    def failed_stat(_path):
        raise OSError(error_number, "record observation failed")

    monkeypatch.setattr(bg, "_file_stat_key", failed_stat)
    result = bg.list_background_processes(tmp_path, thread_id="thread-one")
    assert result["projection_ok"] is False
    assert result["errors"][0].get("errno") == error_number


# LLM: 真实导航与提示生命周期保持，只有观察数据源用确定性响应替代，不修改进程控制或scope解析。
# 函数用途: 创建一个明确绑定会话的TUI观察夹具。
def watch_fixture(tmp_path, monkeypatch):
    runtime = TuiRuntime("wave9-background")
    navigation = TuiAgentNavigationState(runtime)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), thread_id="thread-one")
    row = {"session_id": "bg-one", "thread_id": "thread-one", "command": "fixture",
           "status": "running", "alive": True, "wake_pending": False}
    response = {"value": {"projection_ok": True, "items": [row], "active_count": 1, "errors": []}}

    # LLM: 异常和结构化错误分别穿过产品的真实异常/投影出口，不能只改notice字符串。
    # 函数用途: 为连续刷新提供一个可切换结果。
    def listing(*_args, **_kwargs):
        value = response["value"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(bg, "list_background_processes", listing)
    return agent, navigation, runtime, response


# LLM: 只有同scope已验证帧可静默兜底，第三次仍须告警；errno用于区分原子替换与持久错误。
# 函数用途: 复现目录换代、记录消失及直接OSError的两次静默与第三次升级。
@pytest.mark.parametrize("failure", [
    {"projection_ok": False, "errors": [{"error_code": "DIRECTORY_CHANGED_DURING_READ"}]},
    {"projection_ok": False, "errors": [{"error": "FileNotFoundError", "errno": errno.ENOENT}]},
    FileNotFoundError(errno.ENOENT, "atomic replacement"),
])
def test_same_scope_prior_frame_hides_only_first_two_transient_polls(tmp_path, monkeypatch, failure):
    agent, nav, runtime, response = watch_fixture(tmp_path, monkeypatch)
    assert tui_threading._sync_background_watch(agent, nav)
    assert nav.snapshot().background_process_count == 1
    response["value"] = failure
    for _ in range(2):
        tui_threading._sync_background_watch(agent, nav)
        assert runtime.notice() == ""
        assert nav.snapshot().background_process_count == 1
    tui_threading._sync_background_watch(agent, nav)
    assert "仅保留同会话上次结果" in runtime.notice()
    assert nav._bg_watch_consecutive_transient_failures == 3


# LLM: 一次真正成功才结束连续失败计数并清掉观察专用提示，不清其他kind。
# 函数用途: 验证瞬态恢复后的新一轮失败重新从零计数。
def test_successful_poll_resets_transient_counter_and_own_warning(tmp_path, monkeypatch):
    agent, nav, runtime, response = watch_fixture(tmp_path, monkeypatch)
    good = response["value"]
    tui_threading._sync_background_watch(agent, nav)
    response["value"] = FileNotFoundError(errno.ENOENT, "replacement")
    for _ in range(3):
        tui_threading._sync_background_watch(agent, nav)
    assert "观察刷新失败" in runtime.notice()
    response["value"] = good
    tui_threading._sync_background_watch(agent, nav)
    assert runtime.notice() == "" and getattr(nav, "_bg_watch_consecutive_transient_failures", -1) == 0
    response["value"] = FileNotFoundError(errno.ENOENT, "replacement")
    tui_threading._sync_background_watch(agent, nav)
    assert runtime.notice() == "" and nav._bg_watch_consecutive_transient_failures == 1


# LLM: 结构损坏、混合错误、权限及原因未知的OSError不能套用瞬态静默；同scope旧帧仅作旧结果保留。
# 函数用途: 防止新兜底吞掉持续故障。
@pytest.mark.parametrize("failure", [
    {"projection_ok": False, "errors": [{"error": "JSONDecodeError"}]},
    {"projection_ok": False, "errors": [{"error_code": "DIRECTORY_CHANGED_DURING_READ"}, {"error": "ValueError"}]},
    {"projection_ok": False, "errors": []},
    PermissionError(errno.EACCES, "denied"),
    OSError("unclassified failure"),
])
def test_persistent_or_unclassified_error_warns_immediately(tmp_path, monkeypatch, failure):
    agent, nav, runtime, response = watch_fixture(tmp_path, monkeypatch)
    tui_threading._sync_background_watch(agent, nav)
    response["value"] = failure
    tui_threading._sync_background_watch(agent, nav)
    assert "观察刷新失败" in runtime.notice()


# LLM: 未验证过本scope不能静默使用空帧或另一scope的成功结果；范围解析错误仍立即失败关闭。
# 函数用途: 检查首次失败、owner/thread切换和未知范围不会借用旧缓存。
@pytest.mark.parametrize("switch", ["first", "thread", "owner", "scope_error"])
def test_transient_fallback_never_crosses_scope_or_uses_unverified_frame(tmp_path, monkeypatch, switch):
    agent, nav, runtime, response = watch_fixture(tmp_path, monkeypatch)
    if switch != "first":
        tui_threading._sync_background_watch(agent, nav)
    if switch == "thread":
        agent.thread_id = "thread-two"
    elif switch == "owner":
        agent.home_paths.owner_home_dir = tmp_path / "different-owner"
    response["value"] = FileNotFoundError(errno.ENOENT, "replacement")
    tui_threading._sync_background_watch(agent, nav, scope_error=switch == "scope_error")
    assert "观察刷新失败" in runtime.notice()
    assert nav.snapshot().background_process_count == 0


# LLM: 新本地会话没有canonical thread便不能读取owner全量列表，/new同样清除旧帧与瞬态计数。
# 函数用途: 验证未发首条消息与新建会话返回干净空观察。
@pytest.mark.parametrize("had_frame", [False, True])
def test_unstarted_local_session_and_new_are_clean_empty_without_owner_scan(tmp_path, monkeypatch, had_frame):
    agent, nav, runtime, response = watch_fixture(tmp_path, monkeypatch)
    if had_frame:
        tui_threading._sync_background_watch(agent, nav)
        response["value"] = FileNotFoundError(errno.ENOENT, "replacement")
        tui_threading._sync_background_watch(agent, nav)
    calls = []

    # LLM: 空会话不得触碰owner级查询；若误调用仍返回有效其他线程行，使可见泄漏能落在断言上。
    # 函数用途: 捕捉新会话是否越过会话范围入口。
    def owner_scan(*_args, **_kwargs):
        calls.append(True)
        return {"projection_ok": True, "items": [{"session_id": "bg-other", "thread_id": "other",
            "status": "running", "alive": True, "wake_pending": False}], "active_count": 1, "errors": []}

    agent.thread_id = None
    monkeypatch.setattr(bg, "list_background_processes", owner_scan)
    tui_threading._sync_background_watch(agent, nav)
    assert runtime.notice() == "" and nav.snapshot().background_process_count == 0
    assert calls == []
    assert nav._background_watch_scope is None
    assert nav._bg_watch_consecutive_transient_failures == 0


# LLM: 历史由真实typed事件构造，渲染次序和换行均交给产品renderer，不用假行号替代用户消息位置。
# 函数用途: 建立指定轮数与回复长度的用户历史，覆盖视口和四页窗口内外边界。
def sticky_history(*, turns=3, reply_rows=12):
    from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext
    from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
    from agent_py_agent.cli.chat_parts.tui_view import make_tui_transcript_view
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore

    store = TuiStateStore()
    seq = TuiEventSequencer("wave9-sticky", clock=lambda: 1.0)
    store.publish(seq.emit("system_message", "completed", "intro", {"text": "系统信息"}))
    for index in range(1, turns + 1):
        store.publish(seq.emit("user_message", "completed", f"u-{index}",
            {"text": f"提问 {index}\n  继续\t说明"}))
        store.publish(seq.emit("assistant_completed", "completed", f"a-{index}",
            {"text": "\n".join(f"回复 {index} 第 {row} 行" for row in range(reply_rows))}))
    view = make_tui_transcript_view(store, lambda width: TuiRenderContext(width=width))
    return view, store, seq


# LLM: 新控件缺失时先产生明确断言失败，不把AttributeError或ImportError伪装成有效红例。
# 函数用途: 取得独立header Window，证明其不属于可复制正文。
def sticky_window(view, *, modal=False):
    from prompt_toolkit.layout import Window

    window = view.modal_window if modal else view.window
    header = getattr(window, "sticky_window", None)
    assert isinstance(header, Window), "transcript window must own a dedicated sticky header Window"
    return header


# LLM: 仅运行prompt_toolkit真实Window/Screen布局，不启动交互服务或模型；不能外推为人工终端端到端。
# 函数用途: 为正文与独立顶栏生成真实屏幕和原生鼠标分发表。
def paint_transcript(view, *, modal=False, width=80, height=8, app=None):
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition
    from prompt_toolkit.output import DummyOutput

    window = view.modal_window if modal else view.window
    if app is None:
        app = Application(layout=Layout(window), input=DummyInput(), output=DummyOutput())
    # 与Application._redraw一致：每次真实渲染递增代次，不能让FormattedTextControl复用上一代片段。
    app.render_counter += 1
    screen, handlers = Screen(), MouseHandlers()
    with set_app(app):
        window.write_to_screen(screen, handlers, WritePosition(0, 0, width, height), "", True, None)
    return app, screen, handlers


# LLM: 读取Screen字符格而非原始ANSI输出；中文续格保持产品布局解释，不依赖终端字体。
# 函数用途: 获取指定真实渲染屏幕行的可见文字。
def screen_line(screen, row, width=80):
    return "".join(screen.data_buffer[row][column].char for column in range(width)).rstrip()


# LLM: 用产品稀疏页定位与ReadingAnchor进入指定原文行，不渲染整段历史或伪造页内坐标。
# 函数用途: 在千行回复中打开完整原文并将目标来源行对齐视口顶部。
def focus_long_transcript(view, store, block_id, source_row):
    from agent_py_agent.cli.chat_parts.tui_reading import ReadingAnchor, viewport_top

    view.control.create_content(80, 8)
    view.enter_transcript(store.snapshot())
    view.toggle_full_detail()
    view.provider.transcript_state.focus_complete_block(block_id, source_row)
    view.modal_control.restore_reading_anchor(ReadingAnchor(block_id, source_row, 0))
    view.modal_control.create_content(80, 8)
    frame = view.provider.frame(80)
    assert frame.line_origins[viewport_top(view.modal_control)] == (block_id, source_row)
    assert len(frame.detail_page_offsets) <= 4


# LLM: U1首行在四页窗口之外时仍须有顶栏；使用原生鼠标分发表点击并核对真实正文首行，不只检查回调被调用。
# 函数用途: 复现并守住千行回复第700来源行的用户消息跨窗口跳转。
def test_sticky_full_transcript_1000_line_reply_keeps_user_at_source_row_700():
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from agent_py_agent.cli.chat_parts.tui_reading import ReadingAnchor, viewport_top

    view, store, _seq = sticky_history(turns=1, reply_rows=1000)
    focus_long_transcript(view, store, "a-1", 700)
    control = view.modal_control
    assert all(key != "u-1" for key, _row in view.provider.frame(80).line_origins)
    target = control.sticky_user_turn()
    assert target is not None and target.block_id == "u-1"
    app, screen, handlers = paint_transcript(view, modal=True)
    assert "上一条我说的话" in screen_line(screen, 0)
    with set_app(app):
        handlers.mouse_handlers[0][2](MouseEvent(Point(2, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()))
    assert control.reading_anchor() == ReadingAnchor("u-1", 0, 0)
    assert not control.is_following()
    assert control.sticky_user_turn() is None
    _app, screen, _handlers = paint_transcript(view, modal=True, app=app)
    frame = view.provider.frame(80)
    assert frame.line_origins[viewport_top(control)] == ("u-1", 0)
    assert screen_line(screen, 0).startswith("── 用户")
    assert "上一条我说的话" not in screen_line(screen, 0)
    assert len(frame.detail_page_offsets) <= 4


# LLM: 每轮回复跨越四页；有/无两次点击间重绘都必须立即选到前一条，不能靠下一次布局修补或一次性展开全部页。
# 函数用途: 从A3第20来源行连续左击U3→U2→U1，最终才隐藏独立顶栏。
@pytest.mark.parametrize("redraw_between_clicks", [False, True])
def test_sticky_full_transcript_three_1000_line_turns_step_across_page_windows(redraw_between_clicks):
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from agent_py_agent.cli.chat_parts.tui_reading import ReadingAnchor, viewport_top

    view, store, _seq = sticky_history(reply_rows=1000)
    focus_long_transcript(view, store, "a-3", 20)
    control = view.modal_control
    assert all(key != "u-2" for key, _row in view.provider.frame(80).line_origins)
    app, screen, handlers = paint_transcript(view, modal=True)
    assert "上一条我说的话" in screen_line(screen, 0)
    for expected, previous in (("u-3", "u-2"), ("u-2", "u-1"), ("u-1", None)):
        target = control.sticky_user_turn()
        assert target is not None and target.block_id == expected
        with set_app(app):
            handlers.mouse_handlers[0][2](MouseEvent(Point(2, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()))
        assert control.reading_anchor() == ReadingAnchor(expected, 0, 0)
        assert not control.is_following()
        following = control.sticky_user_turn()
        assert (following.block_id if following else None) == previous
        frame = view.provider.frame(80)
        assert frame.line_origins[viewport_top(control)] == (expected, 0)
        assert len(frame.detail_page_offsets) <= 4
        if redraw_between_clicks:
            app, screen, handlers = paint_transcript(view, modal=True, app=app)
            frame = view.provider.frame(80)
            assert frame.line_origins[viewport_top(control)] == (expected, 0)
    _app, screen, _handlers = paint_transcript(view, modal=True, app=app)
    assert screen_line(screen, 0).startswith("── 用户")
    assert "上一条我说的话" not in screen_line(screen, 0)


# LLM: 页窗口变化只排版至多四页、复用冻结元数据；live新增用户不得混入，重绑来源必须释放旧目标。
# 函数用途: 守住全历史用户索引的轻量缓存、冻结范围和来源切换边界。
def test_sticky_full_index_reuses_frozen_metadata_and_bounds_page_rendering(monkeypatch):
    from agent_py_agent.cli.chat_parts.tui_complete_detail import CompleteDetailPage
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore

    view, store, seq = sticky_history(reply_rows=1000)
    focus_long_transcript(view, store, "a-3", 20)
    cache = view.provider._complete_index
    rows = view.provider.user_turn_lines(80)
    assert [row.block_id for row in rows] == ["u-1", "u-2", "u-3"]
    assert rows[0].start_line is None and rows[1].start_line is None
    render = CompleteDetailPage.render_located
    calls = []

    # LLM: 包装真实页渲染器计数，不替换页内容或来源坐标。
    # 函数用途: 捕获每次窗口刷新实际排版的页数。
    def counted_render(page, width):
        calls.append(page)
        return render(page, width)

    monkeypatch.setattr(CompleteDetailPage, "render_located", counted_render)
    view.provider.transcript_state.focus_complete_block("a-2", 700)
    view.provider.frame(80)
    assert 0 < len(calls) <= 4
    assert view.provider._complete_index is cache
    assert view.provider.user_turn_lines(80) is view.provider.user_turn_lines(80)
    store.publish(seq.emit("user_message", "completed", "late-live", {"text": "冻结之后的新消息"}))
    assert [row.block_id for row in view.provider.user_turn_lines(80)] == ["u-1", "u-2", "u-3"]
    assert view.provider._complete_index is cache
    view.set_state_store(TuiStateStore())
    assert view.provider.user_turn_lines(80) == ()
    assert view.provider._complete_index is not cache


# LLM: 十万远端页只是一条稀疏segment；建立用户索引和跨窗回跳均不得读取全部归档。
# 函数用途: 验证遥远归档窗口仍选到U3，回跳只读取目标附近有限页。
def test_sticky_full_index_keeps_remote_history_sparse():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from agent_py_agent.cli.chat_parts.tui_reading import ReadingAnchor, viewport_top

    view, store, seq = sticky_history()
    reference = {"schema": "display_archive_ref.v1", "archive_id": "c" * 32,
                 "thread_id": "wave9-sticky", "page_count": 100_000}
    store.publish(seq.emit("tool_completed", "completed", "remote", {"output": "preview", "display_archive_ref": reference}))
    view.control.create_content(80, 8)
    view.enter_transcript(store.snapshot())
    view.toggle_full_detail()
    state, control = view.provider.transcript_state, view.modal_control
    requests = []
    state.request_complete_page = lambda ref, page: requests.append((ref, page))
    state.focus_complete_block("remote")
    assert state.move_complete_page(50_000)
    control.restore_reading_anchor(ReadingAnchor("remote", 0, 0))
    control.create_content(80, 8)
    assert len(state._complete_pages.segments) == 2
    assert 0 < len(requests) <= 4
    assert all(49_999 <= page <= 50_002 for _ref, page in requests)
    assert control.sticky_user_turn().block_id == "u-3"
    requests.clear()
    control.sticky_user_bar_mouse_handler(MouseEvent(Point(2, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()))
    assert len(requests) <= 3 and all(page < 3 for _ref, page in requests)
    after_jump = len(requests)
    # 点击末尾invalidate后，下一个观察入口可再构建一帧；分别核对两帧，不能把两次有界读取误算成一次。
    assert control.reading_anchor() == ReadingAnchor("u-3", 0, 0)
    assert view.provider.frame(80).line_origins[viewport_top(control)] == ("u-3", 0)
    assert control.sticky_user_turn().block_id == "u-2"
    assert len(requests) - after_jump <= 3 and all(page < 3 for _ref, page in requests)


# LLM: 索引必须取真实renderer偏移；同帧复用，不因滚动重扫历史；宽度变化必须重排并重建索引。
# 函数用途: 验证用户role筛选、物理起始行与单行摘要。
@pytest.mark.parametrize("width", [5, 20, 80])
def test_sticky_index_uses_exact_rendered_user_block_offsets(width):
    from agent_py_agent.cli.chat_parts.tui_markdown import display_width_text

    view, store, _seq = sticky_history()
    indexer = getattr(view.provider, "user_turn_lines", None)
    assert callable(indexer), "frame provider must index rendered user turn lines"
    frame = view.provider.frame(width)
    indexed = indexer(width)
    offsets = dict(frame.block_line_offsets)
    users = [block for block in store.snapshot().stable_blocks if block.role == "user"]
    assert [(row.block_id, row.start_line) for row in indexed] == [(block.block_id, offsets[block.block_id]) for block in users]
    assert indexed is indexer(width)
    assert all("\n" not in row.summary and "\t" not in row.summary for row in indexed)
    assert all(display_width_text(row.summary) <= width for row in indexed)
    if width == 80:
        assert [row.summary for row in indexed] == [f"提问 {index} 继续 说明" for index in range(1, 4)]


# LLM: start_line必须严格小于viewport_top，等号时选择前一条；首条上方系统文本不冒充用户消息。
# 函数用途: 核对顶栏在顶部、首条及各用户边界的选择。
@pytest.mark.parametrize("target_index", [0, 1, 2])
def test_sticky_selection_is_strictly_above_viewport_and_hides_at_first_user(target_index):
    from agent_py_agent.cli.chat_parts.tui_reading import viewport_top

    view, _store, _seq = sticky_history()
    control = view.control
    selector = getattr(control, "sticky_user_turn", None)
    assert callable(selector), "transcript control must select the prior user turn"
    control.create_content(80, 5)
    rows = view.provider.user_turn_lines(80)
    target = rows[target_index]
    control.jump_to(target.start_line + 4)
    assert viewport_top(control) == target.start_line
    selected = selector()
    assert selected == (rows[target_index - 1] if target_index else None)
    control.move_home()
    assert viewport_top(control) == 0 and selector() is None


# LLM: 真实mouse分发表必须接上header的左键释放；每次点击重新选择而非复用首次捕获的N，普通/详情/原文同样适用。
# 函数用途: 验证连续三次上跳和隐藏，以及逻辑锚点、真实body视口首行与禁用跟随。
@pytest.mark.parametrize("mode", ["normal", "detail", "complete"])
@pytest.mark.parametrize("redraw_between_clicks", [False, True])
def test_sticky_three_step_clicks_jump_n_to_n_minus_one_to_first_then_hide(mode, redraw_between_clicks):
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from agent_py_agent.cli.chat_parts.tui_reading import viewport_top

    view, store, _seq = sticky_history()
    modal = mode != "normal"
    # 详情模式必须继承已显示的主视口位置，不能把尚未布局的初始首行误当成尾部。
    view.control.create_content(80, 8)
    if modal:
        view.enter_transcript(store.snapshot())
        if mode == "complete":
            view.toggle_full_detail()
    header = sticky_window(view, modal=modal)
    control = view.modal_control if modal else view.control
    app, screen, handlers = paint_transcript(view, modal=modal)
    assert "上一条我说的话" in screen_line(screen, 0)
    rows = {row.block_id: row for row in view.provider.user_turn_lines(80)}
    for expected in ("u-3", "u-2", "u-1"):
        assert control.sticky_user_turn().block_id == expected
        with set_app(app):
            event = MouseEvent(Point(2, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
            handlers.mouse_handlers[0][2](event)
        assert viewport_top(control) == rows[expected].start_line
        anchor = control.reading_anchor()
        assert anchor.block_id == expected and anchor.line_offset == 0 and anchor.screen_row == 0
        assert control.is_following() is False
        if redraw_between_clicks:
            app, screen, handlers = paint_transcript(view, modal=modal, app=app)
    assert control.sticky_user_turn() is None
    assert "上一条我说的话" not in "".join(fragment[1] for fragment in header.content.create_content(80, 1).get_line(0))
    _app, screen, _handlers = paint_transcript(view, modal=modal, app=app)
    expected_prefix = "── 用户" if mode == "complete" else "❯ 提问 1"
    assert screen_line(screen, 0).startswith(expected_prefix)
    assert control._last_render_height == 8


# LLM: 独立header不进入transcript_lines、复制、搜索或逻辑行号，实际绘制仅占视口外的一行。
# 函数用途: 验证显示导航不污染正文和复制结果。
def test_sticky_header_is_outside_copyable_transcript_and_line_numbers():
    from agent_py_agent.cli.chat_parts.tui_reading import viewport_top

    view, _store, _seq = sticky_history()
    sticky_window(view)
    control = view.control
    before = view.provider.frame(80)
    control.create_content(80, 8)
    control.select_all()
    plain = control.selected_text()
    _app, screen, _handlers = paint_transcript(view)
    control.select_all()
    assert control.selected_text() == plain
    assert "上一条我说的话" not in plain
    assert before.block_line_offsets == view.provider.frame(80).block_line_offsets
    assert before.transcript_lines == view.provider.frame(80).transcript_lines
    assert "上一条我说的话" in screen_line(screen, 0)
    assert control._last_render_height == 7
    content = control.create_content(80, 7)
    expected = "".join(fragment[1] for fragment in content.get_line(viewport_top(control)))
    assert screen_line(screen, 1) == expected.rstrip()


# LLM: 顶栏点击只能消费左键释放，右键/中键/按下不能跳；正文首行的鼠标选区仍属于正文而非header。
# 函数用途: 核对独立顶栏与正文鼠标语义不混淆。
@pytest.mark.parametrize("event_type,button", [("MOUSE_DOWN", "LEFT"), ("MOUSE_UP", "RIGHT"), ("MOUSE_UP", "MIDDLE")])
def test_sticky_non_left_release_does_not_jump(event_type, button):
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    view, _store, _seq = sticky_history()
    header = sticky_window(view)
    app, _screen, _handlers = paint_transcript(view)
    before = view.control.current_line()
    with set_app(app):
        header.content.mouse_handler(MouseEvent(Point(2, 0), getattr(MouseEventType, event_type), getattr(MouseButton, button), frozenset()))
    assert view.control.current_line() == before and view.control.is_following()


# LLM: 摘要先净化终端控制字符、压空白并按grapheme/终端列截断，不从中间切断组合emoji。
# 函数用途: 核对窄终端摘要省略号、安全性和字形边界。
@pytest.mark.parametrize("width,expected", [(1, "…"), (3, "👩‍👩‍👧‍👦…"), (5, "👩‍👩‍👧‍👦👩‍👩‍👧‍👦…")])
def test_sticky_summary_collapses_sanitizes_and_truncates_display_cells(width, expected):
    view, store, seq = sticky_history()
    indexer = getattr(view.provider, "user_turn_lines", None)
    assert callable(indexer)
    store.publish(seq.emit("user_message", "completed", "unicode-user", {"text": "👩‍👩‍👧‍👦" * 20 + "\n\té\x1b[2J"}))
    row = indexer(width)[-1]
    assert row.block_id == "unicode-user" and row.summary == expected
    assert "\x1b" not in row.summary and "\n" not in row.summary and "\t" not in row.summary


# LLM: provider切换源时索引不能保留另一个会话/代理的用户块；空源顶栏必须隐藏并占零行。
# 函数用途: 保护新会话和代理切换时的顶栏缓存隔离。
def test_sticky_source_switch_clears_old_user_index_and_header():
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore

    view, _store, _seq = sticky_history()
    sticky_window(view)
    app, screen, _handlers = paint_transcript(view)
    assert "上一条我说的话" in screen_line(screen, 0)
    view.set_state_store(TuiStateStore(), start_at_top_if_new=True)
    _app, screen, _handlers = paint_transcript(view, app=app)
    assert view.provider.user_turn_lines(80) == ()
    assert view.control.sticky_user_turn() is None
    assert "上一条我说的话" not in screen_line(screen, 0)


# LLM: 高度变化仅改变正文可用行数；不能将新增header误当成正文行，让手动阅读位置逐帧漂移。
# 函数用途: 验证同帧重复绘制、终端高度变化和仅剩一行时的正文优先。
def test_sticky_layout_preserves_manual_top_across_height_changes():
    from agent_py_agent.cli.chat_parts.tui_reading import viewport_top

    view, _store, _seq = sticky_history()
    sticky_window(view)
    app, _screen, _handlers = paint_transcript(view)
    control = view.control
    control.move(-5)
    top = viewport_top(control)
    for height in (8, 12, 4, 1, 8):
        app, screen, _handlers = paint_transcript(view, height=height, app=app)
        assert viewport_top(control) == top
        assert not control.is_following()
        assert ("上一条我说的话" in screen_line(screen, 0)) is (height > 1)
        assert control._last_render_height == (height - 1 if height > 1 else 1)


# LLM: 整行header都是同一只读点击区，宽字符与尾部填充不能导致右侧点击落空。
# 函数用途: 经真实屏幕鼠标反解确认顶栏最右侧同样能上跳。
def test_sticky_rightmost_header_cell_is_clickable():
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    view, _store, _seq = sticky_history()
    sticky_window(view)
    app, _screen, handlers = paint_transcript(view)
    with set_app(app):
        handlers.mouse_handlers[0][79](MouseEvent(Point(79, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()))
    assert view.control.reading_anchor().block_id == "u-3"
    assert view.control.sticky_user_turn().block_id == "u-2"


# LLM: home丢失不是可沿用旧scope的成功观察；必须立即清掉前一个owner的帧和资格。
# 函数用途: 防止owner范围缺失时遗留上次已验证的进程行。
def test_background_owner_scope_loss_clears_prior_frame_immediately(tmp_path, monkeypatch):
    agent, nav, runtime, _response = watch_fixture(tmp_path, monkeypatch)
    tui_threading._sync_background_watch(agent, nav)
    assert nav.snapshot().background_process_count == 1
    agent.home_paths.owner_home_dir = None
    tui_threading._sync_background_watch(agent, nav)
    assert nav.snapshot().background_process_count == 0
    assert "会话范围未确认" in runtime.notice()
    assert nav._background_watch_scope is None and nav._bg_watch_consecutive_transient_failures == 0


# LLM: 未装配本地owner观察的轻客户端没有旧帧，不应因不支持此观察入口而制造新误报。
# 函数用途: 保护未配置owner的首次刷新仍保持原静默行为。
def test_unconfigured_background_owner_without_prior_frame_stays_silent(tmp_path, monkeypatch):
    agent, nav, runtime, _response = watch_fixture(tmp_path, monkeypatch)
    agent.home_paths.owner_home_dir = None
    tui_threading._sync_background_watch(agent, nav)
    assert runtime.notice() == "" and nav.snapshot().background_process_count == 0


# LLM: 某些旧格式错误在动态概要阶段才发现；不能只隔离reader失败而遗漏同样明确的其他线程错误。
# 函数用途: 验证None线程也隔离概要验证失败，但显式当前线程仍报告真实损坏。
def test_empty_thread_also_isolates_attributed_summary_validation_error(tmp_path):
    root = write_record(tmp_path, "bg-other", "thread-other")
    path = root / "bg-other.json"
    payload = json.loads(path.read_text())
    payload["pid"] = "invalid-pid"
    path.write_text(json.dumps(payload))
    empty = bg.list_background_processes(tmp_path, thread_id=None)
    assert empty["projection_ok"] is True and empty["errors"] == []
    assert len(empty["unrelated_errors"]) == 1
    current = bg.list_background_processes(tmp_path, thread_id="thread-other")
    assert current["projection_ok"] is False and current["errors"][0]["error"] == "ValueError"
