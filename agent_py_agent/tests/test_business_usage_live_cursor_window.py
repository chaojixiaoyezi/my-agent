"""读usage自身写账与并发追加不摧毁有界读窗；保留权限、源缺失和轮转边界。"""
import json

import pytest

from agent_py_agent.agent.capability.business_capabilities import BusinessCapabilityService
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.tests.test_business_usage_bounded_paging import _operations, _setup, registry

_FIXTURES = (registry,)


def test_self_observation_writes_and_skill_append_do_not_expand_or_break_window(tmp_path, registry):
    host, context, calls, repo = _setup(tmp_path, registry)
    store = SkillLearningStore.for_home(host.home_paths)
    for _ in range(3):
        store.record_skill_usage(event='manual_get', skill_id='owner:method', source='skill_search', task_id=context.task_id)
    expected = _operations(repo, context, 7)
    run = repo.agent_run_for_run_id('run-' + context.task_id)
    service = BusinessCapabilityService(host)
    request = {'space_id': context.space_id, 'limit': 2}
    page = service.execute('spaces.usage.list', request, context=context)
    seen, cursor, pages = list(page['items']), page['next_cursor'], 1
    store.record_skill_usage(event='manual_get', skill_id='owner:new-after-cutoff', source='skill_search', task_id=context.task_id)
    repo.mark_operation_executing(expected[-1])
    while cursor:
        repo.create_tool_operation(agent_run_id=run['agent_run_id'], attempt_id=run['current_attempt_id'],
            operation_type='business_spaces_usage_list', canonical_scope='owner')
        before = len(calls)
        page = service.execute('spaces.usage.list', {**request, 'cursor': cursor}, context=context)
        assert len(calls) - before <= 1
        assert len(page['items']) <= 2
        seen.extend(page['items'])
        cursor = page['next_cursor']
        pages += 1
        assert pages <= 7
    assert [row['event_id'] for row in seen[:3]] == ['skill-usage:1', 'skill-usage:2', 'skill-usage:3']
    assert [row['event_id'].split(':', 1)[1] for row in seen[3:]] == expected
    assert len({row['event_id'] for row in seen}) == len(seen)


@pytest.mark.parametrize('missing', ['first', 'last', 'after'])
def test_runtime_rotation_or_missing_anchor_expires_precisely(tmp_path, registry, missing):
    host, context, _calls, repo = _setup(tmp_path, registry)
    operations = _operations(repo, context, 4)
    service = BusinessCapabilityService(host)
    request = {'space_id': context.space_id, 'limit': 2}
    page = service.execute('spaces.usage.list', request, context=context)
    target = operations[0 if missing == 'first' else -1 if missing == 'last' else 1]
    with repo._runtime_connection() as conn:
        conn.execute('DELETE FROM tool_operations WHERE operation_id=?', (target,))
        conn.commit()
    with pytest.raises(BusinessError) as caught:
        service.execute('spaces.usage.list', {**request, 'cursor': page['next_cursor']}, context=context)
    assert caught.value.code == 'CURSOR_EXPIRED'
    assert caught.value.details['reason'] == 'RUNTIME_USAGE_FROZEN_RANGE_MISSING'


def test_skill_retention_trim_does_not_silently_shift_old_offset(tmp_path, registry):
    host, context, _calls, _repo = _setup(tmp_path, registry)
    store = SkillLearningStore.for_home(host.home_paths)
    for _ in range(4):
        store.record_skill_usage(event='manual_get', skill_id='owner:method', source='skill_search', task_id=context.task_id)
    service = BusinessCapabilityService(host)
    request = {'space_id': context.space_id, 'limit': 1}
    page = service.execute('spaces.usage.list', request, context=context)
    path = store.directory / 'usage_ledger.jsonl'
    lines = path.read_text().splitlines()
    path.write_text('\n'.join(lines[1:]) + '\n')
    with pytest.raises(BusinessError) as caught:
        service.execute('spaces.usage.list', {**request, 'cursor': page['next_cursor']}, context=context)
    assert caught.value.code == 'CURSOR_EXPIRED'
    assert caught.value.details['reason'] == 'SKILL_USAGE_FROZEN_RANGE_MISSING'


def test_current_scope_revocation_is_rechecked_with_existing_cursor(tmp_path, registry):
    host, context, _calls, repo = _setup(tmp_path, registry)
    _operations(repo, context, 4)
    service = BusinessCapabilityService(host)
    request = {'space_id': context.space_id, 'limit': 1}
    page = service.execute('spaces.usage.list', request, context=context)
    def denied(*_args, **_kwargs):
        raise PermissionError('space access revoked')
    host.business_spaces.get = denied
    with pytest.raises(PermissionError, match='revoked'):
        service.execute('spaces.usage.list', {**request, 'cursor': page['next_cursor']}, context=context)


def test_legacy_or_malformed_cursor_cannot_claim_unbounded_window(tmp_path, registry):
    import base64
    host, context, _calls, repo = _setup(tmp_path, registry)
    _operations(repo, context, 2)
    service = BusinessCapabilityService(host)
    request = {'space_id': context.space_id, 'limit': 1}
    page = service.execute('spaces.usage.list', request, context=context)
    raw = json.loads(base64.urlsafe_b64decode(page['next_cursor'] + '=' * (-len(page['next_cursor']) % 4)))
    raw['cutoff']['runtime']['last'][0] = 2**64
    cursor = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode()
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        service.execute('spaces.usage.list', {**request, 'cursor': cursor}, context=context)
