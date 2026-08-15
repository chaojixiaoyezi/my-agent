"""TaskRegistry 并发安全(网关多 worker/多终端并发改任务状态的真实场景):CAS 恰好一次 + 终态不复活。

既有 test_task_registry_status_guard 是单线程验 SQL guard。这里真起线程压并发,验证根基:
① N 线程并发 CAS 认领同一任务 → 恰好一个成功(网关"同任务不被两 worker 双重认领"的根基);
② 任务已终态,N 线程并发想复活 → 全拒;③ 并发 register upsert 不崩不损坏。真 SQLite。
"""

from __future__ import annotations

import threading

from agent_py_agent.agent.local_storage import LocalStore


def _registry(tmp_path):
    return LocalStore(tmp_path / "local.db").task_registry


def _run(target, n: int) -> list:
    errors: list[Exception] = []
    threads = [threading.Thread(target=target, args=(errors,)) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_cas_claim_is_exactly_once(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.register_task("t", status="pending", goal="g")
    wins: list[int] = []
    lock = threading.Lock()

    def claim(errors: list) -> None:
        try:
            if reg.update_task_status("t", "running", expected_status="pending"):
                with lock:
                    wins.append(1)
        except Exception as exc:
            errors.append(exc)

    errors = _run(claim, 24)
    assert not errors, f"并发 CAS 不应出错(SQLite 锁?):{errors[:3]}"
    assert sum(wins) == 1  # ⭐ 恰好一个认领成功(CAS 原子,无双重认领)
    assert reg.lookup_task("t")["status"] == "running"


def test_concurrent_terminal_revival_all_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.register_task("t", status="done", goal="g")
    results: list[bool] = []
    lock = threading.Lock()

    def revive(errors: list) -> None:
        try:
            r = reg.update_task_status("t", "running")
            with lock:
                results.append(r)
        except Exception as exc:
            errors.append(exc)

    errors = _run(revive, 24)
    assert not errors
    assert not any(results)  # ⭐ 全 False,终态没被任何并发写复活
    assert reg.lookup_task("t")["status"] == "done"


def test_concurrent_register_upsert_no_corruption(tmp_path) -> None:
    reg = _registry(tmp_path)

    def register(errors: list) -> None:
        try:
            for i in range(10):
                reg.register_task(f"task-{i}", status="pending", goal=f"g{i}", user_id="u")
        except Exception as exc:
            errors.append(exc)

    errors = _run(register, 12)  # 12 线程都 upsert 同样的 10 个 task_id
    assert not errors, f"并发 upsert 不应出错:{errors[:3]}"
    tasks = reg.query_tasks(user_id="u", limit=100)
    assert len(tasks) == 10  # ON CONFLICT 去重,恰好 10 个,无重复无损坏
    assert all(t["status"] == "pending" for t in tasks)
