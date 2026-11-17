"""S10: every line the gateway daemon writes to gateway.log starts with a millisecond ISO-8601 timestamp."""
from __future__ import annotations

import io
import logging
import os
import re
import sys

from agent_py_agent.cli import gateway_log_stamp
from agent_py_agent.cli.gateway_log_stamp import (
    TimestampedLineWriter,
    install_gateway_log_timestamps,
)

_ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} "


def test_writer_stamps_each_line_start_once_and_keeps_partial_lines_together():
    sink = io.StringIO()
    writer = TimestampedLineWriter(sink)
    writer.write("[gateway-run] status=")
    writer.write("running pid=1\nsecond line\nthird ")
    writer.write("part\n")
    lines = sink.getvalue().splitlines()
    assert len(lines) == 3
    assert all(re.match(_ISO, line) for line in lines)
    assert re.fullmatch(_ISO + r"\[gateway-run\] status=running pid=1", lines[0])
    assert lines[2].endswith("third part") and lines[2].count("T") >= 1
    assert writer.write("") == 0


def test_install_is_a_no_op_outside_the_daemon(monkeypatch):
    original_out, original_err = sys.stdout, sys.stderr
    assert install_gateway_log_timestamps(None) is False
    assert install_gateway_log_timestamps("") is False
    assert sys.stdout is original_out and sys.stderr is original_err


def test_daemon_install_wraps_streams_once_and_logs_redacted_stamped_lines(monkeypatch):
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", sink)
    logger = logging.getLogger("agent_py_agent")
    before = list(logger.handlers)
    try:
        assert install_gateway_log_timestamps("/tmp/gateway.log") is True
        assert install_gateway_log_timestamps("/tmp/gateway.log") is True
        assert isinstance(sys.stderr, TimestampedLineWriter)
        assert not isinstance(sys.stderr._inner, TimestampedLineWriter)
        added = [h for h in logger.handlers if h not in before]
        assert len(added) == 1
        logging.getLogger("agent_py_agent.gateway_parts.example").warning(
            "provider call failed Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
        print("[gateway-owner-maintenance] {}", file=sys.stderr)
        lines = sink.getvalue().splitlines()
        assert len(lines) == 2
        assert re.match(_ISO + r"WARNING agent_py_agent\.gateway_parts\.example: provider call failed", lines[0])
        assert "abcdefghijklmnopqrstuvwxyz123456" not in lines[0]
        assert re.fullmatch(_ISO + r"\[gateway-owner-maintenance\] \{\}", lines[1])
    finally:
        for handler in list(logger.handlers):
            if handler not in before:
                logger.removeHandler(handler)


def test_writer_delegates_fileno_and_flush_to_the_original_stream(tmp_path):
    path = tmp_path / "log.txt"
    with open(path, "w", encoding="utf-8") as raw:
        writer = TimestampedLineWriter(raw)
        assert writer.fileno() == raw.fileno()
        writer.write("x\n")
        writer.flush()
        assert re.fullmatch(_ISO + "x\n", path.read_text(encoding="utf-8"))


def test_self_capture_writes_an_iso_header_before_the_thread_dump(tmp_path):
    from agent_py_agent.agent.gateway_parts import hang_diagnostics

    hang_diagnostics.capture_gateway_hang_facts(tmp_path, os.getpid(), send_sigusr1=False)
    text = (tmp_path / hang_diagnostics.GATEWAY_HANG_DUMP_FILENAME).read_text(encoding="utf-8")
    header = re.search(r"^--- (" + _ISO.strip() + r") thread dump pid=(\d+) stage=before_term ---$", text, re.M)
    assert header and int(header.group(2)) == os.getpid()
    assert text.index(header.group(0)) < text.index("Thread 0x") if "Thread 0x" in text else True
    assert gateway_log_stamp.iso_timestamp()[:4].isdigit()
