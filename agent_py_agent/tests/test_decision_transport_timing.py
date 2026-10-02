"""决策调用链路分段计时（B 第 0 步）：各网络阶段耗时与超时所处阶段落盘，超时/失败调用的本地估算输入进 model_usage。

背景（2026-09-28，dsh-9a 分析）：前台超时主要来自连到 Jev 的链路在某些时段变慢；每次调用都经本机代理重新建 TCP、
CONNECT 和 TLS，但阶段耗时只在内存里，分不清慢在代理还是 Jev 服务端；超时调用在 model_usage 里 token 为 0（缺报）。

本文件用本机假 CONNECT 代理、假 TLS 握手（按设定时长等待后原样返回 socket，不加密）和假服务端，
分别给每个阶段注入延迟，断言：阶段耗时与超时阶段正确并写进决策结果日志；估算输入带 unfinished 标记入账且与供应商回报分开；
发送次数不变（零重试、超时后不补发）；未开启计时的调用事件与原先完全相同。不访问任何真实服务。
"""
from __future__ import annotations

import http.client
import json
import select
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.backends import gateway_helpers, keepalive_transport
from agent_py_agent.agent.backends.gateway_helpers import (
    GatewayRequest,
    post_json,
    provider_attempt_observer,
)
from agent_py_agent.agent.backends.transport_timing import (
    AttemptTiming,
    TimedHTTPResponse,
    instrument_connection,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from agent_py_agent.agent.conversation import decision_policy
from agent_py_agent.agent.conversation.decision_audit import decision_usage_summary
from agent_py_agent.agent.conversation.decision_outcome_log import decision_outcome_summary
from agent_py_agent.tests.test_decision_protocol import response as decision_response
from agent_py_agent.tests.test_decision_service_http import configured, decide

PHASES = ("connect", "proxy_connect", "tls_handshake", "request_send", "first_byte", "body_read")
_LOOPBACK = "127.0.0.1,localhost,::1"


# LLM: 测试替身：把两个 socket 之间的一批数据转过去；读到结束或出错返回 False，调用方据此收尾。
# 函数用途: 代理隧道的单向转发一步。
def _forward(source: socket.socket, target: socket.socket) -> bool:
    try:
        data = source.recv(65536)
        if data:
            target.sendall(data)
        return bool(data)
    except OSError:
        return False


# LLM: 测试替身：隧道建立后在两端之间双向转发，任一端关闭即结束；5 秒无数据也结束，防止线程挂住。
# 函数用途: 代理隧道的双向转发循环。
def _pump(client: socket.socket, upstream: socket.socket) -> None:
    while True:
        readable, _, _ = select.select([client, upstream], [], [], 5)
        if not readable or not all(_forward(sock, upstream if sock is client else client) for sock in readable):
            return


# LLM: 测试替身：只认 CONNECT，按设定延迟回 200 后把隧道接到本机假服务端（忽略请求里的目标主机）；记录每次 CONNECT。
# 类用途: 本机假 HTTP 代理。
class _ConnectProxy(socketserver.BaseRequestHandler):
    # 函数用途: 处理一条代理连接。
    def handle(self) -> None:
        state, head = self.server.state, b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        state.connects.append(head.split(b"\r\n", 1)[0].decode("latin-1"))
        state.release.wait(state.proxy_delay)
        with socket.create_connection(("127.0.0.1", state.target_port), timeout=5) as upstream:
            if _forward_reply(self.request):
                _pump(self.request, upstream)


# 函数用途: 回复 CONNECT 成功；客户端已断开时返回 False。
def _forward_reply(client: socket.socket) -> bool:
    try:
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        return True
    except OSError:
        return False


# LLM: 测试替身：读完请求体后按设定延迟才发状态行（首字节），发完头后再按设定延迟发正文；每个请求记一次、结束置 done。
# 类用途: 本机假决策服务端（明文 HTTP，经假代理隧道或直连到达）。
class _SlowTarget(BaseHTTPRequestHandler):
    # 函数用途: 返回设定的 JSON 正文，按阶段注入延迟。
    def do_POST(self) -> None:
        state = self.server.state
        self.rfile.read(int(self.headers["Content-Length"]))
        state.posts.append(self.path)
        try:
            state.release.wait(state.first_byte_delay)
            body = json.dumps(state.body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            state.release.wait(state.body_delay)
            self.wfile.write(body)
        except OSError:
            pass
        finally:
            state.done.set()

    # 函数用途: 关闭默认请求日志。
    def log_message(self, *_args) -> None:
        pass


# LLM: 测试替身：握手处等待设定时长后原样返回 socket（不加密）；只为制造 TLS 阶段的延迟，属性覆盖各 Python 版本构造时读取的字段。
# 类用途: 假 TLS context。
class _SlowHandshake:
    verify_mode = 2
    check_hostname = True
    post_handshake_auth = None

    # 函数用途: 绑定共享状态，握手延迟在调用时读取。
    def __init__(self, state: SimpleNamespace) -> None:
        self._state = state

    # 函数用途: 模拟 TLS 握手耗时。
    def wrap_socket(self, sock, server_hostname=None, **_kwargs):
        self._state.handshakes.append(server_hostname)
        time.sleep(self._state.tls_delay)
        return sock


# 函数用途: 在后台线程启动一个服务并返回线程。
def _serve(server) -> threading.Thread:
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    return worker


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    with decision_policy._LOCK:
        decision_policy._FAILURES.clear()
    yield
    with decision_policy._LOCK:
        decision_policy._FAILURES.clear()


# LLM: 测试夹具：HTTPS 请求经本机假代理和假 TLS 到达假服务端；只改本测试进程的代理环境、https_open 与长连接的 TLS 上下文
#   （决策调用默认复用长连接，新建连接走 keepalive_transport），结束时清空决策连接池并全部回收。
# 函数用途: 提供可按阶段注入延迟的完整链路。
@pytest.fixture
def link(monkeypatch):
    state = SimpleNamespace(proxy_delay=0.0, tls_delay=0.0, first_byte_delay=0.0, body_delay=0.0, connects=[],
                            posts=[], handshakes=[], body={"ok": True}, release=threading.Event(), done=threading.Event())
    target = ThreadingHTTPServer(("127.0.0.1", 0), _SlowTarget)
    proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ConnectProxy)
    proxy.daemon_threads = True
    target.state = proxy.state = state
    state.target_port, state.direct_url = target.server_port, f"http://127.0.0.1:{target.server_port}"
    workers = [_serve(target), _serve(proxy)]
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_address[1]}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, _LOOPBACK)
    context = _SlowHandshake(state)

    # 函数用途: 与产品 https_open 相同，只换成假 TLS context。
    def https_open(handler, req):
        return handler.do_open(gateway_helpers._SplitTimeoutHTTPSConnection, req, context=context,
                               transport_options=handler.transport_options)

    monkeypatch.setattr(gateway_helpers._SplitTimeoutHTTPSHandler, "https_open", https_open)
    monkeypatch.setattr(keepalive_transport, "_tls_context", lambda: context)
    try:
        yield state
    finally:
        keepalive_transport.DECISION_CONNECTION_POOL.close_all()
        state.release.set()
        for server in (target, proxy):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(2)


