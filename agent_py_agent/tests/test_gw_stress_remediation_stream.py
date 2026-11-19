# LLM: X8/C1 回归：冷工具能力探测按完整能力身份（含凭据指纹与失效代次）跨后端实例单飞；
#   SSE 按 HTTPResponse.read1 成块读取仍逐行检查停止与超时，拆行/CRLF/跨块多字节/EOF 尾行与逐行读取一致。
#   C3：失效后旧在途探测的结论只交给本次参与者，只有收尾时仍为当前在途的物理发起者发布共享与实例缓存。
#   R2：真实传输包装上的旧探针拒绝不越过当前 flight 摘除新阳性；定向失效与新阴性撤销同身份驻留实例的阳性。
# 模块用途: 核单飞的结果/失败共享、停止与 TTL 语义、失效后的发布归属，以及块读取与无锁中断判断的等价性。
from __future__ import annotations

import gc
import http.client
import io
import json
import threading
import time
import weakref
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core.native_tool_protocol import (
    ToolProtocolSelectionError,
    select_tool_protocol,
)
from agent_py_agent.agent.backends import gateway_helpers
from agent_py_agent.agent.backends import http as http_backend
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError, ProviderTimeoutError
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.concurrency import interrupt as interrupt_module
from agent_py_agent.agent.concurrency.interrupt import (
    InterruptHandle,
    is_interrupted,
    register_interruptible,
    set_interrupt,
)
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability


def _capability(evidence: str) -> ProviderToolCapability:
    return ProviderToolCapability(provider="openai_chat", endpoint="http://model.invalid/v1", model="m",
                                  stream=True, native_supported=True, evidence=evidence)


def _capture(fn):
    try:
        return fn()
    except BaseException as exc:  # noqa: BLE001 - 测试收集各线程异常
        return exc


