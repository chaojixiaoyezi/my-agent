"""TS TUI launcher 测试（P4 入口集成）：检测/拉起逻辑。

唯一回退语义：node 不可用或产物缺失 → False（不阻塞 Python TUI/plain）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_py_agent.cli.chat_parts.tui_ts_launcher import (
    _tui_dist_entry,
    ts_tui_available,
)


def test_dist_entry_uses_env_override(tmp_path, monkeypatch) -> None:
    entry = tmp_path / "entry.js"
    entry.write_text("// tui", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_TUI_DIST", str(entry))
    assert _tui_dist_entry() == entry


def test_dist_entry_none_without_env_and_without_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MY_AGENT_TUI_DIST", raising=False)
    # 默认候选路径不存在 → None（不在测试目录里找）
    assert _tui_dist_entry() is None or _tui_dist_entry().is_file()


def test_ts_tui_available_false_without_node(monkeypatch) -> None:
    monkeypatch.setattr("agent_py_agent.cli.chat_parts.tui_ts_launcher.shutil.which", lambda _n: None)
    assert ts_tui_available() is False


def test_try_launch_ts_tui_false_without_node(monkeypatch, tmp_path) -> None:
    entry = tmp_path / "entry.js"
    entry.write_text("// tui", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_TUI_DIST", str(entry))
    monkeypatch.setattr("agent_py_agent.cli.chat_parts.tui_ts_launcher.shutil.which", lambda _n: None)
    assert (
        try_launch := __import__(
            "agent_py_agent.cli.chat_parts.tui_ts_launcher", fromlist=["try_launch_ts_tui"]
        ).try_launch_ts_tui(
            gateway_base_url="http://127.0.0.1:8420", session_id="s", model="m"
        )
    ) is False


def test_try_launch_ts_tui_false_on_non_tty(monkeypatch, tmp_path) -> None:
    entry = tmp_path / "entry.js"
    entry.write_text("// tui", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_TUI_DIST", str(entry))
    monkeypatch.setattr("agent_py_agent.cli.chat_parts.tui_ts_launcher.shutil.which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr("agent_py_agent.cli.chat_parts.tui_ts_launcher.sys.stdin.isatty", lambda: False)
    launcher = __import__(
        "agent_py_agent.cli.chat_parts.tui_ts_launcher", fromlist=["try_launch_ts_tui"]
    )
    assert launcher.try_launch_ts_tui(
        gateway_base_url="http://127.0.0.1:8420", session_id="s", model="m"
    ) is False
