"""S2-W2 focused: bounded hints, durable positions, shared projection and real SSE transport."""
from __future__ import annotations

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import http_handlers
from agent_py_agent.agent.gateway_parts import request_event_routes as routes
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_event_hub import (
    Gap,
    RequestEventHub,
    bind_hub,
    hub_from_config,
    notify_committed,
)
from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event


# LLM: All test state belongs to pytest tmp_path; no production home or existing records are touched.
# 函数用途: 为一个独立请求准备真实队列路径。
@pytest.fixture
def request_files(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses, paths.terminal):
        folder.mkdir(parents=True, exist_ok=True)
    (paths.processing / "r.json").write_text(json.dumps({"id": "r", "execution_attempt_id": "attempt-a", "user_id": "alice", "channel": "chat"}))
    return paths


# LLM: Production configuration remains the only capacity source; test inputs deliberately cover both ring sizes.
# 函数用途: 构造不启动线程的有界中心。
def make_hub(ring=8, **kwargs):
    return RequestEventHub(ring_events=ring, topic_capacity=kwargs.get("topics", 16),
        subscribers_per_topic=kwargs.get("subscribers", 8), idle_seconds=kwargs.get("idle", 0), fallback_poll_seconds=0.1)


# LLM: A stalled reader may miss hints but cannot hold publishers or receive body/path fields.
# 函数用途: 验证溢出、白名单、不可变副本及真正的有界环。
@pytest.mark.parametrize("ring", [8, 1024])
def test_ring_gap_metadata_and_monotonic_sequence(ring):
    hub = make_hub(ring)
    subscription = hub.subscribe(("request", "r"), after_seq=0)
    refs = {"offset": 1, "attempt_id": "attempt-a", "text": "secret-body", "path": "/private/secret"}
    for index in range(ring + 3):
        assert hub.publish(("request", "r"), "chunk", "r", {**refs, "offset": index}) == index + 1
    assert len(hub._topics[("request", "r")].ring) == ring
    assert isinstance(subscription.read()[0], Gap)
    assert hub.publish(("request", "r"), "chunk", "r", refs) == ring + 4
    event = subscription.read()[0]
    assert dict(event.ref) == {"offset": 1, "attempt_id": "attempt-a"}
    with pytest.raises(TypeError):
        event.ref["offset"] = 4
    assert "secret-body" not in repr(hub._topics)
    subscription.close()
    hub.close()
    assert hub.publish(("request", "r"), "chunk", "r", {}) == 0


