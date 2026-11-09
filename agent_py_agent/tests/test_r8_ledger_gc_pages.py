"""旧账维护按物理预算推进；续点由正式owner lane持久化，坏账不能当已完成。"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.io import locked_file_transition
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


@pytest.mark.parametrize("size", [17, 1000])
def test_gc_bound_and_serialized_cursor_survive_cold_store(tmp_path, monkeypatch, size):
    store = ConversationStore(tmp_path / "conversations")
    for number in range(size):
        identifier = f"thread-{number:04}"
        thread = ConversationThread(thread_id=identifier, canonical_user_id="test", task_ids=(f"ghost-{number}",))
        store.storage.thread_path(identifier).write_text(
            json.dumps(thread.to_dict()), encoding="utf-8"
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("maintenance traversed full history")

    monkeypatch.setattr(store.threads, "list_report", forbidden)
    monkeypatch.setattr(store.tasks, "iter_task_ids", forbidden)
    from agent_py_agent.agent.user_space import owner_quota

    original_scan = owner_quota._quota_scandir
    physical = [0]
    class CountedScan:
        def __init__(self, descriptor):
            self.actual = original_scan(descriptor)
        def __getattr__(self, name):
            return getattr(self.actual, name)
        @property
        def buffer(self):
            return self.actual.buffer
        @buffer.setter
        def buffer(self, value):
            self.actual.buffer = value
        def __enter__(self):
            self.actual.__enter__()
            return self
        def __exit__(self, *args):
            return self.actual.__exit__(*args)
        def __next__(self):
            physical[0] += 1
            return next(self.actual)
    monkeypatch.setattr(owner_quota, '_quota_scandir', CountedScan)
    cursor, pruned, seen = {"phase": "threads"}, 0, []
    for _ in range(8):
        physical[0] = 0
        report = store.gc_stale_ledger_records(cursor=cursor, max_items=17)
        assert physical[0] <= 17
        assert report["items"] <= 17
        pruned += report["pruned_task_ids"]
        seen.append(pruned)
        cursor = json.loads(json.dumps(report["cursor"]))
        # 新Store不共享线程/任务缓存；原生cookie无驻留FD或迭代器依赖。
        store = ConversationStore(store.storage.root, initialize=False)
        monkeypatch.setattr(store.threads, "list_report", forbidden)
        monkeypatch.setattr(store.tasks, "iter_task_ids", forbidden)
    assert seen[-1] > seen[3] >= 0
    assert pruned < size and cursor


def test_gc_claim_snapshot_cannot_archive_new_running_lease(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import ledger_gc
    from agent_py_agent.tests.test_rework_r9_claim_archive_atomicity import (
        _acquire,
        _case,
        _finished_body,
    )

    store, thread_id, path, archive = _case(tmp_path)
    _finished_body(store, thread_id, "old", started=100)
    lock = Path(f"{path}.lock")
    before = lock.stat().st_ino
    ready, resume = threading.Event(), threading.Event()
    original = ledger_gc._read_claim_report
    blocked = False

    def read(candidate):
        nonlocal blocked
        report = original(candidate)
        if candidate == path and threading.current_thread().name == "gc-page" and not blocked:
            blocked = True
            ready.set()
            assert resume.wait(10)
        return report

    monkeypatch.setattr(ledger_gc, "_read_claim_report", read)
    reports = []
    worker = threading.Thread(
        name="gc-page",
        target=lambda: reports.append(
            store.gc_stale_ledger_records(now=1_800_000_000, cursor={"phase": "claims"})
        ),
    )
    worker.start()
    try:
        assert ready.wait(5)
        successor = _acquire(store, thread_id, "new", now=1_800_000_000)
    finally:
        resume.set()
        worker.join(10)
    assert not worker.is_alive()
    assert reports[0]["archived_claims"] == 0 and not (archive / path.name).exists()
    assert json.loads(path.read_text())["claim_id"] == successor["claim_id"]
    assert lock.stat().st_ino == before


def test_gc_busy_lock_is_unknown_and_preserves_stable_sidecar(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create()
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "bad"})
    path = store.storage.task_path("bad")
    path.write_text("{broken")
    held, release = threading.Event(), threading.Event()

    def hold():
        with locked_file_transition(path):
            held.set()
            assert release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert held.wait(5)
        inode = Path(f"{path}.lock").stat().st_ino
        report = store.gc_stale_ledger_records()
        assert not report["complete"] and report["errors"]
        assert path.exists() and Path(f"{path}.lock").stat().st_ino == inode
    finally:
        release.set()
        worker.join(10)
    assert store.gc_stale_ledger_records()["quarantined_task_links"] == 1
    assert Path(f"{path}.lock").stat().st_ino == inode
    assert not (path.parent / ".ledger_archive" / f"{path.name}.lock").exists()


def test_formal_lane_serialized_progress_and_empty_owner_no_writes(tmp_path):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    writes = []
    request = StorageLaneRequest(
        "conversation_ledgers",
        home,
        1_800_000_000,
        {},
        StorageLaneBudget(17, 8 * 1024 * 1024, None),
        False,
        False,
        None,
        lambda operation: (writes.append(True), operation())[1],
    )
    cursor = {}
    for _ in range(30):
        receipt = maintain_conversation_ledgers_lane(replace(request, cursor=cursor))
        validate_lane_receipt(receipt)
        assert receipt["items"] <= 17
        cursor = json.loads(json.dumps(receipt["cursor"]))
        if receipt["complete"]:
            break
    assert receipt["complete"] and not writes


def test_scheduler_uses_formal_owner_entry_and_gateway_skips_it(monkeypatch):
    from agent_py_agent.agent.conversation.runtime import _BackgroundSchedulerTickMixin
    from agent_py_agent.agent.user_space import owner_maintenance

    agent = object()
    seen = []
    monkeypatch.setattr(
        owner_maintenance,
        "maybe_run_cli_owner_maintenance",
        lambda actual, *, now: seen.append((actual, now)),
    )
    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=agent))
    _BackgroundSchedulerTickMixin._maybe_gc_ledger(scheduler, now=12)
    assert seen == [(agent, 12)]


def test_portable_directory_requires_explicit_opt_in_and_resumes(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
    from agent_py_agent.agent.user_space import directory_paging

    root = tmp_path / 'root'
    root.mkdir()
    for number in range(17):
        (root / f'{number}.json').write_text('{}')
    monkeypatch.setattr(directory_paging, '_native_directory_supported', lambda: False)
    def strict(*args, **kwargs):
        raise NoFollowPathError('strict backend unavailable')
    monkeypatch.setattr(directory_paging, 'open_directory_beneath', strict)
    with pytest.raises(NoFollowPathError):
        directory_paging.directory_page(root, (), {}, 4)
    cursor, names = {}, []
    for _ in range(10):
        page, cursor, complete = directory_paging.directory_page(root, (), cursor, 4, require_dir_fd=False)
        assert len(page) <= 4
        names.extend(page)
        cursor = json.loads(json.dumps(cursor))
        if complete:
            break
    assert complete and set(names) == {f'{number}.json' for number in range(17)}
    assert len(names) == len(set(names))


def test_portable_directory_rejects_replacement_and_symlink(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
    from agent_py_agent.agent.user_space import directory_paging

    root = tmp_path / 'root'
    root.mkdir()
    (root / 'entry').write_text('x')
    monkeypatch.setattr(directory_paging, '_native_directory_supported', lambda: False)
    original = directory_paging._portable_page
    def swapped(*args):
        result = original(*args)
        root.rename(tmp_path / 'retired')
        root.mkdir()
        return result
    monkeypatch.setattr(directory_paging, '_portable_page', swapped)
    with pytest.raises(NoFollowPathError):
        directory_paging.directory_page(root, (), {}, 1, require_dir_fd=False)


def test_gc_checkpoint_continues_in_fresh_process_without_reinitializing_history(tmp_path):
    import subprocess
    import sys

    from agent_py_agent.agent.conversation import task_history

    store = ConversationStore(tmp_path / 'conversations')
    thread = ConversationThread(thread_id='long-thread', canonical_user_id='test',
                                task_ids=tuple(f'ghost-{number}' for number in range(200)))
    store.storage.thread_path(thread.thread_id).write_text(json.dumps(thread.to_dict()))
    first = store.gc_stale_ledger_records(cursor={'phase': 'threads'}, max_items=17)
    checkpoint = tmp_path / 'checkpoint.json'
    checkpoint.write_text(json.dumps(first['cursor']))
    program = '''
import json,sys
from pathlib import Path
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation import task_history

original_initialize=task_history.initialize
def existing(path, thread):
    root=task_history.history_root(path)
    before=task_history._read(root, ('index.json',))
    assert before is not None, 'completed first migration was lost'
    original_initialize(path, thread)
    assert task_history._read(root, ('index.json',)) == before, 'initialization rewrote completed head'
task_history.initialize=existing
store=ConversationStore(Path(sys.argv[1]), initialize=False)
print(json.dumps(store.gc_stale_ledger_records(cursor=json.loads(Path(sys.argv[2]).read_text()),max_items=17)))
'''
    completed = subprocess.run([sys.executable, '-c', program, str(store.storage.root), str(checkpoint)],
                               cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    second = json.loads(completed.stdout)
    assert 0 < first['pruned_task_ids'] <= 17 and 0 < second['pruned_task_ids'] <= 17
    assert second['items'] <= 17 and not second['complete']
    total = first['pruned_task_ids'] + second['pruned_task_ids']
    latest = store.threads.require(thread.thread_id)
    assert latest.task_history_version == 1 and len(latest.task_ids) <= 128
    for number in range(total):
        assert not task_history.contains(store.storage.thread_path(thread.thread_id), latest, f'ghost-{number}')
    assert task_history.contains(store.storage.thread_path(thread.thread_id), latest, f'ghost-{total}')


def test_gc_byte_budget_rejects_body_before_parse_and_later_archives(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import ledger_gc

    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.create()
    policy = store.progress.create({'thread_id': thread.thread_id, 'task_id': 'old',
        'interval_seconds': 120, 'metadata': {'body': 'x' * 4096}})
    store.progress.disable(policy.policy_id, now=1_700_000_000)
    original = ledger_gc._read_progress_policy_report
    reads = []
    def read(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(ledger_gc, '_read_progress_policy_report', read)
    report = store.gc_stale_ledger_records(now=1_800_000_000, max_bytes=1)
    assert not reads and not report['complete'] and report['errors']
    assert store.storage.policy_path(policy.policy_id).exists()
    report = store.gc_stale_ledger_records(now=1_800_000_000, max_bytes=65536)
    assert reads and report['archived_policies'] == 1 and report['bytes'] <= 65536


def test_gc_oversize_history_projection_is_unknown_before_json_parse(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import task_history

    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.create()
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'old'})
    store.tasks.update_status({'task_id': 'old', 'status': 'completed'})
    page = task_history.history_root(store.storage.thread_path(thread.thread_id)) / 'pages' / '0.json'
    raw = b'[' + b' ' * 65536 + b']'
    page.write_bytes(raw)
    original = task_history._read
    def read(root, parts):
        assert parts != ('pages', '0.json'), 'oversize projection body was parsed'
        return original(root, parts)
    monkeypatch.setattr(task_history, '_read', read)
    report = store.gc_stale_ledger_records(cursor={'phase': 'threads'}, max_bytes=8 * 1024 * 1024)
    assert not report['complete'] and report['errors'] and page.read_bytes() == raw
