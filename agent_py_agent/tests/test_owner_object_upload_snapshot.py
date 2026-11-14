"""原full提交须把实际上传字节绑定内容键；普通文件换名不能污染对象。"""
import hashlib
from pathlib import Path

import pytest

from agent_py_agent.agent import owner_object_store as module
from agent_py_agent.tests.test_owner_object_store import _Objects, _store


def test_full_commit_rename_between_digest_and_upload_never_publishes_wrong_content(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    source = home / 'document.txt'
    source.write_bytes(b'original')
    client = _Objects()
    store = _store(client)
    committed = []
    store._replace_manifest = lambda _t, _k, _o, rows: committed.extend(rows)
    upload = store._upload

    def replace_source_then_upload(path, key):
        replacement = tmp_path / 'replacement.txt'
        replacement.write_bytes(b'replacement')
        replacement.replace(source)
        upload(path, key)

    monkeypatch.setattr(store, '_upload', replace_source_then_upload)
    try:
        store.commit('tenant-a', 'user', 'owner-a', home)
    except RuntimeError:
        pass
    assert all(hashlib.sha256(body).hexdigest() == key.rsplit('/', 1)[-1]
               for (_, key), body in client.values.items())
    assert not committed


def test_existing_wrong_object_never_becomes_a_successful_manifest(tmp_path):
    payload = b'correct'
    source = tmp_path / 'source.txt'
    source.write_bytes(payload)
    client = _Objects()
    store = _store(client)
    key = store._object_key('tenant-a', hashlib.sha256(payload).hexdigest())
    client.values[('owners', key)] = b'corrupt'
    committed = []
    store._replace_manifest = lambda *_args: committed.append(True)
    with pytest.raises(RuntimeError, match='已存在对象摘要或大小不符'):
        store.commit('tenant-a', 'user', 'owner-a', tmp_path)
    assert not committed
    assert client.values[('owners', key)] == b'corrupt'


@pytest.mark.parametrize('close_fails', [False, True])
def test_existing_object_stream_is_bounded_and_closed_on_error(tmp_path, close_fails):
    from io import BytesIO
    source = tmp_path / 'large.bin'
    source.write_bytes(b'x' * 131072)
    client = _Objects()
    store = _store(client)
    key = store._object_key('tenant-a', hashlib.sha256(source.read_bytes()).hexdigest())
    client.values[('owners', key)] = source.read_bytes()

    class FailingBody(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            raise OSError('synthetic transport failure')

        def close(self):
            super().close()
            if close_fails:
                raise OSError('synthetic close failure')

    body = FailingBody(b'x')
    client.get_object = lambda **_kwargs: {'Body': body}
    committed = []
    store._replace_manifest = lambda *_args: committed.append(True)
    with pytest.raises(OSError, match='synthetic transport failure'):
        store.commit('tenant-a', 'user', 'owner-a', tmp_path)
    assert body.closed
    assert not committed


def test_repeated_commit_deduplicates_upload_and_closes_verified_stream(tmp_path):
    from io import BytesIO
    source = tmp_path / 'source.txt'
    source.write_bytes(b'repeated')
    client = _Objects()
    store = _store(client)
    store._replace_manifest = lambda *_args: None
    uploaded, reads = [], []
    original = client.upload_fileobj

    def upload(path, bucket, key, *, ExtraArgs):
        uploaded.append(True)
        original(path, bucket, key, ExtraArgs=ExtraArgs)

    def get_object(*, Bucket, Key):
        body = BytesIO(client.values[(Bucket, Key)])
        reads.append(body)
        return {'Body': body}

    client.upload_fileobj, client.get_object = upload, get_object
    assert store.commit('tenant-a', 'user', 'owner-a', tmp_path) == 1
    assert store.commit('tenant-a', 'user', 'owner-a', tmp_path) == 1
    assert len(uploaded) == 1
    assert reads and all(body.closed for body in reads)


def test_upload_failure_cleans_private_stage_without_committing(tmp_path):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'original')
    client = _Objects()
    store = _store(client)
    staged, committed = [], []

    def fail(path, _bucket, _key, *, ExtraArgs):
        staged.append(path)
        assert not path.closed
        raise OSError('synthetic upload failure')

    client.upload_fileobj = fail
    store._replace_manifest = lambda *_args: committed.append(True)
    with pytest.raises(OSError, match='synthetic upload failure'):
        store.commit('tenant-a', 'user', 'owner-a', tmp_path)
    assert staged and staged[0].closed
    assert not committed
    assert source.read_bytes() == b'original'


def test_same_content_concurrent_uploads_never_disagree_with_shared_key(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client = _Objects()
    barrier = Barrier(2)
    head = client.head_object

    def concurrent_head(*, Bucket, Key):
        try:
            return head(Bucket=Bucket, Key=Key)
        except RuntimeError:
            barrier.wait(timeout=5)
            raise

    client.head_object = concurrent_head
    homes = [tmp_path / 'a', tmp_path / 'b']
    for home in homes:
        home.mkdir()
        (home / 'same.txt').write_bytes(b'one content')
    stores = [_store(client), _store(client)]
    for store in stores:
        store._replace_manifest = lambda *_args: None
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(store.commit, 'tenant-a', 'user', str(i), home)
                   for i, (store, home) in enumerate(zip(stores, homes))]
        assert [future.result(timeout=10) for future in futures] == [1, 1]
    assert len(client.values) == 1
    assert all(hashlib.sha256(body).hexdigest() == key.rsplit('/', 1)[-1]
               for (_, key), body in client.values.items())


def test_parent_link_replacement_cannot_read_foreign_owner(tmp_path, monkeypatch):
    home, foreign = tmp_path / 'home', tmp_path / 'foreign'
    home.mkdir()
    foreign.mkdir()
    (home / 'folder').mkdir()
    (home / 'folder/source.txt').write_bytes(b'local')
    (foreign / 'source.txt').write_bytes(b'foreign secret fixture')
    client = _Objects()
    store = _store(client)
    committed = []
    store._replace_manifest = lambda *_args: committed.append(True)
    stage = module._stage_full_owner_file

    def replace_parent(root, parts, staging, *, expected_stat, expected_root=None):
        (home / 'folder').rename(tmp_path / 'original-folder')
        (home / 'folder').symlink_to(foreign, target_is_directory=True)
        return stage(root, parts, staging, expected_stat=expected_stat, expected_root=expected_root)

    monkeypatch.setattr(module, '_stage_full_owner_file', replace_parent)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        store.commit('tenant-a', 'user', 'owner-a', home)
    assert not committed and not client.values


def test_only_service_full_object_checksum_can_skip_existing_body_read(tmp_path):
    import base64
    payload = b'correct'
    source = tmp_path / 'source.txt'
    source.write_bytes(payload)
    client = _Objects()
    store = _store(client)
    store._replace_manifest = lambda *_args: None
    checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode('ascii')
    client.head_object = lambda **_kwargs: {'ChecksumSHA256': checksum, 'ChecksumType': 'FULL_OBJECT',
                                          'ContentLength': len(payload)}
    client.get_object = lambda **_kwargs: pytest.fail('可信完整对象摘要命中不应另读正文')
    assert store.commit('tenant-a', 'user', 'owner-a', tmp_path) == 1


def test_metadata_and_composite_checksum_cannot_hide_wrong_existing_body(tmp_path):
    import base64
    payload = b'correct'
    source = tmp_path / 'source.txt'
    source.write_bytes(payload)
    client = _Objects()
    store = _store(client)
    key = store._object_key('tenant-a', hashlib.sha256(payload).hexdigest())
    client.values[('owners', key)] = b'corrupt'
    client.head_object = lambda **_kwargs: {
        'ChecksumSHA256': base64.b64encode(hashlib.sha256(payload).digest()).decode('ascii'),
        'ChecksumType': 'COMPOSITE', 'ContentLength': len(payload),
        'Metadata': {'sha256': hashlib.sha256(payload).hexdigest()}, 'ETag': 'not-a-sha256'}
    store._replace_manifest = lambda *_args: pytest.fail('坏对象不能提交')
    with pytest.raises(RuntimeError, match='已存在对象摘要或大小不符'):
        store.commit('tenant-a', 'user', 'owner-a', tmp_path)


def test_parent_link_after_upload_cannot_publish_even_the_same_original_inode(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    folder = home / 'folder'
    folder.mkdir()
    (folder / 'source.txt').write_bytes(b'original')
    store = _store(_Objects())
    committed = []
    store._replace_manifest = lambda *_args: committed.append(True)
    upload = store._upload

    def move_parent_then_return(path, key):
        upload(path, key)
        moved = tmp_path / 'outside-owner'
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)

    monkeypatch.setattr(store, '_upload', move_parent_then_return)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        store.commit('tenant-a', 'user', 'owner-a', home)
    assert not committed


def test_owner_controlled_temp_root_is_rejected_before_upload(tmp_path, monkeypatch):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'original')
    client = _Objects()
    store = _store(client)
    store._replace_manifest = lambda *_args: pytest.fail('不可信暂存根不能提交')
    original = module.tempfile.TemporaryDirectory
    monkeypatch.setattr(module.tempfile, 'TemporaryDirectory',
                        lambda **kwargs: original(dir=tmp_path, **kwargs))
    with pytest.raises(RuntimeError, match='暂存目录位于可写owner内'):
        store.commit('tenant-a', 'user', 'owner-a', tmp_path)
    assert not client.values
    assert sorted(p.name for p in tmp_path.iterdir()) == ['source.txt']


