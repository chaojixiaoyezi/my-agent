"""SCALE-FU-01: archived consumed input receipts leave the hot directory; every reader falls back to the archive."""

from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import control_service as controls
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import runtime_file_retention as retention
from agent_py_agent.agent.gateway_parts.http_handlers import http_input_identity
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.wait_acceptance import confirm_wait_acceptance
from agent_py_agent.tests.test_gateway_c_input_receipt_source import (
    arguments,
    run_receipt_work,
    upper,
)
from agent_py_agent.tests.test_gateway_http_current_work import http_case
from agent_py_agent.tests.test_gateway_prepared_route_immutability import (
    context as context,  # noqa: F401
)


def _seed(paths, key='input-a'):
    prepared = {'id': key, 'kind': 'ask', 'goal': 'original complete request', 'user_id': 'user-a',
                'conversation': {'channel_conversation_id': 'conversation-a'},
                'metadata': {'client_input_digest': 'digest-' + key, 'channel': 'chat'}}
    row = inputs.GatewayInputReceipt(key, 'digest-' + key, 'message-a', 'consumed', prepared,
                                     inputs._prepared_payload_digest(prepared), 'original-guidance-key', updated_at=1)
    write_json_file_atomic(inputs.gateway_input_receipt_path(paths, key), row.to_dict())
    return row


def _drain(paths):
    reports = []
    for _ in range(100):
        reports.append(retention.archive_terminal_slice(paths, directory='input_receipts', before=100,
                                                        max_entries=1, max_ms=1000))
        if reports[-1]['complete']:
            return reports
    pytest.fail('bounded input retention never completed')


def _prepare(paths, row, prepare=None):
    return inputs.load_or_prepare_gateway_input_locked(
        paths, request_id=row.request_id, client_input_digest=row.client_input_digest,
        client_message_id=row.client_message_id, guidance_dedupe_key=row.guidance_dedupe_key,
        prepared_request=row.prepared_request, prepare_request=prepare)


def test_verified_archive_removes_input_original_and_replay_reuses_archive(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    hot = inputs.gateway_input_receipt_path(paths, row.request_id)
    original = hot.read_bytes()
    reports = _drain(paths)
    assert not any(report['errors'] for report in reports)
    assert sum(report['archived_entries'] for report in reports) == 1
    assert not hot.exists()
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').read_bytes() == original
    assert inputs.read_gateway_input_receipt(paths, row.request_id) == row
    prepare = Mock(side_effect=AssertionError('archived input must never be prepared twice'))
    reused, created = _prepare(paths, row, prepare)
    assert reused == row and not created
    prepare.assert_not_called()
    with pytest.raises(ValueError):
        inputs.load_or_prepare_gateway_input_locked(
            paths, request_id=row.request_id, client_input_digest='another input',
            client_message_id=row.client_message_id, guidance_dedupe_key=row.guidance_dedupe_key,
            prepared_request=row.prepared_request)


def test_legacy_retained_original_is_removed_by_the_next_pass(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    hot = inputs.gateway_input_receipt_path(paths, row.request_id)
    original = hot.read_bytes()
    _drain(paths)
    if not hot.exists():
        # 旧版本只冷备份不删原件：复原同字节热件模拟存量。
        write_json_file_atomic(hot, row.to_dict())
    assert hot.read_bytes() == original
    reports = _drain(paths)
    assert not any(report['errors'] for report in reports)
    assert not hot.exists()
    assert inputs.read_gateway_input_receipt(paths, row.request_id) == row


def test_publication_failure_keeps_input_original(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    hot = inputs.gateway_input_receipt_path(paths, row.request_id)
    original = hot.read_bytes()

    def failed_write(*args, **kwargs):
        raise OSError('injected publication failure')

    monkeypatch.setattr(retention, 'write_bytes_atomic_beneath', failed_write)
    reports = _drain(paths)
    assert any(report['errors'] for report in reports)
    assert hot.read_bytes() == original
    assert inputs.read_gateway_input_receipt(paths, row.request_id) == row


def test_control_steer_after_cutover_keeps_the_hot_disposition(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    scope = controls.GatewayControlScope('user-a', 'chat', 'conversation-a', metadata={'message_id': 'message-a'})
    key, _ = http_input_identity(user_id=scope.user_id, channel=scope.channel,
                                 conversation_id=scope.conversation_id, message_id='message-a')
    _seed(paths, key)
    store = SimpleNamespace(guidance=SimpleNamespace(append_once=Mock()))
    before = controls._append_ordinary_input_guidance(store, paths, scope, {'message': 'supplement'}, 'turn-a', None)
    _drain(paths)
    inputs.gateway_input_receipt_path(paths, key).unlink(missing_ok=True)
    after = controls._append_ordinary_input_guidance(store, paths, scope, {'message': 'supplement'}, 'turn-a', None)
    store.guidance.append_once.assert_not_called()
    assert after.error_code == before.error_code and after.error_code


def test_wait_acceptance_confirms_archived_receipt_without_hot_file(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    row = _seed(paths)
    _drain(paths)
    hot = inputs.gateway_input_receipt_path(paths, row.request_id)
    hot.unlink(missing_ok=True)
    assert confirm_wait_acceptance(paths, row.request_id, {}, receipt=row) == row
    missing = replace(row, request_id='never-archived')
    with pytest.raises(FileNotFoundError):
        confirm_wait_acceptance(paths, missing.request_id, {}, receipt=missing)



def test_multiprocess_prepare_reuses_archived_receipt(context, monkeypatch):
    source_type = import_module('agent_py_agent.agent.gateway_parts.multiprocess_input_receipt').CoordinatorInputReceiptSource
    with http_case(context, monkeypatch) as (lower, runtime, _group, socket, handler):

        def finish():
            source = source_type(handler, upper(lower, runtime, context))
            receipt, created = source.prepare(inputs.GatewayInputPreparation(**arguments()))
            assert created
            consumed = replace(receipt, state='consumed', updated_at=1)
            hot = inputs.gateway_input_receipt_path(context.paths, receipt.request_id)
            write_json_file_atomic(hot, consumed.to_dict())
            assert not any(report['errors'] for report in _drain(context.paths))
            hot.unlink(missing_ok=True)
            replay, created = source.prepare(inputs.GatewayInputPreparation(**arguments()))
            assert not created and replay == consumed and not hot.exists()
            return replay

        assert run_receipt_work(lower, socket, finish).state == 'consumed'
