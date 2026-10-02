# LLM: 传输分段计时只在观察者显式开启（provider_attempt_observer(transport_timing=True)）时由 _gateway_request_attempt 创建；
#   只包裹标准库连接已有的步骤（建连、代理隧道、TLS、取响应、读状态行）并读单调时钟，每个包裹先原样执行原步骤、异常原样上抛，
#   成功后才推进阶段，因此不改变发送字节、重试、期限或连接复用。阶段名是封闭集合 TRANSPORT_PHASES，进度事件状态固定为
#   PROGRESS_STATUS；新增阶段或改状态须同步账本（超时阶段推导）、决策结果日志与 test_decision_transport_timing.py。
#   依赖的标准库私有接口（_create_connection、_tunnel、_tunnel_host、response_class、HTTPResponse._read_status）由测试钉住，
#   升级 Python 时缺了会直接变红。目前只有非流式 post_json 会收尾 body_read（见 finish_body_read）。
#   长连接复用（keepalive_transport）时同一条连接会经历多次尝试：每次都从第一次装计时前保存的原方法重新包裹，
#   不叠加旧包裹；复用的连接不再建连，由 skip_connection 把建连/隧道/TLS 记为 0 毫秒并在快照里标 connection_reused。
# 模块用途: 给一次真实 HTTP 尝试记下每个网络阶段花了多久、当前卡在哪个阶段，供决策调用落盘分析"慢在代理还是服务端"。
from __future__ import annotations

import http.client
import time
from collections.abc import Callable

# 按发生顺序：建连（直连含 DNS；走代理时是连到代理）、代理 CONNECT 隧道、TLS 握手、请求写完、收到状态行、读完正文。
# 代理隧道只在 HTTPS 走代理时出现，TLS 只在 HTTPS 时出现。
TRANSPORT_PHASES = ("connect", "proxy_connect", "tls_handshake", "request_send", "first_byte", "body_read")
# 只更新某次尝试计时字段的进度事件状态；账本见到它不改尝试状态、完成时间、活动时间和事件列表。
PROGRESS_STATUS = "progress"
# 连接实例上保存“装计时之前的原方法”的属性名；复用连接每次尝试都从这里重新包裹。
_UNTIMED_ATTR = "_my_agent_untimed_methods"


# LLM: 只在发起请求的 provider 线程里推进（标准库建连、发送与读取都在该线程执行）；advance 只接受"当前阶段结束"，
#   乱序或重复调用静默忽略；publish 由传输层绑定到同一 attempt_id，发布失败由 _emit_provider_attempt 吞掉。
# 类用途: 一次 HTTP 尝试的阶段计时器，记录已完成阶段的毫秒数和当前正在进行的阶段（全部完成后为空串）。
class AttemptTiming:
    # LLM: 时钟默认 monotonic，测试可注入；创建即开始计"建连"阶段，调用方应在即将建连前创建。
    # 函数用途: 从建连阶段开始计时。
    def __init__(self, publish: Callable[[dict], None], clock: Callable[[], float] = time.monotonic) -> None:
        self._publish = publish
        self._clock = clock
        self._mark = clock()
        self._phase = TRANSPORT_PHASES[0]
        self._phase_ms: dict[str, float] = {}
        self._reused = False

    # LLM: finished 必须等于当前阶段才推进；following 为空串表示本次尝试的网络阶段全部完成。
    # 函数用途: 结束当前阶段、记下耗时（毫秒，一位小数）、进入下一阶段，并发布一次进度。
    def advance(self, finished: str, following: str) -> None:
        if self._phase != finished:
            return
        now = self._clock()
        self._phase_ms[finished] = round(max(0.0, now - self._mark) * 1000, 1)
        self._mark, self._phase = now, following
        self._publish(self.snapshot())

    # LLM: 只在本次尝试复用了已建好的长连接时调用（建连阶段还没推进时）；tunnel/tls 取这条连接当初建立时的事实。
    #   被跳过的阶段直接记 0 毫秒（取池与准备连接的本地开销不算网络阶段），从此刻起计“请求写完”，后续由原包裹推进。
    # 函数用途: 把复用连接本次没有发生的建连、代理隧道和 TLS 握手记成 0 毫秒，并标记本次是复用。
    def skip_connection(self, *, tunnel: bool, tls: bool) -> None:
        if self._phase != TRANSPORT_PHASES[0]:
            return
        self._reused = True
        skipped = ("connect",) + (("proxy_connect",) if tunnel else ()) + (("tls_handshake",) if tls else ())
        self._phase_ms.update(dict.fromkeys(skipped, 0.0))
        self._mark, self._phase = self._clock(), "request_send"
        self._publish(self.snapshot())

    # LLM: 快照只含阶段名与毫秒数，不含 URL、请求头或正文，可直接写进调用账本和决策结果日志；
    #   复用长连接的尝试另带 connection_reused=True，新建连接的快照不带这个键（与改动前逐字相同）。
    # 函数用途: 返回当前阶段与已完成阶段耗时的副本。
    def snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {"phase": self._phase, "phase_ms": dict(self._phase_ms)}
        if self._reused:
            snapshot["connection_reused"] = True
        return snapshot


