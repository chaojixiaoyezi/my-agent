"""TUI 随 Gateway 升级原地切换：只认结构化 runtime_prefix，空闲才在 UI 线程 exec，会话与原始终端设置跨 exec 传递，失败保留旧界面。"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.cli.chat_parts import tui_upgrade_follow as follow
from agent_py_agent.cli.gateway_process import _build_run_state
from agent_py_agent.tests.test_gateway_status_tool import _agent as _status_agent

IDLE = follow.TuiIdleFacts(is_running=False, pending_jobs=0, input_text="", permission_active=False, navigation_depth=0)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(follow, "_STATE", follow.HandoffState())
    monkeypatch.delenv(follow.HANDOFF_ENV, raising=False)
    registered: list = []
    monkeypatch.setattr(follow.atexit, "register", registered.append)
    yield registered


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
    if os.name != "nt":
        assert follow.upgrade_restart_target(_state(other), str(own), enabled=True) == str(other / "bin" / "my-agent")
    missing = tmp_path / "runtime-c"
    missing.mkdir()
    assert follow.upgrade_restart_target(_state(missing), str(own), enabled=True) == "", "入口文件不存在就不切换"


@pytest.mark.parametrize("change", [
    {"is_running": True}, {"pending_jobs": 1}, {"input_text": "草稿"}, {"permission_active": True},
    {"navigation_depth": 1}, {"following": False}, {"input_focused": False},
])
def test_idle_requires_every_structured_fact(change):
    assert follow.tui_idle_for_restart(IDLE)
    assert not follow.tui_idle_for_restart(follow.TuiIdleFacts(**{**IDLE.__dict__, **change}))


def test_tty_attributes_round_trip_through_json():
    attrs = [1, 2, 3, 4, 38400, 38400, [b"\x03", b"\x1c", 1, 0, b"\x7f"]]
    encoded = follow.encode_tty(attrs)
    assert follow.decode_tty(json.loads(json.dumps(encoded))) == attrs
    assert follow.decode_tty(["x"]) is None and follow.decode_tty(None) is None and follow.encode_tty(None) is None


def _context(tmp_path: Path, state: dict, own: Path, *, idle: bool, enabled: bool = True):
    state_path = tmp_path / "gateway_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    calls = {"handoff": [], "notices": []}
    ctx = follow.UpgradeFollowContext(
        state_path=state_path, own_prefix=str(own), enabled=enabled, stop_event=threading.Event(),
        is_idle=lambda: idle, set_notice=lambda text, seconds, kind: calls["notices"].append(text),
        request_handoff=calls["handoff"].append, poll_seconds=0.01,
    )
    return ctx, calls


def test_idle_client_requests_one_handoff_and_waits_while_pending(tmp_path):
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    ctx, calls = _context(tmp_path, _state(other), own, idle=True)
    assert follow.check_upgrade_once(ctx, last_notice_at=-1e9)[0] == "handoff"
    assert follow.check_upgrade_once(ctx, last_notice_at=-1e9)[0] == ""
    assert calls["handoff"] == [str(other / "bin" / "my-agent")] and ctx.pending and calls["notices"] == []


def test_busy_client_gets_a_rate_limited_notice_and_no_handoff(tmp_path):
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    ctx, calls = _context(tmp_path, _state(other), own, idle=False)
    outcome, last = follow.check_upgrade_once(ctx, last_notice_at=-1e9)
    assert outcome == "notice" and calls["handoff"] == [] and calls["notices"] == [follow.BUSY_NOTICE_TEXT]
    follow.check_upgrade_once(ctx, last_notice_at=last)
    assert len(calls["notices"]) == 1, "30 秒内不重复提示"


def test_platform_without_in_place_exec_only_asks_to_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(follow, "handoff_supported", lambda: False)
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    ctx, calls = _context(tmp_path, _state(other), own, idle=True)
    assert follow.check_upgrade_once(ctx, last_notice_at=-1e9)[0] == "notice"
    assert calls["handoff"] == [] and calls["notices"] == [follow.REOPEN_NOTICE_TEXT]


def test_same_install_disabled_or_failed_does_nothing(tmp_path):
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    for state, enabled in ((_state(own), True), (_state(other), False)):
        ctx, calls = _context(tmp_path, state, own, idle=True, enabled=enabled)
        assert follow.check_upgrade_once(ctx, last_notice_at=-1e9)[0] == "" and calls["handoff"] == []
    ctx, calls = _context(tmp_path, _state(other), own, idle=True)
    ctx.failed = True
    assert follow.check_upgrade_once(ctx, last_notice_at=-1e9)[0] == "" and calls["handoff"] == []


class _Loop:
    def __init__(self):
        self.soon, self.later = [], []

    def call_soon_threadsafe(self, callback):
        self.soon.append(callback)

    def call_later(self, delay, callback):
        self.later.append((delay, callback))


class _App:
    def __init__(self):
        self.loop, self.is_running, self.invalidations = _Loop(), True, 0

    def invalidate(self):
        self.invalidations += 1


def _scheduler(idle_values: list[bool]):
    app, notices, cleared = _App(), [], []
    hooks = follow.HandoffHooks(
        app=app, is_idle=lambda: idle_values.pop(0), stop_event=threading.Event(),
        set_notice=lambda text, seconds, kind: notices.append(text), clear_notice=cleared.append,
        session_id="sess-1", argv_tail=["chat", "--gateway"],
    )
    ctx = follow.UpgradeFollowContext(state_path=Path("unused"), own_prefix="", enabled=True,
                                      stop_event=hooks.stop_event, is_idle=hooks.is_idle, set_notice=hooks.set_notice)
    ctx.pending = True
    return follow.HandoffScheduler(hooks, ctx), app, ctx, notices, cleared


def test_scheduler_shows_switching_then_execs_on_the_ui_loop(monkeypatch):
    execs: list = []
    monkeypatch.setattr(follow, "exec_handoff",
                        lambda target, argv, session: execs.append((target, argv, session)) or "OSError")
    scheduler, app, ctx, notices, _cleared = _scheduler([True, True])
    scheduler.request("/rt-b/bin/my-agent")
    assert app.loop.soon and not execs, "只排到 UI 线程，不在守护线程里 exec"
    app.loop.soon.pop()()
    assert notices == [follow.SWITCHING_NOTICE_TEXT] and app.loop.later and not execs
    app.loop.later.pop()[1]()
    assert execs == [("/rt-b/bin/my-agent", ["chat", "--gateway"], "sess-1")]
    assert ctx.failed and not ctx.pending and notices[-1] == follow.FAILED_NOTICE_TEXT, "exec 失败保留旧界面并提示"


def test_scheduler_backs_off_when_user_becomes_busy(monkeypatch):
    monkeypatch.setattr(follow, "exec_handoff", lambda *args: pytest.fail("忙时不能 exec"))
    scheduler, app, ctx, _notices, cleared = _scheduler([True, False])
    scheduler.request("/rt-b/bin/my-agent")
    app.loop.soon.pop()()
    app.loop.later.pop()[1]()
    assert cleared == [follow.NOTICE_KIND] and not ctx.pending and not ctx.failed
    scheduler, app, ctx, _notices, _cleared = _scheduler([False])
    scheduler.request("/rt-b/bin/my-agent")
    app.loop.soon.pop()()
    assert not app.loop.later and not ctx.pending


def test_exec_handoff_passes_session_and_original_tty_then_cleans_env_on_failure(monkeypatch):
    follow._STATE.original_tty = [1, 2, 3, 4, 5, 6, [b"\x03", 1]]
    seen = {}

    def fake_execv(path, argv):
        seen["path"], seen["argv"] = path, argv
        seen["payload"] = json.loads(os.environ[follow.HANDOFF_ENV])
        raise OSError("exec denied")

    monkeypatch.setattr(follow.os, "execv", fake_execv)
    assert follow.exec_handoff("/rt-b/bin/my-agent", ["chat"], "sess-9") == "OSError"
    assert seen["path"] == "/rt-b/bin/my-agent" and seen["argv"] == ["/rt-b/bin/my-agent", "chat"]
    assert seen["payload"]["session_id"] == "sess-9"
    assert follow.decode_tty(seen["payload"]["tty"]) == [1, 2, 3, 4, 5, 6, [b"\x03", 1]]
    assert follow.HANDOFF_ENV not in os.environ


def test_adopt_handoff_takes_session_quiets_output_and_restores_terminal_on_early_exit(monkeypatch, _fresh_state):
    registered = _fresh_state
    payload = {"schema": "my_agent.tui_handoff.v1", "session_id": "sess-7", "tty": None}
    monkeypatch.setenv(follow.HANDOFF_ENV, json.dumps(payload))
    real_out, real_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", real_out)
    monkeypatch.setattr(sys, "stderr", real_err)
    state = follow.adopt_handoff()
    assert state.child and state.session_id == "sess-7" and follow.HANDOFF_ENV not in os.environ
    assert registered == [follow._restore_terminal_at_exit]
    print("会话打不开", file=sys.stderr)
    assert real_err.getvalue() == "", "旧画面还在屏幕上，启动期间的输出先暂存"
    follow._restore_terminal_at_exit()
    assert sys.stdout is real_out and follow._TERMINAL_RESET in real_out.getvalue()
    assert "会话打不开" in real_err.getvalue(), "终端复位之后才补打暂存的错误"


def test_released_streams_and_normal_exit_do_not_reset_again(monkeypatch):
    payload = {"schema": "my_agent.tui_handoff.v1", "session_id": "sess-8", "tty": None}
    monkeypatch.setenv(follow.HANDOFF_ENV, json.dumps(payload))
    real_out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", real_out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    follow.adopt_handoff()
    print("已恢复会话: sess-8")
    follow.release_quiet_streams()
    assert sys.stdout is real_out and real_out.getvalue() == ""
    follow.terminal_released()
    follow._restore_terminal_at_exit()
    assert real_out.getvalue() == "", "prompt_toolkit 已正常交还终端，不再补发复位序列"


def test_invalid_or_foreign_payload_is_ignored(monkeypatch):
    for raw in ("not json", json.dumps({"schema": "other", "session_id": "x"}),
                json.dumps({"schema": "my_agent.tui_handoff.v1", "session_id": "../etc"})):
        monkeypatch.setenv(follow.HANDOFF_ENV, raw)
        monkeypatch.setattr(follow, "_STATE", follow.HandoffState())
        assert follow.adopt_handoff().child is False


def test_gateway_state_carries_its_runtime_prefix(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.models import GatewayRunContext, GatewayThreadsRequest

    agent = _status_agent(tmp_path)
    context = GatewayRunContext(agent=agent, paths=gateway_paths_from_root(tmp_path / "gateway"),
                                config_path=tmp_path / "config.yaml", log_start_offset_bytes=0)
    state = _build_run_state(GatewayThreadsRequest(context=context, requeued=0, failed=0, http_port=8420), os.getpid())
    assert state["runtime_prefix"] == sys.prefix


def test_child_whose_install_did_not_change_never_switches_again(tmp_path, monkeypatch):
    """入口指向了别的解释器时，exec 后安装没变：本进程只提示重开，不再原地切换，避免循环 exec。"""
    payload = {"schema": "my_agent.tui_handoff.v1", "session_id": "sess-loop", "tty": None, "from_prefix": sys.prefix}
    monkeypatch.setenv(follow.HANDOFF_ENV, json.dumps(payload))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert follow.adopt_handoff().follow_disabled is True
    follow.release_quiet_streams()
    own, other = _runtime_dir(tmp_path, "runtime-a"), _runtime_dir(tmp_path, "runtime-b")
    (tmp_path / "gateway_state.json").write_text(json.dumps(_state(other)), encoding="utf-8")
    notices, stop = [], threading.Event()
    hooks = follow.HandoffHooks(app=_App(), is_idle=lambda: True, stop_event=stop,
                                set_notice=lambda text, seconds, kind: notices.append(text), clear_notice=lambda kind: None,
                                session_id="sess-loop")
    monkeypatch.setattr(follow, "POLL_SECONDS", 0.01)
    thread = follow.start_tui_upgrade_follow(state_path=tmp_path / "gateway_state.json", own_prefix=str(own),
                                             enabled=True, hooks=hooks)
    deadline = time.monotonic() + 5
    while not notices and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    assert notices and notices[0] == follow.REOPEN_NOTICE_TEXT and not hooks.app.loop.soon


def test_exec_payload_records_the_install_it_came_from(monkeypatch):
    seen = {}

    def fake_execv(path, argv):
        seen["payload"] = json.loads(os.environ[follow.HANDOFF_ENV])
        raise OSError("exec denied")

    monkeypatch.setattr(follow.os, "execv", fake_execv)
    follow.exec_handoff("/rt-b/bin/my-agent", [], "sess-1")
    assert seen["payload"]["from_prefix"] == sys.prefix