# 函数用途: 并发启动 n 个调用者，返回各自结果或异常。
def _run_concurrently(fn, count=8):
    outcomes, barrier = [None] * count, threading.Barrier(count)

    def call(index):
        barrier.wait()
        outcomes[index] = _capture(lambda: fn(index))

    threads = [threading.Thread(target=call, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


# 函数用途: 有界轮询直到条件成立；回归时以失败结束而不是挂住整个测试进程。
def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True

@pytest.fixture(autouse=True)
def _clean_probe_memo():
    http_backend.invalidate_capability_probe_memo(reason="test")
    yield
    http_backend.invalidate_capability_probe_memo(reason="test")


def test_cold_probe_single_flight_shares_one_success():
    calls = []
    capability = _capability("nonce_match")

    def probe():
        calls.append(threading.get_ident())
        time.sleep(0.3)
        return capability

    outcomes = _run_concurrently(lambda _i: http_backend._single_flight_probe(("x8", "success"), probe))
    assert len(calls) == 1
    assert all(outcome is capability for outcome in outcomes)
    assert ("x8", "success") not in http_backend._capability_probe_flights


def test_cold_probe_single_flight_shares_provider_failure_without_caching_it():
    calls = []

    def probe():
        calls.append(1)
        time.sleep(0.3)
        raise ProviderTimeoutError("模型接口等待首个流式事件超时", stage="first_event")

    outcomes = _run_concurrently(lambda _i: http_backend._single_flight_probe(("x8", "failure"), probe))
    assert len(calls) == 1
    assert all(isinstance(outcome, ProviderTimeoutError) and outcome.stage == "first_event" for outcome in outcomes)
    # 失败不缓存：之后的调用重新真实探测。
    with pytest.raises(ProviderTimeoutError):
        http_backend._single_flight_probe(("x8", "failure"), probe)
    assert len(calls) == 2


def test_leader_interrupt_is_not_inherited_by_followers():
    calls, release = [], threading.Event()
    capability = _capability("nonce_match")

    def probe():
        calls.append(1)
        if len(calls) == 1:
            release.wait(5)
            raise InterruptedError("模型接口流式请求已被用户停止")
        return capability

    results = []
    key = ("x8", "intr")
    leader = threading.Thread(target=lambda: results.append(_capture(lambda: http_backend._single_flight_probe(key, probe))))
    leader.start()
    assert _wait_until(lambda: calls), "领头探测未开始"
    follower = threading.Thread(target=lambda: results.append(_capture(lambda: http_backend._single_flight_probe(key, probe))))
    follower.start()
    time.sleep(0.2)
    release.set()
    leader.join(5)
    follower.join(5)
    assert any(isinstance(item, InterruptedError) for item in results)
    assert any(item is capability for item in results)
    assert len(calls) == 2


def test_follower_wait_is_interruptible_and_ttl_bounded(monkeypatch):
    release = threading.Event()
    key = ("x8", "hung")

    def hung_probe():
        release.wait(10)
        return _capability("late")

    leader = threading.Thread(target=lambda: _capture(lambda: http_backend._single_flight_probe(key, hung_probe)))
    leader.start()
    assert _wait_until(lambda: key in getattr(http_backend, "_capability_probe_flights", {})), "领头探测未登记在途"
    caught = []
    follower = threading.Thread(target=lambda: caught.append(_capture(lambda: http_backend._single_flight_probe(key, hung_probe))))
    follower.start()
    time.sleep(0.2)
    set_interrupt(True, follower.ident)
    follower.join(5)
    assert isinstance(caught[0], InterruptedError)
    # TTL 到期后跟随者不再等待挂住的领头者，自己探测。
    monkeypatch.setattr(http_backend, "_PROBE_FLIGHT_TTL_SECONDS", 0.0)
    fresh = _capability("fresh")
    assert http_backend._single_flight_probe(key, lambda: fresh) is fresh
    release.set()
    leader.join(5)


def _backend(api_key: str) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(BackendOptions(
        api_base="http://model.invalid/v1", api_key=api_key, model_name="m", request_timeout=240))


def test_probe_single_flight_spans_backend_instances_but_not_credentials(monkeypatch):
    calls = []

    def fake_probe(self):
        calls.append(self.api_key)
        time.sleep(0.3)
        return _capability(f"probe-{self.api_key}")

    monkeypatch.setattr(OpenAICompatibleBackend, "_probe_tool_capability_uncached", fake_probe)
    shared = [_backend("key-a") for _ in range(6)]
    outcomes = _run_concurrently(lambda index: shared[index].probe_tool_capability(), count=6)
    assert calls == ["key-a"]
    assert {outcome.evidence for outcome in outcomes} == {"probe-key-a"}
    # 不同凭据指纹各自探测，不共享在途结果。
    calls.clear()
    http_backend.invalidate_capability_probe_memo(reason="test")
    mixed = [_backend("key-a"), _backend("key-b")]
    outcomes = _run_concurrently(lambda index: mixed[index].probe_tool_capability(), count=2)
    assert sorted(calls) == ["key-a", "key-b"]
    assert {outcome.evidence for outcome in outcomes} == {"probe-key-a", "probe-key-b"}


def test_invalidation_generation_splits_in_flight_probes(monkeypatch):
    started, release, calls = threading.Event(), threading.Event(), []

    def fake_probe(self):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            release.wait(5)
            return _capability("stale")
        return _capability("fresh")

    monkeypatch.setattr(OpenAICompatibleBackend, "_probe_tool_capability_uncached", fake_probe)
    first, second = _backend("key-a"), _backend("key-a")
    results = {}
    leader = threading.Thread(target=lambda: results.setdefault("first", first.probe_tool_capability()))
    leader.start()
    assert started.wait(5)
    # 全局失效后到达的调用属于新代次，不并入旧的在途探测。
    http_backend.invalidate_capability_probe_memo(reason="test")
    results["second"] = second.probe_tool_capability()
    release.set()
    leader.join(5)
    assert results["second"].evidence == "fresh"
    assert results["first"].evidence == "stale"
    assert len(calls) == 2


# ---- C3：失效后的探测发布归属 ----

def _negative(evidence: str) -> ProviderToolCapability:
    return ProviderToolCapability(provider="openai_chat", endpoint="http://model.invalid/v1", model="m",
                                  stream=True, native_supported=False, evidence=evidence)


# 函数用途: 全局失效；推进代次并清空在途表。
def _invalidate_global(_backend_of_b):
    http_backend.invalidate_capability_probe_memo(reason="test")


# 函数用途: 生产式指定失效（同身份另一实例收到 401）；其它形状的直接调用者键不受影响。
def _invalidate_targeted(backend_of_b):
    adhoc = ("x8", "adhoc")
    http_backend._capability_probe_flights[adhoc] = object()
    try:
        http_backend.invalidate_capability_probe_memo(backend_of_b, reason="http_401")
        assert adhoc in http_backend._capability_probe_flights
    finally:
        http_backend._capability_probe_flights.pop(adhoc, None)


@pytest.mark.parametrize("invalidate", [_invalidate_global, _invalidate_targeted], ids=["global", "targeted"])
@pytest.mark.parametrize("scenario", ["old_positive_after_fresh_negative", "old_error_after_fresh_positive"])
def test_stale_flight_never_publishes_over_fresh_probe(monkeypatch, invalidate, scenario):
    # 读者 A 的旧在途探测被失效；B 是失效后的首个新探测；C 是第三个新实例，只能读到 B 及之后的事实。
    old = _capability("old") if scenario == "old_positive_after_fresh_negative" else \
        ProviderRequestRejectedError("HTTP 401: rejected", status_code=401)
    fresh = _negative("fresh") if scenario == "old_positive_after_fresh_negative" else _capability("fresh")
    third = _capability("third")
    calls, guard, started, release = [], threading.Lock(), threading.Event(), threading.Event()

    def fake_probe(self):
        with guard:
            calls.append(1)
            index = len(calls)
        if index == 1:
            started.set()
            release.wait(5)
        outcome = old if index == 1 else fresh if index == 2 else third
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(OpenAICompatibleBackend, "_probe_tool_capability_uncached", fake_probe)
    reader_a, reader_b, reader_c = _backend("key-a"), _backend("key-a"), _backend("key-a")
    results = {}
    leader = threading.Thread(target=lambda: results.setdefault("A", _capture(reader_a.probe_tool_capability)))
    leader.start()
    assert started.wait(5), "旧在途探测未开始"
    invalidate(reader_b)
    follower = threading.Thread(target=lambda: results.setdefault("B", _capture(reader_b.probe_tool_capability)))
    follower.start()
    # 修复后 B 立即另起物理探测并完成；回归时 B 并入旧 flight，有界等待后再放行旧探测，以失败结束而不挂住。
    _wait_until(lambda: "B" in results, timeout=2.0)
    release.set()
    leader.join(5)
    follower.join(5)
    results["C"] = _capture(reader_c.probe_tool_capability)

    assert results["A"] is old  # 旧结论（含旧错误）只交给本次调用者
    assert reader_a._provider_tool_capability_cache is None
    assert results["B"] is fresh
    assert reader_b._provider_tool_capability_cache is (fresh if fresh.native_supported else None)
    expected = third if scenario == "old_positive_after_fresh_negative" else fresh
    assert results["C"] is expected
    assert len(calls) == (3 if expected is third else 2)
    key = http_backend._capability_probe_key(reader_c)
    with http_backend._capability_probe_lock:
        assert dict(http_backend._capability_probe_entries) == {key: expected}
        assert http_backend._capability_probe_flights == {}
        assert http_backend._logical_probe_identity(key) not in http_backend._capability_probe_miss_reasons
    assert reader_c._provider_tool_capability_cache is expected


# ---- R2-P1-01：真实传输包装上的探针错误归属（RC1）与驻留实例失效（RC2） ----

# 函数用途: 按探针提示里的 nonce 回放 OpenAI 原生工具调用（JSON 或 SSE 行）；native=False 回放无工具调用的正文。
def _probe_reply(request, *, stream: bool, native: bool = True):
    prompt = str(request.payload["messages"][-1]["content"])
    nonce = prompt.split("with nonce ", 1)[1].split(".", 1)[0].strip()
    call = {"id": "call-probe-1", "type": "function",
            "function": {"name": "my_agent_capability_probe", "arguments": json.dumps({"nonce": nonce})}}
    finish = "tool_calls" if native else "stop"
    if stream:
        delta = {"tool_calls": [{"index": 0, **call}]} if native else {"content": "no tool call"}
        return [json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}), "[DONE]"]
    message = ({"role": "assistant", "content": "", "tool_calls": [call]} if native
               else {"role": "assistant", "content": "no tool call"})
    return {"choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}


# 函数用途: 建一个只替换最终传输函数的标准 OpenAI 兼容后端；同凭据即同逻辑身份。
def _wire_backend(*, stream: bool = False, api_key: str = "key-a") -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(BackendOptions(api_base="http://model.invalid/v1", api_key=api_key, model_name="m",
                                                  request_timeout=240, stream_enabled=stream))


def _rejection(status_code: int) -> ProviderRequestRejectedError:
    return ProviderRequestRejectedError(f"HTTP {status_code}: rejected", status_code=status_code)


@pytest.mark.parametrize(("stream", "status_code"), [(False, 401), (True, 403)], ids=["json-401", "stream-403"])
def test_late_probe_rejection_from_real_transport_keeps_fresh_positive(monkeypatch, stream, status_code):
    calls, guard, a_waiting, a_release = [], threading.Lock(), threading.Event(), threading.Event()

    def wire(request):
        with guard:
            calls.append(request.path)
            index = len(calls)
        if index == 1:  # A 的物理探测已发出；旧拒绝时段的应答等 B 的新阳性发布后才迟到返回
            a_waiting.set()
            assert a_release.wait(5)
            raise _rejection(status_code)
        if index == 2:  # 同一实例 A 上另一线程的普通业务请求被拒
            raise _rejection(status_code)
        return _probe_reply(request, stream=stream)

    for name in ("post_stream", "post_stream_iter") if stream else ("post_json",):
        monkeypatch.setattr(http_backend, name, wire)
    reader_a, reader_b, reader_c = (_wire_backend(stream=stream) for _ in range(3))
    key = http_backend._capability_probe_key(reader_a)
    results = {}
    prober = threading.Thread(target=lambda: results.setdefault("A", _capture(reader_a.probe_tool_capability)))
    prober.start()
    try:
        assert a_waiting.wait(5), "A 的物理探测未发出"
        with pytest.raises(ProviderRequestRejectedError):
            if stream:
                list(reader_a.request_stream_iter("/chat/completions", {"messages": []}, {}))
            else:
                reader_a.request_json("/chat/completions", {"messages": []}, {})
        # 普通业务拒绝照旧立即定向失效：摘除 A 的在途 flight 并记原因。
        with http_backend._capability_probe_lock:
            reason = http_backend._capability_probe_miss_reasons[http_backend._logical_probe_identity(key)]
            assert reason == f"http_{status_code}"
            assert not [flight for flight in http_backend._capability_probe_flights if flight[:-1] == key]
        fresh = reader_b.probe_tool_capability()
        assert fresh.native_supported is True
    finally:
        a_release.set()
        prober.join(5)
    assert isinstance(results["A"], ProviderRequestRejectedError)  # 迟到拒绝只交给旧探测的参与者
    assert reader_c.probe_tool_capability() is fresh
    assert len(calls) == 3
    with http_backend._capability_probe_lock:
        assert dict(http_backend._capability_probe_entries) == {key: fresh}
        assert http_backend._capability_probe_flights == {}
    assert reader_b._cached_instance_capability() is fresh


# 函数用途: 计数真实物理探测（仍走原探测实现与最终传输）。
def _count_physical_probes(monkeypatch) -> list[int]:
    original, counted = OpenAICompatibleBackend._probe_tool_capability_uncached, []

    def counting(self):
        counted.append(id(self))
        return original(self)

    monkeypatch.setattr(OpenAICompatibleBackend, "_probe_tool_capability_uncached", counting)
    return counted


# 类用途: 只替换 JSON 最终传输；探针按 nonce 回放（native 可翻转），普通业务请求抛出设定的供应商拒绝。
class _ToolWire:
    def __init__(self) -> None:
        self.native, self.business_error = True, None

    def __call__(self, request):
        if "my_agent_capability_probe" not in json.dumps(request.payload.get("tools") or []):
            raise self.business_error
        return _probe_reply(request, stream=False, native=self.native)


def _run_host(backend: OpenAICompatibleBackend) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(enable_tools=True), backend=backend,
                           _model_call_ledger=ModelCallLedger())


