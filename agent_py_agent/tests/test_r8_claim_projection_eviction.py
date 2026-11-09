"""多 store 交错不能因目录缓存淘汰永久失去续点。"""
import pytest

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic_unlocked as write
from agent_py_agent.agent.conversation import claim_activity_projection as view
from agent_py_agent.agent.conversation.store import ConversationStore


@pytest.mark.parametrize("portable,count", [(False, 6), (True, 12)])
def test_six_large_stores_round_robin_converge(tmp_path, monkeypatch, portable, count):
    monkeypatch.setattr(view, "_NATIVE", not portable)
    stores = []
    for number in range(count):
        store = ConversationStore(tmp_path / str(number))
        thread = store.threads.create()
        for item in range(300):
            write(store.storage.background_claims_dir / f'{item}.json',
                  {'status': 'finished', 'thread_id': thread.thread_id})
        stores.append((store, thread.thread_id))
    reads = []
    original = view._read_claim_report
    monkeypatch.setattr(view, '_read_claim_report', lambda path: (reads.append(path), original(path))[1])
    complete = set()
    for _ in range(30):
        for number, (store, thread_id) in enumerate(stores):
            reads.clear()
            state = view.claim_activity_state(store.storage, thread_id)
            assert len(reads) <= 128
            if state == '':
                complete.add(number)
        if len(complete) == len(stores):
            break
    assert len(complete) == len(stores), '交错轮询必须保留每个store的扫描续点'


def test_completed_cache_never_hides_bad_claim_or_deleted_directory(tmp_path):
    store = ConversationStore(tmp_path / 'store')
    directory = store.storage.background_claims_dir
    path = directory / 'bad.json'
    path.write_text('{broken', encoding='utf-8')
    assert view.claim_activity_state(store.storage, 'thread') == 'activity_unknown'
    path.unlink()
    assert view.claim_activity_state(store.storage, 'thread') == ''
    directory.rmdir()
    assert view.claim_activity_state(store.storage, 'thread') == ''
    directory.mkdir()
    write(directory / 'new.json', {'status': 'running', 'thread_id': 'thread'})
    assert view.claim_activity_state(store.storage, 'thread') == 'foreground_active'


def test_cache_sqlite_memory_and_scan_descriptors_are_fixed():
    with view._LOCK:
        conn = view._connection()
        assert conn.execute('PRAGMA cache_size').fetchone()[0] == -256
        assert conn.execute('PRAGMA mmap_size').fetchone()[0] == 0
        assert len(view._PORTABLE) <= 8
