from __future__ import annotations

"""Request execution and handling for gateway.

This module is derived from runtime.py split. It contains the core request
execution logic that was previously in that file.
"""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .audit_service import (
    audit_request_completed,
    audit_request_processing,
    audit_request_queued,
)
from .chunk_service import close_chunk_stream, open_chunk_stream, write_chunk
from .io import (
    append_gateway_history,
    gateway_response_path,
    read_json_file,
    write_json_file,
)
from .lease_service import (
    refresh_processing_lease,
    start_lease_heartbeat,
)
from .paths import GatewayPaths, gateway_chunk_path, gateway_paths
from .queue_service import (
    archive_request,
    claim_request,
    ensure_gateway_folders,
)
from .recovery import _gateway_request_attempts

if TYPE_CHECKING:
    from ...core import SimpleAgent


@dataclass
class GatewayAskParams:
    """Bundle for submit_gateway_ask keyword parameters."""

    prompt: str
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool = True
    include_prompt: bool = False
    resume_context: bool | None = None
    agent: SimpleAgent | None = field(default=None, repr=False)


def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    params: GatewayAskParams,
) -> tuple[str, Path, Path]:
    """LLM contract: enqueue one ask request and return request/response paths."""
    from .io import write_gateway_request

    request_id_obj = str(time.time() * 1000)[:13]
    request_id = f"gw-{request_id_obj}"

    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": params.prompt,
        "inject": params.inject or [],
        "prompt_files": params.prompt_files or [],
        "save": params.save,
        "include_prompt": params.include_prompt,
        "created_at": time.time(),
        "client_pid": 0,
        "status": "pending",
        "attempts": 0,
    }
    if params.resume_context is not None:
        payload["resume_context"] = bool(params.resume_context)
    request_path = write_gateway_request(paths, payload)
    response_path = gateway_response_path(paths, request_id)
    if params.agent is not None:
        audit_request_queued(
            params.agent,
            {**payload, "status": "queued", "ok": False},
            request_path,
            response_path,
        )
    return request_id, request_path, response_path


def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    """LLM contract: poll for a gateway response JSON until timeout."""
    path = gateway_response_path(paths, request_id)
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        payload = read_json_file(path)
        if payload:
            return payload
        time.sleep(0.2)
    return {}


def _process_gateway_requests(agent: SimpleAgent, paths: GatewayPaths, *, worker_id: str = "gw-worker") -> int:
    """LLM contract: claim and process all currently pending gateway requests."""
    from .io import write_json_file_atomic
    from .logging import _report_gateway_side_effect_error

    ensure_gateway_folders(paths)
    processed = 0
    for request_path in sorted(paths.inbox.glob("*.json")):
        processing_path = claim_request(paths, request_path)
        if processing_path is None:
            continue
        request_payload = read_json_file(processing_path)
        request_id = str(request_payload.get("id") or processing_path.stem)
        request_payload.setdefault("id", request_id)
        response_path = gateway_response_path(paths, request_id)
        if response_path.exists():
            archive_request(processing_path, paths.done, request_id)
            processed += 1
            continue
        lease_now = time.time()
        request_payload.update({
            "status": "processing",
            "attempts": _gateway_request_attempts(request_payload) + 1,
            "lease_owner": worker_id,
            "lease_started_at": lease_now,
            "lease_heartbeat_at": lease_now,
            "updated_at": lease_now,
        })
        try:
            write_json_file_atomic(processing_path, request_payload)
        except OSError as exc:
            _report_gateway_side_effect_error("prepare_gateway_request_lease", request_id, exc)
            response = _handle_gateway_request(agent, processing_path, refresh_lease=False, worker_id=worker_id)
        else:
            response = _handle_gateway_request(agent, processing_path, refresh_lease=True, worker_id=worker_id)
        response_path = gateway_response_path(paths, str(response.get("id", processing_path.stem)))
        if not response_path.exists():
            write_json_file(response_path, response)
        append_gateway_history(paths, response)
        target_folder = paths.done if response.get("ok") else paths.failed
        archive_request(processing_path, target_folder, request_id)
        processed += 1
    return processed


