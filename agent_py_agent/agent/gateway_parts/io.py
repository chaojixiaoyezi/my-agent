from __future__ import annotations

"""LLM: provides deterministic JSON-file queue IO helpers for gateway protocol files.

给人看的解释：
这个文件只管 gateway 文件队列的基础读写。
比如写请求文件、读响应文件、数队列里有多少 JSON、追加历史流水。
这里不执行业务，只保证文件格式和移动规则稳定。
"""

import json
import time
import uuid
from pathlib import Path

from ..file_io import append_jsonl
from .paths import GatewayPaths


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