# 函数用途: 三个新 run_id 各走一次真实 run-start 协议选择，返回结构化结果。
def _three_run_starts(host: SimpleNamespace) -> list[str]:
    outcomes = []
    for run_id in ("run-1", "run-2", "run-3"):
        try:
            outcomes.append(select_tool_protocol(host, run_id=run_id).source_protocol)
        except ToolProtocolSelectionError as exc:
            outcomes.append(exc.error_code)
    return outcomes


# 函数用途: B 的普通业务请求遭工具类 400 触发生产定向失效，随后 B 的物理探测确认阴性。
def _reject_tools_and_confirm_negative(wire: _ToolWire, backend: OpenAICompatibleBackend) -> None:
    wire.business_error = ProviderRequestRejectedError(
        "HTTP 400: unsupported tool_choice", status_code=400,
        details={"status_code": 400, "provider_error": {"error": {"code": "unsupported_parameter", "param": "tools"}}})
    with pytest.raises(ProviderRequestRejectedError):
        backend.request_json("/chat/completions", {"messages": [], "tools": [{"name": "t"}]}, {})
    wire.native = False
    assert backend.probe_tool_capability().native_supported is False


def test_resident_sibling_stops_selecting_native_after_targeted_invalidation(monkeypatch):
    wire, probes = _ToolWire(), _count_physical_probes(monkeypatch)
    monkeypatch.setattr(http_backend, "post_json", wire)
    resident_a, resident_b, other = _wire_backend(), _wire_backend(), _wire_backend(api_key="key-b")
    host_a, host_other = _run_host(resident_a), _run_host(other)
    assert select_tool_protocol(host_a, run_id="warm").source_protocol == "native"
    assert resident_b.probe_tool_capability().native_supported is True
    assert select_tool_protocol(host_other, run_id="warm").source_protocol == "native"
    assert len(probes) == 2  # 同身份 B 复用 A 的阳性；不同凭据各探一次
    _reject_tools_and_confirm_negative(wire, resident_b)
    before = len(probes)
    assert _three_run_starts(host_a) == ["TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE"] * 3
    assert len(probes) == before + 3  # 阴性不缓存：每个新 run 都重新物理探测
    # 对照：不同凭据的驻留实例不受撤销影响，继续复用自己的阳性且不发探测。
    assert _three_run_starts(host_other) == ["native"] * 3
    assert len(probes) == before + 3
    with http_backend._capability_probe_lock:
        assert http_backend._capability_probe_flights == {}


