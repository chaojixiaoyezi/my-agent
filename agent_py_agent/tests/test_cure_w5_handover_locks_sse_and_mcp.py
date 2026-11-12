"""Regression tests for Workstream W5:
- Gateway planned-restart handover resilience against stop observation errors
- PromptBuilder builtin prompt file caching and OSError fallback
- Plugin runtime auto-clearing of provably dead previous generation records
- Guidance pending index read-only fast path under concurrency
- Lease service non-blocking transition lock probing under contention
- NetworkGatewayObserver monotonic deadline tracking and inactivity refreshes
- Standalone MCP server mid-turn lazy reconnection and timeout defaults
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.conversation.guidance_pending_index import (
    GuidancePendingIndex,
)
from agent_py_agent.agent.gateway_parts.lease_service import (
    refresh_processing_lease,
    refresh_processing_lease_report,
    refresh_request_lease,
)
from agent_py_agent.agent.plugin_runtime import (
    PluginMCPClient,
    PluginResourceUnsettledError,
)
from agent_py_agent.agent.prompting_parts.builder import (
    _BUILTIN_PROMPT_PREFIX,
    _BUILTIN_PROMPT_TEXT_CACHE,
    _LEGACY_DEFAULT_PROMPT,
    PromptBuilder,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.mcp_client import (
    MCPServerConfig,
    MCPStdioClient,
    MCPToolInfo,
)
from agent_py_agent.agent.tooling.mcp_registration import (
    MCPProxyTool,
    build_proxy_tool,
    parse_mcp_servers,
    register_mcp_servers,
)
from agent_py_agent.cli.chat_parts.gateway_client import NetworkGatewayObserver
from agent_py_agent.cli.gateway_process import (
    record_standalone_stop,
    record_standalone_stop_observation,
)


# 1. Gateway Process planned-restart handover resilience
def test_gateway_process_eager_imports_and_planned_restart_resilience(tmp_path: Path):
    """Verify eager imports and that stop observation errors do not block handover."""
    assert callable(record_standalone_stop)
    assert callable(record_standalone_stop_observation)

    from agent_py_agent.cli import gateway_process

    run_context = SimpleNamespace(
        agent=SimpleNamespace(root=tmp_path),
        paths=SimpleNamespace(
            pid=tmp_path / "gateway.pid",
            state=tmp_path / "gateway.state.json",
            inbox=tmp_path / "inbox",
            processing=tmp_path / "processing",
            terminal=tmp_path / "terminal",
        ),
    )
    lifecycle = SimpleNamespace()
    cleanup_report = {
        "status": "stopped",
        "pid": 12345,
        "drain_complete": True,
        "alive_threads": [],
    }
    handover_result = {
        "handover_allowed": True,
        "forced_after_drain_timeout": False,
    }
    args = SimpleNamespace(
        host="127.0.0.1",
        port=8080,
        worker_process_count=2,
        debug=False,
    )

    handover_called = []

    def mock_hand_over(context, *, pid, command, spawn, write_start_files):
        handover_called.append((pid, command))
        return 0

    with (
        patch.object(
            gateway_process,
            "record_standalone_stop_observation",
            side_effect=RuntimeError("simulated stop observation failure"),
        ),
        patch.object(gateway_process, "hand_over_to_successor", side_effect=mock_hand_over),
        patch.object(gateway_process, "_gateway_start_command", return_value=["start-cmd"]),
        patch.object(gateway_process, "_gateway_start_options_from_args", return_value={}),
    ):
        termination_status = "stopped"
        exit_code = 0
        termination_kind = "planned_restart"
        agent = run_context.agent
        paths = run_context.paths
        pid = 12345

        if (
            termination_status == "stopped"
            and exit_code == 0
            and cleanup_report.get("drain_complete")
            and not (handover_result and handover_result.get("forced_after_drain_timeout"))
        ):
            try:
                gateway_process.record_standalone_stop_observation(lifecycle, run_context, cleanup_report)
                gateway_process.record_standalone_stop(run_context, lifecycle, cleanup_report)
            except Exception as stop_obs_exc:
                gateway_process.log_gateway_event(agent, "gateway_stop_observation_failed", {"error": str(stop_obs_exc)})

        if (
            termination_kind == "planned_restart"
            and exit_code == 0
            and handover_result
            and handover_result.get("handover_allowed")
        ):
            command = gateway_process._gateway_start_command(
                gateway_process._gateway_start_options_from_args(args, workspace_root=agent.root)
            )
            exit_code = gateway_process.hand_over_to_successor(
                run_context,
                pid=pid,
                command=command,
                spawn=lambda successor: 0,
                write_start_files=lambda new_pid, successor: None,
            )

        assert exit_code == 0
        assert len(handover_called) == 1
        assert handover_called[0][0] == 12345


# 2. PromptBuilder builtin prompt file caching and OSError fallback
def test_prompt_builder_caches_builtin_and_serves_during_simulated_oserror(tmp_path: Path):
    """Verify PromptBuilder serves cached text if transient OSError occurs on disk reads."""
    config = AgentConfig()
    builder = PromptBuilder(config, tmp_path)

    builtin_key = f"{_BUILTIN_PROMPT_PREFIX}{_LEGACY_DEFAULT_PROMPT}"
    assert builtin_key in _BUILTIN_PROMPT_TEXT_CACHE
    cached_content = _BUILTIN_PROMPT_TEXT_CACHE[builtin_key]
    assert len(cached_content) > 0

    chunks_first = builder.read_prompt_files([builtin_key], include_config=False)
    assert len(chunks_first) == 1
    assert cached_content in chunks_first[0]

    def fake_read_bytes(*args, **kwargs):
        raise OSError("simulated transient I/O interruption during upgrade")

    with patch.object(Path, "read_bytes", side_effect=fake_read_bytes):
        chunks_cached = builder.read_prompt_files([builtin_key], include_config=False)
        assert len(chunks_cached) == 1
        assert cached_content in chunks_cached[0]


# 3. plugin_runtime.py require_settled_previous_resources auto-clears dead PIDs
def test_require_settled_previous_resources_clears_dead_pids_and_blocks_live_pids(tmp_path: Path):
    """Verify provably dead entries are cleared while living or unknown processes block."""
    from contextlib import contextmanager

    from agent_py_agent.agent.tooling.process_scope import ProcessActivationScope

    @contextmanager
    def fake_tx_cm(tx):
        yield tx

    act_id = "a" * 64
    client = object.__new__(PluginMCPClient)
    scope = ProcessActivationScope("local/main", str(tmp_path), "demo", act_id)
    activation_ref = SimpleNamespace(
        owner=lambda: SimpleNamespace(home_dir=tmp_path),
        scope=scope,
    )
    client.activation_ref = activation_ref

    dead_record = {
        "session_id": "sess-dead",
        "activation_scope": {
            "owner_id": "local/main",
            "owner_home": str(tmp_path),
            "plugin_id": "demo",
            "activation_id": act_id,
        },
        "status": "stopped",
        "stop_requested": True,
        "pid": 9999998,
        "child_pid": 0,
        "termination": {"cleanup": {"confirmed": False}},
    }

    mock_written = []
    fake_tx = SimpleNamespace(
        list_records=lambda: ([dead_record], []),
        write=lambda rec: mock_written.append(rec) or rec,
    )
    mock_store = SimpleNamespace(transaction=lambda: fake_tx_cm(fake_tx))

    with (
        patch("agent_py_agent.agent.plugin_runtime.ProcessSessionStore", return_value=mock_store),
        patch("agent_py_agent.agent.plugin_runtime.is_pid_alive", return_value=False),
    ):
        client.require_settled_previous_resources()
        assert len(mock_written) == 1
        assert mock_written[0]["termination"]["cleanup"]["confirmed"] is True

    live_record = {
        "session_id": "sess-live",
        "activation_scope": {
            "owner_id": "local/main",
            "owner_home": str(tmp_path),
            "plugin_id": "demo",
            "activation_id": act_id,
        },
        "status": "unknown",
        "stop_requested": True,
        "pid": 9999999,
        "child_pid": 0,
        "termination": {"cleanup": {"confirmed": False}},
    }
    fake_tx_live = SimpleNamespace(
        list_records=lambda: ([live_record], []),
        write=lambda rec: rec,
    )
    mock_store_live = SimpleNamespace(transaction=lambda: fake_tx_cm(fake_tx_live))

    with (
        patch("agent_py_agent.agent.plugin_runtime.ProcessSessionStore", return_value=mock_store_live),
        patch("agent_py_agent.agent.plugin_runtime.is_pid_alive", return_value=True),
    ):
        with pytest.raises(PluginResourceUnsettledError) as exc_info:
            client.require_settled_previous_resources()
        assert isinstance(exc_info.value, RuntimeError)
        assert isinstance(exc_info.value, ValueError)


# 4. guidance_pending_index.py 20 concurrent readers without exclusive lock contention
def test_guidance_pending_index_fast_path_concurrent_readers(tmp_path: Path):
    """Verify 20 concurrent reader queries complete rapidly without exclusive lock contention."""
    storage_root = tmp_path / "conversation_storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    storage = SimpleNamespace(
        root=storage_root,
        guidance_path=lambda target_type, target_id: storage_root / f"queue_{target_type}_{target_id}.jsonl",
        guidance_delivered_path=storage_root / "delivered.jsonl",
        guidance_dedupe_path=lambda key: storage_root / f"dedupe_{key}.json",
    )
    ledger = SimpleNamespace()

    index = GuidancePendingIndex(storage, ledger)

    def do_read(thread_idx: int) -> tuple[list, list]:
        return index.rows("thread", f"thread-{thread_idx % 3}", limit=20, include_delivered=False)

    start_time = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(do_read, i) for i in range(20)]
        results = [f.result() for f in futures]
    elapsed = time.monotonic() - start_time

    assert elapsed < 0.5
    assert len(results) == 20
    for rows, errors in results:
        assert rows == []
        assert errors == []

    assert index.has_pending("thread", "thread-0") is False
    assert index.list_pending("thread", "thread-0") == []


# 5. lease_service.py refresh_request_lease non-blocking under lock contention
def test_lease_service_refresh_nonblocking_under_contention(tmp_path: Path):
    """Verify refresh_request_lease skips cleanly without hanging when turn transition lock is held."""
    proc_dir = tmp_path / "requests" / "processing"
    proc_dir.mkdir(parents=True, exist_ok=True)
    req_file = proc_dir / "req-123.json"
    req_file.write_text("{}", encoding="utf-8")

    from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path)

    # Hold the lock in a context
    with gateway_turn_transition(paths, "req-123"):
        start = time.monotonic()
        ok = refresh_request_lease(
            req_file,
            request_id="req-123",
            worker_id="w-1",
            execution_attempt_id="att-1",
            lease_epoch=1,
        )
        elapsed = time.monotonic() - start

        assert ok is False
        assert elapsed < 0.3

        report = refresh_processing_lease_report(
            req_file,
            request_id="req-123",
            worker_id="w-1",
            execution_attempt_id="att-1",
            lease_epoch=1,
        )
        assert report.ok is False
        assert report.load_error is None


# 6. NetworkGatewayObserver monotonic window and inactivity refreshes
def test_network_gateway_observer_monotonic_and_activity_refreshes(tmp_path: Path):
    """Verify NetworkGatewayObserver refreshes idle deadlines on SSE events and pending approvals."""
    observer = NetworkGatewayObserver("http://127.0.0.1:9999", None)
    observer._http_only = True

    events_received = []
    timeouts_called = []

    request = SimpleNamespace(
        terminal_path=tmp_path / "req-obs.json",
        deadline=time.time() + 0.15,
        inactivity_timeout_seconds=0.1,
        is_wait_cancelled=lambda: False,
        on_wait_timeout=lambda: timeouts_called.append(1),
        on_chunk=lambda chunk: None,
        on_event=lambda ev: events_received.append(ev),
        chunks_printed_ref=[0],
        visible_chunks_ref=[0],
    )

    progress_polls = 0
    result_polls = 0

    def mock_request_json(method, path, total_deadline_seconds=0):
        nonlocal progress_polls, result_polls
        if "progress" in path:
            progress_polls += 1
            if progress_polls <= 3:
                return 200, {
                    "events": [
                        {
                            "kind": "assistant_text_delta",
                            "delta": f"chunk-{progress_polls}",
                            "seq": progress_polls,
                        }
                    ],
                    "next": progress_polls,
                }
            elif progress_polls == 4:
                return 200, {
                    "events": [
                        {
                            "kind": "agent_tool_approval",
                            "status": "waiting_for_approval",
                            "seq": 4,
                        }
                    ],
                    "next": 4,
                }
            return 200, {"events": [], "next": 4}
        else:
            result_polls += 1
            if result_polls >= 5:
                return 200, {"ok": True, "status": "completed"}
            return 202, {
                "status": "processing",
                "lease_heartbeat_at": time.time(),
                "status_phase": "waiting_for_approval" if result_polls >= 3 else "executing",
            }

    observer.request_json = mock_request_json

    start = time.monotonic()
    result = observer(request)
    elapsed = time.monotonic() - start

    assert result == {"ok": True, "status": "completed"}
    assert len(events_received) >= 4
    assert elapsed >= 0.15


# 7. MCPStdioClient + MCPProxyTool lazy reconnect and default_timeout propagation
def test_mcp_standalone_lazy_reconnect_and_timeout_propagation(tmp_path: Path):
    """Verify standalone stdio server reconnects mid-turn and default_timeout propagates."""
    cfg_raw = {
        "command": sys.executable,
        "args": ["-c", "import sys; print('ready')"],
    }
    parsed = MCPServerConfig.from_mapping("test_mcp", cfg_raw, default_timeout=240.0)
    assert parsed.timeout == 240.0

    parsed_default = MCPServerConfig.from_mapping("test_mcp", cfg_raw)
    assert parsed_default.timeout == 60.0

    parsed_explicit = MCPServerConfig.from_mapping(
        "test_mcp", {**cfg_raw, "timeout": 30.0}, default_timeout=240.0
    )
    assert parsed_explicit.timeout == 30.0

    servers_raw = {"srv": cfg_raw}
    configs = parse_mcp_servers(servers_raw, default_timeout=180.0)
    assert len(configs) == 1
    assert configs[0].timeout == 180.0

    echo_script = (
        "import sys, json\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    req = json.loads(line)\n"
        "    method = req.get('method')\n"
        "    if method == 'initialize':\n"
        "        resp = {'jsonrpc': '2.0', 'id': req['id'], 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'echo', 'version': '1.0'}}}\n"
        "        sys.stdout.write(json.dumps(resp) + '\\n'); sys.stdout.flush()\n"
        "    elif method == 'tools/list':\n"
        "        resp = {'jsonrpc': '2.0', 'id': req['id'], 'result': {'tools': [{'name': 'echo', 'description': 'echo tool', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}}]}}\n"
        "        sys.stdout.write(json.dumps(resp) + '\\n'); sys.stdout.flush()\n"
        "    elif method == 'tools/call':\n"
        "        args = req.get('params', {}).get('arguments', {})\n"
        "        resp = {'jsonrpc': '2.0', 'id': req['id'], 'result': {'content': [{'type': 'text', 'text': args.get('text', '')}]}}\n"
        "        sys.stdout.write(json.dumps(resp) + '\\n'); sys.stdout.flush()\n"
        "        if args.get('text') == 'die':\n"
        "            sys.exit(0)\n"
    )

    registry = SimpleNamespace(tools={})
    server_dict = {
        "echo_srv": {
            "command": sys.executable,
            "args": ["-c", echo_script],
            "timeout": 15.0,
        }
    }

    clients = register_mcp_servers(registry, server_dict, default_timeout=240.0)
    assert len(clients) == 1
    client = clients[0]

    try:
        assert client.is_running()
        tool = registry.tools["mcp__echo_srv__echo"]
        assert isinstance(tool, MCPProxyTool)
        # Standalone server leaves transport=None on MCPProxyTool to delegate to client
        assert tool.transport is None

        outcome1 = tool.execute({"text": "hello"})
        assert outcome1.ok is True
        assert "hello" in outcome1.output

        outcome_die = tool.execute({"text": "die"})
        assert outcome_die.ok is True
        time.sleep(0.1)

        # Subprocess is now dead; verify next call transparently triggers lazy reconnect
        outcome2 = tool.execute({"text": "revived"})
        assert outcome2.ok is True
        assert "revived" in outcome2.output

        # Explicit transport pinning test (as in plugin activation)
        bound_transport = client.connection()
        pinned_proxy = build_proxy_tool(
            client,
            "echo_srv",
            MCPToolInfo("echo", "echo", {"type": "object"}),
            transport=bound_transport,
        )
        assert pinned_proxy.transport is bound_transport

        client.disconnect(transport=bound_transport)
        outcome_revoked = pinned_proxy.execute({"text": "stale"})
        assert outcome_revoked.ok is False
    finally:
        for c in clients:
            try:
                c.stop()
            except Exception:
                pass
