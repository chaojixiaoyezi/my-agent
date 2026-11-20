"""验证目录续点、访问计量和留存扫描的覆盖。"""
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.session_task_retention import (
    maintain_session_task_payloads_lane,
)
from agent_py_agent.agent.user_space import owner_quota, storage_lanes


def _request(tmp_path, limit):
    root = tmp_path / "conversations"
    (root / "threads").mkdir(parents=True)
    (root / "session_tasks").mkdir()
    home = SimpleNamespace(
        owner_home_dir=tmp_path,
        owner_workspace_dir=tmp_path / "workspace",
        owner_retention_json=tmp_path / "retention.json",
    )
    request = storage_lanes.StorageLaneRequest(
        "session_task_payloads", home, 100.0, {},
        storage_lanes.StorageLaneBudget(limit, None, None), False, False, None, None,
    )
    return request, root / "session_tasks"


@pytest.mark.parametrize("size", [1_000, 100_000])
def test_directory_work_is_bounded_by_the_page(tmp_path, monkeypatch, size):
    request, directory = _request(tmp_path, 7)
    if os.open in os.supports_dir_fd:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            for index in range(size):
                os.close(os.open(f"{index:06d}.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=descriptor))
        finally:
            os.close(descriptor)
    else:
        for index in range(size):
            (directory / f"{index:06d}.json").touch(exist_ok=False)
    visited = 0
    original_scandir = os.scandir
    original_next = owner_quota._QuotaDirectoryStream.__next__

    class CountedScan:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal visited
            entry = next(self.entries)
            visited += 1
            return entry

    def scan(path):
        entries = original_scandir(path)
        return CountedScan(entries) if path == directory else entries

    def native_next(stream):
        nonlocal visited
        entry = original_next(stream)
        visited += 1
        return entry

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(owner_quota._QuotaDirectoryStream, "__next__", native_next)
    monkeypatch.setattr(storage_lanes.time, "monotonic", lambda: 0)
    for _ in range(2):
        visited = 0
        receipt = maintain_session_task_payloads_lane(request)
        assert visited == receipt["items"] == 7
        assert not receipt["complete"]
        request = replace(request, cursor=receipt["cursor"])


def test_ignored_entries_charge_budget_and_legacy_cycle_wraps(tmp_path, monkeypatch):
    request, directory = _request(tmp_path, 2)
    for index in range(5):
        (directory / f"ignored-{index}.txt").touch()
    request = replace(request, cursor={"root": 0, "after": "zz-last-name", "cycle_generation": 4})
    monkeypatch.setattr(storage_lanes.time, "monotonic", lambda: 0)
    charged = 0
    for _ in range(5):
        result = maintain_session_task_payloads_lane(request)
        storage_lanes.validate_lane_receipt(result)
        assert result["items"] <= 2 and not result["protected"]
        charged += result["items"]
        if result["complete"]:
            break
        assert result["cursor"]["cycle_generation"] == 4
        request = replace(request, cursor=result["cursor"])
    assert result["complete"] and result["cursor"]["cycle_generation"] == 5
    assert charged == 5


@pytest.mark.parametrize("mutate", [False, True])
def test_cycle_covers_surviving_entries_and_restarts(tmp_path, monkeypatch, mutate):
    from agent_py_agent.agent.conversation import session_task_retention

    request, directory = _request(tmp_path, 3)
    names = {f"task-{index}.json" for index in range(17)}
    for name in sorted(names):
        (directory / name).write_text("{}")
    reads = []
    original = session_task_retention.read_json_file_report

    def record(path, **kwargs):
        reads.append(path.name)
        return original(path, **kwargs)

    monkeypatch.setattr(storage_lanes.time, "monotonic", lambda: 0)
    monkeypatch.setattr(session_task_retention, "read_json_file_report", record)
    receipt = maintain_session_task_payloads_lane(request)
    if mutate:
        removed = min(names - set(reads))
        (directory / removed).unlink()
        (directory / "new-task.json").write_text("{}")
        names.remove(removed)
    for _ in range(12):
        if receipt["complete"]:
            break
        request = replace(request, cursor=receipt["cursor"])
        receipt = maintain_session_task_payloads_lane(request)
        assert receipt["items"] <= 3
    assert receipt["complete"] and receipt["cycle_generation"] == 1
    assert set(reads) >= names
    assert len(reads) == len(set(reads))
    reads.clear()
    request = replace(request, cursor=receipt["cursor"])
    for _ in range(12):
        receipt = maintain_session_task_payloads_lane(request)
        if receipt["complete"]:
            break
        request = replace(request, cursor=receipt["cursor"])
    assert receipt["complete"] and receipt["cycle_generation"] == 2
    expected = names | ({"new-task.json"} if mutate else set())
    assert sorted(reads) == sorted(expected)


def test_sidecar_rechecks_for_data_inside_write_gate(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.tool_output_paths import tool_output_root
    from agent_py_agent.agent.memory_archive.tool_output_retention import (
        maintain_owner_tool_outputs_lane,
    )

    request, _ = _request(tmp_path, 4)
    outputs = tool_output_root(tmp_path)
    outputs.mkdir(parents=True)
    lock = outputs / "sample.json.lock"
    lock.touch()
    os.utime(lock, (1, 1))
    data = outputs / "sample.json"
    writes = []

    def publish_data_before_cleanup(operation):
        writes.append(True)
        data.write_text("{}")
        return operation()

    request = replace(request, kind="tool_outputs", now=200_000.0, commit=publish_data_before_cleanup)
    monkeypatch.setattr(storage_lanes.time, "monotonic", lambda: 0)
    result = maintain_owner_tool_outputs_lane(request)
    storage_lanes.validate_lane_receipt(result)
    assert result["complete"] and result["cycle_generation"] == 1
    assert result["items"] == 2
    assert writes == [True] and lock.exists() and data.exists()
