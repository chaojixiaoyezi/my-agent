"""新发布必须指向规范可见的原生身份；原回执重查与外部身份不受新门影响。"""
import json
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.subagents.models import TaskStatus
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.tests.test_orch04_leaf_receipt_authority import board, caller  # noqa: F401


def canonical_bytes(service):
    return {str(p.relative_to(service.store.root)): p.read_bytes()
            for p in service.store.root.rglob('*') if p.suffix in {'.json', '.jsonl'}}


def publish(case, recipient, **extra):
    agent, tool, _, root, _, _, _, collaboration, _ = case
    arguments = {'action': 'publish_item', 'case_id': collaboration.case_id, 'request_id': 'new-request',
        'responsible_actor': recipient, 'frozen_input_version': 'sha256:frozen',
        'item_id': 'new-item', 'idempotency_key': 'new-stable-key', **extra}
    with caller(agent, root) as context:
        return tool.execute_scoped(arguments, context)


@pytest.mark.parametrize('bad', ['alias', 'other_root', 'other_owner', 'done', 'conflict', 'unreadable'])
def test_invalid_native_recipient_has_no_handoff_write(board, monkeypatch, bad):  # noqa: F811
    agent, _, service, _, recipient, _, _, _, _ = board
    target = recipient.id
    if bad == 'alias':
        target = 'friendly-name-is-not-an-agent-run'
    elif bad == 'other_root':
        target = agent.subagents.create_run(goal='other', thought='test', plan=['read'], role='worker').id
    elif bad == 'other_owner':
        recipient.owner = 'local/another-owner'
        agent.subagents.save(recipient)
    elif bad == 'done':
        recipient.status = TaskStatus.DONE
        agent.subagents.save(recipient)
    elif bad == 'unreadable':
        original = agent.subagents.load
        def load(run_id):
            if run_id == target:
                raise OSError('private error body must not escape')
            return original(run_id)
        monkeypatch.setattr(agent.subagents, 'load', load)
    before = canonical_bytes(service)
    result = publish(board, target, **({'actor_id': 'another-reference'} if bad == 'conflict' else {}))
    assert not result.ok and result.effect_outcome == 'not_started', result.output
    assert canonical_bytes(service) == before
    assert 'private error body' not in result.output
    details = json.loads(result.output)['details']
    assert details['reason'].startswith('HANDOFF_RECIPIENT_')
    assert details['field'] == 'responsible_actor' and details['example']['action'] == 'publish_item'


def test_native_recipient_identity_supports_exact_read_and_own_ack(board):  # noqa: F811
    agent, tool, service, _, recipient, _, _, collaboration, _ = board
    result = publish(board, recipient.id)
    assert result.ok, result.output
    reference = json.loads(result.output)['handoff_reference']
    assert reference['responsible_actor'] == recipient.id
    with caller(agent, recipient) as context:
        read = tool.execute_scoped({'action': 'list_items', 'case_id': collaboration.case_id, 'item_id': 'new-item'}, context)
        assert read.ok and json.loads(read.output)['items'][0]['responsible_actor'] == recipient.id
        ack = tool.execute_scoped({'action': 'record_ack', 'case_id': collaboration.case_id, 'item_id': 'new-item',
            'frozen_input_version': reference['frozen_input_version'], 'recipient_ack': {'read_reference': True}}, context)
        assert ack.ok, ack.output
    assert service.store.load_handoff_item(collaboration.case_id, 'new-item').state == 'acked'


def test_original_publish_receipt_survives_recipient_done_and_read_failure(board, monkeypatch):  # noqa: F811
    agent, _, service, _, recipient, _, _, collaboration, _ = board
    first = publish(board, recipient.id)
    assert first.ok, first.output
    recipient.status = TaskStatus.DONE
    agent.subagents.save(recipient)
    before = canonical_bytes(service)
    repeated = publish(board, recipient.id)
    assert repeated.ok and canonical_bytes(service) == before
    original = agent.subagents.load
    def fail(run_id):
        if run_id == recipient.id:
            raise OSError('not readable now')
        return original(run_id)
    monkeypatch.setattr(agent.subagents, 'load', fail)
    assert publish(board, recipient.id).ok
    assert canonical_bytes(service) == before
    changed = publish(board, recipient.id, frozen_input_version='sha256:different')
    assert not changed.ok and changed.error_code == 'IDEMPOTENCY_CONFLICT'
    assert canonical_bytes(service) == before
    assert service.store.load_handoff_item(collaboration.case_id, 'new-item').frozen_input_version == 'sha256:frozen'


