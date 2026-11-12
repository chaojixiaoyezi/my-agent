"""上传原引用经真实客户端、HTTP、资产服务往返；只使用隔离的原事实源。"""
from __future__ import annotations

import hashlib
import http.client
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.artifacts.registry import (
    ArtifactRegistration,
    latest_artifact_records,
    register_artifact,
    register_observed_artifact,
)
from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_client_context import (
    GatewayChatClientAgent,
    _gateway_request_identity,
    owner_identity_from_config,
)
from agent_py_agent.cli.space_commands import business_client_for_agent
from agent_py_agent.tests.test_business_v12_background_scheduling import native as native


@pytest.fixture
def chain(native, tmp_path):
    server, agent = native
    wire_business_host_ports(agent)
    client = GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(
        gateway_port=server.server.server_address[1], gateway_auth_token=''),
        tmp_path, [tmp_path], SimpleNamespace(root=agent.home_paths.root),
        owner_identity=OwnerIdentity.local_main())
    return native, business_client_for_agent(client)


def request(native, method, path, body=None, content_type='application/json'):
    server, agent = native
    identity = _gateway_request_identity(owner_identity_from_config(agent.config))
    headers = {'X-Gateway-Token': server._local_client_credential,
               'X-User-Id': identity['user_id'], 'X-Channel': identity['channel'],
               'Content-Type': content_type}
    conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=30)
    try:
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def invoke(client, action, params, **kwargs):
    return client.send(client.prepare(action, params, **kwargs))


def space(client, name='upload-space'):
    result = client.call('spaces.create', {'name': name})
    assert result['ok'] is True, result
    return result['data']['space_id']


def upload(native, sid, key='acceptance-upload', data=b'original uploaded bytes\n'):
    boundary = 'acceptance-artifact-boundary'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="space_id"\r\n\r\n{sid}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="idempotency_key"\r\n\r\n{key}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="orders.txt"\r\n'
            'Content-Type: text/plain\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    status, raw = request(native, 'POST', '/client/business/artifacts', body,
                          'multipart/form-data; boundary=' + boundary)
    assert status == 201, raw
    result = json.loads(raw)
    assert result['content_sha256'] == hashlib.sha256(data).hexdigest()
    return result


def register(client, sid, artifact, **kwargs):
    ref = {key: artifact[key] for key in ('artifact_id', 'version_ref', 'content_sha256')}
    return invoke(client, 'assets.register', {'space_id': sid, 'name': 'orders',
                  'kind': 'document', 'artifact_ref': ref}, **kwargs)


def test_upload_original_reference_client_envelope_roundtrip(chain):
    native, client = chain
    sid = space(client)
    original = upload(native, sid)
    route = ('/client/business/artifacts/' + original['artifact_id']
             + '/content?version_ref=' + original['version_ref'])
    assert request(native, 'GET', route) == (200, b'original uploaded bytes\n')
    result = register(client, sid, original, idempotency_key='register-original')
    assert (result['ok'], result['http_status'], result['state'], result['effect_outcome']) == (
        True, 200, 'completed', 'committed'), result
    assert result['error'] is None and result['data']['version_ref'] == original['version_ref']
    assert result['request_id'] and result['operation_id'] is None
    replay = register(client, sid, original, idempotency_key='register-original')
    assert replay['ok'] is True and replay['data'] == result['data']
    versions = client.call('assets.versions.list', {'space_id': sid, 'asset_id': result['data']['asset_id']})
    assert versions['ok'] is True
    assert versions['data']['items'][0]['artifact_ref'] == {
        key: original[key] for key in ('artifact_id', 'version_ref', 'content_sha256')}


@pytest.mark.parametrize('case', ['space', 'artifact', 'version', 'hash', 'missing', 'changed', 'symlink'])
def test_uploaded_reference_denials_leave_no_asset(chain, case):
    native, client = chain
    sid = space(client)
    original = upload(native, sid)
    blob = native[1].home_paths.owner_home_dir / 'artifacts' / 'business' / (original['artifact_id'] + '.blob')
    if case == 'space':
        sid = space(client, 'other-space')
    elif case == 'artifact':
        original['artifact_id'] = 'artifact-' + '0' * 40
    elif case == 'version':
        original['version_ref'] = 'version-' + '0' * 40
    elif case == 'hash':
        original['content_sha256'] = '0' * 64
    elif case == 'missing':
        blob.unlink()
    elif case == 'changed':
        blob.write_bytes(b'different bytes\n')
    else:
        target = blob.with_name('target.blob')
        target.write_bytes(blob.read_bytes())
        blob.unlink()
        blob.symlink_to(target)
    result = register(client, sid, original)
    assert result['ok'] is False and result['state'] == 'failed', result
    assert result['http_status'] == 404 and result['effect_outcome'] == 'not_started', result
    assert result['error']['code'] in {'REFERENCE_NOT_FOUND', 'SPACE_SCOPE_MISMATCH'}, result
    listed = client.call('assets.list', {'space_id': sid})
    assert listed['ok'] is True and listed['data']['items'] == []


