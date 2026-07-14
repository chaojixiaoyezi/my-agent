
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
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..io import append_jsonl
from ..runtime_errors import runtime_error_report
from .paths import GatewayPaths

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard.
    fcntl = None

_JSON_FILE_LOCKS: dict[str, threading.Lock] = {}
_JSON_FILE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class GatewayJsonReadReport:
    payload: dict
    load_error: dict | None = None


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _JSON_FILE_LOCKS_GUARD:
        lock = _JSON_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JSON_FILE_LOCKS[key] = lock
        return lock


def write_json_file(path: Path, payload: dict) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def write_json_file_atomic(path: Path, payload: dict) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _locked_json_path(path):
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            _replace_with_retry(tmp, path)
        finally:
            _unlink_tmp_file(tmp)


def _unlink_tmp_file(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


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


def read_json_file(path: Path) -> dict:
    return read_json_file_report(path).payload


def read_json_file_report(path: Path, *, context: str = "gateway.json.read") -> GatewayJsonReadReport:
    if not path.exists():
        return GatewayJsonReadReport({})

    try:
        with _locked_json_path(path):
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return GatewayJsonReadReport({}, _gateway_json_load_error(path, exc, context))
    if isinstance(payload, dict):
        return GatewayJsonReadReport(payload)
    return GatewayJsonReadReport(
        {},
        _gateway_json_load_error(
            path,
            ValueError(f"gateway JSON root is {type(payload).__name__}, expected object"),
            context,
        ),
    )


def _gateway_json_load_error(path: Path, exc: BaseException, context: str) -> dict:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


# LLM: require_existing closes move/update races for queue records while preserving the
# create-or-update behavior used by conversation and collaboration state files.
# 函数用途：在单个文件锁内读改写 JSON，并可要求目标必须仍然存在。
def update_json_file_atomic(
    path: Path,
    updater: Callable[[dict], dict],
    *,
    require_existing: bool = False,
) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _locked_json_path(path):
        try:
            if require_existing and not path.is_file():
                raise FileNotFoundError(path)
            current = _read_json_dict_unlocked(path)
            updated = _updated_json_dict(updater, current)
            tmp.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            _replace_with_retry(tmp, path)
            return updated
        finally:
            _unlink_tmp_file(tmp)


def _read_json_dict_unlocked(path: Path) -> dict:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return current if isinstance(current, dict) else {}


def _updated_json_dict(updater: Callable[[dict], dict], current: dict) -> dict:
    updated = updater(dict(current))
    if not isinstance(updated, dict):
        raise TypeError("update_json_file_atomic updater must return dict")
    return updated


@contextmanager
def _locked_json_path(path: Path):
    lock = _path_lock(path)
    with lock:
        with _locked_file_path(path):
            yield


@contextmanager
def _locked_file_path(path: Path):
    with _open_lock_handle(path) as handle:
        _flock_exclusive(handle)
        try:
            yield
        finally:
            _flock_unlock(handle)


def _open_lock_handle(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return lock_path.open("a+", encoding="utf-8")


def _flock_exclusive(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    else:
        from ..common.file_lock_support import warn_file_lock_unavailable_once
        warn_file_lock_unavailable_once()


def _flock_unlock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_pid(path: Path) -> int:

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def tail_lines(path: Path, line_count: int) -> list[str]:

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if line_count <= 0:
        return lines
    return lines[-line_count:]


def new_gateway_request_id() -> str:

    return f"gwreq-{int(time.time())}-{uuid.uuid4().hex}"


def gateway_response_path(paths: GatewayPaths, request_id: str) -> Path:

    return paths.responses / f"{request_id}.json"


def gateway_request_counts(paths: GatewayPaths, *, include_archives: bool = True) -> dict[str, int]:

    def count_json(path: Path) -> int:
        try:
            return len([item for item in path.glob("*.json") if item.is_file()])
        except OSError:
            return 0

    counts = {
        "pending": count_json(paths.inbox),
        "processing": count_json(paths.processing),
    }
    if include_archives:
        counts.update(
            {
                "done": count_json(paths.done),
                "failed": count_json(paths.failed),
                "responses": count_json(paths.responses),
            }
        )
    return counts


def gateway_queue_ages(paths: GatewayPaths) -> dict[str, float]:
    """结构化队列年龄观测：最老 pending 等待秒数与最老 processing lease 年龄。

    只用文件 mtime（结构化事实），不读文件内容；供 heartbeat / status 展示，
    不参与任何调度或恢复决策。"""
    now = time.time()

    def oldest_age(path: Path) -> float:
        try:
            mtimes = [item.stat().st_mtime for item in path.glob("*.json") if item.is_file()]
        except OSError:
            return 0.0
        return round(now - min(mtimes), 3) if mtimes else 0.0

    return {
        "oldest_pending_age_seconds": oldest_age(paths.inbox),
        "oldest_processing_age_seconds": oldest_age(paths.processing),
    }


def write_gateway_request(paths: GatewayPaths, payload: dict) -> Path:

    request_id = str(payload["id"])
    paths.inbox.mkdir(parents=True, exist_ok=True)
    target = paths.inbox / f"{request_id}.json"
    tmp = paths.inbox / f".{request_id}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    from ..observability.concurrency_metrics import gateway_request_enqueued

    gateway_request_enqueued()  # §6-A 进队计数(与 claimed 对比:排队饿死 vs 认领后卡首轮一眼可分)
    return target


def append_gateway_history(paths: GatewayPaths, payload: dict) -> None:

    append_jsonl(paths.history, payload, sort_keys=True)
