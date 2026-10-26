# LLM: 只断言程序事实、渲染与只读边界；临时数据不发唤醒、不停止真实服务。
# 模块用途: 回归回合尾标和后台进程观察入口。
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import background_process_view as bg
from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    fragments_text,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.turn_completion import format_turn_completion_line, format_turn_duration


# LLM: 参数覆盖秒、分钟、小时和进位边界，不调用任何时钟或外部进程。
# 函数用途: 核对时长格式合同。
@pytest.mark.parametrize("seconds,expected", [(0, "0s"), (0.2, "1s"), (12, "12s"), (106, "1m 46s"), (120, "2m 0s"), (3725, "1h 2m 5s"), (59.9, "1m 0s")])
def test_duration(seconds, expected):
    assert format_turn_duration(seconds) == expected


# LLM: 固定动词只隔离选词变量；成功、中断与失败措辞必须沿同一格式入口。
# 函数用途: 核对三类终态尾标。
@pytest.mark.parametrize("status,expected", [("done", "✻ Baked for 1m 46s"), ("interrupted", "✻ Interrupted after 1m 46s"), ("failed", "✻ Failed after 1m 46s")])
def test_completion_line(status, expected):
    assert format_turn_completion_line(106, status=status, verb="Baked") == expected


# LLM: 只替换单调时钟；真实 runtime、去重与渲染入口共同证明尾标位置和样式。
# 函数用途: 验证最终正文后只出现一次灰色计时，原底栏不被覆盖。
def test_completion_is_dim_in_transcript_not_footer(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    runtime = TuiRuntime("wp5-timing")
    runtime.enqueue_prompt("turn-one", "任务", queued=False)
    adapter = runtime.begin_turn("turn-one")
    clock[0] = 116.0
    adapter.finalize(TuiTurnSummary(response_text="最终回复"))
    adapter.finalize(TuiTurnSummary(response_text="最终回复"))
    snapshot = runtime.store.snapshot()
    frame = render_tui_snapshot(snapshot, TuiRenderContext(width=180))
    transcript = [fragments_text(line) for line in frame.transcript_lines]
    timing = [text for text in transcript if "✻" in text]
    assert len(timing) == 1
    assert "1m 46s" in timing[0] and "完成于" in timing[0]
    assert transcript.index(timing[0]) > next(i for i, text in enumerate(transcript) if "最终回复" in text)
    assert "F6 原生模式" in fragments_text(frame.footer)
    assert "✻" not in fragments_text(frame.footer)
    assert any(style == "class:tui-muted" and "✻" in text for line in frame.transcript_lines for style, text, *_ in line)


# LLM: 仅在测试临时目录构造记录和超限日志；不登记或启动真实受管进程。
# 函数用途: 准备传统目录与真实目录格式的观察样本。
def _session(home: Path, *, session_id="bg-example", status="running", thread_id="thread-one", native=False):
    home.mkdir(parents=True, exist_ok=True)
    root = process_session_store_root(home, home) if native else home / "workspace/runtime/processes/sessions"
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{session_id}.log"
    output.write_text("old\n" + "x" * 70000 + "\nline-one\nline-two\n", encoding="utf-8")
    payload = {"session_id": session_id, "command": "python worker.py", "status": status, "pid": os.getpid(),
               "cwd": str(home), "thread_id": thread_id, "started_at": time.time() - 106,
               "exit_code": 0 if status == "exited" else None, "output_path": str(output),
               "completion_notice_id": "notified" if status == "exited" else "",
               "completion_target": {"thread_id": thread_id, "task_id": "task-one", "run_id": "run-one"}}
    (root / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return root, output


# LLM: 字节、inode 和 mtime 同时快照；观察前后比较可捕捉写回和新建锁文件。
# 函数用途: 记录临时数据树的文件事实。
def _files(root):
    return {str(path.relative_to(root)): (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


# LLM: 覆盖两个目录来源并检查线程筛选、活动数、尾部上限和唤醒文件不变。
# 函数用途: 证明列表与详情入口没有磁盘副作用。
@pytest.mark.parametrize("native", [False, True])
def test_list_and_inspect_are_read_only(tmp_path, native):
    home = tmp_path / "owner"
    root, _ = _session(home, native=native)
    _session(home, session_id="bg-finished", status="exited", native=native)
    wake = home / "pending_process_events.json"
    wake.write_text('{"pending":["wake-one"]}', encoding="utf-8")
    before = _files(tmp_path)
    result = bg.list_background_processes(home, thread_id="thread-one")
    assert result["total"] == 2 and result["active_count"] == 1
    assert bg.list_background_processes(home, thread_id="other")["items"] == []
    assert len(bg.list_background_processes(home, active_only=True)["items"]) == 1
    detail = bg.inspect_background_process(home, "bg-example", tail_lines=2)
    assert detail["log_tail"] == ["line-one", "line-two"]
    assert detail["pid"] == os.getpid() and detail["status"] == "running"
    assert detail["task_id"] == "task-one"
    assert _files(tmp_path) == before
    assert not (root / ".process-sessions.lock").exists()


# LLM: 恶意身份必须在读取前被拒绝，不能通过路径解析越出会话目录。
# 函数用途: 回归进程身份的路径穿越防护。
@pytest.mark.parametrize("session_id", ["../bad", "bg-../bad", "/tmp/bad", "bg-a/b", "bg-a\\b"])
def test_inspect_rejects_traversal(tmp_path, session_id):
    with pytest.raises(ValueError):
        bg.inspect_background_process(tmp_path, session_id)


# LLM: 缺失目录也是只读边界，列表空结果不能附带 mkdir。
# 函数用途: 验证观察不会创建私人空间。
def test_missing_home_is_not_created(tmp_path):
    home = tmp_path / "missing"
    assert bg.list_background_processes(home)["items"] == []
    assert not home.exists()


# LLM: 导航只用真实展示状态机；进程页面不能暴露代理控制身份或可输入权限。
# 函数用途: 核对后台入口优先、只读检查和返回路径。
def test_navigation_background_entry_precedes_goal_and_children():
    root = TuiRuntime("wp5-nav")
    nav = TuiAgentNavigationState(root)
    update = getattr(nav, "update_background_watch_rows", lambda rows: False)
    assert update([{"session_id": "bg-example", "command": "worker", "pid": 123,
                    "status": "running", "alive": True, "elapsed_seconds": 106, "log_tail": ["latest"]}]) is True
    nav.update_rows("", [{"run_id": "child-one", "parent_run_id": "", "status": "RUNNING"}])
    nav.update_goal_rows([{"goal_id": "goal-one", "status": "active"}])
    nav.move_selection(-1)
    assert nav.snapshot().selected_run_id == "bg:watch"
    assert nav.enter_selected()
    assert nav.snapshot().can_send_input is False
    assert nav.snapshot().active_run_id == ""
    assert nav.snapshot().background_watch_active is True
    watch = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    assert any("latest" in fragments_text(line) for line in watch.transcript_lines)
    assert nav.back() and nav.active_runtime() is root
    nav.move_selection(1)
    assert nav.snapshot().selected_run_id == "goal:goal-one"


# LLM: 样式、底栏顺序与缓存键必须同时反映后台计数，不能只核对文案。
# 函数用途: 验证后台徽标可见且计数变化触发新帧。
def test_background_badge_is_colored_after_f6_and_part_of_frame_key():
    from dataclasses import replace

    from agent_py_agent.cli.chat_parts.tui_block_renderer import tui_render_context_key

    runtime = TuiRuntime("badge")
    context = TuiRenderContext(width=180)
    # 字段尚未接通时在断言上红灯，而不是构造参数异常。
    assert "background_process_count" in context.__dataclass_fields__
    active = replace(context, background_process_count=2)
    frame = render_tui_snapshot(runtime.store.snapshot(), active)
    text = fragments_text(frame.footer)
    assert text.index("F6 原生模式") < text.index("⚡ 后台驻留 2 项")
    assert any(style == "class:tui-bg-watch" for style, *_ in frame.footer)
    assert tui_render_context_key(runtime.store.snapshot(), context) != tui_render_context_key(runtime.store.snapshot(), active)


# LLM: 多次观察更新只能替换活动块，不应向根对话或观察页永久历史追加日志。
# 函数用途: 核对日志选择与可移除观察块的实时更新。
def test_watch_page_refreshes_same_removable_block():
    root = TuiRuntime("watch-live")
    nav = TuiAgentNavigationState(root)
    rows = [{"session_id": "bg-one", "status": "running", "alive": True, "command": "one", "log_tail": ["first"]},
            {"session_id": "bg-two", "status": "running", "alive": True, "command": "two", "log_tail": ["second"]}]
    assert getattr(nav, "update_background_watch_rows", lambda _: False)(rows)
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    assert nav.snapshot().selected_run_id == "bg:bg-two"
    assert nav.update_background_watch_rows([{**rows[0]}, {**rows[1], "log_tail": ["new-second"]}])
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    text = "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert "new-second" in text and "first" not in text
    assert len(nav.active_runtime().store.snapshot().active_blocks) == 1
    assert not nav.active_runtime().store.snapshot().stable_blocks
    assert not root.store.snapshot().stable_blocks


# LLM: 调用真实通知同步接缝读取临时 owner 记录；根页计数和观察页详情均保持文件不变。
# 函数用途: 验证轮询将只读进程投影接入导航与日志。
def test_polling_connects_owner_records_and_only_reads_logs_when_watching(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    _session(home)
    root = TuiRuntime("poll")
    nav = TuiAgentNavigationState(root)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home))
    before = _files(tmp_path)
    assert _consume_background_notices(agent, "session", root, [None], set(), [0], nav)
    assert nav.snapshot().background_process_count == 1
    nav.move_selection(1)
    nav.enter_selected()
    assert _consume_background_notices(agent, "session", root, [None], set(), [0], nav)
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    assert any("line-two" in fragments_text(line) for line in frame.transcript_lines)
    assert _files(tmp_path) == before


# LLM: Esc 直接走原按键函数并记录中断派发反例，不能把退出观察误作取消前台。
# 函数用途: 验证后台观察的立即返回边界。
def test_escape_from_watch_never_interrupts_foreground(monkeypatch):
    import threading
    from types import SimpleNamespace

    from prompt_toolkit.widgets import TextArea

    from agent_py_agent.cli.chat_parts import tui_keybindings as keys

    root = TuiRuntime("watch-escape")
    nav = TuiAgentNavigationState(root)
    nav.update_background_watch_rows([{"session_id": "bg-example", "alive": True}])
    nav.move_selection(1)
    nav.enter_selected()
    interrupted = []
    monkeypatch.setattr(keys, "_dispatch_active_interrupt", lambda *args: interrupted.append(True))
    params = SimpleNamespace(input_area=TextArea(), agent_navigation=nav, tui_runtime=root,
                             state_lock=threading.RLock(), is_running_ref=[True], pending_jobs_ref=[0],
                             exit_armed_at_ref=[0.0], eof_armed_at_ref=[0.0],
                             escape_armed_at_ref=[0.0], escape_armed_text_ref=[""])
    keys._handle_escape_keybinding(SimpleNamespace(app=SimpleNamespace(invalidate=lambda: None)), params)
    assert interrupted == []
    assert nav.active_runtime() is root


# LLM: 损坏观察记录必须显式报错但仍获取原通知，不能拖停独立的同步链路。
# 函数用途: 回归坏记录与原通知同步的隔离。
def test_bad_process_record_does_not_block_original_notice_fetch(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    root, _ = _session(home)
    (root / "bg-broken.json").write_text("broken-json", encoding="utf-8")
    fetched = []

    # LLM: 记录真实同步接缝是否被调用，不模拟观察器的异常处理。
    # 函数用途: 提供无通知的成功响应并保留调用证据。
    def fetch(*args, **kwargs):
        fetched.append(True)
        return {"ok": True, "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0, "event_stream_id": ""}

    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), request_background_notices=fetch)
    runtime = TuiRuntime("read-failure")
    nav = TuiAgentNavigationState(runtime)
    try:
        result = _consume_background_notices(agent, "session", runtime, [None], set(), [0], nav)
    except OSError:
        result = False
    assert result and fetched == [True]
    assert "观察刷新失败" in runtime.notice()


# LLM: 不安全日志仍应拒读，但选中项详情失败不能阻止原通知响应进入 TUI。
# 函数用途: 回归日志拒读与正常通知链路互不干扰。
def test_bad_log_path_does_not_block_original_notice_fetch(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    root, _ = _session(home)
    record_path = root / "bg-example.json"
    payload = json.loads(record_path.read_text())
    payload["output_path"] = str(tmp_path / "outside-secret")
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    runtime = TuiRuntime("bad-log")
    nav = TuiAgentNavigationState(runtime)
    nav.update_background_watch_rows([{"session_id": "bg-example", "alive": True}])
    nav.move_selection(1)
    nav.enter_selected()
    calls = []
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), request_background_notices=lambda *args, **kwargs: calls.append(True) or
        {"ok": True, "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0, "event_stream_id": ""})
    try:
        result = _consume_background_notices(agent, "session", runtime, [None], set(), [0], nav)
    except ValueError:
        result = False
    assert result and calls == [True]


# LLM: 完整活动数是独立后端事实，不能从有界展示行再次统计。
# 函数用途: 验证大列表截断不缩小徽标计数。
def test_background_count_is_not_limited_to_visible_rows():
    import inspect

    nav = TuiAgentNavigationState(TuiRuntime("count-all"))
    assert "active_count" in inspect.signature(nav.update_background_watch_rows).parameters
    nav.update_background_watch_rows([{"session_id": "bg-example", "alive": True}], active_count=140)
    assert nav.snapshot().background_process_count == 140


# LLM: 缺失终止时间的终态记录不能用当前时间补成持续增长的计时。
# 函数用途: 验证未知耗时保持未知。
def test_terminal_missing_end_time_is_unknown_and_not_growing(tmp_path):
    home = tmp_path / "owner"
    _session(home, status="exited")
    first = bg.inspect_background_process(home, "bg-example")
    assert first["elapsed_seconds"] is None


# LLM: 复用真实 v4 夹具并禁止可写事务；伪 PID 不代表真实进程生命周期验证。
# 函数用途: 验证规范记录与待唤醒状态可只读观察。
def test_actual_v4_records_are_observed_without_recovery_or_wake_consumption(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
    from agent_py_agent.tests import test_process_completion_events as fixtures

    # 夹具中的 PID 为伪终态实例；本测试只验真实 v4 文件读取，不探测伪实例出生时间。
    monkeypatch.setattr(fixtures, "capture_process_birth_token", lambda pid: "fixture-launcher")
    agent, _, authority = fixtures._fixture(tmp_path, managed=True)
    home = agent.home_paths.owner_home_dir
    before = _files(tmp_path)

    # LLM: 观察入口一旦误入 Store 事务就明确失败，不允许恢复或落盘。
    # 函数用途: 捕获观察器的可写查询回归。
    def forbidden(*args, **kwargs):
        pytest.fail("观察不得进入可写的 Store 事务")

    monkeypatch.setattr(ProcessSessionStore, "transaction", forbidden)
    result = bg.list_background_processes(home)
    assert result["active_count"] == 1 and result["total"] == 1
    detail = bg.inspect_background_process(home, "bg-test-notice")
    assert detail["status"] == "exited" and detail["wake_pending"] is True
    assert detail["elapsed_seconds"] == 1.0
    assert _files(tmp_path) == before
    assert authority.root.exists()


# LLM: 运行真实 Application 与键盘管道，读取渲染屏幕和输入框；不启动真实模型或后台进程。
# 函数用途: 验证后台页面按键、实际显示、拒绝输入及返回后的草稿回读。
def test_prompt_toolkit_keys_open_readonly_watch_and_return_with_ctrl_g(tmp_path):
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
    from agent_py_agent.tests.test_tui_decision_menu import wait_app
    from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

    # LLM: 保持 Application 生命周期完整，最终等待退出，避免把已发送按键当作操作成功。
    # 函数用途: 执行观察页进入、切换、返回与输入验证。
    async def scenario():
        runtime = TuiRuntime("watch-keys")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        nav = TuiAgentNavigationState(runtime)
        nav.update_background_watch_rows([
            {"session_id": "bg-one", "command": "first", "alive": True, "log_tail": ["first output"]},
            {"session_id": "bg-two", "command": "second", "alive": True, "log_tail": ["second output"]},
        ])
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            params = _app_params(tmp_path, runtime, agent_navigation=nav)
            app = make_tui_app(params)
            task = asyncio.create_task(app.run_async())

            # LLM: 只读最近真实渲染的字符网格，不能用缺少正文控件的标签收集器代替。
            # 函数用途: 取得已绘制屏幕供观察页可见性断言。
            def rendered_screen():
                screen = app.renderer._last_screen
                return "\n".join("".join(row[x].char for x in sorted(row)) for _, row in sorted(screen.data_buffer.items())) if screen else ""

            try:
                await wait_app(app, lambda: app.is_running and app.render_counter > 0, "观察页测试启动")
                pipe.send_bytes(b"\x1b[A")
                await wait_app(app, lambda: nav.snapshot().selected_run_id == "bg:watch", "上键选中后台徽标")
                pipe.send_bytes(b"\r")
                await wait_app(app, lambda: nav.snapshot().background_watch_active, "进入观察页")
                await wait_app(app, lambda: "后台驻留监控" in rendered_screen(), "后台观察页正文实际显示")
                assert app.current_buffer.read_only()
                pipe.send_text("must-not-send")
                pipe.send_bytes(b"\x1b[B")
                await wait_app(app, lambda: nav.snapshot().selected_run_id == "bg:bg-two", "观察页下键切换日志")
                assert app.current_buffer.text == "" and params.jobs.empty()
                assert not nav.selected_goal()
                pipe.send_bytes(b"\x07")
                await wait_app(app, lambda: not nav.snapshot().background_watch_active, "CtrlG返回主对话")
                assert not app.current_buffer.read_only()
                pipe.send_text("returned draft")
                await wait_app(app, lambda: app.current_buffer.text == "returned draft", "回读主对话草稿")
                assert params.jobs.empty()
            finally:
                app.exit(result=0)
                assert await task == 0

    asyncio.run(scenario())


# LLM: 覆盖越界、软链接与多链接拒读；宿主禁止建硬链接时明确仅验证合成 nlink 反例。
# 函数用途: 核对日志入口的文件身份防护且不改来源内容。
@pytest.mark.parametrize("kind", ["outside", "symlink", "hardlink"])
def test_inspection_rejects_unsafe_log_files(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError

    home = tmp_path / "owner"
    root, output = _session(home)
    secret = tmp_path / "secret"
    secret.write_text("never disclose", encoding="utf-8")
    record_path = root / "bg-example.json"
    payload = json.loads(record_path.read_text())
    if kind == "outside":
        payload["output_path"] = str(secret)
    else:
        replacement = root / "unsafe.log"
        if kind == "symlink":
            replacement.symlink_to(secret)
        else:
            try:
                os.link(secret, replacement)
            except PermissionError:
                # 本机明确拒绝创建硬链接；保留 nlink=2 的同入口反例，不把 EPERM 当产品拒读证据。
                replacement.write_text("never disclose", encoding="utf-8")
                original_stat = os.stat

                # LLM: 仅对指定日志合成链接数，其余 stat 沿原实现；不冒充真实硬链接验证。
                # 函数用途: 在宿主禁止建链时保留同入口的多链接拒读反例。
                def linked_stat(path, *args, **kwargs):
                    observed = original_stat(path, *args, **kwargs)
                    if str(path) == "unsafe.log" or str(path) == str(replacement):
                        fields = list(observed)
                        fields[3] = 2
                        return os.stat_result(fields)
                    return observed

                monkeypatch.setattr(os, "stat", linked_stat)
                monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, linked_stat})
                monkeypatch.setattr(os, "supports_follow_symlinks", {*os.supports_follow_symlinks, linked_stat})
        payload["output_path"] = str(replacement)
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    before = output.read_bytes()
    with pytest.raises((OSError, ValueError)) as failure:
        bg.inspect_background_process(home, "bg-example")
    if kind == "hardlink":
        assert isinstance(failure.value, NoFollowPathError) and "not a regular file" in str(failure.value)
    assert output.read_bytes() == before and secret.read_text() == "never disclose"


# LLM: 输入明确的宿主展示事件并重放两次；此测试不证明上游计时生产或持久化已接好。
# 函数用途: 核对历史与后台计时展示重放去重且不会重启时钟。
@pytest.mark.parametrize("background", [False, True])
def test_host_timing_display_events_replay_without_new_clock(background):
    runtime = TuiRuntime("timing-replay")
    request = "bg-main:task-one:attempt-one" if background else "history:thread-one:message-one"
    row = {"schema": "background_transcript_event.v1" if background else "conversation_history_display.v1",
           "seq": 1, "task_id": "task-one", "request_id": request, "block_id": f"{request}:completion",
           "kind": "system_message", "phase": "completed",
           "payload": {"text": "原程序摘要", "source_kind": "turn_timing", "display": {
               "logical_id": "request-one", "status": "completed", "completed_at": 1700000000.0,
               "elapsed_seconds": 106.0, "source": "gateway"}}}
    for _ in range(2):
        if background:
            runtime.publish_background_transcript_events([row])
        else:
            runtime.publish_recovered_history((), display_events=(row,))
    frame = render_tui_snapshot(runtime.store.snapshot(), TuiRenderContext(width=180))
    timings = [fragments_text(line) for line in frame.transcript_lines if "✻" in fragments_text(line)]
    assert len(timings) == 1 and "1m 46s" in timings[0] and "2023" in timings[0]
    assert not runtime.completions.clocks


# LLM: 只隔离通知获取并记录首轮计划等待值，不等待真实一秒或开启驻留线程。
# 函数用途: 验证后台活动时轮询仍采用活动节奏。
def test_background_processes_keep_notice_loop_at_active_cadence(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts import tui_threading

    # LLM: 单轮退出夹具仅捕获等待参数，不参与生产状态或生命周期。
    # 类用途: 让轮询节奏断言可确定地收口。
    class Stop:
        # LLM: 首次检查保持可运行，终止由 wait 的确定返回值负责。
        # 函数用途: 允许测试进入一次轮询。
        def is_set(self):
            return False

        # LLM: 不真实睡眠，记录待验证的延迟后要求退出。
        # 函数用途: 捕获活动轮询间隔。
        def wait(self, delay):
            waits.append(delay)
            return True

    waits = []
    monkeypatch.setattr(tui_threading, "_consume_background_notices", lambda *args: True)
    runtime = TuiRuntime("bg-cadence")
    nav = TuiAgentNavigationState(runtime)
    nav.update_background_watch_rows([{"session_id": "bg-running", "alive": True}])
    tui_threading._background_notice_loop(Stop(), SimpleNamespace(), "session", runtime, [None], nav, [False])
    assert waits == [tui_threading.TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS]


# LLM: 所有数据均为同owner临时记录；显式指定thread_id时严格只返回同线程记录，未指定线程时返回全owner记录。
# 函数用途: 验证列表严格按会话过滤、Owner全局入口保留未绑定记录及只读文件边界。
@pytest.mark.parametrize("thread_id,expected", [
    (None, {"bg-one", "bg-two", "bg-global"}),
    ("", {"bg-one", "bg-two", "bg-global"}),
    ("thread-one", {"bg-one"}),
    ("thread-two", {"bg-two"}),
])
def test_thread_scoped_background_list_keeps_global_records(tmp_path, thread_id, expected):
    home = tmp_path / "owner"
    for sid, thread in [("bg-one", "thread-one"), ("bg-two", "thread-two"), ("bg-global", "")]:
        _session(home, session_id=sid, thread_id=thread)
    before = _files(tmp_path)
    result = bg.list_background_processes(home, thread_id=thread_id)
    assert {item["session_id"] for item in result["items"]} == expected
    assert result["total"] == len(expected) and result["active_count"] == len(expected)
    assert _files(tmp_path) == before


# LLM: 详情的线程拒绝必须早于日志打开；显式指定线程时跨线程和未绑定记录均拒绝，未指定线程继续支持全局只读。
# 函数用途: 防止同用户其它会话通过直接session_id读取非本会话日志。
def test_thread_scoped_inspection_rejects_foreign_record_before_log_read(tmp_path, monkeypatch):
    import inspect

    assert "thread_id" in inspect.signature(bg.inspect_background_process).parameters
    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    _session(home, session_id="bg-global", thread_id="")
    before = _files(tmp_path)
    original = bg._log_tail
    reads = []

    # LLM: 仍读取真实安全日志路径，只记录被打开的session；拒绝项不得触达该接缝。
    # 函数用途: 证明跨线程请求没有先读取日志再过滤返回值。
    def log_tail(home, root, sid, payload, count):
        reads.append(sid)
        return original(home, root, sid, payload, count)

    monkeypatch.setattr(bg, "_log_tail", log_tail)
    with pytest.raises(KeyError) as rejected:
        bg.inspect_background_process(home, "bg-one", thread_id="thread-two")
    assert rejected.value.args == ("bg-one",) and reads == []
    with pytest.raises(KeyError) as rejected_global:
        bg.inspect_background_process(home, "bg-global", thread_id="thread-two")
    assert rejected_global.value.args == ("bg-global",) and reads == []
    assert bg.inspect_background_process(home, "bg-one", thread_id="thread-one")["session_id"] == "bg-one"
    assert bg.inspect_background_process(home, "bg-global")["session_id"] == "bg-global"
    assert bg.inspect_background_process(home, "bg-one")["session_id"] == "bg-one"
    assert _files(tmp_path) == before


# LLM: 覆盖原reader认可的每个线程字段位置，防止summary漏字段后将明确归属误当全局记录。
# 函数用途: 验证列表和详情共用相同的线程身份解释。
@pytest.mark.parametrize("scope", [None, "access_scope", "execution_scope", "completion_target", "metadata", "scope"])
@pytest.mark.parametrize("key", ["thread_id", "conversation_id"])
def test_thread_scope_covers_all_record_identity_locations(tmp_path, scope, key):
    import inspect

    assert "thread_id" in inspect.signature(bg.inspect_background_process).parameters
    home = tmp_path / "owner"
    root, _ = _session(home, thread_id="")
    path = root / "bg-example.json"
    payload = json.loads(path.read_text())
    if scope is None:
        payload[key] = "thread-other"
    else:
        payload.setdefault(scope, {})[key] = "thread-other"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert bg.list_background_processes(home, thread_id="thread-one")["items"] == []
    own = bg.list_background_processes(home, thread_id="thread-other")["items"]
    assert len(own) == 1 and own[0]["thread_id"] == "thread-other"
    with pytest.raises(KeyError):
        bg.inspect_background_process(home, "bg-example", thread_id="thread-one")


# LLM: 真实runtime解析执行DTO，真实通知消费先发布本轮线程；替身只模拟Gateway运输响应，不读网络。
# 函数用途: 验证首次轮询与打开详情都按本次会话过滤，而不是晚一轮才生效。
def test_gateway_first_poll_scopes_background_list_and_detail(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    for sid, thread in [("bg-one", "thread-one"), ("bg-two", "thread-two"), ("bg-global", "")]:
        _session(home, session_id=sid, thread_id=thread)
    target = GatewayExecutionTarget("thread-one", "request", "turn", "claim").to_dict()
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), request_background_notices=lambda *args, **kwargs:
        {"ok": True, "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0,
         "event_stream_id": "", "execution_target": target})
    runtime = TuiRuntime("thread-scope")
    nav = TuiAgentNavigationState(runtime)
    updates, details = [], []
    update, detail = nav.update_background_watch_rows, bg.inspect_background_process

    # LLM: 保留真实导航更新，并记录实际投影行集合而不只检查kwargs。
    # 函数用途: 捕捉首次轮询展示的任务集合。
    def updated(rows, **kwargs):
        updates.append({row["session_id"] for row in rows})
        return update(rows, **kwargs)

    # LLM: 详情仍走真实reader与日志入口；只额外记录透传的线程字段。
    # 函数用途: 核对打开观察页之后详情同样受当前线程约束。
    def inspected(*args, **kwargs):
        details.append(kwargs.get("thread_id"))
        return detail(*args, **kwargs)

    monkeypatch.setattr(nav, "update_background_watch_rows", updated)
    monkeypatch.setattr(bg, "inspect_background_process", inspected)
    before = _files(tmp_path)
    assert _consume_background_notices(agent, "display-session", runtime, [None], set(), [0], nav)
    assert updates[-1] == {"bg-one"}
    assert nav.snapshot().background_process_count == 1
    nav.move_selection(1)
    assert nav.enter_selected() and nav.snapshot().background_watch_active
    assert _consume_background_notices(agent, "display-session", runtime, [None], set(), [0], nav)
    assert details == ["thread-one"] and updates[-1] == {"bg-one"}
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    assert "本用户全部会话" not in "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert _files(tmp_path) == before


# LLM: 本地session显示名不等于canonical线程；真实线程存储只读解析，通知业务投影独立隔离。
# 函数用途: 验证本地首次刷新使用原解析器返回的线程身份。
def test_local_background_watch_resolves_canonical_thread(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.cli.chat_parts import tui_threading

    home = tmp_path / "owner"
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": LOCAL_AGENT_USER_ID,
        "channel": LOCAL_CHAT_CHANNEL, "channel_conversation_id": "display-session", "channel_user_id": LOCAL_AGENT_USER_ID})
    resolved, error = store.threads.resolve_report(channel=LOCAL_CHAT_CHANNEL,
        channel_conversation_id="display-session", channel_user_id=LOCAL_AGENT_USER_ID)
    assert resolved is not None and resolved.thread_id == thread.thread_id and not error
    _session(home, session_id="bg-one", thread_id=thread.thread_id)
    _session(home, session_id="bg-other", thread_id="display-session")
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), conversation_store=store)
    runtime = TuiRuntime("local-canonical")
    nav = TuiAgentNavigationState(runtime)
    monkeypatch.setattr(tui_threading, "_consume_local_background_snapshot", lambda *args: True)
    assert tui_threading._consume_background_notices(agent, "display-session", runtime, [None], set(), [0], nav)
    assert nav.snapshot().background_process_count == 1


# LLM: 同一导航上的线程变化先清旧帧；新投影报错时仍不能保留上一线程的日志或计数。
# 函数用途: 回归切线程遇读错的跨会话展示残留。
def test_background_thread_switch_clears_stale_logs_before_failed_projection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    runtime = TuiRuntime("switch")
    runtime.publish_execution_target(GatewayExecutionTarget("thread-one", "request", "turn", "claim").to_dict())
    nav = TuiAgentNavigationState(runtime)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home))
    assert _sync_background_watch(agent, nav)
    nav.move_selection(1)
    assert nav.enter_selected()
    assert _sync_background_watch(agent, nav)
    runtime.publish_execution_target(GatewayExecutionTarget("thread-two", "request", "turn-two", "claim-two").to_dict())

    # LLM: 只在换线程之后制造真实刷新入口异常，检查之前的展示状态是否已被清除。
    # 函数用途: 模拟新会话记录暂时不可读。
    def failed_projection(*args, **kwargs):
        raise OSError("projection unavailable")

    monkeypatch.setattr(bg, "list_background_processes", failed_projection)
    assert _sync_background_watch(agent, nav)
    assert nav.snapshot().background_process_count == 0
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    text = "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert "bg-one" not in text and "line-two" not in text


