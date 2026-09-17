from __future__ import annotations

"""Managed background process session and multi-user scope contracts."""

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.tooling.process_registry import ProcessRegistration, process_registry
from agent_py_agent.agent.tooling.process_session_store import (
    PROCESS_SESSION_SCHEMA,
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_approved_registry_test_call,
    execute_registry_test_call,
    runtime_snapshot_for_tools,
)


@pytest.fixture(autouse=True)
def _clear_process_registry():
    process_registry.clear()
    yield
    for row in process_registry.list():
        if row["status"] == "running":
            process_registry.kill(row["session_id"])
    process_registry.clear()


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=100,
            max_matches=50,
            web_max_chars=6000,
            http_timeout=30,
            catalog_limit=30,
            retrieval_limit=5,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            owner_scope_root=str(root),
            operation_store_required=False,
        )
    )


def test_owned_process_stop_is_session_continuation_not_second_approval(
    tmp_path: Path,
) -> None:
    tool = ProcessSessionTool(owner_scope_root=tmp_path, workspace_root=tmp_path)
    snapshot = runtime_snapshot_for_tools(
        {tool.model_spec.name: tool},
        run_id="run-owned-stop",
    )
    call = canonical_test_call(
        snapshot,
        tool.model_spec.name,
        {"action": "stop", "session_id": "bg-owned"},
    )

    decision = ActionPolicy().decide(
        ActionPolicyRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=tmp_path,
            workspace_roots=(tmp_path,),
            owner_scope_root=str(tmp_path),
        )
    )

    assert decision.status == "allow"
    assert decision.resolved_effect == "mutating"


def _call(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, object],
    scope: dict[str, object],
):
    executor = (
        execute_approved_registry_test_call
        if tool_name == "run_command" and bool(arguments.get("run_in_background"))
        else execute_registry_test_call
    )
    return executor(
        registry,
        tool_name,
        arguments,
        trusted_run_context={"run_scope": scope},
        run_id=str(scope["run_id"]),
    )


def _payload(result) -> dict[str, object]:
    assert result.ok, result.output
    start = result.output.find("{")
    end = result.output.rfind("}")
    assert start >= 0 and end >= start, result.output
    return json.loads(result.output[start : end + 1])


