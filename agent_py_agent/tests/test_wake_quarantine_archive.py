"""唤醒毒丸第 4 步：结案留档的 14 天归档（只移不删）。

钉住：
1. 顶层结案记录按 quarantine.quarantined_at 判断，满 14 天才移到 quarantine/archive/，坏账留档 ledger/<id>.json 随记录一起移；
2. 读不出的顶层记录、unreadable/ 下读不出的信封、没有记录对应的孤儿坏账按 max(mtime, ctime) 判断；有记录的坏账不单独移；
3. replayed/ 不动（重放次数的唯一权威）；归档位置已有同名文件时不覆盖；文件名不是合法唤醒 ID 的杂项文件不碰；
4. 归档只换物理位置、不改发布语义：同键再发布仍返回原结案信号、回执是 failed_permanently、不抛数据损坏；
5. 重放对已归档的记录（含读不出的信封）拒绝，码是 WAKE_REPLAY_ARCHIVED；列表与计数不含已归档的；
6. 账本整理周期返回归档计数；会话删除清单包含 archive/ 顶层带 thread_id 的记录。
"""

from __future__ import annotations

import json
import os
import time

from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_wake_attempts import (
    WAKE_REPLAY_ARCHIVED,
    WAKE_REPLAY_NOT_FOUND,
    WakeAttemptStart,
)
from agent_py_agent.agent.conversation.store_wake_quarantine_archive import (
    WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS,
    archive_stale_wake_quarantine,
)
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_POISON_SAME_CAUSE_LIMIT_COUNT,
    WAKE_STATUS_FAILED_PERMANENTLY,
    WAKE_VERDICT_FAILURE,
    WakeAttemptVerdict,
    ledger_corrupt_decision,
)
from agent_py_agent.agent.memory_store.retention_scan import _thread_wake_files

BUG = WakeAttemptVerdict(WAKE_VERDICT_FAILURE, "error:SKILL_TASK_BINDING_INVALID")
DAY = 86400.0
QUARANTINED_AT = 1_000_000.0


def _store(tmp_path) -> ConversationStore:
    store = ConversationStore(tmp_path / "conv")
    for thread_id in ("thread-a", "thread-b"):
        store.threads.write(ConversationThread(thread_id=thread_id, canonical_user_id="u"))
    return store


def _raise(store, *, thread="thread-a", key=""):
    return store.wakes.raise_signal({"thread_id": thread, "reason": "session_task", "summary": "派活正文摘要",
                                     "metadata": {"session_task_id": "stask-1"}, "dedupe_key": key, "now": 10.0})


def _quarantine(store, signal, *, at=QUARANTINED_AT, decision=None):
    if decision is None:
        for attempt in range(WAKE_POISON_SAME_CAUSE_LIMIT_COUNT):
            store.wakes.attempts.begin(signal, WakeAttemptStart(f"claim-{attempt}"), now=100.0 + attempt)
            decision = store.wakes.attempts.record(signal, BUG, now=100.5 + attempt, error=RuntimeError("boom")).decision
    return store.wakes.attempts.quarantine(signal.wake_signal_id, decision, now=at)


def test_records_move_only_after_fourteen_days_by_quarantined_at(tmp_path):
    store = _store(tmp_path)
    old, young = _raise(store, key="old"), _raise(store, key="young")
    _quarantine(store, old, at=QUARANTINED_AT)
    _quarantine(store, young, at=QUARANTINED_AT + 2 * DAY)
    current = QUARANTINED_AT + WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS + 1.0

    assert archive_stale_wake_quarantine(store.storage, current=QUARANTINED_AT + WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS) == 0
    assert archive_stale_wake_quarantine(store.storage, current=current) == 1

    assert not store.storage.wake_quarantine_path(old.wake_signal_id).exists()
    assert json.loads(store.storage.wake_quarantine_archive_path(old.wake_signal_id).read_text())["wake_signal_id"] \
        == old.wake_signal_id
    assert store.storage.wake_quarantine_path(young.wake_signal_id).exists()
    rows, errors = store.wakes.attempts.quarantined()
    assert [row["wake_signal_id"] for row in rows] == [young.wake_signal_id] and errors == []
    assert store.wakes.attempts.quarantined_count() == 1