def test_normal_full_upload_releases_parent_handles_before_network(tmp_path, monkeypatch):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'normal')
    client = _Objects()
    store = _store(client)
    store._replace_manifest = lambda *_args: None
    original_parent = module.SnapshotParent
    active = []

    class ObservedParent(original_parent):
        def __enter__(self):
            result = super().__enter__()
            active.append(self)
            return result

        def __exit__(self, exc_type, exc, tb):
            try:
                return super().__exit__(exc_type, exc, tb)
            finally:
                if self in active:
                    active.remove(self)

    monkeypatch.setattr(module, 'SnapshotParent', ObservedParent)
    upload = client.upload_fileobj

    def observe_upload(path, bucket, key, *, ExtraArgs):
        assert not active
        assert path.read() == b'normal'
        path.seek(0)
        upload(path, bucket, key, ExtraArgs=ExtraArgs)

    client.upload_fileobj = observe_upload
    assert store.commit('tenant-a', 'user', 'owner-a', tmp_path) == 1
    assert not active


def test_stage_expected_owner_boundary_rejects_foreign_parent_link(tmp_path):
    from agent_py_agent.agent.owner_snapshot_index import stage_and_hash
    owner, foreign = tmp_path / 'owner', tmp_path / 'foreign'
    owner.mkdir()
    foreign.mkdir()
    (foreign / 'source.txt').write_bytes(b'foreign fixture')
    (owner / 'linked').symlink_to(foreign, target_is_directory=True)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        stage_and_hash(owner / 'linked/source.txt', tmp_path / 'staging', owner_root=owner)
    assert not (tmp_path / 'staging').exists()


