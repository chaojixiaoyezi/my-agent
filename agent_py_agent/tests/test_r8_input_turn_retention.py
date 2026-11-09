"""输入引用终态、原锁、出生交错和有界目录预算。"""
import hashlib
import os
import threading
import time
from dataclasses import replace

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts.input_turn_retention import retire_turn_refs_slice
from agent_py_agent.agent.gateway_parts.io import locked_file_transition, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


def _case(tmp_path, *, state='consumed', terminal=True):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    request, turn = 'input-001', 'turn-001'
    prepared = {'id': request, 'kind': 'ask', 'goal': 'test input', 'metadata': {'client_input_digest': 'test-digest'}}
    receipt = delivery.GatewayInputReceipt(request_id=request, client_input_digest=delivery._prepared_request_digest(prepared),
        client_message_id=request, state=state, prepared_request=prepared,
        prepared_payload_digest=delivery._prepared_payload_digest(prepared), guidance_dedupe_key=request,
        target_turn_id=turn, updated_at=time.time() - 10 * 86400)
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, request), receipt.to_dict())
    delivery._write_active_turn_ref(paths, receipt)
    ref = paths.root / 'input_receipts/by_turn' / hashlib.sha256(turn.encode()).hexdigest() / (hashlib.sha256(request.encode()).hexdigest() + '.json')
    os.utime(str(ref) + '.lock', (1, 1))
    if terminal:
        write_json_file_atomic(paths.terminal / (turn + '.json'), {'schema_version': 'gateway_terminal_request.v1',
            'id': turn, 'turn_phase': 'closed', 'status': 'done', 'completed_at': time.time() - 10 * 86400})
    return paths, receipt, ref


def _sweep(paths):
    for _ in range(100):
        result = retire_turn_refs_slice(paths, before=time.time() - 7 * 86400, max_entries=3, max_ms=100)
        assert result['scanned_entries'] <= 3
        assert not result['errors'], result
        if result['complete']:
            return
    raise AssertionError('scan did not finish')


def test_consumed_closed_ref_and_orphan_lock_retire_but_canonical_remains(tmp_path):
    paths, receipt, ref = _case(tmp_path)
    _sweep(paths)
    assert not ref.exists()
    assert not ref.with_name(ref.name + '.lock').exists()
    assert delivery.read_gateway_input_receipt(paths, receipt.request_id) == receipt


def test_consumed_without_terminal_proof_protects_long_background_turn(tmp_path):
    paths, _, ref = _case(tmp_path, terminal=False)
    _sweep(paths)
    assert ref.exists()
    assert ref.with_name(ref.name + '.lock').exists()


def test_active_input_or_processing_turn_is_protected(tmp_path):
    paths, receipt, ref = _case(tmp_path, state='pending')
    _sweep(paths)
    assert ref.exists()
    write_json_file_atomic(delivery.gateway_input_receipt_path(paths, receipt.request_id), replace(receipt, state='consumed').to_dict())
    write_json_file_atomic(paths.processing / (receipt.target_turn_id + '.json'), {'id': receipt.target_turn_id})
    _sweep(paths)
    assert ref.exists()


def test_active_ref_lock_cannot_be_removed_then_retry_retires(tmp_path):
    paths, _, ref = _case(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def writer():
        with locked_file_transition(ref):
            entered.set()
            assert release.wait(5)
    thread = threading.Thread(target=writer)
    thread.start()
    assert entered.wait(5)
    try:
        _sweep(paths)
        assert ref.exists()
        assert ref.with_name(ref.name + '.lock').exists()
    finally:
        release.set()
        thread.join(5)
    _sweep(paths)
    assert not ref.exists()


def test_empty_directory_retirement_serializes_with_ref_birth(tmp_path, monkeypatch):
    paths, receipt, ref = _case(tmp_path, terminal=False)
    ref.unlink()
    ref.with_name(ref.name + '.lock').unlink()
    entered, release = threading.Event(), threading.Event()
    original = delivery.read_json_file_report
    failures = []
    def paused(path, **kwargs):
        if path == ref and threading.current_thread().name == 'ref-writer':
            entered.set()
            assert release.wait(5)
        return original(path, **kwargs)
    monkeypatch.setattr(delivery, 'read_json_file_report', paused)
    def writer():
        try:
            delivery._write_active_turn_ref(paths, receipt)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=writer, name='ref-writer')
    thread.start()
    assert entered.wait(5)
    try:
        _sweep(paths)
        assert ref.parent.exists()
    finally:
        release.set()
        thread.join(5)
    assert not failures
    assert ref.exists()


def test_coordinator_lane_uses_each_original_write_callback(tmp_path):
    from agent_py_agent.agent.gateway_parts.input_turn_retention import (
        maintain_input_turn_refs_lane,
    )
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
    from agent_py_agent.agent.user_space.storage_lanes import (
        StorageLaneBudget,
        StorageLaneRequest,
        validate_lane_receipt,
    )
    home = home_paths(tmp_path / 'home')
    home.owner_home_dir.mkdir(parents=True)
    root = runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace']
    # 使用正式owner路径；只复用fixture的canonical生产者和ref writer。
    paths, receipt, old_ref = _case(tmp_path / 'seed')
    import shutil
    shutil.copytree(paths.root, root)
    commits = []
    def commit(operation):
        commits.append(operation)
        return operation()
    request = StorageLaneRequest('gateway_input_refs', home, time.time(), {}, StorageLaneBudget(128, 8 * 1048576, None),
        False, True, None, commit)
    result = validate_lane_receipt(maintain_input_turn_refs_lane(request))
    assert commits
    assert result['deleted_items'] == 1
    assert not (root / old_ref.relative_to(paths.root)).exists()
    held = replace(request, legal_hold=True)
    assert maintain_input_turn_refs_lane(held)['deferred_reason'] == 'legal_hold'