# 函数用途: 构造与 Jev 后端相同限制的严格请求（零重试、绝对期限、禁止重定向）。
def _request(base: str, seconds: float) -> GatewayRequest:
    return GatewayRequest(api_base=base, api_key="k", path="/v1/systemone", payload={"q": 1},
                          headers={"Content-Type": "application/json"}, timeout=seconds, connect_timeout=seconds,
                          allow_redirects=False, deadline=time.monotonic() + seconds, max_retries=0,
                          max_response_bytes=65536)


# 函数用途: 开启分段计时发一次请求，返回（结果或异常，观察到的事件）。
def _send(base: str, seconds: float, *, timing: bool = True):
    events: list[dict] = []
    with provider_attempt_observer(events.append, transport_timing=timing):
        try:
            return post_json(_request(base, seconds)), events
        except Exception as exc:  # noqa: BLE001 超时用例要拿到异常本身
            return exc, events


# 函数用途: 取最后一个带计时快照的事件里的快照。
def _last_transport(events: list[dict]) -> dict:
    return next(event["transport"] for event in reversed(events) if "transport" in event)


def test_proxied_https_call_times_every_phase_in_order(link):
    link.proxy_delay, link.tls_delay, link.first_byte_delay, link.body_delay = 0.15, 0.1, 0.2, 0.15
    result, events = _send("https://jev.test", 5.0)

    assert result == {"ok": True}
    final = _last_transport(events)
    assert final["phase"] == "" and tuple(final["phase_ms"]) == PHASES
    for phase, delay in (("proxy_connect", 0.15), ("tls_handshake", 0.1), ("first_byte", 0.2), ("body_read", 0.15)):
        assert final["phase_ms"][phase] >= delay * 1000 * 0.9, (phase, final)
    statuses = [event["status"] for event in events]
    assert statuses[0] == "started" and statuses.count("started") == 1 and statuses.count("response_opened") == 1
    assert statuses.count("progress") == len(PHASES)
    assert len({event["attempt_id"] for event in events}) == 1
    assert len(link.connects) == 1 and link.connects[0].startswith("CONNECT jev.test:443 ")
    assert link.handshakes == ["jev.test"] and link.posts == ["/v1/systemone"]


