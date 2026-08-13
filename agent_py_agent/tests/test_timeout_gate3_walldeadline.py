from __future__ import annotations

"""第1项 B 门槛3 验收测试: 总墙钟 wall_deadline 独立于 idle_deadline。

steward seq 1500 细化要求(门槛3): _iter_sse_data_lines 加 wall_deadline
(=门槛1修正后的 effective 值); data 行只重置 idle_deadline 不动 wall_deadline。
四类场景全覆盖 + 超时后流/任务清理断言。

stage 语义(seq 1513 验收点1): wall_clock 新增真实抛出路径=transport 总墙钟
预算耗尽(持续 data 续命下唯一能掐断的硬顶), 与 tool_model_generation 墙钟
守卫线程同族(总时长超预算)。idle 检查在前保持门槛2 锁定语义(/keepalive
保活空行=stream_idle 不变)。

场景构造:
- /data_stream: 持续 data 行(0.2s 间隔, 长于总预算) -> idle 每行续命,
  wall 在 timeout 处触发 -> wall_clock
- /data_finish: data 行持续但总时长 < 预算 -> 正常收完(预算内长输出)
- /keepalive_forever: 保活空行持续 -> socket 活跃但 data 空闲 -> stream_idle
  (门槛2 已锁定语义; 「静默触发 idle」另一形态=完全静默的 socket read 超时
  -> provider_declared, 由 gate2 /noreply 确定性覆盖, 本文件不重复断言
  该竞态场景)
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.gateway_helpers import (
    GatewayRequest,
    _iter_sse_data_lines,
    post_stream_iter,
)

_DATA_INTERVAL = 0.2  # data 行间隔, 远小于 timeout=1 -> idle 持续续命


class _SSEHandler(BaseHTTPRequestHandler):
    server_broken_pipe = False  # 类级记录: 客户端掐断后服务端写是否被中断

    def do_POST(self) -> None:  # noqa: N802
        # 消费请求体: 不读则 handler 结束时连接上有未读数据,
        # close 时发 RST 而非 FIN(客户端读到 ConnectionResetError 假噪声)
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if self.path == "/data_stream":
            # 持续 data 行: idle 每行重置, 只有 wall_deadline 能掐断
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            end = time.monotonic() + 3
            try:
                n = 0
                while time.monotonic() < end:
                    self.wfile.write(f"data: {{\"n\":{n}}}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    n += 1
                    time.sleep(_DATA_INTERVAL)
            except (BrokenPipeError, ConnectionResetError):
                type(self).server_broken_pipe = True  # 客户端 wall 掐断后的正常噪音
            return
        if self.path == "/data_finish":
            # data 行持续但总时长(0.5s) < 预算(timeout=5) -> 正常收完
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for n in range(3):
                self.wfile.write(f"data: {{\"n\":{n}}}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(_DATA_INTERVAL)
            return
        if self.path == "/keepalive_forever":
            # 保活空行持续: socket 永不超时, data 空闲 -> stream_idle
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"n":0}\n\n')
            self.wfile.flush()
            end = time.monotonic() + 3
            try:
                while time.monotonic() < end:
                    self.wfile.write(b"\n\n")
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path == "/silent_after_first":
            # 首行后完全静默: socket read 超时先于 SSE deadline -> stream_idle
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"n":0}\n\n')
            self.wfile.flush()
            try:
                time.sleep(3)
            finally:
                try:
                    self.send_response(502)
                    self.end_headers()
                except Exception:
                    pass
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:  # 静默
        del args


@pytest.fixture(scope="module")
def sse_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _call_stream(
    sse_server: ThreadingHTTPServer,
    path: str,
    timeout: int = 1,
) -> tuple[float, list[str], ProviderTimeoutError | None]:
    port = int(sse_server.server_address[1])
    request = GatewayRequest(
        api_base=f"http://127.0.0.1:{port}",
        api_key="test-key",
        path=path,
        payload={"model": "evidence"},
        headers={},
        timeout=timeout,
    )
    started = time.monotonic()
    lines: list[str] = []
    exc: ProviderTimeoutError | None = None
    try:
        for line in post_stream_iter(request):
            lines.append(line)
    except ProviderTimeoutError as err:
        exc = err
    return time.monotonic() - started, lines, exc


# ---------------------------------------------------------------- 1. 持续 data 超总预算 -> wall_clock

def test_sustained_data_exceeds_wall_deadline_raises_wall_clock(
    sse_server: ThreadingHTTPServer,
) -> None:
    """持续 data 行续命 idle, 但总墙钟预算耗尽 -> stage=wall_clock。

    门槛2 前该场景永不超时(SSE idle 被 data 无限续命)——门槛3 的墙钟硬顶。
    """
    elapsed, lines, exc = _call_stream(sse_server, "/data_stream", timeout=1)
    assert exc is not None
    assert exc.stage == "wall_clock"
    assert len(lines) >= 2  # 已收多行后才被总墙钟掐断
    assert elapsed < 3.0  # 掐断时间≈timeout=1, 远小于 server 的 3s 窗口
    assert elapsed >= 0.8


def test_wall_deadline_tracks_effective_not_idle_resets(
    sse_server: ThreadingHTTPServer,
) -> None:
    """持续 data 掐断发生在 idle 续命之后: 证明 wall 未被 data 重置(硬顶独立)。"""
    elapsed, _, exc = _call_stream(sse_server, "/data_stream", timeout=2)
    assert exc is not None
    assert exc.stage == "wall_clock"
    assert elapsed >= 1.8  # 2s 预算: data 每 0.2s 续命 idle, 但 wall 在 2s 掐
    assert elapsed < 2.8


# ---------------------------------------------------------------- 2. 预算内长输出正常收完

def test_sustained_data_within_budget_completes(
    sse_server: ThreadingHTTPServer,
) -> None:
    """data 持续但总时长 < 预算 -> 正常收完, 不误掐。"""
    elapsed, lines, exc = _call_stream(sse_server, "/data_finish", timeout=5)
    assert exc is None
    assert len(lines) == 3
    assert elapsed < 1.0


# ---------------------------------------------------------------- 3. 静默/保活仍走 stream_idle(门槛2 语义保持)

def test_keepalive_without_data_still_stream_idle(
    sse_server: ThreadingHTTPServer,
) -> None:
    """保活空行持续: data 空闲 -> stream_idle(与门槛2 /keepalive 语义一致)。"""
    elapsed, _, exc = _call_stream(sse_server, "/keepalive_forever", timeout=1)
    assert exc is not None
    assert exc.stage == "stream_idle"
    assert elapsed < 3.0
    assert elapsed >= 0.8


# ---------------------------------------------------------------- 4. 超时后流/任务清理断言

def test_wall_timeout_closes_stream_no_lingering(
    sse_server: ThreadingHTTPServer,
) -> None:
    """wall 掐断后: 连接被关闭(服务端写中断), 客户端无残留等待。"""
    _SSEHandler.server_broken_pipe = False
    port = int(sse_server.server_address[1])
    request = GatewayRequest(
        api_base=f"http://127.0.0.1:{port}",
        api_key="test-key",
        path="/data_stream",
        payload={"model": "evidence"},
        headers={},
        timeout=1,
    )
    it = post_stream_iter(request)
    exc: ProviderTimeoutError | None = None
    try:
        for _ in it:
            pass
    except ProviderTimeoutError as err:
        exc = err
    assert exc is not None and exc.stage == "wall_clock"
    # 无残留等待: 掐断后 generator 已耗尽, 再次迭代立即返回(不阻塞)
    started = time.monotonic()
    remaining = list(it)
    assert time.monotonic() - started < 0.5
    assert remaining == []
    # 连接确已关闭(steward seq1533-3): 服务端写侧最终断言观察到客户端
    # 掐断(BrokenPipe/ConnectionReset)——有限等待后必须为真, 否则流残留
    deadline = time.monotonic() + 2.0
    while not _SSEHandler.server_broken_pipe and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _SSEHandler.server_broken_pipe  # 最终断言: 连接确已关闭


# ---------------------------------------------------------------- 5. wall_deadline 显式注入单测

def test_wall_deadline_explicit_short_cutoff() -> None:
    """显式注入更短的 wall_deadline: data 续命 idle 后 wall 快速掐断。

    idle 初始与 wall 同基准(steward seq1533-1)后, wall 独立触发的唯一
    场景=idle 已被 data 续命推后——本测试构造 data 行到达续命 idle,
    第 2 行迭代时 wall(100ms) 到期而 idle(5s+) 未到 -> wall_clock。
    """

    class _FakeLine:
        def __init__(self, text: bytes) -> None:
            self.text = text

        def decode(self, *args: object) -> str:
            # 模拟真实网络逐行到达(60ms/行): 第 1 行(0.06s)未到 wall(0.1s)
            # 并续命 idle, 第 2 行(0.12s)时 wall 到期 -> 掐断
            time.sleep(0.06)
            return self.text.decode("utf-8", "replace")

    fake = [_FakeLine(b'data: {"n":0}\n\n') for _ in range(3)]
    wall_deadline = time.monotonic() + 0.1  # 100ms 硬顶, 远小于 timeout=5
    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError) as err:
        list(
            _iter_sse_data_lines(
                fake,
                timeout=5,
                url="http://fake/wall",
                on_data_line=lambda: None,
                wall_deadline=wall_deadline,
            )
        )
    assert err.value.stage == "wall_clock"
    assert time.monotonic() - started < 1.0  # 不等到 idle(5s), 由 wall 快速掐


def test_same_cutoff_silence_idle_wins() -> None:
    """idle 初始与 wall 同基准(同刻到期), 无 data 续命时 idle 检查在前。

    steward seq1533-1「保留同刻 idle 优先」的确定性证明: 同基准下静默/
    保活(非 data)行不续命 idle, 同刻到期由检查顺序(先 idle 后 wall)定
    stage=stream_idle。
    """

    class _FakeLine:
        def __init__(self, text: bytes) -> None:
            self.text = text

        def decode(self, *args: object) -> str:
            time.sleep(0.03)  # 60ms 内第 2 行迭代时 idle/wall 同刻到期
            return self.text.decode("utf-8", "replace")

    fake = [_FakeLine(b"keepalive\n\n") for _ in range(3)]  # 非 data 行, 不续命
    wall_deadline = time.monotonic() + 0.05  # 与 idle 初始同一基准
    with pytest.raises(ProviderTimeoutError) as err:
        list(
            _iter_sse_data_lines(
                fake,
                timeout=5,
                url="http://fake/idle",
                on_data_line=lambda: None,
                wall_deadline=wall_deadline,
            )
        )
    assert err.value.stage == "stream_idle"  # 同刻到期 idle 优先


# ---------------------------------------------------------------- 门槛2 终审补证(seq1613a): 推进时钟证明同一起算点

def test_advancing_clock_proves_idle_uses_wall_origin(monkeypatch) -> None:
    """推进钟证明 idle 初始与 wall_deadline 同一起算点(非进入函数时点独立起算)。

    start=100.0, 显式 wall=105.0; 进入函数后第 1 次检查推进到 105.5:
    - 若 idle 同基准(wall=105): 105.5 > 105 已到期 -> idle 先检查 -> stream_idle
    - 若 idle 独立起算(进入时 100.5+5=105.5): 105.5 未到期 -> 走到 wall 检查
      -> 105.5 > 105 -> wall_clock
    两种实现给出不同 stage, 断言 stream_idle 即证明同基准。
    """
    import agent_py_agent.agent.backends.gateway_helpers as gh

    times = iter([105.5])  # 第 1 次检查(检查顺序 idle 在前)就到期

    def fake_monotonic() -> float:
        return next(times)

    monkeypatch.setattr(gh.time, "monotonic", fake_monotonic)

    class _FakeLine:
        def decode(self, *args: object) -> str:
            return "keepalive\n\n"

    wall_deadline = 100.0 + 5.0  # start=100.0 + offset(5.0)
    with pytest.raises(ProviderTimeoutError) as err:
        list(
            gh._iter_sse_data_lines(
                [_FakeLine()],
                timeout=5,
                url="http://fake/cutoff",
                on_data_line=lambda: None,
                wall_deadline=wall_deadline,
            )
        )
    assert err.value.stage == "stream_idle"  # idle 与 wall 同基准(105)到期


def test_advancing_clock_idle_and_wall_expire_together(monkeypatch) -> None:
    """推进钟越过同基准 deadline: idle(先检查)与 wall 同刻到期 -> idle 赢。

    检查点 106.0 > 105.0(wall 基准): idle 检查在前触发 stream_idle——
    证明 idle 初始 == wall(同一 monotonic 起点)且同刻 idle 优先(seq1513)。
    """
    import agent_py_agent.agent.backends.gateway_helpers as gh

    times = iter([106.0])

    def fake_monotonic() -> float:
        return next(times)

    monkeypatch.setattr(gh.time, "monotonic", fake_monotonic)

    class _FakeLine:
        def decode(self, *args: object) -> str:
            return "keepalive\n\n"

    wall_deadline = 100.0 + 5.0
    with pytest.raises(ProviderTimeoutError) as err:
        list(
            gh._iter_sse_data_lines(
                [_FakeLine()],
                timeout=5,
                url="http://fake/cutoff",
                on_data_line=lambda: None,
                wall_deadline=wall_deadline,
            )
        )
    assert err.value.stage == "stream_idle"  # 同刻到期 idle 优先
