from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.tui_pty_recorder import (
    PtyAction,
    RecorderConfig,
    _redacted_argv,
    load_actions,
    record_pty_session,
)


def _child_script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pty_child.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_action_loader_validates_schema_order_and_key(tmp_path: Path) -> None:
    path = tmp_path / "actions.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {"at_ms": 10, "type": "write", "text": "你好"},
                    {"at_ms": 20, "type": "key", "key": "enter"},
                ],
            }
        ),
        encoding="utf-8",
    )
    actions = load_actions(path)
    assert [(item.at_ms, item.kind) for item in actions] == [(10, "write"), (20, "key")]
    path.write_text('[{"at_ms": 0, "type": "key", "key": "unknown"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="supported key"):
        load_actions(path)


def test_recorder_captures_raw_output_and_redacts_action_text(tmp_path: Path) -> None:
    child = _child_script(
        tmp_path,
        "import sys\nprint('READY', flush=True)\nline = sys.stdin.readline()\nprint('GOT:' + line.strip(), flush=True)\n",
    )
    actions = (
        PtyAction(50, "write", {"text": "fixture-secret-text"}, "prompt"),
        PtyAction(70, "key", {"key": "enter"}, "submit"),
    )
    result = record_pty_session(
        RecorderConfig(
            output_dir=tmp_path / "evidence",
            name="simple",
            command=(sys.executable, str(child)),
            actions=actions,
            timeout_ms=2_000,
        )
    )
    raw = result.raw_path.read_bytes()
    manifest_text = result.manifest_path.read_text(encoding="utf-8")
    events_text = result.events_path.read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert b"READY" in raw and b"GOT:fixture-secret-text" in raw
    assert "fixture-secret-text" not in manifest_text
    assert "fixture-secret-text" not in events_text
    manifest = json.loads(manifest_text)
    assert manifest["byte_count"] == len(raw)
    assert manifest["actions_executed"] == 2
    assert json.loads(events_text.splitlines()[-1])["type"] == "process_exit"
    assert result.raw_path.stat().st_mode & 0o777 == 0o600
    assert result.events_path.stat().st_mode & 0o777 == 0o600
    assert result.manifest_path.stat().st_mode & 0o777 == 0o600


def test_recorder_applies_resize_before_input(tmp_path: Path) -> None:
    child = _child_script(
        tmp_path,
        "import struct, sys, termios, fcntl\n"
        "sys.stdin.readline()\n"
        "rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\0' * 8))\n"
        "print(f'SIZE:{rows}x{cols}', flush=True)\n",
    )
    actions = (
        PtyAction(30, "resize", {"rows": 24, "cols": 80}, "narrow"),
        PtyAction(40, "checkpoint", {}, "before-input"),
        PtyAction(50, "write", {"text": "go"}),
        PtyAction(60, "key", {"key": "enter"}),
    )
    result = record_pty_session(
        RecorderConfig(
            output_dir=tmp_path / "evidence",
            name="resize",
            command=(sys.executable, str(child)),
            actions=actions,
            timeout_ms=2_000,
        )
    )
    assert b"SIZE:24x80" in result.raw_path.read_bytes()
    event_rows = [json.loads(line) for line in result.events_path.read_text().splitlines()]
    assert any(item.get("action") == "resize" and item.get("cols") == 80 for item in event_rows)
    assert any(item.get("action") == "checkpoint" and "offset" in item for item in event_rows)


def test_timeout_terminates_only_recorded_process_group(tmp_path: Path) -> None:
    child = _child_script(tmp_path, "import time\nprint('WAIT', flush=True)\ntime.sleep(30)\n")
    result = record_pty_session(
        RecorderConfig(
            output_dir=tmp_path / "evidence",
            name="timeout",
            command=(sys.executable, str(child)),
            timeout_ms=100,
            terminate_grace_ms=100,
        )
    )
    assert result.timed_out is True
    assert json.loads(result.manifest_path.read_text())["timed_out"] is True


def test_argv_redaction_covers_flags_assignments_and_token_prefixes() -> None:
    redacted = _redacted_argv(
        (
            "tool",
            "--api-key",
            "secret-value",
            "PASSWORD=top-secret",
            "sk-123456789012345678901234",
            "--safe",
            "visible",
        )
    )
    assert redacted == [
        "tool",
        "--api-key",
        "<redacted>",
        "PASSWORD=<redacted>",
        "<redacted>",
        "--safe",
        "visible",
    ]
