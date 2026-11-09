"""隔离 Linux 中用真实 Watchman 与真实 checkout 连续往返；不修改任何实际 owner。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.owner_change_journal import OwnerChangeJournal
from agent_py_agent.agent.owner_snapshot_cache import OwnerSnapshotIndex
from agent_py_agent.tests.test_s1w2_owner_snapshot_delta import _checkout, _delta_store, _write_old


def verify(root):
    store, warm, home = _delta_store(root)
    store._changes = OwnerChangeJournal()
    try:
        with _checkout(store, warm, home):
            _write_old(home / 'file', b'first')
        assert (warm / '.watchmanconfig').is_file()
        assert not (warm / 'home/.watchmanconfig').exists()

        def forbidden(*args, **kwargs):
            raise AssertionError('second real-observer checkout attempted complete history scan')

        store._read_manifest = forbidden
        store._warm_paths = forbidden
        with _checkout(store, warm, home):
            assert (home / 'file').read_bytes() == b'first'
            _write_old(home / 'file', b'second')
        with OwnerSnapshotIndex.open(warm / 'snapshot-index.sqlite3') as index:
            assert index.metadata()['mode'] == 'incremental', index.metadata()
        assert (warm / '.watchmanconfig').is_file()
        assert store._head.rows['file'].size_bytes == 6
        with _checkout(store, warm, home):
            pass
        assert store._head.commits[-1] == ([], [])
    finally:
        store.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='owner-roundtrip-fixture-') as directory:
        verify(Path(directory))
    print('owner_snapshot_real_observer_roundtrip_passed')
