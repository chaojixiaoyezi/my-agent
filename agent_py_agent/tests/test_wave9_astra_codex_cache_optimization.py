# LLM: Exercise the real WS transport and Responses backend; only sockets/credentials are fixtures, never live providers.
# 模块用途: 钉住会话连接复用、逐请求租约、增量回退、隔离与缓存诊断。
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosedError

from agent_py_agent.agent.backends import responses_websocket as ws
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest, provider_attempt_observer
from agent_py_agent.agent.backends.provider_headers import provider_session_scope, validate_headers
from agent_py_agent.agent.backends.request_scope import provider_endpoint_counts
from agent_py_agent.agent.backends.responses_wire import collect_response

USER = {"role": "user", "content": "你好"}
CALL = {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": json.dumps({"path": "a"}, sort_keys=True)}
RESULT = {"type": "function_call_output", "call_id": "c1", "output": "content"}


# LLM: Each plan starts only on send, so recv(0) genuinely probes unread frames rather than a future reply.
# 类用途: 模拟可跨多次请求存活、能注入关闭和坏帧的连接。
class Socket:
    # LLM: Plans are per-response events, not global state; headers model the real handshake response.
    # 函数用途: 初始化脚本化连接及握手头。
    def __init__(self, plans, state="node-a"):
        self.plans, self.messages, self.sent = list(plans), [], []
        self.closed = False
        self.response = SimpleNamespace(headers={"x-codex-turn-state": state})

    # LLM: Preserve raw serialized bytes for cache-prefix assertions and don't auto-complete malformed plans.
    # 函数用途: 记录首包，并激活本次服务端事件。
    def send(self, text):
        if self.closed:
            raise ConnectionClosedError(None, None)
        self.sent.append(text)
        self.messages.extend(self.plans.pop(0))

    # LLM: Zero-timeout checks never wait; exceptions are injected at the exact recv boundary.
    # 函数用途: 返回一帧、脚本异常或无数据超时。
    def recv(self, timeout=None):
        if self.closed:
            raise ConnectionClosedError(None, None)
        if not self.messages:
            raise TimeoutError()
        value = self.messages.pop(0)
        if isinstance(value, BaseException):
            raise value
        return json.dumps(value)

    # LLM: Immediate shutdown doubles as ordinary close; transport must not depend on GC.
    # 函数用途: 记录物理连接关闭。
    def close_socket(self):
        self.closed = True

    # LLM: Both close paths are observable without opening real sockets.
    # 函数用途: 记录正常连接关闭。
    def close(self):
        self.closed = True


# LLM: Complete events include stable IDs and original provider usage; done-only output models Codex's empty terminal output.
# 函数用途: 生成一个有明确终态的服务端响应。
def complete(response_id="r1", output=None):
    return [{"type": "response.completed", "response": {
        "id": response_id, "status": "completed", "output": output or [],
        "usage": {"input_tokens": 100, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 75}},
    }}]


# LLM: Transport inputs retain native GatewayRequest constraints including physical limit=1.
# 函数用途: 构造同源可比较请求，不填任何真实凭据。
def request(items=None, **changes):
    req = GatewayRequest(api_base="https://fixture.invalid/codex", path="/responses", api_key="fixture-key",
                         headers={"Authorization": "Bearer fixture-key"}, timeout=1,
                         payload={"model": "m", "input": items or [USER], "prompt_cache_key": "stable", "stream": True},
                         provider_max_concurrent_requests=1)
    return replace(req, **changes)


# LLM: Production global pool is replaced only once it exists; the same tests can run red against the old transport.
# 函数用途: 避免测试间复用连接，不影响基线失败断言。
@pytest.fixture(autouse=True)
def isolated_pool(monkeypatch):
    pool = getattr(ws, "_POOL", None)
    if pool is not None:
        fresh = type(pool)()
        monkeypatch.setattr(ws, "_POOL", fresh)
        yield
        fresh.close()
    else:
        yield


# LLM: Real collector stops at terminal without exhausting the generator; release/return must happen before terminal yield.
# 函数用途: 完整消费一次请求，并显式关闭流。
def run(req):
    stream = ws.iter_responses_websocket(req)
    try:
        return collect_response(stream, None, None)
    finally:
        stream.close()