def test_preempted_memo_lookup_cannot_install_a_revoked_positive(monkeypatch):
    wire, probes = _ToolWire(), _count_physical_probes(monkeypatch)
    monkeypatch.setattr(http_backend, "post_json", wire)
    seed, resident_b, resident_a = _wire_backend(), _wire_backend(), _wire_backend()
    assert seed.probe_tool_capability().native_supported is True
    original, looked, resume, gate = http_backend._lookup_capability_probe_memo, threading.Event(), threading.Event(), {}

    def preempted_lookup(key):
        found = original(key)
        if threading.current_thread() is gate.get("thread") and not looked.is_set():
            looked.set()  # A 已读到共享阳性、尚未装入实例缓存时被抢占
            assert resume.wait(5)
        return found

    monkeypatch.setattr(http_backend, "_lookup_capability_probe_memo", preempted_lookup)
    host_a, first = _run_host(resident_a), {}
    gate["thread"] = threading.Thread(
        target=lambda: first.setdefault("run-0", _capture(lambda: select_tool_protocol(host_a, run_id="run-0"))))
    gate["thread"].start()
    try:
        assert looked.wait(5), "A 未进入 memo 查询"
        _reject_tools_and_confirm_negative(wire, resident_b)
    finally:
        resume.set()
        gate["thread"].join(5)
    # 迟到的本地安装不能复活旧阳性：被抢占的 run 与之后三个新 run 均得到能力不可用。
    assert isinstance(first["run-0"], ToolProtocolSelectionError)
    assert _three_run_starts(host_a) == ["TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE"] * 3
    assert resident_a._provider_tool_capability_cache is None and len(probes) == 6
    with http_backend._capability_probe_lock:
        assert http_backend._capability_probe_flights == {} and not http_backend._capability_probe_entries