def test_a_preserved_corrupt_ledger_moves_with_its_record_and_not_before(tmp_path):
    store = _store(tmp_path)
    signal = _raise(store)
    attempts = store.storage.wake_attempt_path(signal.wake_signal_id)
    attempts.parent.mkdir(parents=True, exist_ok=True)
    attempts.write_bytes(b"\x00 corrupt ledger")
    _quarantine(store, signal, decision=ledger_corrupt_decision())
    preserved = store.storage.wake_quarantine_ledger_path(signal.wake_signal_id)
    archived_ledger = store.storage.wake_quarantine_archive_dir / "ledger" / preserved.name
    assert archive_stale_wake_quarantine(store.storage, current=QUARANTINED_AT + DAY) == 0
    # 记录按结案时间已满 14 天；坏账按文件时间（真实时间）还很新，只能是随记录一起移过去的。
    assert archive_stale_wake_quarantine(store.storage, current=QUARANTINED_AT + 15 * DAY) == 2
    assert archived_ledger.read_bytes() == b"\x00 corrupt ledger" and not preserved.exists()
    assert store.storage.wake_quarantine_archive_path(signal.wake_signal_id).exists()


def test_a_young_record_keeps_its_old_looking_ledger_in_place(tmp_path):
    store = _store(tmp_path)
    signal = _raise(store)
    attempts = store.storage.wake_attempt_path(signal.wake_signal_id)
    attempts.parent.mkdir(parents=True, exist_ok=True)
    attempts.write_bytes(b"\x00 corrupt ledger")
    current = time.time() + 30 * DAY
    # 记录按结案时间只有 1 天；坏账按文件时间已有 30 天，但它有记录对应，只能随记录一起走。
    _quarantine(store, signal, at=current - DAY, decision=ledger_corrupt_decision())

    assert archive_stale_wake_quarantine(store.storage, current=current) == 0
    assert store.storage.wake_quarantine_ledger_path(signal.wake_signal_id).exists()


def test_loose_files_use_max_mtime_ctime(tmp_path):
    store = _store(tmp_path)
    signal = _raise(store)
    pending = store.storage.wake_signal_path(signal)
    pending.write_bytes(b"{broken envelope")
    _quarantine(store, signal)
    unreadable = store.storage.wake_quarantine_unreadable_path(signal.wake_signal_id)
    orphan = store.storage.wake_quarantine_ledger_dir / "wake-orphan.json"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan ledger")
    now = time.time()
    # 结案时 os.replace 保留原 mtime：把 mtime 设成 30 天前也不能提前归档（ctime 是移过来的时刻）。
    os.utime(unreadable, (now - 30 * DAY, now - 30 * DAY))
    assert store.wakes.attempts.quarantined_count() == 1, "读不出的信封留档也算已结案"

    assert archive_stale_wake_quarantine(store.storage, current=now + 13 * DAY) == 0
    assert archive_stale_wake_quarantine(store.storage, current=now + 15 * DAY) == 2
    assert (store.storage.wake_quarantine_archive_dir / "unreadable" / unreadable.name).read_bytes() == b"{broken envelope"
    assert (store.storage.wake_quarantine_archive_dir / "ledger" / "wake-orphan.json").read_bytes() == b"orphan ledger"
    assert store.wakes.attempts.quarantined() == ([], []) and store.wakes.attempts.quarantined_count() == 0


