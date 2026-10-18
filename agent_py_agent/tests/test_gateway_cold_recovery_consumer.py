# LLM: 复验实际cold请求tick消费原C/P3/Runtime恢复；只用临时账本与既有合成P2进程表，不启动Worker、业务Agent或模型。
# 模块用途: 证明历史processing和terminal欠账沿同一原gate恢复，输入游标与未知责任不被误清。
import time
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.gateway_parts import recovery
from agent_py_agent.agent.gateway_parts.io import (
    gateway_response_path,
    read_json_file,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    _read_reservation,
    reservation_file,
)
from agent_py_agent.cli import gateway_coordinator_request_pump as module
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.tests.test_gateway_cold_processing_recovery import cold_case
from agent_py_agent.tests.test_gateway_cold_request_pump import cold_context


# LLM: 当前租约/ControlPlane与旧claim都沿前置真实产品producer；只把实际heartbeat时刻改为已过期，不伪造停止许可。
# 函数用途: 准备可由周期恢复消费的旧processing，并构造生产cold泵。
@contextmanager
def pump_case(tmp_path, monkeypatch, *, with_input=False, create_pump=True):
    with cold_case(tmp_path, monkeypatch, with_input=with_input) as (old, control, _gate, _context):
        row = read_json_file(old.claim.path)
        row['lease_heartbeat_at'] = time.time() - 100
        write_json_file_atomic(old.claim.path, row)
        run = cold_context(control)
        current = module.CoordinatorMetadataRequestPump(run, control.paths) if create_pump else run
        yield old, control, current


# LLM: wrapper原样调用真实恢复和投影，只观察原参数/返回对象，不提供许可或替代Store。
# 函数用途: 保存生产tick实际调用顺序与原诊断，便于验证同gate来源。
def observe_repairs(monkeypatch):
    events = []
    original_recover = recovery.recover_gateway_processing_requests_report
    original_project = recovery.repair_gateway_terminal_projections
    original_dispatch = module.dispatch_pending_requests

    def recover(*args, **kwargs):
        report = original_recover(*args, **kwargs)
        events.append(('recover', kwargs['params'], report))
        return report

    def project(*args, **kwargs):
        result = original_project(*args, **kwargs)
        events.append(('project', kwargs, result))
        return result

    def dispatch(*args, **kwargs):
        events.append(('dispatch', kwargs['admission_gate']))
        return original_dispatch(*args, **kwargs)

    monkeypatch.setattr(recovery, 'recover_gateway_processing_requests_report', recover)
    monkeypatch.setattr(recovery, 'repair_gateway_terminal_projections', project)
    monkeypatch.setattr(module, 'dispatch_pending_requests', dispatch)
    return events


def test_actual_cold_tick_recovers_stale_processing_with_its_retained_gate(tmp_path, monkeypatch):
    with pump_case(tmp_path, monkeypatch) as (old, control, current):
        events = observe_repairs(monkeypatch)
        gate = current._admission_gate
        before = _read_reservation(reservation_file(control.paths, old.claim.request_id))
        assert current.tick() == 0
        assert not old.claim.path.exists()
        pending = read_json_file(control.paths.inbox / old.claim.path.name)
        assert pending['status'] == 'pending' and pending['runtime_authority']
        assert [row[0] for row in events] == ['dispatch', 'recover', 'project']
        assert events[0][1] is gate
        assert events[1][1].agent is control and events[1][1].admission_gate is gate
        assert events[1][1].startup is False
        assert events[2][1]['agent'] is control and events[2][1]['recovery_context'].admission_gate is gate
        assert current.runtime._admission_origins[0][0] is gate
        after = _read_reservation(reservation_file(control.paths, old.claim.request_id))
        assert after['state'] == 'occupied' and after['reservation_id'] == before['reservation_id']
        assert not list(control.paths.terminal.glob('*.json'))


