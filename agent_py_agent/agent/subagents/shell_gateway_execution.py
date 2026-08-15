
from __future__ import annotations

"""Execution helpers for controlled subagent shell gateway."""

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from ..tooling.cancellation import cancellation_requested
from ..tooling.process_registry import terminate_process_tree
from .shell_gateway import (
    ShellGatewayDecision,
    ShellGatewayRequest,
    _is_relative_to,
    plan_shell_command,
)


@dataclass
class ShellGatewayExecutionResult:
    decision: ShellGatewayDecision
    executed: bool = False
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    duration_seconds: float = 0.0
    stdout_preview: str = ""
    stderr_preview: str = ""
    stdout_ref: str = ""
    stderr_ref: str = ""
    audit_ref: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass
class _ExecutionCapture:
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_total: int = 0
    stderr_total: int = 0
    timed_out: bool = False
    cancelled: bool = False


def execute_shell_command(request: ShellGatewayRequest) -> ShellGatewayExecutionResult:
    decision = plan_shell_command(request)
    result = ShellGatewayExecutionResult(decision=decision)
    if not decision.allowed:
        return result
    start = time.monotonic()
    budget = decision.output_budget
    try:
        capture = _run_subprocess_with_budget(
            decision,
            int(budget["stdout_bytes"]),
            int(budget["stderr_bytes"]),
            int(budget["timeout_seconds"]),
        )
    except Exception as exc:  # pragma: no cover - platform-specific subprocess failures.
        text = str(exc).encode()
        capture = _ExecutionCapture(exit_code=None, stderr=text, stderr_total=len(text))
    output_dir = _resolve_artifact_dir(request, Path(decision.audit["workspace_root"]))
    return _execution_result_from_capture(result, capture, output_dir, start)


def _run_subprocess_with_budget(
    decision: ShellGatewayDecision,
    stdout_limit: int,
    stderr_limit: int,
    timeout_seconds: int,
) -> _ExecutionCapture:
    process = subprocess.Popen(
        decision.argv,
        cwd=decision.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=os.name != "nt",
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        stdout_future = pool.submit(_read_limited, process.stdout, stdout_limit)
        stderr_future = pool.submit(_read_limited, process.stderr, stderr_limit)
        timed_out = False
        cancelled = False
        deadline = time.monotonic() + max(0, timeout_seconds)
        while True:
            if cancellation_requested():
                cancelled = True
                terminate_process_tree(process.pid, process)
                exit_code = process.wait()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                terminate_process_tree(process.pid, process)
                exit_code = process.wait()
                break
            try:
                exit_code = process.wait(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        stdout, stdout_total = stdout_future.result()
        stderr, stderr_total = stderr_future.result()
    return _ExecutionCapture(
        exit_code,
        stdout,
        stderr,
        stdout_total,
        stderr_total,
        timed_out,
        cancelled,
    )


def _read_limited(stream, limit: int) -> tuple[bytes, int]:
    if stream is None:
        return b"", 0
    chunks: list[bytes] = []
    total = 0
    stored = 0
    while True:
        data = stream.read(8192)
        if not data:
            break
        total += len(data)
        if stored < limit:
            chunk = data[: limit - stored]
            chunks.append(chunk)
            stored += len(chunk)
    return b"".join(chunks), total


def _execution_result_from_capture(
    result: ShellGatewayExecutionResult,
    capture: _ExecutionCapture,
    output_dir: Path,
    start: float,
) -> ShellGatewayExecutionResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    token = _artifact_token(result.decision)
    result.stdout_ref = _write_output(output_dir / f"{token}.stdout.txt", capture.stdout)
    result.stderr_ref = _write_output(output_dir / f"{token}.stderr.txt", capture.stderr)
    result.executed = True
    result.exit_code = capture.exit_code
    result.timed_out = capture.timed_out
    result.cancelled = capture.cancelled
    result.duration_seconds = round(time.monotonic() - start, 4)
    result.stdout_preview = _decode_preview(capture.stdout)
    result.stderr_preview = _decode_preview(capture.stderr)
    result.stdout_bytes = capture.stdout_total
    result.stderr_bytes = capture.stderr_total
    result.stdout_truncated = capture.stdout_total > len(capture.stdout)
    result.stderr_truncated = capture.stderr_total > len(capture.stderr)
    result.audit_ref = _write_audit(output_dir, result)
    return result


def _resolve_artifact_dir(request: ShellGatewayRequest, workspace: Path) -> Path:
    if not str(request.artifact_dir or "").strip():
        return workspace / "shell_gateway_outputs"
    candidate = Path(request.artifact_dir).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    return path if _is_relative_to(path, workspace) else workspace / "shell_gateway_outputs"


def _write_output(path: Path, data: bytes) -> str:
    if not data:
        return ""
    path.write_bytes(data)
    return str(path)


def _write_audit(output_dir: Path, result: ShellGatewayExecutionResult) -> str:
    audit_path = output_dir / "shell_gateway_audit.jsonl"
    record = asdict(result)
    record["decision"].pop("argv", None)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(audit_path)


def _artifact_token(decision: ShellGatewayDecision) -> str:
    run = str(decision.audit.get("run_id") or "run").replace("/", "_")[:40]
    req = str(decision.audit.get("request_id") or "request").replace("/", "_")[:40]
    return f"shell_{run}_{req}_{int(time.time() * 1000)}"


def _decode_preview(data: bytes) -> str:
    return data[:512].decode("utf-8", errors="replace")