# LLM: A true tool loop replays the prior assistant function call before its tool result, not only the previous user input.
# 函数用途: 钉住三步同连接增量链、完整工具历史与每步租约及时归还。
def test_tool_loop_reuses_socket_and_delta_without_idle_lease(monkeypatch):
    conn = Socket([complete(output=[CALL]), complete("r2"), complete("r3")])
    opens = []
    monkeypatch.setattr(ws, "_connect", lambda req: opens.append(req) or conn)
    req = request()
    with provider_session_scope(("local", "owner-a"), "thread-a"):
        first = run(req)
        assert not conn.closed, "clean completed socket must remain idle"
        assert provider_endpoint_counts(req) == (0, 0)
        second = run(request([USER, CALL, RESULT]))
        assert provider_endpoint_counts(req) == (0, 0)
        third = run(request([USER, CALL, RESULT, {"role": "user", "content": "继续"}]))
    assert len(opens) == 1
    assert json.loads(conn.sent[1])["input"] == [RESULT]
    assert json.loads(conn.sent[1])["previous_response_id"] == "r1"
    assert json.loads(conn.sent[2])["previous_response_id"] == "r2"
    assert first["usage"]["ws_connection_reused"] is False
    assert second["usage"]["ws_connection_reused"] is True
    assert second["usage"]["incremental_chain_hit"] is True
    assert third["usage"]["cache_hit_rate"] == .75
    assert third["usage"]["uncached_input_tokens"] == 25
    assert third["usage"]["cached_tokens"] == 75


# LLM: Rejection must be handled before collector sees the terminal error; full-input retry happens once on a new socket.
# 函数用途: 钉住previous ID失效和复用连接断开的一次全量回退。
@pytest.mark.parametrize("failure", [
    {"type": "error", "code": "previous_response_not_found"},
    {"type": "response.failed", "response": {"error": {"code": "invalid_previous_response_id"}}},
    ConnectionClosedError(None, None),
])
def test_reuse_failure_retries_once_with_full_input(monkeypatch, failure):
    old = Socket([complete(), [failure]])
    fresh = Socket([complete("fresh")], state="node-b")
    sockets, opened = iter([old, fresh]), []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or next(sockets))
    full = [USER, {"role": "user", "content": "next"}]
    with provider_session_scope(("local", "owner-a"), "thread-a"):
        run(request())
        result = run(request(full))
    assert len(opened) == 2 and old.closed and not fresh.closed
    sent = json.loads(fresh.sent[0])
    assert sent["input"] == full and "previous_response_id" not in sent
    assert sent["prompt_cache_key"] == "stable"
    assert opened[1].headers.get("x-codex-turn-state") == "node-a"
    assert result["usage"]["incremental_chain_hit"] is False


# LLM: Different caller identity may never reuse a socket even when endpoint/model/body match.
# 函数用途: 验证凭据、宿主owner和session三个隔离边界。
@pytest.mark.parametrize("change", ["credential", "session", "owner"])
def test_cross_identity_isolation(monkeypatch, change):
    opened = []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(Socket([complete()])) or opened[-1])
    with provider_session_scope(("local", "a"), "one"):
        run(request())
    owner, thread = (("local", "b") if change == "owner" else ("local", "a")), ("two" if change == "session" else "one")
    req = request(api_key="other", headers={"Authorization": "Bearer other"}) if change == "credential" else request()
    with provider_session_scope(owner, thread):
        run(req)
    assert len(opened) == 2
    assert all(not conn.closed for conn in opened)


# LLM: Prefix changes (including compaction) must never anchor to the previous response even on a healthy pooled socket.
# 函数用途: 确认历史前缀或模型配置改变后发送全量，仍保留当前会话亲和。
@pytest.mark.parametrize("items", [[{"type": "compaction", "encrypted_content": "compact"}], [{"role": "user", "content": "reset"}]])
def test_prefix_change_invalidates_incremental_chain(monkeypatch, items):
    conn = Socket([complete(), complete("r2")])
    monkeypatch.setattr(ws, "_connect", lambda req: Socket([complete("fallback")]) if conn.closed else conn)
    with provider_session_scope(("local", "a"), "one"):
        run(request())
        run(request(items))
    assert not conn.closed
    assert json.loads(conn.sent[1])["input"] == items
    assert "previous_response_id" not in json.loads(conn.sent[1])


# LLM: Custom header validation is the authenticated host's protection, independent of pool implementation.
# 函数用途: 禁止大小写变体伪造路由状态。
@pytest.mark.parametrize("header", ["x-codex-turn-state", "X-Codex-Turn-State"])
def test_turn_state_cannot_be_spoofed(header):
    with pytest.raises(ValueError, match="保护字段"):
        validate_headers({header: "attacker"})