def test_legacy_registered_reference_keeps_hash_validation(chain):
    native, client = chain
    sid = space(client)
    root = native[1].home_paths.owner_home_dir
    path = root / 'artifacts' / 'legacy.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'legacy document')
    record = register_artifact(ArtifactRegistration(root, path, artifact_id=path.name, status='ready'))
    original = {'artifact_id': record.artifact_id, 'version_ref': record.artifact_id,
                'content_sha256': record.sha256}
    assert register(client, sid, original)['ok'] is True
    original['content_sha256'] = '0' * 64
    assert register(client, sid, original)['ok'] is False


@pytest.mark.parametrize('same_space', [False, True])
def test_identical_uploads_keep_distinct_original_references(chain, same_space):
    native, client = chain
    first = space(client)
    other = first if same_space else space(client, 'other-space')
    a = upload(native, first, 'acceptance-same-bytes-a')
    b = upload(native, other, 'acceptance-same-bytes-b')
    assert a['content_sha256'] == b['content_sha256']
    assert a['version_ref'] != b['version_ref']
    assert register(client, first, a)['ok'] is True
    assert register(client, other, b)['ok'] is True
    if not same_space:
        assert register(client, other, a)['ok'] is False


def append_upload_record(native, artifact, **changes):
    root = native[1].home_paths.owner_home_dir / 'artifacts' / 'business'
    record = replace(latest_artifact_records(root)[artifact['artifact_id']], **changes)
    register_observed_artifact(ArtifactRegistration(root, record.path, artifact_id=record.artifact_id,
        metadata=record.metadata, source=record.source, status=record.status),
        observed_sha256=record.sha256, observed_size_bytes=record.size_bytes)


@pytest.mark.parametrize('case', ['owner', 'path', 'status', 'registry-missing', 'registry-malformed'])
def test_canonical_upload_facts_must_remain_valid(chain, case):
    native, client = chain
    sid = space(client)
    original = upload(native, sid)
    root = native[1].home_paths.owner_home_dir / 'artifacts' / 'business'
    registry = root / 'data' / 'artifacts' / 'registry.jsonl'
    if case == 'owner':
        record = latest_artifact_records(root)[original['artifact_id']]
        append_upload_record(native, original, metadata={**record.metadata, 'owner_id': 'other-owner'})
    elif case == 'path':
        append_upload_record(native, original, path=str(root / 'other.blob'))
    elif case == 'status':
        append_upload_record(native, original, status='missing')
    elif case == 'registry-missing':
        registry.unlink()
    else:
        registry.write_text('{invalid registry}\n')
    result = register(client, sid, original)
    status, code = (503, 'STORE_UNAVAILABLE') if case == 'registry-malformed' else (404, 'REFERENCE_NOT_FOUND')
    assert (result['ok'], result['http_status'], result['effect_outcome']) == (False, status, 'not_started'), result
    assert result['error']['code'] == code


def test_existing_content_based_version_resolves_without_rewriting(chain):
    native, client = chain
    sid = space(client)
    original = upload(native, sid)
    root = native[1].home_paths.owner_home_dir / 'artifacts' / 'business'
    record = latest_artifact_records(root)[original['artifact_id']]
    original['version_ref'] = 'version-' + original['content_sha256'][:40]
    append_upload_record(native, original, metadata={**record.metadata, 'version_ref': original['version_ref']})
    before = (root / 'data/artifacts/registry.jsonl').read_bytes()
    assert register(client, sid, original)['ok'] is True
    assert (root / 'data/artifacts/registry.jsonl').read_bytes() == before


def test_ambiguous_existing_versions_fail_closed(chain):
    native, client = chain
    sid = space(client)
    a = upload(native, sid, 'acceptance-legacy-a')
    b = upload(native, sid, 'acceptance-legacy-b')
    root = native[1].home_paths.owner_home_dir / 'artifacts' / 'business'
    for artifact in (a, b):
        record = latest_artifact_records(root)[artifact['artifact_id']]
        artifact['version_ref'] = 'version-' + artifact['content_sha256'][:40]
        append_upload_record(native, artifact, metadata={**record.metadata, 'version_ref': artifact['version_ref']})
    for artifact in (a, b):
        result = register(client, sid, artifact)
        assert result['ok'] is False and result['error']['code'] == 'REFERENCE_NOT_FOUND'


