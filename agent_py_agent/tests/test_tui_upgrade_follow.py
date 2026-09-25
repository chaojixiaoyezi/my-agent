"""TUI 随 Gateway 升级自动重启：只认结构化 runtime_prefix，空闲才重启，忙时只提示，开关可关，exec 失败不崩。"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from agent_py_agent.cli.chat_parts import tui_upgrade_follow as follow
from agent_py_agent.cli.gateway_process import _build_run_state
from agent_py_agent.tests.test_gateway_status_tool import _agent as _status_agent


def _runtime_dir(tmp_path: Path, name: str) -> Path:
    prefix = tmp_path / name
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "my-agent").write_text("#!/bin/sh\n", encoding="utf-8")
    return prefix


def _state(prefix: Path, status: str = "running") -> dict:
    return {"status": status, "pid": 4242, "runtime_prefix": str(prefix)}


def test_restart_target_requires_running_gateway_with_a_different_install(tmp_path):
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    assert follow.upgrade_restart_target(_state(own), str(own), enabled=True) == ""
    assert follow.upgrade_restart_target(_state(other, status="stopped"), str(own), enabled=True) == ""
    assert follow.upgrade_restart_target(_state(other), str(own), enabled=False) == ""
    assert follow.upgrade_restart_target(None, str(own), enabled=True) == ""
    expected = other / ("Scripts/my-agent.exe" if os.name == "nt" else "bin/my-agent")
    if os.name == "nt":
        expected.parent.mkdir(exist_ok=True)
        expected.write_text("", encoding="utf-8")
    assert follow.upgrade_restart_target(_state(other), str(own), enabled=True) == str(expected)
    missing = tmp_path / "runtime-c"
    missing.mkdir()
    assert follow.upgrade_restart_target(_state(missing), str(own), enabled=True) == "", "入口文件不存在就不重启"


def test_idle_needs_no_turn_no_queue_no_approval_empty_input_main_view():
    assert follow.tui_idle_for_restart(is_running=False, pending_jobs=0, input_text="", permission_active=False, navigation_depth=0)
    assert not follow.tui_idle_for_restart(is_running=True, pending_jobs=0, input_text="", permission_active=False, navigation_depth=0)
    assert not follow.tui_idle_for_restart(is_running=False, pending_jobs=1, input_text="", permission_active=False, navigation_depth=0)
    assert not follow.tui_idle_for_restart(is_running=False, pending_jobs=0, input_text="草稿", permission_active=False, navigation_depth=0)
    assert not follow.tui_idle_for_restart(is_running=False, pending_jobs=0, input_text="", permission_active=True, navigation_depth=0)
    assert not follow.tui_idle_for_restart(is_running=False, pending_jobs=0, input_text="", permission_active=False, navigation_depth=1)


def _context(tmp_path: Path, state: dict, own: Path, *, idle: bool, enabled: bool = True):
    state_path = tmp_path / "gateway_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    calls = {"exit": 0, "notices": []}
    ctx = follow.UpgradeFollowContext(
        state_path=state_path, own_prefix=str(own), enabled=enabled, stop_event=threading.Event(),
        restart_target_ref=[""], is_idle=lambda: idle,
        request_exit=lambda: calls.__setitem__("exit", calls["exit"] + 1),
        set_notice=lambda text, seconds, kind: calls["notices"].append((text, seconds, kind)),
        poll_seconds=0.01,
    )
    return ctx, calls


def test_idle_client_records_target_and_requests_exit_once(tmp_path):
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    ctx, calls = _context(tmp_path, _state(other), own, idle=True)
    outcome, _ = follow.check_upgrade_once(ctx, last_notice_at=-1e9)
    assert outcome == "restart" and calls["exit"] == 1 and calls["notices"] == []
    assert ctx.restart_target_ref[0] == str(other / "bin" / "my-agent")


def test_busy_client_only_gets_a_rate_limited_notice(tmp_path):
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    ctx, calls = _context(tmp_path, _state(other), own, idle=False)
    outcome, last = follow.check_upgrade_once(ctx, last_notice_at=-1e9)
    assert outcome == "notice" and calls["exit"] == 0 and ctx.restart_target_ref[0] == ""
    assert calls["notices"] == [(follow.NOTICE_TEXT, 6.0, follow.NOTICE_KIND)]
    outcome, _ = follow.check_upgrade_once(ctx, last_notice_at=last)
    assert outcome == "notice" and len(calls["notices"]) == 1, "30 秒内不重复提示"


def test_same_install_or_disabled_does_nothing(tmp_path):
    own = _runtime_dir(tmp_path, "runtime-a")
    ctx, calls = _context(tmp_path, _state(own), own, idle=True)
    assert follow.check_upgrade_once(ctx, last_notice_at=-1e9)[0] == ""
    other = _runtime_dir(tmp_path, "runtime-b")
    ctx, calls = _context(tmp_path, _state(other), own, idle=True, enabled=False)
    assert follow.check_upgrade_once(ctx, last_notice_at=-1e9)[0] == "" and calls["exit"] == 0


def test_watcher_thread_stops_after_requesting_restart(tmp_path):
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    ctx, calls = _context(tmp_path, _state(other), own, idle=True)
    thread = follow.start_upgrade_follow_watcher(ctx)
    thread.join(timeout=5)
    assert not thread.is_alive() and calls["exit"] == 1 and ctx.restart_target_ref[0]


def test_reexec_failure_prints_hint_and_returns(tmp_path, monkeypatch):
    lines: list[str] = []

    def boom(*_args):
        raise OSError("exec denied")

    monkeypatch.setattr(os, "execv", boom)
    code = follow.reexec_into_target(str(tmp_path / "missing" / "my-agent"), ["chat", "--gateway"], print_line=lines.append)
    assert code == 0 and any("自动重启失败" in line for line in lines)


def test_gateway_state_carries_its_runtime_prefix(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.models import GatewayRunContext, GatewayThreadsRequest

    agent = _status_agent(tmp_path)
    context = GatewayRunContext(agent=agent, paths=gateway_paths_from_root(tmp_path / "gateway"),
                                config_path=tmp_path / "config.yaml", log_start_offset_bytes=0)
    state = _build_run_state(GatewayThreadsRequest(context=context, requeued=0, failed=0, http_port=8420), os.getpid())
    assert state["runtime_prefix"] == sys.prefix