# LLM: Cache diagnostics belong to actual WS requests, not only the HTTP code path; no raw body may appear.
# 函数用途: 验证WebSocket物理请求也发布已有request_surface诊断。
def test_ws_attempt_exposes_request_surface(monkeypatch):
    monkeypatch.setattr(ws, "_connect", lambda req: Socket([complete()]))
    observed = []
    with provider_attempt_observer(observed.append, cache_diagnostics=True):
        run(request())
    started = next(row for row in observed if row["status"] == "started")
    assert started.get("request_surface", {}).get("schema") == "request_surface.v2"
    assert "你好" not in json.dumps(observed, ensure_ascii=False)


# LLM: The existing parser's error contract is unchanged; transport must discard all non-completed exits and not hold a lease.
# 函数用途: 钉住脏连接、提前关闭、失败终态及多余帧都不能回池。
@pytest.mark.parametrize("bad", [
    [{"type": "response.output_text.delta", "delta": "partial"}, ConnectionClosedError(None, None)],
    [{"type": "response.failed", "response": {"error": {"code": "server_error"}}}],
    [{"type": "response.incomplete", "response": {"status": "incomplete"}}],
    ["malformed"],
    complete() + [{"type": "response.output_text.delta", "delta": "unread"}],
])
def test_dirty_exits_never_return_socket_to_pool(monkeypatch, bad):
    conn = Socket([bad])
    monkeypatch.setattr(ws, "_connect", lambda req: conn)
    req = request()
    with provider_session_scope(("local", "a"), "one"):
        try:
            run(req)
        except Exception:
            pass
    assert conn.closed
    assert provider_endpoint_counts(req) == (0, 0)


# LLM: Closing a suspended generator represents collector cancellation, distinct from protocol completion.
# 函数用途: 确认显式取消会关连接、清租约且不缓存半截历史。
def test_generator_cancel_discards_connection(monkeypatch):
    conn = Socket([[{"type": "response.created", "response": {"id": "partial"}}]])
    monkeypatch.setattr(ws, "_connect", lambda req: conn)
    with provider_session_scope(("local", "a"), "one"):
        lines = ws.iter_responses_websocket(request())
        next(lines)
        lines.close()
    assert conn.closed and provider_endpoint_counts(request()) == (0, 0)


# LLM: 同模型instructions/tools变化在原连接上发全量；切模型必须另用连接，两者都不能继承旧响应ID。
# 函数用途: 确认非input配置变化失效增量链，模型变化额外隔离物理连接。
@pytest.mark.parametrize("change", [{"model": "new-model"}, {"instructions": "new-system"}, {"tools": []}])
def test_option_changes_send_full_input(monkeypatch, change):
    conn = Socket([complete(), complete("r2")])
    fresh = Socket([complete("new-model")])
    opened = []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or (conn if len(opened) == 1 else fresh))
    with provider_session_scope(("local", "a"), "one"):
        run(request())
        req = request([USER, RESULT])
        run(replace(req, payload={**req.payload, **change}))
    assert not conn.closed
    if "model" in change:
        assert len(opened) == 2 and not fresh.closed
        sent = json.loads(fresh.sent[0])
    else:
        assert len(opened) == 1
        sent = json.loads(conn.sent[1])
    assert "previous_response_id" not in sent and sent["input"] == [USER, RESULT]


# LLM: Verify public configuration propagation through real factory; never use setattr as a substitute for a declared key.
# 函数用途: 钉住TTL/容量可配置且默认值随包一致。
def test_pool_config_reaches_backend():
    from pathlib import Path

    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.settings.config import AgentConfig, load_config

    config = AgentConfig()
    assert getattr(config, "responses_ws_idle_ttl_seconds", None) == 300
    assert getattr(config, "responses_ws_max_idle_connections", None) == 32
    config = AgentConfig(model_backend="openai_responses", api_base="https://fixture.invalid", model_name="m",
                         responses_ws_idle_ttl_seconds=17, responses_ws_max_idle_connections=2)
    backend = get_backend("openai_responses", config)
    assert backend.responses_ws_idle_ttl_seconds == 17
    assert backend.responses_ws_max_idle_connections == 2
    shipped = load_config(Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml")
    assert shipped.responses_ws_idle_ttl_seconds == 300 and shipped.responses_ws_max_idle_connections == 32


# LLM: Bounded pools must close LRU victims and expire actual descriptors even when no subsequent request arrives.
# 函数用途: 验证有界容量淘汰和空闲TTL主动关闭。
def test_pool_lru_and_idle_expiry(monkeypatch):
    import time

    pool = getattr(ws, "_POOL", None)
    assert pool is not None, "session pool is missing"
    opened = []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(Socket([complete()])) or opened[-1])
    for thread in ("one", "two", "three"):
        with provider_session_scope(("local", "a"), thread):
            lines = ws.iter_responses_websocket(request(), idle_ttl_seconds=.05, max_idle=2)
            list(lines)
    assert opened[0].closed
    end = time.monotonic() + 1
    while time.monotonic() < end and not all(c.closed for c in opened):
        time.sleep(.01)
    assert all(c.closed for c in opened), "idle TTL must close without another request"


