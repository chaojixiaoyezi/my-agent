"""原快照下载/清理路径真实文件竞态；只替代远端对象字节，不替代宿主FS安全门。"""
from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.owner_object_store import OwnerFile, OwnerSnapshotStore
from agent_py_agent.agent.owner_snapshot_files import (
    SnapshotParent,
    lexical_path,
    validate_relative,
)
from agent_py_agent.agent.owner_snapshot_index import OwnerStatIndex
from agent_py_agent.tests.test_s1w2_owner_snapshot_delta import _checkout, _delta_store, _seed


def _row(path='workspace/report.txt', body=b'OWNER-REMOTE'):
    return OwnerFile(path, 'fixture/' + hashlib.sha256(body).hexdigest(), hashlib.sha256(body).hexdigest(), len(body), 0o600)


def _store(body, callback=None):
    targets = []
    class Objects:
        def download_file(self, bucket, key, target):
            targets.append(Path(target))
            if callback is not None:
                callback()
            Path(target).write_bytes(body)
    store = object.__new__(OwnerSnapshotStore)
    store._client, store._bucket = Objects(), 'synthetic'
    return store, targets


def _link(link, target):
    # Windows原生parent句柄可能先阻止rename；若能走到这里，使用平台正常目录链接能力。
    link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize('replacement', ['symlink', 'directory'])
@pytest.mark.parametrize('entry', ['one', 'restore'])
def test_original_download_api_parent_swap_cannot_overwrite_other_owner(tmp_path, replacement, entry):
    owner = tmp_path / 'alice'
    directory = owner / 'workspace'
    directory.mkdir(parents=True)
    (directory / 'report.txt').write_bytes(b'ALICE-ORIGINAL')
    foreign = tmp_path / 'bob' / 'workspace'
    foreign.mkdir(parents=True)
    victim = foreign / 'report.txt'
    victim.write_bytes(b'BOB-ORIGINAL')
    old = owner / 'old-workspace'
    def swap():
        directory.rename(old)
        if replacement == 'symlink':
            _link(directory, foreign)
        else:
            directory.mkdir()
            (directory / 'report.txt').write_bytes(b'NEW-LEGITIMATE')
    store, targets = _store(b'OWNER-REMOTE', swap)
    store._read_manifest = lambda *_: [_row()]
    with pytest.raises(OSError):
        if entry == 'one':
            store._download_row(owner, _row())
        else:
            store.restore('tenant', 'user', 'alice', owner)
    assert victim.read_bytes() == b'BOB-ORIGINAL'
    assert (old / 'report.txt').read_bytes() == b'ALICE-ORIGINAL'
    if replacement == 'directory':
        assert (directory / 'report.txt').read_bytes() == b'NEW-LEGITIMATE'
    assert targets and all(not path.is_relative_to(owner) for path in targets)
    assert all(not path.parent.exists() for path in targets)


def test_download_same_directory_new_leaf_is_not_silently_overwritten(tmp_path):
    owner = tmp_path / 'alice'
    owner.mkdir()
    target = owner / 'new.txt'
    store, _ = _store(b'OWNER-REMOTE', lambda: target.write_bytes(b'NEW-LEGITIMATE'))
    with pytest.raises(NoFollowPathError, match='destination changed'):
        store._download_row(owner, _row('new.txt'))
    assert target.read_bytes() == b'NEW-LEGITIMATE'


def test_bad_remote_hash_preserves_legitimate_restore_tree_and_cleans_private_download(tmp_path):
    owner = tmp_path / 'alice'
    owner.mkdir()
    legitimate = owner / 'legitimate.txt'
    legitimate.write_bytes(b'KEEP-ORIGINAL')
    store, targets = _store(b'WRONG-REMOTE')
    store._read_manifest = lambda *_: [_row()]
    with pytest.raises(RuntimeError, match='object 校验失败'):
        store.restore('tenant', 'user', 'alice', owner)
    assert legitimate.read_bytes() == b'KEEP-ORIGINAL'
    assert list(owner.iterdir()) == [legitimate]
    assert all(not path.parent.exists() for path in targets)


def test_normal_download_is_atomic_for_existing_readers_and_idempotent(tmp_path):
    owner = tmp_path / 'alice'
    directory = owner / 'workspace'
    directory.mkdir(parents=True)
    target = directory / 'report.txt'
    target.write_bytes(b'OLD-READER')
    store, targets = _store(b'OWNER-REMOTE')
    with target.open('rb') as reader:
        if os.name == 'nt':
            # CRT旧reader若未共享DELETE，平台可拒替换；不得为兼容而截断其原inode。
            try:
                store._download_row(owner, _row())
            except PermissionError:
                assert target.read_bytes() == b'OLD-READER'
        else:
            store._download_row(owner, _row())
        assert reader.read() == b'OLD-READER'
    store._download_row(owner, _row())
    store._download_row(owner, _row())
    assert target.read_bytes() == b'OWNER-REMOTE'
    if os.name != 'nt':
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(directory.glob('*.restore'))
    assert all(not path.parent.exists() for path in targets)


