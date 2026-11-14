"""W1：归档失败保留原件，任务探针失败有界、结构化且不泄漏秘密。"""
import errno
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import runtime, store_io


def test_archive_lock_failure_rolls_back(tmp_path, monkeypatch, caplog):
    source = tmp_path / "source" / "tasks.jsonl"
    source.parent.mkdir()
    source.write_text("data")
    lock = Path(f"{source}.lock")
    lock.write_text("lock")
    archive = tmp_path / "archive"
    rename = Path.rename

    def move(path, target):
        if path.suffix == ".lock":
            raise PermissionError(errno.EACCES, "denied")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", move)
    with caplog.at_level(logging.WARNING):
        assert store_io.archive_ledger_file(source, archive) is False
    assert source.read_text() == "data" and lock.read_text() == "lock"
    assert list(archive.iterdir()) == []
    assert "LEDGER_ARCHIVE_LOCK_MOVE_FAILED" in caplog.text
    assert "rolled_back=True" in caplog.text and "EACCES" in caplog.text
    monkeypatch.setattr(Path, "rename", rename)
    assert store_io.archive_ledger_file(source, archive) is True
    assert (archive / source.name).read_text() == "data"
    assert (archive / lock.name).read_text() == "lock"


def test_archive_rollback_failure_reports_both_errors(tmp_path, monkeypatch, caplog):
    source = tmp_path / "tasks.jsonl"
    source.write_text("data")
    Path(f"{source}.lock").write_text("lock")
    archive = tmp_path / "archive"
    rename = Path.rename

    def move(path, target):
        if path.suffix == ".lock":
            raise PermissionError(errno.EACCES, "denied")
        if path.parent == archive:
            raise OSError(errno.EBUSY, "busy")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", move)
    assert store_io.archive_ledger_file(source, archive) is False
    assert (archive / source.name).read_text() == "data"
    assert "rolled_back=False" in caplog.text
    assert "EACCES" in caplog.text and "EBUSY" in caplog.text
    assert str(source) in caplog.text and str(archive / source.name) in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR


@pytest.mark.parametrize("probe,expected", [("task_work_kind", ""), ("task_has_active_goal", False), ("task_link_status", "unknown")])
@pytest.mark.parametrize("error", [PermissionError("denied"), OSError(errno.EBUSY, "busy"), json.JSONDecodeError("bad", "?", 0)])
@pytest.mark.parametrize("legacy", [False, True])
def test_task_probes_log_structured_failure(probe, expected, error, legacy, caplog):
    def fail(*args):
        raise error

    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    tasks = SimpleNamespace(thread_for=fail, list_report=fail)
    if not legacy:
        tasks.load_report = fail
    scheduler.store = SimpleNamespace(tasks=tasks)
    method = getattr(scheduler, f"_{probe}")
    args = ("thread", "task-id") if probe == "task_link_status" else ("task-id",)
    with caplog.at_level(logging.WARNING):
        assert method(*args) == expected
        assert method(*args) == expected
    records = [r for r in caplog.records if getattr(r, "probe", None) == probe]
    assert len(records) == 1
    row = records[0]
    assert row.task_id == "task-id" and row.error_type == type(error).__name__
    assert row.error == str(error)
    assert f"probe={probe}" in row.message and "task_id=task-id" in row.message


def test_probe_warning_cache_is_bounded_and_redacted(caplog):
    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    for index in range(scheduler._TASK_PROBE_WARN_LIMIT + 3):
        scheduler._record_task_probe_failure("task_work_kind", str(index), ValueError("password=test-secret"))
    assert len(scheduler._task_probe_warnings) == scheduler._TASK_PROBE_WARN_LIMIT
    assert "test-secret" not in caplog.text


def test_real_task_link_corruption_reports_without_raising(tmp_path, caplog):
    from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree

    agent, _, _ = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    store.storage.task_path("task-root").write_text("{")
    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    scheduler.store = store
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            assert scheduler._task_work_kind("task-root") == ""
            assert scheduler._task_link_status("irrelevant", "task-root") == "unknown"
    records = [row for row in caplog.records if getattr(row, "task_id", None) == "task-root"]
    assert len(records) == 2
    assert {row.probe for row in records} == {"task_work_kind", "task_link_status"}
    assert {row.error_type for row in records} == {"JSONDecodeError"}


def test_legacy_error_reports_are_not_discarded(caplog):
    error = {"task_id": "task", "error_type": "PermissionError", "message": "password=test-secret"}
    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    scheduler.store = SimpleNamespace(tasks=SimpleNamespace(
        thread_for=lambda _: SimpleNamespace(thread_id="thread"), list_report=lambda _: ([], [error])))
    for _ in range(2):
        assert scheduler._task_work_kind("task") == ""
        assert scheduler._task_link_status("thread", "task") == "unknown"
    records = [row for row in caplog.records if getattr(row, "task_id", None) == "task"]
    assert len(records) == 2 and all(row.error_type == "PermissionError" for row in records)
    assert "test-secret" not in caplog.text


def test_missing_task_report_is_normal_absence(caplog):
    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    scheduler.store = SimpleNamespace(tasks=SimpleNamespace(load_report=lambda _: (None,
        {"error_type": "FileNotFoundError", "message": "missing"})))
    assert scheduler._task_work_kind("missing") == ""
    assert scheduler._task_link_status("thread", "missing") == "missing"
    assert not [row for row in caplog.records if getattr(row, "probe", None)]