# LLM: Exercise HTTP via the real Responses backend, not a new parallel HTTP client; other protocols must not capture these headers.
# 函数用途: 钉住HTTP响应头亲和回放、会话reset清理和Responses用量字段。
def test_http_responses_sticky_echo_and_reset(tmp_path, monkeypatch):
    import io

    from agent_py_agent.agent.backends import gateway_helpers as gateway
    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.settings.config import AgentConfig

    sent = []

    # LLM: The fixture supplies only the HTTP boundary; request assembly, response JSON parsing and usage projection remain real.
    # 函数用途: 模拟带路由头的真实JSON响应。
    def opened(req, envelope, **kwargs):
        sent.append(dict(req.header_items()))
        body = io.BytesIO(json.dumps(complete()[0]["response"]).encode())
        body.headers = {"x-codex-turn-state": "http-node"}
        body.status = 200
        return body

    monkeypatch.setattr(gateway, "_gateway_urlopen", opened)
    backend = get_backend("openai_responses", AgentConfig(model_name="m", api_base="https://fixture.invalid", api_key="fixture",
                                                          stream_enabled=False))
    with provider_session_scope(("local", "a"), "one"):
        first = backend.generate("hello")
        second = backend.generate("hello again")
        assert any(k.lower() == "x-codex-turn-state" and v == "http-node" for k, v in sent[1].items())
        assert not any(k.lower() == "x-codex-turn-state" for k in sent[0])
        assert second.usage["cached_tokens"] == 75 and first.usage["cache_hit_rate"] == .75
        ws.reset_responses_session()
        backend.generate("after reset")
        assert not any(k.lower() == "x-codex-turn-state" for k in sent[2])
    with provider_session_scope(("local", "a"), "new-thread"):
        backend.generate("new session")
    assert not any(k.lower() == "x-codex-turn-state" for k in sent[3])


# LLM: Real generate must reuse projected reasoning/message/tool IR, including done-only output from the production Codex stream.
# 函数用途: 经完整generate链证明增量前缀不会漏掉上一轮assistant工具调用。
def test_real_oauth_generate_reuses_tool_chain(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.settings.model_profiles import (
        execute_model_profile_operation,
        selected_model_config,
    )
    from agent_py_agent.tests.test_model_oauth import host, login, setup

    alice = host(tmp_path)
    key = setup(alice, "chatgpt")
    login(alice, monkeypatch)
    execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    backend = get_backend("openai_responses", selected_model_config(alice))
    conn = Socket([[{"type": "response.output_item.done", "item": CALL, "output_index": 0}, *complete()], complete("r2")])
    opened = []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or conn)
    with provider_session_scope(("local", "a"), "one"):
        response = backend.generate("hello")
        backend.generate("hello", messages=[{"role": "assistant", "content": response.assistant_content_blocks},
                                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "content"}]}])
    assert len(opened) == 1
    assert json.loads(conn.sent[1])["previous_response_id"] == "r1"
    assert json.loads(conn.sent[1])["input"] == [RESULT]


# LLM: Replay after visible output could duplicate displayed/tool events; it must remain a failure, not become an optimistic recovery.
# 函数用途: 确保复用连接在交出partial后断开也不透明重放。
def test_no_replay_after_partial_output(monkeypatch):
    conn = Socket([complete(), [{"type": "response.output_text.delta", "delta": "partial"}, ConnectionClosedError(None, None)]])
    opened = []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or conn)
    with provider_session_scope(("local", "a"), "one"):
        run(request())
        with pytest.raises(Exception, match="回复完成前断开"):
            run(request([USER, RESULT]))
    assert len(opened) == 1 and conn.closed
    assert provider_endpoint_counts(request()) == (0, 0)