# LLM: execution_target只代表活动claim；空闲/失败的真实运输上下文不等于可跨线程观察的旧无上下文入口。
# 函数用途: 验证空闲首轮、活动转空闲和切会话首轮失败都暂停未知范围，不读取全owner记录。
@pytest.mark.parametrize("phase", ["idle_first", "idle_after_active", "failed_first", "failed_after_switch"])
def test_gateway_unknown_thread_scope_never_expands_to_owner_records(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    _session(home, session_id="bg-two", thread_id="thread-two")
    response = {"ok": True, "notices": [], "cursor": 0, "transcript_events": [], "event_cursor": 0,
                "event_stream_id": "", "execution_target": None}
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home),
        request_background_notices=lambda *args, **kwargs: dict(response))
    runtime = TuiRuntime("unknown-thread")
    nav = TuiAgentNavigationState(runtime)
    if phase in {"idle_after_active", "failed_after_switch"}:
        response["execution_target"] = GatewayExecutionTarget("thread-one", "request", "turn", "claim").to_dict()
        assert _consume_background_notices(agent, "display-one", runtime, [None], set(), [0], nav)
        assert nav.snapshot().background_process_count == 1
        nav.move_selection(1)
        assert nav.enter_selected()
    response["execution_target"] = None
    response["ok"] = not phase.startswith("failed")
    reads = []
    original = bg.list_background_processes

    # LLM: 记录真实列表是否被触达，不用伪造空列表掩盖范围扩大。
    # 函数用途: 证明未知上下文不会读到任一线程记录。
    def listed(*args, **kwargs):
        reads.append(kwargs.get("thread_id"))
        return original(*args, **kwargs)

    monkeypatch.setattr(bg, "list_background_processes", listed)
    result = _consume_background_notices(agent, "display-two" if phase == "failed_after_switch" else "display-one",
        runtime, [None], set(), [0], nav)
    assert result is response["ok"]
    assert reads == []
    assert nav.snapshot().background_process_count == 0
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    text = "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert "bg-one" not in text and "bg-two" not in text


