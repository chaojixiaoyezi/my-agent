# LLM: 使用真实渲染入口与有界替身验证缓存和等待合同，不把组件计数外推为整机CPU收益。
# 模块用途: 锁住降低空转唤醒时必须保留的显示、截止时间与通知语义。
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_native_executor_client as executor
from agent_py_agent.agent.tooling import mcp_transport
from agent_py_agent.cli.chat_parts import gateway_client, tui_threading, tui_view
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


# LLM: 真实store发布历史用户块和活动思考，不直接修改reducer或伪造渲染结果。
# 函数用途: 建立可反复触发动画失效的正文控件。
def _view():
    store = TuiStateStore()
    seq = TuiEventSequencer("cpu-regression", clock=lambda: 1.0)
    for index in range(40):
        store.publish(seq.emit("user_message", "completed", f"user-{index}", {"text": "中文👩‍💻 history " * 20}))
    store.publish(seq.emit("thinking_started", "started", "thinking"))
    provider = tui_view.TuiFrameProvider(store, lambda width: TuiRenderContext(width=width))
    return store, seq, provider, tui_view.TuiTranscriptControl(provider)


# LLM: 强制frame失效模拟动画时钟，监控真实摘要及索引入口；宽度改变必须重新排版。
# 函数用途: 历史未变时不再扫描索引或分割用户长正文。
def test_spinner_reuses_user_index_and_summaries(monkeypatch):
    _store, _seq, provider, _control = _view()
    summary = Mock(wraps=tui_view._single_line_user_summary)
    index = Mock(wraps=tui_view._user_turn_line_index)
    monkeypatch.setattr(tui_view, "_single_line_user_summary", summary)
    monkeypatch.setattr(tui_view, "_user_turn_line_index", index)
    first = provider.user_turn_lines(80)
    for _ in range(5):
        provider.invalidate()
        assert provider.user_turn_lines(80) is first
    assert summary.call_count == 40
    assert index.call_count == 1
    provider.user_turn_lines(40)
    assert summary.call_count == 80


# LLM: 从post-mutation状态重复调用真实create_content，H与H-1、选择与显式锚点必须各自失效。
# 函数用途: 粘性顶栏准备正文后同尺寸第二次布局不重复补行或统计消息。
def test_content_memo_skips_repeated_preparation(monkeypatch):
    _store, _seq, _provider, control = _view()
    pad = Mock(wraps=tui_view._pad_transcript_lines)
    visible = Mock(wraps=control._visible_message_block_ids)
    monkeypatch.setattr(tui_view, "_pad_transcript_lines", pad)
    monkeypatch.setattr(control, "_visible_message_block_ids", visible)
    first = control.create_content(80, 10)
    assert control.create_content(80, 10) is first
    assert pad.call_count == visible.call_count == 1
    shorter = control.create_content(80, 9)
    assert shorter is not first
    assert control.create_content(80, 9) is shorter
    control.select_all()
    selected = control.create_content(80, 9)
    assert selected is not shorter
    control.clear_selection()
    assert control.create_content(80, 9) is not selected
    control.restore_reading_anchor(None)
    assert control.create_content(80, 9) is not shorter


# LLM: 单调假时钟只推进显式sleep，不借墙钟抖动掩盖超时越界。
# 类用途: 捕获等待步长与实际累计观察时间。
class _Clock:
    # LLM: 每例独立保存时间，不影响真实服务或全局系统时钟。
    # 函数用途: 初始化可核对的等待轨迹。
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    # LLM: 只推进调用者要求的时长；负值直接失败以暴露边界错误。
    # 函数用途: 替代轮询sleep并记录步长。
    def sleep(self, delay):
        assert delay > 0
        self.sleeps.append(delay)
        self.now += delay