def _build_gateway_response_base(
    request: dict,
    request_path: Path,
    request_id: str,
    kind: str,
    started_at: float,
) -> dict:
    """Build base gateway response dict."""
    return {
        "id": request_id,
        "kind": kind or "unknown",
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error_code": "",
        "error": "",
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": _gateway_request_attempts(request),
        "lease_owner": request.get("lease_owner", ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }


def _update_response_from_result(
    response: dict,
    result,
    request: dict,
) -> None:
    """Update response dict with successful agent.run result fields."""
    response.update({
        "ok": True,
        "status": "done",
        "response": result.response,
        "backend": result.backend,
        "used_memories": result.used_memories,
        "tool_rounds": result.tool_rounds,
        "prompt": result.prompt if request.get("include_prompt") else "",
        "prompt_token_estimate": result.prompt_token_estimate,
        "runtime_injection_token_estimate": result.runtime_injection_token_estimate,
        "recovery_snapshot_id": result.recovery_snapshot_id,
        "recovery_snapshot_path": result.recovery_snapshot_path,
        "recovery_snapshot_error": result.recovery_snapshot_error,
        "memory_resume_context_injected": result.memory_resume_context_injected,
        "memory_resume_context_query": result.memory_resume_context_query,
        "memory_resume_context_matches": result.memory_resume_context_matches,
        "memory_resume_context_token_estimate": result.memory_resume_context_token_estimate,
        "memory_resume_context_error": result.memory_resume_context_error,
    })


def _start_gateway_request_lease(
    agent: SimpleAgent,
    request: dict,
    request_path: Path,
    request_id: str,
    *,
    refresh_lease: bool,
    worker_id: str,
) -> tuple[threading.Event | None, threading.Thread | None]:
    """Start lease refresh for a processing request when needed."""
    should_refresh = refresh_lease or str(request.get("status") or "") == "processing"
    if not should_refresh:
        return None, None
    lease_worker = worker_id or str(request.get("lease_owner") or "")
    refresh_processing_lease(request_path, request_id=request_id, worker_id=lease_worker)
    return start_lease_heartbeat(
        agent,
        request_path,
        request_id=request_id,
        worker_id=lease_worker,
    )


def _run_gateway_ask(
    agent: SimpleAgent,
    request: dict,
    request_path: Path,
    response_path: Path,
    request_id: str,
    on_chunk,
):
    """Execute a validated ask request through SimpleAgent.run."""
    prompt = str(request.get("prompt") or request.get("goal") or "").strip()
    if not prompt:
        raise ValueError("gateway ask prompt/goal 不能为空。")
    return agent.run(
        prompt,
        inject=[str(item) for item in request.get("inject", [])],
        prompt_files=[str(item) for item in request.get("prompt_files", [])],
        save=bool(request.get("save", True)),
        request_id=request_id,
        source="gateway",
        recovery_snapshot=bool(request.get("save", True)),
        resume_context=request.get("resume_context") if "resume_context" in request else None,
        recovery_next_actions=["如需恢复本次 gateway 请求，先读取 gateway response 和 LocalStore gateway_request 记录。"],
        recovery_content_paths=[str(request_path), str(response_path)],
        on_chunk=on_chunk,
    )


def _stop_gateway_request_lease(
    lease_stop: threading.Event | None,
    lease_thread: threading.Thread | None,
) -> None:
    """Stop the request lease heartbeat thread if it was started."""
    if lease_stop is not None:
        lease_stop.set()
    if lease_thread is not None:
        lease_thread.join(timeout=2)


def _copy_final_lease_fields(response: dict, request_path: Path) -> None:
    """Copy final lease owner/timestamps from the processing request file."""
    final_request = read_json_file(request_path)
    if not final_request:
        return
    response["lease_owner"] = final_request.get("lease_owner", response.get("lease_owner", ""))
    response["lease_started_at"] = final_request.get("lease_started_at", response.get("lease_started_at", 0))
    response["lease_heartbeat_at"] = final_request.get(
        "lease_heartbeat_at",
        response.get("lease_heartbeat_at", 0),
    )


def _execute_gateway_request_body(context: dict, on_chunk) -> None:
    """Validate and execute the request body, updating response on success."""
    response = context["response"]
    kind = str(response.get("kind") or "").strip()
    if kind != "ask":
        response["error_code"] = "UNSUPPORTED_KIND"
        raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
    try:
        result = _run_gateway_ask(
            context["agent"],
            context["request"],
            context["request_path"],
            context["response_path"],
            context["request_id"],
            on_chunk,
        )
    except ValueError as exc:
        if str(exc) == "gateway ask prompt/goal 不能为空。":
            response["error_code"] = "EMPTY_PROMPT"
        raise
    _update_response_from_result(response, result, context["request"])


def _prepare_gateway_request_context(agent: SimpleAgent, request_path: Path) -> dict:
    """Read request metadata, build response base, and audit processing start."""
    request = read_json_file(request_path)
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip() or ("ask" if request_id else "")
    response_path = gateway_response_path(gateway_paths(agent), request_id)
    existing_response = read_json_file(response_path)
    if existing_response:
        return {"existing_response": existing_response}
    started_at = time.time()
    response = _build_gateway_response_base(request, request_path, request_id, kind, started_at)
    context = {
        "request": request,
        "request_id": request_id,
        "kind": kind,
        "started_at": started_at,
        "request_path": request_path,
        "response_path": response_path,
        "response": response,
    }
    audit_request_processing(agent, context)
    return context


def _handle_gateway_request(
    agent: SimpleAgent,
    request_path: Path,
    *,
    refresh_lease: bool = False,
    worker_id: str = "",
) -> dict:
    """LLM contract: execute one gateway request file and return response payload."""
    context = _prepare_gateway_request_context(agent, request_path)
    if context.get("existing_response"):
        return context["existing_response"]
    request = context["request"]
    request_id = context["request_id"]
    response_path = context["response_path"]
    response = context["response"]
    started_at = context["started_at"]
    lease_stop, lease_thread = _start_gateway_request_lease(
        agent,
        request,
        request_path,
        request_id,
        refresh_lease=refresh_lease,
        worker_id=worker_id,
    )
    chunk_path = gateway_chunk_path(gateway_paths(agent), request_id)
    chunk_path_abs, _ = open_chunk_stream(chunk_path)

    try:
        _execute_gateway_request_body(
            {
                **context,
                "agent": agent,
            },
            lambda chunk: write_chunk(chunk_path_abs, chunk),
        )
    except Exception as exc:
        response.update({
            "ok": False,
            "status": "failed",
            "error_code": response.get("error_code") or type(exc).__name__.upper(),
            "error": f"{type(exc).__name__}: {exc}",
        })
    finally:
        _stop_gateway_request_lease(lease_stop, lease_thread)
        close_chunk_stream(chunk_path_abs)
    ended_at = time.time()
    _copy_final_lease_fields(response, request_path)
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - started_at, 3)
    audit_request_completed(agent, response, request, request_path, response_path)
    return response
