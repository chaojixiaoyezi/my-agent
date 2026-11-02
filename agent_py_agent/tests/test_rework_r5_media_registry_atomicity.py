"""通过真实业务 HTTP 和实际 nofollow 写入验证注册表发表的失败边界。"""
from __future__ import annotations

import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from agent_py_agent.agent.artifacts import registry
from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.common.directory_lock import locked_private_directory
from agent_py_agent.agent.gateway_parts import business_artifacts_http as media
from agent_py_agent.agent.user_space.owner_policy_seed_payloads import default_quota_payload
from agent_py_agent.tests.test_mega_business_api_actions import native_http as native_http
from agent_py_agent.tests.test_mega_business_api_actions import native_request


def _upload(http, space_id, key, data, *, filename='original.txt'):
    boundary = 'r2-media-boundary'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="space_id"\r\n\r\n{space_id}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="idempotency_key"\r\n\r\n{key}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        'Content-Type: application/octet-stream\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    status, _, raw = native_request(http, 'POST', '/client/business/artifacts', raw=body,
        headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    return status, json.loads(raw)


def _download(http, artifact):
    path = ('/client/business/artifacts/' + artifact['artifact_id']
        + '/content?version_ref=' + artifact['version_ref'])
    return native_request(http, 'GET', path)


def _seed(http):
    # 明确验证所测为本工作树，不让解释器的 editable 安装悄悄切到另一个检出。
    expected = Path(__file__).resolve().parents[1] / 'agent'
    assert Path(media.__file__).resolve() == expected / 'gateway_parts/business_artifacts_http.py'
    assert Path(registry.__file__).resolve() == expected / 'artifacts/registry.py'
    status, _, raw = native_request(http, 'POST', '/client/business', {
        'action': 'spaces.create', 'request_id': 'r2-space-create', 'params': {'name': '原子媒体'},
        'idempotency_key': 'r2-create-space-key'})
    assert status == 200, raw
    space_id = json.loads(raw)['data']['space_id']
    server, _ = http
    services, = server._business_services.values()
    # 轻量 HTTP fixture 未初始化 quota 文件；只用正式默认种子，不放宽配额或替换准入。
    quota = default_quota_payload()
    (services.owner_home / 'quota.json').write_text(json.dumps(quota), encoding='utf-8')
    assert json.loads((services.owner_home / 'quota.json').read_bytes()) == quota
    data = b'a' * (1024 * 1024)
    status, first = _upload(http, space_id, 'r2-seed-upload-key', data)
    assert status == 201, first
    assert first['size_bytes'] == len(data)
    root = services.owner_home / 'artifacts/business'
    return space_id, services, registry.registry_path(root), first, data