def test_direct_http_call_has_no_proxy_or_tls_phase(link):
    link.first_byte_delay = 0.1
    result, events = _send(link.direct_url, 5.0)

    assert result == {"ok": True}
    final = _last_transport(events)
    assert final["phase"] == "" and tuple(final["phase_ms"]) == ("connect", "request_send", "first_byte", "body_read")
    assert final["phase_ms"]["first_byte"] >= 90
    assert link.connects == [] and link.handshakes == [] and len(link.posts) == 1


def test_stdlib_private_hooks_the_timing_relies_on_still_exist():
    # 计时依赖这些标准库私有接口；升级 Python 后缺了任何一个，这里直接变红，而不是静默丢掉某个阶段。
    for connection in (http.client.HTTPConnection("127.0.0.1", 1), http.client.HTTPSConnection("127.0.0.1", 1)):
        assert callable(connection._create_connection) and callable(connection._tunnel)
        assert hasattr(connection, "_tunnel_host") and connection._tunnel_host is None
        assert connection.response_class is http.client.HTTPResponse
    assert callable(getattr(http.client.HTTPResponse, "_read_status", None))
    assert issubclass(TimedHTTPResponse, http.client.HTTPResponse)


def test_a_timing_failure_right_after_connect_closes_the_new_socket():
    # 函数用途: 观察者回调里冒出的 BaseException（Exception 以外，_emit_provider_attempt 兜不住）。
    def interrupted(_snapshot):
        raise KeyboardInterrupt

    created = SimpleNamespace(closed=False)
    created.close = lambda: setattr(created, "closed", True)
    connection = http.client.HTTPConnection("127.0.0.1", 1)
    connection._create_connection = lambda *_args: created
    instrument_connection(connection, AttemptTiming(interrupted), tls=False)
    with pytest.raises(KeyboardInterrupt):
        connection._create_connection(("127.0.0.1", 1), 1.0, None)
    assert created.closed, "socket 还没交给 connection.sock，推进失败时必须先关掉"


def test_calls_without_the_switch_emit_the_original_events(link):
    result, events = _send("https://jev.test", 5.0, timing=False)

    assert result == {"ok": True}
    assert [event["status"] for event in events] == ["started", "response_opened"]
    assert all("transport" not in event for event in events)


@pytest.mark.parametrize(("phase", "delay_field"), [("proxy_connect", "proxy_delay"), ("tls_handshake", "tls_delay"),
                                                   ("first_byte", "first_byte_delay"), ("body_read", "body_delay")])
def test_timeout_leaves_the_phase_in_progress_and_never_resends(link, phase, delay_field):
    # 期限留足前面各阶段的余量（负载高时也不会提前在别的阶段超时）；假 TLS 是真 sleep，其余延迟超时后立即释放。
    setattr(link, delay_field, 1.6 if phase == "tls_handshake" else 3.0)
    result, events = _send("https://jev.test", 0.8)

    assert isinstance(result, Exception)
    transport = _last_transport(events)
    assert transport["phase"] == phase
    assert tuple(transport["phase_ms"]) == PHASES[:PHASES.index(phase)]
    assert len(link.connects) == 1 and len(link.handshakes) == (0 if phase == "proxy_connect" else 1)
    link.release.set()
    link.done.wait(3)
    assert len(link.posts) == (1 if phase in {"first_byte", "body_read"} else 0), "零重试：超时后不能补发"


# 函数用途: 建一本只含一次调用的账本（固定时钟由测试控制）。
def _ledger(clock: list[float], *, purpose: str = "decision", input_tokens: int = 100) -> ModelCallLedger:
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: clock[0]))
    ledger.started(ModelCallStartedParams(call_id="c1", backend="typesafe_decision", model="jev", input_tokens=input_tokens,
                                          request_id="r1", run_id="run1", metadata={"purpose": purpose}))
    return ledger


# 函数用途: 记一次 HTTP 尝试事件。
def _attempt(ledger: ModelCallLedger, status: str, transport: dict | None = None, call_id: str = "c1") -> None:
    ledger.provider_attempt(ModelCallProviderAttemptParams(call_id=call_id, attempt_id="a1", status=status,
                                                           transport=transport or {}))


