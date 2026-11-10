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


def prepared(tmp_path):
    owner = tmp_path / 'owner'
    task = owner / 'tasks' / '2026-11-10' / '巡检'
    path = task / 'output' / '巡检说明.md'
    outcome = WriteFileTool(task).execute({'path': 'output/巡检说明.md', 'content': '公开合成巡检说明'})
    assert outcome.ok
    call = canonical_history_call('write_file', {'path': 'output/巡检说明.md'}, call_id='write-report', run_id='run-1')
    result = canonical_history_result(call, outcome.output, handler_details=outcome.result_envelope)
    archive_tool_call_record(SimpleNamespace(root=task, config=SimpleNamespace()), ToolCallRecordParams(
        params=_empty_tool_loop_params(), tool_rounds=1, idx=1, call=call, result=result))
    record = next(record for record in latest_artifact_records(task).values() if Path(record.path) == path)
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
    request = SimpleNamespace(tool_name='send_message', tool_params={'__artifact_registry_root': forged}, write_boundary=None)
    assert '__artifact_registry_root' not in _tool_params_with_runtime_boundary(request)
    request.write_boundary = {'task_root': trusted}
    assert _tool_params_with_runtime_boundary(request)['__artifact_registry_root'] == trusted