def test_short_write_then_one_enospc_preserves_registry_and_recovers(native_http, monkeypatch):
    space_id, services, ledger, first, first_data = _seed(native_http)
    before = ledger.read_bytes()
    blobs_before = set(ledger.parents[2].glob('*.blob'))
    real_open, real_write = nofollow_fs._openat_file, os.write
    target = {}
    writes = []

    def observe_registry_open(parent_fd, name, flags, mode):
        descriptor = real_open(parent_fd, name, flags, mode)
        # 旧版命中追加叶；修复版必须命中真实原子候选，而不是 mock 掉登记调用。
        if flags & os.O_WRONLY and (name == 'registry.jsonl' or name.startswith('.registry.jsonl.tmp-')):
            target.update(fd=descriptor, name=name)
        return descriptor

    def short_then_enospc(descriptor, payload):
        if descriptor == target.get('fd') and len(writes) < 2:
            if not writes:
                written = real_write(descriptor, payload[:23])
                writes.append(written)
                return written
            writes.append('ENOSPC')
            raise OSError(errno.ENOSPC, 'injected registry ENOSPC')
        return real_write(descriptor, payload)

    with monkeypatch.context() as fault:
        fault.setattr(nofollow_fs, '_openat_file', observe_registry_open)
        fault.setattr(nofollow_fs.os, 'write', short_then_enospc)
        status, failed = _upload(native_http, space_id, 'r2-failed-upload-key', b'unconfirmed blob')
    assert status == 503 and failed['error']['code'] == 'OUTCOME_UNKNOWN'
    assert failed['effect_outcome'] == 'unknown'
    assert writes == [23, 'ENOSPC']
    assert ledger.read_bytes() == before
    assert target['name'].startswith('.registry.jsonl.tmp-')
    assert not list(ledger.parent.glob('.registry.jsonl.tmp-*'))
    remaining, = set(ledger.parents[2].glob('*.blob')) - blobs_before
    assert remaining.read_bytes() == b'unconfirmed blob'

    # 清掉服务投影，按原入口重新装配；不手工修复账本或删除未知 blob。
    native_http[0]._business_services.clear()
    assert _download(native_http, first)[::2] == (200, first_data)
    for data in (b'unconfirmed blob', b'do not overwrite'):
        status, replay = _upload(native_http, space_id, 'r2-failed-upload-key', data)
        assert status == 503 and replay['error']['code'] == 'OUTCOME_UNKNOWN'
        assert replay['effect_outcome'] == 'unknown'
        assert remaining.read_bytes() == b'unconfirmed blob'
        assert ledger.read_bytes() == before
    status, later = _upload(native_http, space_id, 'r2-independent-upload-key', b'independent')
    assert status == 201, later
    assert _download(native_http, later)[::2] == (200, b'independent')
    assert _download(native_http, first)[::2] == (200, first_data)
    rows = [json.loads(line) for line in ledger.read_bytes().splitlines()]
    assert len(rows) == 2
    assert {row['artifact_id'] for row in rows} == {first['artifact_id'], later['artifact_id']}
    assert services.owner_home == remaining.parent.parent.parent


def test_post_rename_fsync_failure_keeps_complete_row_and_replay_identity(native_http, monkeypatch):
    space_id, _, ledger, first, first_data = _seed(native_http)
    before = ledger.read_bytes()
    parent = ledger.parent.stat()
    real_fsync = os.fsync
    published = []

    def fail_registry_directory_once(descriptor):
        info = os.fstat(descriptor)
        if not published and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            # 只在 rename 之后的父目录刷盘失败；与 ENOSPC 是互相独立的测试。
            published.append(ledger.read_bytes())
            raise OSError(errno.EIO, 'injected post-rename directory fsync')
        return real_fsync(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(nofollow_fs.os, 'fsync', fail_registry_directory_once)
        status, failed = _upload(native_http, space_id, 'r2-post-rename-key', b'published but unacknowledged')
    assert status == 503 and failed['error']['code'] == 'OUTCOME_UNKNOWN'
    assert failed['effect_outcome'] == 'unknown'
    assert len(published) == 1
    after = ledger.read_bytes()
    assert after == published[0] and after.startswith(before) and after.endswith(b'\n')
    rows = [json.loads(line) for line in after.splitlines()]
    assert len(rows) == 2
    committed = rows[-1]
    assert Path(committed['path']).read_bytes() == b'published but unacknowledged'
    native_http[0]._business_services.clear()
    status, replay = _upload(native_http, space_id, 'r2-post-rename-key', b'published but unacknowledged',
        filename='different-name.txt')
    assert status == 201, replay
    assert replay == media._view(registry._record_from_payload(committed))
    assert replay['name'] == 'original.txt'
    assert ledger.read_bytes() == after
    assert _upload(native_http, space_id, 'r2-post-rename-key', b'changed')[0] == 409
    assert ledger.read_bytes() == after
    assert _download(native_http, first)[::2] == (200, first_data)
    assert _download(native_http, replay)[::2] == (200, b'published but unacknowledged')


def test_concurrent_uploads_keep_every_registry_row_under_existing_lock(native_http):
    space_id, _, ledger, first, _ = _seed(native_http)
    workers = 6
    start = Barrier(workers)

    def upload_one(index):
        data = f'concurrent-body-{index}'.encode()
        start.wait(timeout=10)
        status, artifact = _upload(native_http, space_id, f'r2-concurrent-upload-{index}', data)
        assert status == 201, artifact
        return artifact, data

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(upload_one, range(workers)))
    rows = [json.loads(line) for line in ledger.read_bytes().splitlines()]
    ids = [first['artifact_id'], *(artifact['artifact_id'] for artifact, _ in results)]
    assert len(rows) == len(ids) == len(set(ids))
    assert {row['artifact_id'] for row in rows} == set(ids)
    for artifact, data in results:
        assert _download(native_http, artifact)[::2] == (200, data)