def test_resident_positive_survives_capacity_eviction(monkeypatch):
    wire, probes = _ToolWire(), _count_physical_probes(monkeypatch)
    monkeypatch.setattr(http_backend, "post_json", wire)
    resident = _wire_backend()
    positive = resident.probe_tool_capability()
    key = http_backend._capability_probe_key(resident)
    with http_backend._capability_probe_lock:
        for index in range(256):  # 正常 LRU 容量淘汰不是失效：不撤销驻留实例的阳性
            http_backend._publish_capability_probe_locked(("capacity-filler", index, *key[2:]), positive, None)
        assert key not in http_backend._capability_probe_entries
    assert resident.probe_tool_capability() is positive
    assert _three_run_starts(_run_host(resident)) == ["native"] * 3
    assert len(probes) == 1


def test_capability_validity_tokens_live_only_while_an_instance_holds_them(monkeypatch):
    monkeypatch.setattr(http_backend, "post_json", _ToolWire())
    residents = [_wire_backend(api_key=f"key-{index}") for index in range(3)]
    assert all(resident.probe_tool_capability().native_supported for resident in residents)
    assert len(http_backend._capability_validity_tokens) == 3
    del residents
    gc.collect()
    # 弱值表只索引仍被实例缓存引用的标记：无全量后端清单，也不累计历史失效表。
    assert len(http_backend._capability_validity_tokens) == 0