# LLM: A reset invalidates an active borrowed state even if completion races afterwards; it must not reenter the idle pool.
# 函数用途: 验证reset与在途完成交错时不会复活旧头或连接。
def test_reset_revokes_inflight_pool_return(monkeypatch):
    conn = Socket([[{"type": "response.created", "response": {}}, *complete()]])
    monkeypatch.setattr(ws, "_connect", lambda req: conn)
    assert callable(getattr(ws, "reset_responses_session", None))
    with provider_session_scope(("local", "a"), "one"):
        lines = ws.iter_responses_websocket(request())
        next(lines)
        ws.reset_responses_session()
        list(lines)
    assert conn.closed and provider_endpoint_counts(request()) == (0, 0)


# LLM: Invalid settings must normalize at the loader, not crash during a later provider request or create unbounded resources.
# 函数用途: 钉住池配置的非法值告警回默认及字符串数字归一。
@pytest.mark.parametrize("raw, expected", [("bad", 300), (-1, 300), ("17", 17)])
def test_ttl_config_normalization(tmp_path, raw, expected):
    from agent_py_agent.agent.settings.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(f"responses_ws_idle_ttl_seconds: {raw}\nresponses_ws_max_idle_connections: 2\n")
    config = load_config(path)
    assert type(config.responses_ws_idle_ttl_seconds) is int
    assert config.responses_ws_idle_ttl_seconds == expected
    assert config.responses_ws_max_idle_connections == 2


# LLM: A successful compaction must clear the old anchor immediately, not only when a later prefix comparison happens to fail.
# 函数用途: 核实compaction响应即清空旧ID和输入锚点。
def test_compaction_clears_existing_connection_anchor(monkeypatch):
    from agent_py_agent.agent.backends.responses_ws_pool import session_pool_key

    conn = Socket([complete(), complete("compact", [{"type": "compaction", "encrypted_content": "compact"}])])
    monkeypatch.setattr(ws, "_connect", lambda req: conn)
    with provider_session_scope(("local", "a"), "one"):
        run(request())
        run(request([USER, {"type": "compaction_trigger"}]))
        entry = ws._POOL.take(session_pool_key(request()))
        try:
            assert entry._ws_last_response_id == "" and entry._ws_last_items == b""
        finally:
            entry.discard()


# LLM: A popped socket must remain protected even if request serialization fails before a provider lease was acquired.
# 函数用途: 复现并守住独立评审发现的出池后准备失败泄漏。
def test_prepare_failure_discards_borrowed_socket(monkeypatch):
    conn = Socket([complete()])
    monkeypatch.setattr(ws, "_connect", lambda req: conn)
    with provider_session_scope(("local", "a"), "one"):
        run(request())
        invalid = replace(request(), payload={**request().payload, "unsupported": object()})
        with pytest.raises(TypeError):
            run(invalid)
    assert conn.closed and provider_endpoint_counts(request()) == (0, 0)


# LLM: Fault injection follows successful completion; any exception before terminal delivery must revoke the clean-pool decision.
# 函数用途: 复现终态后处理失败仍错误回池。
def test_post_completion_failure_discards_socket(monkeypatch):
    conn = Socket([complete()])
    monkeypatch.setattr(ws, "_connect", lambda req: conn)

    # LLM: Injectable helper models allocation failure/exception after the response has been marked complete.
    # 函数用途: 在终态后处理点抛出确定异常。
    def fail(*args, **kwargs):
        raise RuntimeError("telemetry failed")

    monkeypatch.setattr(ws, "cache_usage", fail)
    with provider_session_scope(("local", "a"), "one"), pytest.raises(RuntimeError, match="telemetry failed"):
        run(request())
    assert conn.closed and provider_endpoint_counts(request()) == (0, 0)


# LLM: Refreshing HTTP sticky metadata cannot postpone an existing idle WS descriptor's own expiry deadline.
# 函数用途: 复现state续期掩盖socket到期时间的定时器错误。
def test_http_state_refresh_keeps_earlier_ws_expiry(monkeypatch):
    from agent_py_agent.agent.backends import responses_ws_pool as pool_module

    clock = [100.0]
    monkeypatch.setattr(pool_module.time, "monotonic", lambda: clock[0])
    pool = ws._POOL
    key = ("provider", "endpoint", "fingerprint", "session")
    state = pool.state(key, 300, 32)
    conn = Socket([])
    pool.put(key, pool_module.ConnectionEntry(conn, state), 300, 32)
    clock[0] = 350
    pool.state(key, 300, 32)
    assert pool._timer.interval <= 50.01