# LLM: 只读取真实导航当前帧，断言旧日志已消失不能以调用清空函数代替。
# 函数用途: 获取观察页实际渲染文字。
def _watch_frame_text(nav):
    frame = render_tui_snapshot(nav.active_runtime().store.snapshot(), TuiRenderContext(width=180))
    return "\n".join(fragments_text(line) for line in frame.transcript_lines)


# LLM: 通过真实观察同步和导航打开已绑定会话日志；不替换范围解析或渲染状态机。
# 函数用途: 准备可检测跨会话残留的已打开观察页。
def _open_local_watch(agent):
    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    runtime = TuiRuntime("local-scope-failure")
    nav = TuiAgentNavigationState(runtime)
    assert _sync_background_watch(agent, nav, session_id="display-one", runtime=runtime)
    assert nav.snapshot().background_process_count == 1
    nav.move_selection(1)
    assert nav.enter_selected()
    assert _sync_background_watch(agent, nav, session_id="display-one", runtime=runtime)
    assert "line-two" in _watch_frame_text(nav)
    return runtime, nav


# LLM: 使用真实Store的绑定索引和路径校验抛错，观察必须清旧帧且保留原通知异常给外层退避。
# 函数用途: 回归切到非法本地绑定时通知消费未触达观察同步的遗漏路径。
def test_local_invalid_store_binding_clears_watch_even_when_notice_consumption_raises(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.conversation.store_threads import _binding_key
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    home = tmp_path / "owner"
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": LOCAL_AGENT_USER_ID,
        "channel": LOCAL_CHAT_CHANNEL, "channel_conversation_id": "display-one", "channel_user_id": LOCAL_AGENT_USER_ID})
    _session(home, session_id="bg-one", thread_id=thread.thread_id)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), conversation_store=store)
    runtime, nav = _open_local_watch(agent)
    path = store.storage.bindings_path
    bindings = json.loads(path.read_text())
    bindings[_binding_key(LOCAL_CHAT_CHANNEL, "display-two", LOCAL_AGENT_USER_ID)] = "invalid/thread"
    path.write_text(json.dumps(bindings), encoding="utf-8")
    with pytest.raises(ValueError):
        store.threads.resolve_report(channel=LOCAL_CHAT_CHANNEL,
            channel_conversation_id="display-two", channel_user_id=LOCAL_AGENT_USER_ID)
    before = _files(tmp_path)
    invalidated = []

    # LLM: 未确认范围不得读取列表；显式失败可防止用全owner空结果掩盖漏接线。
    # 函数用途: 阻止错误路径读取任一线程的观察记录。
    def forbidden_list(*args, **kwargs):
        pytest.fail("未知会话不得读取进程列表")

    monkeypatch.setattr(bg, "list_background_processes", forbidden_list)
    with pytest.raises(ValueError):
        _consume_background_notices(agent, "display-two", runtime,
            [SimpleNamespace(invalidate=lambda: invalidated.append(True))], set(), [0], nav)
    assert nav.snapshot().background_process_count == 0
    assert "bg-one" not in _watch_frame_text(nav) and "line-two" not in _watch_frame_text(nav)
    assert invalidated and nav._background_watch_scope is None
    assert _files(tmp_path) == before


