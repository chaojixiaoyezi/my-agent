"""有界管理诊断分页：真实文件、游标、脱敏与只读行为。"""
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.store_layout import ConversationStorage
from agent_py_agent.agent.conversation.store_wake_attempts import WakeAttemptStore
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.status_rendering import gateway_log_page


def test_forward_pages_reach_history_and_preserve_line_cap(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    expected = [f'2026-11-16 12:00:00 row {n}' for n in range(2000)]
    paths.log.write_text('\n'.join(expected) + '\n')
    cursor, observed = '', []
    for _ in range(1000):
        page = gateway_log_page(paths, {}, log_scope='full_file', log_offset_bytes=0,
                                log_cursor=cursor, line_limit=3)
        assert page['sampled_bytes'] <= 32768
        observed.extend(page['sources']['gateway']['lines'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert observed == expected


def test_time_window_is_partial_and_advances_without_matching_scan(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    paths.log.write_text(('2026-11-16 12:00:00 old\n' * 5000) + '2026-11-17 12:00:00 wanted\n')
    page = gateway_log_page(paths, {}, log_scope='full_file', log_since=1794916800.0, log_query='wanted')
    assert page['sampled_bytes'] <= 32768 and page['coverage'] == 'partial'
    assert page['sources']['gateway']['lines'] == []
    assert page['next_cursor']


@pytest.mark.parametrize('change', ['replace', 'truncate', 'regrow'])
def test_cursor_rejects_rotation_and_truncation(tmp_path, change):
    paths = gateway_paths_from_root(tmp_path)
    paths.log.write_text('first\nsecond\n')
    page = gateway_log_page(paths, {}, log_scope='full_file', log_offset_bytes=0, line_limit=1)
    if change == 'replace':
        paths.log.rename(tmp_path / 'sealed')
    paths.log.write_text('new\n' * (10 if change == 'regrow' else 1))
    resumed = gateway_log_page(paths, {}, log_scope='full_file', log_cursor=page['next_cursor'])
    assert resumed['sources']['gateway']['rotated']
    assert resumed['sources']['gateway']['lines'] == []


def test_filters_are_bound_and_both_sources_share_budget(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    paths.log.write_text('safe\n' * 10000)
    (tmp_path / 'supervisor.log').write_text('safe\n' * 10000)
    page = gateway_log_page(paths, {}, log_scope='full_file', log_source='both', log_offset_bytes=0)
    assert page['sampled_bytes'] <= 32768
    wrong = gateway_log_page(paths, {}, log_scope='full_file', log_source='both', log_cursor=page['next_cursor'], log_query='different')
    assert wrong['error_code'] == 'LOG_CURSOR_INVALID'


def test_partial_and_pem_lines_never_leak_and_continue(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    secret = 'super-secret-private-material'
    paths.log.write_text('-----BEGIN PRIVATE KEY-----\n' + secret + '\n-----END PRIVATE KEY-----\nafter\n')
    cursor, lines = '', []
    for _ in range(10):
        page = gateway_log_page(paths, {}, log_scope='full_file', log_cursor=cursor, log_offset_bytes=0, byte_limit=32)
        lines.extend(page['sources']['gateway']['lines'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert secret not in '\n'.join(lines)
    assert 'after' in lines


def test_quarantine_one_directory_page_no_convenience_scan(tmp_path, monkeypatch):
    storage = ConversationStorage(tmp_path)
    storage.wake_quarantine_dir.mkdir(parents=True, exist_ok=True)
    store = WakeAttemptStore(storage, find_pending_path=lambda _: None, mark_observations_handled=lambda *a, **k: None)
    for n in range(150):
        (storage.wake_quarantine_dir / f'w{n}.json').write_text(json.dumps({'wake_signal_id': f'w{n}', 'summary': 'private body', 'quarantine': {}}))
    monkeypatch.setattr(store, 'quarantined', lambda: pytest.fail('full scan forbidden'))
    ids, cursor = set(), ''
    for _ in range(100):
        page = store.quarantined_page(limit=7, cursor=cursor)
        assert page['visited'] <= 7 and len(page['items']) <= 7
        assert 'private body' not in json.dumps(page)
        ids.update(row['wake_signal_id'] for row in page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert len(ids) == 150


def test_quarantine_bad_and_oversize_records_are_explicit(tmp_path):
    storage = ConversationStorage(tmp_path)
    storage.wake_quarantine_dir.mkdir(parents=True, exist_ok=True)
    store = WakeAttemptStore(storage, find_pending_path=lambda _: None, mark_observations_handled=lambda *a, **k: None)
    (storage.wake_quarantine_dir / 'bad.json').write_text('{')
    (storage.wake_quarantine_dir / 'large.json').write_text('x' * 100000)
    page = store.quarantined_page(limit=100)
    assert page['unreadable'] == 2 and page['coverage'] == 'partial'
    assert page['sampled_bytes'] <= 32768


def test_gateway_queue_page_real_tool_preserves_scope_and_typed_reasons(tmp_path, monkeypatch):
    import os
    import time

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.tooling.gateway_status import GatewayStatusTool
    from agent_py_agent.agent.user_space.owner_admin_controls import set_owner_admin_controls

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path / 'project')
    paths = gateway_paths(agent)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    for request_id, owner in [('own-request', 'local/main'), ('other-request', 'providers/feishu/users/other')]:
        target = paths.inbox / (request_id + '.json')
        target.write_text(json.dumps({'id': request_id, 'status': 'pending', 'owner_id': owner,
            'goal': 'do-not-expose-body', 'conversation_claim': {'thread_id': request_id + '-thread'}}))
        os.utime(target, (time.time() - 86400, time.time() - 86400))
    tool = GatewayStatusTool(agent)
    own = json.loads(tool.execute({'include_log_diagnostics': False, 'include_queue_entries': True, 'queue_limit': 1}).output)
    rows, cursor = own['queue_entries']['entries'], own['queue_entries']['next_cursor']
    while cursor:
        result = json.loads(tool.execute({'include_log_diagnostics': False, 'include_queue_entries': True,
            'queue_limit': 1, 'queue_cursor': cursor}).output)
        rows.extend(result['queue_entries']['entries'])
        cursor = result['queue_entries']['next_cursor']
    assert {row['request_id'] for row in rows} == {'own-request'}
    assert rows[0]['wait_reason'] == 'no_terminal_proof' and rows[0]['orphan_reason'] == ''
    assert rows[0]['state'] == 'pending'
    assert rows[0]['age_seconds'] >= 86000
    assert 'do-not-expose-body' not in json.dumps(own)
    denied = json.loads(tool.execute({'include_log_diagnostics': False, 'include_queue_entries': True,
        'queue_scope': 'all_owners'}).output)
    assert denied['queue_entries']['error_code'] == 'AUDIT_ACCESS_DENIED'
    set_owner_admin_controls(agent.home_paths, agent.home_paths, {'cross_owner_audit_allowed': True}, actor='test')
    allowed = json.loads(tool.execute({'metadata_only': True, 'include_queue_entries': True,
        'queue_scope': 'all_owners'}).output)
    assert {row['request_id'] for row in allowed['queue_entries']['entries']} == {'own-request', 'other-request'}
    assert 'do-not-expose-body' not in json.dumps(allowed)


def test_quarantine_byte_budget_preserves_unread_rows(tmp_path):
    storage = ConversationStorage(tmp_path)
    storage.wake_quarantine_dir.mkdir(parents=True, exist_ok=True)
    store = WakeAttemptStore(storage, find_pending_path=lambda _: None, mark_observations_handled=lambda *a, **k: None)
    for n in range(8):
        (storage.wake_quarantine_dir / f'w{n}.json').write_text(json.dumps({'wake_signal_id': f'w{n}', 'summary': 'x' * 15000}))
    ids, cursor = set(), ''
    for _ in range(20):
        page = store.quarantined_page(limit=7, cursor=cursor)
        assert page['sampled_bytes'] <= 32768 and page['unreadable'] == 0
        ids.update(row['wake_signal_id'] for row in page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert len(ids) == 8


def test_page_completion_preserves_prior_coverage_gaps(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    paths.log.write_text('no timestamp\n2026-11-16 12:00:00 visible\n')
    cursor, last = '', None
    for _ in range(10):
        last = gateway_log_page(paths, {}, log_scope='full_file', log_since=1, log_offset_bytes=0,
                                log_cursor=cursor, byte_limit=40, line_limit=1)
        cursor = last['next_cursor']
        if not cursor:
            break
    assert last['coverage'] == 'partial'
    assert last['sources']['gateway']['ambiguous_lines_in_window'] == 1
    storage = ConversationStorage(tmp_path / 'conversation')
    storage.wake_quarantine_dir.mkdir(parents=True, exist_ok=True)
    (storage.wake_quarantine_dir / 'bad.json').write_text('{')
    store = WakeAttemptStore(storage, find_pending_path=lambda _: None, mark_observations_handled=lambda *a, **k: None)
    cursor = ''
    for _ in range(10):
        page = store.quarantined_page(limit=1, cursor=cursor)
        cursor = page['next_cursor']
        if not cursor:
            break
    assert page['coverage'] == 'partial' and page['unreadable_in_window'] == 1