# LLM: Reset must revoke retained state objects too, not only remove the dictionary entry used by future callers.
# 函数用途: 复现reset后旧state仍可构造带旧sticky头的新请求。
def test_reset_clears_borrowed_header_state():
    from agent_py_agent.agent.backends.responses_ws_pool import session_pool_key

    with provider_session_scope(("local", "a"), "one"):
        key = session_pool_key(request())
        state = ws._POOL.state(key, 300, 32)
        ws._POOL.capture(key, state, {"x-codex-turn-state": "old-node"})
        ws.reset_responses_session()
        assert not state.turn_state_header
        assert "x-codex-turn-state" not in ws._sticky_request(request(), state).headers


# LLM: A foreign anchor may hide history absent from the full local input, invalidating prefix matching; only pool-generated IDs are allowed.
# 函数用途: 拒绝调用方注入previous ID，不将不可见远端历史并入本机增量链。
def test_caller_supplied_previous_id_is_rejected(monkeypatch):
    monkeypatch.setattr(ws, "_connect", lambda req: pytest.fail("must reject before connecting"))
    req = replace(request(), payload={**request().payload, "previous_response_id": "foreign"})
    with provider_session_scope(("local", "a"), "one"):
        with pytest.raises(ValueError, match="previous_response_id"):
            run(req)


# LLM: Borrow-time expiry validation closes the state()/take() scheduling gap, regardless of whether the timer ran yet.
# 函数用途: 钉住借连接时最后一道过期检查。
def test_take_rejects_expired_socket(monkeypatch):
    from agent_py_agent.agent.backends import responses_ws_pool as pool_module

    clock = [100.0]
    monkeypatch.setattr(pool_module.time, "monotonic", lambda: clock[0])
    pool = ws._POOL
    key = ("p", "e", "a", "s")
    state = pool.state(key, 300, 32)
    conn = Socket([])
    pool.put(key, pool_module.ConnectionEntry(conn, state), 300, 32)
    clock[0] = 401
    assert pool.take(key) is None
    assert conn.closed


# LLM: A second rejection must reach the existing error classifier, not recurse or create a third billed request.
# 函数用途: 确认透明恢复最多一次，失败后关闭两条连接。
def test_recovery_stops_after_one_fresh_request(monkeypatch):
    rejection = {"type": "error", "code": "previous_response_not_found"}
    old, fresh = Socket([complete(), [rejection]]), Socket([[rejection]])
    opened, sockets = [], iter([old, fresh])
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or next(sockets))
    with provider_session_scope(("local", "a"), "one"):
        run(request())
        with pytest.raises(ProviderResponseError, match="previous_response_not_found"):
            run(request([USER, RESULT]))
    assert len(opened) == 2 and old.closed and fresh.closed
    assert provider_endpoint_counts(request()) == (0, 0)


# LLM: Explicit zero retries is a native envelope guarantee; continuation recovery cannot override it.
# 函数用途: 固定零重试请求不做隐式全量重放。
def test_zero_retry_disables_reuse_recovery(monkeypatch):
    conn = Socket([complete(), [ConnectionClosedError(None, None)]])
    opened = []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or conn)
    with provider_session_scope(("local", "a"), "one"):
        run(request())
        with pytest.raises(Exception, match="回复完成前断开"):
            run(request([USER, RESULT], max_retries=0))
    assert len(opened) == 1 and conn.closed


# LLM: A late application frame after pool return invalidates that socket before a new response.create can be sent on it.
# 函数用途: 确认空闲期间迟到的旧帧不会串入下一响应。
def test_late_unread_frame_forces_fresh_full_request(monkeypatch):
    old, fresh = Socket([complete()]), Socket([complete("r2")])
    sockets = iter([old, fresh])
    monkeypatch.setattr(ws, "_connect", lambda req: next(sockets))
    with provider_session_scope(("local", "a"), "one"):
        run(request())
        old.messages.append({"type": "response.output_text.delta", "delta": "stale"})
        run(request([USER, RESULT]))
    assert old.closed and "previous_response_id" not in json.loads(fresh.sent[0])
    assert json.loads(fresh.sent[0])["input"] == [USER, RESULT]


