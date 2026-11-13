"""正式归档的永久缺失、坏旁项及64MiB有界运输合同。"""
from __future__ import annotations

import hashlib
import http.client
import os
import stat
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def archive(background, *, size=6, bad_sibling=False, nested=False, business=True):
    services, task = original.observed_task(background)
    home, run, attempt = services.owner_home, 'd' * 32, 'e' * 32
    scratch = home / 'script-scratch'
    folder = scratch / 'run_out'
    folder.mkdir(parents=True)
    name = 'nested/result.bin' if nested else 'result.bin'
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    chunk, digest = b'x' * 65536, hashlib.sha256()
    with path.open('wb') as stream:
        for offset in range(0, size, len(chunk)):
            part = chunk[:min(len(chunk), size - offset)]
            stream.write(part)
            digest.update(part)
    if bad_sibling:
        (folder / 'reports\\2026.txt').write_bytes(b'not a path separator on POSIX')
    batch = RunArtifactBatch(RunOutTarget(home, run, 'release', attempt), OwnerQuotaEnforcer(home, max_bytes=0))
    batch.stage(scratch, max_files=64, max_bytes=64 * 1024 * 1024)
    output, artifacts = batch.publish()
    step = StepExecutionRecord('release', 'release', 'deterministic_script', 'succeeded', 1, 2, 1, False,
        escalation_result={'attempt_id': attempt}, output_dir=output, output_artifacts=artifacts)
    record = WorkflowRunRecord(run, 'flow', services.owner_id, 'manual', 'succeeded', 1, 2, 1,
        steps=(step,), business_context=task.metadata['business_context'] if business else {})
    WorkflowRunStore(home).create_run(record)
    identifier = workflow_artifact_id(run, attempt, name)
    return services, record, identifier, Path(output) / name, digest.hexdigest()


@pytest.mark.parametrize('kind', ['absent', 'foreign', 'no_business'])
def test_permanent_missing_returns_nonretryable_404(background_http, kind):  # noqa: F811
    _, _, identifier, _, digest = archive(background_http, business=kind != 'no_business')
    user = 'business-bob' if kind == 'foreign' else 'business-alice'
    if kind == 'absent':
        identifier = identifier.replace('wfart-d', 'wfart-c', 1)
    result = client_for(background_http, user=user).call('artifacts.get', {'artifact_id': identifier})
    assert result['http_status'] == 404 and result['error']['retryable'] is False, result
    status, _, _ = native_request(background_http, 'GET',
        f'/client/business/artifacts/{identifier}/content?version_ref={digest}', user=user)
    assert status == 404


def test_missing_nested_parent_is_unavailable_not_host_failure(background_http):  # noqa: F811
    _, _, identifier, path, digest = archive(background_http, nested=True)
    path.unlink()
    path.parent.rmdir()
    result = client_for(background_http).call('artifacts.get', {'artifact_id': identifier})
    assert result['ok'] and result['data']['available'] is False, result
    assert native_request(background_http, 'GET',
        f'/client/business/artifacts/{identifier}/content?version_ref={digest}')[0] == 404


def test_invalid_sibling_does_not_hide_valid_manifest_item(background_http):  # noqa: F811
    _, record, identifier, _, digest = archive(background_http, bad_sibling=True)
    result = client_for(background_http).call('artifacts.get', {'artifact_id': identifier})
    assert result['ok'] and result['data']['available'] is True, result
    route = f'/client/business/artifacts/{identifier}/content?version_ref={digest}'
    assert native_request(background_http, 'GET', route)[2] == b'xxxxxx'
    bad = workflow_artifact_id(record.workflow_run_id, 'e' * 32, 'reports\\2026.txt')
    refused = client_for(background_http).call('artifacts.get', {'artifact_id': bad})
    assert refused['http_status'] == 404 and refused['error']['retryable'] is False, refused


@pytest.mark.parametrize('size', [17 * 1024 * 1024, 64 * 1024 * 1024])
def test_production_run_out_full_download_streams_complete_hash(background_http, size):  # noqa: F811
    _, _, identifier, _, digest = archive(background_http, size=size)
    result = client_for(background_http).call('artifacts.get', {'artifact_id': identifier})
    assert result['ok'] and result['data']['artifact']['size_bytes'] == size, result
    server, _ = background_http
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    try:
        connection.request('GET', f'/client/business/artifacts/{identifier}/content?version_ref={digest}', headers={
            'X-Gateway-Token': server._local_client_credential, 'X-User-Id': 'business-alice', 'X-Channel': 'feishu'})
        response = connection.getresponse()
        assert response.status == 200, response.read(1024)
        assert response.getheader('ETag') == '"' + digest + '"'
        assert int(response.getheader('Content-Length')) == size
        actual, count = hashlib.sha256(), 0
        while chunk := response.read(65536):
            actual.update(chunk)
            count += len(chunk)
        assert count == size and actual.hexdigest() == digest
    finally:
        connection.close()
    route = f'/client/business/artifacts/{identifier}/content?version_ref={digest}'
    status, headers, raw = native_request(background_http, 'GET', route,
        headers={'Range': 'bytes=-17', 'If-Range': '"' + digest + '"'})
    assert status == 206 and raw == b'x' * 17 and headers['Content-Range'] == f'bytes {size-17}-{size-1}/{size}'