def test_actual_cold_request_loop_runs_recovery_without_business_dispatcher(tmp_path, monkeypatch):
    with pump_case(tmp_path, monkeypatch, create_pump=False) as (old, control, run):
        from threading import Event

        stop = Event()
        original_finished = loops.loop_health.mark_tick_finished

        def finished(count):
            original_finished(count)
            stop.set()

        def forbidden(*_args, **_kwargs):
            pytest.fail('cold request loop cannot build healthy business dispatcher')

        monkeypatch.setattr(loops.loop_health, 'mark_tick_finished', finished)
        monkeypatch.setattr(loops, '_RequestDispatcher', forbidden)
        loops._gateway_request_loop(run, control.paths, stop)
        assert not old.claim.path.exists()
        assert read_json_file(control.paths.inbox / old.claim.path.name)['status'] == 'pending'


def test_cold_tick_repairs_existing_canonical_without_rewriting_it(tmp_path, monkeypatch):
    with pump_case(tmp_path, monkeypatch, with_input=True) as (old, control, current):
        from dataclasses import replace

        row = read_json_file(old.claim.path)
        row['processing_failure_count'] = old.context.max_attempts - 1
        write_json_file_atomic(old.claim.path, row)
        report = recovery.recover_gateway_processing_requests_report(
            control.paths, params=replace(old.context, agent=control, admission_gate=current._admission_gate),
        )
        assert not report.load_errors and report.summary['failed'] == 1
        canonical = control.paths.terminal / old.claim.path.name
        before = canonical.read_bytes()
        response = gateway_response_path(control.paths, old.claim.request_id)
        response.unlink()
        assert current.tick() == 0
        assert response.is_file() and read_json_file(response)['id'] == old.claim.request_id
        assert canonical.read_bytes() == before
        assert current._last_projection_report[0] == 1 and current._last_projection_report[1] == []


@pytest.mark.parametrize('fault', ['runtime_unknown', 'wrong_owner', 'bad_fingerprint', 'unknown_p2'])
def test_actual_cold_tick_retains_original_unknown_and_reports_without_new_claim(tmp_path, monkeypatch, fault):
    with pump_case(tmp_path, monkeypatch) as (old, control, current):
        if fault == 'runtime_unknown':
            with old.repo.transaction() as db:
                db.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?", (old.binding['attempt_id'],))
        elif fault == 'wrong_owner':
            with old.repo.transaction() as db:
                db.execute("UPDATE tasks SET owner_id='providers/synthetic/users/other' WHERE task_id=?", (old.binding['task_id'],))
        elif fault == 'bad_fingerprint':
            row = read_json_file(old.claim.path)
            row['prompt'] = 'different unconfirmed input'
            write_json_file_atomic(old.claim.path, row)
        else:
            from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2

            monkeypatch.setattr(p2, 'observe_processes', lambda: {
                'platform': 'darwin', 'host_id': 'host-one', 'rows': {}, 'failure': 'unreadable',
            })
        events = observe_repairs(monkeypatch)
        current.tick()
        assert any(row[0] == 'recover' for row in events)
        assert old.claim.path.exists() and not (control.paths.inbox / old.claim.path.name).exists()
        assert _read_reservation(reservation_file(control.paths, old.claim.request_id))['state'] == 'occupied'
        assert not list(control.paths.terminal.glob('*.json'))
        assert current._last_recovery_report.load_errors
        assert current._recovery_backoff.consecutive_failures == 1
        if fault != 'bad_fingerprint':
            assert current._last_recovery_report.load_errors[0]['error_code'] == 'ACTIVE_TURN_OUTCOME_UNCERTAIN'


