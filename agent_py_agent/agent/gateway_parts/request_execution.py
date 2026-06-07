
from __future__ import annotations

"""execution helpers keep one claimed gateway request inside focused contexts.

Gateway responses expose both per-turn and cumulative token estimates. CLI/TUI
status should use the cumulative field when showing current context pressure.
"""

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent_core.runtime_mixin import RunParams
from ..user_space.home_indexes import latest_task_refs
from .audit_service import (
    AuditRequestCompletedParams,
    audit_request_completed,
    audit_request_processing,
)
from .io import gateway_response_path, read_json_file, read_json_file_report
from .lease_service import refresh_processing_lease, start_lease_heartbeat
from .paths import gateway_chunk_path, gateway_paths
from .recovery import _gateway_request_attempts
from .request_errors import gateway_request_load_error_response
from .response_renderer import read_gateway_response_file

if TYPE_CHECKING:
    from ...core import SimpleAgent

_EMPTY_PROMPT_MESSAGE = "gateway ask prompt/goal cannot be empty"


def open_chunk_stream(chunk_path: Path) -> tuple[Path, float]:
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    return chunk_path, time.time()


def write_chunk(chunk_path: Path, text: str) -> None:
    try:
        line = json.dumps({"t": time.time(), "text": text}, ensure_ascii=False)
        with open(chunk_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def close_chunk_stream(chunk_path: Path) -> None:
    # Keep the chunk file after completion so clients that observe the final
    # response first can still drain the last streamed tokens.
    return


@dataclass(frozen=True)
class _GatewayResponseBaseContext:

    request: dict
    request_path: Path
    request_id: str
    kind: str
    started_at: float


@dataclass(frozen=True)
class _GatewayAskRunContext:

    agent: SimpleAgent
    request: dict
    request_path: Path
    response_path: Path
    request_id: str
    on_chunk: object


@dataclass(frozen=True)
class _GatewayConversationContext:

    thread_id: str = ""
    active_task_id: str = ""
    active_task_goal: str = ""
    task_workspace: str = ""
    output_dir: str = ""
    work_dir: str = ""
    load_errors: tuple[dict, ...] = ()


@dataclass(frozen=True)
class _GatewayLeaseStartContext:

    agent: SimpleAgent
    request: dict
    request_path: Path
    request_id: str
    refresh_lease: bool
    worker_id: str


def _build_gateway_response_base(context: _GatewayResponseBaseContext) -> dict:
    request = context.request
    return {
        "id": context.request_id,
        "kind": context.kind or "unknown",
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": context.started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error_code": "",
        "error": "",
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(context.request_path),
        "attempts": _gateway_request_attempts(request),
        "lease_owner": request.get("lease_owner", ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }


def _update_response_from_result(response: dict, result, request: dict) -> None:
    response.update(
        {
            "ok": True,
            "status": "done",
            "response": result.response,
            "backend": result.backend,
            "used_memories": result.used_memories,
            "tool_rounds": result.tool_rounds,
            "prompt": result.prompt if request.get("include_prompt") else "",
            "current_context_token_estimate": result.prompt_token_estimate,
            "prompt_token_estimate": result.prompt_token_estimate,
            "runtime_injection_token_estimate": result.runtime_injection_token_estimate,
            "turn_token_estimate": result.turn_token_estimate,
            "cumulative_token_estimate": result.cumulative_token_estimate,
            "memory_resume_context_injected": result.memory_resume_context_injected,
            "memory_resume_context_query": result.memory_resume_context_query,
            "memory_resume_context_matches": result.memory_resume_context_matches,
            "memory_resume_context_token_estimate": result.memory_resume_context_token_estimate,
            "memory_resume_context_error": result.memory_resume_context_error,
        }
    )


def _start_gateway_request_lease(
    context: _GatewayLeaseStartContext,
) -> tuple[threading.Event | None, threading.Thread | None]:
    should_refresh = context.refresh_lease or str(context.request.get("status") or "") == "processing"
    if not should_refresh:
        return None, None
    lease_worker = context.worker_id or str(context.request.get("lease_owner") or "")
    refresh_processing_lease(context.request_path, request_id=context.request_id, worker_id=lease_worker)
    return start_lease_heartbeat(context.agent, context.request_path, request_id=context.request_id, worker_id=lease_worker)


def _run_gateway_ask(context: _GatewayAskRunContext):
    request = context.request
    prompt = str(request.get("prompt") or request.get("goal") or "").strip()
    if not prompt:
        raise ValueError(_EMPTY_PROMPT_MESSAGE)
    conversation = _gateway_conversation_context(context.agent, request, context.request_id, prompt)
    return context.agent.run(
        prompt,
        params=_gateway_run_params(
            request,
            context,
            conversation,
            prompt,
        ),
    )


def _gateway_run_params(
    request: dict,
    context: _GatewayAskRunContext,
    conversation: _GatewayConversationContext,
    prompt: str,
) -> RunParams:
    return RunParams(
        inject=_gateway_injections(request, conversation),
        prompt_files=[str(item) for item in request.get("prompt_files", [])],
        save=bool(request.get("save", True)),
        request_id=context.request_id,
        source="gateway",
        resume_context=request.get("resume_context") if "resume_context" in request else None,
        task_attributes=_gateway_task_attributes(conversation),
        recovery_task_refs=_gateway_recovery_task_refs(conversation),
        recovery_next_actions=[
            "If this gateway request must be recovered, inspect the gateway response and LocalStore gateway_request records first."
        ],
        recovery_content_paths=[str(context.request_path), str(context.response_path)],
        on_chunk=context.on_chunk,
        root_user_prompt=_root_user_prompt(prompt, conversation),
    )


def _gateway_injections(request: dict, conversation: _GatewayConversationContext) -> list[str]:
    items = [str(item) for item in request.get("inject", [])]
    section = _conversation_prompt_section(conversation)
    return [*items, section] if section else items


def _gateway_task_attributes(conversation: _GatewayConversationContext) -> dict | None:
    attrs: dict[str, object] = {}
    if conversation.thread_id:
        attrs["conversation_thread_id"] = conversation.thread_id
    if conversation.active_task_id:
        attrs["conversation_task_id"] = conversation.active_task_id
    if conversation.task_workspace:
        attrs["run_workspace"] = {
            "task_root": conversation.task_workspace,
            "output_dir": conversation.output_dir or str(Path(conversation.task_workspace) / "output"),
            "work_dir": conversation.work_dir or str(Path(conversation.task_workspace) / "work"),
        }
    return attrs or None


def _gateway_recovery_task_refs(conversation: _GatewayConversationContext) -> list[str] | None:
    refs = [conversation.active_task_id] if conversation.active_task_id else []
    return refs or None


def _root_user_prompt(prompt: str, conversation: _GatewayConversationContext) -> str:
    return prompt


def _gateway_conversation_context(
    agent: SimpleAgent,
    request: dict,
    request_id: str,
    prompt: str,
) -> _GatewayConversationContext:
    spec = request.get("conversation")
    if not isinstance(spec, dict):
        return _GatewayConversationContext()
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return _GatewayConversationContext(load_errors=({"error_code": "conversation_store_unavailable"},))
    load_errors: list[dict] = []
    try:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": str(spec.get("canonical_user_id") or "local-agent"),
                "channel": str(spec.get("channel") or "chat"),
                "channel_conversation_id": str(spec.get("channel_conversation_id") or ""),
                "channel_user_id": str(spec.get("channel_user_id") or "local-cli"),
                "title": prompt[:80] or request_id,
            }
        )
    except Exception as exc:
        return _GatewayConversationContext(load_errors=(_conversation_error(exc, "gateway.conversation.thread"),))
    active_link = _active_thread_task(agent, thread.thread_id, request_id, load_errors)
    if active_link is None:
        _bind_gateway_request_task(store, thread.thread_id, request_id, prompt, load_errors)
    workspace = _task_workspace_for(agent, active_link.task_id if active_link is not None else "")
    return _GatewayConversationContext(
        thread_id=thread.thread_id,
        active_task_id=active_link.task_id if active_link is not None else "",
        active_task_goal=active_link.goal if active_link is not None else "",
        task_workspace=str(workspace) if workspace else "",
        output_dir=str(workspace / "output") if workspace else "",
        work_dir=str(workspace / "work") if workspace else "",
        load_errors=tuple(load_errors),
    )