def test_progress_events_only_replace_the_attempt_timing():
    clock = [10.0]
    ledger = _ledger(clock)
    _attempt(ledger, "started", {"phase": "connect", "phase_ms": {}})
    before = ledger.records()[0]
    clock[0] = 12.0
    _attempt(ledger, "progress", {"phase": "tls_handshake", "phase_ms": {"connect": 1.0, "proxy_connect": 800.0}})
    after = ledger.records()[0]

    assert after.provider_attempts[0]["transport"]["phase"] == "tls_handshake"
    assert after.provider_attempts[0]["status"] == "started" and "finished_at" not in after.provider_attempts[0]
    assert (after.status, after.events, after.last_activity_at, after.provider_attempt_count) == (
        before.status, before.events, before.last_activity_at, 1)
    ledger.provider_attempt(ModelCallProviderAttemptParams(call_id="c1", attempt_id="unknown", status="progress",
                                                           transport={"phase": "first_byte", "phase_ms": {}}))
    assert ledger.records()[0].provider_attempt_count == 1, "进度事件不能凭空新建尝试"


def test_status_event_without_a_snapshot_keeps_the_last_timing():
    clock = [10.0]
    ledger = _ledger(clock)
    _attempt(ledger, "started", {"phase": "connect", "phase_ms": {}})
    _attempt(ledger, "progress", {"phase": "first_byte", "phase_ms": {"connect": 1.0}})
    _attempt(ledger, "failed")

    attempt = ledger.records()[0].provider_attempts[0]
    assert attempt["status"] == "failed" and attempt["transport"]["phase"] == "first_byte"


def test_timeout_records_the_transport_phase_of_the_last_attempt():
    clock = [10.0]
    ledger = _ledger(clock)
    _attempt(ledger, "started", {"phase": "connect", "phase_ms": {}})
    _attempt(ledger, "progress", {"phase": "proxy_connect", "phase_ms": {"connect": 0.4}})
    record = ledger.timeout(ModelCallTimeoutParams(call_id="c1", timeout_seconds=3.0, timeout_stage="wall_clock"))

    assert record.timeout_transport_phase == "proxy_connect"
    assert record.to_dict()["timeout_transport_phase"] == "proxy_connect"
    untimed = _ledger(clock)
    assert untimed.timeout(ModelCallTimeoutParams(call_id="c1", timeout_seconds=3.0,
                                                  timeout_stage="wall_clock")).timeout_transport_phase == ""


# 函数用途: 取某用途分区的 estimated 桶。
def _estimated(ledger: ModelCallLedger, purpose: str = "decision") -> dict:
    summary = ledger.cumulative_summary(request_id="r1")
    return summary["purpose_breakdown"][purpose]["usage_breakdown"]["estimated"], summary


@pytest.mark.parametrize(("ending", "attempted", "counted"), [
    ("timeout", True, True), ("failed", True, True), ("timeout", False, False), ("finished", True, False)])
def test_unfinished_calls_book_the_local_estimate_apart_from_provider_usage(ending, attempted, counted):
    clock = [10.0]
    ledger = _ledger(clock, input_tokens=321)
    if attempted:
        _attempt(ledger, "started", {"phase": "connect", "phase_ms": {}})
    if ending == "timeout":
        ledger.timeout(ModelCallTimeoutParams(call_id="c1", timeout_seconds=1.0, timeout_stage="wall_clock"))
    elif ending == "failed":
        ledger.failed(ModelCallFailureParams(call_id="c1", error_type="URLError"))
    else:
        ledger.finished(ModelCallFinishParams(call_id="c1", input_tokens=300, output_tokens=5,
                                              provider_usage_reported=True))
    estimated, summary = _estimated(ledger)

    assert (estimated["unfinished_input_tokens"], estimated["unfinished_call_count"]) == ((321, 1) if counted else (0, 0))
    assert summary["usage_breakdown"]["estimated"]["unfinished_input_tokens"] == (321 if counted else 0)
    provider = summary["usage_breakdown"]["provider"]["input_tokens"]
    assert provider == (300 if ending == "finished" else 0), "估算不能冒充供应商回报"


def test_a_late_attempt_after_the_timeout_still_books_the_estimate():
    clock = [10.0]
    ledger = _ledger(clock, input_tokens=50)
    ledger.timeout(ModelCallTimeoutParams(call_id="c1", timeout_seconds=1.0, timeout_stage="wall_clock"))
    assert _estimated(ledger)[0]["unfinished_call_count"] == 0
    _attempt(ledger, "started", {"phase": "connect", "phase_ms": {}})
    assert _estimated(ledger)[0]["unfinished_input_tokens"] == 50


