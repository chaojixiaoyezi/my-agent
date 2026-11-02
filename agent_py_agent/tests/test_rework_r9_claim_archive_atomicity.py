"""R9-R1 P1-R9-01：过期终态 claim 的复核、取消证明检查点与 JSON 移动在同一原 claim 迁移锁内完成。

真实 ConversationStore / ClaimStore / SessionTaskStore 组合，使用默认七天保留期；只在原检查点与原 rename
处观察顺序，不替换锁或存储实现。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from agent_py_agent.agent.conversation import store as conversation_store
from agent_py_agent.agent.conversation.session_tasks import SessionTaskStore
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.io import try_locked_file_transition

RETENTION = conversation_store._LEDGER_GC_RETENTION_SECONDS
DAY = 86400.0


def _case(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread_id = store.threads.create(title="r9 claim archive").thread_id
    path = store.storage.background_claim_path(thread_id)
    archive = store.storage.background_claims_dir.parent / ".ledger_archive" / "claims"
    return store, thread_id, path, archive


def _acquire(store, thread_id, task_id, *, now):
    claim = store.claims.acquire({"thread_id": thread_id, "task_id": task_id, "reason": "r9-body", "now": now})
    assert claim is not None and claim["status"] == "running" and claim["task_id"] == task_id
    return claim


def _finish(store, thread_id, claim, *, now):
    finished = store.claims.finish({"thread_id": thread_id, "claim_id": claim["claim_id"], "status": "finished",
                                    "now": now})
    assert finished is not None and finished["status"] == "finished"
    return finished


def _finished_body(store, thread_id, task_id, *, started):
    # 原 acquire → finish 生命周期产生的终态 claim，不手写 JSON。
    return _finish(store, thread_id, _acquire(store, thread_id, task_id, now=started), now=started + 60)


def _identity(path: Path):
    info = os.stat(path)
    return info.st_dev, info.st_ino


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _lock_is_held(path: Path) -> bool:
    # 从另一线程非阻塞试原迁移锁；拿不到即说明调用方仍在同一临界区内。
    seen = []

    def probe():
        with try_locked_file_transition(path) as acquired:
            seen.append(acquired)

    worker = threading.Thread(target=probe, name="r9-lock-probe")
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and len(seen) == 1
    return not seen[0]


def _hook_claim_rename(monkeypatch, path: Path, before):
    real = Path.rename
    calls = []

    def rename(self, target):
        if Path(self) == path:
            calls.append(Path(target))
            before()
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    return calls


def _run_into(results, name, call, done=None):
    try:
        results[name] = call()
    except BaseException as exc:  # noqa: BLE001 - 交给测试线程断言
        results[name] = exc
    finally:
        if done is not None:
            done.set()


def test_waiting_successor_body_is_serialized_behind_locked_archive(tmp_path, monkeypatch):
    store, thread_id, path, archive = _case(tmp_path)
    current = time.time()
    first = _finished_body(store, thread_id, "body-1", started=current - RETENTION - DAY)
    lock = path.with_name(path.name + ".lock")
    lock_identity = _identity(lock)
    entered, release, successor_done = threading.Event(), threading.Event(), threading.Event()
    held_during, results = {}, {}
    real_checkpoint = SessionTaskStore.preserve_terminal_cancellation_claim

    def checkpoint(self, claim):
        if threading.current_thread().name == "r9-archive":
            held_during["checkpoint"] = _lock_is_held(path)
            entered.set()
            assert release.wait(15)
        return real_checkpoint(self, claim)

    def before_rename():
        # 旧实现在此已释放锁：等待中的后继会先写入运行中 claim，再被按路径移走。
        held_during["rename"] = _lock_is_held(path)
        successor_done.wait(0.5)

    monkeypatch.setattr(SessionTaskStore, "preserve_terminal_cancellation_claim", checkpoint)
    renames = _hook_claim_rename(monkeypatch, path, before_rename)
    archiver = threading.Thread(target=_run_into, name="r9-archive", args=(
        results, "archive", lambda: store.claims.archive_stale(current=current, retention_seconds=RETENTION)))
    successor = threading.Thread(target=_run_into, name="r9-successor", args=(
        results, "successor", lambda: _acquire(store, thread_id, "body-2", now=current), successor_done))
    archiver.start()
    try:
        assert entered.wait(15), "archive did not reach the cancellation-proof checkpoint"
        successor.start()
        successor.join(0.3)
        assert successor.is_alive(), "successor acquire must wait for the archive transition lock"
    finally:
        release.set()
        archiver.join(15)
        if successor.ident is not None:
            successor.join(15)
    assert not archiver.is_alive() and not successor.is_alive()
    assert results["archive"] == 1
    assert held_during == {"checkpoint": True, "rename": True}
    assert renames == [archive / path.name]
    archived = _read(archive / path.name)
    assert archived["claim_id"] == first["claim_id"] and archived["status"] == "finished"
    second = results["successor"]
    assert isinstance(second, dict), second
    live = store.claims.load(thread_id)
    assert live["claim_id"] == second["claim_id"] and live["status"] == "running" and live["task_id"] == "body-2"
    # 稳定 sidecar 原位不动，后继与归档始终争用同一锁 inode；归档目录不出现锁文件。
    assert _identity(lock) == lock_identity
    assert not list(archive.glob("*.lock"))


def test_second_same_scope_body_follows_default_retention_through_maintenance(tmp_path):
    store, thread_id, path, archive = _case(tmp_path)
    # 维护时钟始终早于锁文件真实出生时间，原 24h 孤儿锁清扫不会在本用例内合法回收该 sidecar。
    started = time.time() - 3 * RETENTION
    first = _finished_body(store, thread_id, "body-1", started=started)
    lock_identity = _identity(path.with_name(path.name + ".lock"))
    successor_at = started + 60 + RETENTION + DAY
    # 后继先于维护领取：锁内重读看到运行中 claim，不能按旧终态预判移走。
    second = _acquire(store, thread_id, "body-2", now=successor_at)
    assert second["previous_claim"]["claim_id"] == first["claim_id"]
    assert store.gc_stale_ledger_records(now=successor_at)["archived_claims"] == 0
    assert store.claims.load(thread_id)["claim_id"] == second["claim_id"]
    finished = _finish(store, thread_id, second, now=successor_at + 60)
    # 默认七天保留期内保留，超过后才移入归档；两次维护都不移动或重建锁文件。
    assert store.gc_stale_ledger_records(now=successor_at + 60 + RETENTION - DAY)["archived_claims"] == 0
    assert _read(path) == finished
    assert store.gc_stale_ledger_records(now=successor_at + 60 + RETENTION + 1)["archived_claims"] == 1
    assert not path.exists() and _read(archive / path.name) == finished
    assert _identity(path.with_name(path.name + ".lock")) == lock_identity


def test_archive_rename_failure_keeps_original_claim_and_lock_for_retry(tmp_path, monkeypatch):
    store, thread_id, path, archive = _case(tmp_path)
    current = time.time()
    _finished_body(store, thread_id, "body-1", started=current - RETENTION - DAY)
    original = path.read_bytes()
    lock_identity = _identity(path.with_name(path.name + ".lock"))
    failures = []

    def fail_once():
        if not failures:
            failures.append("rename")
            raise OSError("simulated archive rename failure")

    renames = _hook_claim_rename(monkeypatch, path, fail_once)
    assert store.claims.archive_stale(current=current, retention_seconds=RETENTION) == 0
    assert failures == ["rename"] and path.read_bytes() == original
    assert _identity(path.with_name(path.name + ".lock")) == lock_identity
    # 下一维护周期按同一原身份重试成功，归档正文字节不变。
    assert store.claims.archive_stale(current=current, retention_seconds=RETENTION) == 1
    assert len(renames) == 2 and not path.exists()
    assert (archive / path.name).read_bytes() == original
    assert _identity(path.with_name(path.name + ".lock")) == lock_identity