def _active_thread_task(agent: SimpleAgent, thread_id: str, current_request_id: str, load_errors: list[dict]):
    store = getattr(agent, "conversation_store", None)
    try:
        links, errors = store.task_links_report(thread_id)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.task_links"))
        return None
    load_errors.extend(error for error in errors if isinstance(error, dict))
    candidates = [
        link for link in links
        if _is_root_task_link(link, current_request_id)
    ]
    return max(candidates, key=lambda item: float(getattr(item, "created_at", 0.0) or 0.0), default=None)


def _is_root_task_link(link, current_request_id: str) -> bool:
    task_id = str(getattr(link, "task_id", "") or "").strip()
    if not task_id or task_id == current_request_id or task_id.startswith("subagent-"):
        return False
    status = str(getattr(link, "status", "") or "").strip()
    return status == "active"


def _bind_gateway_request_task(store, thread_id: str, request_id: str, prompt: str, load_errors: list[dict]) -> None:
    try:
        store.bind_task(
            {
                "thread_id": thread_id,
                "task_id": request_id,
                "goal": prompt,
                "status": "active",
            }
        )
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.bind_request_task"))


def _task_workspace_for(agent: SimpleAgent, task_id: str) -> Path | None:
    if not task_id:
        return None
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return None
    owner_id = str(getattr(home_paths, "owner_id", "") or "")
    try:
        refs = latest_task_refs(home_paths, owner_id=owner_id, limit=200)
    except Exception:
        return None
    for ref in refs:
        if str(ref.get("task_id") or "") == task_id:
            path = Path(str(ref.get("task_path") or ""))
            return path if path.exists() else None
    return None


