"""W5：原锁准入、配置代次、单飞刷新和停止事实；不连接真实飞书。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu, feishu_ws
from agent_py_agent.agent.adapter.delivery import GatewayReplyDeliveryStore, PendingGatewayReply
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.adapter.protocol import IncomingMessage


def test_eight_invalid_cache_readers_share_one_provider_refresh():
    adapters = [feishu.FeishuAdapter({'feishu_app_id': 'w5-eight', 'feishu_app_secret': 'synthetic',
        'config_generation': 'eight', 'feishu_session_lock_enabled': False}) for _ in range(8)]
    barrier, entered, release = threading.Barrier(8), threading.Event(), threading.Event()
    calls = []

    def refresh():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {'code': 0, 'tenant_access_token': 'synthetic-token', 'expire': 7200}

    for adapter in adapters:
        adapter._request_tenant_access_token = refresh

    def read(adapter):
        barrier.wait()
        return adapter._get_tenant_access_token()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [pool.submit(read, adapter) for adapter in adapters]
        assert entered.wait(1)
        time.sleep(0.03)
        release.set()
        assert [future.result(2) for future in results] == ['synthetic-token'] * 8
    assert calls == [1]


def test_old_refresh_cannot_overwrite_new_generation():
    adapter = feishu.FeishuAdapter({'feishu_app_id': 'w5-generation', 'feishu_app_secret': 'synthetic',
        'config_generation': 'old', 'feishu_session_lock_enabled': False})
    entered, release = threading.Event(), threading.Event()

    def refresh_old():
        entered.set()
        assert release.wait(2)
        return {'code': 0, 'tenant_access_token': 'old-token', 'expire': 7200}

    adapter._request_tenant_access_token = refresh_old
    old = threading.Thread(target=adapter._get_tenant_access_token)
    old.start()
    assert entered.wait(1)
    adapter.config_generation = 'new'
    adapter._request_tenant_access_token = lambda: {'code': 0, 'tenant_access_token': 'new-token', 'expire': 7200}
    assert adapter._get_tenant_access_token() == 'new-token'
    release.set()
    old.join(2)
    assert not old.is_alive()
    assert adapter._get_tenant_access_token() == 'new-token'
    assert adapter._tenant_access_token == 'new-token'


def test_stop_interrupts_real_reconnect_backoff(monkeypatch):
    client = feishu_ws.FeishuWsClient(app_id='w5-stop', app_secret='synthetic', on_payload=lambda _: None)
    entered, dead = threading.Event(), threading.Event()

    def fail():
        entered.set()
        raise OSError('synthetic')

    client.start = fail
    monkeypatch.setattr(feishu_ws, '_RECONNECT_INITIAL_BACKOFF_SECONDS', 30)
    thread = feishu_ws.run_ws_client_thread(client, dead.set)
    assert entered.wait(1)
    started = time.monotonic()
    client.close()
    thread.join(0.5)
    assert not thread.is_alive() and dead.is_set()
    assert time.monotonic() - started < 0.5


def test_unexited_transport_keeps_handle_and_generation():
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    client = SimpleNamespace(close=lambda: None)
    adapter._ws_client, adapter._ws_thread, adapter._running = client, thread, True
    try:
        first = adapter.stop(timeout=0.01)
        assert first.status == 'exit_unconfirmed'
        assert first.reason_code and first.generation_ref
        assert adapter._ws_thread is thread and adapter._ws_client is client
        with pytest.raises(RuntimeError):
            adapter.start()
    finally:
        release.set()
        thread.join(1)
    second = adapter.stop(timeout=0.1)
    assert second.status == 'exited' and second.generation_ref == first.generation_ref
    assert adapter._ws_thread is None


def test_manager_does_not_upgrade_unconfirmed_stop():
    from agent_py_agent.agent.delivery.registry import ChannelStopResult

    manager = ChannelManager()
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._running = True
    adapter.stop = lambda: ChannelStopResult('exit_unconfirmed', 'generation-1', 'CHANNEL_ADAPTER_STOP_FAILED')
    manager.register_adapter(adapter)
    result = manager.stop_all()
    assert result['feishu'].status == 'exit_unconfirmed'
    assert manager._delivery_registry._health['feishu'].state != 'stopped'


def test_stale_claim_epoch_has_no_component_commit_right(tmp_path):
    store = GatewayReplyDeliveryStore(tmp_path)
    record, _ = store.put_if_absent(PendingGatewayReply('w5-epoch', 'feishu', 'ou_one', 'om_anchor'))
    old = store.claim(record, owner='old', now=100, ttl=1)
    new = store.claim(old, owner='new', now=102, ttl=1)
    assert new.claim_epoch == old.claim_epoch + 1
    assert store.commit_claim(old, replace(old, progress_cursor=10), owner='old', epoch=old.claim_epoch) is None
    assert not store.mark_terminal_claimed(old, owner='old', epoch=old.claim_epoch, disposition='sent')
    assert store.pending() == [new]


def test_local_and_scale_read_same_store_and_recheck_before_business(tmp_path, monkeypatch):
    from agent_py_agent.agent.observability.tracing import TraceContext
    from agent_py_agent.agent.scale_downstream import ScaleAgentPool, ScaleMessage
    from agent_py_agent.agent.session_lock import admission

    config = SimpleNamespace(my_agent_home=str(tmp_path), feishu_session_lock_enabled=True,
        feishu_personal_idle_lock_seconds=60)
    adapter = feishu.FeishuAdapter(vars(config))
    adapter._unlock.store.set_password_hash('ou_one', 'synthetic-hash', now=time.time() - 100)
    adapter._send_password_card = lambda *_: True
    msg = IncomingMessage(channel='feishu', user_id='ou_one', content='job', message_id='om_one',
        conversation_id='oc_one', metadata={'feishu_chat_type': 'p2p'})
    calls = []
    original = admission.check_session_admission

    def inspect(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(admission, 'check_session_admission', inspect)
    assert feishu._session_message_lock_disposition(adapter, msg) is not None
    pool = object.__new__(ScaleAgentPool)
    pool._base_config = config
    message = ScaleMessage('t', 'user', 'ou_one', 'job', 'om_one', 'oc_one', 'ou_one')
    agent = SimpleNamespace(config=config)
    with pytest.raises(RuntimeError, match='FEISHU_SESSION'):
        pool._run_conversation_turn(agent, message, TraceContext('1' * 32, '2' * 16))
    assert len(calls) == 2 and all(not item.allowed and item.locked for item in calls)
    adapter._unlock.store.record_activity('ou_one')
    assert admission.admission_from_config(config, 'ou_one').allowed
    assert admission.admission_from_config(config, 'ou_other', is_group=True).allowed


def test_eight_adapters_share_same_app_and_generation_connection(monkeypatch):
    monkeypatch.setattr(feishu, '_CONNECTIONS', {})
    clients, starts = [], []

    def client(**kwargs):
        value = SimpleNamespace(**kwargs)
        clients.append(value)
        return value

    handle = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(feishu_ws, 'FeishuWsClient', client)
    monkeypatch.setattr(feishu_ws, 'run_ws_client_thread', lambda *_: starts.append(1) or handle)
    adapters = [feishu.FeishuAdapter({'feishu_app_id': 'w5-connections', 'feishu_app_secret': 'synthetic',
        'config_generation': 'shared', 'feishu_session_lock_enabled': False}) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda adapter: adapter.start(), adapters))
    assert len(clients) == 1 and starts == [1]
    assert all(adapter._ws_client is clients[0] and adapter._ws_thread is handle for adapter in adapters)


def test_stop_close_and_join_run_outside_lifecycle_lock():
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    released, observed = threading.Event(), []
    thread = threading.Thread(target=released.wait, daemon=True)
    thread.start()

    def close():
        acquired = adapter._lock.acquire(blocking=False)
        observed.append(acquired)
        if acquired:
            adapter._lock.release()
        released.set()

    adapter._ws_client, adapter._ws_thread, adapter._running = SimpleNamespace(close=close), thread, True
    result = adapter.stop(timeout=1)
    assert result.status == 'exited' and observed == [True]


def test_refresh_waiters_have_bounded_wait_and_provider_runs_without_cache_lock(monkeypatch):
    adapter = feishu.FeishuAdapter({'feishu_app_id': 'w5-bounded', 'feishu_app_secret': 'synthetic',
        'config_generation': 'bounded', 'feishu_session_lock_enabled': False})
    waits = []
    event = SimpleNamespace(wait=lambda timeout: waits.append(timeout) or False)
    monkeypatch.setattr(feishu, '_TOKEN_REFRESHES', {('w5-bounded', 'bounded'): feishu._TokenRefresh(inflight=True, done=event)})
    assert adapter._get_tenant_access_token() is None and waits == [11.0]
    feishu._TOKEN_REFRESHES.clear()

    def refresh():
        assert feishu._TOKEN_REFRESH_LOCK.acquire(blocking=False)
        feishu._TOKEN_REFRESH_LOCK.release()
        return {'code': 0, 'tenant_access_token': 'synthetic', 'expire': 7200}

    adapter._request_tenant_access_token = refresh
    assert adapter._get_tenant_access_token() == 'synthetic'


@pytest.mark.parametrize('configured', ['', '~/.w5-lock'])
def test_scale_admission_uses_canonical_environment_and_expanded_home(tmp_path, monkeypatch, configured):
    from agent_py_agent.agent.session_lock.admission import admission_from_config
    from agent_py_agent.agent.session_lock.store import SessionLockStore

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'env-root'))
    root = tmp_path / '.w5-lock' if configured else tmp_path / 'env-root'
    store = SessionLockStore(root / 'session_lock' / 'private_chat_locks.db')
    store.set_password_hash('ou_canonical', 'synthetic-hash')
    store.record_activity('ou_canonical')
    config = SimpleNamespace(my_agent_home=configured, feishu_session_lock_enabled=True)
    assert admission_from_config(config, 'ou_canonical').allowed
    assert not admission_from_config(config, 'ou_missing').allowed


def test_blocking_old_closer_prevents_restart_and_remains_unhealthy():
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    entered, release = threading.Event(), threading.Event()
    old_thread = threading.Thread(target=lambda: None)
    old_thread.start()
    old_thread.join(1)

    def close():
        entered.set()
        assert release.wait(2)

    client = SimpleNamespace(close=close)
    adapter._ws_client, adapter._ws_thread, adapter._running = client, old_thread, True
    manager = ChannelManager()
    manager.register_adapter(adapter)
    try:
        receipt = adapter.stop(timeout=0.01)
        assert entered.wait(1) and receipt.status == 'exit_unconfirmed'
        with pytest.raises(RuntimeError, match='CHANNEL_ADAPTER_STOP_FAILED'):
            adapter.start()
        assert adapter._ws_client is client
        assert adapter.runtime_health()['state'] == 'unhealthy'
        assert next(row for row in manager._delivery_registry.runtime_snapshot() if row.name == 'feishu').health.state == 'unhealthy'
    finally:
        release.set()
    assert adapter.stop(timeout=1).status == 'exited'


def test_close_request_without_execution_exit_observation_cannot_restart():
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    original = SimpleNamespace(close=lambda: None)
    adapter._ws_client = original
    receipt = adapter.stop(timeout=1)
    assert receipt.status == 'exit_unconfirmed'
    assert adapter.runtime_health()['state'] == 'unhealthy'
    with pytest.raises(RuntimeError, match='CHANNEL_ADAPTER_STOP_FAILED'):
        adapter.start()
    assert adapter._ws_client is original


def test_unlocked_scale_executes_original_gateway_transcript_and_conversation_isolation_probe(tmp_path):
    from agent_py_agent.agent.session_lock.store import SessionLockStore
    from agent_py_agent.tests.test_scale_downstream import (
        test_scale_turn_reuses_gateway_transcript_and_isolates_other_conversation as exercise_original_probe,
    )

    # 复用原业务断言，仅补新准入合同要求的原密码/解锁事实，不关闭认证。
    store = SessionLockStore(tmp_path / 'home' / 'session_lock' / 'private_chat_locks.db')
    store.set_password_hash('ou_user', 'synthetic-hash')
    store.record_activity('ou_user')
    exercise_original_probe(tmp_path)


def test_shared_stop_blocks_other_members_and_new_adapter_until_original_closer_exits(monkeypatch):
    monkeypatch.setattr(feishu, '_CONNECTIONS', {})
    release, entered = threading.Event(), threading.Event()
    clients, closes = [], []

    def client(**kwargs):
        value = SimpleNamespace(**kwargs, _stopped=False, exited=threading.Event())

        def close():
            closes.append(value)
            value._stopped = True
            value.exited.set()  # 执行线程先退出，SDK close 仍被刻意阻塞。
            entered.set()
            assert release.wait(2)

        value.close = close
        clients.append(value)
        return value

    def run(value, on_dead):
        def execute():
            value.exited.wait()
            on_dead()

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        return thread

    monkeypatch.setattr(feishu_ws, 'FeishuWsClient', client)
    monkeypatch.setattr(feishu_ws, 'run_ws_client_thread', run)
    adapters = [feishu.FeishuAdapter({'feishu_app_id': 'w5-shared-stop', 'feishu_app_secret': 'synthetic',
        'config_generation': 'same', 'feishu_session_lock_enabled': False}) for _ in range(3)]
    first, sibling, newcomer = adapters
    try:
        first.start()
        sibling.start()
        assert len(clients) == 1
        receipt = first.stop(timeout=0.01)
        assert entered.wait(1)
        first._ws_thread.join(0.1)
        assert not first._ws_thread.is_alive() and receipt.status == 'exit_unconfirmed'
        for member in (newcomer, sibling):
            with pytest.raises(RuntimeError, match='CHANNEL_ADAPTER_STOP_FAILED'):
                member.start()
        assert len(clients) == 1 and closes == [clients[0]]
        assert sibling.stop(timeout=0.01).status == 'exit_unconfirmed'
        assert closes == [clients[0]]  # 所有成员共用唯一 closer，不能重复关闭。
        assert first.runtime_health()['state'] == sibling.runtime_health()['state'] == 'unhealthy'
        release.set()
        assert first.stop(timeout=1).status == sibling.stop(timeout=1).status == 'exited'
        newcomer.start()
        assert len(clients) == 2 and newcomer._ws_client is clients[1]
    finally:
        release.set()
        for adapter in adapters:
            adapter.stop(timeout=1)


def test_shared_stop_also_rejects_follower_already_waiting_to_publish_connection(monkeypatch):
    monkeypatch.setattr(feishu, '_CONNECTIONS', {})
    follower_waiting, release_follower = threading.Event(), threading.Event()
    release_close, execution_exit = threading.Event(), threading.Event()

    def close():
        execution_exit.set()
        assert release_close.wait(2)

    monkeypatch.setattr(feishu_ws, 'FeishuWsClient', lambda **kwargs: SimpleNamespace(**kwargs, close=close))

    def run(*_):
        thread = threading.Thread(target=execution_exit.wait, daemon=True)
        thread.start()
        return thread

    monkeypatch.setattr(feishu_ws, 'run_ws_client_thread', run)
    first, follower = [feishu.FeishuAdapter({'feishu_app_id': 'w5-pending-member', 'feishu_app_secret': 'synthetic',
        'config_generation': 'same', 'feishu_session_lock_enabled': False}) for _ in range(2)]
    first.start()

    def wait(_timeout):
        follower_waiting.set()
        assert release_follower.wait(2)
        return True

    first._shared_connection.ready = SimpleNamespace(is_set=lambda: True, wait=wait)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = pool.submit(follower.start)
            assert follower_waiting.wait(1)
            assert first.stop(timeout=0.01).status == 'exit_unconfirmed'
            release_follower.set()
            with pytest.raises(RuntimeError, match='CHANNEL_ADAPTER_STOP_FAILED'):
                started.result(1)
            assert follower._stop_event.is_set() and not follower.running
            assert follower._closing_thread is first._closing_thread
    finally:
        release_follower.set()
        release_close.set()
        first.stop(timeout=1)
        follower.stop(timeout=1)
