"""变更观察经真实客户端保留原产物身份、版本和覆盖状态。"""
import json

import pytest

from agent_py_agent.agent.conversation.session_task_report import TaskTurnOutcome, close_out_turn
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)
from agent_py_agent.tests.test_mega_business_api_actions import native_request, observed_native_task


def change_source(native, *, legacy=False, count=1, metadata=None):
    services, task = observed_native_task(native)
    paths = [str(services.owner_home / ('长期项目' * 16) / f'report-{index}.md') for index in range(count)]
    artifacts = [{'path': path, 'artifact_id': f'art_result_{index}', 'sha256': 'a' * 64, 'size_bytes': 1}
                 for index, path in enumerate(paths)]
    turn = 'change-test-' + task.task_id
    services.host.conversation_store.session_tasks.bind_turn(task.task_id, turn_id=turn)
    result = close_out_turn(services.host, turn, TaskTurnOutcome(ok=True, summary='done',
        result_refs=tuple(paths), result_artifacts=() if legacy else tuple(artifacts)))
    assert result is not None and result.status == 'done'
    if metadata:
        from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
        update_json_file_atomic(services.host.conversation_store.session_tasks._path(task.task_id),
            lambda row: {**row, 'metadata': {**row['metadata'], **metadata}}, require_existing=True)

    def transport(path, request, **kwargs):
        status, _, raw = native_request(native, 'POST', path, request)
        return status, json.loads(raw)

    return BusinessClient(transport), task, paths


def test_changes_resolve_long_unicode_result_paths_to_original_artifact_ids(background_http):
    client, task, paths = change_source(background_http)
    detail = client.call('tasks.get', {'task_id': task.task_id})
    reply = client.call('changes.get', {'task_id': task.task_id})
    assert detail['ok'] and reply['ok'], reply
    data = reply['data']
    assert data['evidence_refs'] == detail['data']['result_refs'] == ['art_result_0']
    assert data['coverage'] == 'complete'
    assert data['changes'] == [{'target_ref': 'art_result_0', 'field': 'content_digest',
                               'before': None, 'after': 'a' * 64}]
    assert paths[0] not in json.dumps(data, ensure_ascii=False)
    assert client.call('changes.get', {'task_id': task.task_id, 'candidate_ref': 'art_result_0'})['ok']
    invented = client.call('changes.get', {'task_id': task.task_id, 'candidate_ref': 'invented'})
    assert not invented['ok'] and invented['error']['code'] == 'OBSERVATION_NOT_FOUND'
    status, _, raw = native_request(background_http, 'POST', '/client/business',
        {'action': 'changes.get', 'request_id': 'foreign-changes', 'params': {'task_id': task.task_id}},
        user='business-bob')
    assert status != 200 and 'art_result_0' not in raw.decode()


@pytest.mark.parametrize(('legacy', 'count', 'expected'), [(True, 1, 0), (False, 102, 100)])
def test_unresolved_or_omitted_artifacts_keep_partial_coverage(background_http, legacy, count, expected):
    client, task, _ = change_source(background_http, legacy=legacy, count=count)
    reply = client.call('changes.get', {'task_id': task.task_id})
    assert reply['ok'], reply
    assert reply['data']['coverage'] == 'partial'
    assert len(reply['data']['evidence_refs']) == expected
    assert len(reply['data']['changes']) == expected


def test_metadata_change_refs_validate_without_truncating_identity(background_http):
    valid = {'target_ref': 'artifact-before', 'field': 'version', 'before': 1, 'after': 2}
    client, task, _ = change_source(background_http, metadata={
        'changes': [valid, {**valid, 'target_ref': '报告' * 50}],
        'evidence_refs': ['original-evidence', '报告' * 50]})
    reply = client.call('changes.get', {'task_id': task.task_id})
    assert reply['ok'], reply
    assert reply['data']['coverage'] == 'partial'
    assert reply['data']['changes'][0] == valid
    assert reply['data']['evidence_refs'] == ['original-evidence', 'art_result_0']