# ---- C1：SSE 块读取 + 无锁中断判断 ----

# 拆行、CRLF、注释/事件行、跨块多字节字符与无换行的 EOF 尾行。
_SSE_BODY = ('data: {"text":"你好，世界"}\r\n\r\n: keep-alive\n\nevent: x\ndata: second ✓\r\n\r\n'
             "data:  spaced  \n\ndata: [DONE]").encode()


class _LineOnly:
    """没有 read1 的替身：走原逐行迭代。"""

    def __init__(self, body: bytes):
        self._lines = io.BytesIO(body).readlines()

    def __iter__(self):
        return iter(self._lines)


class _Blocks(io.BufferedIOBase):
    """read1 每次交出预定的一块，模拟网络把行、CRLF 与多字节字符切开；声明 BufferedIOBase 合同才走块读。"""

    def __init__(self, blocks):
        super().__init__()
        self.blocks, self.calls = list(blocks), 0

    def read1(self, size=-1):
        self.calls += 1
        return self.blocks.pop(0) if self.blocks else b""


# 函数用途: 给替身装一个有界的 read1：超过 limit 次即抛错，回归时测试失败而不是无界循环吃内存。
def _bounded_read1(calls: list, limit: int = 8):
    def read1(size=-1):
        calls.append(size)
        if len(calls) > limit:
            raise RuntimeError("非流替身的 read1 被无界轮询")
        return MagicMock()
    return read1


class _FakeSock:
    def __init__(self, raw: bytes):
        self._raw = raw

    def makefile(self, *_args, **_kwargs):
        return io.BufferedReader(io.BytesIO(self._raw))


def _chunked_response(body: bytes, size: int) -> http.client.HTTPResponse:
    parts = (body[index:index + size] for index in range(0, len(body), size))
    chunks = b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in parts)
    response = http.client.HTTPResponse(_FakeSock(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
        + chunks + b"0\r\n\r\n"))
    response.begin()
    return response


def _collect(response):
    touched = []
    lines = list(gateway_helpers._iter_sse_data_lines(
        response, timeout=30, url="http://model.invalid", on_data_line=lambda: touched.append(1)))
    return lines, len(touched)


# 函数用途: 在独立线程里跑 fn，避免给 pytest 主线程立中断旗；返回结果或异常。
def _in_worker(fn):
    box = []
    thread = threading.Thread(target=lambda: box.append(_capture(fn)))
    thread.start()
    thread.join(10)
    return box[0]


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64, 65536])
def test_block_reader_matches_line_iteration_across_split_boundaries(size):
    expected = _collect(_LineOnly(_SSE_BODY))
    assert expected == (['{"text":"你好，世界"}', "second ✓", "spaced", "[DONE]"], 4)
    blocks = [_SSE_BODY[index:index + size] for index in range(0, len(_SSE_BODY), size)]
    assert _collect(_Blocks(blocks)) == expected


# 回归：patch urlopen 得到的普通 MagicMock 自带可调用 read1（返回非 bytes 真值）；必须沿原逐行迭代，绝不轮询 read1。
def test_mock_response_without_stream_contract_keeps_line_iteration():
    response, calls = MagicMock(), []
    response.__iter__.return_value = iter([b"data: a\n", b"\n", b"data: [DONE]\n"])
    response.read1.side_effect = _bounded_read1(calls)
    assert _collect(response) == (["a", "[DONE]"], 2)
    assert calls == []