@pytest.mark.parametrize('caller', ['artifact', 'group', 'observed'])
def test_default_callers_still_append_even_with_business_labels(tmp_path, monkeypatch, caller):
    root = tmp_path / 'business_upload'
    root.mkdir()
    path = root / 'business_upload.blob'
    path.write_bytes(b'ordinary')
    real_append = registry.append_text_beneath
    appended = []

    def observe_append(*args, **kwargs):
        appended.append(args[2])
        return real_append(*args, **kwargs)

    monkeypatch.setattr(registry, 'append_text_beneath', observe_append)
    request = registry.ArtifactRegistration(root, path, artifact_id='original-identity',
        status='ready', source='business_upload')

    def register_default():
        if caller == 'observed':
            return registry.register_observed_artifact(request, observed_sha256='trusted', observed_size_bytes=8)
        if caller == 'group':
            return registry.register_artifact_group(registry.ArtifactGroupRegistration(root, [path],
                artifact_id=request.artifact_id, source=request.source))
        return registry.register_artifact(request)

    first = register_default()
    ledger = registry.registry_path(root)
    before, identity = ledger.read_bytes(), ledger.stat().st_ino
    second = register_default()
    assert isinstance(first, registry.ArtifactRegistryRecord)
    assert isinstance(second, registry.ArtifactRegistryRecord)
    assert first.artifact_id == second.artifact_id == request.artifact_id
    assert len(appended) == 2
    assert ledger.stat().st_ino == identity
    assert before == appended[0].encode('utf-8')
    assert ledger.read_bytes() == before + appended[1].encode('utf-8')


def test_atomic_observed_publication_preserves_exact_old_bytes_and_row_encoding(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    path = root / 'original.txt'
    path.write_bytes(b'original')
    registry.register_artifact(registry.ArtifactRegistration(root, path, artifact_id='original'))
    ledger = registry.registry_path(root)
    before = b' \n' + ledger.read_bytes() + b'\n'
    ledger.write_bytes(before)
    request = registry.ArtifactRegistration(root, root / 'unopened.txt', artifact_id='observed',
        status='ready', metadata={'中文': '原样保留'})
    with locked_private_directory(root, lock_name='.business-artifacts.lock'):
        record = registry.register_observed_artifact(request, observed_sha256='trusted-observation',
            observed_size_bytes=42, atomic_publish=True)
    line = (json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    assert ledger.read_bytes() == before + line
    assert record.sha256 == 'trusted-observation' and record.size_bytes == 42
    assert not Path(record.path).exists()


def test_business_media_still_rejects_arbitrary_malformed_registry_rows(native_http):
    space_id, _, ledger, first, _ = _seed(native_http)
    malformed = ledger.read_bytes() + b'{unverified-tail'
    ledger.write_bytes(malformed)
    assert _download(native_http, first)[0] == 503
    status, refused = _upload(native_http, space_id, 'r2-malformed-upload-key', b'must not repair')
    assert status == 503 and refused['error']['code'] == 'STORE_UNAVAILABLE'
    assert ledger.read_bytes() == malformed