def test_task_run_attempt_denial_precedes_any_content_read(background_http, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.conversation import workflow_artifact_observation as reader
    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError

    services, _, identifier, _, _ = archive(background_http)
    def forbidden(*args, **kwargs):
        pytest.fail('denied caller reached content IO')
    monkeypatch.setattr(reader, 'guard_artifact_content_budget', forbidden)
    for field in ('task_id', 'run_id', 'attempt_id'):
        actor = BusinessActor(services.owner_id, 'limited', services.observation,
            lambda action, facts, field=field: not facts.get(field))
        with pytest.raises(BusinessError) as denied:
            services.observation.query('artifacts.get', {'artifact_id': identifier}, actor=actor)
        assert denied.value.code == 'OBSERVATION_NOT_FOUND'


def test_unsafe_matching_manifest_entry_cannot_escape_and_valid_sibling_survives(background_http):  # noqa: F811
    services, record, identifier, _, _ = archive(background_http)
    evil = '../outside.txt'
    evil_id = workflow_artifact_id(record.workflow_run_id, 'e' * 32, evil)
    record.steps[0].output_artifacts = (*record.steps[0].output_artifacts,
        {'path': evil, 'artifact_id': evil_id, 'size_bytes': 6, 'sha256': hashlib.sha256(b'secret').hexdigest()})
    WorkflowRunStore(services.owner_home).save_run(record)
    assert client_for(background_http).call('artifacts.get', {'artifact_id': identifier})['ok']
    refused = client_for(background_http).call('artifacts.get', {'artifact_id': evil_id})
    assert refused['http_status'] == 404 and refused['error']['retryable'] is False


def test_source_permission_error_stays_unknown_not_missing(background_http, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.conversation import task_artifact_observation as source

    _, _, identifier, _, _ = archive(background_http)
    original_read = source.read_bytes_beneath
    def denied(root, parts, **kwargs):
        if parts[-1] == 'run.json':
            raise PermissionError('isolated denial')
        return original_read(root, parts, **kwargs)
    monkeypatch.setattr(source, 'read_bytes_beneath', denied)
    result = client_for(background_http).call('artifacts.get', {'artifact_id': identifier})
    assert result['http_status'] == 503 and result['error']['retryable'] is True


def _stream_record(tmp_path, size=17 * 1024 * 1024):
    path = tmp_path / 'output.bin'
    chunk = b'a' * 65536
    digest = hashlib.sha256()
    with path.open('wb') as stream:
        for offset in range(0, size, len(chunk)):
            part = chunk[:min(len(chunk), size - offset)]
            stream.write(part)
            digest.update(part)
    return SimpleNamespace(path=str(path), status='ready', sha256=digest.hexdigest(), size_bytes=size)


def test_verified_stream_memory_bound_and_post_hash_source_change(tmp_path):
    from agent_py_agent.agent.gateway_parts.business_artifact_stream import verified_workflow_stream

    record = _stream_record(tmp_path)
    checks = []
    tracemalloc.start()
    try:
        body = verified_workflow_stream(tmp_path, ('output.bin',), record, start=0,
            end=record.size_bytes-1, check_visible=lambda: checks.append(True))
        try:
            assert stat.S_IMODE(os.fstat(body._stream.fileno()).st_mode) == 0o600
            Path(record.path).write_bytes(b'new unverified source bytes')
            actual, size = hashlib.sha256(), 0
            for chunk in body.chunks():
                assert len(chunk) <= 65536
                actual.update(chunk)
                size += len(chunk)
            assert actual.hexdigest() == record.sha256 and size == record.size_bytes
            assert tracemalloc.get_traced_memory()[1] < 2 * 1024 * 1024
            assert len(checks) == 2
        finally:
            body.close()
        assert body._stream.closed
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize('failure', ['revoke', 'drift', 'symlink'])
def test_stream_failure_closes_private_snapshot_and_never_yields_bytes(tmp_path, monkeypatch, failure):
    from agent_py_agent.agent.gateway_parts import business_artifact_stream as stream_module
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    record = _stream_record(tmp_path, 65536 * 2)
    handles, calls = [], []
    original_temp = stream_module.tempfile.TemporaryFile
    def tracked(*args, **kwargs):
        handle = original_temp(*args, **kwargs)
        handles.append(handle)
        return handle
    monkeypatch.setattr(stream_module.tempfile, 'TemporaryFile', tracked)
    def check():
        calls.append(True)
        if failure == 'revoke' and len(calls) == 2:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if failure == 'drift':
        Path(record.path).write_bytes(b'b' * record.size_bytes)
    if failure == 'symlink':
        foreign = tmp_path / 'private.bin'
        foreign.write_bytes(b'private')
        Path(record.path).unlink()
        Path(record.path).symlink_to(foreign)
    with pytest.raises((HostAPIError, ValueError, OSError)):
        stream_module.verified_workflow_stream(tmp_path, ('output.bin',), record,
            start=0, end=record.size_bytes-1, check_visible=check)
    assert handles and all(handle.closed for handle in handles)


@pytest.mark.parametrize('number', [13, 28])
def test_download_io_denial_or_spool_full_is_unknown_not_false_404(background_http, monkeypatch, number):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import business_artifacts_http as media

    _, _, identifier, _, digest = archive(background_http)
    def failed(*args, **kwargs):
        raise OSError(number, 'private path and details must not be exposed')
    monkeypatch.setattr(media, 'verified_workflow_stream', failed)
    status, _, raw = native_request(background_http, 'GET',
        f'/client/business/artifacts/{identifier}/content?version_ref={digest}')
    assert status == 503 and b'ARTIFACT_CONTENT_UNREADABLE' in raw
    assert b'private path' not in raw