def test_external_assistant_never_uses_native_lookup(board, monkeypatch):  # noqa: F811
    agent, _, _, root, _, _, _, _, _ = board
    original = agent.subagents.load
    def load(run_id):
        assert run_id == root.id, 'external identity must not be looked up as native run'
        return original(run_id)
    monkeypatch.setattr(agent.subagents, 'load', load)
    result = publish(board, 'external-helper-with-arbitrary-opaque-id', actor_kind='external_assistant')
    assert result.ok, result.output


def test_main_self_recipient_does_not_require_subagent_record(board, monkeypatch):  # noqa: F811
    agent, tool, _, _, _, _, _, collaboration, _ = board
    def no_lookup(run_id):
        raise AssertionError('current main self must use proven host context')
    monkeypatch.setattr(agent.subagents, 'load', no_lookup)
    with host_turn_scope(agent, '', RunParams(run_id='main-without-leaf', request_id='main-without-leaf', save=False),
            reason='handoff_main_self', tool_rounds=0) as turn:
        context = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
        result = tool.execute_scoped({'action': 'publish_item', 'case_id': collaboration.case_id,
            'responsible_actor': turn.params.run_id, 'frozen_input_version': 'v1'}, context)
        assert result.ok, result.output
        turn.runtime_status = 'ok'


def test_native_identity_reads_happen_outside_case_lock(board, monkeypatch):  # noqa: F811
    agent, _, service, _, recipient, _, _, _, _ = board
    held = []
    original_lock, original_load = service.store.handoff_case_lock, agent.subagents.load
    @contextmanager
    def lock(case_id):
        with original_lock(case_id):
            held.append(case_id)
            try:
                yield
            finally:
                held.pop()
    def load(run_id):
        assert held == [], 'canonical manager reads must not invert case lock order'
        return original_load(run_id)
    monkeypatch.setattr(service.store, 'handoff_case_lock', lock)
    monkeypatch.setattr(agent.subagents, 'load', load)
    result = publish(board, recipient.id)
    assert result.ok, result.output


def test_upstream_main_recipient_is_visible_to_same_root_leaf(board):  # noqa: F811
    agent, tool, _, _, _, _, _, collaboration, _ = board
    manager = agent.subagents
    with host_turn_scope(agent, '', RunParams(run_id='real-main-receiver', request_id='real-main-receiver', save=False),
            reason='handoff_root_recipient', tool_rounds=0) as turn:
        author = manager.create_run(goal='publish', thought='test', plan=['publish'], role='coordinator',
            parent_id=turn.params.run_id, root_id=turn.params.run_id, allowed_tools=['manage_handoff_board'])
        reviewer = manager.create_run(goal='review', thought='test', plan=['read'], role='worker',
            parent_id=turn.params.run_id, root_id=turn.params.run_id, allowed_tools=['manage_handoff_board'])
        with caller(agent, author) as context:
            published = tool.execute_scoped({'action': 'publish_item', 'case_id': collaboration.case_id,
                'responsible_actor': turn.params.run_id, 'frozen_input_version': 'main-v1', 'item_id': 'main-received'}, context)
            assert published.ok, published.output
        with caller(agent, reviewer) as context:
            read = tool.execute_scoped({'action': 'list_items', 'case_id': collaboration.case_id, 'item_id': 'main-received'}, context)
            assert read.ok, read.output
            assert json.loads(read.output)['items'][0]['responsible_actor'] == turn.params.run_id
        turn.runtime_status = 'ok'