def test_process_session_wait_replaces_shell_sleep_polling(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    scope = {
        "owner_id": "owner-a",
        "session_id": "thread-a",
        "root_task_id": "task-a",
        "run_id": "run-a",
    }
    command = "printf 'started\\n'; sleep 0.8; printf 'done\\n'"

    started = _payload(
        _call(
            registry,
            "run_command",
            {"command": command, "run_in_background": True},
            scope,
        )
    )
    assert started["status"] == "started"
    assert "pid" not in started and "process_pid" not in started
    assert "process_session" in started["hint"]
    assert "sleep" in started["hint"]

    finished = _payload(
        _call(
            registry,
            "process_session",
            {
                "action": "wait",
                "session_id": started["session_id"],
                "timeout_seconds": 3,
            },
            scope,
        )
    )
    assert finished["status"] == "exited"
    assert finished["exit_code"] == 0
    assert finished["wait_timed_out"] is False
    assert "started" in finished["output_tail"]
    assert "done" in finished["output_tail"]
    assert "pid" not in finished and "process_pid" not in finished


def test_long_wait_is_cancellable_without_killing_background_command(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    record = process_registry.register(ProcessRegistration(
        command="test long wait", pid=process.pid, output_file="", process=process,
    ))
    token = CancellationToken()
    timer = threading.Timer(0.1, token.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with bind_cancellation_token(token), pytest.raises(ToolCancelled):
            process_registry.wait(record.session_id, 600)
        assert time.monotonic() - started < 2
        assert process.poll() is None
        assert process_registry.status(record.session_id)["status"] == "running"
    finally:
        timer.cancel()
        process_registry.kill(record.session_id)


def test_long_wait_budget_exposed_and_bounded() -> None:
    assert ProcessSessionTool._wait_timeout(600) == 600
    assert ProcessSessionTool._wait_timeout(999999) == 600
    assert ProcessSessionTool._wait_timeout(-2) == 0
    assert ProcessSessionTool._wait_timeout(None) == 30


def test_wait_timeout_does_not_terminate_process() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    record = process_registry.register(ProcessRegistration(
        command="test wait deadline", pid=process.pid, output_file="", process=process,
    ))
    try:
        result = process_registry.wait(record.session_id, 0.01)
        assert result["wait_timed_out"] is True
        assert result["status"] == "running"
        assert process.poll() is None
    finally:
        process_registry.kill(record.session_id)


def test_process_summary_uses_session_id_as_only_management_handle(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    scope = {
        "owner_id": "owner-a",
        "session_id": "thread-a",
        "root_task_id": "task-a",
        "run_id": "run-a",
    }
    started = _payload(
        _call(
            registry,
            "run_command",
            {"command": "echo managed", "run_in_background": True},
            scope,
        )
    )

    result = _call(
        registry, "process_session", {"action": "status", "session_id": started["session_id"]}, scope,
    )
    status = _payload(result)

    assert status["session_id"] == started["session_id"]
    assert "pid" not in status and "process_pid" not in status
    observation = result.metadata["handler_details"]["progress_observation"]
    assert len(observation["sha256"]) == 64
    assert observation["pending"] is (status["status"] == "running")


def test_process_wait_soft_observation_ignores_elapsed_time(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.tool_guard.call_guardrail import (
        record_tool_guard_observation,
        tool_guardrail_records,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts

    tool = ProcessSessionTool(owner_scope_root=tmp_path, workspace_root=tmp_path)
    snapshot = runtime_snapshot_for_tools({"process_session": tool}, run_id="run-a")
    params = SimpleNamespace(task_attributes={"repeated_success_hint_threshold": 5}, tool_runtime_snapshot=snapshot)
    agent = SimpleNamespace()
    payload = {"session_id": "bg-pending", "status": "running", "output_tail": "", "output_bytes": 0}
    monkeypatch.setattr(process_registry, "wait", lambda *_: dict(payload))
    hints = []
    raw_hashes = set()
    for index in range(33):
        payload.update(uptime_seconds=index * 30, wait_timed_out=True)
        args = {"action": "wait", "session_id": "bg-pending", "timeout_seconds": 30}
        call = canonical_test_call(snapshot, "process_session", args, call_id=f"poll-{index}")
        outcome = tool.execute({**args, "__run_scope": {"owner_id": "owner-a", "session_id": "thread-a"}})
        result = ToolResult.succeeded(call, outcome.output, facts=ToolSuccessFacts(
            metadata={"handler_details": outcome.result_envelope},
        ))
        hints.append(record_tool_guard_observation(agent, params, call, result))
        raw_hashes.add(tool_guardrail_records(agent)[-1]["result_hash"])
    assert len(raw_hashes) == 33  # 原始审计保留时长差异，软观察不再把它当进展。
    assert [i + 1 for i, hint in enumerate(hints) if hint] == [5, 10, 15, 20, 25, 30]
    assert "不代表进程卡死" in hints[4]
    payload.update(output_bytes=10, output_tail="new output", uptime_seconds=1000)
    outcome = tool.execute({**args, "__run_scope": {"owner_id": "owner-a", "session_id": "thread-a"}})
    result = ToolResult.succeeded(call, outcome.output, facts=ToolSuccessFacts(
        metadata={"handler_details": outcome.result_envelope},
    ))
    assert not record_tool_guard_observation(agent, params, call, result)
    assert tool_guardrail_records(agent)[-1]["repeated_success_count"] == 1


def test_process_progress_observation_changes_with_log_growth_and_exit(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling.process_registry import BackgroundProcess

    log = tmp_path / "output.log"
    log.write_text("x" * 8000, encoding="utf-8")
    record = BackgroundProcess("bg-log", "build", 0, time.time(), output_file=str(log))
    tool = ProcessSessionTool(owner_scope_root=tmp_path, workspace_root=tmp_path)
    first = record.to_summary(include_output=True)
    log.write_text("x" * 9000, encoding="utf-8")
    second = record.to_summary(include_output=True)
    assert first["output_tail"] == second["output_tail"]
    assert first["output_bytes"] < second["output_bytes"]
    left = tool._ok(first, observe_progress=True).result_envelope["progress_observation"]
    right = tool._ok(second, observe_progress=True).result_envelope["progress_observation"]
    assert left["sha256"] != right["sha256"]
    assert left["pending"] is True
    record.status, record.exit_code = "exited", 0
    ended = tool._ok(record.to_summary(include_output=True), observe_progress=True)
    assert ended.result_envelope["progress_observation"]["pending"] is False
    assert ended.result_envelope["progress_observation"]["sha256"] != right["sha256"]


def test_process_session_hides_other_tui_session(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    scope_a = {
        "owner_id": "owner-a",
        "session_id": "thread-a",
        "root_task_id": "task-a",
        "run_id": "run-a",
    }
    scope_b = {
        "owner_id": "owner-a",
        "session_id": "thread-b",
        "root_task_id": "task-b",
        "run_id": "run-b",
    }
    started = _payload(
        _call(
            registry,
            "run_command",
            {"command": "echo scoped", "run_in_background": True},
            scope_a,
        )
    )

    denied = _call(
        registry,
        "process_session",
        {"action": "status", "session_id": started["session_id"]},
        scope_b,
    )
    assert denied.ok is False
    assert denied.error_code == "PROCESS_NOT_FOUND"
    assert _payload(_call(registry, "process_session", {"action": "list"}, scope_b)) == {
        "processes": []
    }


def test_process_session_stop_terminates_owned_process_tree(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    scope = {
        "owner_id": "owner-a",
        "session_id": "thread-a",
        "root_task_id": "task-a",
        "run_id": "run-a",
    }
    command = "sleep 20"
    started = _payload(
        _call(
            registry,
            "run_command",
            {"command": command, "run_in_background": True},
            scope,
        )
    )
    # 启动与停止都经过同一 host-bound executor；不能用测试传入的 owner-a
    # 直接冒充执行器已校准的身份，否则验证的是越权拒绝而不是进程回收。
    stopped = _call(
        registry,
        "process_session",
        {
            "action": "stop",
            "session_id": started["session_id"],
        },
        scope,
    )

    assert stopped.ok is True
    payload = _payload(stopped)
    assert payload["status"] == "killed"
    assert process_registry.status(started["session_id"])["status"] == "killed"


def test_process_session_unconfirmed_stop_is_not_success(monkeypatch):
    monkeypatch.setattr(process_registry, "kill", lambda *args: {
        "session_id": "bg-test", "status": "running", "termination": {"confirmed": False}
    })
    result = ProcessSessionTool().execute({
        "action": "stop", "session_id": "bg-test",
        "__run_scope": {"owner_id": "owner-a", "session_id": "thread-a", "run_id": "run-a", "root_task_id": "task-a"},
    })
    assert result.ok is False
    assert result.effect_outcome == "unknown"
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"


def test_process_session_requires_host_bound_scope(tmp_path: Path) -> None:
    result = ProcessSessionTool(str(tmp_path)).execute({"action": "list"})

    assert result.ok is False
    assert result.error_code == "OWNER_SCOPE_UNAVAILABLE"


def test_process_session_is_registered_in_model_manifest(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    names = {spec.name for spec in registry.model_visible_specs()}

    assert "process_session" in names


def test_process_session_follows_background_command_tool_profiles() -> None:
    """能启动后台命令的主/子代理都必须同时拿到唯一的续接工具。"""
    from agent_py_agent.agent.agent_core.orchestration.tool_grants import (
        CODING_SUBAGENT_TOOLS,
        READ_ONLY_SUBAGENT_TOOLS,
    )
    from agent_py_agent.agent.conversation.runtime import (
        DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    )

    assert "process_session" in DEFAULT_BACKGROUND_ALLOWED_TOOLS
    assert "process_session" in CODING_SUBAGENT_TOOLS
    assert "process_session" not in READ_ONLY_SUBAGENT_TOOLS


def test_managed_network_status_never_promotes_local_listener_to_lan_success(
    monkeypatch,
) -> None:
    """0.0.0.0 与主机放行都只是本机证据，仍必须由另一台机器真实探测。"""
    from agent_py_agent.agent.tooling import process_network_status

    monkeypatch.setattr(
        process_network_status,
        "_managed_listener_bindings",
        lambda _record, requested_port: (
            [{"host": "0.0.0.0", "port": requested_port, "scope": "non_loopback"}],
            "observed",
        ),
    )
    monkeypatch.setattr(
        process_network_status,
        "_host_firewall_observation",
        lambda _bindings: {
            "status": "explicitly_allowed",
            "ports": [3000],
            "active_zones": ["public"],
            "explicit_rules": [{"zone": "public", "port": 3000, "protocol": "tcp"}],
        },
    )

    payload = process_network_status.managed_process_network_status(
        SimpleNamespace(session_id="bg-network", status="running"),
        requested_port=3000,
    )

    assert payload["schema"] == "managed_process_network_status.v1"
    assert payload["listener_pid_evidence"] == {
        "source": "host_kernel_process_tree",
        "authority": "observed",
        "sandbox_visibility": "may_be_hidden",
    }
    assert "ps/lsof" in payload["evidence_boundary"]
    assert payload["lan_reachability"] == "unverified_external_probe_required"
    assert payload["external_probe_required"] is True
    assert payload["host_firewall"]["status"] == "explicitly_allowed"


def test_managed_network_status_reports_loopback_only(monkeypatch) -> None:
    """只监听 127.0.0.1 时直接指出局域网不可用，不运行防火墙写操作。"""
    from agent_py_agent.agent.tooling import process_network_status

    monkeypatch.setattr(
        process_network_status,
        "_managed_listener_bindings",
        lambda _record, requested_port: (
            [{"host": "127.0.0.1", "port": requested_port, "scope": "loopback"}],
            "observed",
        ),
    )

    payload = process_network_status.managed_process_network_status(
        SimpleNamespace(session_id="bg-loopback", status="running"),
        requested_port=8080,
    )

    assert payload["lan_reachability"] == "loopback_only"
    assert payload["external_probe_required"] is False
    assert payload["host_firewall"] == {"status": "not_applicable", "ports": []}


@pytest.mark.parametrize("return_code", [None, 29, 251, 252, 253])
def test_firewall_query_failure_is_not_reported_as_stopped(monkeypatch, return_code) -> None:
    from agent_py_agent.agent.tooling import process_network_status as network

    monkeypatch.setattr(network.shutil, "which", lambda _name: "/usr/bin/firewall-cmd")
    result = None if return_code is None else subprocess.CompletedProcess([], return_code, "", "")
    monkeypatch.setattr(network, "_firewall_command", lambda *_args: result)
    observed = network._host_firewall_observation([{"port": 18778}])
    assert observed["status"] == ("not_running" if return_code == 252 else "unknown")


@pytest.mark.parametrize("zone_result", [None, (29, ""), (0, "")])
def test_firewall_missing_zone_evidence_stays_unknown(monkeypatch, zone_result) -> None:
    from agent_py_agent.agent.tooling import process_network_status as network

    monkeypatch.setattr(network.shutil, "which", lambda _name: "/usr/bin/firewall-cmd")
    replies = iter([
        subprocess.CompletedProcess([], 0, "running", ""),
        None if zone_result is None else subprocess.CompletedProcess([], *zone_result, ""),
    ])
    monkeypatch.setattr(network, "_firewall_command", lambda *_args: next(replies))
    assert network._host_firewall_observation([{"port": 18778}])["status"] == "unknown"


@pytest.mark.parametrize(
    ("codes", "expected"),
    [((0, 0), "explicitly_allowed"), ((0, 1), "partially_allowed"),
     ((1, 1), "not_explicitly_allowed"), ((0, 29), "unknown"), ((0, None), "unknown")],
)
def test_firewall_port_summary_requires_every_zone_port_observation(monkeypatch, codes, expected) -> None:
    from agent_py_agent.agent.tooling import process_network_status as network

    monkeypatch.setattr(network.shutil, "which", lambda _name: "/usr/bin/firewall-cmd")
    replies = iter([
        subprocess.CompletedProcess([], 0, "running", ""),
        subprocess.CompletedProcess([], 0, "public\n  interfaces: eth0\n", ""),
        *(None if code is None else subprocess.CompletedProcess([], code, "", "") for code in codes),
    ])
    monkeypatch.setattr(network, "_firewall_command", lambda *_args: next(replies))
    observed = network._host_firewall_observation([{"port": 18081}, {"port": 18778}])
    assert observed["status"] == expected
    assert len(observed["port_observations"]) == 2
    assert len(observed["explicit_rules"]) == codes.count(0)
    assert "nftables" in observed["evidence_boundary"]


def test_firewall_command_preserves_exit_code_and_marks_timeout(monkeypatch) -> None:
    from agent_py_agent.agent.tooling import process_network_status as network

    denied = subprocess.CompletedProcess([], 253, "", "denied")
    monkeypatch.setattr(network.subprocess, "run", lambda *_args, **_kwargs: denied)
    assert network._firewall_command("firewall-cmd", ("--state",)) is denied
    monkeypatch.setattr(
        network.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("firewall-cmd", 1.5)),
    )
    assert network._firewall_command("firewall-cmd", ("--state",)) is None


def test_proc_listener_reports_actual_socket_owner_pids(tmp_path: Path) -> None:
    """监听行展示持有 socket 的真实 PID，不把托管 host 或命令入口冒充监听者。"""
    from agent_py_agent.agent.tooling.process_network_status import _proc_tcp_listeners

    proc_tcp = tmp_path / "tcp"
    proc_tcp.write_text(
        "  sl  local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
        "   0: 00000000:46A1 00000000:0000 0A 00000000:00000000 "
        "00:00000000 00000000 0 0 4242 1\n",
        encoding="ascii",
    )

    bindings = _proc_tcp_listeners(
        proc_tcp,
        family=socket.AF_INET,
        owned_inodes={"4242": {1552016, 1552015}},
        requested_port=18081,
    )

    assert bindings == [
        {
            "host": "0.0.0.0",
            "port": 18081,
            "scope": "non_loopback",
            "listener_pids": [1552015, 1552016],
        }
    ]


def test_background_session_outlives_one_shot_launcher_and_is_rehydrated(tmp_path: Path) -> None:
    """子代理式短命 Python 进程退出后，另一个进程仍能查询并停止同一后台会话。"""

    scope = {
        "owner_id": "owner-cross-process",
        "session_id": "thread-cross-process",
        "root_task_id": "task-cross-process",
        "run_id": "subagent-cross-process",
    }
    command = "sleep 20"
    script = "\n".join(
        (
            "import json",
            "from pathlib import Path",
            "from agent_py_agent.agent.tooling.process_registry import ProcessAccessScope",
            "from agent_py_agent.agent.tooling.shell import ShellTool",
            f"root = Path({str(tmp_path)!r})",
            f"command = {command!r}",
            "tool = ShellTool(root)",
            "result = tool._start_background_command(",
            "    command, root, None, None, None,",
            "    ProcessAccessScope('owner-cross-process', 'thread-cross-process', ''),",
            ")",
            "print(result.output, flush=True)",
            "raise SystemExit(0 if result.ok else 1)",
        )
    )
    launcher = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert launcher.returncode == 0, launcher.stderr or launcher.stdout
    started = json.loads(launcher.stdout.strip().splitlines()[-1])

    tool = ProcessSessionTool("", tmp_path)
    deadline = time.monotonic() + 3
    status = None
    while time.monotonic() < deadline:
        status = tool.execute(
            {
                "action": "status",
                "session_id": started["session_id"],
                "__run_scope": scope,
            }
        )
        if status.ok and json.loads(status.output)["status"] == "running":
            break
        time.sleep(0.05)
    assert status is not None and status.ok is True, getattr(status, "output", "")
    assert json.loads(status.output)["status"] == "running"

    stopped = tool.execute(
        {
            "action": "stop",
            "session_id": started["session_id"],
            "__run_scope": scope,
        }
    )
    assert stopped.ok is True
    assert json.loads(stopped.output)["status"] == "killed"


def test_process_session_authority_store_is_outside_owner_sandbox(tmp_path: Path) -> None:
    owner_home = tmp_path / "owners" / "alice"
    workspace = owner_home / "task"
    workspace.mkdir(parents=True)

    store_root = process_session_store_root(workspace, owner_home)

    assert not store_root.is_relative_to(owner_home)
    assert store_root.parent.parent == owner_home.parent / ".my-agent-runtime"


def test_process_session_terminal_state_never_regresses_to_running(tmp_path: Path) -> None:
    store = ProcessSessionStore(tmp_path / "authority")
    running = {
        "schema": PROCESS_SESSION_SCHEMA,
        "session_id": "bg-terminal-monotonic",
        "pid": 12345,
        "pid_birth_token": "birth-a",
        "started_at": 1.0,
        "status": "running",
        "access_scope": {
            "owner_id": "owner-a",
            "conversation_id": "thread-a",
            "owner_home": "/owners/alice",
        },
    }
    store.write(running)
    store.write({**running, "status": "killed", "finished_at": 2.0})

    effective = store.write(running)

    assert effective["status"] == "killed"
    assert store.load("bg-terminal-monotonic").record["status"] == "killed"


def test_configured_background_shell_profile_adds_session_companion() -> None:
    """用户只列 run_command 时也不能得到一张无法续接后台命令的残缺快照。"""
    from agent_py_agent.agent.conversation.runtime import (
        background_tool_policy_decision,
    )

    decision = background_tool_policy_decision(
        config=SimpleNamespace(background_main_agent_allowed_tools=["run_command"])
    )

    assert decision.allowed_tools == ("run_command", "process_session")
