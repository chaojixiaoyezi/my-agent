from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.tui_ansi_snapshot import AnsiReplayConfig, replay_ansi_session


def _write_replay_fixture(tmp_path: Path, raw: bytes, events: list[dict]) -> tuple[Path, Path]:
    raw_path = tmp_path / "fixture.raw.ansi"
    events_path = tmp_path / "fixture.events.jsonl"
    raw_path.write_bytes(raw)
    events_path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
    return raw_path, events_path


def test_replay_preserves_unicode_styles_background_and_checkpoint(tmp_path: Path) -> None:
    first = "\x1b[38;5;153m你\x1b[48;5;237m好  ".encode()
    second = b"\x1b[0m\r\nDONE"
    raw = first + second
    events = [
        {"type": "output", "at_ms": 10, "offset": 0, "length": len(first)},
        {
            "type": "action",
            "action": "checkpoint",
            "label": "styled",
            "at_ms": 20,
            "offset": len(first),
        },
        {"type": "output", "at_ms": 30, "offset": len(first), "length": len(second)},
        {"type": "process_exit", "at_ms": 40, "exit_code": 0},
    ]
    raw_path, events_path = _write_replay_fixture(tmp_path, raw, events)
    artifacts = replay_ansi_session(
        AnsiReplayConfig(raw_path, events_path, tmp_path / "out", "fixture", rows=4, cols=12)
    )
    styled, final = artifacts.payload["checkpoints"]
    assert styled["label"] == "styled"
    assert "你好" in styled["lines"][0]
    assert any(run["style"]["bg"] == "3a3a3a" for run in styled["style_runs"][0])
    assert any("DONE" in line for line in final["lines"])
    assert artifacts.json_path.stat().st_mode & 0o777 == 0o600
    assert artifacts.text_path.stat().st_mode & 0o777 == 0o600


def test_replay_applies_resize_at_exact_offset(tmp_path: Path) -> None:
    first = b"wide"
    second = b"\x1b[2;1Hnarrow"
    raw_path, events_path = _write_replay_fixture(
        tmp_path,
        first + second,
        [
            {"type": "output", "offset": 0, "length": len(first)},
            {
                "type": "action",
                "action": "resize",
                "offset": len(first),
                "rows": 3,
                "cols": 8,
            },
            {"type": "output", "offset": len(first), "length": len(second)},
        ],
    )
    payload = replay_ansi_session(
        AnsiReplayConfig(raw_path, events_path, tmp_path / "out", "resize", rows=4, cols=12)
    ).payload
    final = payload["checkpoints"][-1]
    assert (final["rows"], final["cols"]) == (3, 8)
    assert final["lines"][1].startswith("narrow")


def test_replay_rejects_raw_gap_and_duplicate_checkpoint(tmp_path: Path) -> None:
    raw_path, events_path = _write_replay_fixture(
        tmp_path,
        b"abc",
        [{"type": "output", "offset": 1, "length": 2}],
    )
    with pytest.raises(ValueError, match="invalid ANSI output span"):
        replay_ansi_session(
            AnsiReplayConfig(raw_path, events_path, tmp_path / "out", "gap", rows=2, cols=4)
        )

    events = [
        {"type": "output", "offset": 0, "length": 3},
        {"type": "action", "action": "checkpoint", "offset": 3, "label": "same"},
        {"type": "action", "action": "checkpoint", "offset": 3, "label": "same"},
    ]
    raw_path, events_path = _write_replay_fixture(tmp_path, b"abc", events)
    with pytest.raises(ValueError, match="duplicate ANSI checkpoint"):
        replay_ansi_session(
            AnsiReplayConfig(raw_path, events_path, tmp_path / "out", "duplicate", rows=2, cols=4)
        )