def test_stage_rejects_inplace_write_even_when_mtime_is_restored(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent import owner_snapshot_index
    source = tmp_path / 'source.txt'
    source.write_bytes(b'x' * 131072)
    original = source.stat()
    read = owner_snapshot_index.os.read
    changed = []

    def mutate_read(descriptor, size):
        chunk = read(descriptor, size)
        if not changed:
            changed.append(True)
            source.write_bytes(b'y' * original.st_size)
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        return chunk

    monkeypatch.setattr(owner_snapshot_index.os, 'read', mutate_read)
    with pytest.raises(RuntimeError, match='OWNER_SNAPSHOT_SOURCE_CHANGED'):
        owner_snapshot_index.stage_and_hash(source, tmp_path / 'staging', owner_root=tmp_path,
                                           expected_stat=original)
    assert list((tmp_path / 'staging').iterdir()) == []


def test_owned_stage_rename_and_replacement_do_not_redirect_upload_or_cleanup(tmp_path):
    from agent_py_agent.agent.owner_snapshot_index import stage_and_hash
    owner, staging = tmp_path / 'owner', tmp_path / 'staging'
    owner.mkdir()
    source = owner / 'source.txt'
    source.write_bytes(b'original')
    staged, digest, _size = stage_and_hash(source, staging, owner_root=owner)
    name = staged.name
    original_parent = tmp_path / 'old-stage-parent'
    staging.rename(original_parent)
    staging.mkdir()
    replacement = staging / name
    replacement.write_bytes(b'foreign replacement')
    # 改名后的原目录里也放同名普通替换物；两者均不能成为上传来源或关闭时的删除目标。
    original_collision = original_parent / name
    if original_collision.exists():
        original_collision.rename(original_parent / 'original-stage-name')
    original_collision.write_bytes(b'other replacement')
    client = _Objects()
    store = _store(client)
    try:
        store._upload(staged, store._object_key('tenant-a', digest))
    finally:
        staged.unlink(missing_ok=True)
    assert list(client.values.values()) == [b'original']
    assert replacement.read_bytes() == b'foreign replacement'
    assert original_collision.read_bytes() == b'other replacement'


def test_stage_readers_have_independent_seek_positions_and_release(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.owner_snapshot_index import stage_and_hash
    source = tmp_path / 'source.txt'
    source.write_bytes(b'0123456789')
    staged, _digest, _size = stage_and_hash(source, tmp_path / 'staging', owner_root=tmp_path)
    try:
        def read_at(position):
            with staged.open_reader() as reader:
                reader.seek(position)
                assert reader.tell() == position
                result = reader.read(2)
                reader.seek(-1, 2)
                assert reader.read() == b'9'
                return result
        with ThreadPoolExecutor(2) as pool:
            assert list(pool.map(read_at, [1, 5])) == [b'12', b'56']
    finally:
        staged.unlink(missing_ok=True)
    with pytest.raises(ValueError, match='lease is closed'):
        staged.open_reader()


@pytest.mark.parametrize('multipart', [False, True])
def test_actual_boto3_managed_transfer_reads_owned_fd_and_multipart(tmp_path, multipart):
    boto3 = pytest.importorskip('boto3')
    from botocore.config import Config
    from botocore.stub import Stubber

    from agent_py_agent.agent.owner_snapshot_index import stage_and_hash
    payload = b'normal bytes' if not multipart else b'abcd1234' * (1152 * 1024)
    source = tmp_path / 'source.bin'
    source.write_bytes(payload)
    staged, digest, _size = stage_and_hash(source, tmp_path / 'staging', owner_root=tmp_path)
    # 显式假凭据+Stubber消费全部API；不访问真实用户、S3或元数据凭据服务。
    client = boto3.client('s3', region_name='us-east-1', endpoint_url='https://s3.example.invalid',
        aws_access_key_id='qa-fixture', aws_secret_access_key='qa-fixture-not-secret',
        config=Config(request_checksum_calculation='when_required', response_checksum_validation='when_required'))
    store = _store(client)
    key = store._object_key('tenant-a', digest)
    parts = {}

    def observe(params, model, context, **kwargs):
        body = params['Body']
        position = body.tell()
        parts[params.get('PartNumber', 1)] = body.read()
        body.seek(position)

    client.meta.events.register('before-parameter-build.s3.PutObject', observe)
    client.meta.events.register('before-parameter-build.s3.UploadPart', observe)
    try:
        with Stubber(client) as stubber:
            stubber.add_client_error('head_object', service_error_code='404', http_status_code=404)
            if multipart:
                stubber.add_response('create_multipart_upload', {'UploadId': 'qa-upload'})
                stubber.add_response('upload_part', {'ETag': 'qa-etag'})
                stubber.add_response('upload_part', {'ETag': 'qa-etag'})
                stubber.add_response('complete_multipart_upload', {'ETag': 'qa-complete'})
            else:
                stubber.add_response('put_object', {'ETag': 'qa-etag'})
            store._upload(staged, key)
            stubber.assert_no_pending_responses()
        assert b''.join(parts[i] for i in sorted(parts)) == payload
        assert len(parts) == (2 if multipart else 1)
        assert staged.stat().st_size == len(payload)
    finally:
        staged.unlink(missing_ok=True)
        client.close()


def test_stage_equal_signatures_do_not_override_actual_source_bytes(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent import owner_snapshot_index as index
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    info = source.stat()
    signature = index.file_signature
    with source.open('rb') as handle:
        original = signature(handle.fileno())
    identity = (info.st_dev, info.st_ino)

    def coarse_signature(descriptor):
        current = os.fstat(descriptor)
        return original if (current.st_dev, current.st_ino) == identity else signature(descriptor)

    read, changed = index.os.read, []

    def change_after_read(descriptor, size):
        content = read(descriptor, size)
        if not changed:
            changed.append(True)
            source.write_bytes(b'tamper!')
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        return content

    # Linux tmpfs原物理反例已证明完全相等的tuple；此处固定该分辨率，跨平台确定性回归真实字节变化。
    monkeypatch.setattr(index, 'file_signature', coarse_signature)
    monkeypatch.setattr(index.os, 'read', change_after_read)
    with pytest.raises(RuntimeError, match='OWNER_SNAPSHOT_SOURCE_CHANGED'):
        index.stage_and_hash(source, tmp_path / 'staging', owner_root=tmp_path)


def test_full_final_equal_token_requires_actual_digest_before_manifest(tmp_path, monkeypatch):
    import os
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    info = source.stat()
    original_signature = module.file_signature
    with source.open('rb') as handle:
        before = original_signature(handle.fileno())
    store = _store(_Objects())
    committed = []
    store._replace_manifest = lambda *_args: committed.append(True)
    upload = store._upload

    def mutate_after_upload(stage, key):
        upload(stage, key)
        source.write_bytes(b'tamper!')
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))

    def coarse_signature(descriptor):
        current = os.fstat(descriptor)
        return before if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino) else original_signature(descriptor)

    monkeypatch.setattr(module, 'file_signature', coarse_signature)
    monkeypatch.setattr(store, '_upload', mutate_after_upload)
    with pytest.raises(RuntimeError, match='仍被写入'):
        store.commit('tenant-a', 'user', 'owner-a', tmp_path)
    assert not committed


