"""正式写入/归档登记到send_message与冻结附件adapter的同scope往返。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.artifacts.registry import (
    ArtifactRegistration,
    latest_artifact_records,
    register_artifact,
)
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    execute_registry_test_call,
)
from agent_py_agent.tests.test_artifact_registry import _empty_tool_loop_params
from agent_py_agent.tests.test_channel_message_tool import _message_registry, _tool


def prepared(tmp_path, *, separate_registry=False):
    owner = tmp_path / 'owner'
    task = owner / 'tasks' / '2026-11-10' / '巡检'
    path = task / 'output' / '巡检说明.md'
    outcome = WriteFileTool(task).execute({'path': 'output/巡检说明.md', 'content': '公开合成巡检说明'})
    assert outcome.ok
    call = canonical_history_call('write_file', {'path': 'output/巡检说明.md'}, call_id='write-report', run_id='run-1')
    result = canonical_history_result(call, outcome.output, handler_details=outcome.result_envelope)
    registry_root = owner / 'runs' / 'current' if separate_registry else task
    archive_tool_call_record(SimpleNamespace(root=registry_root, config=SimpleNamespace()), ToolCallRecordParams(
        params=_empty_tool_loop_params(), tool_rounds=1, idx=1, call=call, result=result))
    record = next(record for record in latest_artifact_records(registry_root).values() if Path(record.path) == path)
    tool, adapter = _tool(owner)
    store = LocalStore(owner / 'data' / 'local.db', enable_fts=False)
    registry = _message_registry(owner, store, tool)
    return owner, task, path, record, registry, adapter, store


@pytest.mark.parametrize('kind', ['absolute', 'task_relative', 'owner_relative', 'artifact_id', 'basename'])
def test_registered_task_reference_reaches_real_delivery_without_path_guessing(tmp_path, kind):
    owner, task, path, record, registry, adapter, store = prepared(tmp_path)
    refs = {'absolute': str(path), 'task_relative': str(path.relative_to(task)),
            'owner_relative': str(path.relative_to(owner)), 'artifact_id': record.artifact_id, 'basename': path.name}
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [refs[kind]]},
        write_boundary={} if kind == 'owner_relative' else {'task_root': str(task)}, trusted_run_context={'run_scope': {'task_id': 'run-1',
            'owner_type': 'user', 'owner_id': 'providers/feishu/users/ou_current_user'}})
    assert result.ok, (result.error_code, result.output)
    assert len(adapter.files) == 1 and adapter.files[0][1] == path
    assert json.loads(result.output)['attachments'][0]['artifact_id'] == record.artifact_id


def test_current_task_filename_is_not_searched_in_other_tasks_and_ambiguity_is_rejected(tmp_path):
    owner, task, path, record, registry, adapter, store = prepared(tmp_path)
    other = owner / 'tasks' / 'other'
    foreign = other / 'output' / 'other-only.md'
    foreign.parent.mkdir(parents=True)
    foreign.write_text('public synthetic', encoding='utf-8')
    register_artifact(ArtifactRegistration(workspace_root=other, path=foreign, artifact_id='other-only', status='ready'))
    duplicate = task / 'second' / path.name
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(path.read_bytes())
    register_artifact(ArtifactRegistration(workspace_root=task, path=duplicate, artifact_id='second', status='ready'))
    for ref, code in [('other-only', 'ARTIFACT_NOT_REGISTERED'), (path.name, 'ARTIFACT_VALIDATION_FAILED')]:
        result = execute_registry_test_call(registry, 'send_message', {'attachments': [ref]},
            write_boundary={'task_root': str(task)}, call_id=ref)
        assert not result.ok and result.error_code == code
    assert adapter.files == []


def test_task_relative_changed_file_still_fails_original_hash_gate(tmp_path):
    owner, task, path, record, registry, adapter, store = prepared(tmp_path)
    path.write_text('changed after registration', encoding='utf-8')
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [str(path.relative_to(task))]},
        write_boundary={'task_root': str(task)})
    assert not result.ok and result.error_code == 'ARTIFACT_VALIDATION_FAILED'
    assert adapter.files == [] and adapter.messages == []


def test_same_artifact_id_in_owner_and_current_task_is_not_arbitrarily_selected(tmp_path):
    owner, task, path, record, registry, adapter, store = prepared(tmp_path)
    second = owner / 'second.md'
    second.write_text('other public document', encoding='utf-8')
    register_artifact(ArtifactRegistration(workspace_root=owner, path=second, artifact_id=record.artifact_id, status='ready'))
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [record.artifact_id]},
        write_boundary={'task_root': str(task)})
    assert not result.ok and result.error_code == 'ARTIFACT_VALIDATION_FAILED'
    assert not adapter.files and not adapter.messages


def test_foreign_host_registry_root_cannot_expand_owner_delivery(tmp_path):
    owner, task, path, record, registry, adapter, store = prepared(tmp_path)
    foreign = tmp_path / 'another-owner'
    foreign.mkdir()
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [str(path)]},
        write_boundary={'task_root': str(foreign)})
    assert not result.ok and result.error_code == 'PATH_OUTSIDE_WORKSPACE'
    assert not adapter.files and not adapter.messages


def test_model_hidden_registry_root_cannot_replace_host_boundary(tmp_path):
    from agent_py_agent.agent.tooling.registry_invoke import _tool_params_with_runtime_boundary
    forged = str(tmp_path / 'foreign')
    trusted = str(tmp_path / 'owner' / 'current-task')
    request = SimpleNamespace(tool_name='send_message', tool_params={'__artifact_registry_root': forged,
        '__artifact_task_root': forged, '__artifact_gateway_root': forged}, write_boundary=None)
    cleaned = _tool_params_with_runtime_boundary(request)
    assert not any(key in cleaned for key in ('__artifact_registry_root', '__artifact_task_root', '__artifact_gateway_root'))
    request.write_boundary = {'task_root': trusted, 'artifact_gateway_root': trusted + '/gateway'}
    assert _tool_params_with_runtime_boundary(request)['__artifact_registry_root'] == trusted
    assert _tool_params_with_runtime_boundary(request)['__artifact_gateway_root'] == trusted + '/gateway'


@pytest.mark.parametrize('ref_kind', ['task_relative', 'owner_relative', 'artifact_id', 'basename'])
def test_distinct_current_run_registry_and_durable_task_roots_are_both_honored(tmp_path, ref_kind):
    owner, task, path, record, registry, adapter, store = prepared(tmp_path, separate_registry=True)
    registry_root = owner / 'runs' / 'current'
    refs = {'task_relative': str(path.relative_to(task)), 'owner_relative': str(path.relative_to(owner)),
            'artifact_id': record.artifact_id, 'basename': path.name}
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [refs[ref_kind]]},
        write_boundary={'task_root': str(task), 'artifact_registry_root': str(registry_root)})
    assert result.ok, (result.error_code, result.output)
    assert len(adapter.files) == 1 and adapter.files[0][1] == path


def historical_delivery(tmp_path, *, sealed=False):
    from dataclasses import replace

    from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
    from agent_py_agent.agent.delivery import (
        ChannelAdapterRegistry,
        ChannelCapabilities,
        DeliveryService,
    )
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
    from agent_py_agent.tests.test_channel_message_tool import _RecordingAdapter
    from agent_py_agent.tests.test_r9_write_artifact_registry_provenance import history_fixture

    fixture = history_fixture(tmp_path, with_refs=False)
    agent = fixture.agent
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    fixture.gateway_root = gateway_paths(agent).root
    agent.config.gateway_workspace = str(fixture.gateway_root)
    owner = agent.home_paths.owner_home_dir
    path = owner / 'tasks' / 'old-task' / 'report.md'
    outcome = WriteFileTool(owner).execute({'path': str(path), 'content': '公开旧回合正式产物'})
    assert outcome.ok
    params = replace(_empty_tool_loop_params(), task_attributes={'run_workspace': {'task_root': str(fixture.root)}})
    call = canonical_history_call('write_file', {'path': str(path)}, call_id='old-write', run_id='previous-request')
    result = canonical_history_result(call, outcome.output, handler_details=outcome.result_envelope)
    archive_tool_call_record(agent, ToolCallRecordParams(params=params, tool_rounds=1, idx=1, call=call, result=result))
    record = next(record for record in latest_artifact_records(fixture.root).values() if Path(record.path) == path)
    if sealed:
        from agent_py_agent.agent.gateway_parts.owner_retention import _publish_receipt_segment
        from agent_py_agent.agent.gateway_parts.paths import gateway_paths
        paths = gateway_paths(agent)
        _publish_receipt_segment(paths, [('previous-request', [(str(fixture.path.relative_to(paths.root)), fixture.path.read_bytes())])])
        fixture.path.unlink()
    adapter = _RecordingAdapter()
    channels = ChannelAdapterRegistry()
    channels.register_adapter('feishu', adapter, capabilities=ChannelCapabilities(
        text=True, reply=True, proactive=True, files=True, frozen_attachment_content=True))
    agent.delivery_service = DeliveryService(channels)
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
    agent.config.admin_channel_identity_enabled = True
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(admin_message_channels=['feishu']))
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_current_user')
    tool = SendMessageTool(agent)
    store = LocalStore(owner / 'data' / 'local.db', enable_fts=False)
    registry = ToolRegistry(ToolRegistryParams(workspace_root=owner, max_chars=6000, max_entries=200,
        max_matches=50, web_max_chars=12000, http_timeout=30, catalog_limit=20, retrieval_limit=3,
        vector_search_enabled=False, operation_store=store, operation_store_required=True,
        operation_owner_id=agent.home_paths.owner_id))
    registry.register(tool)
    return fixture, path, record, registry, adapter


@pytest.mark.parametrize('sealed', [False, True])
@pytest.mark.parametrize('relative', [False, True])
def test_next_turn_sends_original_registered_file_from_verified_previous_receipt(tmp_path, sealed, relative):
    fixture, path, record, registry, adapter = historical_delivery(tmp_path, sealed=sealed)
    ref = str(path.relative_to(fixture.agent.home_paths.owner_home_dir)) if relative else str(path)
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [ref]},
        trusted_run_context={'run_scope': fixture.scope},
        write_boundary={'artifact_gateway_root': str(fixture.gateway_root)})
    assert result.ok, (result.error_code, result.output)
    assert len(adapter.files) == 1 and adapter.files[0][1] == path
    assert json.loads(result.output)['attachments'][0]['artifact_id'] == record.artifact_id


@pytest.mark.parametrize('reference', ['id', 'name'])
def test_unbound_historical_bare_ref_stays_partial_and_is_not_guessed(tmp_path, reference):
    fixture, path, record, registry, adapter = historical_delivery(tmp_path)
    ref = record.artifact_id if reference == 'id' else path.name
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [ref]},
        trusted_run_context={'run_scope': fixture.scope},
        write_boundary={'artifact_gateway_root': str(fixture.gateway_root)})
    assert not result.ok and result.error_code == 'ARTIFACT_NOT_REGISTERED'
    assert json.loads(result.output)['coverage'] == {'scope': 'current_and_recent_verified',
        'complete': False, 'reason': 'structured_ref_required'}
    assert not adapter.files and not adapter.messages


@pytest.mark.parametrize('fault', ['hash', 'receipt'])
def test_previous_attachment_unknown_or_changed_source_never_sends(tmp_path, fault):
    fixture, path, record, registry, adapter = historical_delivery(tmp_path)
    if fault == 'hash':
        path.write_text('modified after old registration', encoding='utf-8')
    else:
        fixture.path.write_text('{invalid', encoding='utf-8')
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [str(path)]},
        trusted_run_context={'run_scope': fixture.scope},
        write_boundary={'artifact_gateway_root': str(fixture.gateway_root)})
    assert not result.ok and result.error_code == 'ARTIFACT_VALIDATION_FAILED'
    assert not adapter.files and not adapter.messages


def test_multiple_attachments_read_each_trusted_registry_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import channel_message_tool as message_tool
    owner, task, path, record, registry, adapter, store = prepared(tmp_path)
    refs = [str(path.relative_to(task))]
    for name in ('second.md', 'third.md'):
        target = task / 'output' / name
        outcome = WriteFileTool(task).execute({'path': str(target), 'content': 'public fixture'})
        assert outcome.ok
        register_artifact(ArtifactRegistration(workspace_root=task, path=target, status='ready'))
        refs.append(str(target.relative_to(task)))
    reads = {}
    original = message_tool._read_registry_report_beneath
    def read(owner_root, root):
        reads[root] = reads.get(root, 0) + 1
        return original(owner_root, root)
    monkeypatch.setattr(message_tool, '_read_registry_report_beneath', read)
    result = execute_registry_test_call(registry, 'send_message', {'attachments': refs}, write_boundary={'task_root': str(task)})
    assert result.ok, (result.error_code, result.output)
    assert len(adapter.files) == 3 and reads and all(count == 1 for count in reads.values())


@pytest.mark.parametrize('stage', ['after_proof', 'before_registry_read'])
def test_verified_root_cannot_be_redirected_to_sibling_run_between_proof_and_read(tmp_path, monkeypatch, stage):
    import shutil

    from agent_py_agent.agent.artifacts import registry_provenance
    from agent_py_agent.agent.capability import channel_message_tool

    fixture, path, record, registry, adapter = historical_delivery(tmp_path)
    sibling = fixture.root.with_name('sibling')
    shutil.copytree(fixture.root, sibling)
    swapped = []
    def redirect():
        assert not swapped
        fixture.root.rename(fixture.root.with_name('original-held'))
        fixture.root.symlink_to(sibling, target_is_directory=True)
        swapped.append(True)
    if stage == 'after_proof':
        original = registry_provenance.verified_recent_registry_roots
        def proof(*args, **kwargs):
            result = original(*args, **kwargs)
            assert result[0] == (fixture.root,)
            redirect()
            return result
        monkeypatch.setattr(registry_provenance, 'verified_recent_registry_roots', proof)
    else:
        original = channel_message_tool._read_registry_report_beneath
        def read(owner, root):
            if root == fixture.root:
                redirect()
            return original(owner, root)
        monkeypatch.setattr(channel_message_tool, '_read_registry_report_beneath', read)
    result = execute_registry_test_call(registry, 'send_message', {'attachments': [str(path)]},
        trusted_run_context={'run_scope': fixture.scope},
        write_boundary={'artifact_gateway_root': str(fixture.gateway_root)})
    assert swapped and not result.ok
    assert result.error_code == ('PATH_OUTSIDE_WORKSPACE' if stage == 'after_proof' else 'ARTIFACT_VALIDATION_FAILED')
    assert not adapter.files and not adapter.messages
