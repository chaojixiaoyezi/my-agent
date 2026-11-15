"""Verify PD-5b three-state settled background reclaim, CLI flags, and TTL config."""

from __future__ import annotations

import errno
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

import agent_py_agent.agent.gateway_parts.background_resource_report as bg_resource_report
import agent_py_agent.agent.tooling.background_process_launch as bg_launch
import agent_py_agent.agent.tooling.pty_sessions as pty_sessions_mod
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.background_resource_report import (
    background_store_root,
    freeze_settled_leftover_processes,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.parameter_metadata import field_readers, ranges, unit_for_key
from agent_py_agent.agent.settings.parameter_registry import (
    applied_value,
    applied_value_with,
    parameter_registry,
)
from agent_py_agent.agent.settings.services._normalize import TimeoutFieldsService
from agent_py_agent.agent.settings.user_config_capability import (
    EFFECT_GATEWAY_RESTART,
    TUNABLE_KEYS,
)
from agent_py_agent.agent.tooling.process_session_records import (
    ACTIVATION_PROCESS_SESSION_SCHEMA,
    PROCESS_SESSION_SCHEMA,
    PROCESS_TERMINAL_STATUSES,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.tooling.pty_sessions import PtySessionRegistry
from agent_py_agent.agent.tooling.sandbox import SandboxUnavailable
from agent_py_agent.cli.gateway_loops import _start_owner_maintenance_loop
from agent_py_agent.cli.gateway_process import (
    _attach_cold_start_settled_intent,
    _run_standalone_gateway,
    cmd_gateway_stop,
    run_cold_start_settled_sweep_step,
)
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.cli.parser import build_parser


def _spawn_sleeper() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )


def _write_running_session(
    store: ProcessSessionStore,
    *,
    owner_home: Path,
    session_id: str,
    thread_id: str,
    root_task_id: str,
    run_id: str,
    attempt_id: str = "att-1",
    started_at: float,
    pid: int,
    birth_token: str,
    child_pid: int,
    child_birth: str,
    completion_target: dict[str, str] | None = None,
    raw_execution_scope: dict[str, str] | None = None,
) -> dict[str, object]:
    now_ts = started_at
    session_doc: dict[str, object] = {
        "schema": PROCESS_SESSION_SCHEMA,
        "session_id": session_id,
        "command": "sleep 60",
        "cwd": str(owner_home),
        "output_file": str(owner_home / f"{session_id}.log"),
        "host_state_file": "",
        "completion_notice_id": "",
        "launcher_pid": pid,
        "launcher_birth_token": birth_token,
        "pid": pid,
        "pid_birth_token": birth_token,
        "child_pid": child_pid,
        "child_pid_birth_token": child_birth,
        "child_launch_started": True,
        "handoff_confirmed": True,
        "status": "running",
        "stop_requested": False,
        "retain_until_consumed": True,
        "revision": 0,
        "reserved_at": now_ts,
        "started_at": now_ts,
        "finished_at": None,
        "exit_code": None,
        "termination": {},
        "completion_target": completion_target or {},
        "activation_scope": None,
        "access_scope": {
            "owner_id": "owner-test",
            "conversation_id": thread_id or "t-fallback",
            "owner_home": str(owner_home),
        },
        "execution_scope": raw_execution_scope or {
            "owner_home": str(owner_home),
            "thread_id": thread_id,
            "root_task_id": root_task_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
        },
    }
    return store.write(session_doc)


def _write_activation_session(store: ProcessSessionStore, *, owner_home: Path, session_id: str, started_at: float) -> None:
    session_doc: dict[str, object] = {
        "schema": ACTIVATION_PROCESS_SESSION_SCHEMA,
        "session_id": session_id,
        "command": "plugin-host",
        "cwd": str(owner_home),
        "output_file": str(owner_home / f"{session_id}.log"),
        "host_state_file": "",
        "completion_notice_id": "",
        "launcher_pid": 999991,
        "launcher_birth_token": "act-host-birth",
        "pid": 999991,
        "pid_birth_token": "act-host-birth",
        "child_pid": 999992,
        "child_pid_birth_token": "act-child-birth",
        "child_launch_started": True,
        "handoff_confirmed": True,
        "status": "running",
        "stop_requested": False,
        "revision": 0,
        "reserved_at": started_at,
        "started_at": started_at,
        "finished_at": None,
        "exit_code": None,
        "termination": {},
        "completion_target": {},
        "access_scope": {
            "owner_id": "owner-test",
            "conversation_id": "",
            "owner_home": str(owner_home),
        },
        "execution_scope": {
            "owner_home": str(owner_home),
            "thread_id": "",
            "root_task_id": "",
            "run_id": "",
            "attempt_id": "",
        },
        "activation_scope": {
            "owner_id": "owner-test",
            "owner_home": str(owner_home),
            "plugin_id": "plugin.demo",
            "activation_id": "a" * 64,
        },
    }
    store.write(session_doc)


def _bind_task_link(store: ConversationStore, *, task_id: str, status: str) -> str:
    thread = store.threads.create(title=task_id)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": task_id})
    if status != "active":
        store.tasks.update_status({"task_id": task_id, "status": status, "expected_status": "active"})
    return thread.thread_id


def _insert_runtime_run(
    db: Path,
    *,
    task_run_id: str,
    task_id: str,
    task_status: str,
    agent_run_id: str,
    run_id: str,
    agent_status: str,
    attempt_id: str,
    attempt_status: str,
    parent_agent_run_id: str = "",
    lock_expires_at: float | None = None,
    ts: float | None = None,
    closed_at: float | None = None,
    ended_at: float | None = None,
) -> None:
    base_ts = float(time.time() if ts is None else ts)
    effective_closed_at = (base_ts if closed_at is None else float(closed_at)) if task_status in {"done", "failed", "cancelled"} else 0.0
    effective_ended_at = (base_ts if ended_at is None else float(ended_at)) if attempt_status != "running" else 0.0
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO task_runs(task_run_id, task_id, status, created_at, updated_at, closed_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (task_run_id, task_id, task_status, base_ts, base_ts, effective_closed_at),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agent_runs(agent_run_id, task_run_id, parent_agent_run_id, run_id, role, status, "
            "current_attempt_id, current_attempt_generation, workspace_epoch, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_run_id, task_run_id, parent_agent_run_id, run_id, "main", agent_status, attempt_id, 1, 1, base_ts, base_ts),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, started_at, ended_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (attempt_id, agent_run_id, 1, attempt_status, base_ts, effective_ended_at),
        )
        if lock_expires_at is not None:
            conn.execute(
                "INSERT OR REPLACE INTO resource_locks(lock_id, canonical_scope, holder_instance, pid, start_token, "
                "attempt_id, attempt_generation, workspace_epoch, tool_operation_generation, lease_expires_at, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"lock-{attempt_id}",
                    f"agent_run:{agent_run_id}",
                    "holder-1",
                    1234,
                    "tok",
                    attempt_id,
                    1,
                    1,
                    0,
                    lock_expires_at,
                    base_ts,
                    base_ts,
                ),
            )
        conn.commit()


def test_freeze_settled_leftover_processes_enforces_three_state_active_unknown_settled(tmp_path: Path) -> None:
    owner_home = (tmp_path / "owners" / "local" / "main").resolve()
    conv_root = owner_home / "conversations"
    conv_root.mkdir(parents=True)
    conv_store = ConversationStore(conv_root)
    db = runtime_db_path(owner_home)
    RuntimeRepository(db)

    now = 2_000_000.0
    store_root = process_session_store_root(owner_home, owner_home)
    store = ProcessSessionStore(store_root)
    procs: list[subprocess.Popen[str]] = []

    try:
        active_proc = _spawn_sleeper()
        procs.append(active_proc)
        settled_proc = _spawn_sleeper()
        procs.append(settled_proc)
        dead_proc = _spawn_sleeper()
        dead_pid = dead_proc.pid
        dead_birth = bg_launch.capture_process_birth_token(dead_pid) or "dead-birth"
        dead_proc.terminate()
        dead_proc.wait(timeout=5)

        active_birth = bg_launch.capture_process_birth_token(active_proc.pid) or "active-birth"
        settled_birth = bg_launch.capture_process_birth_token(settled_proc.pid) or "settled-birth"

        # 1. Active conversation task -> preserved
        t_active = _bind_task_link(conv_store, task_id="task-active", status="active")
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-active-task",
            thread_id=t_active,
            root_task_id="task-active",
            run_id="run-active",
            started_at=now - 1000,
            pid=active_proc.pid,
            birth_token=active_birth,
            child_pid=active_proc.pid,
            child_birth=active_birth,
        )

        # 2. Interrupted conversation task (/interrupt retains for /continue) -> active, preserved
        t_int = _bind_task_link(conv_store, task_id="task-int", status="interrupted")
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-interrupted-task",
            thread_id=t_int,
            root_task_id="task-int",
            run_id="run-int",
            started_at=now - 1000,
            pid=dead_pid,
            birth_token=dead_birth,
            child_pid=dead_pid,
            child_birth=dead_birth,
        )

        # 3. Parent run done, child subagent still running in runtime.db -> active, preserved
        t_child = _bind_task_link(conv_store, task_id="task-child", status="completed")
        _insert_runtime_run(
            db,
            task_run_id="tr-child",
            task_id="task-child",
            task_status="done",
            agent_run_id="ar-parent",
            run_id="run-parent",
            agent_status="done",
            attempt_id="att-parent",
            attempt_status="done",
            ts=now - 1000,
        )
        _insert_runtime_run(
            db,
            task_run_id="tr-child",
            task_id="task-child",
            task_status="done",
            agent_run_id="ar-sub",
            run_id="run-sub",
            agent_status="running",
            attempt_id="att-sub",
            attempt_status="running",
            parent_agent_run_id="ar-parent",
            ts=now - 1000,
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-active-child",
            thread_id=t_child,
            root_task_id="task-child",
            run_id="run-parent",
            started_at=now - 1000,
            pid=dead_pid,
            birth_token=dead_birth,
            child_pid=dead_pid,
            child_birth=dead_birth,
        )

        # 4. Scheduler active run -> active, preserved
        t_sched = _bind_task_link(conv_store, task_id="task-sched", status="completed")
        sched_path = owner_home / "data" / "scheduler" / "store.json"
        sched_path.parent.mkdir(parents=True, exist_ok=True)
        sched_path.write_text(
            json.dumps({"runs": {"srun_1": {"run_id": "run-sched", "task_id": "task-sched", "status": "running"}}}),
            encoding="utf-8",
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-active-sched",
            thread_id=t_sched,
            root_task_id="task-sched",
            run_id="run-sched",
            started_at=now - 1000,
            pid=dead_pid,
            birth_token=dead_birth,
            child_pid=dead_pid,
            child_birth=dead_birth,
        )

        # 5. Unsettled workflow run referencing task -> active, preserved
        t_wf = _bind_task_link(conv_store, task_id="task-wf", status="completed")
        wf_steps = owner_home / "data" / "workflow_runs" / "wf-active" / "steps"
        wf_steps.mkdir(parents=True, exist_ok=True)
        (wf_steps.parent / "run.json").write_text(
            json.dumps({"workflow_run_id": "wf-active", "status": "running"}),
            encoding="utf-8",
        )
        (wf_steps / "step-1.json").write_text(
            json.dumps({"escalation_result": {"task_id": "task-wf", "run_id": "run-wf"}}),
            encoding="utf-8",
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-active-wf",
            thread_id=t_wf,
            root_task_id="task-wf",
            run_id="run-wf",
            started_at=now - 1000,
            pid=dead_pid,
            birth_token=dead_birth,
            child_pid=dead_pid,
            child_birth=dead_birth,
        )

        # 6. Unknown: no settlement record in any authority (root_state_unknown)
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-unknown-missing-root",
            thread_id="t-missing",
            root_task_id="task-missing",
            run_id="run-missing",
            started_at=now - 1000,
            pid=dead_pid,
            birth_token=dead_birth,
            child_pid=dead_pid,
            child_birth=dead_birth,
        )

        # 7. Unknown: incomplete scope (missing run_id)
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-unknown-incomplete",
            thread_id="t-inc",
            root_task_id="task-inc",
            run_id="",
            started_at=now - 1000,
            pid=dead_pid,
            birth_token=dead_birth,
            child_pid=dead_pid,
            child_birth=dead_birth,
            raw_execution_scope={
                "owner_home": str(owner_home),
                "thread_id": "t-inc",
                "root_task_id": "task-inc",
                "run_id": "",
                "attempt_id": "",
            },
        )

        # 8. Unknown: attempt status unknown in runtime.db
        t_att_unk = _bind_task_link(conv_store, task_id="task-att-unk", status="completed")
        _insert_runtime_run(
            db,
            task_run_id="tr-att-unk",
            task_id="task-att-unk",
            task_status="done",
            agent_run_id="ar-att-unk",
            run_id="run-att-unk",
            agent_status="done",
            attempt_id="att-unk",
            attempt_status="unknown",
            ts=now - 1000,
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-unknown-attempt",
            thread_id=t_att_unk,
            root_task_id="task-att-unk",
            run_id="run-att-unk",
            started_at=now - 1000,
            pid=dead_pid,
            birth_token=dead_birth,
            child_pid=dead_pid,
            child_birth=dead_birth,
        )

        # 9. Unknown: plugin activation schema (non-execution)
        _write_activation_session(store, owner_home=owner_home, session_id="bg-unknown-activation", started_at=now - 1000)

        # 10. Unknown: corrupt record JSON on disk (must NOT abort stopping settled records)
        (store_root / "bg-corrupt.json").write_text("{corrupt", encoding="utf-8")

        # 11. Settled: old enough (>= 300s) -> STOPPED cleanly (even live OS child!)
        # 12. Settled sibling in SAME scope but younger (< 300s) -> DEFERRED untouched!
        t_settled = _bind_task_link(conv_store, task_id="task-settled", status="completed")
        _insert_runtime_run(
            db,
            task_run_id="tr-settled",
            task_id="task-settled",
            task_status="done",
            agent_run_id="ar-settled",
            run_id="run-settled",
            agent_status="done",
            attempt_id="att-settled",
            attempt_status="done",
            ts=now - 600,
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-settled-old",
            thread_id=t_settled,
            root_task_id="task-settled",
            run_id="run-settled",
            started_at=now - 600,
            pid=settled_proc.pid,
            birth_token=settled_birth,
            child_pid=settled_proc.pid,
            child_birth=settled_birth,
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-settled-young",
            thread_id=t_settled,
            root_task_id="task-settled",
            run_id="run-settled",
            started_at=now - 30,
            pid=dead_pid,
            birth_token=dead_birth,
            child_pid=dead_pid,
            child_birth=dead_birth,
        )

        report = freeze_settled_leftover_processes(owner_home, min_settled_age_seconds=300, now_ts=now)

        active_ids = {row["session_id"] for row in report["active_processes"]}
        unknown_ids = {row["session_id"] for row in report["unknown_processes"]}
        stopped_ids = {row["session_id"] for row in report["stopped_processes"] if row.get("stopped")}
        deferred_ids = {row["session_id"] for row in report["deferred_settled_processes"]}

        assert active_ids == {
            "bg-active-task",
            "bg-interrupted-task",
            "bg-active-child",
            "bg-active-sched",
            "bg-active-wf",
        }
        assert unknown_ids == {
            "bg-unknown-missing-root",
            "bg-unknown-incomplete",
            "bg-unknown-attempt",
            "bg-unknown-activation",
            "bg-corrupt",
        }
        assert stopped_ids == {"bg-settled-old"}
        assert deferred_ids == {"bg-settled-young"}
        assert report["counts"] == {
            "active": 5,
            "settled": 2,
            "stopped": 1,
            "deferred_settled": 1,
            "stop_failed": 0,
            "unknown": 5,
        }

        # Verify actual OS process + session store states
        settled_proc.wait(timeout=5)
        assert active_proc.poll() is None
        assert store.load("bg-settled-old").record["status"] in PROCESS_TERMINAL_STATUSES
        assert store.load("bg-settled-young").record["status"] == "running"
        for kept_id in (
            "bg-active-task",
            "bg-interrupted-task",
            "bg-active-child",
            "bg-active-sched",
            "bg-active-wf",
            "bg-unknown-missing-root",
            "bg-unknown-incomplete",
            "bg-unknown-attempt",
            "bg-unknown-activation",
        ):
            assert store.load(kept_id).record["status"] == "running"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_gateway_stop_settled_only_stops_settled_preserves_active_and_lists_unknown(
    tmp_path: Path, capsys,
) -> None:
    parser = build_parser()
    parsed = parser.parse_args(["gateway", "stop", "--stop-background", "--settled-only"])
    assert parsed.stop_background is True
    assert parsed.settled_only is True

    # --settled-only without --stop-background must fail BEFORE stopping Gateway
    solo_args = parser.parse_args(["gateway", "stop", "--settled-only"])
    with patch("agent_py_agent.cli.gateway_process.make_lightweight_cli_agent_context") as mock_make:
        code = cmd_gateway_stop(solo_args)
    assert code == 2
    mock_make.assert_not_called()
    captured = capsys.readouterr()
    assert "my-agent gateway stop --stop-background --settled-only" in captured.err

    # --stop-background --settled-only stops only settled, keeps active and unknown
    owner_home = (tmp_path / "owners" / "local" / "main").resolve()
    conv_root = owner_home / "conversations"
    conv_root.mkdir(parents=True)
    conv_store = ConversationStore(conv_root)
    t_active = _bind_task_link(conv_store, task_id="task-active", status="active")
    t_settled = _bind_task_link(conv_store, task_id="task-settled", status="completed")

    procs: list[subprocess.Popen[str]] = []
    try:
        active_proc = _spawn_sleeper()
        procs.append(active_proc)
        settled_proc = _spawn_sleeper()
        procs.append(settled_proc)
        unknown_proc = _spawn_sleeper()
        procs.append(unknown_proc)

        active_birth = bg_launch.capture_process_birth_token(active_proc.pid) or "active-birth"
        settled_birth = bg_launch.capture_process_birth_token(settled_proc.pid) or "settled-birth"
        unknown_birth = bg_launch.capture_process_birth_token(unknown_proc.pid) or "unknown-birth"

        store_root = background_store_root(tmp_path / "workspace", owner_home)
        store = ProcessSessionStore(Path(store_root))
        now = time.time() - 600
        _write_running_session(
            store, owner_home=owner_home, session_id="bg-active", thread_id=t_active,
            root_task_id="task-active", run_id="run-active", started_at=now,
            pid=active_proc.pid, birth_token=active_birth, child_pid=active_proc.pid, child_birth=active_birth,
        )
        _write_running_session(
            store, owner_home=owner_home, session_id="bg-settled", thread_id=t_settled,
            root_task_id="task-settled", run_id="run-settled", started_at=now,
            pid=settled_proc.pid, birth_token=settled_birth, child_pid=settled_proc.pid, child_birth=settled_birth,
        )
        _write_running_session(
            store, owner_home=owner_home, session_id="bg-unknown", thread_id="t-unk",
            root_task_id="task-unk", run_id="run-unk", started_at=now,
            pid=unknown_proc.pid, birth_token=unknown_birth, child_pid=unknown_proc.pid, child_birth=unknown_birth,
        )

        fake_agent = SimpleNamespace(
            root=tmp_path / "workspace",
            config=AgentConfig(),
            home_paths=SimpleNamespace(root=tmp_path, owner_home_dir=owner_home),
            subagents=SimpleNamespace(workspace=tmp_path / "subagents"),
        )
        stop_args = parser.parse_args(["gateway", "stop", "--stop-background", "--settled-only"])
        gw_paths = SimpleNamespace(pid=tmp_path / "gw.pid", stop_request=tmp_path / "stop_request.json")

        with (
            patch("agent_py_agent.cli.gateway_process.make_lightweight_cli_agent_context", return_value=fake_agent),
            patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=gw_paths),
            patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=0),
        ):
            rc = cmd_gateway_stop(stop_args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "active=1" in out and "settled=1" in out and "unknown=1" in out
        assert "root_state_unknown" in out
        settled_proc.wait(timeout=5)
        assert active_proc.poll() is None
        assert unknown_proc.poll() is None
        assert store.load("bg-settled").record["status"] in PROCESS_TERMINAL_STATUSES
        assert store.load("bg-active").record["status"] == "running"
        assert store.load("bg-unknown").record["status"] == "running"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_background_process_settled_ttl_seconds_registered_in_parameter_registry_and_sweeps_cold_start_only(
    tmp_path: Path,
) -> None:
    yaml_path = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    yaml_text = yaml_path.read_text(encoding="utf-8")
    parsed_yaml = yaml.safe_load(yaml_text)
    assert parsed_yaml["background_process_settled_ttl_seconds"] == 0
    # PD-1 Option B YAML comment alignment
    assert "撤掉该尾部项并保留原请求/线程思考强度" in yaml_text

    defaults = AgentConfig()
    assert defaults.background_process_settled_ttl_seconds == 0
    assert TimeoutFieldsService.normalize({"background_process_settled_ttl_seconds": -5}, defaults)[0]["background_process_settled_ttl_seconds"] == 0
    assert TimeoutFieldsService.normalize({"background_process_settled_ttl_seconds": "120"}, defaults)[0]["background_process_settled_ttl_seconds"] == 120

    spec_cfg = TUNABLE_KEYS["background_process_settled_ttl_seconds"]
    assert spec_cfg.effect == EFFECT_GATEWAY_RESTART
    assert spec_cfg.validate("0") == (True, "", 0)
    assert spec_cfg.validate("-1")[0] is False

    registry = parameter_registry()
    spec = registry["background_process_settled_ttl_seconds"]
    assert spec.value_type == "int"
    assert spec.default == 0
    assert spec.unit == "秒"
    assert spec.range == "≥0"
    assert spec.tier == "advanced"
    assert spec.writable is True
    assert spec.category == "Gateway 与调度"
    assert ranges()["background_process_settled_ttl_seconds"] == "≥0"
    assert unit_for_key("background_process_settled_ttl_seconds") == "秒"
    assert field_readers()["background_process_settled_ttl_seconds"] == ("cli", "cli/gateway_process")
    assert applied_value("background_process_settled_ttl_seconds", AgentConfig())[0] == 0
    assert applied_value_with("background_process_settled_ttl_seconds", "300", AgentConfig())[0] == 300

    # Cold start vs restart handover sweep verification inside _run_standalone_gateway
    owner_home = (tmp_path / "owners" / "local" / "main").resolve()
    conv_root = owner_home / "conversations"
    conv_root.mkdir(parents=True)
    conv_store = ConversationStore(conv_root)
    db = runtime_db_path(owner_home)
    RuntimeRepository(db)
    t_old = _bind_task_link(conv_store, task_id="task-settled-old", status="completed")
    t_young = _bind_task_link(conv_store, task_id="task-settled-young", status="completed")

    procs: list[subprocess.Popen[str]] = []
    try:
        old_proc = _spawn_sleeper()
        procs.append(old_proc)
        young_proc = _spawn_sleeper()
        procs.append(young_proc)
        old_birth = bg_launch.capture_process_birth_token(old_proc.pid) or "old-birth"
        young_birth = bg_launch.capture_process_birth_token(young_proc.pid) or "young-birth"

        store_root = background_store_root(tmp_path / "workspace", owner_home)
        store = ProcessSessionStore(Path(store_root))
        now = time.time()
        _insert_runtime_run(
            db,
            task_run_id="tr-settled-old",
            task_id="task-settled-old",
            task_status="done",
            agent_run_id="ar-settled-old",
            run_id="run-settled-old",
            agent_status="done",
            attempt_id="att-settled-old",
            attempt_status="done",
            ts=now - 900,
        )
        _insert_runtime_run(
            db,
            task_run_id="tr-settled-young",
            task_id="task-settled-young",
            task_status="done",
            agent_run_id="ar-settled-young",
            run_id="run-settled-young",
            agent_status="done",
            attempt_id="att-settled-young",
            attempt_status="done",
            ts=now - 10,
        )
        _write_running_session(
            store, owner_home=owner_home, session_id="bg-old-settled", thread_id=t_old,
            root_task_id="task-settled-old", run_id="run-settled-old", started_at=now - 900,
            pid=old_proc.pid, birth_token=old_birth, child_pid=old_proc.pid, child_birth=old_birth,
        )
        _write_running_session(
            store, owner_home=owner_home, session_id="bg-young-settled", thread_id=t_young,
            root_task_id="task-settled-young", run_id="run-settled-young", started_at=now - 900,
            pid=young_proc.pid, birth_token=young_birth, child_pid=young_proc.pid, child_birth=young_birth,
        )

        events: list[tuple[str, dict[str, object]]] = []
        gw_root = tmp_path / "gw"
        gw_paths = SimpleNamespace(
            root=gw_root,
            pid=gw_root / "gw.pid",
            state=gw_root / "gw.state",
            inbox=gw_root / "inbox",
            processing=gw_root / "processing",
            done=gw_root / "done",
            failed=gw_root / "failed",
            responses=gw_root / "responses",
        )

        def _run_gateway(ttl: int, *, restart_marker: dict | None, after_pid: int) -> None:
            fake_agent = SimpleNamespace(
                root=tmp_path / "workspace",
                config=AgentConfig(
                    background_process_settled_ttl_seconds=ttl,
                    owner_maintenance_scan_interval_seconds=0.0,
                ),
                home_paths=SimpleNamespace(root=tmp_path, owner_home_dir=owner_home),
                subagents=SimpleNamespace(workspace=tmp_path / "subagents"),
            )
            args = SimpleNamespace(after_pid=after_pid)
            run_ctx = GatewayRunContext(
                agent=fake_agent,
                paths=gw_paths,
                config_path=tmp_path / "agent_config.yaml",
                process_started_at=time.time(),
                process_identity={"pid": 1},
            )
            stop_event = threading.Event()
            service_loop_entered = threading.Event()
            sweep_entered = threading.Event()
            sweep_finished = threading.Event()
            sweep_ran_during_cmd_threads: list[bool] = []
            maint_threads: list[threading.Thread] = []

            def _fake_cmd_threads(req, **_kwargs):
                maint_thread = _start_owner_maintenance_loop(req.context, stop_event)
                if maint_thread is not None:
                    maint_threads.append(maint_thread)
                sweep_ran_during_cmd_threads.append(sweep_entered.is_set())
                return stop_event, None, None, None, None

            def _fake_service_loop(_ctx, **_kwargs):
                service_loop_entered.set()
                if maint_threads:
                    assert sweep_finished.wait(timeout=5)
                    maint_threads[0].join(timeout=5)
                return {"status": "stopped", "reason": "test"}

            def _gated_freeze(target_home: str | Path, **kwargs):
                sweep_entered.set()
                assert service_loop_entered.wait(timeout=5)
                try:
                    return freeze_settled_leftover_processes(target_home, **kwargs)
                finally:
                    sweep_finished.set()

            with ExitStack() as stack:
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.make_agent", return_value=fake_agent))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=gw_paths))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._gateway_run_context_from_args", return_value=run_ctx))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._preflight_required_local_credential"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.take_restart_marker", return_value=restart_marker))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._cmd_gateway_run_setup", return_value=(0, 12345)))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._cmd_gateway_run_threads", side_effect=_fake_cmd_threads))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.freeze_settled_leftover_processes", side_effect=_gated_freeze))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._install_gateway_signal_handlers", return_value=()))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._run_gateway_service_loop", side_effect=_fake_service_loop))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._classify_gateway_service_return", return_value=SimpleNamespace(kind="stopped", status="stopped", reason="test", exit_code=0)))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._record_gateway_service_termination"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._cmd_gateway_run_cleanup", return_value={"drain_complete": True}))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._restore_gateway_signal_handlers"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.record_standalone_stop_observation"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.record_standalone_stop"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.log_gateway_event", side_effect=lambda _a, name, event_data: events.append((name, event_data))))
                try:
                    assert _run_standalone_gateway(args, object()) == 0
                finally:
                    stop_event.set()
                    service_loop_entered.set()
                    for maint_thread in maint_threads:
                        maint_thread.join(timeout=5)
            assert sweep_ran_during_cmd_threads == [False]
            if ttl <= 0 or restart_marker is not None or after_pid > 0:
                assert run_ctx.cold_start_settled_ttl_seconds == 0  # type: ignore[attr-defined]
                assert maint_threads == []
                assert not sweep_entered.is_set()
            else:
                assert run_ctx.cold_start_settled_ttl_seconds == ttl  # type: ignore[attr-defined]
                assert len(maint_threads) == 1
                assert not maint_threads[0].is_alive()

        # 1. TTL=0 -> no sweep
        _run_gateway(0, restart_marker=None, after_pid=0)
        assert store.load("bg-old-settled").record["status"] == "running"
        assert not [e for e in events if e[0] == "gateway_settled_background_sweep"]

        # 2. Restart marker present -> no sweep even with TTL=300
        _run_gateway(300, restart_marker={"request_id": "req-1"}, after_pid=0)
        assert store.load("bg-old-settled").record["status"] == "running"

        # 3. after_pid handover -> no sweep even with TTL=300
        _run_gateway(300, restart_marker=None, after_pid=99999)
        assert store.load("bg-old-settled").record["status"] == "running"

        # 4. True cold start with TTL=300 -> sweeps asynchronously post-ready ONLY aged settled process
        _run_gateway(300, restart_marker=None, after_pid=0)
        old_proc.wait(timeout=5)
        assert young_proc.poll() is None
        assert store.load("bg-old-settled").record["status"] in PROCESS_TERMINAL_STATUSES
        assert store.load("bg-young-settled").record["status"] == "running"
        sweep_events = [event_data for name, event_data in events if name == "gateway_settled_background_sweep"]
        assert len(sweep_events) == 1
        assert sweep_events[0]["stopped"] == 1
        assert sweep_events[0]["deferred_settled"] == 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_stat_eio_and_scheduler_blocking_io_fail_closed_to_unknown(tmp_path: Path) -> None:
    """Stat EIO on authority paths or scheduler EAGAIN must keep sessions unknown without stopping."""
    owner_home = tmp_path / "owner_home"
    conv_root = owner_home / "conversations"
    conv_store = ConversationStore(conv_root, initialize=True)
    t_settled = _bind_task_link(conv_store, task_id="task-eio-settled", status="completed")

    RuntimeRepository(runtime_db_path(owner_home))
    wf_dir = owner_home / "data" / "workflow_runs" / "wf-settled-1"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "run.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    (conv_root / "wake_queue" / "urgent").mkdir(parents=True, exist_ok=True)

    store_root = background_store_root(tmp_path / "workspace", owner_home)
    store = ProcessSessionStore(Path(store_root))
    proc = _spawn_sleeper()
    try:
        birth = bg_launch.capture_process_birth_token(proc.pid) or "eio-birth"
        now = time.time()
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-eio-settled",
            thread_id=t_settled,
            root_task_id="task-eio-settled",
            run_id="run-eio-settled",
            started_at=now - 600,
            pid=proc.pid,
            birth_token=birth,
            child_pid=proc.pid,
            child_birth=birth,
        )

        supports_sets = tuple(
            sup_set
            for sup_set in (
                getattr(os, "supports_fd", None),
                getattr(os, "supports_dir_fd", None),
                getattr(os, "supports_follow_symlinks", None),
                getattr(os, "supports_effective_ids", None),
            )
            if isinstance(sup_set, set)
        )
        real_stat = os.stat
        real_lstat = os.lstat

        def _make_eio_wrapper(real_fn, target_substr: str):
            def _wrapped(path, *args, **kwargs):
                if target_substr in str(path):
                    raise OSError(errno.EIO, "simulated I/O error", str(path))
                return real_fn(path, *args, **kwargs)
            return _wrapped

        @contextmanager
        def _inject_stat_eio(target_substr: str):
            stat_wrapper = _make_eio_wrapper(real_stat, target_substr)
            lstat_wrapper = _make_eio_wrapper(real_lstat, target_substr)
            added = [
                (sup_set, fn)
                for sup_set in supports_sets
                for real_fn, fn in ((real_stat, stat_wrapper), (real_lstat, lstat_wrapper))
                if real_fn in sup_set
            ]
            for sup_set, fn in added:
                sup_set.add(fn)
            try:
                with patch("os.stat", stat_wrapper), patch("os.lstat", lstat_wrapper):
                    yield
            finally:
                for sup_set, fn in added:
                    sup_set.discard(fn)

        for target, prefix in (
            ("runtime.db", "runtime_db_"),
            ("workflow_runs", "workflow_runs_"),
            ("run.json", "workflow_dir_"),
            ("wake_queue", "conversation_wake_"),
            (str(store_root), "store_root_"),
        ):
            with _inject_stat_eio(target):
                report = freeze_settled_leftover_processes(owner_home, store_root=store_root, now_ts=now)
            assert report["counts"]["stopped"] == 0
            assert report["counts"]["settled"] == 0
            assert report["counts"]["unknown"] == 1
            assert str(report["unknown_processes"][0]["reason"]).startswith(prefix)
            assert proc.poll() is None
            assert store.load("bg-eio-settled").record["status"] == "running"

        with patch(
            "agent_py_agent.agent.gateway_parts.background_resource_report.read_atomic_store_bytes",
            side_effect=BlockingIOError(errno.EAGAIN, "busy"),
        ):
            sched_report = freeze_settled_leftover_processes(owner_home, store_root=store_root, now_ts=now)
        assert sched_report["counts"]["stopped"] == 0
        assert sched_report["counts"]["settled"] == 0
        assert sched_report["counts"]["unknown"] == 1
        assert sched_report["unknown_processes"][0]["reason"] == "scheduler_store_BlockingIOError"
        assert proc.poll() is None

        # Control: without faults, settled session stops cleanly.
        clean_report = freeze_settled_leftover_processes(owner_home, store_root=store_root, now_ts=now)
        assert clean_report["counts"]["stopped"] == 1
        assert clean_report["counts"]["unknown"] == 0
        proc.wait(timeout=5)
        assert store.load("bg-eio-settled").record["status"] in PROCESS_TERMINAL_STATUSES
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_settled_age_uses_terminal_timestamp_over_process_birth_age(tmp_path: Path) -> None:
    """ASTRA-F3: long-lived processes settled recently are deferred; max across authorities and skipped workflow runs apply."""
    owner_home = tmp_path / "owner_home"
    conv_root = owner_home / "conversations"
    conv_store = ConversationStore(conv_root, initialize=True)
    db = runtime_db_path(owner_home)
    RuntimeRepository(db)
    store_root = background_store_root(tmp_path / "workspace", owner_home)
    store = ProcessSessionStore(Path(store_root))
    now = 2_000_000.0

    # Prepare workflow index exceeding max_settled_dir_scan=256 so wf-259 is skipped during directory scan
    index_db = owner_home / "data" / "workflow_run_index.sqlite3"
    index_db.parent.mkdir(parents=True, exist_ok=True)
    wf_runs_dir = owner_home / "data" / "workflow_runs"
    wf_runs_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(index_db)) as conn:
        conn.execute("CREATE TABLE runs(run_id TEXT, status TEXT)")
        conn.execute("CREATE TABLE pending(run_id TEXT)")
        conn.execute("CREATE TABLE legacy(run_id TEXT)")
        for idx in range(260):
            wf_id = f"wf-{idx:03d}"
            conn.execute("INSERT INTO runs(run_id, status) VALUES(?, ?)", (wf_id, "completed"))
            run_dir = wf_runs_dir / wf_id
            run_dir.mkdir(parents=True, exist_ok=True)
            ended_at = (now - 15.0) if idx == 259 else (now - 1000.0)
            (run_dir / "run.json").write_text(
                json.dumps({"workflow_run_id": wf_id, "status": "completed", "ended_at": ended_at}),
                encoding="utf-8",
            )
        conn.commit()

    procs: list[subprocess.Popen[str]] = []
    try:
        # 1. Old process (started 7200s ago), runtime settled only 20s ago -> deferred
        p_recent = _spawn_sleeper()
        procs.append(p_recent)
        b_recent = bg_launch.capture_process_birth_token(p_recent.pid) or "b-recent"
        t_recent = _bind_task_link(conv_store, task_id="task-recent", status="completed")
        _insert_runtime_run(
            db,
            task_run_id="tr-recent",
            task_id="task-recent",
            task_status="done",
            agent_run_id="ar-recent",
            run_id="run-recent",
            agent_status="done",
            attempt_id="att-recent",
            attempt_status="done",
            ts=now - 20.0,
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-old-proc-recent-settle",
            thread_id=t_recent,
            root_task_id="task-recent",
            run_id="run-recent",
            started_at=now - 7200.0,
            pid=p_recent.pid,
            birth_token=b_recent,
            child_pid=p_recent.pid,
            child_birth=b_recent,
        )

        # 2. Old process (started 7200s ago), runtime settled 400s ago -> stopped
        p_old = _spawn_sleeper()
        procs.append(p_old)
        b_old = bg_launch.capture_process_birth_token(p_old.pid) or "b-old"
        t_old = _bind_task_link(conv_store, task_id="task-old", status="completed")
        _insert_runtime_run(
            db,
            task_run_id="tr-old",
            task_id="task-old",
            task_status="done",
            agent_run_id="ar-old",
            run_id="run-old",
            agent_status="done",
            attempt_id="att-old",
            attempt_status="done",
            ts=now - 400.0,
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-old-proc-old-settle",
            thread_id=t_old,
            root_task_id="task-old",
            run_id="run-old",
            started_at=now - 7200.0,
            pid=p_old.pid,
            birth_token=b_old,
            child_pid=p_old.pid,
            child_birth=b_old,
        )

        # 3. Multi-authority settled: runtime (400s ago) + scheduler (350s ago) + workflow wf-259 (15s ago, past scan cap) -> max wins -> deferred
        p_multi = _spawn_sleeper()
        procs.append(p_multi)
        b_multi = bg_launch.capture_process_birth_token(p_multi.pid) or "b-multi"
        t_multi = _bind_task_link(conv_store, task_id="task-multi", status="completed")
        _insert_runtime_run(
            db,
            task_run_id="tr-multi",
            task_id="task-multi",
            task_status="done",
            agent_run_id="ar-multi",
            run_id="wf-259",
            agent_status="done",
            attempt_id="att-multi",
            attempt_status="done",
            ts=now - 400.0,
        )
        sched_path = owner_home / "data" / "scheduler" / "store.json"
        sched_path.parent.mkdir(parents=True, exist_ok=True)
        sched_path.write_text(
            json.dumps(
                {
                    "runs": {
                        "sched-multi": {
                            "run_id": "wf-259",
                            "task_id": "task-multi",
                            "status": "done",
                            "ended_at": now - 350.0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-multi-auth-skipped-wf",
            thread_id=t_multi,
            root_task_id="task-multi",
            run_id="wf-259",
            started_at=now - 7200.0,
            pid=p_multi.pid,
            birth_token=b_multi,
            child_pid=p_multi.pid,
            child_birth=b_multi,
        )

        report = freeze_settled_leftover_processes(owner_home, min_settled_age_seconds=300, now_ts=now)
        assert report["counts"] == {
            "active": 0,
            "settled": 3,
            "stopped": 1,
            "deferred_settled": 2,
            "stop_failed": 0,
            "unknown": 0,
        }
        stopped_ids = {row["session_id"] for row in report["stopped_processes"] if row.get("stopped")}
        assert stopped_ids == {"bg-old-proc-old-settle"}

        deferred_by_id = {row["session_id"]: row for row in report["deferred_settled_processes"]}
        assert set(deferred_by_id) == {"bg-old-proc-recent-settle", "bg-multi-auth-skipped-wf"}
        assert deferred_by_id["bg-old-proc-recent-settle"]["reason"] == "younger_than_min_settled_age"
        assert deferred_by_id["bg-old-proc-recent-settle"]["settled_age_known"] is True
        assert deferred_by_id["bg-old-proc-recent-settle"]["settled_age_seconds"] == pytest.approx(20.0)
        assert deferred_by_id["bg-multi-auth-skipped-wf"]["reason"] == "younger_than_min_settled_age"
        assert deferred_by_id["bg-multi-auth-skipped-wf"]["settled_age_known"] is True
        assert deferred_by_id["bg-multi-auth-skipped-wf"]["settled_age_seconds"] == pytest.approx(15.0)
        assert set(deferred_by_id["bg-multi-auth-skipped-wf"]["affirmed_sources"]) >= {
            "runtime_db",
            "scheduler",
            "workflow",
        }

        p_old.wait(timeout=5)
        assert p_recent.poll() is None
        assert p_multi.poll() is None
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_missing_or_invalid_settled_timestamps_defer_when_min_age_positive_and_stop_when_zero(tmp_path: Path) -> None:
    """ASTRA-F3: missing/incomplete/future timestamps or conversation-link-only defer if min_age>0, stop if min_age==0."""
    owner_home = tmp_path / "owner_home"
    conv_root = owner_home / "conversations"
    conv_store = ConversationStore(conv_root, initialize=True)
    db = runtime_db_path(owner_home)
    RuntimeRepository(db)
    store_root = background_store_root(tmp_path / "workspace", owner_home)
    store = ProcessSessionStore(Path(store_root))
    now = 2_000_000.0

    procs: list[subprocess.Popen[str]] = []
    try:
        # 1. runtime.db with closed_at=0.0, ended_at=0.0
        _insert_runtime_run(
            db,
            task_run_id="tr-zero-ts",
            task_id="task-zero-ts",
            task_status="done",
            agent_run_id="ar-zero-ts",
            run_id="run-zero-ts",
            agent_status="done",
            attempt_id="att-zero-ts",
            attempt_status="done",
            ts=now - 1000.0,
            closed_at=0.0,
            ended_at=0.0,
        )
        # 2. runtime.db partial: closed_at valid, attempt ended_at=0.0 -> incomplete
        _insert_runtime_run(
            db,
            task_run_id="tr-partial-ts",
            task_id="task-partial-ts",
            task_status="done",
            agent_run_id="ar-partial-ts",
            run_id="run-partial-ts",
            agent_status="done",
            attempt_id="att-partial-ts",
            attempt_status="done",
            ts=now - 1000.0,
            closed_at=now - 800.0,
            ended_at=0.0,
        )
        # 3. runtime.db future drifted timestamps
        _insert_runtime_run(
            db,
            task_run_id="tr-future-ts",
            task_id="task-future-ts",
            task_status="done",
            agent_run_id="ar-future-ts",
            run_id="run-future-ts",
            agent_status="done",
            attempt_id="att-future-ts",
            attempt_status="done",
            ts=now + 600.0,
        )
        # 4. scheduler missing ended_at
        sched_path = owner_home / "data" / "scheduler" / "store.json"
        sched_path.parent.mkdir(parents=True, exist_ok=True)
        sched_path.write_text(
            json.dumps({"runs": {"sched-no-ts": {"run_id": "run-sched-nots", "task_id": "task-sched-nots", "status": "done"}}}),
            encoding="utf-8",
        )
        # 5. workflow missing ended_at
        wf_dir = owner_home / "data" / "workflow_runs" / "run-wf-nots"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "run.json").write_text(
            json.dumps({"workflow_run_id": "run-wf-nots", "task_id": "task-wf-nots", "status": "completed"}),
            encoding="utf-8",
        )

        cases = [
            ("bg-zero-ts", "task-zero-ts", "run-zero-ts"),
            ("bg-partial-ts", "task-partial-ts", "run-partial-ts"),
            ("bg-future-ts", "task-future-ts", "run-future-ts"),
            ("bg-sched-nots", "task-sched-nots", "run-sched-nots"),
            ("bg-wf-nots", "task-wf-nots", "run-wf-nots"),
            ("bg-link-only", "task-link-only", "run-link-only"),
        ]
        for session_id, task_id, run_id in cases:
            proc = _spawn_sleeper()
            procs.append(proc)
            birth = bg_launch.capture_process_birth_token(proc.pid) or session_id
            thread_id = _bind_task_link(conv_store, task_id=task_id, status="completed")
            _write_running_session(
                store,
                owner_home=owner_home,
                session_id=session_id,
                thread_id=thread_id,
                root_task_id=task_id,
                run_id=run_id,
                started_at=now - 3600.0,
                pid=proc.pid,
                birth_token=birth,
                child_pid=proc.pid,
                child_birth=birth,
            )

        deferred_report = freeze_settled_leftover_processes(owner_home, min_settled_age_seconds=300, now_ts=now)
        assert deferred_report["counts"] == {
            "active": 0,
            "settled": 6,
            "stopped": 0,
            "deferred_settled": 6,
            "stop_failed": 0,
            "unknown": 0,
        }
        for row in deferred_report["deferred_settled_processes"]:
            assert row["reason"] == "settled_at_unknown"
            assert row["settled_age_seconds"] is None
            assert row["settled_age_known"] is False
            assert row["eligible_to_stop"] is False
            assert row["stopped"] is False
        assert all(proc.poll() is None for proc in procs)

        stop_report = freeze_settled_leftover_processes(owner_home, min_settled_age_seconds=0, now_ts=now)
        assert stop_report["counts"]["stopped"] == 6
        for proc in procs:
            proc.wait(timeout=5)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_cold_start_settled_sweep_step_paginates_handles_errors_and_yields_on_stop(tmp_path: Path) -> None:
    """ASTRA-F4: bounded cold-start sweep paginates across owners, tolerates owner errors, and yields cleanly on stop."""
    primary_home = (tmp_path / "owners" / "local" / "main").resolve()
    primary_home.mkdir(parents=True)
    extra_homes = []
    for idx in range(1, 5):
        oh = (tmp_path / "owners" / "providers" / "feishu" / "users" / f"u{idx}").resolve()
        oh.mkdir(parents=True)
        extra_homes.append(oh)

    fake_agent = SimpleNamespace(
        root=tmp_path / "workspace",
        config=AgentConfig(background_process_settled_ttl_seconds=300),
        home_paths=SimpleNamespace(root=tmp_path, owner_home_dir=primary_home),
    )
    gw_paths = SimpleNamespace(root=tmp_path / "gw")
    frozen_ctx = GatewayRunContext(
        agent=fake_agent,
        paths=gw_paths,
        config_path=tmp_path / "agent_config.yaml",
    )
    ready_ev = threading.Event()
    _attach_cold_start_settled_intent(frozen_ctx, ttl_seconds=300, ready_event=ready_ev)
    assert frozen_ctx.cold_start_settled_ttl_seconds == 300  # type: ignore[attr-defined]
    assert frozen_ctx.cold_start_ready_event is ready_ev  # type: ignore[attr-defined]

    events: list[tuple[str, dict[str, object]]] = []
    visited: list[str] = []

    def _fake_freeze(owner_home: str | Path, **_kwargs):
        visited.append(str(owner_home))
        if str(owner_home).endswith("u2"):
            raise RuntimeError("owner_read_error")
        return {
            "counts": {
                "active": 0,
                "settled": 1,
                "stopped": 1,
                "deferred_settled": 0,
                "stop_failed": 0,
                "unknown": 0,
            }
        }

    with (
        patch("agent_py_agent.cli.gateway_process.freeze_settled_leftover_processes", side_effect=_fake_freeze),
        patch("agent_py_agent.cli.gateway_process.log_gateway_event", side_effect=lambda _a, name, event_data: events.append((name, event_data))),
    ):
        state: dict[str, object] = {}
        stop_ev = threading.Event()
        assert run_cold_start_settled_sweep_step(fake_agent, state, ttl_seconds=300, max_owners_per_tick=2, slice_ms=0, stop_event=stop_ev) is False
        assert len(visited) == 2
        assert events == []

        assert run_cold_start_settled_sweep_step(fake_agent, state, ttl_seconds=300, max_owners_per_tick=2, slice_ms=0, stop_event=stop_ev) is False
        assert len(visited) == 4
        assert events == []

        stop_ev.set()
        assert run_cold_start_settled_sweep_step(fake_agent, state, ttl_seconds=300, max_owners_per_tick=2, slice_ms=0, stop_event=stop_ev) is False
        assert len(visited) == 4
        assert events == []

        stop_ev.clear()
        assert run_cold_start_settled_sweep_step(fake_agent, state, ttl_seconds=300, max_owners_per_tick=2, slice_ms=0, stop_event=stop_ev) is True
        assert len(visited) == 5
        assert len(events) == 1
        assert events[0][0] == "gateway_settled_background_sweep"
        assert events[0][1]["stopped"] == 4
        assert events[0][1]["settled"] == 4
        assert len(events[0][1]["errors"]) == 1
        assert events[0][1]["errors"][0]["error_type"] == "RuntimeError"


def test_owner_maintenance_controller_interval_zero_runs_one_shot_cold_sweep_and_exits(tmp_path: Path) -> None:
    """ASTRA-F4: when scan interval is 0 and cold sweep TTL>0, controller waits on ready_event, runs sweep once, and exits."""
    owner_home = (tmp_path / "owners" / "local" / "main").resolve()
    owner_home.mkdir(parents=True)
    fake_agent = SimpleNamespace(
        root=tmp_path / "workspace",
        config=AgentConfig(
            background_process_settled_ttl_seconds=300,
            owner_maintenance_scan_interval_seconds=0.0,
            retention_scan_slice_ms=25,
        ),
        home_paths=SimpleNamespace(root=tmp_path, owner_home_dir=owner_home),
        _owner_wake_hints=None,
    )
    ctx = GatewayRunContext(
        agent=fake_agent,
        paths=SimpleNamespace(root=tmp_path / "gw"),
        config_path=tmp_path / "agent_config.yaml",
    )
    ready_event = threading.Event()
    _attach_cold_start_settled_intent(ctx, ttl_seconds=300, ready_event=ready_event)

    freeze_called = threading.Event()
    events: list[tuple[str, dict[str, object]]] = []

    def _fake_freeze(_home: str | Path, **_kwargs):
        freeze_called.set()
        return {"counts": {"active": 0, "settled": 0, "stopped": 0, "deferred_settled": 0, "stop_failed": 0, "unknown": 0}}

    stop_event = threading.Event()
    with (
        patch("agent_py_agent.cli.gateway_process.freeze_settled_leftover_processes", side_effect=_fake_freeze),
        patch("agent_py_agent.cli.gateway_process.log_gateway_event", side_effect=lambda _a, name, event_data: events.append((name, event_data))),
    ):
        thread = _start_owner_maintenance_loop(ctx, stop_event)
        assert thread is not None
        try:
            assert not freeze_called.wait(timeout=0.15)
            ready_event.set()
            assert freeze_called.wait(timeout=5)
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert [name for name, _ in events] == ["gateway_settled_background_sweep"]
        finally:
            stop_event.set()
            ready_event.set()
            thread.join(timeout=5)


def _terminate_sleeper_proc(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _wait_proc_dead(proc: subprocess.Popen[str], timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"subprocess {proc.pid} remained alive after {timeout}s")


def _wait_pty_stopped(session: object, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = getattr(session, "process", None)
        proc_dead = proc is None or proc.poll() is not None
        if proc_dead and (getattr(session, "closed", False) or getattr(session, "termination", None) is not None):
            return
        time.sleep(0.05)
    proc = getattr(session, "process", None)
    raise AssertionError(
        f"PTY session did not settle within {timeout}s: "
        f"poll={proc.poll() if proc else None}, closed={getattr(session, 'closed', None)}, "
        f"termination={getattr(session, 'termination', None)}"
    )


def _start_pty_sleeper(
    registry: PtySessionRegistry,
    *,
    work_dir: Path,
    owner_home: Path,
    thread_id: str,
    task_id: str,
    run_id: str,
    attempt_id: str,
    monkeypatch: pytest.MonkeyPatch,
):
    py_expr = '__import__("time").sleep(60)'
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(py_expr)}"
    scope = {
        "owner_home": str(owner_home),
        "session_id": thread_id,
        "root_task_id": task_id,
        "task_id": f"gateway:{task_id}",
        "run_id": run_id,
        "attempt_id": attempt_id,
    }
    try:
        return registry.start(cmd, work_dir, owner_home=owner_home, run_scope=scope)
    except SandboxUnavailable:
        # 受限容器缺少宿主沙箱后端时，仅在测试内直起真实子进程并保持 PTY 与 Scope 链路完整。
        monkeypatch.setattr(
            pty_sessions_mod,
            "_sandbox_exec",
            lambda c, target, owner_home=None, protected_persona_root=None, write_roots=None, read_roots=None, write_protection=None, *, host_policy=None, sandbox_factory=None: (c, True),
        )
        return registry.start(cmd, work_dir, owner_home=owner_home, run_scope=scope)


@pytest.mark.skipif(os.name == "nt", reason="stdlib pty is POSIX-only")
def test_settled_sweep_scopes_batch_freeze_to_unique_attempt_id_isolating_concurrent_create_attempt_bg_and_pty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_home = tmp_path / "owner-home"
    work_dir = tmp_path / "work"
    owner_home.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    conv_root = owner_home / "conversations"
    conv_root.mkdir(parents=True, exist_ok=True)
    conv_store = ConversationStore(conv_root, initialize=True)
    store = ProcessSessionStore(process_session_store_root(owner_home, owner_home))
    db = runtime_db_path(owner_home)
    repo = RuntimeRepository(db)
    pty_registry = PtySessionRegistry()
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.process_resource_stop.pty_session_registry",
        pty_registry,
    )

    settled_ts = time.time() - 3600.0
    task_id = "task-fw41r2-unique-attempt"
    thread_id = _bind_task_link(conv_store, task_id=task_id, status="completed")
    run_id = "run-fw41r2-unique"
    task_run_id = "tr-fw41r2-unique"
    agent_run_id = "ar-fw41r2-unique"
    old_attempt_id = "att-old-unique"
    _insert_runtime_run(
        db,
        task_run_id=task_run_id,
        task_id=task_id,
        task_status="done",
        agent_run_id=agent_run_id,
        run_id=run_id,
        agent_status="done",
        attempt_id=old_attempt_id,
        attempt_status="done",
        ts=settled_ts,
        closed_at=settled_ts,
        ended_at=settled_ts,
    )

    proc_old: subprocess.Popen[str] | None = None
    proc_new: subprocess.Popen[str] | None = None
    classified_ready = threading.Event()
    proceed_stop = threading.Event()
    gated = [False]

    try:
        proc_old = _spawn_sleeper()
        proc_old_birth = bg_launch.capture_process_birth_token(proc_old.pid)
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-old",
            thread_id=thread_id,
            root_task_id=task_id,
            run_id=run_id,
            attempt_id=old_attempt_id,
            started_at=settled_ts,
            pid=proc_old.pid,
            birth_token=proc_old_birth,
            child_pid=proc_old.pid,
            child_birth=proc_old_birth,
        )
        pty_old = _start_pty_sleeper(
            pty_registry,
            work_dir=work_dir,
            owner_home=owner_home,
            thread_id=thread_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=old_attempt_id,
            monkeypatch=monkeypatch,
        )

        requested_pty_scopes: list[dict[str, str]] = []
        requested_pty_ids: list[str] = []
        orig_request_stop = pty_registry.request_stop

        def _spy_request_stop(
            *,
            owner_home: object,
            thread_id: str = "",
            task_id: str = "",
            run_id: str = "",
            attempt_id: str = "",
        ) -> tuple[str, ...]:
            requested_pty_scopes.append({
                "owner_home": str(owner_home or ""),
                "thread_id": str(thread_id or ""),
                "task_id": str(task_id or ""),
                "run_id": str(run_id or ""),
                "attempt_id": str(attempt_id or ""),
            })
            stopped = orig_request_stop(
                owner_home=owner_home,
                thread_id=thread_id,
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            requested_pty_ids.extend(stopped)
            return stopped

        monkeypatch.setattr(pty_registry, "request_stop", _spy_request_stop)

        freeze_scopes: list[object] = []
        orig_freeze_process_stop = bg_resource_report.freeze_process_stop

        def _spy_freeze_process_stop(scope: object):
            freeze_scopes.append(scope)
            return orig_freeze_process_stop(scope)

        monkeypatch.setattr(bg_resource_report, "freeze_process_stop", _spy_freeze_process_stop)

        orig_stop_settled_batch = bg_resource_report._stop_settled_batch

        def _gated_stop_settled_batch(
            *,
            store: ProcessSessionStore,
            resolved_store_root: Path,
            default_store_root: Path,
            owner_home_str: str,
            store_has_errors: bool,
            all_scope_keys_by_scope: dict[tuple[str, str, str], set[str]],
            eligible_entries: list[tuple[dict[str, object], dict[str, object]]],
        ) -> list[dict[str, object]]:
            if not gated[0] and eligible_entries:
                gated[0] = True
                classified_ready.set()
                assert proceed_stop.wait(timeout=10)
            return orig_stop_settled_batch(
                store=store,
                resolved_store_root=resolved_store_root,
                default_store_root=default_store_root,
                owner_home_str=owner_home_str,
                store_has_errors=store_has_errors,
                all_scope_keys_by_scope=all_scope_keys_by_scope,
                eligible_entries=eligible_entries,
            )

        monkeypatch.setattr(bg_resource_report, "_stop_settled_batch", _gated_stop_settled_batch)

        sweep_results: list[dict[str, object]] = []
        sweep_errors: list[BaseException] = []

        def _run_sweep() -> None:
            try:
                sweep_results.append(
                    freeze_settled_leftover_processes(owner_home, min_settled_age_seconds=60.0)
                )
            except BaseException as exc:
                sweep_errors.append(exc)

        sweep_thread = threading.Thread(target=_run_sweep, daemon=True)
        sweep_thread.start()
        assert classified_ready.wait(timeout=10)

        new_attempt_row = repo.create_attempt(agent_run_id)
        new_attempt_id = str(new_attempt_row["attempt_id"])
        assert new_attempt_id
        assert new_attempt_id != old_attempt_id

        proc_new = _spawn_sleeper()
        proc_new_birth = bg_launch.capture_process_birth_token(proc_new.pid)
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-new",
            thread_id=thread_id,
            root_task_id=task_id,
            run_id=run_id,
            attempt_id=new_attempt_id,
            started_at=time.time(),
            pid=proc_new.pid,
            birth_token=proc_new_birth,
            child_pid=proc_new.pid,
            child_birth=proc_new_birth,
        )
        pty_new = _start_pty_sleeper(
            pty_registry,
            work_dir=work_dir,
            owner_home=owner_home,
            thread_id=thread_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=new_attempt_id,
            monkeypatch=monkeypatch,
        )

        diag_report = freeze_settled_leftover_processes(
            owner_home,
            min_settled_age_seconds=999999.0,
        )
        assert diag_report["counts"]["active"] == 2

        proceed_stop.set()
        sweep_thread.join(timeout=10)
        assert not sweep_thread.is_alive()
        assert not sweep_errors
        assert len(sweep_results) == 1

        assert len(freeze_scopes) == 1
        assert getattr(freeze_scopes[0], "attempt_id", "") == old_attempt_id
        assert len(requested_pty_scopes) == 1
        assert requested_pty_scopes[0]["attempt_id"] == old_attempt_id
        assert pty_old.session_id in requested_pty_ids
        assert pty_new.session_id not in requested_pty_ids

        _wait_proc_dead(proc_old)
        _wait_pty_stopped(pty_old)
        bg_old_doc = store.load("bg-old").record
        assert bg_old_doc["stop_requested"] is True or bg_old_doc["status"] in PROCESS_TERMINAL_STATUSES

        bg_new_doc = store.load("bg-new").record
        assert bg_new_doc["status"] == "running"
        assert bg_new_doc["stop_requested"] is False
        assert proc_new.poll() is None
        assert pty_new.closed is False
        assert pty_new.process is not None and pty_new.process.poll() is None
        latest_attempt = repo.get_attempt(new_attempt_id)
        assert latest_attempt is not None and str(latest_attempt["status"]) == "running"
    finally:
        proceed_stop.set()
        _terminate_sleeper_proc(proc_old)
        _terminate_sleeper_proc(proc_new)
        pty_registry.clear()


@pytest.mark.skipif(os.name == "nt", reason="stdlib pty is POSIX-only")
@pytest.mark.parametrize("case", ["mixed_attempts", "missing_attempt"])
def test_settled_sweep_mixed_or_missing_attempt_id_falls_back_to_per_record_stop_preserving_resumed_attempt(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_home = tmp_path / "owner-home"
    work_dir = tmp_path / "work"
    owner_home.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    conv_root = owner_home / "conversations"
    conv_root.mkdir(parents=True, exist_ok=True)
    conv_store = ConversationStore(conv_root, initialize=True)
    store = ProcessSessionStore(process_session_store_root(owner_home, owner_home))
    db = runtime_db_path(owner_home)
    repo = RuntimeRepository(db)
    pty_registry = PtySessionRegistry()
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.process_resource_stop.pty_session_registry",
        pty_registry,
    )

    settled_ts = time.time() - 3600.0
    task_id = f"task-fw41r2-{case}"
    thread_id = _bind_task_link(conv_store, task_id=task_id, status="completed")
    run_id = f"run-fw41r2-{case}"
    task_run_id = f"tr-fw41r2-{case}"
    agent_run_id = f"ar-fw41r2-{case}"

    initial_attempt = "att-a" if case == "mixed_attempts" else "att-settled-db"
    _insert_runtime_run(
        db,
        task_run_id=task_run_id,
        task_id=task_id,
        task_status="done",
        agent_run_id=agent_run_id,
        run_id=run_id,
        agent_status="done",
        attempt_id=initial_attempt,
        attempt_status="done",
        ts=settled_ts,
        closed_at=settled_ts,
        ended_at=settled_ts,
    )
    if case == "mixed_attempts":
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, started_at, ended_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                ("att-b", agent_run_id, 2, "done", settled_ts, settled_ts),
            )
            conn.execute(
                "UPDATE agent_runs SET current_attempt_id = ?, current_attempt_generation = ? "
                "WHERE agent_run_id = ?",
                ("att-b", 2, agent_run_id),
            )
            conn.commit()

    old_procs: list[subprocess.Popen[str]] = []
    proc_new: subprocess.Popen[str] | None = None
    classified_ready = threading.Event()
    proceed_stop = threading.Event()
    gated = [False]

    try:
        old_attempt_specs = (
            [("bg-old-1", "att-a"), ("bg-old-2", "att-b")]
            if case == "mixed_attempts"
            else [("bg-old-1", "")]
        )
        expected_old_sids = {sid for sid, _ in old_attempt_specs}
        for sid, att_id in old_attempt_specs:
            proc = _spawn_sleeper()
            old_procs.append(proc)
            birth = bg_launch.capture_process_birth_token(proc.pid)
            _write_running_session(
                store,
                owner_home=owner_home,
                session_id=sid,
                thread_id=thread_id,
                root_task_id=task_id,
                run_id=run_id,
                attempt_id=att_id,
                started_at=settled_ts,
                pid=proc.pid,
                birth_token=birth,
                child_pid=proc.pid,
                child_birth=birth,
            )

        freeze_calls: list[object] = []
        orig_freeze_process_stop = bg_resource_report.freeze_process_stop

        def _spy_freeze_process_stop(scope: object):
            freeze_calls.append(scope)
            return orig_freeze_process_stop(scope)

        monkeypatch.setattr(bg_resource_report, "freeze_process_stop", _spy_freeze_process_stop)

        per_record_stop_ids: list[str] = []
        orig_stop_process_session = bg_resource_report.stop_process_session

        def _spy_stop_process_session(
            target_store: ProcessSessionStore,
            selected: dict[str, object],
            *,
            host_process: subprocess.Popen | None = None,
        ):
            per_record_stop_ids.append(str(selected.get("session_id") or ""))
            return orig_stop_process_session(target_store, selected, host_process=host_process)

        monkeypatch.setattr(bg_resource_report, "stop_process_session", _spy_stop_process_session)

        orig_stop_settled_batch = bg_resource_report._stop_settled_batch

        def _gated_stop_settled_batch(
            *,
            store: ProcessSessionStore,
            resolved_store_root: Path,
            default_store_root: Path,
            owner_home_str: str,
            store_has_errors: bool,
            all_scope_keys_by_scope: dict[tuple[str, str, str], set[str]],
            eligible_entries: list[tuple[dict[str, object], dict[str, object]]],
        ) -> list[dict[str, object]]:
            if not gated[0] and eligible_entries:
                gated[0] = True
                classified_ready.set()
                assert proceed_stop.wait(timeout=10)
            return orig_stop_settled_batch(
                store=store,
                resolved_store_root=resolved_store_root,
                default_store_root=default_store_root,
                owner_home_str=owner_home_str,
                store_has_errors=store_has_errors,
                all_scope_keys_by_scope=all_scope_keys_by_scope,
                eligible_entries=eligible_entries,
            )

        monkeypatch.setattr(bg_resource_report, "_stop_settled_batch", _gated_stop_settled_batch)

        sweep_results: list[dict[str, object]] = []
        sweep_errors: list[BaseException] = []

        def _run_sweep() -> None:
            try:
                sweep_results.append(
                    freeze_settled_leftover_processes(owner_home, min_settled_age_seconds=60.0)
                )
            except BaseException as exc:
                sweep_errors.append(exc)

        sweep_thread = threading.Thread(target=_run_sweep, daemon=True)
        sweep_thread.start()
        assert classified_ready.wait(timeout=10)

        new_attempt_row = repo.create_attempt(agent_run_id)
        new_attempt_id = str(new_attempt_row["attempt_id"])
        assert new_attempt_id and new_attempt_id != initial_attempt

        proc_new = _spawn_sleeper()
        proc_new_birth = bg_launch.capture_process_birth_token(proc_new.pid)
        _write_running_session(
            store,
            owner_home=owner_home,
            session_id="bg-new",
            thread_id=thread_id,
            root_task_id=task_id,
            run_id=run_id,
            attempt_id=new_attempt_id,
            started_at=time.time(),
            pid=proc_new.pid,
            birth_token=proc_new_birth,
            child_pid=proc_new.pid,
            child_birth=proc_new_birth,
        )
        pty_new = _start_pty_sleeper(
            pty_registry,
            work_dir=work_dir,
            owner_home=owner_home,
            thread_id=thread_id,
            task_id=task_id,
            run_id=run_id,
            attempt_id=new_attempt_id,
            monkeypatch=monkeypatch,
        )

        proceed_stop.set()
        sweep_thread.join(timeout=10)
        assert not sweep_thread.is_alive()
        assert not sweep_errors
        assert len(sweep_results) == 1

        assert freeze_calls == []
        assert set(per_record_stop_ids) == expected_old_sids
        assert "bg-new" not in per_record_stop_ids

        for proc in old_procs:
            _wait_proc_dead(proc)
        for sid in expected_old_sids:
            doc = store.load(sid).record
            assert doc["stop_requested"] is True or doc["status"] in PROCESS_TERMINAL_STATUSES

        bg_new_doc = store.load("bg-new").record
        assert bg_new_doc["status"] == "running"
        assert bg_new_doc["stop_requested"] is False
        assert proc_new.poll() is None
        assert pty_new.closed is False
        assert pty_new.process is not None and pty_new.process.poll() is None
    finally:
        proceed_stop.set()
        for proc in old_procs:
            _terminate_sleeper_proc(proc)
        _terminate_sleeper_proc(proc_new)
        pty_registry.clear()