# LLM: Test the actual websockets sync client/server and zero-timeout drain semantics, but only on local loopback with synthetic usage.
# 函数用途: 经真实本机WebSocket确认连接复用和增量首包，不冒充真实服务商缓存命中率。
def test_native_loopback_websocket_reuses_connection():
    from threading import Thread

    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve

    seen, handshakes = [], []

    # LLM: The local fixture implements Responses response.create only; input accounting remains synthetic, never a provider observation.
    # 函数用途: 在同一条本地连接上回应连续的两次模型请求。
    def handler(connection):
        handshakes.append(connection)
        try:
            for raw in connection:
                seen.append(json.loads(raw))
                connection.send(json.dumps(complete(f"r{len(seen)}")[0]))
        except ConnectionClosed:
            pass

    with serve(handler, "127.0.0.1", 0) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        req = request(api_base=f"http://127.0.0.1:{server.socket.getsockname()[1]}", timeout=3)
        try:
            with provider_session_scope(("local", "native"), "one"):
                run(req)
                assert provider_endpoint_counts(req) == (0, 0)
                result = run(replace(req, payload={**req.payload, "input": [USER, RESULT]}))
            assert len(handshakes) == 1 and len(seen) == 2
            assert seen[1]["previous_response_id"] == "r1" and seen[1]["input"] == [RESULT]
            assert result["usage"]["ws_connection_reused"] is True
        finally:
            ws._POOL.close()
            server.shutdown()
            worker.join(timeout=3)
        assert not worker.is_alive()


# LLM: Red-test cleanup only; it runs after assertions and releases traceback-held leases so a regression cannot poison other tests.
# 函数用途: 清理由失败回归保留的租约，不让故障断言依赖GC或污染后续测试。
def _release_traceback_leases(error):
    from agent_py_agent.agent.backends.request_scope import ProviderLease

    traceback = error.__traceback__
    while traceback is not None:
        lease = traceback.tb_frame.f_locals.get("lease")
        if isinstance(lease, ProviderLease):
            lease.release()
        traceback = traceback.tb_next


# LLM: Retain the actual TypeError traceback and assert counts inside except, before fixture cleanup or any GC can release the lease.
# 函数用途: 复现新连接诊断异常泄漏，并确认随后的同端点请求能立刻获准。
def test_new_connection_diagnostic_failure_releases_lease_with_retained_traceback(monkeypatch):
    import time

    opened, retained = [], []
    conn = Socket([complete()])
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or conn)
    req = request()
    invalid = replace(req, payload={**req.payload, "unsupported": object()})
    try:
        with provider_session_scope(("local", "a"), "diagnostic-failure"):
            with provider_attempt_observer(lambda event: None, cache_diagnostics=True):
                try:
                    run(invalid)
                except TypeError as error:
                    retained.append(error)
                    assert retained[0].__traceback__ is not None
                    assert provider_endpoint_counts(req) == (0, 0)
                    assert not opened, "the diagnostic failure must precede any physical connection"
                    result = run(replace(req, deadline=time.monotonic() + 1))
                    assert result["id"] == "r1" and len(opened) == 1
                    assert provider_endpoint_counts(req) == (0, 0)
                else:
                    pytest.fail("cache diagnostics must reject a non-JSON payload")
    finally:
        for error in retained:
            _release_traceback_leases(error)


# LLM: BaseException at either admission diagnostic boundary must not depend on exception GC, and an opened-but-unreturned socket is owned here.
# 函数用途: 验证诊断被中断时及时释放租约，并关闭尚未交给调用方的连接。
@pytest.mark.parametrize("phase", ["started", "response_opened"])
def test_connection_diagnostic_interrupt_releases_lease(monkeypatch, phase):
    conn = Socket([complete()])
    failure = KeyboardInterrupt("diagnostic interrupted")
    monkeypatch.setattr(ws, "_connect", lambda req: conn)

    # LLM: Fault injection targets host diagnostic emission, not the socket handshake or the provider error classifier.
    # 函数用途: 在指定诊断阶段模拟真实BaseException中断。
    def emit(event):
        if event["status"] == phase:
            raise failure

    monkeypatch.setattr(ws, "_emit_provider_attempt", emit)
    try:
        with pytest.raises(KeyboardInterrupt, match="diagnostic interrupted") as caught:
            run(request())
        assert caught.value is failure and caught.value.__traceback__ is not None
        assert provider_endpoint_counts(request()) == (0, 0)
        if phase == "response_opened":
            assert conn.closed
    finally:
        _release_traceback_leases(failure)