# LLM: 两个入口分别覆盖通知消费和同步自身的解析抛错，异常类型不应决定是否错误保留旧范围。
# 函数用途: 验证OSError与ValueError解析失败均清空旧行和旧日志。
@pytest.mark.parametrize("entry", ["consume", "sync"])
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_local_scope_resolution_exception_clears_previous_watch(tmp_path, monkeypatch, entry, error_type):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts import tui_threading

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    broken = [False]

    # LLM: 初始返回明确会话，随后仅在范围解析接缝抛异常，不改进程reader或导航。
    # 函数用途: 构造一次可信展示后无法确认新范围的确定反例。
    def resolve(**kwargs):
        if broken[0]:
            raise error_type("conversation binding unavailable")
        return SimpleNamespace(thread_id="thread-one"), None

    store = SimpleNamespace(storage=SimpleNamespace(root=home / "conversations"), threads=SimpleNamespace(resolve_report=resolve))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), conversation_store=store)
    runtime, nav = _open_local_watch(agent)
    broken[0] = True
    before = _files(tmp_path)
    if entry == "consume":
        with pytest.raises(error_type):
            tui_threading._consume_background_notices(agent, "display-two", runtime, [None], set(), [0], nav)
    else:
        assert tui_threading._sync_background_watch(agent, nav, session_id="display-two", runtime=runtime)
    assert nav.snapshot().background_process_count == 0
    assert "bg-one" not in _watch_frame_text(nav) and "line-two" not in _watch_frame_text(nav)
    assert nav._background_watch_scope is None
    assert "会话范围未确认" in nav.active_runtime().notice()
    assert _files(tmp_path) == before


# LLM: 范围确认后的普通投影错误不同于解析失败，保留同范围旧帧且只提示，不改变任务或文件。
# 函数用途: 防止修复范围失信时误删同会话有效展示的兼容回归。
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_confirmed_local_scope_projection_failure_keeps_same_thread_frame(tmp_path, monkeypatch, error_type):
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_parts.tui_threading import _sync_background_watch

    home = tmp_path / "owner"
    _session(home, session_id="bg-one", thread_id="thread-one")
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home), thread_id="thread-one")
    runtime, nav = _open_local_watch(agent)
    before = _files(tmp_path)

    # LLM: 明确会话解析保持成功，仅在随后列表读取制造异常。
    # 函数用途: 隔离已确认同范围的普通读取失败。
    def failed_projection(*args, **kwargs):
        raise error_type("process projection unavailable")

    monkeypatch.setattr(bg, "list_background_processes", failed_projection)
    assert _sync_background_watch(agent, nav, session_id="display-one", runtime=runtime)
    assert nav.snapshot().background_process_count == 1 and "line-two" in _watch_frame_text(nav)
    assert "仅保留同会话上次结果" in nav.active_runtime().notice()
    assert nav._background_watch_scope == (str(home), "thread-one")
    assert _files(tmp_path) == before
