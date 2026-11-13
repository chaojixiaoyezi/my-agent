"""正式RUN_OUT原发布、RunStore与业务Client/HTTP原运输的往返。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.run_artifacts import (
    RunArtifactBatch,
    RunOutTarget,
    workflow_artifact_id,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests import test_v13_w0_observation_readers as original
from agent_py_agent.tests.test_acceptance_observation_projections import (
    background_http,
    client_for,
    isolated_home,
)
from agent_py_agent.tests.test_mega_business_api_actions import native_request

_FIXTURES = (background_http, isolated_home)


def archived(background, *, run_id='a' * 32, legacy=False):
    services, task = original.observed_task(background)
    home = services.owner_home
    attempt = 'b' * 32
    scratch = home / 'isolated-script-scratch'
    (scratch / 'run_out').mkdir(parents=True)
    raw = b'{"grand_total_cents":2363,"row_count":3}'
    relative = 'release result.json'
    (scratch / 'run_out' / relative).write_bytes(raw)
    batch = RunArtifactBatch(RunOutTarget(home, run_id, 'release', attempt), OwnerQuotaEnforcer(home, max_bytes=0))
    batch.stage(scratch, max_files=64, max_bytes=64 * 1024 * 1024)
    output_dir, artifacts = batch.publish()
    if legacy:
        artifacts = tuple({k: v for k, v in item.items() if k != 'artifact_id'} for item in artifacts)
    step = StepExecutionRecord('release', 'release', 'deterministic_script', 'succeeded', 1, 2, 1, False,
        escalation_result={'attempt_id': attempt}, output_dir=output_dir, output_artifacts=artifacts)
    record = WorkflowRunRecord(run_id, 'formal-release', services.owner_id, 'manual', 'succeeded', 1, 2, 1,
        steps=(step,), business_context=task.metadata['business_context'])
    WorkflowRunStore(home).create_run(record)
    return services, task, record, workflow_artifact_id(run_id, attempt, relative), Path(output_dir) / relative, raw


@pytest.mark.parametrize(('run_id', 'legacy'), [('a' * 32, True), ('c' * 64, False), ('custom-run-with-hyphens', False), ('x' * 60 + '-' + 'x' * 64, False)])
def test_formal_archive_original_version_native_get_download_range_and_legacy(background_http, run_id, legacy):  # noqa: F811
    services, task, record, artifact_id, path, raw = archived(background_http, run_id=run_id, legacy=legacy)
    assert len(artifact_id.encode()) <= 128
    client = client_for(background_http)
    digest = hashlib.sha256(raw).hexdigest()
    result = client.call('artifacts.get', {'artifact_id': artifact_id, 'version_ref': digest})
    assert result['ok'], result
    assert result['data']['artifact']['task_id'] == task.task_id
    assert result['data']['artifact']['content_sha256'] == digest and result['data']['available'] is True
    route = '/client/business/artifacts/' + artifact_id + '/content?version_ref=' + digest
    status, headers, body = native_request(background_http, 'GET', route)
    assert status == 200 and body == raw and headers['ETag'] == '"' + digest + '"'
    status, headers, body = native_request(background_http, 'GET', route, headers={'Range': 'bytes=1-7'})
    assert status == 206 and body == raw[1:8] and headers['Content-Range'] == f'bytes 1-7/{len(raw)}'
    assert native_request(background_http, 'GET', route, headers={'Range': 'bytes=999-1000'})[0] == 416
    assert native_request(background_http, 'GET', route, headers={'Range': 'bytes=1-7', 'If-Range': '"other"'})[2] == raw
    wrong = client.call('artifacts.get', {'artifact_id': artifact_id, 'version_ref': '0' * 64})
    assert wrong['http_status'] == 404
    foreign = client_for(background_http, user='business-bob').call('artifacts.get', {'artifact_id': artifact_id})
    assert not foreign['ok'] and '2363' not in str(foreign['data'])
    path.write_bytes(b'changed after formal publication')
    changed = client.call('artifacts.get', {'artifact_id': artifact_id})
    assert changed['http_status'] == 409 and 'ARTIFACT_CONTENT_DRIFTED' in str(changed['error'])
    assert native_request(background_http, 'GET', route)[0] == 409
    # 长编号也必须核原actor；对端用户没有下载此owner文件的资格。
    assert native_request(background_http, 'GET', route, user='business-bob')[0] != 200


def test_workflow_artifact_scope_denial_precedes_content_symlink_and_manifest_forgery(background_http, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.conversation import workflow_artifact_observation as reader

    services, task, record, artifact_id, path, raw = archived(background_http)
    original_guard = reader.guard_artifact_content_budget
    def forbidden_read(*args, **kwargs):
        raise AssertionError('unauthorized content read')
    monkeypatch.setattr(reader, 'guard_artifact_content_budget', forbidden_read)
    actor = BusinessActor(services.owner_id, 'restricted', services.observation,
        lambda action, facts: facts.get('space_id') != task.metadata['business_context']['space_id'])
    with pytest.raises(BusinessError):
        services.observation.query('artifacts.get', {'artifact_id': artifact_id}, actor=actor)
    from agent_py_agent.agent.gateway_parts import business_artifacts_http
    from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    for denied_field in ('space_id', 'thread_id'):
        context = bind_business_context(services.host,
            authorize=lambda action, facts: facts.get(denied_field) is None)
        with pytest.raises(HostAPIError) as caught:
            business_artifacts_http.artifact_content(artifact_id, hashlib.sha256(raw).hexdigest(), context, services)
        assert caught.value.status == 404
    monkeypatch.setattr(reader, 'guard_artifact_content_budget', original_guard)
    client = client_for(background_http)
    changed_id = artifact_id[:-1] + ('0' if artifact_id[-1] != '0' else '1')
    assert client.call('artifacts.get', {'artifact_id': changed_id})['http_status'] == 404
    from agent_py_agent.agent.workflows import run_artifacts

    original_locator = run_artifacts.workflow_artifact_run_ids
    monkeypatch.setattr(run_artifacts, 'workflow_artifact_run_ids', lambda *args: (record.workflow_run_id, 'conflicting-run'))
    conflict = client.call('artifacts.get', {'artifact_id': artifact_id})
    assert conflict['http_status'] == 503 and 'WORKFLOW_ARTIFACT_RUN_LOCATOR_CONFLICT' in str(conflict['error'])
    monkeypatch.setattr(run_artifacts, 'workflow_artifact_run_ids', original_locator)
    monkeypatch.setattr(reader, '_WORKFLOW_CONTENT_BYTES', 8)
    budget = client.call('artifacts.get', {'artifact_id': artifact_id})
    assert budget['http_status'] == 409 and 'WORKFLOW_ARTIFACT_SIZE_LIMIT' in str(budget['error'])
    monkeypatch.setattr(reader, '_WORKFLOW_CONTENT_BYTES', 64 * 1024 * 1024)
    outside = services.owner_home / 'never-exported.txt'
    outside.write_text('not a workflow output')
    path.unlink()
    path.symlink_to(outside)
    denied = client.call('artifacts.get', {'artifact_id': artifact_id})
    assert not denied['ok'] and 'not a workflow output' not in str(denied)
    route = '/client/business/artifacts/' + artifact_id + '/content?version_ref=' + hashlib.sha256(raw).hexdigest()
    status, _, body = native_request(background_http, 'GET', route)
    assert status != 200 and b'not a workflow output' not in body
    path.unlink()
    missing = client.call('artifacts.get', {'artifact_id': artifact_id})
    assert missing['ok'] and missing['data']['available'] is False
    route = '/client/business/artifacts/' + artifact_id + '/content?version_ref=' + hashlib.sha256(raw).hexdigest()
    assert native_request(background_http, 'GET', route)[0] == 404
    path.write_bytes(raw)
    record.steps[0].output_dir = str(outside.parent)
    WorkflowRunStore(services.owner_home).save_run(record)
    refused = client.call('artifacts.get', {'artifact_id': artifact_id})
    assert refused['http_status'] == 409 and 'WORKFLOW_ARTIFACT_OUTPUT_ROOT_MISMATCH' in str(refused['error'])


def test_locator_sqlite_failure_is_recoverable_unknown_not_not_found(background_http, monkeypatch):  # noqa: F811
    import sqlite3

    from agent_py_agent.agent.workflows import run_artifacts

    _, _, _, artifact_id, _, _ = archived(background_http)
    def unavailable(*args):
        raise sqlite3.OperationalError('isolated unavailable locator')
    monkeypatch.setattr(run_artifacts, 'workflow_artifact_run_ids', unavailable)
    result = client_for(background_http).call('artifacts.get', {'artifact_id': artifact_id})
    assert result['http_status'] == 503 and not result['ok'], result
    assert 'WORKFLOW_ARTIFACT_RUN_LOCATOR_UNAVAILABLE' in str(result['error'])
    assert result['data'] == {}


def test_missing_long_run_index_reports_incomplete_on_detail_and_download(background_http):  # noqa: F811
    services, _, _, artifact_id, _, raw = archived(background_http, run_id='long-order-' + 'a' * 86)
    (services.owner_home / 'data' / 'workflow_run_index.sqlite3').unlink()
    result = client_for(background_http).call('artifacts.get', {'artifact_id': artifact_id})
    assert result['http_status'] == 503 and 'WORKFLOW_ARTIFACT_RUN_LOCATOR_INCOMPLETE' in str(result['error'])
    version = hashlib.sha256(raw).hexdigest()
    status, _, body = native_request(background_http, 'GET',
        f'/client/business/artifacts/{artifact_id}/content?version_ref={version}')
    assert status == 503 and b'WORKFLOW_ARTIFACT_RUN_LOCATOR_INCOMPLETE' in body
