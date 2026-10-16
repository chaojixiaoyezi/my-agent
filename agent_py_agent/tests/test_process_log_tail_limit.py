"""E13：stat 之后追加 200 MiB；文件仅位于 pytest 临时目录。"""
from __future__ import annotations

import json
import os
import tracemalloc
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling import process_registry


def test_growth_after_size_observation_has_hard_read_limit(tmp_path, monkeypatch):
    path = tmp_path / "growing.log"
    original_text = "head\n" + "tail\n" * 1200
    path.write_text(original_text)
    old_stat, old_fstat = Path.stat, os.fstat
    appended = False

    def append_once():
        nonlocal appended
        if appended:
            return
        appended = True
        with path.open("ab") as writer:
            block = b"x" * 65536
            for _ in range(3200):
                writer.write(block)

    def stat(self, *args, **kwargs):
        observed = old_stat(self, *args, **kwargs)
        if self == path:
            append_once()
        return observed

    def fstat(fd):
        observed = old_fstat(fd)
        if observed.st_ino == old_stat(path).st_ino:
            append_once()
        return observed

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(os, "fstat", fstat)
    tracemalloc.start()
    try:
        tail = process_registry._read_log_tail(str(path), 4000)
        text = tail.text
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(json.dumps({"tail_peak_bytes": peak, "tail_return_chars": len(text), "appended_bytes": 200 * 1024 * 1024}))
    assert appended
    assert tail.truncated and tail.bytes_read == 4000 and tail.output_bytes == len(original_text.encode())
    assert peak < 2 * 1024 * 1024, f"增长日志导致无界分配：{peak} bytes"
    assert len(text) < 4100 and "x" not in text
    assert text.startswith("...[日志已截断") and text.endswith("tail\n")


@pytest.mark.parametrize("content,limit", [("中文\n" * 20, 31), ("abc\n", 4000), ("中文", 4), ("abcdef", 3)])
def test_no_growth_preserves_hint_and_utf8_boundary(tmp_path, content, limit):
    path = tmp_path / "stable.log"
    path.write_text(content)
    data = content.encode()
    expected = data[-limit:].decode("utf-8", errors="replace")
    if len(data) > limit:
        if "\n" in expected:
            expected = expected[expected.find("\n") + 1:]
        expected = f"...[日志已截断,只显示尾部 {len(expected)} 字符]...\n{expected}"
    assert process_registry._read_log_tail(str(path), limit).text == expected


def test_tail_size_comes_from_open_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "replaced.log"
    path.write_text("expected\n")
    original = Path.stat

    def stat(self, *args, **kwargs):
        if self == path:
            pytest.fail("不能在打开前用路径取长度")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert process_registry._read_log_tail(str(path), 4000).text == "expected\n"


@pytest.mark.parametrize('data,invalid', [(b'a\xffb', True), ('\ufffd'.encode(), False)])
def test_tail_encoding_fact_comes_from_bytes(tmp_path, data, invalid):
    path = tmp_path / 'encoding.log'
    path.write_bytes(data)
    tail = process_registry._read_log_tail(str(path), 4000)
    assert tail.text == data.decode('utf-8', errors='replace')
    assert tail.invalid_utf8 is invalid
    assert tail.bytes_read == len(data) and not tail.truncated and tail.error_type == ''


def test_missing_tail_reports_error_without_changing_process_status(tmp_path):
    path = tmp_path / 'missing.log'
    record = process_registry.BackgroundProcess('bg-observe', 'synthetic', 123, 0, output_file=str(path), status='exited', exit_code=7)
    summary = record.to_summary(include_output=True)
    assert summary['status'] == 'exited' and summary['exit_code'] == 7
    assert summary['output_tail'] == '' and summary['output_bytes'] is None
    assert summary['output_tail_capture']['error_type'] == 'FileNotFoundError'
    assert summary['output_tail_capture']['bytes_read'] == 0


def test_short_read_after_observation_has_explicit_error(tmp_path, monkeypatch):
    path = tmp_path / 'shrinking.log'
    path.write_bytes(b'old-content')
    original = os.fstat

    def shrink(fd):
        observed = original(fd)
        if observed.st_ino == path.stat().st_ino:
            path.write_bytes(b'')
        return observed

    monkeypatch.setattr(os, 'fstat', shrink)
    tail = process_registry._read_log_tail(str(path), 4000)
    assert tail.output_bytes == len(b'old-content')
    assert tail.bytes_read == 0 and tail.truncated and tail.error_type == 'short_read'


def test_tail_reads_only_bytes_observed_before_growth(tmp_path, monkeypatch):
    path = tmp_path / 'small-growing.log'
    path.write_bytes(b'before')
    original = os.fstat

    def grow(fd):
        observed = original(fd)
        if observed.st_ino == path.stat().st_ino:
            with path.open('ab') as writer:
                writer.write(b'after' * 10)
        return observed

    monkeypatch.setattr(os, 'fstat', grow)
    tail = process_registry._read_log_tail(str(path), 4000)
    assert tail.text == 'before'
    assert tail.output_bytes == 6 and tail.bytes_read == 6 and not tail.truncated
