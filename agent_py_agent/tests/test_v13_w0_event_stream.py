"""W0事件流：原运行库、确定性时钟及真实HTTP共同验证有界交付与恢复。"""
import socket
import threading
import traceback
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.business_event_stream import stream_business_events
from agent_py_agent.tests.test_business_v12_f7_observation import fixture
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)
from agent_py_agent.tests.test_mega_business_api_actions import (
    create_background,
    open_native_observation,
    seed_observed_runtime,
)
from agent_py_agent.tests.test_v13_w0_recover_consumer import seed_task


class Clock:
    """只推进测试逻辑时间，不替换来源或响应。"""

    # LLM: 用单调逻辑时间验证退避，避免真实睡眠掩盖轮询缺陷。
    # 函数用途: 初始化逻辑时钟与等待记录。
    def __init__(self):
        self.now = 0
        self.waits = []

    # LLM: 时钟读取不推进时间。
    # 函数用途: 返回当前逻辑秒数。
    def __call__(self):
        return self.now

    # LLM: 等待只推进请求的时间，保留每次超时供断言。
    # 函数用途: 记录并推进测试等待。
    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


# LLM: 测试只向原运行库追加规范事件。
# 函数用途: 生成可被原观察投影读取的进度事件。
def append(case):
    return case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])


# LLM: 仅注入时钟与等待，授权及事件源均使用原对象。
# 函数用途: 创建确定性typed流与时钟。
def make_stream(case, *, transport='native', cursor=None, signal=None):
    clock = Clock()
    request = {'request_id': 'w0-event-stream', 'filter': {'space_id': 'space-a'}}
    if cursor is not None:
        request['cursor'] = cursor
    stream = stream_business_events(request, authenticated_context=case.actor,
        services=SimpleNamespace(observation=case.service, clock=clock, wait=clock.wait,
            stream_transport=transport, event_signal=signal))
    return stream, clock


# LLM: 每次交付都复核界限，验证真实来源中的顺序和稳定身份。
# 函数用途: 确认突发130事件不会产生背压缺口或清空积压。
@pytest.mark.parametrize('transport,bound', [('native', 128), ('plugin', 8)])
def test_burst_of_130_is_delivered_in_order_without_gap_or_clear(tmp_path, transport, bound):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    stream, clock = make_stream(case, transport=transport, cursor=baseline['next_cursor'])
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    expected = [append(case)['event_id'] for _ in range(130)]
    received = []
    for _ in expected:
        frame = next(iterator)
        assert frame['stream_item'] == 'event'
        assert stream.pending_count <= bound
        received.append(frame['event_id'])
    assert received == expected
    assert next(iterator)['stream_item'] == 'heartbeat'
    assert clock.now >= stream.heartbeat_seconds
    iterator.close()
    assert stream.pending_count == 0


