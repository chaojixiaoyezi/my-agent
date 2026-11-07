"""单进程迁移使用真实旧记录生产者、真实文件移动和生命周期锁。"""
import json
import os
from argparse import Namespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts.legacy_mp_migration import run_single_process_migration
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.standalone_sources import StandaloneSourceUnproven
from agent_py_agent.cli import gateway_coordinated_run, gateway_process


def legacy_home(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_publish import (
        publish_worker_registry,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
        _publish_generation_once,
    )
    from agent_py_agent.cli.gateway_worker_runtime import worker_process_identity

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    identity = worker_process_identity(os.getpid())
    workers = [{'worker': index, 'instance_id': f'old-{index}',
                'process_identity': {**identity, 'pid': 900000000 + index},
                'executor_ledger_ref': f'workers/executors/w{index}'} for index in range(2)]
    publish_worker_registry(paths.root, {'schema_version': 'multiprocess_worker_registry.v1',
        'c_run_id': 'old-c', 'worker_count': 2, 'process_identity': identity,
        'http_port': 43123, 'workers': workers}, None)
    _publish_generation_once(paths.root, {'schema_version': 'multiprocess_c_generation.v2',
        'c_run_id': 'old-c', 'worker_count': 2, 'closure': None})
    write_json_file_atomic(paths.state, {'c_run_id': 'old-c', 'worker_count': 2,
        'status': 'killed', 'pid': identity['pid']})
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    return paths


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_clean_home_has_complete_named_journal_and_repeat_is_noop(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    result = run_single_process_migration(paths, dry_run=False)
    assert result is not None and result['state'] == 'complete'
    assert [phase['name'] for phase in result['phases']] == [
        'drain', 'prove-exit', 'retire-generations', 'close-out-unroutable', 'archive']
    before = files(paths.root)
    assert run_single_process_migration(paths, dry_run=False) == result
    assert files(paths.root) == before


def test_dry_run_writes_nothing_even_for_missing_root(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'absent')
    result = run_single_process_migration(paths, dry_run=True)
    assert result is not None and result['dry_run'] is True
    assert not paths.root.exists()


def test_legacy_generation_is_archived_with_original_bytes(tmp_path, monkeypatch):
    paths = legacy_home(tmp_path, monkeypatch)
    before = files(paths.root)
    result = run_single_process_migration(paths, dry_run=False)
    assert result is not None and result['state'] == 'complete'
    archive = paths.root / result['archive']
    for name in result['sources']:
        assert (archive / name).read_bytes() == before[name]
        assert not (paths.root / name).exists()
    assert json.loads((archive / 'manifest.json').read_text())['moved'] == result['sources']


@pytest.mark.parametrize('live', [True, None])
def test_live_or_unknown_worker_refuses_before_moving(tmp_path, monkeypatch, live):
    paths = legacy_home(tmp_path, monkeypatch)
    before = files(paths.root)
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live',
                        lambda identity: live if identity and identity['pid'] == 900000000 else False)
    with pytest.raises(StandaloneSourceUnproven):
        run_single_process_migration(paths, dry_run=False)
    assert all((paths.root / name).read_bytes() == raw for name, raw in before.items())


def test_positive_config_runs_journal_then_single_process_not_workers(tmp_path, monkeypatch):
    paths = legacy_home(tmp_path, monkeypatch)
    config = tmp_path / 'agent.yaml'
    config.write_text(f'model_backend: echo\nmy_agent_home: "{tmp_path / "home"}"\ngateway_worker_process_count: 2\n')
    monkeypatch.setattr(gateway_coordinated_run, '_cold_gateway_paths', lambda _: paths)
    monkeypatch.setattr(gateway_process, 'mark_hosting_gateway_process', lambda: None)
    monkeypatch.setattr(gateway_process, 'wait_for_predecessor_exit', lambda _: True)
    workers = Mock(side_effect=AssertionError('retired topology must not start workers'))
    monkeypatch.setattr(gateway_coordinated_run, 'run_coordinated_gateway', workers)
    observed = []
    def serve(args, lifecycle):
        lifecycle.require_held()
        assert args._gateway_worker_count == 0
        assert args._gateway_frozen_config.gateway_worker_process_count == 0
        observed.append(files(paths.root))
        return 17
    monkeypatch.setattr(gateway_process, '_run_standalone_gateway', serve)
    assert gateway_process.cmd_gateway_run(Namespace(config=config)) == 17
    assert observed and not paths.state.exists()
    workers.assert_not_called()


@pytest.mark.parametrize('phase', ['drain', 'prove-exit', 'retire-generations', 'close-out-unroutable', 'archive'])
def test_crash_after_each_phase_commit_resumes_without_losing_sources(tmp_path, monkeypatch, phase):
    from agent_py_agent.agent.gateway_parts.legacy_mp_migration import journal

    paths = legacy_home(tmp_path, monkeypatch)
    original = journal._write_journal
    def interrupt(paths, row):
        original(paths, row)
        if row['phases'] and row['phases'][-1]['name'] == phase:
            raise OSError('phase_commit_interrupted')
    monkeypatch.setattr(journal, '_write_journal', interrupt)
    with pytest.raises(OSError, match='phase_commit_interrupted'):
        run_single_process_migration(paths, dry_run=False)
    monkeypatch.setattr(journal, '_write_journal', original)
    result = run_single_process_migration(paths, dry_run=False)
    assert result['state'] == 'complete'
    assert len(result['phases']) == len({p['name'] for p in result['phases']}) == 5
    assert result['moved'] == result['sources']


@pytest.mark.parametrize('boundary', ['move', 'manifest'])
def test_move_or_manifest_interruption_keeps_same_archive_and_original_bytes(tmp_path, monkeypatch, boundary):
    from agent_py_agent.agent.gateway_parts import multiprocess_generation_retirement as old

    paths = legacy_home(tmp_path, monkeypatch)
    before = files(paths.root)
    original = old.os.replace if boundary == 'move' else old._write_manifest
    fired = []
    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        selected = (kwargs.get('src_dir_fd') is not None and args[0] == args[1]) if boundary == 'move' else bool(args[1]['moved'])
        if selected and not fired:
            fired.append(True)
            raise OSError('retirement_commit_interrupted')
        return result
    monkeypatch.setattr(old.os if boundary == 'move' else old,
                        'replace' if boundary == 'move' else '_write_manifest', interrupted)
    with pytest.raises(OSError, match='retirement_commit_interrupted'):
        run_single_process_migration(paths, dry_run=False)
    monkeypatch.setattr(old.os if boundary == 'move' else old,
                        'replace' if boundary == 'move' else '_write_manifest', original)
    result = run_single_process_migration(paths, dry_run=False)
    assert fired and result['state'] == 'complete'
    assert len(list((paths.root / 'multiprocess_retired').iterdir())) == 1
    for name in result['sources']:
        assert (paths.root / result['archive'] / name).read_bytes() == before[name]


def test_empty_remaining_directory_is_not_completion_proof(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.legacy_mp_migration import journal

    paths = legacy_home(tmp_path, monkeypatch)
    original = journal._write_journal
    def interrupt(paths, row):
        original(paths, row)
        if row['phases'] and row['phases'][-1]['name'] == 'prove-exit':
            raise OSError('pause_before_moves')
    monkeypatch.setattr(journal, '_write_journal', interrupt)
    with pytest.raises(OSError):
        run_single_process_migration(paths, dry_run=False)
    row = json.loads((paths.root / 'single_process_migration.json').read_text())
    for name in row['sources']:
        (paths.root / name).unlink()
    monkeypatch.setattr(journal, '_write_journal', original)
    with pytest.raises(StandaloneSourceUnproven, match='migration_source_missing'):
        run_single_process_migration(paths, dry_run=False)


def test_unsettled_cancellation_is_preserved_and_refused(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    paths = legacy_home(tmp_path, monkeypatch)
    _request_id, path, _response = submit_gateway_ask(paths, params=GatewayAskParams(prompt='prior pending', channel='chat'))
    row = json.loads(path.read_text())
    row.update(route={'c_run_id': 'old-c'}, cancellation_control={'state': 'pending'})
    write_json_file_atomic(path, row)
    before = files(paths.root)
    with pytest.raises(StandaloneSourceUnproven, match='unsettled'):
        run_single_process_migration(paths, dry_run=False)
    assert files(paths.root) == before


def legacy_request(paths):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    request_id, path, response = submit_gateway_ask(paths, params=GatewayAskParams(prompt='old routed turn'))
    row = json.loads(path.read_text())
    row['route'] = {'c_run_id': 'old-c'}
    write_json_file_atomic(path, row)
    return request_id, path, response


def test_terminal_commit_then_projection_crash_repairs_without_reexecuting(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import io

    paths = legacy_home(tmp_path, monkeypatch)
    request_id, path, response = legacy_request(paths)
    original = io.write_json_file_atomic
    def fail_response(target, *args, **kwargs):
        if target == response:
            raise OSError('response_projection_interrupted')
        return original(target, *args, **kwargs)
    monkeypatch.setattr(io, 'write_json_file_atomic', fail_response)
    with pytest.raises(OSError, match='response_projection_interrupted'):
        run_single_process_migration(paths, dry_run=False)
    assert not path.exists() and not response.exists()
    canonical = paths.terminal / (request_id + '.json')
    committed = canonical.read_bytes()
    monkeypatch.setattr(io, 'write_json_file_atomic', original)
    assert run_single_process_migration(paths, dry_run=False)['state'] == 'complete'
    assert response.exists()
    assert json.loads(response.read_text())['error_code'] == 'GATEWAY_REQUEST_ROUTE_RETIRED'
    assert canonical.read_bytes() == committed


def test_count0_start_refuses_incomplete_migration_before_business(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    write_json_file_atomic(paths.root / 'single_process_migration.json', {
        'schema': 'gateway_single_process_migration.v1', 'state': 'running', 'phases': []})
    config = tmp_path / 'agent.yaml'
    config.write_text(f'model_backend: echo\nmy_agent_home: "{tmp_path / "home"}"\ngateway_worker_process_count: 0\n')
    monkeypatch.setattr(gateway_coordinated_run, '_cold_gateway_paths', lambda _: paths)
    monkeypatch.setattr(gateway_process, 'mark_hosting_gateway_process', lambda: None)
    monkeypatch.setattr(gateway_process, 'wait_for_predecessor_exit', lambda _: True)
    business = Mock(return_value=91)
    monkeypatch.setattr(gateway_process, '_run_standalone_gateway', business)
    assert gateway_process.cmd_gateway_run(Namespace(config=config)) == 2
    business.assert_not_called()