# LLM: 只覆盖读状态行这一步来记首字节；构造器不改（标准库按原签名构造），计时器是每次尝试专属子类的类属性。
#   标准库的 _tunnel 也用 response_class 读代理 CONNECT 回复，此时阶段还是 proxy_connect，advance 按当前阶段忽略；
#   100-continue 之后的状态行同理不再推进。urllib 的 HTTPError 也原样包裹本对象。
# 类用途: 带阶段计时的标准库响应，读到状态行后把尝试推进到"读正文"阶段。
class TimedHTTPResponse(http.client.HTTPResponse):
    attempt_timing: AttemptTiming | None = None

    # LLM: 先完整执行标准库读状态行，异常原样上抛，成功后才推进。
    # 函数用途: 读取状态行后记下首字节阶段结束。
    def _read_status(self):
        status = super()._read_status()
        if self.attempt_timing is not None:
            self.attempt_timing.advance("first_byte", "body_read")
        return status


# LLM: timing 为 None 时不装计时（普通调用零改动；装过计时的复用连接恢复原方法）；否则在实例上包裹建连、隧道、
#   connect 与 getresponse，并把响应类换成本次尝试专属的计时子类（计时器是类属性，构造签名与标准库一致）。
#   原方法在第一次装计时前存进实例（_UNTIMED_ATTR），之后每次都包原方法，复用连接不会层层叠包裹。
#   每个包裹先原样调用原步骤，成功后才推进，异常原样上抛；是否走隧道读标准库在建连前已设置的 _tunnel_host。
# 函数用途: 给一条连接装上本次尝试的阶段计时（HTTPS 另记 TLS 握手）。
def instrument_connection(connection: http.client.HTTPConnection, timing: AttemptTiming | None, *, tls: bool) -> None:
    untimed = connection.__dict__.get(_UNTIMED_ATTR)
    if timing is None:
        if untimed is not None:
            _restore_untimed(connection, untimed)
        return
    if untimed is None:
        untimed = (connection._create_connection, connection._tunnel, connection.connect, connection.getresponse,
                   connection.response_class)
        setattr(connection, _UNTIMED_ATTR, untimed)
    after_tunnel = "tls_handshake" if tls else "request_send"
    create, tunnel, connect, getresponse, _response_class = untimed

    # 函数用途: TCP 连接建立后结束建连阶段；走代理隧道时下一阶段是 CONNECT（参数与标准库 connect 的调用一致）。
    #   新 socket 此刻还没交给 connection.sock：推进时若抛出任何异常（含观察者回调里的 KeyboardInterrupt），先关掉它再上抛，
    #   测量不能泄漏连接。
    def timed_create(address, timeout, source_address):
        sock = create(address, timeout, source_address)
        try:
            timing.advance("connect", "proxy_connect" if connection._tunnel_host else after_tunnel)
        except BaseException:
            sock.close()
            raise
        return sock

    # 函数用途: 代理回复 CONNECT 成功后结束隧道阶段。
    def timed_tunnel() -> None:
        tunnel()
        timing.advance("proxy_connect", after_tunnel)

    # 函数用途: 整个 connect 返回即 TLS 握手完成（仅 HTTPS）。
    def timed_connect() -> None:
        connect()
        if tls:
            timing.advance("tls_handshake", "request_send")

    # 函数用途: urllib 调 getresponse 时请求已写完，结束发送阶段后再等响应。
    def timed_getresponse():
        timing.advance("request_send", "first_byte")
        return getresponse()

    connection._create_connection, connection._tunnel = timed_create, timed_tunnel
    connection.connect, connection.getresponse = timed_connect, timed_getresponse
    connection.response_class = type("TimedHTTPResponse", (TimedHTTPResponse,), {"attempt_timing": timing})


# LLM: 只恢复 instrument_connection 第一次保存的原方法与原响应类，不碰连接状态与 socket。
# 函数用途: 让一条装过计时的复用连接在不计时的尝试里回到标准库原样行为。
def _restore_untimed(connection: http.client.HTTPConnection, untimed: tuple) -> None:
    create, tunnel, connect, getresponse, response_class = untimed
    connection._create_connection, connection._tunnel = create, tunnel
    connection.connect, connection.getresponse = connect, getresponse
    connection.response_class = response_class


# LLM: 只由非流式 post_json 在完整读完正文后调用；读失败不调用（阶段停在 body_read 即表示卡在读正文）。
#   流式调用（post_stream / post_stream_iter）不走这里：今后若对流式调用开启计时，成功的调用也会停在 body_read，
#   须先在流式读取收尾处补一个等价的结束点，否则 body_read 不能当“卡在读正文”解读。
# 函数用途: 记下读完正文，原样返回正文字节。
def finish_body_read(response: object, body: bytes) -> bytes:
    timing = getattr(response, "attempt_timing", None)
    if isinstance(timing, AttemptTiming):
        timing.advance("body_read", "")
    return body


__all__ = ["PROGRESS_STATUS", "TRANSPORT_PHASES", "AttemptTiming", "TimedHTTPResponse", "finish_body_read",
           "instrument_connection"]
