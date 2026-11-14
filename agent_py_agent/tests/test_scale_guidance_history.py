"""插话历史规模、热路径结构与原协议的回归护栏。"""
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.store_guidance_records import _guidance_entry_from_request


# LLM: 固定切片迁移真实队列，热路只计实际解析与返回结果。
# 函数用途: 验证 pending 和 recent 不随已消费历史增长。
@pytest.mark.parametrize("history", [1000, 10000])
@pytest.mark.parametrize("pending", [0, 1, 20])
def test_guidance_pending_and_recent_cost_does_not_grow_with_history(tmp_path, monkeypatch, history, pending):
    store = ConversationStore(tmp_path / "conversations")
    rows = [_guidance_entry_from_request({"target_type": "thread", "target_id": "target",
             "message": f"entry-{n}", "now": n + 1}).to_dict() for n in range(history + pending)]
    path = store.storage.guidance_path("thread", "target")
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    store.storage.guidance_delivered_path.write_text(json.dumps({row["guidance_id"]: 1 for row in rows[:history]}))
    factory = getattr(store.guidance.ledger, "pending_index", None)
    if factory:
        for _ in range(history // 64 + 10):
            if factory().refresh("thread", "target"):
                break
        else:
            pytest.fail("guidance bounded backfill did not converge")
    counts = {"G": 0, "D": 0, "json": 0, "bytes": 0}
    loads = json.loads

    # LLM: 原解析器照常执行，返回值与异常不变。
    # 函数用途: 计数 guidance 与 delivered 的真实解析工作。
    def observe(raw, *args, **kwargs):
        counts["json"] += 1
        counts["bytes"] += len(raw.encode() if isinstance(raw, str) else raw)
        value = loads(raw, *args, **kwargs)
        if isinstance(value, dict):
            counts["G"] += int("guidance_id" in value)
            counts["D"] += len(value) if value and all(str(key).startswith("guidance-") for key in value) else 0
        return value

    monkeypatch.setattr(json, "loads", observe)
    before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
    found = store.guidance.pending("thread", "target", limit=20)
    recent, errors = store.guidance.recent_report("thread", "target", limit=20)
    if before is not None:
        assert len(os.listdir("/dev/fd")) == before
    assert not errors
    assert {entry.guidance_id for entry in found} == {row["guidance_id"] for row in rows[history:]}
    assert len(recent) == 20
    assert counts["G"] <= 50 and counts["D"] <= 40, counts
    assert counts["json"] <= 100 and counts["bytes"] < 131072, counts


# LLM: Event 规定 ACK 胜出后的恢复顺序，状态变更仍走原协议。
# 函数用途: 验证恢复不会重放已消费插话。
def test_guidance_ack_and_rebind_interleaving_preserves_one_terminal_fact(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    guidance = store.guidance
    entry = guidance.append_once({"target_type": "thread", "target_id": "target", "message": "one",
        "metadata": {"expected_turn_id": "old"}}, dedupe_key="one")
    assert guidance.claim_for_turn(entry, expected_turn_id="old", attempt_id="old")
    guidance.submissions.mark_submitted("old", [entry], attempt_id="old", provider_call_id="call")
    start, submitted = threading.Event(), threading.Event()

    # LLM: 原提交凭证和 provider_call_id 不变，消费后才通知恢复。
    # 函数用途: 提交唯一消费事实。
    def ack():
        start.wait(timeout=5)
        result = guidance.acknowledgements.consume_submitted("old", [entry], provider_call_id="call", now=10)
        submitted.set()
        return result

    # LLM: 不替代恢复方法，只等待同一消费事务先完成。
    # 函数用途: 检查终态之后的恢复结果。
    def rebind():
        start.set()
        assert submitted.wait(timeout=5)
        return guidance.recovery.rebind_unsubmitted("thread", "target", dead_turn_ids=["old"], recovered_turn_id="new")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(ack), pool.submit(rebind)
        assert a.result(timeout=10) == (entry.guidance_id,)
        assert b.result(timeout=10)["rebound"] == 0
    assert guidance.receipt("one").status == "consumed"
    assert guidance.pending("thread", "target") == []
    reopened = ConversationStore(store.storage.root)
    assert reopened.guidance.pending("thread", "target") == []


# LLM: 内部无 dedupe 消息仍须保存独立精确退休证明。
# 函数用途: 验证内部消息重启后不会再次待投递。
def test_legacy_internal_delivery_survives_index_rebuild(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    entry = store.guidance.append({"target_type": "request", "target_id": "turn", "message": "internal"})
    store.guidance.ledger.mark_delivered([entry.guidance_id], now=10)
    assert store.guidance.pending("request", "turn") == []
    assert ConversationStore(store.storage.root).guidance.pending("request", "turn") == []
    assert not store.storage.guidance_delivered_path.exists()


# LLM: 仅供应商传输为替身，停止、换会话、重组 Agent 和持久读写均为真实入口。
# 函数用途: 验证控制序列和重启后的观察责任连续性。
def test_headless_steer_stop_new_restart_reopens_real_store_indexes(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import control_service
    from agent_py_agent.tests.test_session_task_real_chain import (
        RealChain,
        _agent_config,
        _ready_gateway_paths,
        _real_chain,
    )

    chain = _real_chain(tmp_path, monkeypatch)
    store = chain.agent.conversation_store
    observation = store.observations.append({"thread_id": chain.threads["C"], "event_type": "w2",
        "summary": "restart responsibility", "requires_main_agent": True})
    acked = store.observations.append({"thread_id": chain.threads["C"], "event_type": "w2",
        "summary": "durable acknowledgement", "requires_main_agent": True})
    store.observations.mark_handled([acked.observation_id])
    errors = []

    # LLM: 供应商返回前挂起原请求，主线程经真实控制服务停止它。
    # 函数用途: 为控制序列启动真实忙碌回合。
    def busy_turn():
        try:
            chain.ask("B", "RC-BUSY-STOP bounded index continuity")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=busy_turn)
    worker.start()
    try:
        assert chain.wire.wait_holding("busy")
        scope = control_service.GatewayControlScope(chain.agent.home_paths.owner_id, "chat", "rc-session-B",
            metadata={"message_id": "w2-steer"})
        steer = control_service.execute_gateway_conversation_control(chain.agent, chain.paths,
            parse_conversation_control("/btw RC-NOTE-W2 keep the pending responsibility"), scope)
        assert steer.ok, steer
        # 此窗口刻意停在供应商返回前：先证明真实入口已提交待办，再停止，不能把enqueue当作模型ACK。
        queued = store.guidance.pending("request", steer.request_id)
        assert any("RC-NOTE-W2" in entry.message for entry in queued), (steer, queued)
        assert chain.stop("B").ok
        worker.join(timeout=20)
        assert not worker.is_alive(), errors
    finally:
        chain.wire.release("busy")
        chain.wire.release("busy-note")
        worker.join(timeout=20)
    reset = control_service.execute_gateway_conversation_control(chain.agent, chain.paths,
        parse_conversation_control("/new index continuity"), scope)
    assert reset.ok, reset
    # 真正重新组装Agent，不把替换一个方法/假存储当成重启。
    reopened = SimpleAgent(_agent_config(tmp_path), tmp_path / "root")
    restart = RealChain(reopened, _ready_gateway_paths(reopened), chain.wire, {}, scheduler=None, asks=chain.asks)
    successor = restart.open_session("B")
    assert successor != chain.threads["B"]
    restart.ask("B", "RC-ACK after restart")
    pending = reopened.conversation_store.observations.unhandled_requiring_main(limit=20)
    assert observation.observation_id in {item.observation_id for item in pending}
    assert acked.observation_id not in {item.observation_id for item in pending}
    reopened.conversation_store.observations.mark_handled([observation.observation_id])
    assert reopened.conversation_store.observations.unhandled_requiring_main(limit=20) == []
    events, errors = reopened.conversation_store.model_usage.events_report(successor)
    assert not errors and events
    assert reopened.conversation_store.model_usage.summary(successor)["event_count"] == len(events)


# LLM: 原回合锁真实重叠；ACK持锁未完成时rebind已尝试同锁，不能用先结束ACK再启动恢复替代并发。
# 函数用途: 验证ACK/恢复竞争中唯一消费事实及索引终态单调性。
def test_guidance_ack_holds_original_turn_while_rebind_is_waiting(tmp_path, monkeypatch):
    from contextlib import contextmanager

    store = ConversationStore(tmp_path / 'conversations')
    guidance = store.guidance
    entry = guidance.append_once({'target_type': 'thread', 'target_id': 'target', 'message': 'one',
        'metadata': {'expected_turn_id': 'old'}}, dedupe_key='overlap')
    assert guidance.claim_for_turn(entry, expected_turn_id='old', attempt_id='old')
    guidance.submissions.mark_submitted('old', [entry], attempt_id='old', provider_call_id='call')
    held, trying, release = threading.Event(), threading.Event(), threading.Event()
    guard = guidance.ledger.turn_guard

    # LLM: 原 guard 仍持有真实回合锁，只记录恢复线程已尝试获取它。
    # 函数用途: 观察实际锁竞争时机。
    @contextmanager
    def observed(turn_id):
        if threading.current_thread().name == 'rebind-worker' and turn_id == 'old':
            trying.set()
        with guard(turn_id):
            yield

    apply = guidance.acknowledgements._apply_locked

    # LLM: 仅延迟原 ACK 应用，放行后调用原方法，不预先写终态。
    # 函数用途: 让 ACK 和恢复线程真实重叠。
    def paused(batch):
        held.set()
        assert release.wait(5)
        return apply(batch)

    monkeypatch.setattr(guidance.ledger, 'turn_guard', observed)
    monkeypatch.setattr(guidance.acknowledgements, '_apply_locked', paused)
    results, errors = {}, []

    # LLM: 异常交回主线程，不能在线程失败后仍判成功。
    # 函数用途: 运行真实提交消费事务。
    def ack():
        try:
            results['ack'] = guidance.acknowledgements.consume_submitted('old', [entry], provider_call_id='call')
        except BaseException as exc:
            errors.append(exc)

    # LLM: 恢复沿原回合锁等待，结果与异常均由主测试核对。
    # 函数用途: 并发尝试恢复同一插话。
    def rebind():
        try:
            results['rebind'] = guidance.recovery.rebind_unsubmitted('thread', 'target', dead_turn_ids=['old'], recovered_turn_id='new')
        except BaseException as exc:
            errors.append(exc)

    a = threading.Thread(target=ack, name='ack-worker')
    b = threading.Thread(target=rebind, name='rebind-worker')
    a.start()
    try:
        assert held.wait(5)
        b.start()
        assert trying.wait(5)
        assert b.is_alive() and 'rebind' not in results
    finally:
        release.set()
        a.join(10)
        if b.ident is not None:
            b.join(10)
    assert not a.is_alive() and not b.is_alive() and not errors
    assert results['ack'] == (entry.guidance_id,)
    assert results['rebind']['rebound'] == 0 and results['rebind']['errors'] == 0
    assert guidance.receipt('overlap').status == 'consumed'
    assert ConversationStore(store.storage.root).guidance.pending('thread', 'target') == []


# LLM: 独立journal是内部退休事实；删除/损坏SQLite后只能有界恢复，不能复活旧消息。
# 函数用途: 验证投影丢弃、legacy固定迁移及重启后的精确退休证明。
@pytest.mark.parametrize('damage', ['delete', 'corrupt', 'generation'])
def test_guidance_retirement_survives_projection_damage(tmp_path, damage):
    store = ConversationStore(tmp_path / 'conversations')
    entries = [store.guidance.append({'target_type': 'thread', 'target_id': 'target', 'message': str(n)}) for n in range(150)]
    store.guidance.ledger.mark_delivered([item.guidance_id for item in entries[:-1]], now=10)
    index = store.guidance.ledger.pending_index()
    if damage == 'delete':
        index.path.unlink()
    elif damage == 'corrupt':
        index.path.write_bytes(b'not a database')
    else:
        with sqlite3.connect(index.path) as conn:
            conn.execute("UPDATE meta SET value='old-generation' WHERE key='generation'")
    reopened = ConversationStore(store.storage.root)
    for _ in range(10):
        found, errors = reopened.guidance.pending_report('thread', 'target')
        if not errors:
            break
        assert not found
    else:
        pytest.fail('bounded guidance rebuild did not converge')
    assert [item.guidance_id for item in found] == [entries[-1].guidance_id]
    assert len(reopened.guidance.recent('thread', 'target', limit=0)) == len(entries)
    assert not store.storage.guidance_delivered_path.exists()


# LLM: 未迁移旧ack不能解释成未送达；写新ack不被旧map大小阻塞，迁移游标由冷进程接续。
# 函数用途: 验证固定切片与unknown fail-closed，不读取整个legacy map。
def test_guidance_legacy_partial_resumes_and_unknown_never_replays(tmp_path):
    from agent_py_agent.agent.conversation.guidance_pending_index import GuidanceIndexIncomplete

    store = ConversationStore(tmp_path / 'conversations')
    entry = store.guidance.append({'target_type': 'thread', 'target_id': 'target', 'message': 'legacy'})
    payload = {f'old-{n}': 2 for n in range(300)}
    payload[entry.guidance_id] = 7
    store.storage.guidance_delivered_path.write_text(json.dumps(payload))
    with pytest.raises(GuidanceIndexIncomplete):
        store.guidance.ledger.delivered_at(entry.guidance_id)
    index = store.guidance.ledger.pending_index()
    with sqlite3.connect(index.path) as conn:
        offset = int(conn.execute("SELECT value FROM meta WHERE key='legacy_offset'").fetchone()[0])
    assert 0 < offset < store.storage.guidance_delivered_path.stat().st_size
    store.guidance.ledger.mark_delivered(['new-id'], now=9)
    reopened = ConversationStore(store.storage.root)
    for _ in range(10):
        if reopened.guidance.ledger.pending_index().refresh('thread', 'target'):
            break
    else:
        pytest.fail('legacy guidance migration did not converge')
    assert reopened.guidance.ledger.delivered_at(entry.guidance_id) == 7
    assert reopened.guidance.ledger.delivered_at('new-id') == 9
    assert reopened.guidance.pending('thread', 'target') == []


# LLM: 新SQLite锁与原queue/delivered锁同样必须遵守try-lock，不在短预算里等待默认5秒。
# 函数用途: 验证新增SQLite争用和deadline合同。
def test_guidance_projection_busy_is_nonblocking_and_preserves_canonical(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    request = {'target_type': 'thread', 'target_id': 'target', 'message': 'one'}
    entry = store.guidance.append_once(request, dedupe_key='busy')
    path = store.storage.guidance_dedupe_path('busy')
    original = path.read_bytes()
    index = store.guidance.ledger.pending_index()
    with sqlite3.connect(index.path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        with pytest.raises(BlockingIOError):
            store.guidance.append_once(request, dedupe_key='busy', blocking=False, deadline=started + 0.1)
        assert time.monotonic() - started < 0.25
    assert path.read_bytes() == original
    assert store.guidance.append_once(request, dedupe_key='busy').guidance_id == entry.guidance_id


# LLM: canonical提交成功后投影失败必须留下可发现意图；恢复只发布原事实，不重造guidance身份。
# 函数用途: 验证跨库崩溃窗口的恢复与有界脏集。
def test_guidance_committed_receipt_repairs_after_projection_error(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations')
    request = {'target_type': 'thread', 'target_id': 'target', 'message': 'one'}
    index = store.guidance.ledger.pending_index()
    publish = index.publish
    error = OSError('simulated projection write error')

    # LLM: 只在已有投影发布点制造 I/O 错误，canonical 写入仍真实完成。
    # 函数用途: 打开可复现的跨库中断窗口。
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(index, 'publish', fail)
    with pytest.raises(OSError) as caught:
        store.guidance.append_once(request, dedupe_key='crash')
    assert caught.value is error
    canonical = json.loads(store.storage.guidance_dedupe_path('crash').read_text())
    assert canonical['status'] == 'pending'
    assert list(index.dirty_dir.glob('*.json'))
    monkeypatch.setattr(index, 'publish', publish)
    reopened = ConversationStore(store.storage.root)
    assert [item.guidance_id for item in reopened.guidance.pending('thread', 'target')] == [canonical['entry']['guidance_id']]
    assert reopened.guidance.append_once(request, dedupe_key='crash').guidance_id == canonical['entry']['guidance_id']
    assert list(index.dirty_dir.iterdir()) == []


# LLM: 稳定队列按精确位置去重，不把整queue读取藏进helper；原始入口追加/重试都纳入计数。
# 函数用途: 验证append_once在历史增长后只访问有限JSON行，且未知provider提交永不重放。
@pytest.mark.parametrize('history', [1000, 10000])
def test_guidance_append_exact_queue_position_and_unknown_submission(tmp_path, monkeypatch, history):
    store = ConversationStore(tmp_path / 'conversations')
    rows = [_guidance_entry_from_request({'target_type': 'thread', 'target_id': 'target',
             'message': str(n)}).to_dict() for n in range(history)]
    path = store.storage.guidance_path('thread', 'target')
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    index = store.guidance.ledger.pending_index()
    for _ in range(history // 64 + 10):
        if index.refresh('thread', 'target'):
            break
    else:
        pytest.fail('queue backfill did not converge')
    decoded = []
    loads = json.loads

    # LLM: 原解析结果原样透传，仅记录实际读到的 guidance 身份。
    # 函数用途: 检查追加去重没有隐藏整队列读取。
    def observe(raw, *args, **kwargs):
        value = loads(raw, *args, **kwargs)
        if isinstance(value, dict) and 'guidance_id' in value:
            decoded.append(value['guidance_id'])
        return value

    monkeypatch.setattr(json, 'loads', observe)
    request = {'target_type': 'thread', 'target_id': 'target', 'message': 'new',
               'metadata': {'expected_turn_id': 'old'}}
    entry = store.guidance.append_once(request, dedupe_key='new')
    assert store.guidance.append_once(request, dedupe_key='new').guidance_id == entry.guidance_id
    assert len(decoded) <= 10
    assert store.guidance.claim_for_turn(entry, expected_turn_id='old', attempt_id='old')
    store.guidance.submissions.mark_submitted('old', [entry], attempt_id='old', provider_call_id='unknown')
    recovered = store.guidance.recovery.rebind_unsubmitted('thread', 'target', dead_turn_ids=['old'], recovered_turn_id='new')
    assert recovered['rebound'] == 0 and recovered['submitted_unknown'] == 1
    assert store.guidance.receipt('new').status == 'submitted'
    assert entry.guidance_id not in {item.guidance_id for item in store.guidance.pending('thread', 'target')}


# LLM: JSON计数无法发现SQL全表扫描；断言精确退休和pending分页的真实查询计划。
# 函数用途: 防止将全图成本从Python转移到SQLite隐藏扫描。
def test_guidance_retirement_and_pending_sql_use_exact_indexes(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations')
    entry = store.guidance.append({'target_type': 'thread', 'target_id': 'target', 'message': 'one'})
    index = store.guidance.ledger.pending_index()
    statements = []
    connect = sqlite3.connect

    # LLM: 保留真实连接和 SQL，只安装 SQLite 原生 trace 回调。
    # 函数用途: 捕获精确退休与 pending 分页的实际语句。
    def observed(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', observed)
    store.guidance.ledger.mark_delivered([entry.guidance_id], now=1)
    assert store.guidance.pending('thread', 'target') == []
    retire_sql = next(sql for sql in statements if sql.startswith('DELETE FROM pending WHERE id='))
    page_sql = next(sql for sql in statements if sql.startswith('SELECT e.* FROM pending p JOIN entries'))
    with connect(index.path) as conn:
        retire = conn.execute('EXPLAIN QUERY PLAN ' + retire_sql).fetchall()
        page = conn.execute('EXPLAIN QUERY PLAN ' + page_sql).fetchall()
    retire_plan = ' '.join(str(row[3]) for row in retire)
    page_plan = ' '.join(str(row[3]) for row in page)
    assert 'SEARCH pending' in retire_plan and 'SEARCH e' in retire_plan
    assert 'SCAN' not in retire_plan
    assert 'pending_target' in page_plan and 'SCAN' not in page_plan and 'TEMP B-TREE' not in page_plan
