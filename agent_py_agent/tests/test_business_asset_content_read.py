"""正式业务正文页复用原上传/采纳/内容resolver，归档read_artifact保持分离。"""
import json
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_context import resolve_business_context
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_asset_read as reader_module
from agent_py_agent.agent.gateway_parts.business_artifact_pages import BLOCK_BYTES
from agent_py_agent.agent.gateway_parts.business_artifacts_http import (
    artifact_content,
    upload_artifact,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests.test_business_asset_version_freeze import approve
from agent_py_agent.tests.test_business_model_tools import agent as agent


@pytest.fixture
def material(agent, tmp_path, request):
    data = getattr(request, 'param', b'nonce-only-in-formal-upload\nvalue=137\n')
    agent.config.business_host_ports_enabled = True
    context, services = api.local_business_context(agent, model=True)
    actor = services.actor(context)
    sid = services.spaces.create({'name': '正式正文测试'}, actor=actor, idempotency_key='reader-space-key-0001')['space_id']
    uploaded = upload_artifact(BytesIO(data), {'space_id': sid, 'idempotency_key': 'reader-upload-key-0001'},
        'formal.txt', context, services)
    ref = {k: uploaded[k] for k in ('artifact_id', 'version_ref', 'content_sha256')}
    created = services.spaces.call('assets.register', {'space_id': sid, 'name': '输入资料', 'kind': 'document',
        'artifact_ref': ref}, actor=actor, idempotency_key='reader-register-key-0001')
    api.register_business_tools(agent)
    accept = {'action': 'assets.versions.accept', 'request_id': 'reader-adopt-request',
        'idempotency_key': 'reader-adoption-key-0001', 'expected_revision': 0,
        'params': {'space_id': sid, 'asset_id': created['asset_id'], 'version_ref': ref['version_ref']}}
    approve(agent, tmp_path / 'approval', agent.tools.tools['business_assets_versions_accept'], accept)
    params = dict(accept['params'])
    with reader_module._PROOF_LOCK:
        reader_module._PROOFS.clear()
    return SimpleNamespace(agent=agent, context=context, services=services, actor=actor, params=params,
        ref=ref, body=data, tool=agent.tools.tools['read_business_asset'], root=services.owner_home)


def pages(m, maximum=8):
    result, rows, cursor = b'', [], None
    for _ in range(100):
        params = {**m.params, 'max_bytes': maximum, **({'cursor': cursor} if cursor else {})}
        row = m.tool.read_page(params)
        result += row['content'].encode()
        rows.append(row)
        cursor = row['next_cursor']
        if cursor is None:
            return result, rows
    pytest.fail('正文页不能空页循环')


def test_read_real_adopted_upload_without_workspace_copy(material):
    m = material
    assert not list(m.agent.root.rglob('formal.txt'))
    data, rows = pages(m)
    assert data == m.body and rows[-1]['complete'] and rows[-1]['next_cursor'] is None
    assert all(x['page_integrity_verified'] and x['version_ref'] == m.ref['version_ref'] for x in rows)
    status, _, original = artifact_content(m.ref['artifact_id'], m.ref['version_ref'], m.context, m.services)
    assert status == 200 and original == data


@pytest.mark.parametrize('material', ['甲乙丙丁🙂\n正文nonce'.encode()], indirect=True)
def test_utf8_page_boundaries_never_lose_bytes_or_stall(material):
    content, rows = pages(material, 4)
    assert content == material.body and all(x['returned_bytes'] > 0 for x in rows)
    assert all(x['returned_bytes'] <= 4 for x in rows)


@pytest.mark.parametrize('mutation', ['space', 'asset', 'version', 'project', 'cursor_cut', 'cursor_tamper'])
def test_scope_guesses_and_truncated_cursor_never_return_body(material, mutation):
    m = material
    first = m.tool.read_page({**m.params, 'max_bytes': 4})
    params = {**m.params, 'max_bytes': 4}
    if mutation in ('space', 'asset', 'version'):
        params[mutation + '_id' if mutation != 'version' else 'version_ref'] = 'unavailable-guessed-ref'
    elif mutation == 'project':
        params['project_ref'] = 'other-project'
    elif mutation == 'cursor_cut':
        params['cursor'] = first['next_cursor'][:-7]
    else:
        params['cursor'] = first['next_cursor'][:-1] + ('A' if first['next_cursor'][-1] != 'A' else 'B')
    result = m.tool.execute(params)
    assert not result.ok and result.effect_outcome == 'not_started'
    error = json.loads(result.output)
    assert 'content' not in error and 'nonce-only' not in result.output
    assert error['next_action']


@pytest.mark.parametrize('material', [b'%PDF-1.7\nnot-text-media', b'a\x00b', b'\xff\xfe\x00\x01'], indirect=True)
def test_binary_or_invalid_utf8_never_exposes_text(material):
    result = material.tool.execute(material.params)
    assert not result.ok and result.error_code == 'BINARY_CONTENT'
    assert 'content' not in json.loads(result.output)


def _candidate(m, data=b'unapproved candidate nonce'):
    uploaded = upload_artifact(BytesIO(data), {'space_id': m.params['space_id'], 'idempotency_key': 'candidate-upload-key-0001'},
        'candidate.txt', m.context, m.services)
    ref = {k: uploaded[k] for k in ('artifact_id', 'version_ref', 'content_sha256')}
    m.services.spaces.call('assets.versions.add', {'space_id': m.params['space_id'], 'asset_id': m.params['asset_id'],
        'artifact_ref': ref}, actor=m.actor, expected_revision=1, idempotency_key='candidate-add-key-0001')
    return ref


def test_explicit_candidate_review_keeps_existing_authority_but_not_execution_default(material):
    m = material
    candidate = _candidate(m)
    params = {**m.params, 'version_ref': candidate['version_ref']}
    denied = m.tool.execute(params)
    assert not denied.ok and denied.error_code == 'VERSION_SCOPE_MISMATCH'
    read = m.tool.read_page({**params, 'selection': 'review'})
    assert read['content'] == 'unapproved candidate nonce'
    assert m.services.spaces.call('assets.get', {'space_id': m.params['space_id'], 'asset_id': m.params['asset_id']},
        actor=m.actor)['accepted_version'] == m.params['version_ref']


def _freeze(m, monkeypatch):
    frozen = resolve_business_context(actor=m.actor, task_id='reader-frozen-task',
        requested_space_id=m.params['space_id'], project_ref=None, binding_id=None)
    issued = api.bind_business_context(m.agent, authorize=m.context.authorize, subject_id=m.context.subject_id,
        actor=m.context.actor, business_context=frozen)
    monkeypatch.setattr(api, 'local_business_context', lambda *_a, **_k: (issued, m.services))
    return frozen


def test_frozen_source_cannot_escape_via_review_flag(material, monkeypatch):
    m = material
    _freeze(m, monkeypatch)
    candidate = _candidate(m)
    result = m.tool.execute({**m.params, 'version_ref': candidate['version_ref'], 'selection': 'review'})
    assert not result.ok and result.error_code == 'VERSION_SCOPE_MISMATCH'
    assert m.tool.read_page(m.params)['content'].encode() == m.body


def test_frozen_old_version_survives_new_accepted_without_switching_latest(material, monkeypatch, tmp_path):
    m = material
    _freeze(m, monkeypatch)
    candidate = _candidate(m)
    # 原Host批准输入只用于新版本采用；当前reader仍使用先前host-only签发的冻结上下文。
    request = {'action': 'assets.versions.accept', 'request_id': 'reader-accept-v2',
        'idempotency_key': 'reader-adopt-new-version-0001', 'expected_revision': 2,
        'params': {**m.params, 'version_ref': candidate['version_ref']}}
    approve(m.agent, tmp_path / 'approval-new', m.agent.tools.tools['business_assets_versions_accept'], request)
    assert m.tool.read_page(m.params)['content'].encode() == m.body
    assert not m.tool.execute({**m.params, 'version_ref': candidate['version_ref']}).ok


@pytest.mark.parametrize('material', [b'x' * (BLOCK_BYTES * 3 + 16)], indirect=True, ids=['three-block-text'])
def test_second_page_whole_chain_only_hashes_covered_block(material, monkeypatch):
    m = material
    first = m.tool.read_page({**m.params, 'max_bytes': BLOCK_BYTES})
    blob = m.root / 'artifacts/business' / (m.ref['artifact_id'] + '.blob')
    import os
    original, count = os.read, []
    inode = blob.stat().st_ino
    def measured(fd, size):
        data = original(fd, size)
        if os.fstat(fd).st_ino == inode:
            count.append(len(data))
        return data
    monkeypatch.setattr(os, 'read', measured)
    second = m.tool.read_page({**m.params, 'cursor': first['next_cursor'], 'max_bytes': 16})
    assert second['content'] == 'x' * 16 and sum(count) == BLOCK_BYTES


@pytest.mark.parametrize('material', [b'x' * (BLOCK_BYTES * 2 + 16)], indirect=True, ids=['same-tick-changed-block'])
def test_same_stat_tick_changed_page_is_rejected_even_when_fingerprint_collides(material, monkeypatch):
    m = material
    first = m.tool.read_page({**m.params, 'max_bytes': BLOCK_BYTES})
    blob = m.root / 'artifacts/business' / (m.ref['artifact_id'] + '.blob')
    import os
    before = blob.stat()
    # 模拟真实同tick碰撞：所有metadata观察均相同，只有FD正文已改变。
    for name in ('stat', 'lstat', 'fstat'):
        original = getattr(os, name)
        def same_stat(*args, _original=original, **kwargs):
            observed = _original(*args, **kwargs)
            return before if observed.st_ino == before.st_ino and observed.st_dev == before.st_dev else observed
        for support in ('supports_dir_fd', 'supports_fd', 'supports_follow_symlinks'):
            members = getattr(os, support)
            if original in members:
                monkeypatch.setattr(os, support, members | {same_stat})
        monkeypatch.setattr(os, name, same_stat)
    with blob.open('r+b') as stream:
        stream.seek(BLOCK_BYTES)
        stream.write(b'Y')
    with pytest.raises(HostAPIError) as caught:
        m.tool.read_page({**m.params, 'cursor': first['next_cursor'], 'max_bytes': 16})
    assert caught.value.code == 'ARTIFACT_CHANGED'
    result = m.tool.execute({**m.params, 'cursor': first['next_cursor'], 'max_bytes': 16})
    assert not result.ok and result.error_code == 'ARTIFACT_CHANGED'
    assert 'content' not in json.loads(result.output)
    # 原HTTP默认入口仍核整文件；不能因分页proof或同stat标记省掉原完整SHA。
    with pytest.raises(HostAPIError, match='ARTIFACT_CHANGED'):
        artifact_content(m.ref['artifact_id'], m.ref['version_ref'], m.context, m.services)


def test_selected_default_space_does_not_revoke_explicit_authorized_asset(material, monkeypatch):
    m = material
    context = api.bind_business_context(m.agent, authorize=m.context.authorize,
        subject_id=m.context.subject_id, actor=m.context.actor, selected_space_id='another-default-space')
    monkeypatch.setattr(api, 'local_business_context', lambda *_a, **_k: (context, m.services))
    assert m.tool.read_page(m.params)['content'].encode() == m.body


def test_revoked_original_actor_cannot_reuse_cursor_or_see_body(material, monkeypatch):
    m = material
    first = m.tool.read_page({**m.params, 'max_bytes': 4})
    denied = api.bind_business_context(m.agent, authorize=lambda *_: False,
        subject_id=m.context.subject_id, actor=m.context.actor)
    monkeypatch.setattr(api, 'local_business_context', lambda *_a, **_k: (denied, m.services))
    result = m.tool.execute({**m.params, 'cursor': first['next_cursor']})
    assert not result.ok and result.effect_outcome == 'not_started'
    assert 'content' not in json.loads(result.output) and 'nonce-only' not in result.output


@pytest.mark.parametrize('part', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_short_cursor_authenticates_every_canonical_binding_part(material, part):
    m = material
    binding = m.tool._binding(m.params)[-1]
    token = m.tool._cursor(binding, 8)
    assert len(token) == 66 and m.tool._position(token, binding) == 8
    altered = list(binding)
    altered[part] = 'another-canonical-source'
    with pytest.raises(HostAPIError, match='CURSOR_EXPIRED'):
        m.tool._position(token, altered)


def test_short_cursor_expiry_and_header_before_large_body(material, monkeypatch):
    m = material
    row = m.tool.read_page({**m.params, 'max_bytes': 4})
    token = row['next_cursor']
    encoded = json.dumps({**row, 'content': 'x' * 20000}, ensure_ascii=False)
    assert encoded.index('next_cursor') < encoded.index('content"')
    assert token in encoded[:1200] and 'original_version_blocks' in encoded[:1200]
    monkeypatch.setattr(reader_module.time, 'time', lambda: 10**12)
    with pytest.raises(HostAPIError, match='CURSOR_EXPIRED'):
        m.tool._position(token, m.tool._binding(m.params)[-1])


def test_large_old_upload_registry_first_read_then_short_cursor_uses_exact_row(material, monkeypatch):
    from agent_py_agent.agent.artifacts.registry import REGISTRY_RELATIVE_PATH
    from agent_py_agent.agent.gateway_parts import business_upload_registry as registry

    m = material
    path = m.root / 'artifacts' / 'business' / REGISTRY_RELATIVE_PATH
    original = json.loads(path.read_text().strip().splitlines()[0])
    rows = [original]
    for index in range(2200):
        rows.append({**original, 'artifact_id': 'artifact-' + f'{index:040x}',
                     'metadata': {**original['metadata'], 'history': 'x' * 900}})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    assert path.stat().st_size > 2 * 1024 * 1024
    first = m.tool.read_page({**m.params, 'max_bytes': 4})
    assert first['content'].encode() == m.body[:4] and len(first['next_cursor']) < 100
    monkeypatch.setattr(registry, 'lookup_business_upload',
        lambda *_args, **_kwargs: pytest.fail('续页不能重新扫描完整旧登记簿'))
    second = m.tool.read_page({**m.params, 'cursor': first['next_cursor'], 'max_bytes': 4})
    assert second['content'].encode() == m.body[4:8]
    # 原记录消失不可继续沿缓存proof发正文。
    path.unlink()
    denied = m.tool.execute({**m.params, 'cursor': second['next_cursor'], 'max_bytes': 4})
    assert not denied.ok and 'content' not in json.loads(denied.output)


def test_changed_registry_rejects_old_page_but_fresh_same_version_recovers(material):
    from agent_py_agent.agent.artifacts.registry import REGISTRY_RELATIVE_PATH

    m = material
    first = m.tool.read_page({**m.params, 'max_bytes': 4})
    path = m.root / 'artifacts' / 'business' / REGISTRY_RELATIVE_PATH
    # 相同合法登记原子换代；旧cursor不能把旧source证明当新权限。
    replacement = path.with_name('replacement.jsonl')
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(path)
    old = m.tool.execute({**m.params, 'cursor': first['next_cursor'], 'max_bytes': 4})
    assert not old.ok and 'content' not in json.loads(old.output)
    fresh = m.tool.read_page({**m.params, 'max_bytes': 4})
    assert fresh['content'].encode() == m.body[:4]
    assert fresh['version_ref'] == first['version_ref'] and fresh['next_cursor']
    next_page = m.tool.read_page({**m.params, 'cursor': fresh['next_cursor'], 'max_bytes': 4})
    assert next_page['content'].encode() == m.body[4:8]