@pytest.mark.parametrize('status', [TaskStatus.PAUSED, TaskStatus.BLOCKED, TaskStatus.FAILED])
def test_recoverable_recipient_and_pending_tool_grant_are_not_permanent_rejections(board, status):  # noqa: F811
    agent, _, _, _, recipient, _, _, _, _ = board
    recipient.status, recipient.allowed_tools = status, ['read_file']
    agent.subagents.save(recipient)
    result = publish(board, recipient.id)
    assert result.ok, result.output


def test_forged_root_claim_cannot_authorize_another_native_work(board):  # noqa: F811
    agent, tool, service, root, _, _, _, collaboration, _ = board
    other = agent.subagents.create_run(goal='separate original task', thought='test', plan=['receive'], role='worker')
    root.root_id = other.id
    agent.subagents.save(root)
    before = canonical_bytes(service)
    with caller(agent, root) as context:
        result = tool.execute_scoped({'action': 'publish_item', 'case_id': collaboration.case_id,
            'responsible_actor': other.id, 'frozen_input_version': 'v1', 'item_id': 'forged-scope'}, context)
    assert not result.ok and result.effect_outcome == 'not_started'
    assert 'HANDOFF_RECIPIENT_NOT_IN_SCOPE' in result.output
    assert canonical_bytes(service) == before


def test_leaf_forged_root_cannot_read_another_original_task(board):  # noqa: F811
    agent, tool, service, _, _, reader, _, collaboration, _ = board
    foreign = agent.subagents.create_run(goal='other work', thought='test', plan=['receive'], role='worker')
    item = service.publish_item(case_id=collaboration.case_id, request_id='foreign-test',
        responsible_actor=foreign.id, frozen_input_version='foreign-v1', item_id='foreign-item')
    reader.root_id = foreign.id
    agent.subagents.save(reader)
    before = canonical_bytes(service)
    with caller(agent, reader) as context:
        result = tool.execute_scoped({'action': 'list_items', 'case_id': collaboration.case_id, 'item_id': item.item_id}, context)
    assert not result.ok and result.effect_outcome == 'not_started'
    assert 'HANDOFF_TASK_SCOPE_MISMATCH' in result.output
    assert 'foreign-v1' not in result.output
    assert canonical_bytes(service) == before


def test_publish_reports_only_actual_missing_or_typed_fields_without_version_coercion(board):  # noqa: F811
    agent, tool, service, root, recipient, _, _, collaboration, _ = board
    before = canonical_bytes(service)
    for actor_field in ('responsible_actor', 'actor_id'):
        for raw_version in (None, '', '  ', 42, False, {'hash': 'not-a-version-string'}, ['not-string']):
            params = {'action': 'publish_item', 'case_id': collaboration.case_id, actor_field: recipient.id}
            if raw_version is not None:
                params['frozen_input_version'] = raw_version
            with caller(agent, root) as context:
                result = tool.execute_scoped(params, context)
            assert not result.ok and result.error_code == 'TOOL_INVALID_ARGUMENTS'
            assert result.effect_outcome == 'not_started'
            details = json.loads(result.output)['details']
            assert set(details['invalid_fields']) == {'$.frozen_input_version'}
            assert details['provided_responsible_actor'] == recipient.id
            assert details['example']['responsible_actor'] == recipient.id
            assert details['invalid_fields']['$.frozen_input_version']['actual_type'] == type(raw_version).__name__
            assert canonical_bytes(service) == before
    for supplied, expected in (({'frozen_input_version': 'release-v1'}, {'$.responsible_actor'}),
                               ({}, {'$.responsible_actor', '$.frozen_input_version'})):
        with caller(agent, root) as context:
            result = tool.execute_scoped({'action': 'publish_item', 'case_id': collaboration.case_id, **supplied}, context)
        assert not result.ok and result.effect_outcome == 'not_started'
        details = json.loads(result.output)['details']
        assert set(details['invalid_fields']) == expected
        assert details['provided_frozen_input_version'] == supplied.get('frozen_input_version')
        assert canonical_bytes(service) == before
    # 合法非哈希版本仍是原合同的string，不新增格式枚举，也不排除external助手。
    result = publish(board, 'external-test-recipient', actor_kind='external_assistant', frozen_input_version='release-v1')
    assert result.ok and json.loads(result.output)['item']['frozen_input_version'] == 'release-v1'


