from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.common import authenticated_http
from agent_py_agent.agent.tooling import process_session_cleanup, sandbox, shell
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions, WriteScopeError
from agent_py_agent.agent.tooling._persona_write_guard import _persona_approval_write_error
from agent_py_agent.agent.tooling.process_registry import (
    ProcessTerminationReceipt,
    capture_process_birth_token,
)
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.cli.chat_parts import gateway_client
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    NetworkGatewayObserver,
)
from scripts.private_release import installer


class _DummySSEAgent:
    def _network_gateway_base_url(self) -> str:
        return "http://127.0.0.1:8420"

    def _network_gateway_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-token"}

    def refresh_gateway_network_credentials(self) -> bool:
        return False


class _FakeSSEResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __enter__(self) -> _FakeSSEResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def readline(self, _limit: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


@pytest.fixture(autouse=True)
def _reset_sandbox_binary_probe() -> None:
    sandbox._reset_bwrap_binary_probe_state()
    yield
    sandbox._reset_bwrap_binary_probe_state()


def test_sse_wait_window_expiry_on_heartbeats_does_not_downgrade_http_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE4V-P2-03: normal heartbeat wait-window expiry refreshes without HTTP-only downgrade."""
    fake_now = [0.0]
    sse_opens = 0
    poll_calls = 0
    wait_timeouts = 0

    def fake_monotonic() -> float:
        return fake_now[0]

    def fake_open_authenticated_request(req, timeout=None):  # noqa: ANN001, ARG001
        nonlocal sse_opens, poll_calls
        url = req.full_url
        if "/events?" in url:
            sse_opens += 1
            if sse_opens < 4:
                class _HeartbeatResponse:
                    def __init__(self) -> None:
                        self._lines = [b"event: heartbeat\n", b"data: {}\n", b"\n"]

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                    def readline(self, _limit: int = -1) -> bytes:
                        if self._lines:
                            chunk = self._lines.pop(0)
                            if chunk == b"\n":
                                fake_now[0] += 10.0
                            return chunk
                        return b""

                return _HeartbeatResponse()

            terminal = json.dumps({"result": {"status": "ok", "reply": "done"}}, ensure_ascii=False).encode("utf-8")
            return _FakeSSEResponse(
                [
                    b"id: v1:req-hb:0:1:0\n",
                    b"event: terminal\n",
                    b"data: " + terminal + b"\n",
                    b"\n",
                ]
            )
        poll_calls += 1
        raise AssertionError(f"unexpected fallback HTTP poll: {url}")

    monkeypatch.setattr(gateway_client.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _s: None)
    monkeypatch.setattr(authenticated_http, "open_authenticated_request", fake_open_authenticated_request)

    observer = NetworkGatewayObserver(_DummySSEAgent(), "http://127.0.0.1:8420")

    def on_wait_timeout() -> None:
        nonlocal wait_timeouts
        wait_timeouts += 1

    result = observer(
        GatewayChunkPollRequest(
            chunk_path=tmp_path / "chunks-hb.jsonl",
            terminal_path=tmp_path / "req-hb.json",
            deadline=0.5,
            on_chunk=lambda _line: None,
            chunks_printed_ref=[0],
            visible_chunks_ref=[0],
            inactivity_timeout_seconds=0.5,
            on_wait_timeout=on_wait_timeout,
        )
    )
    assert result == {"status": "ok", "reply": "done"}
    assert sse_opens == 4
    assert wait_timeouts == 3
    assert poll_calls == 0
    assert observer._http_only is False


def test_sse_wait_window_socket_timeout_at_deadline_does_not_downgrade_http_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE4V-P2-03: silent SSE socket TimeoutError at wait deadline leaves _http_only False."""
    fake_now = [0.0]
    sse_opens = 0
    poll_calls = 0
    wait_timeouts = 0

    def fake_monotonic() -> float:
        return fake_now[0]

    def fake_open_authenticated_request(req, timeout=None):  # noqa: ANN001, ARG001
        nonlocal sse_opens, poll_calls
        if "/events?" in req.full_url:
            sse_opens += 1
            if sse_opens < 4:
                class _SilentTimeoutResponse:
                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                    def readline(self, _limit: int = -1) -> bytes:
                        fake_now[0] += 10.0
                        raise TimeoutError("stream read timed out")

                return _SilentTimeoutResponse()

            terminal = json.dumps({"result": {"status": "ok", "reply": "after-timeout"}}, ensure_ascii=False).encode(
                "utf-8"
            )
            return _FakeSSEResponse([b"event: terminal\n", b"data: " + terminal + b"\n", b"\n"])
        poll_calls += 1
        raise AssertionError(f"unexpected fallback HTTP poll: {req.full_url}")

    monkeypatch.setattr(gateway_client.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _s: None)
    monkeypatch.setattr(authenticated_http, "open_authenticated_request", fake_open_authenticated_request)

    observer = NetworkGatewayObserver(_DummySSEAgent(), "http://127.0.0.1:8420")

    def on_wait_timeout() -> None:
        nonlocal wait_timeouts
        wait_timeouts += 1

    result = observer(
        GatewayChunkPollRequest(
            chunk_path=tmp_path / "chunks-to.jsonl",
            terminal_path=tmp_path / "req-to.json",
            deadline=0.5,
            on_chunk=lambda _line: None,
            chunks_printed_ref=[0],
            visible_chunks_ref=[0],
            inactivity_timeout_seconds=0.5,
            on_wait_timeout=on_wait_timeout,
        )
    )
    assert result == {"status": "ok", "reply": "after-timeout"}
    assert sse_opens == 4
    assert wait_timeouts == 3
    assert poll_calls == 0
    assert observer._http_only is False


def test_sse_pre_deadline_eof_and_transport_error_still_fallback_to_http_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE4V-P2-03: genuine SSE stream drops before wait deadline still degrade after 2 failures."""
    fake_now = [0.0]
    sse_opens = 0
    poll_calls = 0

    def fake_open_authenticated_request(req, timeout=None):  # noqa: ANN001, ARG001
        nonlocal sse_opens, poll_calls
        if "/events?" in req.full_url:
            sse_opens += 1
            if sse_opens == 1:
                return _FakeSSEResponse([])
            raise OSError("connection reset by peer")
        poll_calls += 1

        class _JsonPollResponse:
            status = 200

            def __init__(self, target_url: str) -> None:
                self._target_url = target_url

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, _limit: int = -1) -> bytes:
                if "/progress/" in self._target_url:
                    return json.dumps({"events": [], "next": 0}).encode("utf-8")
                return json.dumps({"status": "ok", "reply": "via-poll"}).encode("utf-8")

        return _JsonPollResponse(req.full_url)

    monkeypatch.setattr(gateway_client.time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _s: None)
    monkeypatch.setattr(authenticated_http, "open_authenticated_request", fake_open_authenticated_request)

    observer = NetworkGatewayObserver(_DummySSEAgent(), "http://127.0.0.1:8420")
    result = observer(
        GatewayChunkPollRequest(
            chunk_path=tmp_path / "chunks-eof.jsonl",
            terminal_path=tmp_path / "req-eof.json",
            deadline=10.0,
            on_chunk=lambda _line: None,
            chunks_printed_ref=[0],
            visible_chunks_ref=[0],
            inactivity_timeout_seconds=10.0,
        )
    )
    assert result == {"status": "ok", "reply": "via-poll"}
    assert sse_opens == 2
    assert poll_calls >= 1
    assert observer._http_only is True


def _sample_process_record(
    session_id: str,
    *,
    pid: int,
    birth_token: str,
    prior_receipt: dict[str, object],
    tmp_path: Path,
) -> dict[str, object]:
    now = time.time()
    return {
        "schema": "managed_process_session.v3",
        "session_id": session_id,
        "access_scope": {"owner_id": "local/main", "conversation_id": "", "owner_home": str(tmp_path)},
        "execution_scope": {"owner_home": str(tmp_path), "thread_id": "", "root_task_id": "", "run_id": "", "attempt_id": ""},
        "activation_scope": {"owner_id": "local/main", "owner_home": str(tmp_path), "plugin_id": "sample", "activation_id": "a" * 64},
        "completion_target": {},
        "launcher_pid": os.getpid(),
        "launcher_birth_token": capture_process_birth_token(os.getpid()) or "launcher",
        "pid": pid + 10000,
        "pid_birth_token": "host-bt",
        "child_pid": pid,
        "child_pid_birth_token": birth_token,
        "revision": 0,
        "stop_requested": True,
        "handoff_confirmed": True,
        "child_launch_started": True,
        "reserved_at": now - 30,
        "started_at": now - 29,
        "status": "unknown",
        "exit_code": None,
        "finished_at": None,
        "command": "sleep 10",
        "cwd": str(tmp_path),
        "output_file": str(tmp_path / "out.log"),
        "host_state_file": str(tmp_path / "host.json"),
        "completion_notice_id": "",
        "termination": {"cleanup": {"confirmed": False, "instances": [prior_receipt]}},
    }


def test_stop_process_session_reconciles_prior_receipts_by_pid_and_birth_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F01: reusing the same numeric PID with a different birth_token does not mask prior retained receipts."""
    store = ProcessSessionStore(tmp_path / "sessions")
    prior_receipt = {
        "method": "SIGTERM",
        "confirmed": False,
        "return_code": None,
        "observed_processes": 1,
        "tree_observation_complete": False,
        "unresolved_pids": [424242],
        "unresolved_instances": [{"pid": 424242, "birth_token": "bt-old"}],
    }
    written = store.write(
        _sample_process_record(
            "bg-f01",
            pid=424242,
            birth_token="bt-new",
            prior_receipt=prior_receipt,
            tmp_path=tmp_path,
        )
    )

    monkeypatch.setattr(
        process_session_cleanup,
        "_terminate_frozen_instances",
        lambda *_args, **_kwargs: (
            ProcessTerminationReceipt(
                method="SIGTERM",
                confirmed=False,
                return_code=None,
                observed_processes=1,
                unresolved_pids=(424242,),
                unresolved_instances=({"pid": 424242, "birth_token": "bt-new"},),
            ),
        ),
    )

    cleanup = process_session_cleanup.stop_process_session(store, written)
    assert cleanup.confirmed is False
    instances = cleanup.record["termination"]["cleanup"]["instances"]
    assert len(instances) == 2
    birth_tokens = {
        inst.get("birth_token")
        for receipt in instances
        for inst in (receipt.get("unresolved_instances") or ())
        if isinstance(inst, dict)
    }
    assert birth_tokens == {"bt-old", "bt-new"}

    # Bare legacy prior receipt (missing birth_token -> (pid, None)) is also preserved when current uses (pid, "bt-new").
    store_bare = ProcessSessionStore(tmp_path / "sessions-bare")
    prior_bare = {
        "method": "SIGTERM",
        "confirmed": False,
        "return_code": None,
        "observed_processes": 1,
        "unresolved_pids": [424242],
        "unresolved_instances": [],
    }
    written_bare = store_bare.write(
        _sample_process_record(
            "bg-f01-bare",
            pid=424242,
            birth_token="bt-new",
            prior_receipt=prior_bare,
            tmp_path=tmp_path,
        )
    )
    cleanup_bare = process_session_cleanup.stop_process_session(store_bare, written_bare)
    assert len(cleanup_bare.record["termination"]["cleanup"]["instances"]) == 2


def test_stop_process_session_deduplicates_matching_pid_birth_token_or_missing_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F01: identical (pid, birth_token) or matching tokenless (pid, None) deduplicate; tokenless stays distinct from tokenized."""
    keys_old_none = process_session_cleanup._receipt_unresolved_identity_keys(
        {"unresolved_pids": [777], "unresolved_instances": []}
    )
    keys_new_token = process_session_cleanup._receipt_unresolved_identity_keys(
        {"unresolved_pids": [777], "unresolved_instances": [{"pid": 777, "birth_token": "bt-new"}]}
    )
    assert keys_old_none == {(777, None)}
    assert keys_new_token == {(777, "bt-new")}
    assert keys_old_none - keys_new_token == {(777, None)}

    store = ProcessSessionStore(tmp_path / "sessions-dedup")
    prior_same = {
        "method": "SIGTERM",
        "confirmed": False,
        "return_code": None,
        "observed_processes": 1,
        "tree_observation_complete": False,
        "unresolved_pids": [888],
        "unresolved_instances": [{"pid": 888, "birth_token": "bt-same"}],
    }
    written = store.write(
        _sample_process_record(
            "bg-f01-dedup",
            pid=888,
            birth_token="bt-same",
            prior_receipt=prior_same,
            tmp_path=tmp_path,
        )
    )

    monkeypatch.setattr(
        process_session_cleanup,
        "_terminate_frozen_instances",
        lambda *_args, **_kwargs: (
            ProcessTerminationReceipt(
                method="SIGTERM",
                confirmed=False,
                return_code=None,
                observed_processes=1,
                unresolved_pids=(888,),
                unresolved_instances=({"pid": 888, "birth_token": "bt-same"},),
            ),
        ),
    )

    cleanup = process_session_cleanup.stop_process_session(store, written)
    instances = cleanup.record["termination"]["cleanup"]["instances"]
    assert len(instances) == 1


def test_write_scope_error_and_apply_patch_preserve_space_containing_paths(tmp_path: Path) -> None:
    """F07: WriteScopeError.target_path and ApplyPatchTool preserve spaces in denied paths."""
    owner_root = tmp_path / "owner root"
    workspace_root = owner_root / "ws"
    workspace_root.mkdir(parents=True)

    inside_owner_outside_ws = owner_root / "dir with spaces" / "target file.txt"
    inside_owner_outside_ws.parent.mkdir(parents=True)
    inside_owner_outside_ws.write_text("hello\n", encoding="utf-8")

    # Compatibility check: positional access_code plus keyword-only target_path
    compat_err = WriteScopeError("denied", "WRITE_FORBIDDEN", target_path="/a b/c d.txt")
    assert compat_err.access_code == "WRITE_FORBIDDEN"
    assert compat_err.target_path == "/a b/c d.txt"

    tool = ApplyPatchTool(
        workspace_root,
        access_options=FileSystemAccessOptions(owner_scope_root=str(owner_root)),
    )
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            f"*** Update File: {inside_owner_outside_ws}",
            "@@",
            "-hello",
            "+world",
            "*** End Patch",
        ]
    )
    outcome = tool.execute({"patch": patch_text})
    assert outcome.ok is False
    assert outcome.error_code == "WRITE_FORBIDDEN"
    assert outcome.result_envelope["denied_path"] == str(inside_owner_outside_ws.resolve(strict=False))

    outside_owner = tmp_path / "outside owner" / "outside note.txt"
    outside_owner.parent.mkdir(parents=True)
    outside_owner.write_text("before\n", encoding="utf-8")
    patch_outside = "\n".join(
        [
            "*** Begin Patch",
            f"*** Update File: {outside_owner}",
            "@@",
            "-before",
            "+after",
            "*** End Patch",
        ]
    )
    outcome_outside = tool.execute({"patch": patch_outside})
    assert outcome_outside.ok is False
    assert outcome_outside.error_code == "WRITE_FORBIDDEN"
    assert outcome_outside.result_envelope["denied_path"] == str(outside_owner.resolve(strict=False))


def test_persona_write_guard_agents_md_denial_uses_agents_write_preview_guidance(tmp_path: Path) -> None:
    """LIVE4V-P2-02: AGENTS.md denial guidance points to /agents show + /agents write preview flow."""
    agents_msg = _persona_approval_write_error(tmp_path / "AGENTS.md", protected_root=tmp_path)
    assert "/agents show" in agents_msg
    assert '/agents write "..."' in agents_msg
    assert "update_persona target=agents" not in agents_msg
    assert "自主完成" not in agents_msg

    soul_msg = _persona_approval_write_error(tmp_path / "SOUL.md", protected_root=tmp_path)
    assert "update_persona target=soul" in soul_msg


def test_installer_linux_bwrap_preflight_passes_ro_bind_and_dev(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F08: _linux_bwrap_preflight verifies mount+dev inside userns before reporting healthy."""
    prefix = tmp_path / "prefix"
    bundled = (
        prefix
        / "current"
        / "runtime"
        / "lib"
        / "python3.14"
        / "site-packages"
        / "agent_py_agent"
        / "vendor"
        / "bin"
        / "bwrap.linux-x86_64"
    )
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"#!/bin/sh\n")

    recorded: list[list[str]] = []

    def ok_bwrap(argv, **kwargs):  # noqa: ANN001, ARG001
        recorded.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer.subprocess, "run", ok_bwrap)
    warning = installer._linux_bwrap_preflight(prefix)
    assert warning is None
    assert recorded == [[str(bundled), "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-user", "--", "true"]]


