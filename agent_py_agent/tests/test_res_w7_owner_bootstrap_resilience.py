from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.common.json_io import JsonObjectReadReport
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler import creator_migration, recovery
from agent_py_agent.agent.scheduler.delivery_policy import (
    ScheduleDeliveryTargetUnauthorized,
    available_delivery_targets,
)
from agent_py_agent.agent.scheduler.repository import SchedulerStateError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space import capability_requests as requests
from agent_py_agent.agent.user_space import temporary_grants as grants
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaUnavailable


def _agent_case(tmp_path):
    home = ensure_my_agent_home(tmp_path / 'home')
    config = AgentConfig(my_agent_home=str(home.root), model_backend='echo',
        enable_tools=False, memory_path='memory.jsonl', orphan_supervision_interval_seconds=0)
    def construct():
        agent = SimpleAgent(config, tmp_path / 'workspace')
        # 仅模型使用本地 echo，启动与完整运行入口仍走真实实现。
        agent.backend = EchoBackend()
        return agent
    return home, construct


def _assert_deferred(caplog, error_name):
    messages = [record.getMessage() for record in caplog.records
        if 'SCHEDULER_STARTUP_MIGRATION_DEFERRED' in record.getMessage()]
    assert len(messages) == 2
    assert any('phase=aliases' in message for message in messages)
    assert any('phase=prepare' in message for message in messages)
    assert all(f'error={error_name}' in message for message in messages)


@pytest.mark.parametrize('corruption', ['{', '[]', 'directory'])
def test_bad_scheduler_store_does_not_block_agent_or_chat(tmp_path, caplog, corruption):
    home, construct = _agent_case(tmp_path)
    path = home.owner_scheduler_dir / 'store.json'
    if corruption == 'directory':
        path.mkdir()
    else:
        path.write_text(corruption, encoding='utf-8')
    with caplog.at_level(logging.WARNING):
        agent = construct()
    _assert_deferred(caplog, 'SchedulerStateError')
    result = agent.run('resilience chat still works', save=False)
    assert 'resilience chat still works' in result.response
    with pytest.raises(SchedulerStateError):
        agent.scheduler_repository.list_jobs()
    assert path.is_dir() if corruption == 'directory' else path.read_text(encoding='utf-8') == corruption


def test_bad_quota_preserves_write_refusal_and_original_bytes(tmp_path, caplog):
    home, construct = _agent_case(tmp_path)
    home.owner_quota_json.write_text('{', encoding='utf-8')
    with caplog.at_level(logging.WARNING):
        agent = construct()
    assert 'phase=aliases error=OwnerQuotaUnavailable' in caplog.text
    with pytest.raises(OwnerQuotaUnavailable):
        with agent.owner_quota.admission():
            pass
    assert home.owner_quota_json.read_text(encoding='utf-8') == '{'
    assert not (home.owner_scheduler_dir / 'store.json').exists()