# 声明 HTTPResponse 规格的替身若 read1 交出非 bytes：第一块即 TypeError，不累积、不无界循环。
def test_spec_mock_with_non_bytes_read1_fails_fast():
    response, calls = MagicMock(spec=http.client.HTTPResponse), []
    response.read1.side_effect = _bounded_read1(calls)
    with pytest.raises(TypeError):
        _collect(response)
    assert calls == [gateway_helpers._SSE_READ_BLOCK_BYTES]


@pytest.mark.parametrize("size", [1, 4, 7])
def test_block_reader_uses_decoded_chunked_read1(size, monkeypatch):
    response = _chunked_response(_SSE_BODY, size)
    calls = []
    original = http.client.HTTPResponse.read1
    monkeypatch.setattr(http.client.HTTPResponse, "read1", lambda self, n=-1: calls.append(n) or original(self, n))
    assert _collect(response) == _collect(_LineOnly(_SSE_BODY))
    # 只经已解 chunked 的 read1 成块读取，chunk 框架字节不会混进 SSE 行；读到末块即 EOF。
    assert calls and set(calls) == {gateway_helpers._SSE_READ_BLOCK_BYTES}
    assert response.isclosed()


def test_stop_is_checked_on_every_line_inside_one_block():
    response = _Blocks([b"".join(b"data: %d\n\n" % index for index in range(5))])

    def body():
        lines = gateway_helpers._iter_sse_data_lines(response, timeout=30, url="u", on_data_line=lambda: None)
        first = next(lines)
        set_interrupt(True)
        return first, _capture(lambda: next(lines))

    first, stopped = _in_worker(body)
    assert first == "0" and isinstance(stopped, InterruptedError)
    assert response.calls == 1  # 余下 4 行已在同一块内，仍逐行停下


@pytest.mark.parametrize("cancel_before_register", [True, False])
def test_block_reader_sees_exact_handle_cancel(cancel_before_register):
    response = _Blocks([b"data: a\n\ndata: b\n\n"])
    handle = InterruptHandle()

    def body():
        if cancel_before_register:
            handle.cancel()  # 早于线程登记的取消：登记时投影到本线程
        with register_interruptible("gw-stress-c1", handle=handle):
            lines = gateway_helpers._iter_sse_data_lines(response, timeout=30, url="u", on_data_line=lambda: None)
            seen = [] if cancel_before_register else [next(lines)]
            if not cancel_before_register:
                handle.cancel()
            return seen, _capture(lambda: next(lines))

    seen, stopped = _in_worker(body)
    assert seen == ([] if cancel_before_register else ["a"])
    assert isinstance(stopped, InterruptedError)


def test_block_reader_keeps_first_event_deadline_per_line():
    lines = gateway_helpers._iter_sse_data_lines(
        _Blocks([b": ping\n\ndata: late\n\n"]), timeout=30, url="u", on_data_line=lambda: None,
        initial_deadline=time.monotonic() - 1)
    with pytest.raises(ProviderTimeoutError) as raised:
        next(lines)
    assert raised.value.stage == "first_event"


class _RecordingLock:
    def __init__(self):
        self._lock, self.owners = threading.Lock(), []

    def __enter__(self):
        self.owners.append(threading.get_ident())
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)


def test_is_interrupted_takes_global_lock_only_when_a_flag_exists(monkeypatch):
    recording = _RecordingLock()
    monkeypatch.setattr(interrupt_module, "_lock", recording)
    exited = threading.Thread(target=lambda: None)
    exited.start()
    exited.join(5)

    def body():
        me = threading.get_ident()
        quiet = [is_interrupted() for _ in range(200)]
        quiet_locks = recording.owners.count(me)
        # ident 复用留下的旧旗：必须进锁按原规则核对身份并清除，不能误判为已中断。
        with recording:
            interrupt_module._interrupted_threads[me] = weakref.ref(exited)
        before = recording.owners.count(me)
        stale = is_interrupted()
        stale_locked = recording.owners.count(me) > before
        set_interrupt(True)
        flagged = is_interrupted()
        set_interrupt(False)
        return quiet, quiet_locks, stale, stale_locked, me in interrupt_module._interrupted_threads, flagged

    quiet, quiet_locks, stale, stale_locked, still_flagged, flagged = _in_worker(body)
    assert not any(quiet) and quiet_locks == 0
    assert stale is False and stale_locked
    assert not still_flagged and flagged is True
