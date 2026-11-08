"""R5A-W1：思考尾块刷新不再一窗口一条 Timer 线程（3.14t 短命线程按创建次数涨 RSS），尾块仍按时发布。"""
from __future__ import annotations

import contextvars
import json
import threading
import time

from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter

# 连续流式刷新窗口数：旧实现每个窗口新建一条线程
WINDOW_COUNT = 12
# 单个尾块等待发布的上限（秒）
TAIL_WAIT_SECONDS = 2.0
# 测试用刷新间隔（秒），让多个窗口在短时间内跑完
FLUSH_INTERVAL_SECONDS = 0.02


# 函数用途: 读回 writer 已落盘的 thinking_delta 文本。
def _deltas(writer) -> list[str]:
    if not writer.chunk_path.exists():
        return []
    rows = [json.loads(line) for line in writer.chunk_path.read_text(encoding="utf-8").splitlines()]
    return [row["text"] for row in rows if row.get("kind") == "thinking_delta"]


# 函数用途: 等到指定文本随 thinking_delta 落盘或超时。
def _wait_text(writer, text: str) -> bool:
    deadline = time.monotonic() + TAIL_WAIT_SECONDS
    while time.monotonic() < deadline:
        if text in "".join(_deltas(writer)):
            return True
        time.sleep(0.005)
    return False


# 函数用途: 连续多个刷新窗口的尾块都靠延时刷新发布，且整个过程最多新起一条线程（共享延时线程首次懒启动）。
def test_tail_flushes_publish_without_a_thread_per_window(tmp_path, monkeypatch):
    starts = []
    original_start = threading.Thread.start

    def counting_start(self, *args, **kwargs):
        starts.append(self.name)
        return original_start(self, *args, **kwargs)

    writer = BufferedChunkStreamWriter(tmp_path / "thinking.jsonl", rich_transcript=True, delivery_channel="feishu")
    writer.thinking_flush_interval_seconds = FLUSH_INTERVAL_SECONDS
    monkeypatch.setattr(threading.Thread, "start", counting_start)
    try:
        for window in range(WINDOW_COUNT):
            writer.write_thinking_delta(f"窗口{window}首块")
            writer.write_thinking_delta(f"窗口{window}尾块")
            assert _wait_text(writer, f"窗口{window}尾块"), f"窗口{window}的尾块必须在没有新增量时按间隔发布"
    finally:
        monkeypatch.undo()
        writer.close()
    assert "".join(_deltas(writer)) == "".join(f"窗口{index}首块窗口{index}尾块" for index in range(WINDOW_COUNT))
    assert len(starts) <= 1, f"每个刷新窗口不得新建线程，实际新建 {len(starts)} 条：{starts[:4]}"


# 函数用途: 边界刷新先于延时到点时，被取消的延时刷新不得重复发布同一尾块。
def test_boundary_flush_cancels_the_pending_tail_flush(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / "thinking.jsonl", rich_transcript=True, delivery_channel="feishu")
    writer.thinking_flush_interval_seconds = FLUSH_INTERVAL_SECONDS
    writer.write_thinking_delta("首块")
    writer.write_thinking_delta("尾块")
    writer.flush()
    time.sleep(FLUSH_INTERVAL_SECONDS * 5)
    writer.close()
    assert _deltas(writer) == ["首块", "尾块"]


# 函数用途: 共享延时线程按到期顺序执行、取消即不执行、回调异常不终止线程，回调在登记时的 context 里运行。
def test_shared_deferred_calls_order_cancel_errors_and_context():
    from agent_py_agent.agent.concurrency.daemon_executor import DeferredCall, SharedDeferredCalls

    calls = SharedDeferredCalls("r5a-w1-test-timer")
    marker = contextvars.ContextVar("r5a_w1_marker", default="unset")
    seen, done = [], threading.Event()
    token = marker.set("scheduled")
    try:
        late = DeferredCall(lambda: (seen.append(("late", marker.get(), threading.current_thread().name)), done.set()))
        early = DeferredCall(lambda: seen.append(("early", marker.get(), threading.current_thread().name)))
        cancelled = DeferredCall(lambda: seen.append(("cancelled", "", "")))
        failing = DeferredCall(lambda: 1 / 0)
    finally:
        marker.reset(token)
    calls.start(late, 0.06)
    calls.start(cancelled, 0.02)
    calls.start(failing, 0.01)
    calls.start(early, 0.03)
    cancelled.cancel()
    assert done.wait(TAIL_WAIT_SECONDS)
    assert [item[0] for item in seen] == ["early", "late"]
    assert all(item[1] == "scheduled" and item[2] == "r5a-w1-test-timer" for item in seen)
