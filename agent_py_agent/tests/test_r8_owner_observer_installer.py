"""固定资产构建器的完整性与解包边界，使用临时 zip，不下载替身二进制。"""
from __future__ import annotations

import hashlib
import stat
import zipfile

import pytest

from deploy.install_watchman import install


def _archive(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pinned_archive_installs_binary_and_absolute_dependency_library(tmp_path):
    archive = tmp_path / 'archive.zip'
    digest = _archive(archive, [('watchman-release/bin/watchman', b'executable'),
                               ('watchman-release/lib/libexample.so.1', b'library')])
    install(archive, tmp_path / 'prefix', tmp_path / 'system-libs', digest)
    assert (tmp_path / 'prefix/bin/watchman').read_bytes() == b'executable'
    assert (tmp_path / 'prefix/bin/watchman').stat().st_mode & 0o111
    assert (tmp_path / 'system-libs/libexample.so.1').read_bytes() == b'library'


def test_archive_digest_mismatch_never_installs_bytes(tmp_path):
    archive = tmp_path / 'archive.zip'
    _archive(archive, [('release/bin/watchman', b'wrong')])
    with pytest.raises(RuntimeError, match='SHA256'):
        install(archive, tmp_path / 'prefix', tmp_path / 'libs', '0' * 64)
    assert not (tmp_path / 'prefix').exists()


@pytest.mark.parametrize('kind', ['parent', 'absolute', 'symlink', 'backslash'])
def test_unsafe_archive_layout_is_rejected(tmp_path, kind):
    name = {'parent': '../escape', 'absolute': '/escape', 'symlink': 'release/bin/watchman',
            'backslash': 'release/bin/..\\escape'}[kind]
    item = zipfile.ZipInfo(name)
    if kind == 'symlink':
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = tmp_path / 'archive.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr(item, b'not executable')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match='危险'):
        install(archive, tmp_path / 'prefix', tmp_path / 'libs', digest)
    assert not (tmp_path / 'escape').exists()