# LLM: 只替换C返回事实和管道线程状态，不启动进程，真实等待函数负责期限与轮询。
# 函数用途: 准备已经开始通信的空管道进程替身。
def _process(monkeypatch, clock, *, done=False):
    process = executor.NativeExecutorProcess.__new__(executor.NativeExecutorProcess)
    process._require_original = lambda: None
    process.args, process.returncode = ["probe"], None
    process._communication_lock = __import__("threading").Lock()
    process._started, process._threads, process._errors = True, [], []
    process.stdout = process.stderr = None
    process._poll = Mock(side_effect=lambda: 0 if done else None)
    monkeypatch.setattr(executor.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(executor.time, "sleep", clock.sleep)
    return process


# LLM: wait和communicate须共享5/10/20/50ms退避，短尾只能睡剩余正预算，RPC参数仍用默认独立预算。
# 函数用途: 检查长等待减少轮询而最后一拍不越过deadline。
@pytest.mark.parametrize("method", ["wait", "communicate"])
def test_executor_bounded_wait_backoff(monkeypatch, method):
    clock = _Clock()
    process = _process(monkeypatch, clock)
    with pytest.raises(subprocess.TimeoutExpired):
        getattr(process, method)(timeout=.1)
    assert clock.sleeps == pytest.approx([.005, .01, .02, .05, .015])
    assert clock.now == pytest.approx(.1)
    assert all(call.args == () and call.kwargs == {} for call in process._poll.call_args_list)


# LLM: timeout=0不允许跳过第一次C查询；通信必须同时确认退出与管道EOF。
# 函数用途: 已退出零超时能成功，没有退出则查询一次后报超时。
@pytest.mark.parametrize("method", ["wait", "communicate"])
@pytest.mark.parametrize("done", [False, True])
def test_executor_zero_timeout_still_polls(monkeypatch, method, done):
    process = _process(monkeypatch, _Clock(), done=done)
    if done:
        assert getattr(process, method)(timeout=0) == (0 if method == "wait" else (None, None))
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            getattr(process, method)(timeout=0)
    process._poll.assert_called_once_with()


# LLM: writer未完成时检查真实MCP超时分类；完成事件必须按剩余3ms而非固定10ms等待。
# 函数用途: 防止发送等待超过极短deadline。
def test_mcp_short_deadline_wait_is_clamped(monkeypatch):
    clock = _Clock()
    writer = mcp_transport.MCPFrameWriter.__new__(mcp_transport.MCPFrameWriter)
    writer.error = None
    writer.complete = SimpleNamespace(wait=lambda timeout: clock.sleep(timeout) or False)
    monkeypatch.setattr(mcp_transport.time, "monotonic", lambda: clock.now)
    with pytest.raises(mcp_transport.MCPError) as caught:
        writer.wait(.003, __import__("threading").Event(), allow_cancelled=True)
    assert caught.value.code == "MCP_TIMEOUT"
    assert clock.sleeps == [.003]


# LLM: 每拍续租但第四拍才有字节；终态第五拍出现，最后仍排空一次同一游标的chunk。
# 函数用途: 租约不重置字节退避、新字节立刻恢复0.1秒，保留终态尾读。
def test_heartbeat_does_not_count_as_chunk_activity(monkeypatch):
    clock = _Clock()
    offsets = iter([0, 0, 0, 10, 10, 20])
    terminal = Mock(side_effect=[{}, {}, {}, {}, {"ok": True}])
    monkeypatch.setattr(gateway_client.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(gateway_client.time, "time", lambda: 0.0)
    monkeypatch.setattr(gateway_client.time, "sleep", clock.sleep)
    monkeypatch.setattr(gateway_client, "_extend_active_request_deadline", lambda *args, deadline: deadline + 1)
    monkeypatch.setattr(gateway_client, "_poll_chunk_file", lambda request: (0, 0, next(offsets)))
    monkeypatch.setattr(gateway_client, "read_gateway_terminal_response_file_when_ready", terminal)
    offset_ref = [0]
    request = gateway_client.GatewayChunkPollRequest(Path("chunks"), Path("terminal"), 10.0, lambda text: None, [0], chunk_offset_ref=offset_ref)
    assert gateway_client.poll_gateway_chunks(request) == {"ok": True}
    assert clock.sleeps == pytest.approx([.15, .225, .25, .1])
    assert offset_ref == [20]


# LLM: 直接运行刷新循环，实际store事件和runtime身份在假时钟等待间切换；不靠常量源码断言。
# 函数用途: 安静期逐级退避，事件和换页恢复快拍，小于250ms的测试覆盖值仍有效。
@pytest.mark.parametrize("base", [.25, .01])
def test_refresh_quiet_backoff_and_event_reset(monkeypatch, base):
    clock = _Clock()
    store = TuiStateStore()
    runtime = SimpleNamespace(store=store, needs_periodic_refresh=lambda: True)
    seq = TuiEventSequencer("refresh-cpu", clock=lambda: clock.now)
    switched = SimpleNamespace(store=TuiStateStore(), needs_periodic_refresh=lambda: True)
    active, waits, reset_delays = [runtime], [], []
    app = SimpleNamespace(invalidate=lambda: None)
    # LLM: 等待中触发一次真实事件并切换runtime，检查下一拍而不是仅检查总次数。
    # 函数用途: 驱动安静、收到事件和切换页面三个阶段。
    def wait(delay):
        waits.append(delay)
        clock.sleep(delay)
        if len(waits) == 1:
            clock.now = 11.0
        elif len(waits) == 3:
            store.publish(seq.emit("user_message", "completed", "new-user", {"text": "new"}))
        elif len(waits) == 4:
            reset_delays.append(delay)
            active[0] = switched
        elif len(waits) == 5:
            reset_delays.append(delay)
        return len(waits) == 6
    monkeypatch.setattr(tui_threading.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(tui_threading, "TUI_REFRESH_INTERVAL_SECONDS", base)
    tui_threading._refresh_loop(SimpleNamespace(wait=wait), [app], runtime, SimpleNamespace(active_runtime=lambda: active[0]))
    assert waits[0] == base
    assert waits[2] == (1.0 if base == .25 else base)
    assert reset_delays == [base, base]


# LLM: 使用真实store订阅入口触发用户提交/停止/reset，动画及通知自身发表不得形成网络轮询风暴。
# 函数用途: 确认交互会唤醒通知等待，前台流式字节不冒充交互。
def test_notice_wake_on_submit_stop_reset_and_navigation():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("notice-wake")
    old_callback = Mock()
    navigation = SimpleNamespace(_lock=__import__("threading").RLock(), _view_change=old_callback)
    wake = tui_threading._NoticeRefreshWake(runtime, navigation)
    seq = TuiEventSequencer("notice-wake", clock=lambda: 1.0)
    runtime.store.publish(seq.emit("user_message", "completed", "new", {"text": "submit"}))
    assert wake.event.is_set()
    wake.event.clear()
    runtime.store.publish(seq.emit("thinking_started", "started", "think"))
    assert not wake.event.is_set()
    runtime.store.publish(seq.emit("turn_interrupt_requested", "started", "interrupt"))
    assert wake.event.is_set()
    wake.event.clear()
    runtime.publish_conversation_reset("fresh-thread")
    assert wake.event.is_set()
    wake.event.clear()
    child = TuiRuntime("child-wake")
    navigation._view_change(child)
    old_callback.assert_called_once_with(child)
    assert wake.event.is_set()
    wake.close()
    assert navigation._view_change is old_callback


# LLM: 运行真实Event等待，stop与业务唤醒彼此独立；短join只检查线程是否立即响应，不测CPU百分比。
# 函数用途: 回归通知退出及慢刷新中途收到事件，不允许等满五秒或一秒。
@pytest.mark.parametrize("stop_only", [False, True])
def test_long_wait_interruptible_by_stop_or_event(stop_only):
    stop = threading.Event()
    waiter = tui_threading._WakeableStop(stop)
    entered, finished = threading.Event(), threading.Event()
    # LLM: 单一测试线程只执行一次长等待，不读业务状态。
    # 函数用途: 让主测试线程在等待开始后制造精确唤醒。
    def run():
        entered.set()
        waiter.wait(5.0)
        finished.set()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert entered.wait(1)
    (stop if stop_only else waiter.event).set()
    try:
        assert finished.wait(.3)
    finally:
        stop.set()
        thread.join(1)