@pytest.mark.parametrize('method', ['warm', 'observed', 'prune'])
def test_cleanup_parent_swap_never_deletes_foreign_or_replacement_files(tmp_path, monkeypatch, method):
    home = tmp_path / 'home'
    owner = home / 'owners' / 'alice'
    directory = owner / 'workspace'
    directory.mkdir(parents=True)
    (directory / 'unwanted.txt').write_bytes(b'KEEP-OLD-ALICE')
    foreign = tmp_path / 'bob'
    foreign.mkdir()
    (foreign / 'unwanted.txt').write_bytes(b'KEEP-BOB')
    store, _ = _store(b'OWNER-REMOTE')
    original = SnapshotParent.remove
    raced = []
    if method == 'prune':
        selected_parent, name = home / 'owners', 'stale'
        (selected_parent / name).write_bytes(b'KEEP-OLD-ALICE')
        (foreign / name).write_bytes(b'KEEP-BOB')
    else:
        selected_parent, name = directory, 'unwanted.txt'
    old = selected_parent.with_name(selected_parent.name + '-old')
    def remove(parent, leaf, *args, **kwargs):
        if parent.path == selected_parent and leaf == name and not raced:
            selected_parent.rename(old)
            _link(selected_parent, foreign)
            raced.append(True)
        return original(parent, leaf, *args, **kwargs)
    monkeypatch.setattr(SnapshotParent, 'remove', remove)
    with pytest.raises(OSError):
        if method == 'warm':
            store._reconcile_warm(owner, [], OwnerStatIndex())
        elif method == 'observed':
            index = SimpleNamespace(manifest=lambda _: {}, forget=lambda _: None)
            store._reconcile_observed(home, owner, {'workspace/unwanted.txt'}, False, index)
        else:
            store._prune_warm_home(home, owner)
    assert raced
    assert (foreign / name).read_bytes() == b'KEEP-BOB'
    assert (old / name).read_bytes() == b'KEEP-OLD-ALICE'


@pytest.mark.parametrize('link_kind', ['warm', 'home', 'owner'])
def test_checkout_lexical_roots_cannot_be_prebound_to_another_owner(tmp_path, link_kind):
    store, warm, owner = _delta_store(tmp_path)
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    marker = foreign / 'KEEP.txt'
    marker.write_bytes(b'FOREIGN-MUST-NOT-CHANGE')
    if link_kind == 'warm':
        _link(warm, foreign)
    elif link_kind == 'home':
        warm.mkdir()
        _link(warm / 'home', foreign)
    else:
        owner.parent.mkdir(parents=True)
        _link(owner, foreign)
    _seed(store, 'remote.txt', b'REMOTE')
    with pytest.raises(OSError):
        with _checkout(store, warm, owner):
            pytest.fail('foreign lexical root must never enter execution')
    assert marker.read_bytes() == b'FOREIGN-MUST-NOT-CHANGE'
    assert sorted(path.name for path in foreign.iterdir()) == ['KEEP.txt']
    assert store._client.downloads == [] and store._head.commits == []


@pytest.mark.parametrize('value', ['C:/steal', 'C:steal', 'folder/:stream', 'folder/a:stream',
    'folder/a\\b', 'CON', 'aux.txt', 'COM\u00b9.log', 'LPT9', 'NUL.', 'name.', 'name ',
    'a/<b', 'a/?', 'a/\x01name', '\\\\server\\share\\file', '//server/share/file'])
def test_windows_manifest_names_reject_drive_ads_devices_and_aliases(value):
    with pytest.raises(NoFollowPathError):
        validate_relative(value, windows=True)


@pytest.mark.parametrize('value', ['C:relative', 'name:stream', 'a\\b', 'NUL', 'name.', 'name '])
def test_posix_ordinary_names_are_not_rejected_by_windows_rules(value):
    assert validate_relative(value, windows=False) == (value,)


def test_publish_and_remove_reject_host_leaf_traversal_without_touching_other_files(tmp_path):
    owner = tmp_path / 'owner'
    owner.mkdir()
    victim = tmp_path / 'victim'
    victim.write_bytes(b'KEEP')
    with SnapshotParent(owner) as parent:
        with pytest.raises(NoFollowPathError):
            parent.remove('../victim')
        with pytest.raises(NoFollowPathError):
            parent.publish('../victim', io.BytesIO(b'OWNER-REMOTE'), _row('../victim'))
    assert victim.read_bytes() == b'KEEP'


