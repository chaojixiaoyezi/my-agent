"""真实写工具→Executor→外置归档→原registry；登记根与业务文件路径独立。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    current_run_task_workspace_root,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.artifacts.registry import (
    latest_artifact_records,
    resolve_artifact_record_report,
)
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_artifact_registry import _empty_tool_loop_params


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('location', ['run', 'durable_task'])
@pytest.mark.parametrize('mode', ['create', 'overwrite', 'append'])
def test_actual_write_executor_archive_keeps_nonempty_path_and_original_registry(tmp_path, external, location, mode):
    owner = tmp_path / 'owner'
    run_root, task_root = owner / 'runs' / '2026-11-09' / 'opaque-run', owner / 'tasks' / '2026-11-09' / 'durable'
    target_root = run_root if location == 'run' else task_root
    target_root.mkdir(parents=True)
    target = target_root / 'report.md'
    if mode != 'create':
        target.write_text('old\n', encoding='utf-8')
    params = replace(_empty_tool_loop_params(), task_attributes={'run_workspace': {'task_root': str(run_root)}})
    agent = SimpleNamespace(root=owner, config=SimpleNamespace(
        tool_output_externalize_min_chars=1 if external else 1_000_000,
        tool_output_preview_chars=120, tool_output_externalize_on_low_headroom=False))
    tool = WriteFileTool(owner)
    execution = execute_canonical_test_call(owner, tools={'write_file': tool}, tool_name='write_file',
        arguments={'path': str(target), 'content': 'new\n', 'mode': 'append' if mode == 'append' else 'overwrite'},
        run_id='run-1',
        output_archiver=lambda call, outcome: archive.archive_tool_output_projection(agent, params, call, outcome))
    assert execution.result.ok, execution.result.content
    details = execution.result.metadata['handler_details']
    assert details['path'] == details['target_path'] == details['output_path'] == str(target)
    record = archive.archive_tool_call_record(agent, ToolCallRecordParams(
        params=params, tool_rounds=1, idx=1, call=execution.call, result=execution.result))
    registered = [item for item in record['artifact_registry_refs'] if item['path'] == str(target)]
    assert len(registered) == 1 and registered[0]['run_id'] == 'run-1'
    assert registered[0]['task_id'] == 'task-1' and registered[0]['status'] == 'ready'
    assert current_run_task_workspace_root(agent, params) == run_root
    canonical = resolve_artifact_record_report(run_root, path=target)
    assert not canonical.errors and canonical.record.to_dict() == registered[0]
    assert target.read_text(encoding='utf-8') == ('old\nnew\n' if mode == 'append' else 'new\n')
    if location == 'durable_task':
        assert not latest_artifact_records(task_root)
    # 新对象模拟下一轮/进程重启，只有原registry和ref，不重新登记/重hash当前文件。
    cold = resolve_artifact_record_report(run_root, registered[0]['artifact_id'])
    assert not cold.errors and cold.record.sha256 == registered[0]['sha256']


def history_fixture(tmp_path, *, with_refs=True):
    """真实会话尾+规范终态旧request；不调用LLM或外部通道。"""
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    base = home_paths(tmp_path / 'home')
    owner = resolve_owner_home(base.root, OwnerIdentity.local_main())
    owner.home_dir.mkdir(parents=True)
    home = home_paths_with_owner(base, owner)
    store = ConversationStore(owner.home_dir / 'conversations')
    thread = store.threads.create(title='isolated provenance')
    root = owner.home_dir / 'runs' / '2026-11-09' / 'previous'
    root.mkdir(parents=True)
    agent = SimpleNamespace(root=owner.home_dir, home_paths=home, conversation_store=store,
                            config=SimpleNamespace(gateway_workspace='gateway'))
    payload = {'schema_version': 'gateway_terminal_request.v1', 'id': 'previous-request',
        'host_request_owner.v1': {'provider': 'local', 'owner_kind': 'main', 'owner_id': 'main'},
        'conversation_runtime': {'request_id': 'previous-request', 'thread_id': thread.thread_id,
                                 'task_id': 'previous-task', 'task_path': str(root)},
        'terminal_response': {'id': 'previous-request', 'ok': True, 'status': 'completed'}}
    path = gateway_paths(agent).terminal / 'previous-request.json'
    write_json_file_atomic(path, payload)
    metadata = {'gateway_request_id': 'previous-request'}
    if with_refs:
        metadata['delivery_artifacts'] = [{'artifact_id': 'previous-artifact', 'name': 'report.md'}]
    store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': 'synthetic', 'metadata': metadata})
    scope = {'session_id': thread.thread_id, 'request_id': 'current-request', 'owner_id': home.owner_id}
    return SimpleNamespace(agent=agent, root=root, payload=payload, path=path, scope=scope, thread=thread)


@pytest.mark.parametrize('with_refs', [False, True])
def test_exact_prior_request_recovers_original_registry_root_without_path_or_full_history(tmp_path, with_refs):
    from agent_py_agent.agent.artifacts.registry_provenance import verified_recent_registry_roots
    fixture = history_fixture(tmp_path, with_refs=with_refs)
    roots, errors = verified_recent_registry_roots(fixture.agent, fixture.scope)
    assert roots == (fixture.root,)
    assert errors == ({'context': 'artifact.registry.history', 'reason': 'history_window_partial', 'partial': True},)


@pytest.mark.parametrize('fault', ['owner', 'thread', 'request', 'missing', 'corrupt', 'outside', 'symlink'])
def test_unconfirmed_previous_source_never_authorizes_registry_root(tmp_path, fault):
    from agent_py_agent.agent.artifacts.registry_provenance import verified_recent_registry_roots
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    fixture = history_fixture(tmp_path)
    payload = fixture.payload
    if fault == 'owner':
        payload['host_request_owner.v1'] = {'provider': 'feishu', 'owner_kind': 'user', 'owner_id': 'other'}
    elif fault == 'thread':
        payload['conversation_runtime']['thread_id'] = 'other-thread'
    elif fault == 'request':
        payload['conversation_runtime']['request_id'] = 'other-request'
    elif fault == 'outside':
        payload['conversation_runtime']['task_path'] = str(tmp_path)
    elif fault == 'symlink':
        link = fixture.root.parent / 'linked'
        link.symlink_to(fixture.root, target_is_directory=True)
        payload['conversation_runtime']['task_path'] = str(link)
    write_json_file_atomic(fixture.path, payload)
    if fault == 'missing':
        fixture.path.unlink()
    elif fault == 'corrupt':
        fixture.path.write_text('{broken')
    roots, errors = verified_recent_registry_roots(fixture.agent, fixture.scope)
    assert roots == () and errors


def test_nonconversation_does_not_claim_historical_provenance(tmp_path):
    from agent_py_agent.agent.artifacts.registry_provenance import verified_recent_registry_roots
    assert verified_recent_registry_roots(SimpleNamespace(), {'request_id': 'standalone'}) == ((), ())


def test_sealed_exact_previous_request_and_cold_store_recover_same_root(tmp_path):
    from agent_py_agent.agent.artifacts.registry_provenance import verified_recent_registry_roots
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.gateway_parts.owner_retention import _publish_receipt_segment
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    fixture = history_fixture(tmp_path)
    paths = gateway_paths(fixture.agent)
    raw = fixture.path.read_bytes()
    _publish_receipt_segment(paths, [('previous-request', [(str(fixture.path.relative_to(paths.root)), raw)])])
    fixture.path.unlink()
    fixture.agent.conversation_store = ConversationStore(fixture.agent.home_paths.owner_home_dir / 'conversations', initialize=False)
    roots, errors = verified_recent_registry_roots(fixture.agent, fixture.scope)
    assert roots == (fixture.root,) and errors[0]['partial'] is True
    assert not fixture.path.exists()


def test_historical_request_lookup_never_exceeds_eight_receipts(monkeypatch, tmp_path):
    from agent_py_agent.agent.artifacts import registry_provenance as provenance
    fixture = history_fixture(tmp_path)
    store = fixture.agent.conversation_store
    for index in range(20):
        store.messages.append({'thread_id': fixture.thread.thread_id, 'role': 'assistant', 'content': '',
                               'metadata': {'gateway_request_id': f'old-{index}'}})
    calls = []
    original = provenance._read_previous_receipt
    def counted(paths, previous_id):
        path = paths.terminal / f'{previous_id}.json'
        calls.append(path.stem)
        # 每份都用真实有效原信封，只替换测试消息引用对应的结构化ID。
        from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
        payload = dict(fixture.payload)
        payload['id'] = path.stem
        payload['terminal_response'] = {**payload['terminal_response'], 'id': path.stem}
        payload['conversation_runtime'] = {**payload['conversation_runtime'], 'request_id': path.stem}
        write_json_file_atomic(path, payload)
        return original(paths, previous_id)
    monkeypatch.setattr(provenance, '_read_previous_receipt', counted)
    roots, errors = provenance.verified_recent_registry_roots(fixture.agent, fixture.scope)
    assert roots == (fixture.root,) and errors[0]['partial'] is True
    assert len(calls) == 8 and len(set(calls)) == 8
    assert calls[0] == 'old-19'


@pytest.mark.parametrize('shape', ['display', 'huge', 'blank', 'cold'])
def test_history_lookup_has_one_fixed_physical_tail_budget(monkeypatch, tmp_path, shape):
    from agent_py_agent.agent.artifacts import registry_provenance as provenance
    fixture = history_fixture(tmp_path)
    storage = fixture.agent.conversation_store.storage
    path = storage.message_path(fixture.thread.thread_id)
    if shape == 'cold':
        path.with_suffix('.manifest.json').write_bytes(b'not opened or parsed')
    elif shape == 'display':
        for _ in range(90):
            fixture.agent.conversation_store.messages.append({'thread_id': fixture.thread.thread_id,
                'role': 'display', 'content': 'synthetic display'})
    else:
        with path.open('ab') as stream:
            stream.write(b' ' * (2 * provenance._HISTORY_WINDOW_BYTES) if shape == 'blank'
                         else b'{"oversized":"' + b'x' * (2 * provenance._HISTORY_WINDOW_BYTES))
    calls = []
    original = provenance.read_tail_snapshot_beneath
    def bounded(*args, **kwargs):
        calls.append(kwargs['max_bytes'])
        value = original(*args, **kwargs)
        assert value is None or len(value.content) <= provenance._HISTORY_WINDOW_BYTES
        return value
    monkeypatch.setattr(provenance, 'read_tail_snapshot_beneath', bounded)
    roots, errors = provenance.verified_recent_registry_roots(fixture.agent, fixture.scope)
    assert roots == () and errors[0]['partial'] is True
    assert calls == ([] if shape == 'cold' else [provenance._HISTORY_WINDOW_BYTES])


def test_oversized_or_linked_terminal_cannot_authorize_historical_registry(tmp_path):
    from agent_py_agent.agent.artifacts.registry_provenance import verified_recent_registry_roots
    fixture = history_fixture(tmp_path)
    fixture.path.write_bytes(b' ' * 1_048_577)
    roots, errors = verified_recent_registry_roots(fixture.agent, fixture.scope)
    assert not roots and errors and not errors[0].get('partial')
    fixture.path.unlink()
    unrelated = tmp_path / 'outside.json'
    unrelated.write_text('{}')
    fixture.path.symlink_to(unrelated)
    roots, errors = verified_recent_registry_roots(fixture.agent, fixture.scope)
    assert not roots and errors and not errors[0].get('partial')


def test_rotation_published_during_tail_read_does_not_authorize_hot_only_source(monkeypatch, tmp_path):
    from agent_py_agent.agent.artifacts import registry_provenance as provenance
    fixture = history_fixture(tmp_path)
    path = fixture.agent.conversation_store.storage.message_path(fixture.thread.thread_id)
    original = provenance.read_tail_snapshot_beneath
    def rotating(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        path.with_suffix('.manifest.json').write_bytes(b'new canonical cold layout')
        return snapshot
    monkeypatch.setattr(provenance, 'read_tail_snapshot_beneath', rotating)
    roots, errors = provenance.verified_recent_registry_roots(fixture.agent, fixture.scope)
    assert not roots and errors[0]['partial'] is True
    assert errors[0]['window_reason'] == 'cold_transcript'
