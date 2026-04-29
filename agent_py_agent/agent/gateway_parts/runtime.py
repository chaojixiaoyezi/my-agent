from __future__ import annotations

"""LLM: runs gateway request submission, status rendering, queue workers, and ask handling.

给人看的解释：
这个文件是 gateway 的主运行时。
它负责投递 ask 请求、等待响应、显示状态、处理 pending 队列，并把一条请求真正交给 agent.run。
底层 JSON 读写、进程检查、恢复归档都已经拆到别的文件。
"""

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .io import (
    append_gateway_history,
    gateway_request_counts,
    gateway_response_path,
    new_gateway_request_id,
    read_json_file,
    read_pid,
    write_gateway_request,
    write_json_file,
)
from .logging import _index_gateway_payload, _report_gateway_side_effect_error, log_gateway_payload
from .paths import GatewayPaths, gateway_paths
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

    print(
        f"\n[request_id={payload.get('id', '-')}; status={payload.get('status', '-')}; "
        f"backend={payload.get('backend', '-')}; tool_rounds={payload.get('tool_rounds', 0)}]"
    )
    return 0 if payload.get("ok") else 2


def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    prompt: str,
    inject: list[str] | None = None,
    prompt_files: list[str] | None = None,
    save: bool = True,
    include_prompt: bool = False,
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

    pid = read_pid(paths.pid)
    return pid, bool(pid and is_pid_alive(pid))


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
        response_path = gateway_response_path(paths, request_id)
        if response_path.exists():
            try:
                _archive_gateway_request(processing_path, paths.done)
            except OSError as exc:
                _report_gateway_side_effect_error("archive_duplicate_gateway_request", request_id, exc)
            processed += 1
            continue
        request_payload.update(
            {
                "status": "processing",
                "attempts": _gateway_request_attempts(request_payload) + 1,
                "lease_owner": worker_id,
                "lease_started_at": time.time(),
            }
        )
        write_json_file(processing_path, request_payload)
        response = _handle_gateway_request(agent, processing_path)
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


def _handle_gateway_request(agent: SimpleAgent, request_path: Path) -> dict:
    """LLM contract: execute one gateway request file and return response payload.

    Human version:
    现在只支持 `kind=ask`，也就是“一条用户消息 -> 一次 agent.run”。响应里会记录
    request_id、状态、耗时、backend、tool_rounds 和错误码，方便后续追踪。
    """

    request = read_json_file(request_path)
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip()
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
        response_path=gateway_response_path(gateway_paths(agent), request_id),
    )
    try:
        if kind != "ask":
            response["error_code"] = "UNSUPPORTED_KIND"
            raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
        prompt = str(request.get("prompt") or "").strip()
        if not prompt:
            response["error_code"] = "EMPTY_PROMPT"
            raise ValueError("gateway ask prompt 不能为空。")
        result = agent.run(
            prompt,
            inject=[str(item) for item in request.get("inject", [])],
            prompt_files=[str(item) for item in request.get("prompt_files", [])],
            save=bool(request.get("save", True)),
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
    ended_at = time.time()
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - started_at, 3)
    log_gateway_payload(
        agent,
        {**response, "prompt": request.get("prompt", "")},
        event_type="gateway_request_completed" if response.get("ok") else "gateway_request_failed",
        request_path=request_path,
        response_path=gateway_response_path(gateway_paths(agent), request_id),
    )
    return response