# LLM: 来源故意忽略limit，但仍以真实查询返回事件和游标。
# 函数用途: 验证超长页截断后续点是实际入队末项，且满队列不再查询。
def test_truncated_page_uses_last_admitted_cursor_and_full_queue_skips_query(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    expected = [append(case)['event_id'] for _ in range(30)]
    original = case.service.query
    calls = []

    # LLM: 只改变来源的页长行为，不手工制造游标或事件字典。
    # 函数用途: 模拟未遵守limit但遵守cursor的真实来源。
    def oversized(action, params, *, actor):
        calls.append(params)
        return original(action, {**params, 'limit': 100}, actor=actor)

    monkeypatch.setattr(case.service, 'query', oversized)
    stream, _clock = make_stream(case, transport='plugin')
    gap, _coverage = stream._fill()
    assert gap is None and stream.pending_count == 8
    assert stream._cursor == stream._pending[-1]['cursor']
    before = len(calls)
    assert stream._fill()[0] is None and len(calls) == before
    assert all(0 < call['limit'] <= 8 for call in calls)
    received = [row['event_id'] for row in stream._pending]
    stream._pending.clear()
    while not all(identifier in received for identifier in expected):
        assert stream._fill()[0] is None
        assert stream.pending_count <= 8
        received.extend(row['event_id'] for row in stream._pending)
        stream._pending.clear()
    assert received[-30:] == expected and len(received) == len(set(received))


# LLM: gap是结构化测试故障，故障后的数据仍来自原运行库。
# 函数用途: 确认gap及一次游标过期都不结束迭代器。
def test_gap_and_cursor_expired_do_not_end_the_generator(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    stream, _clock = make_stream(case)
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    original = stream._fill
    faults = ['gap', 'expired']

    # LLM: 每种故障只触发一次，让后续循环走真实来源。
    # 函数用途: 注入gap或过期，再恢复原填充路径。
    def fail_once():
        if faults:
            if faults.pop(0) == 'gap':
                return stream._gap('gap', 'SOURCE_RESET'), 'partial'
            raise BusinessError('CURSOR_EXPIRED')
        return original()

    monkeypatch.setattr(stream, '_fill', fail_once)
    assert next(iterator)['stream_item'] == 'gap'
    assert next(iterator)['stream_item'] == 'cursor_expired'
    assert next(iterator)['stream_item'] == 'event'
    iterator.close()


# LLM: 连续过期必须有限停止，不能把snapshot误当恢复成功。
# 函数用途: 验证第二次连续过期后停止查询且不会热循环。
def test_second_consecutive_expiry_stops_without_hot_loop(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    stream, _clock = make_stream(case)
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    calls = []

    # LLM: 只注入结构化错误码。
    # 函数用途: 模拟持续过期的不可恢复来源。
    def expired():
        calls.append(True)
        raise BusinessError('CURSOR_EXPIRED')

    monkeypatch.setattr(stream, '_fill', expired)
    assert next(iterator)['stream_item'] == 'cursor_expired'
    assert next(iterator)['stream_item'] == 'cursor_expired'
    with pytest.raises(StopIteration):
        next(iterator)
    assert len(calls) == 2


# LLM: 连续过期保护不能跨越真实进展永久锁死流。
# 函数用途: 验证事件或心跳交付后允许再次从过期恢复。
@pytest.mark.parametrize('progress', ['event', 'heartbeat'])
def test_delivery_resets_consecutive_expiry_guard(tmp_path, monkeypatch, progress):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    stream, _clock = make_stream(case, cursor=baseline['next_cursor'])
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    original = stream._fill
    remaining = [True]

    # LLM: 每次由测试明确安排一次错误，不凭文案判断来源。
    # 函数用途: 注入可恢复过期后再交由原来源读取。
    def once():
        if remaining:
            remaining.pop()
            raise BusinessError('CURSOR_EXPIRED')
        return original()

    monkeypatch.setattr(stream, '_fill', once)
    for _ in range(2):
        assert next(iterator)['stream_item'] == 'cursor_expired'
        # 重置续点会重新扫描原事件；它们也是实际恢复进展，不能算第二次连续过期。
        frame = next(iterator)
        assert frame['stream_item'] == 'event'
        if progress == 'heartbeat':
            while frame['stream_item'] != 'heartbeat':
                frame = next(iterator)
        else:
            while stream.pending_count:
                assert next(iterator)['stream_item'] == 'event'
        remaining.append(True)
    iterator.close()


# LLM: 用真实空页和可控时间验证轮询与心跳不是同一个频率。
# 函数用途: 验证首次小等待、倍增上限、完整空闲后才发心跳，以及事件重置退避。
def test_idle_poll_backoff_starts_small_and_heartbeat_only_after_interval(tmp_path):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    stream, clock = make_stream(case, cursor=baseline['next_cursor'])
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    assert next(iterator)['stream_item'] == 'heartbeat'
    heartbeat = stream.heartbeat_seconds
    assert clock.now == heartbeat
    assert clock.waits[:5] == [heartbeat / 60 * 2 ** power for power in range(5)]
    assert all(0 < seconds <= heartbeat for seconds in clock.waits)
    clock.waits.clear()
    assert next(iterator)['stream_item'] == 'heartbeat'
    assert clock.waits[0] == heartbeat / 60
    append(case)
    assert next(iterator)['stream_item'] == 'event'
    clock.waits.clear()
    idle_since = clock.now
    assert next(iterator)['stream_item'] == 'heartbeat'
    assert clock.waits[0] == heartbeat / 60
    assert clock.now - idle_since == heartbeat
    iterator.close()


# LLM: 信号在等待入口真实追加规范事件，不用sleep或假响应制造唤醒。
# 函数用途: 验证event_signal优先等待且事件在心跳前立即交付。
def test_event_signal_wakes_immediately(tmp_path):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    seen = []

    # LLM: 同步模拟宿主通知到达，不推进逻辑时间。
    # 函数用途: 在首次wait时写入来源并返回已唤醒。
    def wake(timeout):
        seen.append((timeout, append(case)['event_id']))
        return True

    stream, clock = make_stream(case, cursor=baseline['next_cursor'], signal=SimpleNamespace(wait=wake))
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    frame = next(iterator)
    assert frame['stream_item'] == 'event' and frame['event_id'] == seen[0][1]
    assert seen[0][0] == stream.heartbeat_seconds / 60
    assert clock.now == 0 and clock.waits == []
    iterator.close()


# LLM: 此用例走真实HTTP，不能用编译或typed生成器替代连接观察点。
# 函数用途: 验证同一NDJSON连接跨越130条突发仍能交付新事件。
@pytest.mark.parametrize('small_send_buffer', [False, True])
def test_native_http_stream_keeps_connection_across_burst(background_http, monkeypatch, small_send_buffer):
    import json

    params, agent = create_background(background_http)
    [services] = [value for value in background_http[0]._business_services.values()
        if value.owner_id.endswith('/business-alice')]
    task = seed_task(services, params['space_id'], agent['agent_id'])
    runtime, run = seed_observed_runtime(services, task)
    with runtime._runtime_connection() as conn:
        initial_ids = [row[0] for row in conn.execute('SELECT event_id FROM runtime_events ORDER BY seq')]
    if small_send_buffer:
        pool = background_http[0].business_stream_pool
        submit = pool.submit
        def bounded_socket(ticket, sock, iterator, encode=None, check=None):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            return submit(ticket, sock, iterator, encode, check)
        monkeypatch.setattr(pool, 'submit', bounded_socket)
    connection, response = open_native_observation(background_http, {'request_id': 'w0-burst-http', 'filter': {}})
    transport = response.fp.raw._sock
    frames, failures = [], []
    reader = None
    try:
        assert response.status == 200
        assert response.getheader('Connection') == 'close'
        assert response.getheader('Content-Type').startswith('application/x-ndjson')
        assert json.loads(response.readline())['stream_item'] == 'snapshot'
        # 持续消费真实连接，不让fixture的130次fsync把自己变成超过原sendall预算的慢消费者。
        def consume():
            try:
                for _ in range(len(initial_ids) + 130):
                    line = response.readline()
                    assert line, '突发事件尚未读完，原生连接已EOF'
                    frames.append(json.loads(line))
            except BaseException:
                failures.append(traceback.format_exc())
        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        expected = [runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
            agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id'])['event_id'] for _ in range(130)]
        with runtime._runtime_connection() as conn:
            all_ids = [row[0] for row in conn.execute('SELECT event_id FROM runtime_events ORDER BY seq')]
        reader.join(timeout=20)
        assert not reader.is_alive() and failures == [], failures
        assert all_ids == initial_ids + expected
        assert len(frames) == len(all_ids)
        assert all(frame['stream_item'] == 'event' for frame in frames)
        assert [frame['event_id'] for frame in frames] == all_ids
        assert all_ids[-130:] == expected
        later = runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
            agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id'])
        assert json.loads(response.readline())['event_id'] == later['event_id']
    finally:
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if reader is not None and reader.ident is not None:
            reader.join(timeout=5)
            assert not reader.is_alive(), '原生读取线程未回收'
        response.close()
        connection.close()


def test_event_attempt_snapshot_survives_concurrent_append_without_expiring_history_cursor(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    first = append(case)
    original = case.service._event_execution_page
    later = []
    selected = []
    def concurrent_append(conn, rows):
        selected.append(tuple(dict.fromkeys(row['attempt_id'] for row in rows if row['attempt_id'])))
        assert len(selected[-1]) <= 100
        if not later:
            later.append(append(case))  # 真实独立WAL写事务，发生在事件读取与attempt批量join之间。
        return original(conn, rows)
    monkeypatch.setattr(case.service, '_event_execution_page', concurrent_append)
    page = case.service.query('events.list', {'filter': {'space_id': 'space-a'},
        'cursor': baseline['next_cursor'], 'limit': 100}, actor=case.actor)
    assert [row['event_id'] for row in page['events']] == [first['event_id']]
    following = case.service.query('events.list', {'filter': {'space_id': 'space-a'},
        'cursor': page['next_cursor'], 'limit': 100}, actor=case.actor)
    assert [row['event_id'] for row in following['events']] == [later[0]['event_id']]
    assert selected == [(case.root['attempt_id'],), (case.root['attempt_id'],)]
