"""The self-stop guard checks every command segment, not only the first word.

Live clone (2026-11-17, W6 Full Access admin turn): ``kill -TERM <gateway pid>; echo "kill_exit=$?"`` stopped
the clone gateway twice.  The guard only looked at the first word of the whole command, and ``<pid>;`` is not
a number, so no target was found.
"""
from __future__ import annotations

import json
import os

import pytest

from agent_py_agent.agent.gateway_parts.daemon_metadata import build_process_identity
from agent_py_agent.agent.tooling.active_service_guard import (
    active_service_stop_denial,
    guarded_command_text,
)

PID = os.getpid()
PGID = os.getpgid(PID)


@pytest.fixture
def live_gateway(tmp_path):
    root = tmp_path / "owners/local/main/workspace/runtime/services/gateway"
    root.mkdir(parents=True)
    record = {**build_process_identity(), "kind": "my-agent-gateway"}
    (root / "gateway.pid").write_text(json.dumps(record), encoding="utf-8")
    return tmp_path


def _deny(command: str, data_root):
    return active_service_stop_denial(command, data_root=data_root, foreground_admin_full_access=True)


@pytest.mark.parametrize("command", [
    f'kill -TERM {PID}; echo "kill_exit=$?"',
    f"true && kill {PID}",
    f"false || kill -9 {PID}",
    f"echo x | kill {PID}",
    f"kill -s TERM {PID}",
    f"kill -- {PID}",
    f"(kill {PID})",
    f"kill {PID}&",
    f"kill {PID}>/dev/null 2>&1",
    f"sudo -n kill {PID}",
    f"env A=1 /bin/kill -HUP {PID}",
    f"echo start\nkill {PID}",
    f"cat > note.md <<'EOF'\ndon't stop\nEOF\nkill {PID}",
    f"kill -- -{PGID}",
    "kill -9 -1",
    "ls; my-agent gateway stop",
    "cd /x && my-agent gateway restart",
    "python -m agent_py_agent gateway stop",
])
def test_a_live_service_target_in_any_segment_is_refused(live_gateway, command: str) -> None:
    denial = _deny(command, live_gateway)
    assert denial is not None and denial["error_code"] == "ACTIVE_SERVICE_SELF_STOP_BLOCKED"
    assert denial["effect_outcome"] == "not_started" and denial["role"] == "gateway"
    assert "restart_gateway" in denial["next_step"]


@pytest.mark.parametrize("command", [
    "kill $(lsof -ti :18429)",
    "kill `cat gateway.pid`",
    'kill "$PID"',
    "lsof -ti :18429 | xargs kill",
])
def test_targets_known_only_at_run_time_are_refused_while_a_service_runs(live_gateway, command: str) -> None:
    denial = _deny(command, live_gateway)
    assert denial is not None and denial["error_code"] == "ACTIVE_SERVICE_SELF_STOP_BLOCKED"
    assert "字面进程号" in denial["next_step"]


@pytest.mark.parametrize("command", [
    "kill -TERM 999999999; echo done",
    "ls -la; echo kill",
    "grep -n kill gateway.log",
    "kill -l",
    "kill %1",
    "my-agent gateway status",
    "echo my-agent gateway",
    "ps -o pid,command",
])
def test_unrelated_commands_and_other_targets_pass(live_gateway, command: str) -> None:
    assert _deny(command, live_gateway) is None


def test_nothing_is_refused_without_a_live_service_or_full_access(tmp_path, live_gateway) -> None:
    assert _deny("kill $(cat gateway.pid); my-agent gateway stop", tmp_path / "no-service") is None
    assert active_service_stop_denial(f"kill {PID}; echo", data_root=live_gateway) is None


def test_terminal_session_write_data_is_part_of_the_guarded_text(live_gateway) -> None:
    text = guarded_command_text("terminal_session", {"action": "write", "data": f"kill -TERM {PID}"})
    assert _deny(text, live_gateway) is not None
    assert guarded_command_text("run_command", {"command": "ls", "data": f"kill {PID}"}) == "ls"
