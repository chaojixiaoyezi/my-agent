"""真实SQLite授权器观察写闭包；未知新helper同样必须经过提交后通知。"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

import pytest

from agent_py_agent.agent.runtime_db import input_lifecycle
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.tests.test_s2w4_due_wakeup import KEY, OWNER, context_for, store


class WriteMeter:
    def __init__(self, monkeypatch):
        original = sqlite3.connect
        self.commits = []
        self.statements = []
        self.connections = []
        self.notifications = []
        meter = self
        classes = {}

        def connect(*args, **kwargs):
            base = kwargs.pop("factory", sqlite3.Connection)
            if base not in classes:
                class ObservedConnection(base):
                    def commit(self):
                        super().commit()
                        if self._s2_pending:
                            meter.commits.append(tuple(self._s2_pending))
                            self._s2_pending.clear()

                    def rollback(self):
                        super().rollback()
                        self._s2_pending.clear()
                classes[base] = ObservedConnection
            kwargs.update(factory=classes[base], cached_statements=0)
            conn = original(*args, **kwargs)
            conn._s2_pending = []

            def authorizer(action, table, column, database, source):
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and table in {"wake_queue", "scheduler_due_owners"}:
                    entry = (str(args[0]), table, action)
                    conn._s2_pending.append(entry)
                    meter.statements.append(entry)
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorizer)
            meter.connections.append(conn)
            return conn
        monkeypatch.setattr(sqlite3, "connect", connect)

    def step(self, operation, hints, kind, *, writes=True):
        before, notified = len(self.commits), len(self.notifications)
        generation = hints.timer.snapshot()["generation"]
        previous = hints.index.row(KEY)
        revision = (previous or {}).get("deadline_revisions", {}).get(kind, 0)
        result = operation()
        assert len(self.commits) - before == int(writes), "unexpected committed DML transaction"
        assert len(self.notifications) - notified == 1, "committed write missing exactly one notification"
        assert hints.timer.snapshot()["generation"] > generation
        assert hints.index.row(KEY)["deadline_revisions"][kind] > revision
        return result


def test_all_canonical_writers_and_background_pass_close(monkeypatch, tmp_path, record_property):
    meter = WriteMeter(monkeypatch)
    context, _registry, due = context_for(tmp_path)
    home = tmp_path / "owners/providers/feishu/users/alice"
    home.mkdir(parents=True)
    repo = RuntimeRepository(home / "runtime.db")
    hints = loops._OwnerWakeHints(context)
    removers = [due.add_due_listener(lambda *args: meter.notifications.append(("due", args))),
                input_lifecycle.add_wake_listener(lambda *args: meter.notifications.append(("runtime", args)))]
    def step(operation, kind="runtime_wake", **kwargs):
        return meter.step(operation, hints, kind, **kwargs)
    try:
        step(lambda: due.sync_owner(OWNER, store(200), now=100), "scheduler_due")
        step(lambda: due.claim_due_owners(now=200, lease_seconds=30), "scheduler_due")
        ledger = home / "data/scheduler"
        ledger.mkdir(parents=True)
        (ledger / "store.json").write_text(json.dumps({**store(201), "updated_at": 201}))
        step(lambda: due.repair_legacy_owner_ledgers(tmp_path / "owners"), "scheduler_due")
        step(lambda: repo.upsert_wake(root_task_id="a", next_due_at=100))
        step(lambda: repo.pop_due_wakes(now=101))
        wake = repo.next_runtime_wake()["wake_id"]
        step(lambda: repo.release_wake(wake, retry_after=102))
        step(lambda: repo.pop_due_wakes(now=103))
        step(lambda: repo.reclaim_expired_wakes(now=404))
        step(lambda: repo.complete_wake(wake))
        step(lambda: repo.upsert_wake(root_task_id="b", next_due_at=100))
        step(lambda: repo.cancel_wakes_for_task("b"))
        row = dict(step(lambda: repo.upsert_wake(root_task_id="c", next_due_at=100)))
        claim = step(lambda: repo.claim_runtime_wake(row, now=101))
        step(lambda: repo.perform_runtime_wake(claim, lambda: None), writes=False)
        step(lambda: repo.settle_runtime_wake(claim, retry_after=102))
        claim = step(lambda: repo.claim_runtime_wake(repo.next_runtime_wake(), now=103))
        step(lambda: repo.settle_runtime_wake(claim))
        row = dict(step(lambda: repo.upsert_wake(root_task_id="d", next_due_at=100, kind="task_input_deadline")))
        step(lambda: repo.retire_input_deadline(row))
        controller = loops._GatewaySchedulerDueController(context)
        hints.bind_controller("scheduler_due", controller)
        step(lambda: controller.tick(now=500), "scheduler_due")
        assert len(meter.commits) == len(meter.notifications) - 1  # perform不改wake，但契约要求同样通知
        record_property("writer_closure", json.dumps({"authorized_writes": len(meter.statements),
            "committed_write_transactions": len(meter.commits), "notifications": len(meter.notifications), "unmatched": 0}))
        assert meter.connections
    finally:
        for remove in removers:
            remove()
        hints.close()


def test_runtime_meter_falsifies_helper_that_skips_hook(monkeypatch, tmp_path):
    meter = WriteMeter(monkeypatch)
    context, _registry, _due = context_for(tmp_path)
    home = tmp_path / "owners/providers/feishu/users/alice"
    home.mkdir(parents=True)
    repo = RuntimeRepository(home / "runtime.db")
    hints = loops._OwnerWakeHints(context)
    remove = input_lifecycle.add_wake_listener(lambda *args: meter.notifications.append(args))
    try:
        meter.step(lambda: repo.upsert_wake(root_task_id="a", next_due_at=100), hints, "runtime_wake")

        def helper_without_hook():
            with closing(repo._runtime_connect()) as conn:
                conn.execute("DELETE FROM wake_queue")
                conn.commit()

        with pytest.raises(AssertionError, match="missing exactly one notification"):
            meter.step(helper_without_hook, hints, "runtime_wake")
        assert repo.next_runtime_wake() is None
    finally:
        remove()
        hints.close()


def test_notification_failure_is_counted_without_failing_runtime_commit(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    before = input_lifecycle._wake_listener_failures

    def fail(*args):
        raise RuntimeError("hint lost after commit")

    remove = input_lifecycle.add_wake_listener(fail)
    try:
        row = repo.upsert_wake(root_task_id="a", next_due_at=time.time() - 1)
        assert repo.read_runtime_wake(row["wake_id"]) is not None
        assert before + 1 == input_lifecycle._wake_listener_failures
        assert repo.due_runtime_wakes(now=time.time())
    finally:
        remove()
