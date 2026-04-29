from __future__ import annotations

"""Gateway file-queue and adapter protocol boundary.

This module owns the local gateway protocol: paths, request queue movement,
response files, adapter inbox/outbox conversion, recovery, and LocalStore
indexing. In plain terms, CLI code should ask this module to move gateway work
forward instead of knowing how every request file is named or archived.
"""

import ctypes
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .file_io import append_jsonl

if TYPE_CHECKING:
    from .core import SimpleAgent


@dataclass
class GatewayPaths:
    """LLM contract: all filesystem endpoints used by the gateway queue.

    Human version:
    这里集中保存 gateway 会读写的所有文件夹和文件。比如 `inbox` 是待处理请求，
    `processing` 是正在处理的请求，`responses` 是结果。CLI 不需要自己拼路径，
    只要拿到这组对象就知道 gateway 的“现场”在哪里。
    """

    root: Path
    pid: Path
    state: Path
    heartbeat: Path
    stop_request: Path
    log: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    responses: Path
    history: Path


@dataclass
class AdapterPaths:
    """LLM contract: file-adapter inbox/outbox directory set.

    Human version:
    文件适配器是给外部聊天工具/TUI 用的。外部程序把消息 JSON 放进 `inbox`，
    my-agent 处理后把回复 JSON 放到 `outbox`。中间的 `processing/done/failed`
    让人可以直接看目录判断消息走到哪一步。
    """

    root: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    outbox: Path


def gateway_paths(agent: SimpleAgent) -> GatewayPaths:
    """LLM contract: resolve gateway control and queue paths from agent config.

    Human version:
    根据配置算出 gateway 的工作目录。测试环境可以把它指到临时目录，真实运行时
    默认落到 `agent_py_agent/data/gateway`，这样不会把路径写死在命令逻辑里。
    """

    root = agent.root / agent.config.gateway_workspace
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )


def adapter_paths(agent: SimpleAgent) -> AdapterPaths:
    """LLM contract: resolve file-adapter paths from agent config.

    Human version:
    和 `gateway_paths()` 类似，只是这里服务外部消息适配器。以后如果 adapter
    从文件协议换成别的协议，CLI 入口也不需要知道太多底层细节。
    """

    root = agent.root / agent.config.adapter_workspace
    return AdapterPaths(
        root=root,
        inbox=root / "inbox",
        processing=root / "processing",
        done=root / "done",
        failed=root / "failed",
        outbox=root / "outbox",
    )


