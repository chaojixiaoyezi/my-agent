# LLM: r5 D-1 回归：请求离开 pending/processing（认领、终态退役、流式归档）后源 <name>.lock 即刻回收，
#   不再每请求留 1~2 个孤儿锁 24h 挤进 5Hz 调度 glob；回收复用孤儿锁协议（试锁、核 inode、数据缺失）。
# 模块用途: 钉住队列伴生锁的即时回收、活锁/数据仍在时保留、迟到者互斥与回收失败不影响认领。

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import append_private_text
from agent_py_agent.agent.gateway_parts import io as gateway_io
from agent_py_agent.agent.gateway_parts import recovery
from agent_py_agent.agent.gateway_parts.io import (
    _flock_exclusive,
    _open_private_lock_descriptor,
    write_gateway_request,
)
from agent_py_agent.agent.gateway_parts.io import _locked_file_path as gateway_locked_file_path
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders


def _paths(tmp_path: Path):
    paths = gateway_paths_from_root(tmp_path / "gw")
    ensure_gateway_folders(paths)
    return paths


def _locks(folder: Path) -> list[str]:
    return sorted(name for name in os.listdir(folder) if name.endswith(".lock"))


# 函数用途: 真实认领→终态→流式归档整条主路径后，pending/processing 不再残留任何 .lock。
def test_claim_terminal_and_chunk_projection_leave_no_queue_sidecars(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    request = write_gateway_request(paths, {"id": "r1", "prompt": "hi", "status": "pending", "created_at": 1.0})
    claim = claim_request(paths, request)
    assert claim is not None and claim.path == paths.processing / "r1.json"
    assert _locks(paths.inbox) == []

    append_private_text(paths.processing / "r1.chunks.jsonl", '{"type": "delta"}\n')
    response = {"status": "completed", "response": "ok", "id": "r1"}
    recovery.terminalize_gateway_request_file(
        paths, claim.path, paths.done, "r1", terminal_response=response,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    recovery.repair_gateway_chunk_projection(paths, "r1", response)
    assert (paths.terminal / "r1.json").is_file() and (paths.done / "r1.chunks.jsonl").is_file()
    assert os.listdir(paths.processing) == [] and os.listdir(paths.inbox) == []


# 函数用途: 有人持锁或数据文件仍在时不回收；持有者释放、数据确已移走后才删除。
def test_live_holder_or_present_data_keeps_sidecar(tmp_path: Path) -> None:
    release_moved_lock_sidecar = gateway_io.release_moved_lock_sidecar
    data = tmp_path / "x.json"
    lock_path = tmp_path / "x.json.lock"
    handle = os.fdopen(_open_private_lock_descriptor(lock_path), "a+", encoding="utf-8")
    _flock_exclusive(handle)
    try:
        assert release_moved_lock_sidecar(data) is False
        assert lock_path.exists()
    finally:
        handle.close()
    data.write_text("{}", encoding="utf-8")
    assert release_moved_lock_sidecar(data) is False and lock_path.exists()
    data.unlink()
    assert release_moved_lock_sidecar(data) is True and not lock_path.exists()
    assert release_moved_lock_sidecar(data) is False and not lock_path.exists()


# 函数用途: 回收者持锁 unlink 时已打开旧 inode 的迟到者必须重开新锁，与新来者任何时刻最多一方在临界区。
def test_release_and_late_locker_never_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_moved_lock_sidecar = gateway_io.release_moved_lock_sidecar
    data = tmp_path / "x.json"
    lock_path = tmp_path / "x.json.lock"
    with gateway_locked_file_path(data):
        pass
    guard, counters, threads = threading.Lock(), {"active": 0, "max": 0}, []

    def critical() -> None:
        with guard:
            counters["active"] += 1
            counters["max"] = max(counters["max"], counters["active"])
        time.sleep(0.3)
        with guard:
            counters["active"] -= 1

    def latecomer() -> None:
        with gateway_locked_file_path(data):
            critical()

    real_unlink = Path.unlink

    def delayed_unlink(self, *args, **kwargs):
        if str(self) == str(lock_path) and not threads:
            thread = threading.Thread(target=latecomer)
            thread.start()
            threads.append(thread)
            time.sleep(0.2)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", delayed_unlink)
    try:
        assert release_moved_lock_sidecar(data) is True
    finally:
        monkeypatch.undo()
    with gateway_locked_file_path(data):
        critical()
    for thread in threads:
        thread.join()
    assert counters["max"] == 1


# 函数用途: 回收异常只降级为保留孤儿锁，绝不让已完成的认领回滚或失败。
def test_release_failure_never_breaks_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    request = write_gateway_request(paths, {"id": "r2", "prompt": "hi", "status": "pending", "created_at": 1.0})

    def boom(*_args, **_kwargs):
        raise OSError("simulated sidecar failure")

    monkeypatch.setattr(gateway_io, "_remove_orphan_lock_file", boom)
    assert gateway_io.release_moved_lock_sidecar(paths.inbox / "absent.json") is False
    claim = claim_request(paths, request)
    assert claim is not None and (paths.processing / "r2.json").is_file()
    assert not request.exists() and _locks(paths.inbox) == ["r2.json.lock"]
