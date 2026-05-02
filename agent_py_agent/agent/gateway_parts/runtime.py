from __future__ import annotations

"""LLM: runs gateway request submission, status rendering, queue workers, and ask handling.

给人看的解释：
这个文件是 gateway 的主运行时。
它负责投递 ask 请求、等待响应、显示状态、处理 pending 队列，并把一条请求真正交给 agent.run。
底层 JSON 读写、进程检查、恢复归档都已经拆到别的文件。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

# Liveness registry: tracks which request IDs have an active heartbeat thread.
_active_heartbeat_request_ids: set[str] = set()

from .daemon_control import get_running_pid, read_pid_record
from .io import (
    append_gateway_history,
    gateway_request_counts,
    gateway_response_path,
    new_gateway_request_id,
    read_json_file,
    write_gateway_request,
    write_json_file,
    write_json_file_atomic,
)
from .logging import _index_gateway_payload, _report_gateway_side_effect_error, log_gateway_payload
from .paths import GatewayPaths, gateway_chunk_path, gateway_paths
from .process_control import is_pid_alive
from .recovery import _archive_gateway_request, _gateway_request_attempts

if TYPE_CHECKING:
    from ..core import SimpleAgent


def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    """LLM contract: poll for a gateway response JSON until timeout.

    Human version:
    `gateway ask` 写入请求后会等这个响应文件。`--no-wait` 跳过等待，用户之后可以用 request_id 再查。
    """

    path = gateway_response_path(paths, request_id)
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        payload = read_json_file(path)
        if payload:
            return payload
        time.sleep(0.2)
    return {}


def print_gateway_response(payload: dict, *, json_mode: bool = False, show_prompt: bool = False) -> int:
    """LLM contract: render a gateway response for CLI users.

    Human version:
    默认打印人看的回复；`--json` 打印完整结构，方便脚本或外部工具继续处理。
    """

    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 2

    if show_prompt and payload.get("prompt"):
        print("===== FINAL PROMPT =====")
        print(payload.get("prompt", ""))
        print("===== RESPONSE =====")

    response = str(payload.get("response", "") or "")
    if response:
        print(response)
    else:
        print(str(payload.get("error", "gateway 请求没有返回内容。") or "gateway 请求没有返回内容。"))

    status_line = f"request_id={payload.get('id', '-')}; status={payload.get('status', '-')}; backend={payload.get('backend', '-')}; tool_rounds={payload.get('tool_rounds', 0)}; prompt_tokens≈{payload.get('prompt_token_estimate', 0)}; resume_context={1 if payload.get('memory_resume_context_injected') else 0}"
    print(f"\n[{status_line}]")
    return 0 if payload.get("ok") else 2


def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    prompt: str,
    inject: list[str] | None = None,
    prompt_files: list[str] | None = None,
    save: bool = True,
    include_prompt: bool = False,
    resume_context: bool | None = None,
    agent: SimpleAgent | None = None,
) -> tuple[str, Path, Path]:
    """LLM contract: enqueue one ask request and return request/response paths.

    Human version:
    `gateway ask` 和 `chat --gateway` 都走这里。上层只关心“我投递了一条消息”，不用知道文件队列怎么命名。
    """

    request_id = new_gateway_request_id()
    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": prompt,
        "inject": inject or [],
        "prompt_files": prompt_files or [],
        "save": save,
        "include_prompt": include_prompt,
        "created_at": time.time(),
        "client_pid": os.getpid(),
        "status": "pending",
        "attempts": 0,
    }
    if resume_context is not None:
        payload["resume_context"] = bool(resume_context)
    request_path = write_gateway_request(paths, payload)
    response_path = gateway_response_path(paths, request_id)
    if agent is not None:
        log_gateway_payload(
            agent,
            {**payload, "status": "queued", "ok": False},
            event_type="gateway_request_queued",
            request_path=request_path,
            response_path=response_path,
        )
    return request_id, request_path, response_path


def gateway_running(paths: GatewayPaths) -> tuple[int, bool]:
    """LLM contract: return gateway pid and liveness flag.

    Human version:
    `pid` 是文件里记录的进程号，`alive` 是系统层面确认它还活着。两者分开返回，方便状态页说清楚。
    """

    pid = get_running_pid(paths.pid)
    return pid, bool(pid)


def wait_for_gateway_running(paths: GatewayPaths, timeout: float = 10.0) -> tuple[int, bool]:
    """LLM contract: wait for a recently started gateway pid to become alive.

    Human version:
    Windows 上启动进程后，下一条命令可能太快读不到活跃 pid。这里短暂等待一下，减少 `start && ask` 的竞态。
    """

    deadline = time.time() + max(0.0, timeout)
    last_pid = 0
    while True:
        pid, alive = gateway_running(paths)
        if pid:
            last_pid = pid
        if alive:
            return pid, True
        if time.time() >= deadline:
            return pid or last_pid, False
        time.sleep(0.2)


def render_gateway_status(agent: SimpleAgent, paths: GatewayPaths) -> list[str]:
    """LLM contract: build human-readable gateway status lines.

    Human version:
    这个函数只组织状态文字，不直接解析命令行。`gateway status` 和 chat 里的 `/status` 可以复用它。
    """

    pid, alive = gateway_running(paths)
    state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    age = time.time() - heartbeat_at if heartbeat_at else 0
    stale = bool(heartbeat_at and age > agent.config.gateway_stale_seconds)
    status = "running" if alive else state.get("status", "stopped")
    if alive and stale:
        status = "stale"

    lines = [
        f"gateway status={status} pid={pid if pid else '-'} alive={alive}",
        "gateway requests=" + json.dumps(gateway_request_counts(paths), ensure_ascii=False, sort_keys=True),
    ]
    if heartbeat_at:
        lines.append(f"gateway heartbeat_age_seconds={age:.1f}")
    return lines


def rebuild_gateway_index(agent: SimpleAgent) -> int:
    """LLM contract: rebuild LocalStore gateway records from queue/history files.

    Human version:
    如果 LocalStore 索引丢了，gateway 的文件事实源还在。这个函数会扫历史、请求队列和响应文件，
    重新把它们写回可搜索索引。
    """

    paths = gateway_paths(agent)
    count = 0
    if paths.history.exists():
        for line in paths.history.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _index_gateway_payload(agent, payload, response_path=gateway_response_path(paths, str(payload.get("id") or ""))):
                count += 1
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed):
        for request_path in sorted(folder.glob("*.json")):
            payload = read_json_file(request_path)
            if not payload:
                continue
            request_id = str(payload.get("id") or request_path.stem)
            response_path = gateway_response_path(paths, request_id)
            response_payload = read_json_file(response_path)
            merged = {**payload, **response_payload} if response_payload else payload
            if _index_gateway_payload(agent, merged, request_path=request_path, response_path=response_path):
                count += 1
    for response_path in sorted(paths.responses.glob("*.json")):
        payload = read_json_file(response_path)
        if not payload:
            continue
        if _index_gateway_payload(agent, payload, response_path=response_path):
            count += 1
    return count


def _gateway_processing_lease_interval(agent: SimpleAgent) -> float:
    """Return a heartbeat cadence short enough to keep processing leases fresh."""

    try:
        gateway_interval = float(agent.config.gateway_heartbeat_interval or 5)
    except (TypeError, ValueError):
        gateway_interval = 5.0
    try:
        processing_timeout = float(agent.config.gateway_processing_timeout_seconds or 900)
    except (TypeError, ValueError):
        processing_timeout = 900.0
    if processing_timeout > 0:
        gateway_interval = min(gateway_interval, max(0.2, processing_timeout / 3.0))
    return max(0.2, gateway_interval)


def _touch_gateway_processing_lease(request_path: Path, *, request_id: str, worker_id: str = "") -> bool:
    """Refresh the lease heartbeat on a processing request file."""

    payload = read_json_file(request_path)
    if not payload:
        return False
    payload_id = str(payload.get("id") or request_path.stem)
    if payload_id != request_id:
        return False
    now = time.time()
    payload["id"] = payload_id
    payload["status"] = "processing"
    if worker_id:
        payload["lease_owner"] = worker_id
    else:
        payload.setdefault("lease_owner", "")
    payload.setdefault("lease_started_at", now)
    payload["lease_heartbeat_at"] = now
    payload["updated_at"] = now
    try:
        write_json_file_atomic(request_path, payload)
    except OSError as exc:
        _report_gateway_side_effect_error("gateway_lease_heartbeat", request_id, exc)
        return False
    return True


def is_heartbeat_alive_for_request(request_id: str) -> bool:
    """Check whether a heartbeat thread is currently active for the given request."""

    return request_id in _active_heartbeat_request_ids


def _start_gateway_processing_lease_heartbeat(
    agent: SimpleAgent,
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
) -> tuple[threading.Event, threading.Thread]:
    """Start a daemon thread that keeps one processing lease alive during agent.run."""

    stop_event = threading.Event()
    interval = _gateway_processing_lease_interval(agent)
    consecutive_failures = 0
    max_failures = 3
    _active_heartbeat_request_ids.add(request_id)

    def heartbeat_loop() -> None:
        nonlocal consecutive_failures
        try:
            while not stop_event.wait(interval):
                try:
                    if not _touch_gateway_processing_lease(request_path, request_id=request_id, worker_id=worker_id):
                        return
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    _report_gateway_side_effect_error("heartbeat", request_id, exc)
                    if consecutive_failures >= max_failures:
                        log_gateway_payload(
                            agent,
                            {"id": request_id, "status": "heartbeat_abandoned", "failures": consecutive_failures},
                            event_type="gateway_heartbeat_abandoned",
                            request_path=request_path,
                        )
                        return
        finally:
            _active_heartbeat_request_ids.discard(request_id)

    thread = threading.Thread(
        target=heartbeat_loop,
        name=f"gateway-lease-{request_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _process_gateway_requests(agent: SimpleAgent, paths: GatewayPaths, *, worker_id: str = "gw-worker") -> int:
    """LLM contract: claim and process all currently pending gateway requests.

    Human version:
    文件流转是 `pending -> processing -> responses + done/failed`。用 rename 表达状态变化，
    人直接看目录也能判断请求卡在哪一步。
    """

    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.processing.mkdir(parents=True, exist_ok=True)
    paths.done.mkdir(parents=True, exist_ok=True)
    paths.failed.mkdir(parents=True, exist_ok=True)
    paths.responses.mkdir(parents=True, exist_ok=True)
    processed = 0
    for request_path in sorted(paths.inbox.glob("*.json")):
        processing_path = paths.processing / request_path.name
        try:
            request_path.replace(processing_path)
        except OSError as exc:
            _report_gateway_side_effect_error("claim_gateway_request", request_path.stem, exc)
            continue
        request_payload = read_json_file(processing_path)
        request_id = str(request_payload.get("id") or processing_path.stem)
        request_payload.setdefault("id", request_id)
        response_path = gateway_response_path(paths, request_id)
        if response_path.exists():
            try:
                _archive_gateway_request(processing_path, paths.done)
            except OSError as exc:
                _report_gateway_side_effect_error("archive_duplicate_gateway_request", request_id, exc)
            processed += 1
            continue
        lease_now = time.time()
        request_payload.update(
            {
                "status": "processing",
                "attempts": _gateway_request_attempts(request_payload) + 1,
                "lease_owner": worker_id,
                "lease_started_at": lease_now,
                "lease_heartbeat_at": lease_now,
                "updated_at": lease_now,
            }
        )
        try:
            write_json_file_atomic(processing_path, request_payload)
        except OSError as exc:
            _report_gateway_side_effect_error("prepare_gateway_request_lease", request_id, exc)
            # The lease heartbeat is observability, not the user's work itself. Very deep Windows paths can make
            # the atomic temp filename too long, so keep processing the request without lease refresh instead of
            # stranding it in requests/processing.
            response = _handle_gateway_request(agent, processing_path, refresh_lease=False, worker_id=worker_id)
        else:
            response = _handle_gateway_request(agent, processing_path, refresh_lease=True, worker_id=worker_id)
        response_path = gateway_response_path(paths, str(response.get("id", processing_path.stem)))
        if not response_path.exists():
            write_json_file(response_path, response)
        append_gateway_history(paths, response)
        try:
            _archive_gateway_request(processing_path, paths.done if response.get("ok") else paths.failed)
        except OSError as exc:
            _report_gateway_side_effect_error("archive_gateway_request", request_id, exc)
        processed += 1
    return processed


def _handle_gateway_request(
    agent: SimpleAgent,
    request_path: Path,
    *,
    refresh_lease: bool = False,
    worker_id: str = "",
) -> dict:
    """LLM contract: execute one gateway request file and return response payload.

    Human version:
    现在只支持 `kind=ask`，也就是“一条用户消息 -> 一次 agent.run”。响应里会记录
    request_id、状态、耗时、backend、tool_rounds 和错误码，方便后续追踪。
    """

    request = read_json_file(request_path)
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip()
    # 兼容旧格式（无 kind 字段但有 request_id 的文件，视为 ask 类型）
    if not kind and request_id:
        kind = "ask"
    response_path = gateway_response_path(gateway_paths(agent), request_id)
    existing_response = read_json_file(response_path)
    if existing_response:
        return existing_response
    started_at = time.time()
    response = {
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
    log_gateway_payload(
        agent,
        {
            **request,
            "id": request_id,
            "kind": kind or "unknown",
            "status": "processing",
            "ok": False,
            "started_at": started_at,
        },
        event_type="gateway_request_processing",
        request_path=request_path,
        response_path=response_path,
    )
    lease_stop: threading.Event | None = None
    lease_thread: threading.Thread | None = None
    should_refresh_lease = refresh_lease or str(request.get("status") or "") == "processing"
    if should_refresh_lease:
        lease_worker = worker_id or str(request.get("lease_owner") or "")
        _touch_gateway_processing_lease(request_path, request_id=request_id, worker_id=lease_worker)
        lease_stop, lease_thread = _start_gateway_processing_lease_heartbeat(
            agent,
            request_path,
            request_id=request_id,
            worker_id=lease_worker,
        )
    # LLM: chunk file for streaming output — daemon writes chunks, CLI polls and displays.
    _paths = gateway_paths(agent)
    chunk_path = gateway_chunk_path(_paths, request_id)

    def _on_gateway_chunk(chunk: str) -> None:
        try:
            line = json.dumps({"t": time.time(), "text": chunk}, ensure_ascii=False)
            with open(chunk_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    try:
        if kind != "ask":
            response["error_code"] = "UNSUPPORTED_KIND"
            raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
        prompt = str(request.get("prompt") or request.get("goal") or "").strip()
        if not prompt:
            response["error_code"] = "EMPTY_PROMPT"
            raise ValueError("gateway ask prompt/goal 不能为空。")
        result = agent.run(
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
            on_chunk=_on_gateway_chunk,
        )
        response.update(
            {
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
            }
        )
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
        if lease_stop is not None:
            lease_stop.set()
        if lease_thread is not None:
            lease_thread.join(timeout=2)
        try:
            chunk_path.unlink(missing_ok=True)
        except OSError:
            pass
    ended_at = time.time()
    final_request = read_json_file(request_path)
    if final_request:
        response["lease_owner"] = final_request.get("lease_owner", response.get("lease_owner", ""))
        response["lease_started_at"] = final_request.get("lease_started_at", response.get("lease_started_at", 0))
        response["lease_heartbeat_at"] = final_request.get(
            "lease_heartbeat_at",
            response.get("lease_heartbeat_at", 0),
        )
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - started_at, 3)
    log_gateway_payload(
        agent,
        {**response, "prompt": request.get("prompt", "")},
        event_type="gateway_request_completed" if response.get("ok") else "gateway_request_failed",
        request_path=request_path,
        response_path=response_path,
    )
    return response