def _conversation_prompt_section(conversation: _GatewayConversationContext) -> str:
    if not conversation.thread_id:
        return ""
    lines = [
        "# Conversation Task Context",
        f"- thread_id: {conversation.thread_id}",
    ]
    if conversation.active_task_id:
        lines.append(f"- active_root_task_id: {conversation.active_task_id}")
        lines.append("- 如果用户追问后台、子代理、等待、汇总、验收或接管，默认针对 active_root_task_id 对应的任务树。")
        lines.append("- 查看子代理状态时优先查看该任务树，不要把当前 gateway request id 当成新的 root。")
    if conversation.task_workspace:
        lines.extend(
            [
                f"- task_root: {conversation.task_workspace}",
                f"- output_dir: {conversation.output_dir}",
                f"- work_dir: {conversation.work_dir}",
            ]
        )
    if conversation.load_errors:
        lines.append(f"- conversation_context_load_errors: {len(conversation.load_errors)}")
    return "\n".join(lines)


def _conversation_error(exc: BaseException, context: str) -> dict:
    from ..runtime_errors import runtime_error_report

    return runtime_error_report(exc, context=context)


def _stop_gateway_request_lease(
    lease_stop: threading.Event | None,
    lease_thread: threading.Thread | None,
) -> None:
    if lease_stop is not None:
        lease_stop.set()
    if lease_thread is not None:
        lease_thread.join(timeout=2)


def _copy_final_lease_fields(response: dict, request_path: Path) -> None:
    report = read_json_file_report(request_path, context="gateway.request_execution.final_request.read")
    if report.load_error is not None:
        response["final_request_load_error"] = report.load_error
        return
    final_request = report.payload
    if not final_request:
        return
    response["lease_owner"] = final_request.get("lease_owner", response.get("lease_owner", ""))
    response["lease_started_at"] = final_request.get("lease_started_at", response.get("lease_started_at", 0))
    response["lease_heartbeat_at"] = final_request.get(
        "lease_heartbeat_at",
        response.get("lease_heartbeat_at", 0),
    )