def test_next_construction_retries_after_operator_removes_bad_store(tmp_path, caplog):
    home, construct = _agent_case(tmp_path)
    path = home.owner_scheduler_dir / 'store.json'
    path.write_text('{', encoding='utf-8')
    with caplog.at_level(logging.WARNING):
        construct()
    _assert_deferred(caplog, 'SchedulerStateError')
    path.unlink()
    caplog.clear()
    agent = construct()
    assert agent.scheduler_repository.list_jobs() == ([], [])
    assert not any('SCHEDULER_STARTUP_MIGRATION_DEFERRED' in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize('exception', [SchedulerStateError('private text'), OwnerQuotaUnavailable('private text')])
def test_prepare_contains_recovery_error_after_creator_migration(monkeypatch, caplog, exception):
    migrate = Mock()
    monkeypatch.setattr(creator_migration, 'migrate_monitor_creators', migrate)
    repository = SimpleNamespace(recover_interrupted_executions=Mock(side_effect=exception))
    with caplog.at_level(logging.WARNING):
        recovery.prepare_repository(repository, object(), now=123)
    migrate.assert_called_once()
    repository.recover_interrupted_executions.assert_called_once_with(now=123)
    assert 'phase=prepare' in caplog.text and type(exception).__name__ in caplog.text
    assert 'private text' not in caplog.text


def test_unrelated_bootstrap_exception_propagates(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler.repository import SchedulerRepository

    _, construct = _agent_case(tmp_path)
    monkeypatch.setattr(SchedulerRepository, 'migrate_monitor_aliases', Mock(side_effect=RuntimeError('unexpected')))
    with pytest.raises(RuntimeError, match='unexpected'):
        construct()


def test_unrelated_prepare_exception_propagates(monkeypatch):
    monkeypatch.setattr(creator_migration, 'migrate_monitor_creators', Mock())
    repository = SimpleNamespace(recover_interrupted_executions=Mock(side_effect=RuntimeError('unexpected')))
    with pytest.raises(RuntimeError, match='unexpected'):
        recovery.prepare_repository(repository, object(), now=123)


def _delivery_case(tmp_path):
    _, construct = _agent_case(tmp_path)
    agent = construct()
    fields = {'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir),
        'canonical_user_id': agent.home_paths.owner_id, 'channel': 'chat', 'channel_user_id': 'local'}
    threads = [agent.conversation_store.threads.get_or_create({**fields, 'channel_conversation_id': name})
        for name in ['healthy-one', 'broken', 'healthy-two']]
    return agent, threads


def test_one_unreadable_thread_keeps_healthy_targets(tmp_path, caplog):
    agent, threads = _delivery_case(tmp_path)
    broken = threads[1]
    agent.conversation_store.storage.thread_path(broken.thread_id).write_text('{', encoding='utf-8')
    _, errors = agent.conversation_store.threads.list_report(limit=0)
    assert errors and errors[0]['thread_id'] == broken.thread_id
    with caplog.at_level(logging.WARNING):
        targets = available_delivery_targets(agent)
    assert {target['thread_id'] for target in targets} == {threads[0].thread_id, threads[2].thread_id}
    assert 'SCHEDULE_TARGET_THREAD_SKIPPED' in caplog.text and broken.thread_id in caplog.text


def test_thread_error_cannot_leave_same_thread_in_delivery_targets(tmp_path, monkeypatch):
    agent, threads = _delivery_case(tmp_path)
    monkeypatch.setattr(agent.conversation_store.threads, 'list_report',
        lambda **_kwargs: (threads, [{'thread_id': threads[1].thread_id}]))
    assert threads[1].thread_id not in {row['thread_id'] for row in available_delivery_targets(agent)}


def test_global_thread_index_failure_still_refuses_delivery(tmp_path, monkeypatch):
    agent, threads = _delivery_case(tmp_path)
    monkeypatch.setattr(agent.conversation_store.threads, 'list_report',
        lambda **_kwargs: (threads, [{'thread_id': threads[1].thread_id}, {'context': 'thread-index'}]))
    with pytest.raises(ScheduleDeliveryTargetUnauthorized):
        available_delivery_targets(agent)


def _grant(home):
    return grants.create_temporary_grant(home, grants.CreateTemporaryGrant(
        granted_to='synthetic-agent', capability='synthetic-capability', path_prefix='',
        expires_at='2000-01-01T00:00:00+00:00'))


def _request(home):
    return requests.create_capability_request(home, requests.CreateCapabilityRequest(
        requested_by='synthetic-agent', capability='synthetic-capability', reason='synthetic',
        expires_at='2000-01-01T00:00:00+00:00'))


def test_revoked_grant_cannot_be_revived_by_operator_backups(tmp_path, monkeypatch, caplog):
    home = ensure_my_agent_home(tmp_path / 'home')
    grant = _grant(home)
    active = grant.path.read_text(encoding='utf-8')
    copies = [grant.path.with_name(f'{grant.grant_id}{suffix}.json') for suffix in [' copy', '.bak']]
    for path in copies:
        path.write_text(active, encoding='utf-8')
    payload = json.loads(active)
    payload['status'] = 'revoked'
    grant.path.write_text(json.dumps(payload), encoding='utf-8')
    reader = Mock(wraps=grants._read_payload_report)
    monkeypatch.setattr(grants, '_read_payload_report', reader)
    with caplog.at_level(logging.DEBUG):
        assert not grants.has_active_capability_grant(home.owner_temporary_grants_dir,
            'synthetic-capability', now='1999-01-01T00:00:00+00:00')
    assert [call.args[0] for call in reader.call_args_list] == [grant.path]
    assert all(path.read_text(encoding='utf-8') == active for path in copies)
    assert any(record.levelno == logging.DEBUG for record in caplog.records)


def test_copied_capability_request_is_not_listed_or_read(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path / 'home')
    request = _request(home)
    for suffix in [' copy', '.bak']:
        request.path.with_name(f'{request.request_id}{suffix}.json').write_bytes(request.path.read_bytes())
    reader = Mock(wraps=requests._read_payload_report)
    monkeypatch.setattr(requests, '_read_payload_report', reader)
    assert [row.request_id for row in requests.list_capability_requests_report(home).requests] == [request.request_id]
    assert [call.args[0] for call in reader.call_args_list] == [request.path]


@pytest.mark.parametrize('kind', ['grant', 'request'])
def test_expiry_scans_never_read_or_modify_noncanonical_copies(tmp_path, monkeypatch, kind):
    home = ensure_my_agent_home(tmp_path / 'home')
    module = grants if kind == 'grant' else requests
    record = _grant(home) if kind == 'grant' else _request(home)
    backup = record.path.with_name(f'{record.path.stem}.bak.json')
    original = record.path.read_bytes()
    backup.write_bytes(original)
    reader = Mock(wraps=module._read_payload_report)
    monkeypatch.setattr(module, '_read_payload_report', reader)
    expired = (grants.expire_temporary_grants if kind == 'grant' else requests.expire_capability_requests)(
        home, now='2020-01-01T00:00:00+00:00')
    assert len(expired) == 1 and expired[0].path == record.path
    assert [call.args[0] for call in reader.call_args_list] == [record.path]
    assert backup.read_bytes() == original


@pytest.mark.parametrize('kind', ['grant', 'request'])
def test_file_disappearing_between_glob_and_read_has_no_phantom_record(tmp_path, monkeypatch, kind):
    home = ensure_my_agent_home(tmp_path / 'home')
    if kind == 'grant':
        _grant(home)
        monkeypatch.setattr(grants, '_read_payload_report', lambda _path: JsonObjectReadReport({}))
        report = grants.list_temporary_grants_report(home)
        assert report.grants == [] and report.load_errors == []
        assert not grants.has_active_capability_grant(home.owner_temporary_grants_dir, 'synthetic-capability')
    else:
        _request(home)
        monkeypatch.setattr(requests, '_read_payload_report', lambda _path: ({}, None))
        report = requests.list_capability_requests_report(home)
        assert report.requests == [] and report.load_errors == []


@pytest.mark.parametrize('kind', ['grant', 'request'])
@pytest.mark.parametrize('corruption', ['{', 'directory'])
def test_broken_canonical_records_still_report_errors(tmp_path, kind, corruption):
    home = ensure_my_agent_home(tmp_path / 'home')
    record = _grant(home) if kind == 'grant' else _request(home)
    if corruption == 'directory':
        record.path.unlink()
        record.path.mkdir()
    else:
        record.path.write_text(corruption, encoding='utf-8')
    report = (grants.list_temporary_grants_report if kind == 'grant' else requests.list_capability_requests_report)(home)
    assert report.load_errors and report.load_errors[0]['path'] == str(record.path)
    assert (report.grants if kind == 'grant' else report.requests) == []
