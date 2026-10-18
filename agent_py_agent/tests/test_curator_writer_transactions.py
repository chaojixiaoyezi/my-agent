"""真实C/BG claim与原Curator writer；仅供应商等待和设施节拍由测试控制。"""
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition, update_json_file_atomic
from agent_py_agent.agent.gateway_parts.request_errors import RequestExecutionOutcomeUncertainError
from agent_py_agent.agent.memory_store import curator as curator_module
from agent_py_agent.agent.memory_store import curator_lease
from agent_py_agent.tests.test_agents_cleanup_review33 import _setup
from agent_py_agent.tests.test_agents_phase0_guard import _curator
from agent_py_agent.tests.test_gateway_background_curator_transactions import (
    curator_case,
    run_original,
)


@pytest.mark.parametrize('fault', ['cancel', 'generation'])
def test_original_heartbeat_renews_then_rejects_revocation_without_state_write(tmp_path, monkeypatch, fault):
    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, close):
        real_init, real_renew = curator_lease._LeaseHeartbeat.__init__, curator_lease.renew_curator_lease
        before_revoke, revoked, after_revoke = threading.Event(), threading.Event(), threading.Event()
        failures, successes = [], []

        # LLM: 只缩短原心跳设施节拍，不改持久期限/许可或替换线程；每次仍调用产品原renew。
        # 函数用途: 在可控时间内观察原心跳线程实际写入与撤权后的拒绝。
        def short_period(heartbeat, *args):
            real_init(heartbeat, *args)
            heartbeat.seconds = 0.03

        def observe_renew(*args):
            try:
                result = real_renew(*args)
            except BaseException as error:
                if revoked.is_set():
                    failures.append(error)
                    after_revoke.set()
                raise
            else:
                if threading.current_thread().name == 'curator-lease-heartbeat':
                    successes.append(True)
                    if revoked.is_set():
                        after_revoke.set()
                    else:
                        before_revoke.set()
                return result

        monkeypatch.setattr(curator_lease._LeaseHeartbeat, '__init__', short_period)
        monkeypatch.setattr(curator_lease, 'renew_curator_lease', observe_renew)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            try:
                assert backend.started.wait(3)
                assert before_revoke.wait(3), 'original heartbeat must renew before revocation'
                if fault == 'cancel':
                    with gateway_turn_transition(gate.paths, claim.request_id):
                        update_json_file_atomic(claim.path, lambda row: {**row, 'cancel_requested': True, 'turn_phase': 'closing'})
                        state = service.state_store.path.read_bytes()
                        revoked.set()
                else:
                    close()
                    state = service.state_store.path.read_bytes()
                    revoked.set()
                assert after_revoke.wait(3), 'same original heartbeat must attempt its next renewal'
                assert len(failures) == 1 and isinstance(failures[0], RequestExecutionOutcomeUncertainError)
                assert failures[0].request_id == claim.request_id and failures[0].operation_entered is False
                assert service.state_store.path.read_bytes() == state
            finally:
                backend.release.set()
            with pytest.raises(RequestExecutionOutcomeUncertainError):
                future.result(3)
        assert successes and service.candidate_service.list() == []
        assert service.state_store.path.read_bytes() == state


def test_closed_original_context_cannot_register_a_late_curator_trigger(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, _close):
        real_execute = service._execute
        contexts = []

        # LLM: 捕获原BG调用的同一cell，不安装新source；真实请求返回后cell应共同失效。
        # 函数用途: 保存退出前的只读上下文引用，验证它不能延寿到后续写入。
        def capture(context):
            contexts.append(contextvars.copy_context())
            return real_execute(context)

        monkeypatch.setattr(service, '_execute', capture)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            try:
                assert backend.started.wait(3)
            finally:
                backend.release.set()
            assert future.result(3)['ok'] is True
        state = service.state_store.path.read_bytes()
        assert len(contexts) == 1
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            contexts[0].run(service.state_store.request, 'admin')
        assert caught.value.request_id == claim.request_id and caught.value.operation_entered is False
        assert service.state_store.path.read_bytes() == state