def test_upload_reference_cannot_be_registered_by_another_owner(chain, tmp_path):
    native, client = chain
    original = upload(native, space(client))
    server, agent = native
    other = GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(
        gateway_port=server.server.server_address[1], gateway_auth_token=''),
        tmp_path, [tmp_path], SimpleNamespace(root=agent.home_paths.root),
        owner_identity=OwnerIdentity.provider_user('feishu', 'asset-other-owner'))
    other_client = business_client_for_agent(other)
    sid = space(other_client, 'other-owner-space')
    result = register(other_client, sid, original)
    assert (result['ok'], result['http_status'], result['effect_outcome']) == (False, 404, 'not_started'), result
    assert result['error']['code'] == 'REFERENCE_NOT_FOUND'


def test_uploaded_candidate_versions_share_the_same_reference_checks(chain):
    native, client = chain
    sid = space(client)
    first = upload(native, sid)
    asset = register(client, sid, first)
    assert asset['ok'] is True
    second = upload(native, sid, 'acceptance-version-two', b'updated uploaded bytes\n')
    ref = {key: second[key] for key in ('artifact_id', 'version_ref', 'content_sha256')}
    params = {'space_id': sid, 'asset_id': asset['data']['asset_id'], 'artifact_ref': ref}
    rejected = invoke(client, 'assets.versions.add', {**params, 'artifact_ref': {
        **ref, 'content_sha256': '0' * 64}}, expected_revision=0)
    assert rejected['ok'] is False and rejected['error']['code'] == 'REFERENCE_NOT_FOUND'
    added = invoke(client, 'assets.versions.add', params, expected_revision=0)
    assert added['ok'] is True and added['data']['state'] == 'candidate'
    assert added['data']['version_ref'] == second['version_ref']
    versions = client.call('assets.versions.list', {'space_id': sid, 'asset_id': asset['data']['asset_id']})
    assert versions['ok'] is True
    assert {row['version_ref'] for row in versions['data']['items']} == {first['version_ref'], second['version_ref']}


@pytest.mark.parametrize('action', ['assets.register', 'assets.versions.add'])
@pytest.mark.parametrize('corrupt', [False, True], ids=['valid-ledger', 'invalid-utf8'])
def test_registry_utf8_failure_is_storage_error_without_mutation(chain, action, corrupt):
    native, client = chain
    sid = space(client)
    first = upload(native, sid)
    asset = register(client, sid, first)
    assert asset['ok'] is True
    second = upload(native, sid, 'acceptance-utf8-second', b'second uploaded bytes\n')
    root = native[1].home_paths.owner_home_dir
    registry = root / 'artifacts/business/data/artifacts/registry.jsonl'
    catalog = root / 'data/business/catalog.json'
    before_catalog = catalog.read_bytes()
    if corrupt:
        with registry.open('ab') as stream:
            stream.write(b'\xff\n')
    before_registry = registry.read_bytes()
    if action == 'assets.register':
        result = register(client, sid, second)
    else:
        ref = {key: second[key] for key in ('artifact_id', 'version_ref', 'content_sha256')}
        result = invoke(client, action, {'space_id': sid, 'asset_id': asset['data']['asset_id'],
                        'artifact_ref': ref}, expected_revision=0)
    # 即使错误分类断言失败，也先核对拒绝没有改写账本或资产/版本事实。
    assert registry.read_bytes() == before_registry
    if corrupt:
        assert catalog.read_bytes() == before_catalog
        assert (result['ok'], result['http_status'], result['effect_outcome']) == (
            False, 503, 'not_started'), result
        assert result['error']['code'] == 'STORE_UNAVAILABLE', result
    else:
        assert (result['ok'], result['http_status'], result['effect_outcome']) == (
            True, 200, 'committed'), result
        after = json.loads(catalog.read_bytes())['assets']
        before = json.loads(before_catalog)['assets']
        if action == 'assets.register':
            assert len(after) == len(before) + 1
            assert after[result['data']['asset_id']]['versions'][0]['version_ref'] == second['version_ref']
        else:
            aid = asset['data']['asset_id']
            assert len(after) == len(before)
            assert len(after[aid]['versions']) == len(before[aid]['versions']) + 1
            assert after[aid]['revision'] == before[aid]['revision'] + 1
            assert after[aid]['versions'][-1]['version_ref'] == second['version_ref']
