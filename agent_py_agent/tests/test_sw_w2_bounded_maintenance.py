"""显式运维用真实临时库/文件完成多片，单片仍保留原安全与预算边界。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.memory_store import retention_scan_budget as budgets
from agent_py_agent.agent.memory_store.retention import apply_owner_retention
from agent_py_agent.agent.runtime_db import repository as runtime
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.cli import home_runtime_commands as commands
from agent_py_agent.cli.parser import build_parser


def _locks(repo, count, *, live_head=0):
    # 前缀活锁与死锁共享排序键范围，确保空删除页也必须继续。
    rows = [(f"lock-{index:04d}", f"scope-{index}", 1 if index < live_head else 2)
            for index in range(count)]
    with repo.transaction() as conn:
        conn.executemany(
            "INSERT INTO resource_locks(lock_id,canonical_scope,holder_instance,pid,start_token,attempt_id,"
            "attempt_generation,workspace_epoch,lease_expires_at,created_at,updated_at) "
            "VALUES(?,?,'holder',?,'birth','',1,1,1,1,1)", rows)
    return [row[0] for row in rows]


@pytest.mark.parametrize("bounded", [None, False, True])
def test_lock_cleanup_is_always_one_bounded_page(tmp_path, monkeypatch, bounded):
    repo = runtime.RuntimeRepository(tmp_path / "runtime.db")
    page_limit = 256  # 复用原清理SQL的页大小，不为CLI另设上限。
    lock_ids = _locks(repo, 3 * page_limit)
    monkeypatch.setattr(runtime, "holder_is_alive", lambda *_: False)
    options = {} if bounded is None else {"bounded": bounded}
    released = repo.cleanup_stale_resource_locks(now=100, **options)
    assert released == lock_ids[:page_limit]
    assert repo._stale_lock_cursor == (1, lock_ids[page_limit - 1])
    for _ in range(3):
        released.extend(repo.cleanup_stale_resource_locks(now=100, **options))
    assert released == lock_ids and repo._stale_lock_cursor == (0.0, "")


@pytest.mark.parametrize("live_head", [False, True])
@pytest.mark.parametrize("resumed", [False, True])
def test_settle_finishes_three_pages_without_one_large_transaction(
    tmp_path, monkeypatch, capsys, live_head, resumed,
):
    repo = runtime.RuntimeRepository(tmp_path / "runtime.db")
    page_limit = 256
    head_size = page_limit if live_head else 0
    lock_ids = _locks(repo, 3 * page_limit + head_size, live_head=head_size)
    monkeypatch.setattr(runtime, "holder_is_alive", lambda pid, *_: pid == 1)
    if resumed:
        repo._stale_lock_cursor = (1, lock_ids[page_limit - 1])
    original = repo.transaction
    changed_per_transaction = []

    @contextmanager
    def observed():
        with original() as conn:
            before = conn.total_changes
            yield conn
            changed_per_transaction.append(conn.total_changes - before)
        # 真正提交后另一连接可立即取得写锁；不是把多个页包在同一大事务里。
        writer = sqlite3.connect(repo.db_path, timeout=0)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
        finally:
            writer.close()

    monkeypatch.setattr(repo, "transaction", observed)
    monkeypatch.setattr(commands, "make_agent", lambda _: SimpleNamespace(
        subagents=SimpleNamespace(runtime_db=repo)))
    args = build_parser().parse_args(["runtime-stale-attempts", "--settle", "--json"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["released_resource_lock_ids"] == lock_ids[head_size:]
    assert payload["settled_agent_run_ids"] == [] and payload["remaining"] == []
    assert [count for count in changed_per_transaction if count] == [page_limit] * 3
    assert repo._stale_lock_cursor == (0.0, "")
    with original() as conn:
        kept = [row[0] for row in conn.execute("SELECT lock_id FROM resource_locks ORDER BY lock_id")]
    assert kept == lock_ids[:head_size]


def _threads(home, count):
    now = datetime.now(timezone.utc)
    paths = []
    for index in range(count):
        thread = ConversationThread(thread_id=f"thread-{index:04d}", canonical_user_id="test",
            updated_at=(now - timedelta(days=400)).timestamp())
        path = home.owner_home_dir / "conversations" / "threads" / f"{thread.thread_id}.json"
        write_private_json_file_atomic(path, thread.to_dict())
        paths.append(path)
    return paths


@pytest.mark.parametrize("scan_budget", [None, False, True])
def test_retention_apply_always_budgets_and_saves_its_cursor(tmp_path, monkeypatch, scan_budget):
    home = ensure_my_agent_home(tmp_path / "home")
    paths = _threads(home, 5)
    monkeypatch.setattr(budgets.RetentionScanBudget, "default",
        classmethod(lambda cls: cls(2, 1000000, float("inf"))))
    options = {} if scan_budget is None else {"scan_budget": scan_budget}
    report = apply_owner_retention(home, **options)
    assert report.scan_partial and report.scan_cursor
    assert len(report.actions) == 2 and all(not path.exists() for path in paths[:2])
    assert all(path.exists() for path in paths[2:])
    cursor = home.owner_data_dir / "retention_scan_cursor.json"
    assert json.loads(cursor.read_text()) == report.scan_cursor


def test_home_retention_cli_finishes_budgeted_pages_and_reports_all_actions(tmp_path, monkeypatch, capsys):
    home = ensure_my_agent_home(tmp_path / "home")
    paths = _threads(home, 7)
    monkeypatch.setattr(budgets.RetentionScanBudget, "default",
        classmethod(lambda cls: cls(2, 1000000, float("inf"))))
    reports = []

    def applied(*args, **kwargs):
        report = apply_owner_retention(*args, **kwargs)
        reports.append(report)
        return report

    monkeypatch.setattr(commands, "apply_owner_retention", applied)
    monkeypatch.setattr(commands, "make_agent", lambda _: SimpleNamespace(home_paths=home))
    args = build_parser().parse_args(["home", "retention", "--apply", "--json"])
    assert args.func(args) == 0
    report = json.loads(capsys.readouterr().out)["retention"]
    assert len(reports) > 1 and all(page.scan_partial for page in reports[:-1])
    assert all(len(page.actions) <= 2 for page in reports)
    assert not report["scan_partial"] and not report["scan_cursor"]
    assert {action["path"] for action in report["actions"]} == {str(path) for path in paths}
    assert all(action["status"] == "trashed" for action in report["actions"])
    assert not any(path.exists() for path in paths)


def test_home_retention_cli_keeps_retry_errors_partial_without_spinning(tmp_path, monkeypatch, capsys):
    home = ensure_my_agent_home(tmp_path / "home")
    _threads(home, 1)
    broken = home.owner_home_dir / "memory_archive" / "snapshots" / "context_bundles"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not a directory")
    reports = []

    def applied(*args, **kwargs):
        assert not reports, "持久来源错误不能在显式CLI中无限续扫"
        report = apply_owner_retention(*args, **kwargs)
        reports.append(report)
        return report

    monkeypatch.setattr(commands, "apply_owner_retention", applied)
    monkeypatch.setattr(commands, "make_agent", lambda _: SimpleNamespace(home_paths=home))
    args = build_parser().parse_args(["home", "retention", "--apply", "--json"])
    assert args.func(args) == 0
    report = json.loads(capsys.readouterr().out)["retention"]
    assert report["errors"] and report["scan_partial"] and report["scan_cursor"]
    assert not report["ok"] and broken.read_text() == "not a directory"