@pytest.mark.parametrize('failure_point', ['verify', 'open_read', 'signature'])
def test_full_stage_exception_retains_traceback_but_closes_lease(tmp_path, monkeypatch, failure_point):
    import os

    source = tmp_path / 'source'
    source.write_bytes(b'content')
    original_stage = module.stage_open_file
    retained = []
    failure = RuntimeError('injected post-stage validation failure')

    def fail(*_args, **_kwargs):
        raise failure

    def staged_then_fault(*args, **kwargs):
        result = original_stage(*args, **kwargs)
        stage = result[0]
        foreign = stage.path if os.name != 'nt' else stage.path.with_suffix('.foreign')
        retained.append((stage, stage._descriptor, foreign))
        # 同名后来文件不属于本次stage租约；异常清理不能误删它。
        foreign.write_bytes(b'foreign replacement')
        if failure_point == 'signature':
            monkeypatch.setattr(module, 'file_signature', fail)
        else:
            monkeypatch.setattr(module.SnapshotParent, failure_point, fail)
        return result

    monkeypatch.setattr(module, 'stage_open_file', staged_then_fault)
    with pytest.raises(RuntimeError) as captured:
        module._stage_full_owner_file(tmp_path, ('source',), tmp_path / 'staging',
                                      expected_stat=source.stat())
    assert captured.value is failure
    assert captured.value.__traceback__ is not None
    stage, descriptor, foreign = retained[0]
    assert stage._descriptor == -1
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert foreign.read_bytes() == b'foreign replacement'
    assert source.read_bytes() == b'content'
