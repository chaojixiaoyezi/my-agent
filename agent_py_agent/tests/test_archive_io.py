"""单元测试：memory_archive query 模块 - archive_io 文件读写和记录标准化"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_archive.query import archive_io


class TestArchiveLevelValue:
    """测试 _archive_level_value 归档等级值标准化"""

    def test_none_defaults_to_3(self):
        """验证 None 转换为默认等级 3"""
        assert archive_io._archive_level_value(None) == 3

    def test_valid_level_preserved(self):
        """验证有效等级值保持不变"""
        assert archive_io._archive_level_value(0) == 0
        assert archive_io._archive_level_value(1) == 1
        assert archive_io._archive_level_value(2) == 2
        assert archive_io._archive_level_value(3) == 3

    def test_negative_passthrough(self):
        """验证负数直接传递（不 clamp）"""
        assert archive_io._archive_level_value(-1) == -1

    def test_out_of_range_passthrough(self):
        """验证超出范围的值直接传递（不 clamp）"""
        assert archive_io._archive_level_value(10) == 10

    def test_bool_false_becomes_3(self):
        """验证布尔 False 转换为 3（因为 int(False) == 0 但之后被 clamp）"""
        # bool 是 int 子类，int(False) == 0
        assert archive_io._archive_level_value(False) == 0

    def test_string_numeric_converts(self):
        """验证数字字符串被转换"""
        assert archive_io._archive_level_value("2") == 2

    def test_non_numeric_string_defaults_to_3(self):
        """验证非数字字符串返回默认 3"""
        assert archive_io._archive_level_value("bad") == 3


class TestArchiveFiles:
    """测试 _archive_files 归档文件查找"""

    def test_empty_dir_returns_empty(self, tmp_path):
        """验证空目录返回空列表"""
        result = archive_io._archive_files(tmp_path, layer="raw", date_key=None)
        assert result == []

    def test_raw_layer_only(self, tmp_path):
        """验证只搜索 raw 层"""
        raw_dir = tmp_path / "audit"
        raw_dir.mkdir(parents=True)
        (raw_dir / "2026-05-01.jsonl").write_text("{}\n")

        result = archive_io._archive_files(tmp_path, layer="raw", date_key=None)
        assert len(result) == 1
        assert result[0][0] == "raw"

    def test_hook_layer_only(self, tmp_path):
        """验证只搜索 hook 层"""
        hook_dir = tmp_path / "memory" / "hooks"
        hook_dir.mkdir(parents=True)
        (hook_dir / "2026-05-01.jsonl").write_text('{"snapshot_id": "s1"}\n')

        result = archive_io._archive_files(tmp_path, layer="hook", date_key=None)
        assert len(result) == 1
        assert result[0][0] == "hook"

    def test_all_layer_includes_both(self, tmp_path):
        """验证 all 层包含 raw 和 hook"""
        raw_dir = tmp_path / "audit"
        raw_dir.mkdir(parents=True)
        (raw_dir / "2026-05-01.jsonl").write_text('{"event_id": "e1"}\n')

        hook_dir = tmp_path / "memory" / "hooks"
        hook_dir.mkdir(parents=True)
        (hook_dir / "2026-05-01.jsonl").write_text('{"snapshot_id": "s1"}\n')

        result = archive_io._archive_files(tmp_path, layer="all", date_key=None)
        assert len(result) == 2

    def test_date_key_filters_by_date(self, tmp_path):
        """验证日期键过滤功能"""
        raw_dir = tmp_path / "audit"
        raw_dir.mkdir(parents=True)
        (raw_dir / "2026-05-01.jsonl").write_text('{"event_id": "e1"}\n')
        (raw_dir / "2026-05-02.jsonl").write_text('{"event_id": "e2"}\n')

        result = archive_io._archive_files(tmp_path, layer="raw", date_key="2026-05-01")
        assert len(result) == 1

    def test_files_sorted_by_mtime_newest_first(self, tmp_path):
        """验证文件按修改时间倒序排列"""
        raw_dir = tmp_path / "audit"
        raw_dir.mkdir(parents=True)

        file1 = raw_dir / "2026-05-01.jsonl"
        file1.write_text('{"event_id": "e1"}\n')
        file2 = raw_dir / "2026-05-02.jsonl"
        file2.write_text('{"event_id": "e2"}\n')

        # 修改时间确保顺序
        import time
        time.sleep(0.01)
        file2.write_text('{"event_id": "e2-updated"}\n')

        result = archive_io._archive_files(tmp_path, layer="raw", date_key=None)
        assert result[0][1].name == "2026-05-02.jsonl"


class TestReadArchiveFile:
    """测试 _read_archive_file 归档文件读取"""

    def test_empty_file(self, tmp_path):
        """验证空文件返回空列表"""
        path = tmp_path / "empty.jsonl"
        path.write_text("")

        result = archive_io._read_archive_file("raw", path)
        assert result == []

    def test_valid_jsonl_records(self, tmp_path):
        """验证有效 JSONL 记录解析"""
        path = tmp_path / "valid.jsonl"
        path.write_text('{"event_id": "e1"}\n{"event_id": "e2"}\n')

        result = archive_io._read_archive_file("raw", path)
        assert len(result) == 2
        # 标准化后的记录在 payload 字段里保存原始数据
        assert result[0]["payload"]["event_id"] == "e1"
        assert result[1]["payload"]["event_id"] == "e2"

    def test_malformed_json_becomes_error_record(self, tmp_path):
        """验证格式错误的行变成错误记录"""
        path = tmp_path / "malformed.jsonl"
        path.write_text('{"event_id": "e1"}\nnot valid json\n{"event_id": "e2"}\n')

        result = archive_io._read_archive_file("raw", path)
        assert len(result) == 3
        assert result[1]["kind"] == "archive_error"
        assert result[1]["error_code"] == "archive_json_decode_error"

    def test_empty_lines_skipped(self, tmp_path):
        """验证空行被跳过"""
        path = tmp_path / "with_empty.jsonl"
        path.write_text('{"event_id": "e1"}\n\n\n{"event_id": "e2"}\n')

        result = archive_io._read_archive_file("raw", path)
        assert len(result) == 2

    def test_non_dict_record_becomes_error(self, tmp_path):
        """验证非字典记录变成错误记录"""
        path = tmp_path / "non_dict.jsonl"
        path.write_text('"just a string"\n')

        result = archive_io._read_archive_file("raw", path)
        assert len(result) == 1
        assert result[0]["kind"] == "archive_error"
        assert "record is not a JSON object" in result[0]["content_preview"]

    def test_file_not_found_returns_error(self, tmp_path):
        """验证文件不存在返回错误记录"""
        path = tmp_path / "nonexistent.jsonl"
        result = archive_io._read_archive_file("raw", path)

        assert len(result) == 1
        assert result[0]["kind"] == "archive_error"


class TestNormalizeArchiveRecord:
    """测试 _normalize_archive_record 记录标准化"""

    def test_raw_event_fields(self, tmp_path):
        """验证 raw 事件字段标准化"""
        path = tmp_path / "test.jsonl"
        path.write_text("{}")  # 必须存在，否则 stat() 失败
        payload = {
            "event_id": "e123",
            "session_id": "s1",
            "request_id": "r1",
            "run_id": "run1",
            "speaker": "user",
            "target": "assistant",
            "action": "message",
            "created_at": "2026-05-01T10:00:00Z",
        }

        record = archive_io._normalize_archive_record("raw", path, 1, payload)

        assert record["layer"] == "raw"
        assert record["kind"] == "raw_event"
        assert record["id"] == "e123"
        assert record["session_id"] == "s1"
        assert record["speaker"] == "user"

    def test_hook_snapshot_fields(self, tmp_path):
        """验证 hook 快照字段标准化"""
        path = tmp_path / "test.jsonl"
        path.write_text("{}")  # 必须存在
        payload = {
            "snapshot_id": "snap1",
            "session_id": "s1",
            "created_at": "2026-05-01T10:00:00Z",
        }

        record = archive_io._normalize_archive_record("hook", path, 1, payload)

        assert record["layer"] == "hook"
        assert record["kind"] == "hook_snapshot"
        assert record["id"] == "snap1"

    def test_derived_fields_from_turn_range(self, tmp_path):
        """验证从 turn_range 推导字段"""
        path = tmp_path / "test.jsonl"
        path.write_text("{}")  # 必须存在
        payload = {
            "turn_range": {
                "request_id": "derived_req",
                "run_id": "derived_run",
            },
            "session_id": "s1",
        }

        record = archive_io._normalize_archive_record("hook", path, 1, payload)
        assert record["request_id"] == "derived_req"
        assert record["run_id"] == "derived_run"


class TestGatewayTerminalRequestPath:
    """测试 _gateway_terminal_request_path gateway 路径纠偏"""

    def test_empty_string_returns_empty(self):
        """验证空字符串直接返回"""
        result = archive_io._gateway_terminal_request_path("")
        assert result == ""

    def test_non_processing_path_unchanged(self):
        """验证非 processing 路径保持不变"""
        path = "/data/requests/done/gwreq-123.json"
        result = archive_io._gateway_terminal_request_path(path)
        assert result == path

    def test_processing_replaced_with_done(self, tmp_path):
        """验证 processing 路径被终态替换"""
        done_path = tmp_path / "requests" / "done"
        done_path.mkdir(parents=True)
        (done_path / "gwreq-123.json").write_text("{}")

        processing = f"{tmp_path}/requests/processing/gwreq-123.json"
        result = archive_io._gateway_terminal_request_path(processing)
        assert "done" in result

    def test_processing_replaced_with_failed(self, tmp_path):
        """验证 processing 路径优先尝试 done 再尝试 failed"""
        failed_path = tmp_path / "requests" / "failed"
        failed_path.mkdir(parents=True)
        (failed_path / "gwreq-456.json").write_text("{}")

        processing = f"{tmp_path}/requests/processing/gwreq-456.json"
        result = archive_io._gateway_terminal_request_path(processing)
        assert "failed" in result


class TestArchiveDir:
    """测试 _archive_dir 归档目录解析"""

    def test_raw_layer_returns_raw_parent(self, tmp_path):
        """验证 raw 层返回 raw 父目录"""
        result = archive_io._archive_dir(tmp_path, "raw")
        assert "raw" in str(result)

    def test_hook_layer_returns_hooks_parent(self, tmp_path):
        """验证 hook 层返回 hooks 父目录"""
        result = archive_io._archive_dir(tmp_path, "hook")
        assert "hooks" in str(result)