# 函数用途: 以 HTTPS 经假代理配置一个真实决策服务宿主，并补上真实 owner 布局才有的结果日志规范路径。
def _configured(tmp_path, timeout: float):
    host, params, thread, stage = configured(tmp_path, SimpleNamespace(url="https://jev.test"), timeout=timeout)
    host.home_paths.owner_decision_outcomes_jsonl = tmp_path / "data" / "decision" / "outcomes.jsonl"
    return host, params, thread, stage


# 函数用途: 读本 owner 决策结果日志里最近一行。
def _last_row(host) -> dict:
    return decision_outcome_summary(host.home_paths, since=0)["recent"][-1]


def test_successful_decision_persists_every_phase(tmp_path, link):
    link.body = decision_response()
    link.first_byte_delay = 0.1
    host, params, _thread, stage = _configured(tmp_path, timeout=5.0)
    outcome = decide(host, params, stage)

    assert outcome.status == "success"
    transport = _last_row(host)["transport"]
    assert transport["call_status"] == "finished" and transport["timeout_phase"] == ""
    assert transport["estimated_input_tokens"] > 0 and len(transport["attempts"]) == 1
    attempt = transport["attempts"][0]
    assert attempt["status"] == "response_opened" and attempt["phase"] == ""
    assert tuple(attempt["phase_ms"]) == PHASES and attempt["phase_ms"]["first_byte"] >= 90
    assert len(link.connects) == 1 and len(link.posts) == 1


@pytest.mark.parametrize("case", [("proxy_connect", "proxy_delay", 0), ("first_byte", "first_byte_delay", 1)],
                         ids=["proxy_connect", "first_byte"])
def test_timed_out_decision_persists_the_phase_and_books_the_estimate(tmp_path, link, case):
    phase, delay_field, posts = case
    link.body = decision_response()
    setattr(link, delay_field, 3.0)
    host, params, thread, stage = _configured(tmp_path, timeout=1.0)
    outcome = decide(host, params, stage)

    assert outcome.status == "deadline"
    row = _last_row(host)
    transport = row["transport"]
    assert (row["status"], transport["call_status"], transport["timeout_stage"]) == ("deadline", "timed_out", "wall_clock")
    assert transport["timeout_phase"] == phase and transport["attempts"][0]["phase"] == phase
    estimate = transport["estimated_input_tokens"]
    summary = model_call_summary(host, request_id=params.request_id, run_id=params.run_id)
    host.conversation_store.model_usage.append_snapshot_once({"thread_id": thread.thread_id,
        "request_id": params.request_id, "run_id": params.run_id, "task_id": params.task_id, "source": "test",
        "model_calls": summary})
    usage = host.conversation_store.model_usage.summary(thread.thread_id)
    decision = usage["purpose_breakdown"]["decision"]["usage_breakdown"]
    assert (usage["estimated"]["unfinished_input_tokens"], usage["estimated"]["unfinished_call_count"]) == (estimate, 1)
    assert decision["estimated"]["unfinished_input_tokens"] == estimate and decision["provider"]["input_tokens"] == 0
    audit = decision_usage_summary(host.conversation_store, [thread.thread_id], since=0)
    assert (audit["input_tokens_estimated_unfinished"], audit["estimated_unfinished_calls"]) == (estimate, 1)
    assert audit["input_tokens_reported"] == 0
    link.release.set()
    if posts:
        link.done.wait(3)
    time.sleep(0.2)
    assert len(link.connects) == 1 and len(link.posts) == posts, "零重试：超时后不能补发"


def test_calls_that_never_reach_the_network_carry_no_transport(tmp_path, link, monkeypatch):
    from agent_py_agent.agent.conversation import decision_model_call as calls

    # 函数用途: 替身调用边界，在联网前就失败（相当于准入/许可阶段被挡下）。
    def never_sent(*_args, **_kwargs):
        raise TimeoutError("admission")

    host, params, _thread, stage = _configured(tmp_path, timeout=1.0)
    monkeypatch.setattr(calls, "invoke_decision_model_call", never_sent)
    decide(host, params, stage)

    assert "transport" not in _last_row(host)
    assert link.connects == [] and link.posts == []


def test_outcome_rows_without_transport_keep_the_original_recent_shape():
    from agent_py_agent.agent.conversation.decision_outcome_log import _recent_row

    assert set(_recent_row({"point": "p", "status": "off"})) == {
        "created_at", "point", "scope", "mode", "status", "reason", "elapsed_ms", "blocking"}
    assert _recent_row({"point": "p", "transport": {"call_status": "finished"}})["transport"] == {
        "call_status": "finished"}