def _execute_gateway_request_body(context: dict, on_chunk) -> None:
    response = context["response"]
    kind = str(response.get("kind") or "").strip()
    if kind != "ask":
        response["error_code"] = "UNSUPPORTED_KIND"
        raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
    try:
        result = _run_gateway_ask(
            _GatewayAskRunContext(
                context["agent"],
                context["request"],
                context["request_path"],
                context["response_path"],
                context["request_id"],
                on_chunk,
            )
        )
    except ValueError as exc:
        if str(exc) == _EMPTY_PROMPT_MESSAGE:
            response["error_code"] = "EMPTY_PROMPT"
        raise
    _update_response_from_result(response, result, context["request"])


def _prepare_gateway_request_context(agent: SimpleAgent, request_path: Path) -> dict:
    request_report = read_json_file_report(request_path, context="gateway.request_execution.request.read")
    if request_report.load_error is not None:
        started_at = time.time()
        response = gateway_request_load_error_response(
            request_path,
            request_report.load_error,
            started_at=started_at,
        )
        context = {
            "request": {},
            "request_id": str(request_path.stem),
            "kind": "unknown",
            "started_at": started_at,
            "request_path": request_path,
            "response_path": gateway_response_path(gateway_paths(agent), request_path.stem),
            "response": response,
            "skip_execution": True,
        }
        audit_request_processing(agent, context)
        return context
    request = request_report.payload
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip() or ("ask" if request_id else "")
    response_path = gateway_response_path(gateway_paths(agent), request_id)
    existing_response = read_gateway_response_file(
        response_path,
        request_id=request_id,
        context="gateway.request_execution.response.read",
    )
    if existing_response:
        return {"existing_response": existing_response}
    started_at = time.time()
    response = _build_gateway_response_base(
        _GatewayResponseBaseContext(request, request_path, request_id, kind, started_at)
    )
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


def _finalize_gateway_response(context: dict, response: dict) -> None:
    ended_at = time.time()
    _copy_final_lease_fields(response, context["request_path"])
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - context["started_at"], 3)


def _complete_gateway_request_audit(agent: SimpleAgent, context: dict, request_path: Path, response: dict) -> None:
    audit_request_completed(
        agent,
        params=AuditRequestCompletedParams(
            response=response,
            request=context["request"],
            request_path=request_path,
            response_path=context["response_path"],
        ),
    )


def _handle_gateway_request(
    agent: SimpleAgent,
    request_path: Path,
    *,
    refresh_lease: bool = False,
    worker_id: str = "",
) -> dict:
    context = _prepare_gateway_request_context(agent, request_path)
    if context.get("existing_response"):
        return context["existing_response"]
    response = context["response"]
    if context.get("skip_execution"):
        _finalize_gateway_response(context, response)
        _complete_gateway_request_audit(agent, context, request_path, response)
        return response
    lease_stop, lease_thread = _start_gateway_request_lease(
        _GatewayLeaseStartContext(
            agent,
            context["request"],
            request_path,
            context["request_id"],
            refresh_lease,
            worker_id,
        )
    )
    chunk_path = gateway_chunk_path(gateway_paths(agent), context["request_id"])
    chunk_path_abs, _ = open_chunk_stream(chunk_path)

    try:
        _execute_gateway_request_body({**context, "agent": agent}, lambda chunk: write_chunk(chunk_path_abs, chunk))
    except Exception as exc:
        response.update(
            {
                "ok": False,
                "status": "failed",
                "error_code": response.get("error_code") or type(exc).__name__.upper(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        _stop_gateway_request_lease(lease_stop, lease_thread)
        close_chunk_stream(chunk_path_abs)
    _finalize_gateway_response(context, response)
    _complete_gateway_request_audit(agent, context, request_path, response)
    return response