# LLM: Build next-turn history through the real collector/replay codec; the WS anchor must follow the same index, duplicate and unknown-index rules.
# 函数用途: 钉住乱序done、重复done及未知索引锚点仍能接上下一轮增量链。
@pytest.mark.parametrize("indices", [(1, 0), (1, 0, 1), (1, None, 0)])
def test_out_of_order_done_events_preserve_incremental_anchor(monkeypatch, indices):
    from agent_py_agent.agent.backends.responses_wire import message_items, response_fields

    calls = [CALL, {**CALL, "call_id": "c2", "arguments": json.dumps({"path": "b"}, sort_keys=True)}]
    note = {"id": "note", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "note"}]}
    events = [{"type": "response.output_item.done", "item": note if index is None else calls[index],
               **({"output_index": index} if index is not None else {})} for index in indices]
    conn = Socket([[*events, *complete()], complete("r2")])
    opened = []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or conn)
    results = [RESULT, {**RESULT, "call_id": "c2", "output": "second result"}]
    with provider_session_scope(("local", "a"), "out-of-order"):
        first = run(request())
        blocks = response_fields(first, "m")["assistant_content_blocks"]
        replay = message_items({"role": "assistant", "content": blocks}, "m")
        assert [item["call_id"] for item in replay if item.get("type") == "function_call"] == ["c1", "c2"]
        second = run(request([USER, *replay, *results]))
    assert len(opened) == 1 and second["usage"]["ws_connection_reused"] is True
    assert second["usage"]["incremental_chain_hit"] is True
    sent = json.loads(conn.sent[1])
    assert sent["previous_response_id"] == "r1" and sent["input"] == results


# LLM: Model-specific sockets and turn-state must stay isolated under the same host identity; returning to model A should retain A's valid chain.
# 函数用途: 验证同线程切模型分连接和路由状态，切回时仍复用原模型链，reset一次清理两者。
def test_same_session_models_have_separate_sockets_and_state(monkeypatch):
    from agent_py_agent.agent.backends.responses_ws_pool import session_pool_key

    a = Socket([complete("a1"), complete("a2")], state="model-a-node")
    b = Socket([complete("b1")], state="model-b-node")
    sockets = iter([a, b])
    opened = []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or next(sockets))
    base = request()
    req_a = replace(base, payload={**base.payload, "model": "model-a"})
    req_b = replace(base, payload={**base.payload, "model": "model-b"})
    with provider_session_scope(("local", "a"), "same-model-thread"):
        run(req_a)
        second = run(req_b)
        assert len(opened) == 2, "model-a and model-b must not share a physical websocket"
        assert second["usage"]["ws_connection_reused"] is False
        assert "x-codex-turn-state" not in opened[1].headers
        assert json.loads(b.sent[0])["model"] == "model-b"
        third = run(replace(req_a, payload={**req_a.payload, "input": [USER, RESULT]}))
        assert len(opened) == 2 and third["usage"]["incremental_chain_hit"] is True
        assert json.loads(a.sent[1])["previous_response_id"] == "a1"
        assert json.loads(a.sent[1])["input"] == [RESULT]
        assert session_pool_key(req_a) != session_pool_key(req_b)
        spaced = replace(req_a, payload={**req_a.payload, "model": " model-a "})
        assert session_pool_key(spaced) == session_pool_key(req_a)
        ws.reset_responses_session()
        assert a.closed and b.closed


# LLM: Reusing the collector must preserve conflict metadata, not merely its sorted output list; malformed tool identities cannot seed a chain.
# 函数用途: 补证done身份冲突在锚点回填中仍然有效，不因字典复制丢失拒绝工具标记。
def test_conflicting_done_identity_does_not_seed_incremental_anchor(monkeypatch):
    from agent_py_agent.agent.backends.responses_wire import response_fields
    from agent_py_agent.agent.backends.responses_ws_pool import session_pool_key

    second = {**CALL, "call_id": "conflicting-call"}
    events = [{"type": "response.output_item.done", "output_index": 0, "item": item} for item in (CALL, second)]
    conn = Socket([[*events, *complete()]])
    monkeypatch.setattr(ws, "_connect", lambda req: conn)
    with provider_session_scope(("local", "a"), "identity-conflict"):
        response = run(request())
        fields = response_fields(response, "m")
        assert fields["truncated"] is True and not fields["tool_use_blocks"]
        entry = ws._POOL.take(session_pool_key(request()))
        try:
            assert entry._ws_last_response_id == "" and entry._ws_last_items == b""
        finally:
            entry.discard()