@pytest.mark.parametrize('writer', ['canonical_target', 'state'])
def test_copied_live_transaction_cannot_write_from_another_thread(tmp_path, monkeypatch, writer):
    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, _close):
        real_write = service.committer._write_target
        effects, threads = [], []

        # LLM: 故障注入发生在真实原提交T窗口内，子线程只消费同一copied context；不签新scope或mock拒绝。
        # 函数用途: 尝试把原活窗口跨线程使用，必须由产品inner writer拒绝且原正文不变。
        def observe(path, content):
            if not threads:
                target = path if writer == 'canonical_target' else service.state_store.path
                original = target.read_bytes() if target.exists() else None
                state = service.state_store._load_unlocked()
                captured = contextvars.copy_context()
                done = threading.Event()

                def late_writer():
                    try:
                        if writer == 'canonical_target':
                            captured.run(real_write, path, '{"foreign_thread": true}\n')
                        else:
                            captured.run(service.state_store._write_unlocked, replace(state, config_revision='foreign-thread'))
                    except BaseException as error:
                        effects.append(error)
                    else:
                        effects.append(None)
                    finally:
                        effects.append((target.read_bytes() if target.exists() else None) == original)
                        done.set()

                thread = threading.Thread(target=late_writer)
                threads.append(thread)
                thread.start()
                assert done.wait(3)
            return real_write(path, content)

        monkeypatch.setattr(service.committer, '_write_target', observe)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run_original, agent, gate, claim)
                try:
                    assert backend.started.wait(3)
                finally:
                    backend.release.set()
                assert future.result(3)['ok'] is True
        finally:
            for thread in threads:
                thread.join(3)
                assert not thread.is_alive()
        assert len(effects) == 2 and isinstance(effects[0], RequestExecutionOutcomeUncertainError)
        assert effects[0].request_id == claim.request_id and effects[0].operation_entered is False
        assert effects[1] is True


@pytest.mark.parametrize('join_unknown', [False, True])
def test_started_heartbeat_ack_error_still_owns_original_stop_join(tmp_path, monkeypatch, join_unknown):
    env = _setup(tmp_path)
    service = _curator(tmp_path, env.conversations, env.service.candidates)
    real_constructor = curator_lease._LeaseHeartbeat
    real_start, real_join, real_renew = threading.Thread.start, threading.Thread.join, curator_lease.renew_curator_lease
    entered, release = threading.Event(), threading.Event()
    born, body, joins = [], [], []
    original, join_error = RuntimeError('synthetic_start_ack_error'), RuntimeError('synthetic_join_unknown')

    def constructor(*args):
        heartbeat = real_constructor(*args)
        born.append(heartbeat)
        return heartbeat

    def hold_renew(*args):
        if born and threading.current_thread() is born[0].thread:
            entered.set()
            assert release.wait(5)
        return real_renew(*args)

    def start_then_error(thread):
        real_start(thread)
        assert entered.wait(3)
        raise original

    def observe_join(thread, *args, **kwargs):
        if born and thread is born[0].thread:
            joins.append(thread)
            if join_unknown:
                raise join_error
            release.set()
        return real_join(thread, *args, **kwargs)

    monkeypatch.setattr(curator_module, '_lease_seconds', lambda _config: 0)
    monkeypatch.setattr(curator_lease, '_LeaseHeartbeat', constructor)
    monkeypatch.setattr(curator_lease, 'renew_curator_lease', hold_renew)
    monkeypatch.setattr(threading.Thread, 'start', start_then_error)
    monkeypatch.setattr(threading.Thread, 'join', observe_join)
    monkeypatch.setattr(service, '_execute', lambda _context: body.append(True))
    try:
        if join_unknown:
            with pytest.raises(RuntimeError) as caught:
                service.run(reason='admin')
            assert caught.value is original
            assert curator_lease.lease_exception_metadata(original)['lease_heartbeat_stop_error_class'] == 'RuntimeError'
            assert born[0].thread.is_alive()
            assert service.state_store.load().active_lease['phase'] == 'executing'
            assert service.run_log.list() == []
        else:
            assert service.run(reason='admin').status == 'failed'
            assert not born[0].thread.is_alive()
            assert not service.state_store.load().active_lease
        assert joins == [born[0].thread] and born[0].stop.is_set()
        assert not body
    finally:
        release.set()
        for heartbeat in born:
            heartbeat.stop.set()
            real_join(heartbeat.thread, 3)
            assert not heartbeat.thread.is_alive()
