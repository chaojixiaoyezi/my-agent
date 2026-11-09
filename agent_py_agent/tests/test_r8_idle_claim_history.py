"""历史任务窗口化以后，孤立旧 claim 仍必须阻止闲置压缩。"""
import threading

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic_unlocked as write
from agent_py_agent.agent.conversation import claim_activity_projection as view
from agent_py_agent.agent.conversation.store import ConversationStore


def test_history_is_scanned_in_slices_and_orphan_active_claim_is_preserved(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversation')
    thread = store.threads.create()
    directory = store.storage.background_claims_dir
    for number in range(500):
        write(directory / f'{number}.json', {'status': 'finished', 'thread_id': thread.thread_id})
    read = view._read_claim_report
    reads = []
    monkeypatch.setattr(view, '_read_claim_report', lambda path: (reads.append(path), read(path))[1])
    for call in range(100):
        reads.clear()
        state = view.claim_activity_state(store.storage, thread.thread_id)
        assert len(reads) <= 128
        if not state:
            break
        assert state == 'activity_unknown'
    else:
        raise AssertionError('静止历史必须最终核对完成')
    assert call >= 3
    reads.clear()
    assert view.claim_activity_state(store.storage, thread.thread_id) == ''
    assert reads == []
    # 没有 TaskLink 的物理租约也属于未退出执行体，不能被最近任务窗口隐藏。
    write(directory / '499.json', {'status': 'running', 'thread_id': thread.thread_id})
    for _ in range(100):
        state = view.claim_activity_state(store.storage, thread.thread_id)
        assert state
        if state == 'foreground_active':
            break
    else:
        raise AssertionError('遗漏孤立旧 claim')


def test_atomic_claim_change_during_scan_never_proves_idle(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversation')
    thread = store.threads.create()
    path = store.storage.background_claims_dir / 'old.json'
    write(path, {'status': 'finished', 'thread_id': thread.thread_id})
    read_started, rewritten = threading.Event(), threading.Event()
    original = view._read_claim_report

    def read(candidate):
        snapshot = original(candidate)
        read_started.set()
        assert rewritten.wait(2)
        return snapshot

    def change():
        assert read_started.wait(2)
        write(path, {'status': 'running', 'thread_id': thread.thread_id})
        rewritten.set()

    monkeypatch.setattr(view, '_read_claim_report', read)
    writer = threading.Thread(target=change)
    writer.start()
    try:
        assert view.claim_activity_state(store.storage, thread.thread_id) == 'activity_unknown'
    finally:
        writer.join(2)
    assert not writer.is_alive()
    monkeypatch.setattr(view, '_read_claim_report', original)
    assert view.claim_activity_state(store.storage, thread.thread_id) == 'foreground_active'
