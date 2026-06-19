"""P8 并发一致性加固:jsonl append 并发安全(不撕行)+ CAS 自动重试。"""

import json
import threading

from agent.common.json_io import append_jsonl_records


def test_append_jsonl_concurrent_no_corruption(tmp_path):
    """8 线程并发 append 同一 jsonl:一条不丢不重,且每行都是完整 JSON(不撕裂/不交错)。"""
    path = tmp_path / "audit.jsonl"

    def worker(wid):
        for i in range(40):
            append_jsonl_records(path, [{"w": wid, "i": i, "pad": "x" * 80}])

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * 40  # 320 行,无丢无重
    for line in lines:
        json.loads(line)  # 每行完整可解析(并发未撕行)


def test_append_jsonl_batch_written_in_order(tmp_path):
    path = tmp_path / "a.jsonl"
    append_jsonl_records(path, [{"a": 1}, {"b": 2}, {"c": 3}])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[2]) == {"c": 3}


class _Cfg:
    def __init__(self, ws):
        self.subagent_workspace = str(ws)


def test_cas_update_retries_on_conflict(tmp_path):
    """CAS 版本冲突 → 自动重读重做,直到成功(mutate 每次重跑)。"""
    from agent.concurrency.exceptions import ConcurrencyConflictError
    from agent.concurrency.optimistic_lock import OptimisticLock

    lock = OptimisticLock(_Cfg(tmp_path))
    calls = {"release": 0, "mutate": 0}

    def fake_release(task_id, version):
        calls["release"] += 1
        if calls["release"] < 3:  # 前两次冲突
            raise ConcurrencyConflictError(task_id=task_id, expected_version=version, actual_version=version + 1)
        return version + 1

    lock.acquire = lambda task_id: 1
    lock.release = fake_release
    result = lock.cas_update("t1", lambda: calls.__setitem__("mutate", calls["mutate"] + 1), max_retries=5)
    assert result == 2  # 第 3 次成功
    assert calls["release"] == 3 and calls["mutate"] == 3  # 重试到第3次,mutate 每次重跑


def test_cas_update_gives_up_after_max_retries(tmp_path):
    from agent.concurrency.exceptions import ConcurrencyConflictError
    from agent.concurrency.optimistic_lock import OptimisticLock

    import pytest

    lock = OptimisticLock(_Cfg(tmp_path))
    lock.acquire = lambda task_id: 1

    def always_conflict(task_id, version):
        raise ConcurrencyConflictError(task_id=task_id, expected_version=version, actual_version=version + 1)

    lock.release = always_conflict
    with pytest.raises(ConcurrencyConflictError):
        lock.cas_update("t1", lambda: None, max_retries=3)