def test_an_unreadable_top_level_record_falls_back_to_file_time(tmp_path):
    store = _store(tmp_path)
    signal = _raise(store)
    _quarantine(store, signal)
    record = store.storage.wake_quarantine_path(signal.wake_signal_id)
    record.write_text("{not json", encoding="utf-8")
    now = time.time()

    assert archive_stale_wake_quarantine(store.storage, current=now + 13 * DAY) == 0
    assert archive_stale_wake_quarantine(store.storage, current=now + 15 * DAY) == 1
    assert store.storage.wake_quarantine_archive_path(signal.wake_signal_id).read_text(encoding="utf-8") == "{not json"


def test_replayed_history_collisions_and_stray_files_are_left_alone(tmp_path):
    store = _store(tmp_path)
    replayed_signal, collided = _raise(store, key="replayed"), _raise(store, key="collided")
    _quarantine(store, replayed_signal)
    assert store.wakes.attempts.replay(replayed_signal.wake_signal_id, now=QUARANTINED_AT + 1).ok
    _quarantine(store, collided)
    occupied = store.storage.wake_quarantine_archive_path(collided.wake_signal_id)
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_text('{"keep": true}', encoding="utf-8")
    stray = store.storage.wake_quarantine_dir / "not a wake id!.json"
    stray.write_text("{}", encoding="utf-8")

    assert archive_stale_wake_quarantine(store.storage, current=time.time() + 30 * DAY) == 0
    history = store.storage.wake_replayed_dir / replayed_signal.wake_signal_id / "1.json"
    assert history.exists(), "replayed/ 是重放次数的唯一权威，不能被归档"
    assert occupied.read_text(encoding="utf-8") == '{"keep": true}'
    assert store.storage.wake_quarantine_path(collided.wake_signal_id).exists() and stray.exists()


def test_archiving_keeps_publication_semantics_and_blocks_replay(tmp_path):
    store = _store(tmp_path)
    signal = _raise(store, key="stask-1:body")
    _quarantine(store, signal)
    assert archive_stale_wake_quarantine(store.storage, current=QUARANTINED_AT + 15 * DAY) == 1

    again = _raise(store, key="stask-1:body")
    assert again.wake_signal_id == signal.wake_signal_id and again.status == WAKE_STATUS_FAILED_PERMANENTLY
    assert store.wakes.pending(limit=0) == []
    assert store.wakes.delivery_receipt("thread-a", "stask-1:body") == WAKE_STATUS_FAILED_PERMANENTLY
    replay = store.wakes.attempts.replay(signal.wake_signal_id, now=QUARANTINED_AT + 16 * DAY)
    assert (replay.ok, replay.error_code) == (False, WAKE_REPLAY_ARCHIVED)
    assert store.wakes.attempts.replay_source(signal.wake_signal_id) == (WAKE_REPLAY_ARCHIVED, {})
    assert store.wakes.attempts.replay_source("wake-unknown") == (WAKE_REPLAY_NOT_FOUND, {})


def test_an_archived_unreadable_envelope_is_refused_as_archived(tmp_path):
    store = _store(tmp_path)
    signal = _raise(store)
    store.storage.wake_signal_path(signal).write_bytes(b"{broken envelope")
    _quarantine(store, signal)
    assert archive_stale_wake_quarantine(store.storage, current=time.time() + 15 * DAY) == 1

    replay = store.wakes.attempts.replay(signal.wake_signal_id, now=time.time())
    assert (replay.ok, replay.error_code) == (False, WAKE_REPLAY_ARCHIVED)


def test_ledger_gc_reports_the_archive_and_retention_lists_archived_records(tmp_path):
    store = _store(tmp_path)
    mine, other = _raise(store, key="mine"), _raise(store, thread="thread-b", key="other")
    _quarantine(store, mine)
    _quarantine(store, other)

    summary = store.gc_stale_ledger_records(now=QUARANTINED_AT + 15 * DAY)

    assert summary["archived_wake_quarantine"] == 2
    files = _thread_wake_files(store.storage.wake_queue_dir, "thread-a", [])
    assert store.storage.wake_quarantine_archive_path(mine.wake_signal_id) in files
    assert store.storage.wake_quarantine_archive_path(other.wake_signal_id) not in files
