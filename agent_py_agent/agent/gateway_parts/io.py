# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""provides deterministic JSON-file queue IO helpers for gateway protocol files.

这个文件只管 gateway 文件队列的基础读写。
比如写请求文件、读响应文件、数队列里有多少 JSON、追加历史流水。
这里不执行业务，只保证文件格式和移动规则稳定。
"""

import json
import threading
import time
import uuid
from pathlib import Path

from ..file_io import append_jsonl
from .paths import GatewayPaths

_JSON_FILE_LOCKS: dict[str, threading.Lock] = {}
_JSON_FILE_LOCKS_GUARD = threading.Lock()


# LLM: _path_lock 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理路径锁相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _JSON_FILE_LOCKS_GUARD:
        lock = _JSON_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JSON_FILE_LOCKS[key] = lock
        return lock


# LLM: write_json_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入JSON文件的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def write_json_file(path: Path, payload: dict) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# LLM: write_json_file_atomic 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入JSON文件atomic的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def write_json_file_atomic(path: Path, payload: dict) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _path_lock(path):
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            _replace_with_retry(tmp, path)
        finally:
            _unlink_tmp_file(tmp)


# LLM: _unlink_tmp_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理unlinktmp文件相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _unlink_tmp_file(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


# LLM: _replace_with_retry 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理replaceretry相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _replace_with_retry(tmp: Path, path: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.01 * (attempt + 1))
    if last_error is not None:
        raise last_error


# LLM: read_json_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询JSON文件需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def read_json_file(path: Path) -> dict:

    try:
        with _path_lock(path):
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: read_pid 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询pid需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def read_pid(path: Path) -> int:

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


# LLM: tail_lines 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理taillines相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def tail_lines(path: Path, line_count: int) -> list[str]:

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if line_count <= 0:
        return lines
    return lines[-line_count:]


# LLM: new_gateway_request_id 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 构建网关请求id所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def new_gateway_request_id() -> str:

    return f"gwreq-{int(time.time())}-{uuid.uuid4().hex}"


# LLM: gateway_response_path 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关响应路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def gateway_response_path(paths: GatewayPaths, request_id: str) -> Path:

    return paths.responses / f"{request_id}.json"


# LLM: gateway_request_counts 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关请求counts相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def gateway_request_counts(paths: GatewayPaths) -> dict[str, int]:

    # LLM: count_json 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 计算数量JSON的预算、数量或限制，影响后续调度节奏；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
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


# LLM: write_gateway_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入网关请求的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def write_gateway_request(paths: GatewayPaths, payload: dict) -> Path:

    request_id = str(payload["id"])
    paths.inbox.mkdir(parents=True, exist_ok=True)
    target = paths.inbox / f"{request_id}.json"
    tmp = paths.inbox / f".{request_id}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


# LLM: append_gateway_history 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入网关history的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def append_gateway_history(paths: GatewayPaths, payload: dict) -> None:

    append_jsonl(paths.history, payload, sort_keys=True)