# LLM: All eight actual publisher threads must finish with distinct sequence numbers despite an unconsumed subscriber.
# 函数用途: 实测并发发布不等待慢消费者，序号连续且不重号。
def test_eight_publishers_with_stalled_consumer():
    hub = make_hub(1024)
    subscription = hub.subscribe(("request", "r"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(lambda _: hub.publish(("request", "r"), "chunk", "r", {}), range(800)))
    assert sorted(sequences) == list(range(1, 801))
    assert len(subscription.read()) == 800
    assert not subscription.wait(0)


# LLM: Capacity refusal is explicit and does not grow state; only idle unsubscribed topics can be evicted.
# 函数用途: 验证topic/subscriber硬上限和空闲淘汰。
def test_capacity_gap_and_idle_eviction():
    hub = make_hub(topics=1, subscribers=1)
    first = hub.subscribe(("request", "a"))
    refused = hub.subscribe(("request", "a"))
    assert refused.read()[0].reset_required
    assert hub.subscribe(("request", "b")).read()[0].reset_required
    assert hub.publish(("request", "b"), "chunk", "b", {}) == 0
    first.close()
    second = hub.subscribe(("request", "b"))
    assert second._state is not None
    assert len(hub._topics) == 1
    second.close()


# LLM: Subscribe/read/wait ordering must retain a commit that happens before wait; a hint by itself never creates durable completion.
# 函数用途: 验证先订阅后重读，以及停止/被拒信号不会忙转。
def test_signal_pre_wait_wake_stop_and_refusal():
    hub = make_hub(subscribers=1)
    signal = hub.signal(("request", "r"))
    hub.publish(("request", "r"), "terminal", "r", {})
    assert signal.wait(0)
    assert not signal.wait(0)
    stop = threading.Event()
    stop.set()
    assert not signal.wait(10, stop)
    denied = hub.signal(("request", "r"))
    assert denied.wait(0)
    start = time.monotonic()
    assert not denied.wait(0.03)
    assert time.monotonic() - start >= 0.02
    signal.close()


# LLM: Absent/zero knobs preserve legacy off; enabled caps are derived and not separate operator policies.
# 函数用途: 核对配置来源和推导公式。
def test_config_derived_bounds():
    assert hub_from_config(SimpleNamespace()) is None
    assert hub_from_config(SimpleNamespace(gateway_event_hub_ring_events=0)) is None
    hub = hub_from_config(SimpleNamespace(gateway_event_hub_ring_events=8, gateway_global_inflight_limit=10,
        owner_agent_pool_max_agents=3, gateway_user_inflight_limit=4, owner_agent_idle_seconds=9,
        gateway_event_fallback_poll_seconds=7))
    assert (hub.topic_capacity, hub.subscribers_per_topic, hub.idle_seconds, hub.fallback_poll_seconds) == (19, 4, 9, 7)


# LLM: Only the private append's successful final predicate may trigger a hint; a write exception is not a commit.
# 函数用途: 用真实落盘反证“先通知后写”和迟到写复活。
def test_chunk_hint_is_after_durable_append(request_files, monkeypatch):
    paths = request_files
    hub = make_hub()
    bind_hub(paths, hub)
    subscription = hub.subscribe(("request", "r"))
    path = gateway_chunk_path(paths, "r")
    write_chunk_event(path, {"kind": "thinking", "text": "private"}, still_current=lambda: False)
    assert not path.exists()
    assert not subscription.read()
    write_chunk_event(path, {"kind": "assistant_commentary", "text": "公开"})
    event = subscription.read()[0]
    assert event.ref["offset"] == path.stat().st_size
    assert json.loads(path.read_text())["text"] == "公开"
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.stream_writer.append_private_text", lambda *a, **k: (_ for _ in ()).throw(OSError("failed")))
    write_chunk_event(path, {"kind": "assistant_commentary", "text": "not committed"})
    assert not subscription.read()


# LLM: Resume must be independent of volatile ring size or a freshly recreated hub.
# 函数用途: 核对每条记录的独立字节游标及跨中心重启恢复。
def test_durable_resume_ignores_ring_restart(request_files):
    paths = request_files
    path = gateway_chunk_path(paths, "r")
    lines = [json.dumps({"kind": "assistant_commentary", "text": f"第{i}行"}, ensure_ascii=False) for i in range(20)]
    path.write_text("\n".join(lines) + "\n")
    first = routes.RequestReplayReader(paths, "r")
    rows, _ = first.read_page()
    cursor = rows[6][1]
    hub = make_hub(8)
    bind_hub(paths, hub)
    for _ in range(30):
        notify_committed(paths, "r", "chunk")
    hub.close()
    replacement = make_hub(8)
    bind_hub(paths, replacement)
    assert replacement.publish(("request", "r"), "chunk", "r", {}) == 1
    resumed = routes.RequestReplayReader(paths, "r", cursor)
    remaining, _ = resumed.read_page()
    assert [row for row, _ in remaining] == lines[7:]
    assert not resumed.reset_required
    assert rows[0][1].endswith(f":1:{len((lines[0]+chr(10)).encode())}")


# LLM: Malformed, foreign, stale, mid-line and past-end cursors must replay rather than silently skip.
# 函数用途: 用反例核对游标拒绝与安全重置。
@pytest.mark.parametrize("cursor", ["bad", "v1:other:attempt-a:1:1", "v1:r:old:1:1", "v1:r:attempt-a:1:2", "v1:r:attempt-a:1:99999", "v1:r:attempt-a:-1:0"])
def test_invalid_cursor_resets(request_files, cursor):
    path = gateway_chunk_path(request_files, "r")
    path.write_text('{"kind":"thinking"}\n')
    reader = routes.RequestReplayReader(request_files, "r", cursor)
    assert reader.reset_required
    rows, _ = reader.read_page()
    assert len(rows) == 1
    assert rows[0][1].endswith(f":1:{path.stat().st_size}")


# LLM: Incomplete UTF-8 and LF tails are never acknowledged; legacy line-only resume remains available.
# 函数用途: 验证半个中文字符和行号回退，不用字符长度冒充字节偏移。
def test_partial_utf8_and_line_fallback(request_files):
    path = gateway_chunk_path(request_files, "r")
    complete = '{"kind":"assistant_commentary","text":"中文"}\n'.encode()
    path.write_bytes(complete[:-3])
    reader = routes.RequestReplayReader(request_files, "r")
    assert reader.read_page() == ((), False)
    assert reader.offset == 0
    with path.open("ab") as handle:
        handle.write(complete[-3:] + complete)
    rows, _ = reader.read_page()
    assert len(rows) == 2
    fallback = routes.RequestReplayReader(request_files, "r", "v1:r:attempt-a:1")
    assert len(fallback.read_page()[0]) == 1


# LLM: SSE and /progress must share exactly one white-list projection, including oversize markers and private thinking exclusion.
# 函数用途: 对比原轮询与新逐行投影，保留脱敏和include选择。
def test_same_public_projection(request_files):
    rows = [json.dumps(x) for x in [
        {"kind": "thinking", "text": "private"}, {"kind": "assistant_commentary", "text": "public"},
        {"kind": "progress_oversize", "line": 4, "bytes": 1234},
        {"kind": "assistant_text_delta", "model_call_id": "c", "seq": 0, "text": "字", "level": "stream"},
    ]]
    path = gateway_chunk_path(request_files, "r")
    path.write_text("\n".join(rows) + "\n")
    include = frozenset({"assistant_text_delta"})
    expected, count = http_handlers._read_public_progress_events(path, 0, include=include)
    actual = []
    for row, _ in routes.RequestReplayReader(request_files, "r").read_page()[0]:
        actual.extend(http_handlers._project_public_progress_rows((row,), include=include)[0])
    assert actual == expected
    assert count == 4
    assert "private" not in repr(actual)
    assert any(e["kind"] == "assistant_text_delta" for e in actual)


# LLM: New query normalization is narrow; existing unmatched /status query behavior remains unchanged.
# 函数用途: 覆盖上次阻塞的真实匹配反例和兼容别名。
def test_only_new_routes_normalize_query():
    from agent_py_agent.agent.gateway_parts.http_routes import match_gateway_http_route
    for path in ("/client/requests/r/events?cursor=bad", "/v1/requests/r/events?after=bad"):
        assert match_gateway_http_route("GET", path).handler_name == "handle_request_events"
    assert match_gateway_http_route("GET", "/status?cursor=bad") is None


# LLM: The existing access gate is rechecked while streaming, not only during admission.
# 函数用途: 流建立后撤销归属权限，确认没有继续吐出公开正文。
def test_access_revocation_stops_stream(request_files, monkeypatch):
    server = SimpleNamespace(paths=request_files)
    handler = SimpleNamespace(headers={}, _auth_middleware=None)
    stream = routes.request_event_frames(handler, server, "r")
    assert next(stream)[0] == "heartbeat"
    monkeypatch.setattr(routes, "_can_read_finished_request", lambda *a: routes._FinishedRequestAccess.DENIED)
    with pytest.raises(PermissionError):
        next(stream)
    stream.close()


# LLM: No-wait notices must retain the exact original object and arguments, not a reconstructed approximation.
# 函数用途: 反证没有wait参数时通知字节行为被改动。
def test_notices_without_wait_are_unchanged():
    result = {"notices": [], "cursor": 7, "event_stream_id": "stream"}
    called = []
    def reader(agent, **kwargs):
        called.append(kwargs)
        return result
    assert http_handlers._read_client_notices_with_wait(reader, object(), hub=None, after=7) is result
    assert called == [{"after": 7}]
