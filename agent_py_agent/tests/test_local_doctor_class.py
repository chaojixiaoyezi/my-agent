"""local_doctor 模块测试。

测试 build_local_doctor_report、rebuild_local_store、rebuild_subagent_index、build_status_suggestions 等函数。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.cli.local_doctor import (
    _add_doctor_check,
    build_local_doctor_report,
    build_status_suggestions,
    rebuild_local_store,
    rebuild_subagent_index,
)

# ── _add_doctor_check 测试 ─────────────────────────────────────────────────

def test_add_doctor_check_ok():
    """测试添加正常的检查项。"""
    checks = []
    _add_doctor_check(
        checks,
        name="test_check",
        ok=True,
        severity="P1",
        message="检查通过",
    )
    assert len(checks) == 1
    assert checks[0]["name"] == "test_check"
    assert checks[0]["ok"] is True
    assert checks[0]["severity"] == "ok"
    assert checks[0]["details"] == {}


def test_add_doctor_check_with_details():
    """测试带详情信息的检查项。"""
    checks = []
    details = {"path": "/tmp/test", "count": 5}
    _add_doctor_check(
        checks,
        name="test_detail",
        ok=False,
        severity="P0",
        message="检查失败",
        details=details,
    )
    assert checks[0]["details"] == details


def test_add_doctor_check_not_ok_sets_severity():
    """测试检查失败时 severity 被设置为传入值。"""
    checks = []
    _add_doctor_check(
        checks,
        name="fail_check",
        ok=False,
        severity="P2",
        message="有问题",
    )
    assert checks[0]["severity"] == "P2"


# ── build_local_doctor_report 测试 ─────────────────────────────────────────

def test_build_local_doctor_report_basic(tmp_path):
    """测试基本报告构建。"""
    db_file = tmp_path / "test.db"
    files_dir = tmp_path / "files"
    events_file = tmp_path / "events.jsonl"
    files_dir.mkdir()
    db_file.touch()
    events_file.touch()

    mock_agent = MagicMock()
    mock_agent.configure_mock(**{
        "local_store.stats.return_value": {
            "db_path": str(db_file),
            "files_dir": str(files_dir),
            "events_path": str(events_file),
            "record_count": 10,
        },
        "local_store.source_counts.return_value": {"memory": 5, "gateway": 3},
        "memory.path": tmp_path / "memory",
        "subagents.workspace": tmp_path / "subagents",
        "config.gateway_processing_timeout_seconds": 300,
    })
    mock_agent.local_store.missing_content_files.return_value = []

    with patch("agent_py_agent.cli.local_doctor.gateway_paths") as mock_paths:
        mock_paths.return_value = MagicMock(root=tmp_path)
        with patch("agent_py_agent.cli.local_doctor.gateway_request_counts") as mock_counts:
            mock_counts.return_value = {"pending": 0, "processing": 0, "done": 5}
            with patch("agent_py_agent.cli.local_doctor.gateway_stale_processing") as mock_stale:
                mock_stale.return_value = []
                with patch("agent_py_agent.cli.local_doctor._memory_record_count") as mock_mem:
                    mock_mem.return_value = 5
                    mock_agent.subagents.list_runs.return_value = []

                    result = build_local_doctor_report(mock_agent)

    assert result["ok"] is True
    assert "checks" in result
    assert "suggestions" in result


def test_build_local_doctor_report_with_stale_processing(tmp_path):
    """测试存在 stale processing 时的报告。"""
    db_file = tmp_path / "test.db"
    files_dir = tmp_path / "files"
    events_file = tmp_path / "events.jsonl"
    files_dir.mkdir()
    db_file.touch()
    events_file.touch()

    mock_agent = MagicMock()
    mock_agent.configure_mock(**{
        "local_store.stats.return_value": {
            "db_path": str(db_file),
            "files_dir": str(files_dir),
            "events_path": str(events_file),
            "record_count": 10,
        },
        "local_store.source_counts.return_value": {"memory": 5, "gateway": 3},
        "memory.path": tmp_path / "memory",
        "subagents.workspace": tmp_path / "subagents",
        "config.gateway_processing_timeout_seconds": 300,
    })
    mock_agent.local_store.missing_content_files.return_value = []

    with patch("agent_py_agent.cli.local_doctor.gateway_paths") as mock_paths:
        mock_paths.return_value = MagicMock(root=tmp_path)
        with patch("agent_py_agent.cli.local_doctor.gateway_request_counts") as mock_counts:
            mock_counts.return_value = {"pending": 2, "processing": 1, "done": 5}
            with patch("agent_py_agent.cli.local_doctor.gateway_stale_processing") as mock_stale:
                mock_stale.return_value = [{"id": "stale-1"}]
                with patch("agent_py_agent.cli.local_doctor._memory_record_count") as mock_mem:
                    mock_mem.return_value = 5
                    mock_agent.subagents.list_runs.return_value = []

                    result = build_local_doctor_report(mock_agent)

    assert result["ok"] is False
    assert any("processing" in s.lower() for s in result["suggestions"])


def test_build_local_doctor_report_missing_content_files(tmp_path):
    """测试存在缺失正文文件时的报告。"""
    db_file = tmp_path / "test.db"
    files_dir = tmp_path / "files"
    events_file = tmp_path / "events.jsonl"
    files_dir.mkdir()
    db_file.touch()
    events_file.touch()

    mock_agent = MagicMock()
    mock_agent.configure_mock(**{
        "local_store.stats.return_value": {
            "db_path": str(db_file),
            "files_dir": str(files_dir),
            "events_path": str(events_file),
            "record_count": 5,
        },
        "local_store.source_counts.return_value": {"memory": 5},
        "local_store.missing_content_files.return_value": ["file1.txt", "file2.txt"],
        "memory.path": tmp_path / "memory",
        "subagents.workspace": tmp_path / "subagents",
        "config.gateway_processing_timeout_seconds": 300,
    })

    with patch("agent_py_agent.cli.local_doctor.gateway_paths") as mock_paths:
        mock_paths.return_value = MagicMock(root=tmp_path)
        with patch("agent_py_agent.cli.local_doctor.gateway_request_counts") as mock_counts:
            mock_counts.return_value = {}
            with patch("agent_py_agent.cli.local_doctor.gateway_stale_processing") as mock_stale:
                mock_stale.return_value = []
                with patch("agent_py_agent.cli.local_doctor._memory_record_count") as mock_mem:
                    mock_mem.return_value = 5
                    mock_agent.subagents.list_runs.return_value = []

                    result = build_local_doctor_report(mock_agent)

    assert result["ok"] is False
    content_check = next(c for c in result["checks"] if c["name"] == "local_store_content_files")
    assert content_check["ok"] is False


# ── rebuild_subagent_index 测试 ───────────────────────────────────────────

def test_rebuild_subagent_index_basic():
    """测试基本子代理索引重建。"""
    mock_agent = MagicMock()
    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_task.task_dir = "/tmp/tasks/task-123"
    mock_agent.subagents.list_runs.return_value = [mock_task]

    count = rebuild_subagent_index(mock_agent)

    assert count >= 1
    mock_agent.subagents._index_task.assert_called_once_with(mock_task)


def test_rebuild_subagent_index_with_existing_files(tmp_path):
    """测试存在工单文件时的索引重建。"""
    mock_agent = MagicMock()
    task_dir = tmp_path / "task-123"
    task_dir.mkdir(parents=True)
    (task_dir / "WORK_LOG.md").write_text("work log content", encoding="utf-8")
    (task_dir / "execution_context.json").write_text("{}", encoding="utf-8")

    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_task.task_dir = str(task_dir)
    mock_agent.subagents.list_runs.return_value = [mock_task]

    count = rebuild_subagent_index(mock_agent)

    assert count >= 2  # _index_task + WORK_LOG.md


# ── rebuild_local_store 测试 ───────────────────────────────────────────────

def test_rebuild_local_store_memory_only():
    """测试仅重建 memory 源。"""
    mock_agent = MagicMock()
    mock_agent.memory.index_all.return_value = 10
    mock_agent.local_store.stats.return_value = {"record_count": 10}
    mock_agent.local_store.source_counts.return_value = {"memory": 10}

    result = rebuild_local_store(mock_agent, sources={"memory"})

    assert result["reset"] is False
    assert "memory" in result["sources"]
    assert result["memory_indexed"] == 10


def test_rebuild_local_store_with_reset():
    """测试带 reset 的重建。"""
    mock_agent = MagicMock()
    mock_agent.memory.index_all.return_value = 5
    mock_agent.local_store.rebuild_fts.return_value = 100
    mock_agent.local_store.stats.return_value = {"record_count": 5}
    mock_agent.local_store.source_counts.return_value = {"memory": 5}

    result = rebuild_local_store(mock_agent, sources={"memory", "fts"}, reset=True)

    assert result["reset"] is True
    mock_agent.local_store.reset.assert_called_once()
    assert result["fts_rebuilt"] == 100


def test_rebuild_local_store_all_sources():
    """测试重建所有源。"""
    mock_agent = MagicMock()
    mock_agent.memory.index_all.return_value = 10
    mock_agent.local_store.rebuild_fts.return_value = 50
    mock_agent.local_store.stats.return_value = {"record_count": 30}
    mock_agent.local_store.source_counts.return_value = {"memory": 10, "subagent": 20}

    with patch("agent_py_agent.cli.local_doctor.rebuild_gateway_index") as mock_gw:
        mock_gw.return_value = 5
        mock_agent.subagents.list_runs.return_value = []

        result = rebuild_local_store(mock_agent, sources={"memory", "gateway", "subagent", "fts"})

    assert result["memory_indexed"] == 10
    assert result["gateway_indexed"] == 5
    assert "subagent_indexed" in result


# ── build_status_suggestions 测试 ─────────────────────────────────────────

def test_build_status_suggestions_gateway_stopped_with_requests():
    """测试 gateway 停止但队列有请求时的建议。"""
    mock_agent = MagicMock()
    payload = {
        "gateway": {"status": "stopped", "request_counts": {"pending": 5}},
        "local_store": {"record_count": 10},
        "subagents": {"hot_count": 0},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("gateway start" in s for s in suggestions)


def test_build_status_suggestions_stale_gateway():
    """测试 stale gateway 的建议。"""
    mock_agent = MagicMock()
    payload = {
        "gateway": {"status": "stale", "request_counts": {}},
        "local_store": {"record_count": 10},
        "subagents": {"hot_count": 0},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("restart" in s for s in suggestions)


def test_build_status_suggestions_failed_requests():
    """测试有失败请求时的建议。"""
    mock_agent = MagicMock()
    payload = {
        "gateway": {"status": "running", "request_counts": {"failed": 3}},
        "local_store": {"record_count": 10},
        "subagents": {"hot_count": 0},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("gateway logs" in s or "timeline" in s for s in suggestions)


def test_build_status_suggestions_empty_local_store():
    """测试本地存储为空时的建议。"""
    mock_agent = MagicMock()
    mock_agent.subagents.list_runs.return_value = [MagicMock()]
    mock_agent.memory.path = Path("/tmp/memory")
    payload = {
        "gateway": {"status": "stopped", "request_counts": {}},
        "local_store": {"record_count": 0},
        "subagents": {"hot_count": 0},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("local-rebuild" in s for s in suggestions)


def test_build_status_suggestions_hot_subagents():
    """测试存在热子代理时的建议。"""
    mock_agent = MagicMock()
    payload = {
        "gateway": {"status": "running", "request_counts": {}},
        "local_store": {"record_count": 10},
        "subagents": {"hot_count": 3},
        "timeline": True,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("subagents-due-check" in s for s in suggestions)


def test_build_status_suggestions_no_timeline():
    """测试无时间线但有记录时的建议。"""
    mock_agent = MagicMock()
    mock_agent.subagents.list_runs.return_value = []
    payload = {
        "gateway": {"status": "running", "request_counts": {}},
        "local_store": {"record_count": 100},
        "subagents": {"hot_count": 0},
        "timeline": None,
    }

    suggestions = build_status_suggestions(mock_agent, payload)

    assert any("timeline" in s and "rebuild" in s for s in suggestions)


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_build_local_doctor_report_empty_local_store_with_sources(tmp_path):
    """测试本地存储为空但有其他来源时的建议。"""
    db_file = tmp_path / "test.db"
    files_dir = tmp_path / "files"
    events_file = tmp_path / "events.jsonl"
    files_dir.mkdir()
    db_file.touch()
    events_file.touch()

    mock_agent = MagicMock()
    mock_agent.configure_mock(**{
        "local_store.stats.return_value": {
            "db_path": str(db_file),
            "files_dir": str(files_dir),
            "events_path": str(events_file),
            "record_count": 0,
        },
        "local_store.source_counts.return_value": {},
        "memory.path": tmp_path / "memory",
        "subagents.workspace": tmp_path / "subagents",
        "config.gateway_processing_timeout_seconds": 300,
    })
    mock_agent.subagents.list_runs.return_value = [MagicMock()]  # 有 subagent

    with patch("agent_py_agent.cli.local_doctor.gateway_paths") as mock_paths:
        mock_paths.return_value = MagicMock(root=tmp_path)
        with patch("agent_py_agent.cli.local_doctor.gateway_request_counts") as mock_counts:
            mock_counts.return_value = {"pending": 1}  # 有请求
            with patch("agent_py_agent.cli.local_doctor.gateway_stale_processing") as mock_stale:
                mock_stale.return_value = []
                with patch("agent_py_agent.cli.local_doctor._memory_record_count") as mock_mem:
                    mock_mem.return_value = 10
                    result = build_local_doctor_report(mock_agent)

    assert any("local-rebuild" in s for s in result["suggestions"])


def test_add_doctor_check_empty_message():
    """测试空消息的检查项。"""
    checks = []
    _add_doctor_check(
        checks,
        name="empty_msg",
        ok=True,
        severity="P1",
        message="",
    )
    assert checks[0]["message"] == ""


def test_rebuild_local_store_unknown_source():
    """测试未知来源被忽略（只处理已知来源）。"""
    mock_agent = MagicMock()
    mock_agent.memory.index_all.return_value = 0
    mock_agent.local_store.stats.return_value = {"record_count": 0}
    mock_agent.local_store.source_counts.return_value = {}

    result = rebuild_local_store(mock_agent, sources={"unknown_source"})

    # unknown_source 保留在 sources 列表中，但不会触发任何处理
    assert "unknown_source" in result["sources"]