def test_unsettled_input_is_only_examined_while_cold_cursor_rotates(tmp_path, monkeypatch):
    with pump_case(tmp_path, monkeypatch, with_input=True) as (_old, control, current):
        from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
        from agent_py_agent.agent.gateway_parts import multiprocess_input_reconcile as queued

        cursor = control.paths.root / 'input_receipts/state/reconcile_cursor.json'
        write_json_file_atomic(cursor, {'schema_version': 'gateway_input_reconcile_cursor.v1', 'last_name': 'original-position', 'updated_at': 1})
        before = cursor.read_bytes()
        original_scan = queued.reconcile_coordinator_queued_inputs
        observed = []

        def forbidden(*_args, **_kwargs):
            pytest.fail('cold repair must not call whole unsettled input reconcile')

        monkeypatch.setattr(inputs, 'reconcile_gateway_input_receipts', forbidden)

        # LLM: 只观察queued扫描边界；原恢复/终态仍真实执行，扫描时禁止取guidance，不能把输入正文副作用算成修游标。
        # 函数用途: 对照同一拍扫描前后原receipt字节，保留active输入不被当前C消费的合同。
        def scan(*args, **kwargs):
            receipt_dir = control.paths.root / 'input_receipts'
            previous = {path.name: path.read_bytes() for path in receipt_dir.glob('*.json')}
            with monkeypatch.context() as guard:
                guard.setattr(inputs, 'gateway_input_guidance_binding', forbidden)
                report = original_scan(*args, **kwargs)
            assert previous == {path.name: path.read_bytes() for path in receipt_dir.glob('*.json')}
            observed.append(report)
            return report

        monkeypatch.setattr(queued, 'reconcile_coordinator_queued_inputs', scan)
        current.tick()
        assert cursor.read_bytes() != before
        assert observed[0]['deferred'] >= 1


def test_repair_throttles_are_independent_and_preserve_first_full_terminal_pass(tmp_path, monkeypatch):
    with pump_case(tmp_path, monkeypatch) as (_old, _control, current):
        events = observe_repairs(monkeypatch)
        current.tick()
        assert [row[0] for row in events] == ['dispatch', 'recover', 'project']
        assert events[-1][1]['full_scan'] is True and events[-1][1]['limit'] == 16
        events.clear()
        current.tick()
        assert [row[0] for row in events] == ['dispatch']
        current._next_terminal_projection_at = 0
        events.clear()
        current.tick()
        assert [row[0] for row in events] == ['dispatch', 'project']
        assert events[-1][1]['full_scan'] is False


def test_escaped_typed_repair_error_keeps_original_identity_and_other_segment_runs(tmp_path, monkeypatch):
    with pump_case(tmp_path, monkeypatch) as (old, control, current):
        error = RequestExecutionOutcomeUncertainError(read_json_file(old.claim.path), 'synthetic-original-unknown')
        caught = []
        original_note = loops._note_loop_tick_error

        def unavailable(*_args, **_kwargs):
            raise error

        def note(site, actual, backoff):
            caught.append(actual)
            return original_note(site, actual, backoff)

        monkeypatch.setattr(recovery, 'recover_gateway_processing_requests_report', unavailable)
        monkeypatch.setattr(loops, '_note_loop_tick_error', note)
        current.tick()
        assert caught == [error] and caught[0] is error
        assert current._last_projection_report == (0, [])
        assert current._recovery_backoff.consecutive_failures == 1
        assert current.backoff.consecutive_failures == 0 and not current.tick_failed
        assert old.claim.path.exists() and _read_reservation(reservation_file(control.paths, old.claim.request_id))['state'] == 'occupied'


def test_source_closing_after_dispatch_refuses_repairs_before_any_recovery_write(tmp_path, monkeypatch):
    with pump_case(tmp_path, monkeypatch) as (old, control, current):
        original = module.dispatch_pending_requests
        before = old.claim.path.read_bytes()

        def close(*args, **kwargs):
            value = original(*args, **kwargs)
            current.runtime.stop_producers()
            return value

        monkeypatch.setattr(module, 'dispatch_pending_requests', close)
        with pytest.raises(ValueError):
            current.tick()
        assert old.claim.path.read_bytes() == before
        assert _read_reservation(reservation_file(control.paths, old.claim.request_id))['state'] == 'occupied'