@pytest.mark.parametrize('closed_status', ['done', 'cancelled', 'interrupted'])
def test_main_finished_turn_can_receive_only_while_original_task_link_active(board, closed_status):  # noqa: F811
    agent, tool, service, _, _, _, _, collaboration, _ = board
    store = agent.conversation_store
    root_id = 'yielded-main-recipient'
    thread = store.threads.create(canonical_user_id='local/main', title='handoff yielded root')
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': root_id, 'goal': 'receive child handoff', 'status': 'active'})
    with host_turn_scope(agent, '', RunParams(run_id=root_id, request_id=root_id, save=False),
            reason='handoff_root_yield', tool_rounds=0) as turn:
        author = agent.subagents.create_run(goal='publish', thought='test', plan=['publish'], role='coordinator',
            parent_id=root_id, root_id=root_id, allowed_tools=['manage_handoff_board'])
        turn.runtime_status = 'ok'
    assert agent.subagents.runtime_db.agent_run_for_run_id(root_id)['status'] == 'done'
    params = {'action': 'publish_item', 'case_id': collaboration.case_id, 'responsible_actor': root_id,
        'frozen_input_version': 'original-v1', 'item_id': 'yielded-item'}
    with caller(agent, author) as context:
        result = tool.execute_scoped(params, context)
        assert result.ok, result.output
    store.tasks.update_status({'task_id': root_id, 'status': closed_status})
    before = canonical_bytes(service)
    with caller(agent, author) as context:
        rejected = tool.execute_scoped({**params, 'item_id': 'new-after-task-closed'}, context)
        assert not rejected.ok and rejected.effect_outcome == 'not_started'
        assert 'HANDOFF_RECIPIENT_CLOSED' in rejected.output
        assert tool.execute_scoped(params, context).ok  # 原发表回执仍可安全重查。
    assert canonical_bytes(service) == before


@pytest.mark.parametrize('corruption', ['owner', 'task', 'thread', 'read_error', 'malformed_origin'])
def test_active_task_label_cannot_override_original_root_identity(board, monkeypatch, corruption):  # noqa: F811
    from dataclasses import replace

    agent, tool, service, _, _, _, _, collaboration, _ = board
    store = agent.conversation_store
    root_id = 'main-root-with-active-link'
    thread = store.threads.create(canonical_user_id='local/main', title='active root identity')
    link = store.tasks.bind({'thread_id': thread.thread_id, 'task_id': root_id, 'goal': 'handoff', 'status': 'active'})
    with host_turn_scope(agent, '', RunParams(run_id=root_id, request_id=root_id, save=False),
            reason='handoff_root_identity', tool_rounds=0) as turn:
        author = agent.subagents.create_run(goal='publish', thought='test', plan=['publish'], role='coordinator',
            parent_id=root_id, root_id=root_id, allowed_tools=['manage_handoff_board'])
        turn.runtime_status = 'ok'
    origin = {'owner_id': agent.home_paths.owner_id, 'task_id': root_id, 'thread_id': thread.thread_id}
    if corruption == 'owner':
        origin['owner_id'] = 'local/other'
    elif corruption == 'task':
        origin['task_id'] = 'other-task'
    elif corruption == 'thread':
        origin['thread_id'] = 'other-thread'
    elif corruption == 'malformed_origin':
        origin = 'active is not an identity'
    altered = replace(link, host_user_origin=origin)
    monkeypatch.setattr(store.tasks, 'load_report', lambda task_id:
        (None, {'exception_type': 'ValueError'}) if corruption == 'read_error' else (altered, None))
    before = canonical_bytes(service)
    with caller(agent, author) as context:
        result = tool.execute_scoped({'action': 'publish_item', 'case_id': collaboration.case_id,
            'responsible_actor': root_id, 'frozen_input_version': 'v1', 'item_id': 'must-not-write'}, context)
    assert not result.ok and result.effect_outcome == 'not_started', result.output
    assert 'HANDOFF_RECIPIENT_' in result.output and canonical_bytes(service) == before