def test_host_system_tmp_alias_remains_usable_without_resolving_owner_links(tmp_path):
    import sys

    if sys.platform == 'darwin':
        with tempfile.TemporaryDirectory(prefix='owner-snapshot-alias-', dir='/private/tmp') as private:
            owner = Path('/tmp') / Path(private).name / 'alice'
            store, _ = _store(b'OWNER-REMOTE')
            store._download_row(owner, _row('report.txt'))
            assert (Path(private) / 'alice' / 'report.txt').read_bytes() == b'OWNER-REMOTE'
            assert lexical_path(owner) == Path(private) / 'alice'
    else:
        owner = tmp_path / 'alice'
        store, _ = _store(b'OWNER-REMOTE')
        store._download_row(owner, _row('report.txt'))
        assert (owner / 'report.txt').read_bytes() == b'OWNER-REMOTE'


def test_checkout_failed_parent_swap_stays_dirty_and_safe_retry_is_idempotent(tmp_path):
    store, warm, owner = _delta_store(tmp_path)
    _seed(store, 'workspace/report.txt', b'SAFE')
    with _checkout(store, warm, owner):
        pass
    version = store._head.version
    directory = owner / 'workspace'
    target = directory / 'report.txt'
    target.write_bytes(b'EVIL')
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    victim = foreign / 'report.txt'
    victim.write_bytes(b'KEEP-FOREIGN')
    old = owner / 'old-workspace'
    original_download = store._client.download_file

    def swapped(bucket, key, path):
        directory.rename(old)
        _link(directory, foreign)
        original_download(bucket, key, path)

    store._client.download_file = swapped
    entered = False
    with pytest.raises(OSError):
        with _checkout(store, warm, owner):
            entered = True
    assert entered is False and store._head.version == version
    assert victim.read_bytes() == b'KEEP-FOREIGN'
    assert (old / 'report.txt').read_bytes() == b'EVIL'
    import json

    assert json.loads((warm / 'state.json').read_text()) == {'state': 'DIRTY'}
    # 仅测试方恢复本次故障注入的链接；不补业务产物、不改清单或CLEAN标志。
    directory.unlink()
    old.rename(directory)
    store._client.download_file = original_download
    with _checkout(store, warm, owner):
        assert target.read_bytes() == b'SAFE'
    with _checkout(store, warm, owner):
        assert target.read_bytes() == b'SAFE'
    assert victim.read_bytes() == b'KEEP-FOREIGN'
    assert json.loads((warm / 'state.json').read_text()) == {'state': 'CLEAN'}


def test_namespace_change_after_atomic_publish_is_not_success_or_foreign_cleanup(tmp_path, monkeypatch):
    owner = tmp_path / 'alice'
    directory = owner / 'workspace'
    directory.mkdir(parents=True)
    target = directory / 'report.txt'
    target.write_bytes(b'ORIGINAL')
    old_inode = target.stat().st_ino
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'report.txt').write_bytes(b'KEEP-FOREIGN')
    old = owner / 'old-workspace'
    verify = SnapshotParent.verify
    raced = []

    def after_publish(parent):
        verify(parent)
        if parent.path == directory and not raced:
            try:
                published = parent.info('report.txt').st_ino != old_inode
            except FileNotFoundError:
                published = False
            if published:
                raced.append(True)
                directory.rename(old)
                _link(directory, foreign)
                verify(parent)

    monkeypatch.setattr(SnapshotParent, 'verify', after_publish)
    store, _ = _store(b'OWNER-REMOTE')
    with pytest.raises(OSError):
        store._download_row(owner, _row())
    assert raced and (foreign / 'report.txt').read_bytes() == b'KEEP-FOREIGN'
    # 若平台允许移动已打开目录，发表只落在原目录；若原生句柄阻止移动，保留该平台原目录。
    original_directory = old if old.exists() else directory
    assert (original_directory / 'report.txt').read_bytes() == b'OWNER-REMOTE'


def test_deleted_warm_owner_is_fully_restored_with_original_missing_reason(tmp_path):
    import json
    import shutil

    from agent_py_agent.agent.owner_snapshot_cache import OwnerSnapshotIndex

    store, warm, owner = _delta_store(tmp_path)
    _seed(store, 'workspace/report.txt', b'REMOTE-AUTHORITY')
    with _checkout(store, warm, owner):
        pass
    shutil.rmtree(owner)
    with _checkout(store, warm, owner):
        assert (owner / 'workspace/report.txt').read_bytes() == b'REMOTE-AUTHORITY'
    with OwnerSnapshotIndex.open(warm / 'snapshot-index.sqlite3') as index:
        assert index.metadata()['reason'] == 'owner_home_missing'
        assert index.metadata()['mode'] == 'full_reconcile'
    assert json.loads((warm / 'state.json').read_text()) == {'state': 'CLEAN'}


def test_recent_ctime_and_future_timestamps_never_authorize_cached_digest():
    from agent_py_agent.agent import owner_object_store

    touched = []
    index = SimpleNamespace(lookup=lambda *args: touched.append(args) or 'cached')
    now = owner_object_store.time.time()
    for mtime, ctime in [(now - 100, now), (now + 100, now - 100), (now - 100, now + 100)]:
        assert owner_object_store._cached_file_digest(index, 'data', SimpleNamespace(
            st_mtime=mtime, st_ctime=ctime)) is None
    assert touched == []