def test_unmanaged_shell_outcome_passes_request_operation_id_to_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10: _commit_protected_shell_outcome forwards request.operation_id on unmanaged settlement."""
    workspace = tmp_path / "workspace"
    output_dir = workspace / "outputs"
    output_dir.mkdir(parents=True)
    note_path = output_dir / "notes.custom"
    note_path.write_text("old content", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=note_path,
            artifact_id="notes",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="custom",
            status="ready",
            source="test",
        )
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    tool = ShellTool(workspace, options=ShellToolOptions(artifact_backup_root=backup_root))

    seen: list[dict[str, object]] = []
    real_settle = shell.settle_shell_artifact_operation

    def spy_settle(backup_root_arg: Path, *, run_scope: str, operation_id: str, operation_key: str):
        seen.append(
            {
                "backup_root": backup_root_arg,
                "run_scope": run_scope,
                "operation_id": operation_id,
                "operation_key": operation_key,
            }
        )
        return real_settle(
            backup_root_arg,
            run_scope=run_scope,
            operation_id=operation_id,
            operation_key=operation_key,
        )

    monkeypatch.setattr(shell, "settle_shell_artifact_operation", spy_settle)
    outcome = tool.execute(
        {
            "command": "printf ok",
            "__operation_id": "op-f10-unmanaged",
            "__operation_managed": False,
        }
    )
    assert outcome.ok is True
    assert len(seen) == 1
    assert seen[0]["operation_id"] == "op-f10-unmanaged"
    assert seen[0]["operation_key"] != ""


def test_probe_binary_stat_memo_reuse_invalidation_failures_and_lru_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI-6: _probe_binary stores positive results by stat, invalidates on update, skips failures, bounds LRU at 16."""
    bin_path = tmp_path / "bwrap"
    bin_path.write_bytes(b"v1")
    link_path = tmp_path / "bwrap-link"
    link_path.symlink_to(bin_path)

    run_calls: list[list[str]] = []
    next_returncode = 0
    next_stdout = "bubblewrap 0.9.0\n"

    def fake_run(argv, **kwargs):  # noqa: ANN001, ARG001
        run_calls.append(list(argv))
        return types.SimpleNamespace(returncode=next_returncode, stdout=next_stdout, stderr="err")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    first = sandbox._probe_binary(str(bin_path), timeout=3.0)
    second = sandbox._probe_binary(str(bin_path), timeout=3.0)
    via_link = sandbox._probe_binary(str(link_path), timeout=3.0)
    assert first.ready is True
    assert second == first
    assert via_link.ready is True
    assert via_link.bwrap_path == str(link_path)
    assert len(run_calls) == 1

    # Mutate file content & mtime_ns -> stat key changes -> re-probe triggered.
    bin_path.write_bytes(b"v2-updated-binary")
    st = os.stat(bin_path)
    os.utime(bin_path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    third = sandbox._probe_binary(str(bin_path), timeout=3.0)
    assert third.ready is True
    assert len(run_calls) == 2

    # Failure probes must not be stored.
    sandbox._reset_bwrap_binary_probe_state()
    next_returncode = 1
    fail1 = sandbox._probe_binary(str(bin_path), timeout=3.0)
    fail2 = sandbox._probe_binary(str(bin_path), timeout=3.0)
    assert fail1.ready is False
    assert fail2.ready is False
    assert len(run_calls) == 4

    # LRU bound of 16: probing 17 distinct binaries evicts the oldest one.
    sandbox._reset_bwrap_binary_probe_state()
    run_calls.clear()
    next_returncode = 0
    binaries: list[Path] = []
    for idx in range(17):
        candidate = tmp_path / f"bwrap-{idx}"
        candidate.write_bytes(f"bin-{idx}".encode("ascii"))
        binaries.append(candidate)
        assert sandbox._probe_binary(str(candidate), timeout=3.0).ready is True
    assert len(run_calls) == 17

    # Most recent entry (16) hits memo; oldest entry (0) was evicted.
    sandbox._probe_binary(str(binaries[16]), timeout=3.0)
    assert len(run_calls) == 17
    sandbox._probe_binary(str(binaries[0]), timeout=3.0)
    assert len(run_calls) == 18


def test_sandbox_resource_launcher_startup_succeeds_without_full_imports() -> None:
    """CI-6 safety: keeping probe state inside `not _IS_RESOURCE_LAUNCHER` keeps `-I -B -S` launcher healthy."""
    res = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(Path(sandbox.__file__).resolve()),
            "--resource-launcher",
            json.dumps({"nice_increment": 0, "max_memory_mb": 0, "max_processes": 0, "oom_score_adj": 0}),
            sys.executable,
            "-c",
            "print('launcher-ok')",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert res.returncode == 0, f"stderr={res.stderr!r}"
    assert "launcher-ok" in res.stdout