def write_json_file(path: Path, payload: dict) -> None:
    """LLM contract: write one JSON object with deterministic formatting.

    Human version:
    统一写 JSON 文件的格式，父目录不存在就创建。这样队列里的请求、响应、状态文件
    都长得一样，人打开看也更省心。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def read_json_file(path: Path) -> dict:
    """LLM contract: read an optional JSON object; invalid/missing means empty.

    Human version:
    gateway 目录里有些文件可能还没生成，或者进程崩溃时只写了一半。这里返回空 dict
    代表“没有可用内容”，调用方再决定是跳过、重排还是失败归档。
    """

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_pid(path: Path) -> int:
    """LLM contract: parse a pid file into a positive integer or 0.

    Human version:
    pid 文件可能不存在，也可能因为旧进程退出留下脏内容。这里统一把读不到的情况
    变成 0，后面判断进程是否存活会更简单。
    """

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def is_pid_alive(pid: int) -> bool:
    """LLM contract: check whether a pid currently belongs to a live process.

    Human version:
    `gateway status` 和 `gateway stop` 都需要知道后台进程是不是真的还活着。
    Windows 和 Unix 的检查方式不一样，所以统一收口在这里。
    """

    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_pid(pid: int) -> None:
    """LLM contract: send a normal termination signal to a live pid.

    Human version:
    这是“礼貌关停”，不是强杀。进程不存在时直接返回，让 stop/restart 命令保持幂等。
    """

    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def wait_for_pid_exit(pid: int, timeout: float) -> bool:
    """LLM contract: poll for process exit until timeout.

    Human version:
    发出停止信号后等一会儿，看 gateway 有没有自己退出。超过时间还活着，调用方再决定
    要不要强制处理。
    """

    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
    return not is_pid_alive(pid)


def tail_lines(path: Path, line_count: int) -> list[str]:
    """LLM contract: return the last N text lines from a log file.

    Human version:
    `gateway logs` 只需要看末尾几行，不应该把整个日志刷屏。日志不存在时返回空列表。
    """

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if line_count <= 0:
        return lines
    return lines[-line_count:]


def new_gateway_request_id() -> str:
    """LLM contract: create a unique, time-sortable gateway request id.

    Human version:
    ID 里有时间戳和随机片段。人看到文件名能大概猜出请求时间，同时同一秒多个请求也不容易撞名。
    """

    return f"gwreq-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def gateway_response_path(paths: GatewayPaths, request_id: str) -> Path:
    """LLM contract: map request_id to its response JSON file path.

    Human version:
    所有 gateway 响应都放在 `responses/<request_id>.json`。这个小函数避免各处手写规则。
    """

    return paths.responses / f"{request_id}.json"


def gateway_request_counts(paths: GatewayPaths) -> dict[str, int]:
    """LLM contract: count JSON files in each gateway queue state.

    Human version:
    status/doctor 会显示 pending、processing、done、failed、responses 的数量。
    这些数字是快速判断 gateway 堵在哪一步的入口。
    """

    def count_json(path: Path) -> int:
        try:
            return len([item for item in path.glob("*.json") if item.is_file()])
        except OSError:
            return 0

    return {
        "pending": count_json(paths.inbox),
        "processing": count_json(paths.processing),
        "done": count_json(paths.done),
        "failed": count_json(paths.failed),
        "responses": count_json(paths.responses),
    }


def write_gateway_request(paths: GatewayPaths, payload: dict) -> Path:
    """LLM contract: atomically enqueue one gateway request JSON file.

    Human version:
    先写 `.tmp`，再 rename 成正式文件。这样 worker 不会读到半截 JSON，这是一种很便宜但很有效的文件队列保护。
    """

    request_id = str(payload["id"])
    paths.inbox.mkdir(parents=True, exist_ok=True)
    target = paths.inbox / f"{request_id}.json"
    tmp = paths.inbox / f".{request_id}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def append_gateway_history(paths: GatewayPaths, payload: dict) -> None:
    """LLM contract: append one immutable gateway history event.

    Human version:
    `gateway_requests.jsonl` 是 gateway 的流水账。每个响应追加一行，排查问题时可以按时间追踪请求发生了什么。
    """

    append_jsonl(paths.history, payload, sort_keys=True)


def log_gateway_payload(
    agent: SimpleAgent,
    payload: dict,
    *,
    event_type: str,
    request_path: Path | None = None,
    response_path: Path | None = None,
) -> None:
    """LLM contract: index a gateway request/response payload into LocalStore.

    Human version:
    文件队列是事实源，LocalStore 是搜索索引。这里负责把 gateway 的关键请求和响应同步进索引；
    如果索引失败，会把错误写到 stderr，但不会让 gateway 请求本身失败。
    """

    request_id = str(payload.get("id") or "")
    if not request_id:
        return
    try:
        status = str(payload.get("status") or "queued")
        kind = str(payload.get("kind") or "unknown")
        content = "\n".join(
            [
                "# Gateway Request",
                f"id: {request_id}",
                f"kind: {kind}",
                f"status: {status}",
                f"ok: {payload.get('ok', '')}",
                f"backend: {payload.get('backend', '')}",
                f"tool_rounds: {payload.get('tool_rounds', '')}",
                f"prompt: {payload.get('prompt', '')}",
                f"response: {payload.get('response', '')}",
                f"error_code: {payload.get('error_code', '')}",
                f"error: {payload.get('error', '')}",
                f"request_file: {request_path or payload.get('request_file', '')}",
                f"response_file: {response_path or ''}",
                "",
                "## Payload",
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            ]
        )
        agent.local_store.log_record(
            source_type="gateway_request",
            source_id=request_id,
            title=f"Gateway {kind} {status} {request_id}",
            content=content,
            metadata={
                "request_id": request_id,
                "kind": kind,
                "status": status,
                "ok": bool(payload.get("ok", False)),
                "backend": str(payload.get("backend", "")),
                "tool_rounds": int(payload.get("tool_rounds", 0) or 0),
                "error_code": str(payload.get("error_code", "")),
                "created_at": float(payload.get("created_at", 0) or 0),
                "started_at": float(payload.get("started_at", 0) or 0),
                "ended_at": float(payload.get("ended_at", 0) or 0),
                "request_path": str(request_path or payload.get("request_file", "")),
                "response_path": str(response_path or ""),
            },
            event_type=event_type,
        )
    except Exception as exc:
        _report_gateway_side_effect_error("log_gateway_payload", request_id, exc)


def log_gateway_event(agent: SimpleAgent, event_type: str, payload: dict) -> None:
    """LLM contract: index a gateway lifecycle event into LocalStore.

    Human version:
    start/stop/restart/heartbeat 这类事件不是用户请求，但排查后台状态时很有用，所以也写入 LocalStore。
    """

    try:
        created_at = time.time()
        source_id = f"{event_type}:{created_at:.6f}"
        agent.local_store.log_record(
            source_type="gateway_event",
            source_id=source_id,
            title=f"Gateway event {event_type}",
            content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            metadata={
                "event_type": event_type,
                "status": str(payload.get("status", "")),
                "pid": int(payload.get("pid", 0) or 0),
                "created_at": created_at,
            },
            event_type=event_type,
        )
    except Exception as exc:
        _report_gateway_side_effect_error("log_gateway_event", str(payload.get("id", event_type)), exc)


def gateway_stale_processing(paths: GatewayPaths, timeout_seconds: int) -> list[dict]:
    """LLM contract: list processing requests older than the configured lease timeout.

    Human version:
    这给 local-doctor 用。它会找出那些在 `processing` 里待太久的请求，帮助我们判断 gateway 是不是卡住了。
    """

    items: list[dict] = []
    now = time.time()
    timeout_seconds = max(1, int(timeout_seconds or 1))
    for path in sorted(paths.processing.glob("*.json")):
        payload = read_json_file(path)
        started_at = _gateway_processing_started_at(payload, path)
        age = now - started_at if started_at else 0
        if started_at and age < timeout_seconds:
            continue
        items.append(
            {
                "request_id": str(payload.get("id") or path.stem),
                "path": str(path),
                "age_seconds": round(age, 1) if started_at else 0,
                "attempts": _gateway_request_attempts(payload),
            }
        )
    return items


def recover_gateway_processing_requests(
    paths: GatewayPaths,
    *,
    max_attempts: int = 2,
    timeout_seconds: int = 900,
    startup: bool = False,
    agent: SimpleAgent | None = None,
) -> dict[str, int]:
    """LLM contract: requeue or fail stale gateway processing requests.

    Human version:
    如果 gateway 崩在半路，请求会留在 `processing`。启动时我们把旧请求退回 pending；
    运行中只处理超过超时时间的请求。重试次数太多的请求会归档到 failed，并写失败响应。
    """

    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.processing.mkdir(parents=True, exist_ok=True)
    paths.failed.mkdir(parents=True, exist_ok=True)
    summary = {"requeued": 0, "failed": 0, "checked": 0}
    now = time.time()
    max_attempts = max(1, int(max_attempts or 1))
    timeout_seconds = max(1, int(timeout_seconds or 1))
    for request_path in sorted(paths.processing.glob("*.json")):
        payload = read_json_file(request_path)
        if not payload:
            payload = {"id": request_path.stem, "kind": "unknown", "created_at": 0}
        summary["checked"] += 1
        started_at = _gateway_processing_started_at(payload, request_path)
        stale = startup or not started_at or now - started_at >= timeout_seconds
        if not stale:
            continue
        attempts = _gateway_request_attempts(payload)
        if attempts >= max_attempts:
            _write_gateway_failure_response(
                paths,
                request_path,
                payload,
                status="failed",
                error_code="GATEWAY_PROCESSING_TIMEOUT",
                error=f"gateway processing timeout after {timeout_seconds}s; attempts={attempts}",
                event_type="gateway_request_processing_failed",
                agent=agent,
            )
            try:
                _archive_gateway_request(request_path, paths.failed)
            except OSError as exc:
                _report_gateway_side_effect_error("archive_gateway_failed_request", request_path.stem, exc)
                continue
            summary["failed"] += 1
            continue
        payload.update(
            {
                "status": "pending",
                "requeued_at": now,
                "last_error": (
                    "gateway restarted before request completed"
                    if startup
                    else f"gateway processing timeout after {timeout_seconds}s"
                ),
            }
        )
        try:
            write_json_file(request_path, payload)
            request_path.replace(paths.inbox / request_path.name)
        except OSError as exc:
            _report_gateway_side_effect_error("requeue_gateway_request", request_path.stem, exc)
            continue
        summary["requeued"] += 1
    return summary


def requeue_gateway_processing_requests(paths: GatewayPaths) -> int:
    """LLM contract: compatibility wrapper for startup processing recovery.

    Human version:
    老测试和场景里还会直接调用这个名字。它现在只是恢复逻辑的一个简短入口。
    """

    return recover_gateway_processing_requests(paths, startup=True)["requeued"]


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


def process_file_adapter_once(
    agent: SimpleAgent,
    *,
    gateway_paths_obj: GatewayPaths,
    adapter_paths_obj: AdapterPaths,
    timeout: float,
    limit: int = 20,
) -> int:
    """LLM contract: process one bounded batch of adapter inbox messages.

    Human version:
    外部工具把 JSON 消息放进 adapter inbox。这里每次拿一批，转成 gateway ask，
    等结果，再把回复写进 outbox。limit 防止一次循环无限处理。
    """

    for path in (
        adapter_paths_obj.inbox,
        adapter_paths_obj.processing,
        adapter_paths_obj.done,
        adapter_paths_obj.failed,
        adapter_paths_obj.outbox,
    ):
        path.mkdir(parents=True, exist_ok=True)
    processed = 0
    for message_path in sorted(adapter_paths_obj.inbox.glob("*.json")):
        if limit > 0 and processed >= limit:
            break
        processing_path = adapter_paths_obj.processing / message_path.name
        try:
            message_path.replace(processing_path)
        except OSError as exc:
            _report_gateway_side_effect_error("adapter_claim_message", message_path.stem, exc)
            continue
        payload = read_json_file(processing_path)
        message_id = _adapter_message_id(payload, processing_path)
        prompt = _adapter_message_prompt(payload)
        output_path = _adapter_output_path(adapter_paths_obj, message_id)
        started_at = time.time()
        if not prompt:
            response = {
                "id": message_id,
                "ok": False,
                "status": "failed",
                "error_code": "ADAPTER_EMPTY_PROMPT",
                "error": "adapter message 缺少 prompt/text/message/content 字段。",
                "created_at": payload.get("created_at", 0),
                "started_at": started_at,
                "ended_at": time.time(),
                "source_file": str(processing_path),
            }
            write_json_file(output_path, response)
            _archive_adapter_message(processing_path, adapter_paths_obj.failed)
            processed += 1
            continue

        request_id, request_path, gateway_response = submit_gateway_ask(
            gateway_paths_obj,
            prompt=prompt,
            inject=[str(item) for item in payload.get("inject", [])],
            prompt_files=[str(item) for item in payload.get("prompt_files", [])],
            save=not bool(payload.get("no_save", False)),
            include_prompt=bool(payload.get("include_prompt", False)),
            agent=agent,
        )
        response = wait_for_gateway_response(gateway_paths_obj, request_id, timeout)
        if not response:
            response = {
                "id": request_id,
                "kind": "ask",
                "ok": False,
                "status": "timeout",
                "error_code": "GATEWAY_TIMEOUT",
                "error": f"timeout after {timeout}s",
                "created_at": payload.get("created_at", 0),
                "started_at": started_at,
                "ended_at": time.time(),
                "response": "",
                "request_file": str(request_path),
            }
        adapter_response = {
            "adapter_message_id": message_id,
            "conversation_id": payload.get("conversation_id", ""),
            "user": payload.get("user", ""),
            "gateway_request_id": request_id,
            "gateway_request_file": str(request_path),
            "gateway_response_file": str(gateway_response),
            "source_file": str(processing_path),
            "ok": bool(response.get("ok", False)),
            "status": response.get("status", "unknown"),
            "error_code": response.get("error_code", ""),
            "response": response.get("response", ""),
            "error": response.get("error", ""),
            "payload": response,
            "created_at": payload.get("created_at", 0),
            "started_at": started_at,
            "ended_at": time.time(),
        }
        write_json_file(output_path, adapter_response)
        _archive_adapter_message(
            processing_path,
            adapter_paths_obj.done if adapter_response["ok"] else adapter_paths_obj.failed,
        )
        processed += 1
    return processed


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


def _gateway_request_attempts(payload: dict) -> int:
    """LLM contract: parse request attempt count safely.

    Human version:
    文件里的 attempts 可能缺失或是奇怪类型。这里统一转成整数，转不了就按 0 次处理。
    """

    try:
        return int(payload.get("attempts", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _gateway_processing_started_at(payload: dict, request_path: Path) -> float:
    """LLM contract: derive processing start timestamp from payload or file mtime.

    Human version:
    有些旧请求没有 lease 字段，那就按 started/updated/created 或文件修改时间兜底。
    """

    for key in ("lease_started_at", "started_at", "updated_at", "created_at"):
        try:
            value = float(payload.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    try:
        return request_path.stat().st_mtime
    except OSError:
        return 0


def _write_gateway_failure_response(
    paths: GatewayPaths,
    request_path: Path,
    payload: dict,
    *,
    status: str,
    error_code: str,
    error: str,
    event_type: str,
    agent: SimpleAgent | None = None,
) -> dict:
    """LLM contract: write a terminal failure response for a queued request.

    Human version:
    当请求重试太多或 processing 超时时，不能只把文件扔进 failed。还要写一个响应 JSON，
    让客户端可以通过 request_id 查到明确失败原因。
    """

    request_id = str(payload.get("id") or request_path.stem)
    now = time.time()
    started_at = _gateway_processing_started_at(payload, request_path) or now
    response = {
        "id": request_id,
        "kind": str(payload.get("kind") or "unknown"),
        "ok": False,
        "status": status,
        "created_at": payload.get("created_at", 0),
        "started_at": started_at,
        "ended_at": now,
        "duration_seconds": round(now - started_at, 3),
        "response": "",
        "error_code": error_code,
        "error": error,
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": _gateway_request_attempts(payload),
    }
    response_path = gateway_response_path(paths, request_id)
    if not response_path.exists():
        write_json_file(response_path, response)
    append_gateway_history(paths, response)
    if agent:
        log_gateway_payload(
            agent,
            {**response, "prompt": payload.get("prompt", "")},
            event_type=event_type,
            request_path=request_path,
            response_path=response_path,
        )
    return response


def _archive_gateway_request(path: Path, target_dir: Path) -> Path:
    """LLM contract: move a request file into a terminal archive directory.

    Human version:
    done/failed 目录里可能已经有同名文件，所以必要时会给文件名加时间戳，避免覆盖旧证据。
    """

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
    path.replace(target)
    return target


def _archive_adapter_message(path: Path, target_dir: Path) -> None:
    """LLM contract: archive an adapter message and report archive failures.

    Human version:
    adapter 消息处理完要归档。归档失败不是业务结果失败，但不能静默吞掉，所以会打到 stderr。
    """

    try:
        _archive_gateway_request(path, target_dir)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_adapter_message", path.stem, exc)


def _adapter_message_id(payload: dict, path: Path) -> str:
    """LLM contract: pick a stable adapter message id from payload or filename.

    Human version:
    外部工具可能叫 `id`，也可能叫 `message_id`。都没有时用文件名，保证 outbox 一定有稳定名字。
    """

    return str(payload.get("id") or payload.get("message_id") or path.stem).strip() or path.stem


def _adapter_message_prompt(payload: dict) -> str:
    """LLM contract: normalize adapter payload text into a prompt string.

    Human version:
    外部系统字段名不一定统一，所以按 `prompt/text/message/content` 这个顺序找第一段非空文本。
    """

    for key in ("prompt", "text", "message", "content"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _adapter_output_path(paths: AdapterPaths, message_id: str) -> Path:
    """LLM contract: map adapter message id to outbox response path.

    Human version:
    每条 adapter 输入消息都会对应 `outbox/<message_id>.json`，外部程序按这个文件取回复。
    """

    return paths.outbox / f"{message_id}.json"


def _index_gateway_payload(
    agent: SimpleAgent,
    payload: dict,
    *,
    request_path: Path | None = None,
    response_path: Path | None = None,
    event_type: str = "gateway_request_rebuilt",
) -> bool:
    """LLM contract: index one gateway payload during LocalStore rebuild.

    Human version:
    重建索引时会从很多文件里读 payload。只要能识别 request_id，就复用 gateway 日志索引逻辑写入 LocalStore。
    """

    request_id = str(payload.get("id") or (request_path.stem if request_path else "")).strip()
    if not request_id:
        return False
    log_gateway_payload(
        agent,
        {
            **payload,
            "id": request_id,
            "kind": str(payload.get("kind") or "ask"),
            "status": str(payload.get("status") or "rebuilt"),
            "ok": bool(payload.get("ok", False)),
        },
        event_type=event_type,
        request_path=request_path,
        response_path=response_path,
    )
    return True


def _report_gateway_side_effect_error(operation: str, request_id: str, exc: Exception) -> None:
    """LLM contract: report non-fatal gateway side-effect failures with context.

    Human version:
    有些失败不该打断主请求，比如写索引失败、归档失败。但也不能悄悄吃掉，所以统一把
    operation、request_id、错误类型和错误内容写到 stderr。
    """

    print(
        f"gateway side-effect failed operation={operation} request_id={request_id} "
        f"error_code={type(exc).__name__} error={exc}",
        file=sys.stderr,
    )
