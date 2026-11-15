"""Verify PD-5b three-state settled background reclaim, CLI flags, and TTL config."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

import agent_py_agent.agent.tooling.background_process_launch as bg_launch
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
from agent_py_agent.cli.gateway_process import _run_standalone_gateway, cmd_gateway_stop
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
) -> None:
    now = time.time()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO task_runs(task_run_id, task_id, status, created_at, updated_at, closed_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (task_run_id, task_id, task_status, now, now, now if task_status in {"done", "failed", "cancelled"} else 0.0),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agent_runs(agent_run_id, task_run_id, parent_agent_run_id, run_id, role, status, "
            "current_attempt_id, current_attempt_generation, workspace_epoch, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_run_id, task_run_id, parent_agent_run_id, run_id, "main", agent_status, attempt_id, 1, 1, now, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, started_at, ended_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (attempt_id, agent_run_id, 1, attempt_status, now, now if attempt_status != "running" else 0.0),
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
                    now,
                    now,
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
    t_settled = _bind_task_link(conv_store, task_id="task-settled", status="completed")

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
        _write_running_session(
            store, owner_home=owner_home, session_id="bg-old-settled", thread_id=t_settled,
            root_task_id="task-settled", run_id="run-settled", started_at=now - 900,
            pid=old_proc.pid, birth_token=old_birth, child_pid=old_proc.pid, child_birth=old_birth,
        )
        _write_running_session(
            store, owner_home=owner_home, session_id="bg-young-settled", thread_id=t_settled,
            root_task_id="task-settled", run_id="run-settled", started_at=now - 10,
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
                config=AgentConfig(background_process_settled_ttl_seconds=ttl),
                home_paths=SimpleNamespace(root=tmp_path, owner_home_dir=owner_home),
                subagents=SimpleNamespace(workspace=tmp_path / "subagents"),
            )
            args = SimpleNamespace(after_pid=after_pid)
            run_ctx = SimpleNamespace(process_started_at=time.time(), process_identity={"pid": 1}, control_plane=None)
            with ExitStack() as stack:
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.make_agent", return_value=fake_agent))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=gw_paths))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._gateway_run_context_from_args", return_value=run_ctx))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._preflight_required_local_credential"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.take_restart_marker", return_value=restart_marker))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._cmd_gateway_run_setup", return_value=(0, 12345)))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._cmd_gateway_run_threads", return_value=(None, None, None, None, None)))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._install_gateway_signal_handlers", return_value=()))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._run_gateway_service_loop", return_value={"status": "stopped", "reason": "test"}))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._classify_gateway_service_return", return_value=SimpleNamespace(kind="stopped", status="stopped", reason="test", exit_code=0)))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._record_gateway_service_termination"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._cmd_gateway_run_cleanup", return_value={"drain_complete": True}))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process._restore_gateway_signal_handlers"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.record_standalone_stop_observation"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.record_standalone_stop"))
                stack.enter_context(patch("agent_py_agent.cli.gateway_process.log_gateway_event", side_effect=lambda _a, name, event_data: events.append((name, event_data))))
                assert _run_standalone_gateway(args, object()) == 0

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

        # 4. True cold start with TTL=300 -> sweeps ONLY aged settled process, keeps younger settled
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
